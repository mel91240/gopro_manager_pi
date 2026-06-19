#!/usr/bin/env python3
"""Operator menu for the AUV GoPro subsystem.

Run on the AUV Pi; the operator connects over WiFi, checks the cameras are READY,
then starts/stops recording and changes capture settings. Talks to gopro_manager
over ROS 2 services and reads its published GoProSystem state.

    ros2 run gopro_control pi_menu
"""
import select
import shutil
import sys
import termios
import threading
import time

import rclpy
from rclpy.executors import SingleThreadedExecutor
from rclpy.node import Node
from std_srvs.srv import SetBool

from gopro_msgs.msg import GoProSystem
from gopro_msgs.srv import GoProSettings

from gopro_control.core import settings as gp

RECORD_SRV = '/gopro_manager/record'
SETTINGS_SRV = '/gopro_manager/settings'
SYSTEM_TOPIC = '/gopro_manager/system'

DEFAULT_PROFILE = dict(camera_mode='Video', resolution='4K', fps='24',
                       fov='Wide', hypersmooth='Off', wind_reduction='Off')


class PiMenu(Node):
    def __init__(self):
        super().__init__('pi_menu')
        self.record_cli = self.create_client(SetBool, RECORD_SRV)
        self.settings_cli = self.create_client(GoProSettings, SETTINGS_SRV)
        self.system = None
        self.create_subscription(GoProSystem, SYSTEM_TOPIC, self._on_system, 10)

    def _on_system(self, msg):
        self.system = msg

    def call(self, client, req, timeout=20.0):
        """Call a service and wait for the result (the executor spins in a
        background thread, so we just poll the future here)."""
        if not client.wait_for_service(timeout_sec=10.0):   # cross-container DDS
            return None                                     # discovery can be slow
        fut = client.call_async(req)
        deadline = time.time() + timeout
        while not fut.done() and time.time() < deadline:
            time.sleep(0.05)
        return fut.result() if fut.done() else None


def _status_line(s) -> str:
    """Minimal, state-appropriate one-liner for the banner."""
    if s is None:
        return 'connecting to gopro_manager...'
    if s.state == GoProSystem.STATE_RECORDING:
        return f'RECORDING ({s.num_recording}/{s.num_cameras})'
    if s.state == GoProSystem.STATE_DEGRADED:
        return f'DEGRADED -- {s.message}'
    if s.state == GoProSystem.STATE_FAULT:
        return f'EMERGENCY -- {s.message}'
    if s.state == GoProSystem.STATE_READY:
        return 'READY'
    return s.state          # INITIALIZING


def _menu_body(node):
    s = node.system
    cards = s.sd_info if (s is not None and s.sd_info) else '--'
    return [
        '=== GoPro Recording ===',
        f'Status: {_status_line(s)}',
        f'Cards:  {cards}',
        '  [1] Start recording',
        '  [2] Stop recording',
        '  [3] Change settings',
        '  [0] Exit',
    ]


def _print_menu(node):
    """Full draw of the menu. Each line is cut to the terminal width so it never
    wraps (which would break the in-place status refresh)."""
    width = shutil.get_terminal_size((80, 24)).columns
    body = [ln[:width - 1] for ln in _menu_body(node)]
    sys.stdout.write('\n' + '\n'.join(body) + '\nCommand: ')
    sys.stdout.flush()


def _refresh_status(node):
    """Update ONLY the Status and Cards lines, in place, WITHOUT touching the
    'Command:' input line: the cursor is saved and restored, so whatever the
    operator has already typed stays visible and intact. Runs every ~1s while idle
    at the prompt. Body order is header, Status, Cards, [1], [2], [3], [0] -> from
    the prompt the Status line is 6 up and Cards 5 up."""
    if not sys.stdout.isatty():
        return
    width = shutil.get_terminal_size((80, 24)).columns
    body = [ln[:width - 1] for ln in _menu_body(node)]
    sys.stdout.write('\033[s'                        # save cursor (at the prompt)
                     + '\033[6A\r\033[2K' + body[1]  # up to Status, clear line, rewrite
                     + '\033[1B\r\033[2K' + body[2]  # down to Cards, clear line, rewrite
                     + '\033[u')                     # restore cursor (back to the prompt)
    sys.stdout.flush()


def _flush_input():
    """Discard anything already sitting in the terminal input buffer (e.g. keys
    or Enters pressed during the slow DDS discovery, when stdin isn't being
    read). Without this the first menu prompt reads a stale buffered newline ->
    choice='' -> a spurious 'Unknown command', desyncing every keystroke from
    the prompt. No-op when stdin isn't a TTY (piped)."""
    try:
        termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    except Exception:
        pass


def _await_command(node, refresh=1.0):
    """Wait for a command. While idle at the prompt, refresh the live Status / SD
    line every ~1s WITHOUT disturbing what the operator has typed. Entering a
    DEGRADED/FAULT state also rings the terminal bell -- critical just before diving."""
    last_state = node.system.state if node.system else None
    while True:
        ready, _, _ = select.select([sys.stdin], [], [], refresh)
        if ready:
            line = sys.stdin.readline()
            if line == '':                       # EOF
                raise EOFError
            return line.strip()
        cur_state = node.system.state if node.system else None
        if (cur_state != last_state and node.system is not None
                and node.system.state in (GoProSystem.STATE_DEGRADED, GoProSystem.STATE_FAULT)):
            sys.stdout.write('\a')               # bell on entering a degraded/fault state
        last_state = cur_state
        _refresh_status(node)


def _choose(prompt, options, default):
    """Numbered choice from a list of names; Enter keeps the default."""
    names = list(options)
    print(prompt)
    for i, name in enumerate(names, 1):
        mark = ' (default)' if name == default else ''
        print(f"  [{i}] {name}{mark}")
    raw = input('> ').strip()
    if raw == '':
        return default
    if raw.isdigit() and 1 <= int(raw) <= len(names):
        return names[int(raw) - 1]
    print('Invalid choice, keeping default.')
    return default


def _build_settings_request():
    req = GoProSettings.Request()
    print('\n--- Mission profile ---')
    print('  [1] Default: Video | 4K | 24fps | Wide | HyperSmooth Off | Wind Off')
    print('  [2] Custom')
    if input('Choose (1/2, Enter=1): ').strip() != '2':
        for k, v in DEFAULT_PROFILE.items():
            setattr(req, k, v)
        return req
    req.camera_mode = 'Video'
    req.resolution = _choose('\nResolution:', gp.RESOLUTION, '4K')
    req.fps = _choose('FPS:', gp.FPS, '24')
    req.fov = _choose('FOV:', gp.FOV, 'Wide')
    req.hypersmooth = _choose('HyperSmooth:', gp.HYPERSMOOTH, 'Off')
    req.wind_reduction = _choose('Wind reduction:', gp.WIND, 'Off')
    return req


def main(args=None):
    rclpy.init(args=args)
    node = PiMenu()
    executor = SingleThreadedExecutor()
    executor.add_node(node)
    spin_thread = threading.Thread(target=executor.spin, daemon=True)
    spin_thread.start()

    # Wait for the manager's first state so a reconnecting operator sees the real
    # status (e.g. RECORDING) instead of "connecting...". The menu is a fresh
    # container each time, so DDS discovery with the persistent manager can take
    # several seconds -- be patient rather than show a misleading screen.
    print('Discovering gopro_manager...', end='', flush=True)
    deadline = time.time() + 15.0
    while node.system is None and time.time() < deadline:
        print('.', end='', flush=True)
        time.sleep(0.5)
    print(' connected.' if node.system is not None else ' not found (is the manager up?).')
    _flush_input()      # drop keystrokes typed during discovery so [0]/[1].. read clean

    try:
        while True:
            _print_menu(node)
            choice = _await_command(node)

            if choice == '1':
                # Block a double-start only when genuinely recording. In FAULT
                # (recording=true is just the unfulfilled intent) let the call go
                # through so the manager replies with the real reason (e.g. SD full).
                if (node.system and node.system.recording
                        and node.system.state != GoProSystem.STATE_FAULT):
                    print('Already recording (stop with [2] before starting again).')
                    continue
                if node.system and not node.system.all_ready:
                    if input('Cameras not all READY. Start anyway? (y/N): ').strip().lower() != 'y':
                        continue
                resp = node.call(node.record_cli, SetBool.Request(data=True))
                print(f'-> {resp.message}' if resp else '-> no response from gopro_manager')

            elif choice == '2':
                resp = node.call(node.record_cli, SetBool.Request(data=False))
                print(f'-> {resp.message}' if resp else '-> no response from gopro_manager')

            elif choice == '3':
                if node.system and node.system.recording:
                    print('Stop recording before changing settings.')
                    continue
                resp = node.call(node.settings_cli, _build_settings_request())
                print(f'-> {resp.message}' if resp else '-> no response from gopro_manager')

            elif choice == '0':
                break
            elif choice == '':
                continue            # bare Enter / stray newline -> just redraw, not an error
            else:
                print(f"Unknown command: {choice!r} -- nothing launched. Use 1/2/3/0.")

            if choice in ('1', '2', '3'):
                time.sleep(0.4)   # let the fresh system state arrive before redisplaying
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        executor.shutdown()
        spin_thread.join(timeout=2.0)     # let the spin thread exit before teardown
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
