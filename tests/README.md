# Tests & operator console

Run inside the vehicle's ROS 2 container (`cosma_auv`), from the AUV Pi.

## Mission console (operator)
Arms the cameras then opens the operator menu:
```bash
docker run --rm -it --network host \
  -v ~/dev/swarm-vehicle:/home/cosma_auv/swarm-vehicle \
  --entrypoint bash cosma_auv:latest \
  /home/cosma_auv/swarm-vehicle/mission.sh
```

## Automated test suite (~2 min)
Exercises every operator action (record start/stop, guards, state transitions,
settings validation, settings matrix) via the ROS 2 services and prints a
PASS/FAIL report. Uses only the guarded service path, so it never double-starts
a recording camera (no error beep).
```bash
# copy test_all.py + run_tests.sh next to the workspace, then:
docker run --rm --network host \
  -v ~/dev/swarm-vehicle:/home/cosma_auv/swarm-vehicle \
  --entrypoint bash cosma_auv:latest \
  /home/cosma_auv/swarm-vehicle/run_tests.sh
```

Note: HERO12 2.7K is a high-frame-rate (slow-motion) resolution; 2.7K with a low
fps (e.g. 30) is rejected by the camera. That is expected, not a bug.
