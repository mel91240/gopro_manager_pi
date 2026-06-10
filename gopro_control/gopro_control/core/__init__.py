"""ROS-independent GoPro engine: wired control + uhubctl power switching."""
from .camera import GoPro, discover, set_recording, system_clock_synced

__all__ = ["GoPro", "discover", "set_recording", "system_clock_synced"]
