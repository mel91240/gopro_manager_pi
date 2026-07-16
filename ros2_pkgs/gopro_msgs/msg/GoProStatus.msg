# Per-camera status snapshot, published periodically by gopro_manager.
string label             # LEFT / RIGHT / ...
string ip                # camera IP (Open GoPro wired, e.g. 172.24.163.51)
bool reachable           # HTTP API answering
bool recording           # encoding active
bool sd_ok               # SD card present and writable
bool can_power_cycle     # hub port resolved -> Vbus power-cycle possible (done by the host revive watcher)
