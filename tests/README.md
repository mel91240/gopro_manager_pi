# Automated tests

`test_all.py` exercises every operator action (record start/stop, guards, state
transitions, settings validation, the settings matrix) through the ROS 2 service
interface and prints a PASS/FAIL report. It uses only the guarded service path,
so it never double-starts a recording camera (no error beep).

Run it inside the `cosma_auv` ROS 2 container (a dev container WITHOUT the
persistent/systemd manager — `run_tests.sh` spawns its own throwaway manager):

```bash
docker run --rm --network host \
  -v ~/dev/swarm-vehicle:/home/cosma_auv/swarm-vehicle \
  --entrypoint bash cosma_auv:latest \
  /home/cosma_auv/swarm-vehicle/ros2_ws/src/gopro_control/tests/run_tests.sh
```

Note: HERO12 2.7K is a high-frame-rate (slow-motion) resolution; 2.7K with a low
fps (e.g. 30) is rejected by the camera. That is expected, not a bug.
