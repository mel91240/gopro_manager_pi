# Operating the GoPro rig on the AUV

The manager and the auto-revive watcher run as systemd boot services, so the
cameras arm themselves on power-up -- you normally never start anything by hand.
Everything is driven from small scripts (no interactive menu).

## Setup (once)

From the repo root (not here), run `./install.sh` -- it builds the packages,
installs these scripts into `gopro_scripts/`, and enables the two boot services
(`gopro-manager` + `gopro-autorevive`). See the top-level README.

## Operator commands

Recording -- these talk to the running manager, so the watchdog / EMERGENCY logic
stays in charge:
```bash
./gopro_ctl.sh record            # start recording on all cameras
./gopro_ctl.sh stop              # stop
./gopro_ctl.sh status            # READY/RECORDING + per-camera SD
./gopro_ctl.sh settings resolution=4K fps=30 fov=Linear   # change only the fields you pass
./gopro_ctl.sh solo LEFT         # keep one camera, power the other off
./gopro_ctl.sh duo               # re-enable both
```

Offload / delete / logs:
```bash
./download.sh                    # mount the SSD + resumable offload (live %/speed/ETA)
./download.sh --verify           # check every camera clip has a same-size copy on the SSD
./gopro_delete.py --pick         # delete selected clips   (--all wipes the cards)
./manager_log.sh                 # live manager + auto-revive + download logs
```

## Why the manager is decoupled

The manager owns the cameras and must run for the whole mission -- underwater and
after SSH drops -- so it runs as a systemd-managed Docker container that survives
the session ending. If it restarts while filming it **adopts** the in-progress
recording (no re-arm, no beep), and the in-recording guard means starting again
never double-shutters a recording camera.

## Low-level scripts (normally not needed)

`manager_up.sh` / `manager_down.sh` -- manual manager control (also the service's
Exec hooks); `manager_log.sh` -- logs; `revive.sh` -- power-cycle a camera
confirmed off the bus.
