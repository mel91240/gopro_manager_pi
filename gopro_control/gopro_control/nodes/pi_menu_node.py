#!/usr/bin/env python3
"""Operator menu for the AUV GoPro subsystem.

Run on the AUV Pi; the operator connects over WiFi, checks the cameras are READY,
then starts/stops recording and changes capture settings. Talks to gopro_manager
over ROS 2 services and reads its published GoProSystem state.

    ros2 run gopro_control pi_menu
"""
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
        if not client.wait_for_service(timeout_sec=3.0):
            return None
        fut = client.call_async(req)
        deadline = time.time() + timeout
        while not fut.done() and time.time() < deadline:
            time.sleep(0.05)
        return fut.result() if fut.done() else None


def _status_line(sys_msg) -> str:
    if sys_msg is None:
        return 'state: (waiting for gopro_manager...)'
    return (f"state: {sys_msg.state}  |  {sys_msg.num_recording}/{sys_msg.num_cameras} recording"
            f"  |  {sys_msg.message}")


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

    try:
        while True:
            print('\n=== AUV GoPro Master Control ===')
            print(_status_line(node.system))
            print('  [1] Start recording')
            print('  [2] Stop recording')
            print('  [3] Change settings')
            print('  [0] Exit')
            choice = input('Command: ').strip()

            if choice == '1':
                if node.system and node.system.state == GoProSystem.STATE_RECORDING:
                    print('Already recording.')
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
            else:
                print('Unknown command.')

            if choice in ('1', '2', '3'):
                time.sleep(0.4)   # let the fresh system state arrive before redisplaying
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        executor.shutdown()
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
