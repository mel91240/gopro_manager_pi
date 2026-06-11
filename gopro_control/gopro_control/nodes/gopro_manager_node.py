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
        self.declare_parameter('strikes_before_restart', 2)          # consecutive bad checks before acting
        self.declare_parameter('record_grace_period', 10.0)          # [s] after start: encoder init, watchdog waits
        self.declare_parameter('restart_cooldown', 8.0)              # [s] between recovery attempts of a camera
        self.declare_parameter('fault_after', 30.0)                  # [s] a cam lost this long -> mission compromised
        self.declare_parameter('discovery_timeout', 20.0)            # [s] wait for all cams to enumerate at boot

        self.labels = [str(x) for x in self.get_parameter('camera_labels').value]
        self.expected = len(self.labels)
        self.strikes_max = int(self.get_parameter('strikes_before_restart').value)
        self.grace_period = float(self.get_parameter('record_grace_period').value)
        self.restart_cooldown = float(self.get_parameter('restart_cooldown').value)
        self.fault_after = float(self.get_parameter('fault_after').value)
        self.discovery_timeout = float(self.get_parameter('discovery_timeout').value)

        # --- State -------------------------------------------------------
        self.cameras = []
        self.recording = False
        self.state = GoProSystem.STATE_INITIALIZING
        self.message = 'starting up'
        self._strikes = {}
        self._grace_until = {}
        self._cooldown_until = {}
        self._recovering = {}       # label -> monotonic time it dropped (soft, being retried)
        self._faulted = {}          # label -> reason (hard fault: reachable but SD unusable)
        self._ready = set()         # labels armed and verified
        self._started = False       # discovery+arm done
        self._first_seen = None     # monotonic time the first camera enumerated

        # --- Interfaces --------------------------------------------------
        self.create_service(SetBool, '~/record', self._on_record)
        self.create_service(GoProSettings, '~/settings', self._on_settings)
        self.status_pub = self.create_publisher(GoProStatus, '~/status', 10)
        self.system_pub = self.create_publisher(GoProSystem, '~/system', 10)

        self.create_timer(float(self.get_parameter('tick_period').value), self._tick)
        self.create_timer(2.0, self._startup)

    # =====================================================================
    # Startup: discover (retry) -> arm + time + verify
    # =====================================================================
    def _startup(self):
        """Discover the cameras and arm them. Retries discovery: right after a
        Pi reboot the USB-ethernet interfaces may not be up yet when this node
        starts, so a single scan would find nothing. We keep scanning, wait a
        short window for every expected camera to enumerate, then arm whatever is
        present (so one missing camera doesn't block the others forever)."""
        if self._started:
            return
        cams = discover(labels=self.labels)
        if len(cams) != len(self.cameras):
            self._set_cameras(cams)
            if cams:
                for cam in cams:
                    self.get_logger().info(f'Found {cam!r}')
                self.get_logger().info(f'Discovered {len(cams)}/{self.expected} camera(s).')
        if not self.cameras:
            return                                  # USB not up yet -- keep scanning
        if self._first_seen is None:
            self._first_seen = time.monotonic()
        have_all = len(self.cameras) >= self.expected
        waited = time.monotonic() - self._first_seen
        if not have_all and waited < self.discovery_timeout:
            return                                  # give the other camera(s) time to appear
        self._started = True
        self._arm_all()

    def _set_cameras(self, cams):
        """Adopt a (re)discovered camera list, keeping per-camera bookkeeping."""
        self.cameras = cams
        for c in cams:
            self._strikes.setdefault(c.label, 0)
            self._grace_until.setdefault(c.label, 0.0)
            self._cooldown_until.setdefault(c.label, 0.0)

    def _arm_all(self):
        if len(self.cameras) < self.expected:
            self.get_logger().warn(
                f'Only {len(self.cameras)}/{self.expected} camera(s) found -- arming those.')
        self.get_logger().info('Arming cameras...')
        if not system_clock_synced():
            self.get_logger().warn('Pi clock not NTP-synced yet; camera time will be set at record time.')
        for cam in self.cameras:
            self._arm(cam)
        if self.recording:
            # We adopted an in-progress recording (manager restarted / Pi rebooted
            # while filming). Give the watchdog a grace period so it does not
            # mistake the adoption moment for a dropout.
            grace = time.monotonic() + self.grace_period
            for c in self.cameras:
                self._grace_until[c.label] = grace
            self.get_logger().info('Adopted an in-progress recording; watchdog now active.')
        self._publish(self._snapshot())

    def _arm(self, cam) -> bool:
        """Arm one camera for wired control, set its clock, verify it records.

        If the camera is ALREADY recording (the manager was restarted while the
        AUV is in the water, or the operator reconnected mid-mission), we must
        NOT re-init or shutter-test it: that would beep loudly and could stop the
        take. Instead we adopt its running state so the operator can stop it."""
        if cam.recording_now():
            self._ready.add(cam.label)
            self._faulted.pop(cam.label, None)
            self.recording = True
            self.get_logger().info(f'[{cam.label}] already recording -- adopting state (no re-arm).')
            return True
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
            self._recovering.clear()
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
            self._recovering.clear()      # mission ended -- stop chasing dropped cams
            self._faulted.clear()
            failed = [lbl for lbl, ok in results.items() if not ok]
            success = not failed
            msg = ('Recording STOPPED on all cameras.' if success
                   else f'STOPPED; {failed} did not confirm (likely disconnected).')

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
                if now < self._grace_until[cam.label]:
                    continue
                if h['recording']:
                    self._mark_healthy(cam)
                    continue
                if h['reachable'] and h['busy']:
                    # Reachable but busy = the camera is working (e.g. rebuilding
                    # its last file after a power blip -- "SD recovery", which can
                    # take a while on a long clip). Let it finish: hold it in the
                    # recovering state but reset the clock so we do NOT raise a
                    # false emergency while it is making progress.
                    if cam.label not in self._recovering:
                        self.get_logger().warn(f'[{cam.label}] busy (recovering file?) -- waiting.')
                    self._recovering[cam.label] = now
                    continue
                # Camera dropped out of recording. Flag it right away (so the
                # operator is warned live) and keep trying to recover it.
                self._strikes[cam.label] += 1
                self._recovering.setdefault(cam.label, now)
                if self._strikes[cam.label] == 1:
                    self.get_logger().warn(
                        f'[{cam.label}] stopped recording (reachable={h["reachable"]}) -- recovering.')
                if (self._strikes[cam.label] >= self.strikes_max
                        and now >= self._cooldown_until[cam.label]):
                    self._recover(cam, now)
        self._publish(snapshot)

    def _mark_healthy(self, cam):
        """Camera is recording again -- clear any drop/fault bookkeeping."""
        if cam.label in self._recovering or cam.label in self._faulted:
            self.get_logger().info(f'[{cam.label}] recording recovered.')
        self._recovering.pop(cam.label, None)
        self._faulted.pop(cam.label, None)
        self._strikes[cam.label] = 0

    def _recover(self, cam, now):
        """Bring a dropped camera back: re-arm (out of USB-connected mode) and
        restart recording. Never gives up while the mission is recording -- a
        camera that was briefly unplugged is recovered as soon as it answers
        again. No power cycling (would corrupt the SD). A reachable camera with
        no usable SD cannot be fixed in software, so it is flagged as a hard
        fault -- but still re-checked, in case the card is swapped back."""
        self._cooldown_until[cam.label] = now + self.restart_cooldown
        if not cam.reachable(timeout=2):
            self.get_logger().warn(f'[{cam.label}] still disconnected -- will keep retrying.')
            return
        if not cam.sd_present():
            self._faulted[cam.label] = 'SD missing/full/unformatted'
            self.get_logger().error(f'[{cam.label}] reachable but SD unusable -> cannot record.')
            return
        self.get_logger().warn(f'[{cam.label}] reachable again -- re-arming and restarting recording...')
        cam.init()
        cam.set_datetime()
        cam.start()
        if cam.encoding():
            self._mark_healthy(cam)

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
        now = time.monotonic()
        if self._faulted:
            self.state = GoProSystem.STATE_FAULT
            reasons = ', '.join(f'{k} ({v})' for k, v in sorted(self._faulted.items()))
            self.message = f'MISSION COMPROMISED -- {reasons}'
        elif self._recovering:
            labels = sorted(self._recovering)
            worst = max(now - t for t in self._recovering.values())
            if worst >= self.fault_after:        # down too long -> warn the surface
                self.state = GoProSystem.STATE_FAULT
                self.message = (f'MISSION COMPROMISED -- {labels} lost '
                                f'{int(worst)}s (still retrying)')
            else:
                self.state = GoProSystem.STATE_DEGRADED
                self.message = f'{recording_now}/{n} recording, recovering {labels}'
        elif self.recording:
            self.state, self.message = GoProSystem.STATE_RECORDING, 'all cameras recording'
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
