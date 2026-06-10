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

Single-threaded by design: camera /state replies are fast (~50ms), so the
periodic tick is short and the executor stays responsive. This also avoids the
rclpy multi-threaded-logging crash and the need for locks. The only inherently
"simultaneous" action -- the two-camera shutter and clock-sync -- is done with a
barrier inside the core, so both cameras fire within ~1ms of each other.

All camera I/O lives in gopro_control.core; this node is the ROS 2 wrapper.
"""
import time

import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from std_srvs.srv import SetBool

from gopro_msgs.msg import GoProStatus, GoProSystem
from gopro_msgs.srv import GoProSettings

from gopro_control.core import settings as gp_settings
from gopro_control.core.camera import (
    discover, set_recording, sync_datetime, system_clock_synced)


class GoProManagerNode(Node):
    def __init__(self):
        super().__init__('gopro_manager')

        # --- Parameters --------------------------------------------------
        self.declare_parameter('camera_labels', ['LEFT', 'RIGHT'])   # assigned in discovery order
        self.declare_parameter('tick_period', 2.0)                   # status publish + watchdog period [s]
        self.declare_parameter('strikes_before_restart', 3)          # consecutive bad checks before acting
        self.declare_parameter('max_restart_attempts', 3)            # soft restarts before declaring FAULT
        self.declare_parameter('record_grace_period', 10.0)          # [s] after start: encoder init, watchdog waits
        self.declare_parameter('restart_cooldown', 8.0)              # [s] between soft restarts of a camera

        labels = [str(x) for x in self.get_parameter('camera_labels').value]
        self.strikes_max = int(self.get_parameter('strikes_before_restart').value)
        self.max_restarts = int(self.get_parameter('max_restart_attempts').value)
        self.grace_period = float(self.get_parameter('record_grace_period').value)
        self.restart_cooldown = float(self.get_parameter('restart_cooldown').value)

        # --- State -------------------------------------------------------
        self.cameras = discover(labels=labels)
        self.recording = False
        self.state = GoProSystem.STATE_INITIALIZING
        self.message = 'starting up'
        self._strikes = {c.label: 0 for c in self.cameras}
        self._restart_attempts = {c.label: 0 for c in self.cameras}
        self._grace_until = {c.label: 0.0 for c in self.cameras}
        self._cooldown_until = {c.label: 0.0 for c in self.cameras}
        self._faulted = {}          # label -> reason
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

        self.create_timer(float(self.get_parameter('tick_period').value), self._tick)
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
        self._publish(self._snapshot())

    def _arm(self, cam) -> bool:
        """Arm one camera for wired control, set its clock, verify it records."""
        ok = cam.init()
        cam.set_datetime()
        ready = ok and cam.shutter_works()
        if ready:
            self._ready.add(cam.label)
            self._faulted.pop(cam.label, None)
            self.get_logger().info(f'[{cam.label}] armed & verified.')
        else:
            self._ready.discard(cam.label)
            self.get_logger().warn(f'[{cam.label}] NOT ready (init={ok}). Is the SD card formatted?')
        return ready

    # =====================================================================
    # Services
    # =====================================================================
    def _on_record(self, request, response):
        """std_srvs/SetBool: data=true starts recording on all cameras, false stops."""
        n = len(self.cameras)
        if request.data:
            if self.recording:
                return self._reply(response, False, 'Already recording. Stop first ([2]) before starting again.')
            if not self.cameras:
                return self._reply(response, False, 'No camera connected.')
            if self._faulted:
                return self._reply(response, False, f'Camera(s) faulted: {sorted(self._faulted)}. Cannot start.')

            sync_datetime(self.cameras)         # same UTC second on all cams (barrier)
            results = set_recording(self.cameras, True)
            started = [lbl for lbl, ok in results.items() if ok]

            self.recording = len(started) > 0
            self._strikes = {c.label: 0 for c in self.cameras}
            self._restart_attempts = {c.label: 0 for c in self.cameras}
            self._faulted.clear()
            grace = time.monotonic() + self.grace_period   # encoder-init grace
            for c in self.cameras:
                self._grace_until[c.label] = grace
                self._cooldown_until[c.label] = 0.0

            if len(started) == n:
                msg, success = f'Recording STARTED on all {n} cameras.', True
            elif started:
                failed = [lbl for lbl, ok in results.items() if not ok]
                msg, success = f'STARTED, but {failed} failed (watchdog will retry).', True
            else:
                msg, success = 'FAILED to start recording on any camera.', False
        else:
            if not self.recording:
                self.get_logger().warn('Stop requested but state says not recording -- sending stop anyway.')
            results = set_recording(self.cameras, False)
            self.recording = False
            success = bool(results) and all(results.values())
            msg = 'Recording STOPPED on all cameras.' if success else f'Stop result: {results}'

        self._publish(self._snapshot())          # push the new state immediately (no lag)
        return self._reply(response, success, msg)

    def _on_settings(self, request, response):
        """gopro_msgs/GoProSettings: apply the same capture settings to all cameras."""
        if self.recording:
            return self._reply(response, False, 'Cannot change settings while recording. Stop first ([2]).')

        err = gp_settings.validate(
            camera_mode=request.camera_mode, resolution=request.resolution, fps=request.fps,
            fov=request.fov, hypersmooth=request.hypersmooth, wind_reduction=request.wind_reduction)
        if err:
            return self._reply(response, False, f'Rejected: {err}.')

        details, all_ok = [], True
        for cam in self.cameras:
            ok, detail = gp_settings.apply_settings(
                cam, camera_mode=request.camera_mode, resolution=request.resolution,
                fps=request.fps, fov=request.fov, hypersmooth=request.hypersmooth,
                wind_reduction=request.wind_reduction)
            all_ok = all_ok and ok
            if not ok:
                details.append(f'{cam.label}: {detail}')
        msg = 'Settings applied on all cameras.' if all_ok else '; '.join(details)
        return self._reply(response, all_ok, msg)

    def _reply(self, response, success, message):
        response.success = success
        response.message = message
        if success:                              # separate call sites: rclpy forbids
            self.get_logger().info(message)      # changing a logger's severity at the
        else:                                    # same line between calls
            self.get_logger().warn(message)
        return response

    # =====================================================================
    # Periodic tick: snapshot -> watchdog -> publish
    # =====================================================================
    def _tick(self):
        snapshot = self._snapshot()
        if self.recording:
            now = time.monotonic()
            for cam, h in snapshot:
                if cam.label in self._faulted or now < self._grace_until[cam.label]:
                    continue
                if h['recording']:
                    self._strikes[cam.label] = 0
                    continue
                if not h['sd_ok']:               # missing/full SD -> soft restart can't help
                    self._faulted[cam.label] = 'SD missing/full/unformatted'
                    self.get_logger().error(f'[{cam.label}] SD not usable -> mission compromised.')
                    continue
                self._strikes[cam.label] += 1
                self.get_logger().warn(f'[{cam.label}] not recording ({self._strikes[cam.label]}/{self.strikes_max})')
                if self._strikes[cam.label] >= self.strikes_max and now >= self._cooldown_until[cam.label]:
                    self._soft_restart(cam)
        self._publish(snapshot)

    def _soft_restart(self, cam):
        """Re-arm and restart recording on a dropped camera. No power cycling.
        After max_restart_attempts failures the camera is declared FAULT."""
        self._restart_attempts[cam.label] += 1
        attempt = self._restart_attempts[cam.label]
        self.get_logger().warn(f'[{cam.label}] soft restart (attempt {attempt}/{self.max_restarts})...')
        cam.init()
        cam.set_datetime()
        cam.start()
        self._cooldown_until[cam.label] = time.monotonic() + self.restart_cooldown
        if cam.encoding():
            self._strikes[cam.label] = 0
            self.get_logger().info(f'[{cam.label}] recording recovered.')
        elif attempt >= self.max_restarts:
            self._faulted[cam.label] = f'unrecoverable after {attempt} restarts'
            self.get_logger().error(f'[{cam.label}] UNRECOVERABLE -> mission compromised.')

    # =====================================================================
    # Snapshot + publish
    # =====================================================================
    def _snapshot(self):
        return [(cam, cam.health()) for cam in self.cameras]

    def _publish(self, snapshot):
        recording_now = 0
        for cam, h in snapshot:
            recording_now += 1 if h['recording'] else 0
            self.status_pub.publish(GoProStatus(
                label=h['label'], ip=h['ip'], reachable=h['reachable'],
                recording=h['recording'], sd_ok=h['sd_ok'],
                can_power_cycle=h['can_power_cycle']))

        n = len(self.cameras)
        if self._faulted:
            self.state = GoProSystem.STATE_FAULT
            reasons = ', '.join(f'{k} ({v})' for k, v in sorted(self._faulted.items()))
            self.message = f'MISSION COMPROMISED -- {reasons}'
        elif self.recording:
            if recording_now == n:
                self.state, self.message = GoProSystem.STATE_RECORDING, 'all cameras recording'
            else:
                self.state = GoProSystem.STATE_DEGRADED
                self.message = f'{recording_now}/{n} recording, recovering'
        elif n > 0 and len(self._ready) == n:
            self.state, self.message = GoProSystem.STATE_READY, 'all cameras ready to record'
        else:
            self.state = GoProSystem.STATE_INITIALIZING
            self.message = f'{len(self._ready)}/{n} cameras ready'

        self.system_pub.publish(GoProSystem(
            state=self.state, message=self.message, recording=self.recording,
            all_ready=(n > 0 and len(self._ready) == n),
            num_cameras=n, num_recording=recording_now))


def main(args=None):
    rclpy.init(args=args)
    exit_code = 0
    node = None
    try:
        node = GoProManagerNode()
        rclpy.spin(node)                         # single-threaded executor
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
