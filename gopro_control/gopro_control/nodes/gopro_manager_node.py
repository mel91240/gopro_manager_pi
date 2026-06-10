#!/usr/bin/env python3
"""GoPro manager node.

Owns the wired GoPro cameras of the AUV (connected through a powered USB hub)
and drives the full operational flow:

  1. On boot the cameras power up in "USB connected" mode.
  2. Arm each one for wired control (wired_usb + video preset) and set its clock
     from the Pi (which is NTP-synced before diving).
  3. Verify each camera can actually record, and expose the overall state so the
     operator menu can decide whether it is safe to dive.
  4. On the operator's command, (re)sync the clock and start/stop recording.
  5. A watchdog checks recording stays alive. If a camera drops out it is
     restarted in software ONLY (re-arm + start) -- never by cutting power, which
     can corrupt the SD card. If it cannot be recovered, the system goes FAULT so
     the surface knows the mission is compromised.

All camera I/O lives in gopro_control.core; this node is the ROS 2 wrapper.
"""
import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from std_srvs.srv import SetBool

from gopro_msgs.msg import GoProStatus, GoProSystem
from gopro_msgs.srv import GoProSettings

from gopro_control.core import settings as gp_settings
from gopro_control.core.camera import discover, set_recording, system_clock_synced


class GoProManagerNode(Node):
    def __init__(self):
        super().__init__('gopro_manager')

        # --- Parameters --------------------------------------------------
        self.declare_parameter('camera_labels', ['LEFT', 'RIGHT'])   # assigned in discovery order
        self.declare_parameter('status_period', 2.0)                 # status/state publish period [s]
        self.declare_parameter('watchdog_period', 3.0)               # recording health check period [s]
        self.declare_parameter('strikes_before_restart', 3)          # consecutive bad checks before acting
        self.declare_parameter('max_restart_attempts', 3)            # soft restarts before declaring FAULT

        labels = [str(x) for x in self.get_parameter('camera_labels').value]
        self.strikes_max = int(self.get_parameter('strikes_before_restart').value)
        self.max_restarts = int(self.get_parameter('max_restart_attempts').value)

        # --- State -------------------------------------------------------
        self.cameras = discover(labels=labels)
        self.recording = False
        self.state = GoProSystem.STATE_INITIALIZING
        self.message = 'starting up'
        self._strikes = {c.label: 0 for c in self.cameras}
        self._restart_attempts = {c.label: 0 for c in self.cameras}
        self._faulted = set()       # labels that could not be recovered
        self._ready = set()         # labels armed and verified

        if not self.cameras:
            self.get_logger().warn('No GoPro found on any USB-ethernet interface.')
        for cam in self.cameras:
            self.get_logger().info(f'Found {cam!r}')

        # --- Interfaces --------------------------------------------------
        self.create_service(SetBool, '~/record', self._on_record)
        self.create_service(GoProSettings, '~/settings', self._on_settings)
        self.status_pub = self.create_publisher(GoProStatus, '~/status', 10)
        self.system_pub = self.create_publisher(GoProSystem, '~/system', 10)

        period = float(self.get_parameter('status_period').value)
        self.create_timer(period, self._publish)
        self.create_timer(float(self.get_parameter('watchdog_period').value), self._watchdog)

        # Arm the cameras shortly after start (not in the constructor, so the node
        # is already spinning and answering while the ~10s arming runs).
        self._armed_once = False
        self.create_timer(1.0, self._arm_once)

    # =====================================================================
    # Startup: arm + time + verify
    # =====================================================================
    def _arm_once(self):
        if self._armed_once:
            return
        self._armed_once = True
        self.get_logger().info('Arming cameras...')
        if not system_clock_synced():
            self.get_logger().warn('Pi clock not NTP-synced yet; camera time will be set at record time.')
        for cam in self.cameras:
            self._arm(cam)
        self._recompute_state()

    def _arm(self, cam) -> bool:
        """Arm one camera for wired control, set its clock, and verify it records.
        Software only -- no power cycling."""
        ok = cam.init()
        cam.set_datetime()
        ready = ok and cam.shutter_works()
        if ready:
            self._ready.add(cam.label)
            self._faulted.discard(cam.label)
            self.get_logger().info(f'[{cam.label}] armed & verified.')
        else:
            self._ready.discard(cam.label)
            self.get_logger().warn(f'[{cam.label}] not ready (init={ok}). Check SD card is formatted.')
        return ready

    # =====================================================================
    # Services
    # =====================================================================
    def _on_record(self, request, response):
        """std_srvs/SetBool: data=true starts recording on all cameras, false stops."""
        if request.data:
            for cam in self.cameras:          # re-sync clock right before recording
                cam.set_datetime()
            results = set_recording(self.cameras, True)
            self.recording = any(results.values())
            self._strikes = {c.label: 0 for c in self.cameras}
            self._restart_attempts = {c.label: 0 for c in self.cameras}
            self._faulted.clear()
        else:
            results = set_recording(self.cameras, False)
            self.recording = False
        response.success = bool(results) and all(results.values())
        response.message = ('start ' if request.data else 'stop ') + str(results)
        self._recompute_state()
        (self.get_logger().info if response.success else self.get_logger().warn)(
            f'record({request.data}) -> {results}')
        return response

    def _on_settings(self, request, response):
        """gopro_msgs/GoProSettings: apply the same capture settings to all cameras."""
        details, all_ok = [], True
        for cam in self.cameras:
            ok, detail = gp_settings.apply_settings(
                cam,
                camera_mode=request.camera_mode,
                resolution=request.resolution,
                fps=request.fps,
                fov=request.fov,
                hypersmooth=request.hypersmooth,
                wind_reduction=request.wind_reduction,
            )
            all_ok = all_ok and ok
            details.append(f'{cam.label}:{detail}')
        response.success = all_ok
        response.message = '; '.join(details)
        self.get_logger().info(f'settings -> {response.message}')
        return response

    # =====================================================================
    # Watchdog (software-only recovery, never cuts power)
    # =====================================================================
    def _watchdog(self):
        if not self.recording:
            return
        for cam in self.cameras:
            if cam.label in self._faulted:
                continue
            if cam.encoding():
                self._strikes[cam.label] = 0
                continue
            self._strikes[cam.label] += 1
            self.get_logger().warn(
                f'[{cam.label}] not recording ({self._strikes[cam.label]}/{self.strikes_max})')
            if self._strikes[cam.label] >= self.strikes_max:
                self._soft_restart(cam)
        self._recompute_state()

    def _soft_restart(self, cam):
        """Re-arm and restart recording on a dropped camera. No power cycling.
        After max_restart_attempts failures the camera is declared FAULT."""
        self._restart_attempts[cam.label] += 1
        attempt = self._restart_attempts[cam.label]
        self.get_logger().warn(f'[{cam.label}] soft restart (attempt {attempt}/{self.max_restarts})...')
        cam.init()
        cam.set_datetime()
        cam.start()
        if cam.encoding():
            self._strikes[cam.label] = 0
            self.get_logger().info(f'[{cam.label}] recording recovered.')
        elif attempt >= self.max_restarts:
            self._faulted.add(cam.label)
            self.get_logger().error(
                f'[{cam.label}] UNRECOVERABLE after {attempt} restarts -> mission compromised.')

    # =====================================================================
    # State + publishing
    # =====================================================================
    def _recompute_state(self):
        n = len(self.cameras)
        recording_now = sum(1 for c in self.cameras if c.encoding()) if self.recording else 0
        if self._faulted:
            self.state = GoProSystem.STATE_FAULT
            self.message = f'cameras unrecoverable: {sorted(self._faulted)}'
        elif self.recording:
            if recording_now == n:
                self.state = GoProSystem.STATE_RECORDING
                self.message = 'all cameras recording'
            else:
                self.state = GoProSystem.STATE_DEGRADED
                self.message = f'{recording_now}/{n} recording, recovering'
        elif len(self._ready) == n and n > 0:
            self.state = GoProSystem.STATE_READY
            self.message = 'all cameras ready to record'
        else:
            self.state = GoProSystem.STATE_INITIALIZING
            self.message = f'{len(self._ready)}/{n} cameras ready'
        return recording_now

    def _publish(self):
        for cam in self.cameras:
            h = cam.health()
            self.status_pub.publish(GoProStatus(
                label=h['label'], ip=h['ip'], reachable=h['reachable'],
                recording=h['recording'], sd_ok=h['sd_ok'],
                can_power_cycle=h['can_power_cycle']))
        recording_now = self._recompute_state()
        self.system_pub.publish(GoProSystem(
            state=self.state, message=self.message, recording=self.recording,
            all_ready=(len(self._ready) == len(self.cameras) and len(self.cameras) > 0),
            num_cameras=len(self.cameras), num_recording=recording_now))


def main(args=None):
    rclpy.init(args=args)
    exit_code = 0
    node = None
    try:
        node = GoProManagerNode()
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    except Exception as e:  # noqa: BLE001
        print(f'Exception in gopro_manager node: {e}')
        exit_code = 1
    finally:
        if node is not None:
            node.destroy_node()
        rclpy.shutdown()
    return exit_code


if __name__ == '__main__':
    main()
