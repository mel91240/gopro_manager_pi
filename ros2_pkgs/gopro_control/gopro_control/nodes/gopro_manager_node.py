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
import os
import time

import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile
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
        self.declare_parameter('camera_labels', ['LEFT', 'RIGHT'])   # 1st label -> 1st USB socket in (hub,port) order: hub port 2 = LEFT, hub port 4 = RIGHT (this rig's wiring; serials end ...185 / ...575)
        self.declare_parameter('tick_period', 1.0)                   # status publish + watchdog period [s]
        self.declare_parameter('strikes_before_restart', 2)          # consecutive bad checks before acting
        self.declare_parameter('fault_after_attempts', 6)            # spaced recovery attempts on a recovering cam before escalating DEGRADED -> FAULT (6 x restart_cooldown ~= fault_after)
        self.declare_parameter('unreliable_after', 3)                # distinct drop episodes for ONE cam within ONE take -> latched EMERGENCY (flapping camera)
        self.declare_parameter('record_grace_period', 10.0)          # [s] after start: encoder init, watchdog waits
        self.declare_parameter('restart_cooldown', 5.0)              # [s] between recovery attempts (a rebooting cam has time to return before the next is counted)
        self.declare_parameter('fault_after', 30.0)                  # [s] a cam lost this long -> mission compromised
        self.declare_parameter('discovery_timeout', 20.0)            # [s] wait for all cams to enumerate at boot
        self.declare_parameter('resume_on_restart', True)            # auto-resume recording after a reboot
        self.declare_parameter('vbus_recover_after', 3)              # failed soft re-arms before asking the host watcher for a Vbus cycle
        self.declare_parameter('vbus_cooldown', 45.0)                # [s] min between Vbus requests for one camera (cycle+reboot+re-arm)
        self.declare_parameter(                                      # host watcher (revive.sh) polls this for a targeted Vbus cycle
            'vbus_request_file', '/home/cosma_auv/swarm-vehicle/gopro_scripts/.revive_request')
        self.declare_parameter(                                      # persists the "was recording" intent
            'state_file', '/home/cosma_auv/swarm-vehicle/gopro_scripts/.recording_intent')
        self.declare_parameter(                                      # CLI drops a label (or "duo") here; the manager consumes it
            'solo_request_file', '/home/cosma_auv/swarm-vehicle/gopro_scripts/.solo_request')
        self.declare_parameter(                                      # manager writes the DISABLED sockets here; the host watcher keeps them powered off
            'solo_file', '/home/cosma_auv/swarm-vehicle/gopro_scripts/.solo')
        self.declare_parameter(                                      # manager publishes socket->label here so the host watcher can log [LEFT]/[RIGHT], not "socket 2-2:2"
            'socket_labels_file', '/home/cosma_auv/swarm-vehicle/gopro_scripts/.socket_labels')

        self.labels = [str(x) for x in self.get_parameter('camera_labels').value]
        self.expected = len(self.labels)
        self.strikes_max = int(self.get_parameter('strikes_before_restart').value)
        self.fault_after_attempts = int(self.get_parameter('fault_after_attempts').value)
        self.unreliable_after = int(self.get_parameter('unreliable_after').value)
        self.grace_period = float(self.get_parameter('record_grace_period').value)
        self.restart_cooldown = float(self.get_parameter('restart_cooldown').value)
        self.fault_after = float(self.get_parameter('fault_after').value)
        self.discovery_timeout = float(self.get_parameter('discovery_timeout').value)
        self.resume_on_restart = bool(self.get_parameter('resume_on_restart').value)
        self.state_file = str(self.get_parameter('state_file').value)
        self.vbus_recover_after = int(self.get_parameter('vbus_recover_after').value)
        self.vbus_cooldown = float(self.get_parameter('vbus_cooldown').value)
        self.vbus_request_file = str(self.get_parameter('vbus_request_file').value)
        self.solo_request_file = str(self.get_parameter('solo_request_file').value)
        self.solo_file = str(self.get_parameter('solo_file').value)
        self.socket_labels_file = str(self.get_parameter('socket_labels_file').value)

        # The Vbus-request and recording-intent files MUST land in a directory the
        # host watcher (revive.sh) also sees (a bind mount). If that directory is
        # missing/read-only the write silently throws and BOTH the brown-out Vbus
        # recovery and the resume-after-reboot would be dead with no symptom. Verify
        # it loudly at startup so the operator knows before diving.
        self._verify_handoff_dir()

        # --- State -------------------------------------------------------
        self.cameras = []
        self.recording = False
        self.state = GoProSystem.STATE_INITIALIZING
        self.message = 'starting up'
        self._emergency_logged = set()  # labels already logged as EMERGENCY this episode (log once, not every tick)
        self._reviving_logged = set()   # expected labels logged as reviving while idle (log once until back)
        self._drop_episodes = {}        # label -> distinct drop episodes in the CURRENT take (reset on each record start)
        self._unreliable = set()        # labels that flapped too many times this take -> latched EMERGENCY until the take ends
        self._disabled = set()          # labels deliberately powered off (solo mode): not expected, not revived, not an EMERGENCY
        self._strikes = {}
        self._grace_until = {}
        self._record_grace_until = 0.0   # post-(re)start encoder-init window; suppresses the all-lost emergency ONLY during a legitimate start, not a per-camera recovery
        self._cooldown_until = {}
        self._recovering = {}       # label -> monotonic time it dropped (soft, being retried)
        self._recover_attempts = {} # label -> recovery cycles tried (0 = drop seen, not retried yet)
        self._faulted = {}          # label -> reason (hard fault: reachable but SD unusable)
        self._vbus_cooldown_until = {}  # label -> monotonic time before the next host Vbus request is allowed
        self._ready = set()         # labels armed and verified
        self._label_by_slot = {}    # (hub,port) -> label: a socket keeps its label across swaps
        self._started = False       # discovery+arm done
        self._first_seen = None     # monotonic time the first camera enumerated

        # --- Interfaces --------------------------------------------------
        self.create_service(SetBool, '~/record', self._on_record)
        self.create_service(GoProSettings, '~/settings', self._on_settings)
        self.status_pub = self.create_publisher(GoProStatus, '~/status', 10)
        # ~/system is LATCHED (transient-local, depth 1): it is published only on a
        # state change, so a late-joining reader -- the nav, or `gopro_ctl.sh status`
        # querying an idle-but-stable rig -- must still get the current state
        # immediately instead of waiting (forever) for the next change.
        self.system_pub = self.create_publisher(
            GoProSystem, '~/system',
            QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL))

        self._load_solo()   # a solo choice persists across a manager restart / reboot

        self.create_timer(float(self.get_parameter('tick_period').value), self._tick)
        self.create_timer(2.0, self._startup)
        self.create_timer(5.0, self._rescan)
        self.create_timer(15.0, self._enforce_auto_power_off)   # keep Auto Power Off = Never

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
        cams = self._discover()
        self._write_socket_labels()                 # publish socket->label for the watcher's logs
        if self._disabled:                          # solo: don't wait for / adopt a deliberately-off camera
            cams = [c for c in cams if c.label not in self._disabled]
        if len(cams) != len(self.cameras):
            self._set_cameras(cams)
            if cams:
                for cam in cams:
                    self.get_logger().info(f'[{cam.label}] found')
        if not self.cameras:
            return                                  # USB not up yet -- keep scanning
        if self._first_seen is None:
            self._first_seen = time.monotonic()
        have_all = len(self.cameras) >= self.expected - len(self._disabled)
        waited = time.monotonic() - self._first_seen
        if not have_all and waited < self.discovery_timeout:
            return                                  # give the other camera(s) time to appear
        self._started = True
        self._arm_all()

    def _discover(self):
        """Discover cameras on the bus, each labelled by its USB socket."""
        return self._relabel(discover(labels=self.labels))

    def _relabel(self, cams):
        """Give each camera the label tied to its USB socket (hub/port), so a
        socket keeps its label (LEFT/RIGHT) across camera swaps -- even when the
        other socket is momentarily empty. A camera in a never-seen socket takes
        the next free configured label. Cameras with no resolvable socket keep
        discover()'s order-based label.

        When idle, the labels of sockets that are no longer present are released
        first, so a camera moved to a DIFFERENT socket can reuse a freed label
        instead of clashing with an empty socket's stale entry (which would leave
        two cameras sharing a label and wedge the system in INITIALIZING). The
        map is left untouched while recording (a dropped camera is recovering)."""
        present = {(c.hub, c.port) for c in cams if c.hub and c.port}
        if not self.recording:
            self._label_by_slot = {s: l for s, l in self._label_by_slot.items()
                                   if s in present}
        used = set(self._label_by_slot.values())
        for cam in cams:
            if not cam.hub or not cam.port:
                continue                            # no ppps socket -> keep order label
            slot = (cam.hub, cam.port)
            if slot not in self._label_by_slot:
                free = [l for l in self.labels if l not in used]
                self._label_by_slot[slot] = free[0] if free else cam.label
                used.add(self._label_by_slot[slot])
            cam.label = self._label_by_slot[slot]
        return cams

    def _track(self, cam):
        """Start per-camera bookkeeping with safe defaults."""
        self._strikes.setdefault(cam.label, 0)
        self._grace_until.setdefault(cam.label, 0.0)
        self._cooldown_until.setdefault(cam.label, 0.0)

    def _forget(self, cam):
        """Drop every trace of a camera whose socket left the bus."""
        self._ready.discard(cam.label)
        for d in (self._strikes, self._grace_until, self._cooldown_until,
                  self._recovering, self._recover_attempts, self._faulted,
                  self._vbus_cooldown_until):
            d.pop(cam.label, None)

    def _set_cameras(self, cams):
        """Adopt a (re)discovered camera list, keeping per-camera bookkeeping."""
        self.cameras = cams
        for c in cams:
            self._track(c)

    def _rescan(self):
        """Keep the camera set in sync with the bus (software only -- no Vbus).
        Idle: reconcile to exactly what is present, keyed by USB socket -- arm a
        camera that (re)appeared or was *swapped in*, and forget a socket that
        was unplugged, so changing cameras never leaves a ghost that blocks READY
        (no manual restart needed). Recording: only ADD a (re)appeared camera,
        never forget one -- a missing camera is being recovered, not gone."""
        if not self._started:
            return
        found = self._discover()
        self._write_socket_labels()                 # keep the watcher's socket->label map fresh
        if self._disabled:                          # solo: a powered-off camera is treated as absent
            self._sync_solo_file()                  # a disabled cam that (re)appeared becomes cuttable
            found = [c for c in found if c.label not in self._disabled]

        if self.recording:
            known = {c.ip for c in self.cameras}
            for cam in found:
                if cam.ip not in known:
                    self.cameras.append(cam)
                    self._track(cam)
                    # A camera that just (re)enumerated mid-recording needs to be
                    # re-armed + restarted before it encodes again; give it the same
                    # grace as a fresh start so the watchdog doesn't strike it
                    # immediately (premature Vbus request / FAULT on a routine USB
                    # re-enum).
                    self._grace_until[cam.label] = time.monotonic() + self.grace_period
                    self.get_logger().info(f'[{cam.label}] found')
            return

        # --- idle: reconcile to exactly what is on the bus (label == socket) ---
        dirty = False
        present = {c.label for c in found}
        for cam in self.cameras:                    # forget sockets that left
            if cam.label not in present:
                self.get_logger().info(f'[{cam.label}] unplugged')
                self._forget(cam)
                dirty = True
        old_ip = {c.label: c.ip for c in self.cameras}
        self.cameras = found
        now = time.monotonic()
        for cam in self.cameras:
            self._track(cam)
            if old_ip.get(cam.label) != cam.ip:     # new socket or a swapped-in unit
                self._ready.discard(cam.label)
                self.get_logger().info(f'[{cam.label}] found')
                dirty = True
            up = cam.reachable(timeout=1)
            if cam.label in self._ready:
                if not up:                          # a ready camera fell off the bus
                    self._ready.discard(cam.label)
                    self.get_logger().warn(f'[{cam.label}] unplugged')
                    dirty = True
                    continue
                # Reachable and believed READY -- but a brief unplug/replug (same IP,
                # within one rescan) or just sitting idle silently drops the camera
                # back to MTP mode: it still answers /version (reachable) yet is no
                # longer armed (shutter would 500), so READY would be a LIE. Re-assert
                # wired control every scan (idempotent, no beep, no recording) so a
                # camera can never stay stuck in MTP. If it refuses the command it has
                # genuinely lost wired control -> drop from READY and re-arm fully.
                if cam.enable_wired_control():
                    continue
                self._ready.discard(cam.label)
                self.get_logger().warn(f'[{cam.label}] re-arming (lost wired ctrl)')
                dirty = True
                # fall through to the re-arm path below
            if not up or now < self._cooldown_until.get(cam.label, 0.0):
                continue                            # off the bus -- wait for the revive
            self._cooldown_until[cam.label] = now + self.restart_cooldown
            if self._arm(cam):                      # came back / swapped -> re-arm to READY
                dirty = True

        # Idle visibility: an expected camera absent from the bus is being brought
        # back by the host auto-revive watcher (it power-cycles the empty socket).
        # Say so once in the uniform per-camera stream -- `[RIGHT] reviving` -- so
        # the operator is not left reading only the watcher's raw socket line;
        # `[RIGHT] found` / `armed` then close the loop when it returns. (This runs
        # only when idle: _rescan returns early while recording, where the watchdog
        # uses `recovering` -> `EMERGENCY` instead.)
        present_now = {c.label for c in self.cameras}
        for lbl in self.labels:
            if lbl in self._disabled:               # solo: deliberately off, not "missing"
                continue
            if lbl not in present_now and lbl not in self._reviving_logged:
                self.get_logger().warn(f'[{lbl}] reviving')
                self._reviving_logged.add(lbl)
        for lbl in list(self._reviving_logged):
            if lbl in present_now:
                self._reviving_logged.discard(lbl)

        if dirty:
            self._publish(self._snapshot())

    def _arm_all(self):
        want = self.expected - len(self._disabled)   # solo: don't count the powered-off camera as missing
        if len(self.cameras) < want:
            self.get_logger().warn(
                f'Only {len(self.cameras)}/{want} camera(s) found -- arming those.')
        if not system_clock_synced():
            self.get_logger().warn('clock not synced (camera time set at record)')
        for cam in self.cameras:
            self._arm(cam)
        if self.recording:
            # We adopted an in-progress recording (manager restarted / Pi rebooted
            # while filming). Give the watchdog a grace period so it does not
            # mistake the adoption moment for a dropout.
            grace = time.monotonic() + self.grace_period
            self._record_grace_until = grace
            for c in self.cameras:
                self._grace_until[c.label] = grace
            self.get_logger().info('Adopted an in-progress recording; watchdog now active.')
        elif self.resume_on_restart and self._intent_recording() and self._ready:
            # A reboot interrupted a mission (cameras stopped, but the intent flag
            # says we were filming) -> resume recording autonomously on a new
            # segment. Footage before the reboot is a separate (timestamped) file.
            self.get_logger().warn('Recording intent set (reboot during a mission) -- RESUMING recording.')
            self._resume_recording()
        self._publish(self._snapshot())

    # =====================================================================
    # Persistent recording intent (survives a reboot)
    # =====================================================================
    def _verify_handoff_dir(self):
        """Ensure the directory shared with the host watcher exists and is writable.
        Both the Vbus request file and the recording-intent file live here; a missing
        or read-only dir silently disables Vbus recovery AND resume-after-reboot."""
        for path in (self.vbus_request_file, self.state_file):
            d = os.path.dirname(path) or '.'
            try:
                os.makedirs(d, exist_ok=True)
            except OSError as e:
                self.get_logger().error(
                    f'Hand-off dir {d} could not be created ({e}); Vbus recovery and '
                    f'resume-after-reboot are DISABLED. Check the container bind mount.')
                continue
            if not os.access(d, os.W_OK):
                self.get_logger().error(
                    f'Hand-off dir {d} is not writable; Vbus recovery and '
                    f'resume-after-reboot are DISABLED. Check the container bind mount.')

    def _set_intent(self, recording: bool):
        """Persist whether a mission recording is in progress, so that after a Pi
        reboot the manager knows to resume filming (vs stay idle)."""
        try:
            if recording:
                with open(self.state_file, 'w') as f:
                    f.write('recording\n')
            elif os.path.exists(self.state_file):
                os.remove(self.state_file)
        except OSError as e:
            self.get_logger().warn(f'Could not update state file {self.state_file}: {e}')

    def _intent_recording(self) -> bool:
        return os.path.exists(self.state_file)

    def _resume_recording(self):
        """Auto-restart recording after a reboot, on the cameras that armed OK.
        Any camera still down is left to the watchdog (and raises EMERGENCY if it
        cannot be recovered), exactly like a drop during a live mission."""
        ready_cams = [c for c in self.cameras if c.label in self._ready]
        if not ready_cams:
            return
        for cam in ready_cams:                     # re-assert wired control (cams revert to MTP) before shutter
            cam.enable_wired_control()
        time.sleep(1.5)                            # let cameras leave MTP / settle before the restart,
        #                                            otherwise some reject the immediate restart
        sync_datetime(ready_cams)                  # same UTC second (barrier)
        results = set_recording(ready_cams, True)
        started = [lbl for lbl, ok in results.items() if ok]
        self.recording = len(started) > 0
        self._strikes = {c.label: 0 for c in self.cameras}
        self._recovering.clear()
        self._recover_attempts.clear()
        grace = time.monotonic() + self.grace_period
        self._record_grace_until = grace
        for c in self.cameras:
            self._grace_until[c.label] = grace
            self._cooldown_until[c.label] = 0.0
        self.get_logger().info(f'Resumed recording on {started} (new segment after reboot).')

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
        # A full/missing/unformatted SD makes the shutter test below FAIL and BEEP
        # on every re-arm cycle. Detect an unusable card explicitly and fault it
        # QUIETLY (no shutter test, no beep loop). Only when the camera actually
        # answers: a failed state read returns None and must NOT fault a good cam.
        st = cam.state()
        if st is not None and not cam._sd_usable(st):
            self._faulted[cam.label] = 'SD missing/full/unformatted'
            self._ready.discard(cam.label)
            self.get_logger().error(
                f'[{cam.label}] SD unusable (full/missing/unformatted) -> cannot record. Empty the cards ([3]).')
            return False
        # Honest readiness test: a real (brief) shutter start/stop. This actually
        # proves the camera can record -- a weaker SD-only check would call a
        # camera ready when its first real recording would fail. (A marginal
        # USB3-cabled camera may drop here, but it drops on a real recording too,
        # so this correctly flags it as not reliably recordable -> use USB2.)
        ready = ok and cam.shutter_works()
        if ready:
            self._ready.add(cam.label)
            self._faulted.pop(cam.label, None)
            self.get_logger().info(f'[{cam.label}] armed')
        else:
            self._ready.discard(cam.label)
            self.get_logger().warn(f'[{cam.label}] NOT ready (init={ok}). SD formatted / cable ok?')
        return ready

    # =====================================================================
    # Services
    # =====================================================================
    def _on_record(self, request, response):
        """std_srvs/SetBool: data=true starts recording on all cameras, false stops."""
        n = len(self.cameras)
        if request.data:
            # Report a hard fault (e.g. SD full/unusable) FIRST and by name: in that
            # state self.recording is still True, so the generic "already recording"
            # guard would otherwise hide the real reason the operator cannot start.
            if self._faulted:
                details = '; '.join(f'{lbl} ({why})' for lbl, why in sorted(self._faulted.items()))
                return self._reply(response, False,
                    f'Cannot record -- {details}. Empty the cards ([3]) or swap the SD, then retry.')
            if self.recording:
                return self._reply(response, False, 'Already recording. Stop first ([2]) before starting again.')
            if not self.cameras:
                return self._reply(response, False, 'No camera connected.')

            # Cameras silently fall back to MTP mode after sitting idle or
            # re-enumerating, which makes shutter/start return HTTP 500. Re-assert
            # wired control just before starting so [1] doesn't spuriously fail.
            for cam in self.cameras:
                cam.enable_wired_control()
            time.sleep(1.0)                     # let them leave MTP before the synchronized start
            sync_datetime(self.cameras)         # same UTC second on all cams (barrier)
            results = set_recording(self.cameras, True)
            started = [lbl for lbl, ok in results.items() if ok]

            self.recording = len(started) > 0
            if self.recording:
                self._set_intent(True)          # remember it across a reboot -> auto-resume
            self._strikes = {c.label: 0 for c in self.cameras}
            self._recovering.clear()
            self._recover_attempts.clear()
            self._faulted.clear()
            self._drop_episodes = {c.label: 0 for c in self.cameras}  # new take -> fresh flapping count
            self._unreliable.clear()
            grace = time.monotonic() + self.grace_period   # encoder-init grace
            self._record_grace_until = grace
            for c in self.cameras:
                self._grace_until[c.label] = grace
                self._cooldown_until[c.label] = 0.0

            for lbl in started:
                self.get_logger().info(f'[{lbl}] recording')
            for lbl in [l for l, ok in results.items() if not ok]:
                self.get_logger().warn(f'[{lbl}] failed to start (watchdog will retry)')
            success = len(started) > 0
            msg = (f'Recording started on {started}.' if success
                   else 'Failed to start recording on any camera.')
        else:
            if not self.recording:
                self.get_logger().warn('Stop requested but state says not recording -- sending stop anyway.')
            results = set_recording(self.cameras, False)
            self.recording = False
            self._set_intent(False)       # clean stop -> do NOT auto-resume after a reboot
            self._recovering.clear()      # mission ended -- stop chasing dropped cams
            self._recover_attempts.clear()
            self._faulted.clear()
            confirmed = [lbl for lbl, ok in results.items() if ok]
            failed = [lbl for lbl, ok in results.items() if not ok]
            success = not failed
            for lbl in confirmed:
                self.get_logger().info(f'[{lbl}] stopped recording')
            for lbl in failed:
                self.get_logger().error(f'[{lbl}] stop failed -- not confirmed (may still be filming)')
            msg = ('Recording stopped.' if success
                   else f'Stop not confirmed on {failed}.')

        self._publish(self._snapshot())          # push the new state immediately (no lag)
        return self._reply(response, success, msg)

    def _on_settings(self, request, response):
        """gopro_msgs/GoProSettings: apply the same capture settings to all cameras.

        Each camera gets exactly one journalctl line -- `[LABEL] settings applied`
        or `[LABEL] settings not applied (why)` -- so a reviewer at the surface can
        tell, per camera, whether the change took and why it did not.
        """
        if self.recording:
            for lbl in self.labels:
                self.get_logger().warn(f'[{lbl}] settings not applied (recording)')
            return self._reply(response, False, 'Cannot change settings while recording. Stop first ([2]).')

        err = gp_settings.validate(
            camera_mode=request.camera_mode, resolution=request.resolution, fps=request.fps,
            fov=request.fov, hypersmooth=request.hypersmooth, wind_reduction=request.wind_reduction)
        if err:
            for lbl in self.labels:
                self.get_logger().warn(f'[{lbl}] settings not applied ({err})')
            return self._reply(response, False, f'Rejected: {err}.')

        details, oks = [], []
        for cam in self.cameras:
            ok, detail = gp_settings.apply_settings(
                cam, camera_mode=request.camera_mode, resolution=request.resolution,
                fps=request.fps, fov=request.fov, hypersmooth=request.hypersmooth,
                wind_reduction=request.wind_reduction)
            if ok:
                oks.append(cam.label)
                self.get_logger().info(f'[{cam.label}] settings applied')
            else:
                details.append(f'{cam.label}: {detail}')
                self.get_logger().warn(f'[{cam.label}] settings not applied ({detail})')
        present = {c.label for c in self.cameras}
        for lbl in self.labels:                       # expected sockets with no camera present
            if lbl not in present:
                details.append(f'{lbl}: absent (not applied)')
                self.get_logger().warn(f'[{lbl}] settings not applied (absent)')
        all_ok = (not details) and (len(oks) == self.expected)
        if all_ok:
            msg = f'Settings applied on all {self.expected} cameras.'
        else:
            msg = (f'applied on {", ".join(oks)}; ' if oks else '') + '; '.join(details)
        return self._reply(response, all_ok, msg)

    def _reply(self, response, success, message):
        # RPC feedback for the menu ONLY -- deliberately not logged: journalctl
        # carries the per-camera event lines ([LEFT] recording, ...) instead, so the
        # service reply is never duplicated as an aggregate line.
        response.success = success
        response.message = message
        return response

    # =====================================================================
    # Solo mode (deliberately run on ONE camera; power the other off)
    # =====================================================================
    def _consume_solo_request(self):
        """The CLI (gopro_ctl.sh solo/duo) drops a label (or "duo") into
        solo_request_file. Consume it here -- on the manager's own thread, so it
        alone touches the disabled set -- once per request."""
        if not os.path.exists(self.solo_request_file):
            return
        try:
            with open(self.solo_request_file) as f:
                token = f.read().strip()
            open(self.solo_request_file, 'w').close()   # consume: one action per request
        except OSError:
            return
        if token:
            self._apply_solo(token)

    def _socket_of(self, label):
        """The USB socket (hub, port) for a label: a present camera's live socket,
        else the last-known socket for that label -- so we can still power off a
        camera that has already dropped, as long as it was seen once this boot."""
        cam = next((c for c in self.cameras if c.label == label), None)
        if cam is not None and cam.hub and cam.port:
            return (cam.hub, cam.port)
        for slot, lbl in self._label_by_slot.items():
            if lbl == label:
                return slot
        return None

    def _write_solo_file(self, sockets=None):
        """Publish the disabled sockets to solo_file for the host watcher
        ('hub:port LABEL' per line). Empty file = duo (nothing disabled)."""
        try:
            with open(self.solo_file, 'w') as f:
                for lbl, hp in (sockets or {}).items():
                    f.write(f'{hp} {lbl}\n')
        except OSError as e:
            self.get_logger().error(f'solo: could not write {self.solo_file}: {e}')

    def _sync_solo_file(self):
        """(Re)write .solo with the socket of every disabled label we can resolve,
        so a disabled camera gets its Vbus cut even if its socket was unknown when
        the solo was requested (it becomes known the moment it appears on the bus)."""
        sockets = {}
        for lbl in self._disabled:
            slot = self._socket_of(lbl)
            if slot is not None:
                sockets[lbl] = f'{slot[0]}:{slot[1]}'
        self._write_solo_file(sockets)

    def _write_socket_labels(self):
        """Publish the socket->label map so the host watcher can log [LEFT]/[RIGHT]
        instead of a raw 'socket 2-2:2'. Atomic (temp + rename) since the watcher
        reads it on every scan."""
        tmp = self.socket_labels_file + '.tmp'
        try:
            with open(tmp, 'w') as f:
                for (hub, port), lbl in self._label_by_slot.items():
                    f.write(f'{hub}:{port} {lbl}\n')
            os.replace(tmp, self.socket_labels_file)
        except OSError:
            pass

    def _load_solo(self):
        """Re-adopt a solo choice that persisted across a restart/reboot."""
        try:
            with open(self.solo_file) as f:
                for ln in f:
                    parts = ln.split()
                    if len(parts) >= 2:
                        self._disabled.add(parts[1])
        except OSError:
            return
        if self._disabled:
            active = '/'.join(l for l in self.labels if l not in self._disabled)
            self.get_logger().warn(
                f'solo persisted -- only {active} active (disabled {"/".join(sorted(self._disabled))})')

    def _apply_solo(self, token):
        """Act on a solo/duo request. 'duo' re-enables everything; a label keeps
        only that camera and disables the other(s). A disabled camera on the bus
        gets its Vbus cut; one already absent is simply no longer expected (nothing
        to cut -- solo still engages, so it stops being revived / raising EMERGENCY).
        The ONE thing we refuse is disabling a camera that is actually recording --
        stop it first, so a take is never severed."""
        t = token.strip().upper()
        if t in ('DUO', 'OFF'):
            if self._disabled:
                for lbl in sorted(self._disabled):
                    self.get_logger().info(f'[{lbl}] solo off -- re-enabled')
            self._disabled.clear()
            self._write_solo_file()          # empty -> watcher powers all back on
            return
        if t not in self.labels:
            self.get_logger().warn(
                f"solo: unknown camera '{token}' (expected {'/'.join(self.labels)} or duo)")
            return
        keep = t
        disabled = set()
        for lbl in self.labels:
            if lbl == keep:
                continue
            cam = next((c for c in self.cameras if c.label == lbl), None)
            if cam is not None and cam.recording_now():
                self.get_logger().warn(f'[{lbl}] solo refused -- it is recording (stop it first)')
                continue
            disabled.add(lbl)
        if not disabled:
            self.get_logger().warn(f'solo {keep}: nothing to disable -- aborted')
            return
        self._disabled = disabled
        self._reviving_logged -= disabled
        self.cameras = [c for c in self.cameras if c.label not in disabled]
        self._ready -= disabled
        self._sync_solo_file()               # cut the sockets we know; the rest are already absent
        for lbl in sorted(disabled):
            where = 'powering down' if self._socket_of(lbl) else 'already absent'
            self.get_logger().info(f'[{lbl}] solo off -- {where}')
        self.get_logger().info(f'[{keep}] solo -- only active camera')

    # =====================================================================
    # Periodic tick: snapshot -> watchdog -> publish
    # =====================================================================
    def _tick(self):
        self._consume_solo_request()   # apply any pending solo/duo command first
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
                        self.get_logger().warn(f'[{cam.label}] recovering (finishing file)')
                    self._recovering[cam.label] = now
                    continue
                # Camera dropped out of recording. Flag it right away (so the
                # operator is warned live) and keep trying to recover it.
                self._strikes[cam.label] += 1
                self._recovering.setdefault(cam.label, now)
                if self._strikes[cam.label] == 1:
                    # Distinct, unambiguous verbs:
                    #  "unreachable"  = no USB answer at all (still on the bus? brown-out?)
                    #                   -- vs "unplugged" which means the socket LEFT the bus.
                    #  "not filming"  = answers but stopped encoding on its own
                    #                   -- vs "stopped recording" which is an operator stop.
                    if not h['reachable']:
                        self.get_logger().warn(f'[{cam.label}] unreachable')
                    else:
                        self.get_logger().warn(f'[{cam.label}] not filming')
                    # A new drop episode for this take. Count it; a camera that
                    # flaps unreliable_after times is declared unreliable and
                    # latches EMERGENCY for the rest of the take (even if it keeps
                    # coming back), because footage from it can't be trusted.
                    self._drop_episodes[cam.label] = self._drop_episodes.get(cam.label, 0) + 1
                    if self._drop_episodes[cam.label] >= self.unreliable_after:
                        self._unreliable.add(cam.label)
                if (self._strikes[cam.label] >= self.strikes_max
                        and now >= self._cooldown_until[cam.label]):
                    self._recover(cam, now)
        self._publish(snapshot)

    def _mark_healthy(self, cam):
        """Camera is recording again -- clear any drop/fault bookkeeping."""
        if cam.label in self._recovering or cam.label in self._faulted:
            self.get_logger().info(f'[{cam.label}] recovered')
        self._recovering.pop(cam.label, None)
        self._faulted.pop(cam.label, None)
        self._recover_attempts.pop(cam.label, None)
        self._strikes[cam.label] = 0

    def _enforce_auto_power_off(self):
        """Keep Auto Power Off = Never on every present camera (also set at arm).
        A manual change or a post-power-loss revert must never let an idle camera
        sleep into a capture-dead state. Skipped while recording (settings cannot
        change then, and an encoding camera will not sleep anyway)."""
        if self.recording:
            return
        for cam in self.cameras:
            try:
                if cam.ensure_auto_power_off_never():
                    self.get_logger().info(f'[{cam.label}] Auto Power Off drifted -> re-set to Never.')
            except Exception:
                pass

    def _recover(self, cam, now):
        """Bring a dropped camera back: re-arm (out of USB-connected mode) and
        restart recording. Never gives up while the mission is recording -- a
        camera that was briefly unplugged is recovered as soon as it answers
        again. No power cycling (would corrupt the SD). A reachable camera with
        no usable SD cannot be fixed in software, so it is flagged as a hard
        fault -- but still re-checked, in case the card is swapped back."""
        self._cooldown_until[cam.label] = now + self.restart_cooldown
        self._recover_attempts[cam.label] = self._recover_attempts.get(cam.label, 0) + 1
        if not cam.reachable(timeout=2):
            self.get_logger().warn(f'[{cam.label}] recovering')
            return
        if cam.recording_now():
            # It is actually recording already (a previous start finally took, or a
            # transient bad read). Do NOT send another start -- that is the loud
            # double-start beep. Just clear the drop state.
            self._mark_healthy(cam)
            return
        if not cam.sd_present():
            # A full/unusable SD is TERMINAL, not a transient drop: the vehicle has
            # to surface. Stop ALL cameras cleanly (so the others finalise their
            # files, exactly like an operator [2]) and drop the recording intent so
            # nothing auto-resumes when the card is later cleared. The FAULT state
            # stays raised (via _faulted) so autonomy still sees MISSION COMPROMISED;
            # once the card is emptied the re-arm clears the fault -> READY, and the
            # operator restarts manually with [1].
            self._faulted[cam.label] = 'SD full'
            self.get_logger().error(f'[{cam.label}] SD full -- recording stopped')
            set_recording(self.cameras, False)
            self.recording = False
            self._set_intent(False)
            self._recovering.clear()
            self._recover_attempts.clear()
            return
        self.get_logger().warn(f'[{cam.label}] re-arming')
        cam.init()
        cam.set_datetime()
        cam.start()
        if cam.encoding():
            self._mark_healthy(cam)
            return
        # Reachable + SD ok but STILL won't record after a soft re-arm = the
        # "NOT ENOUGH POWER" brown-out zombie (answers /state 200 but /shutter 500).
        # Soft recovery cannot fix this -- only a real power cycle does. The manager
        # has no Vbus (no uhubctl in the container), so it asks the host watcher
        # (revive.sh) to Vbus-cycle THIS camera's socket. Safe: the camera is not
        # recording (enc=0 -> SD idle) and only its own socket is cycled, so a
        # camera filming on another port is never touched.
        if (cam.can_power_cycle()
                and self._recover_attempts.get(cam.label, 0) >= self.vbus_recover_after
                and now >= self._vbus_cooldown_until.get(cam.label, 0.0)):
            self._request_vbus_cycle(cam, now)

    def _request_vbus_cycle(self, cam, now):
        """Ask the host watcher (revive.sh) to Vbus power-cycle this camera's
        socket -- the only fix for a reachable-but-capture-dead (brown-out) cam.
        Written to a file the host watcher polls; the manager has no Vbus itself."""
        try:
            with open(self.vbus_request_file, 'w') as f:
                f.write(f'{cam.hub}:{cam.port} {cam.label}\n')   # label so the host watcher can log [LEFT]/[RIGHT]
        except OSError as e:
            # Do NOT advance the cooldown: the request never reached the watcher, so
            # the next tick should retry rather than back off for vbus_cooldown.
            self.get_logger().error(f'[{cam.label}] could not write Vbus request: {e}')
            return
        # Only now that the request is on disk do we start the cooldown (cycle +
        # cold-boot + re-arm take ~vbus_cooldown s; don't spam the watcher meanwhile).
        self._vbus_cooldown_until[cam.label] = now + self.vbus_cooldown
        self.get_logger().warn(f'[{cam.label}] power-cycle requested (brown-out)')

    # =====================================================================
    # Snapshot + publish
    # =====================================================================
    def _snapshot(self):
        return [(cam, cam.health()) for cam in self.cameras]

    def _publish(self, snapshot):
        recording_now = 0
        sd_parts = []
        for cam, h in snapshot:
            recording_now += 1 if h['recording'] else 0
            sec = h['remaining_sec']
            if not h['reachable'] or sec is None:
                rem = '--'
            elif sec >= 3600:
                rem = f"{sec // 3600}h{(sec % 3600) // 60:02d}"
            elif sec >= 60:
                rem = f"{sec // 60}min"
            else:
                rem = f"{sec}s"
            sd_parts.append(f"{h['label']} {rem}")
            self.status_pub.publish(GoProStatus(
                label=h['label'], ip=h['ip'], reachable=h['reachable'],
                recording=h['recording'], sd_ok=h['sd_ok'],
                can_power_cycle=h['can_power_cycle']))
        sd_info = ' . '.join(sd_parts)

        n = len(self.cameras)
        now = time.monotonic()
        # Recording, but NOTHING is being filmed (every camera dropped at once)?
        # Immediate emergency -- don't wait fault_after: the vehicle must hold
        # position because we are no longer capturing. Auto-clears the instant
        # any camera resumes. Suppressed ONLY during the post-(re)start encoder-init
        # window (_record_grace_until). Deliberately NOT gated on the per-camera
        # graces: a single freshly re-appeared camera's recovery grace used to mask
        # this emergency for the genuinely-dead ones (via max()) for up to
        # grace_period -- exactly when the vehicle most needs to hold.
        # Immediate "vehicle must hold" only makes sense when we HAD several cameras
        # and they ALL vanished at once. A single-camera take (or solo mode) instead
        # goes through the patient recovery path -- one drop should not instantly
        # emergency, it should get its recovery attempts / fault_after first.
        all_lost = (self.recording and n >= 2 and recording_now == 0
                    and now >= self._record_grace_until)
        if self._faulted:
            self.state = GoProSystem.STATE_FAULT
            reasons = ', '.join(f'{k} ({v})' for k, v in sorted(self._faulted.items()))
            self.message = f'MISSION COMPROMISED -- {reasons}'
        elif all_lost:
            self.state = GoProSystem.STATE_FAULT
            self.message = 'MISSION COMPROMISED -- no camera filming (vehicle should hold)'
        elif self._unreliable:
            # A camera flapped too many times this take: latched EMERGENCY even if
            # it is filming again right now (its footage can't be trusted). Clears
            # only when a new take starts.
            names = ', '.join(sorted(self._unreliable))
            self.state = GoProSystem.STATE_FAULT
            self.message = f'MISSION COMPROMISED -- {names} unreliable (flapping this take)'
        elif self._recovering:
            labels = sorted(self._recovering)
            worst = max(now - t for t in self._recovering.values())
            # DEGRADED = a camera just dropped and its first recovery attempts are
            # still in flight -> the AUV may slow down. It escalates to FAULT (the
            # AUV should stop) once a SECOND attempt has failed (one failed retry is
            # often just a transient re-enumeration), while we keep retrying.
            # Backstop: FAULT anyway if a camera has been lost past fault_after.
            # FAULT self-clears the instant the camera records again.
            failed = any(self._recover_attempts.get(l, 0) >= self.fault_after_attempts
                         for l in self._recovering)
            if failed or worst >= self.fault_after:
                self.state = GoProSystem.STATE_FAULT
                why = 'recovery failed' if failed else f'lost {int(worst)}s'
                self.message = f'MISSION COMPROMISED -- {labels} ({why}, still retrying)'
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

        # Human channel: name the EMERGENCY culprit(s) per camera, once per episode.
        # The aggregate state itself still rides the ~/system topic for the nav; here
        # we only surface it to a human reviewing journalctl at the surface.
        culprits = {}
        if self.state == GoProSystem.STATE_FAULT:
            if self._faulted:
                culprits = dict(self._faulted)
            elif all_lost:
                culprits = {c.label: 'no camera filming' for c in self.cameras}
            elif self._recovering:
                culprits = {lbl: f'could not recover after {self._recover_attempts.get(lbl, 0)} tries'
                            for lbl in self._recovering}
            # Flapping cameras stay named even once they film again, and their
            # reason wins over a transient "recovery failed".
            for lbl in self._unreliable:
                culprits[lbl] = f'unreliable, {self._drop_episodes.get(lbl, 0)} drops'
        for lbl, why in culprits.items():
            if lbl not in self._emergency_logged:
                self.get_logger().error(f'[{lbl}] EMERGENCY ({why})')
                self._emergency_logged.add(lbl)
        for lbl in list(self._emergency_logged):
            if lbl not in culprits:
                self._emergency_logged.discard(lbl)   # cleared -> a later fault logs again

        self.system_pub.publish(GoProSystem(
            state=self.state, message=self.message, recording=self.recording,
            all_ready=(n > 0 and len(self._ready) == n),
            num_cameras=n, num_recording=recording_now, sd_info=sd_info))


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
