# Overall GoPro subsystem state, published periodically by gopro_manager.
# The operator menu (pi_menu) uses this to know if it is safe to dive.
string STATE_INITIALIZING=INITIALIZING   # cameras booting / being armed
string STATE_READY=READY                 # all cameras armed and verified, not recording
string STATE_RECORDING=RECORDING         # all cameras recording
string STATE_DEGRADED=DEGRADED           # recording but at least one camera was recovered/needs attention
string STATE_FAULT=FAULT                 # unrecoverable: a camera cannot be made to record -> abort mission

string state
string message
bool recording
bool all_ready
uint8 num_cameras
uint8 num_recording
string sd_info            # per-camera SD time left, human-readable (e.g. "LEFT 42min . RIGHT 38min")
