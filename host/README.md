# Operating the GoPro rig on the AUV

`./gopro.sh` is the single operator entry point. The manager and the auto-revive
watcher run as systemd boot services, so the cameras arm themselves on power-up —
you normally never start anything by hand.

## Setup (once)

From the repo root (not here), run `./install.sh` — it builds the packages,
installs these scripts into `gopro_scripts/`, and enables the two boot services
(`gopro-manager` + `gopro-autorevive`). See the top-level README.

## Operator menu

```bash
./gopro.sh
```
```
=== AUV GoPro ===  (manager: UP | SSD: mounted)
  [1] Recording (record / stop / settings)
  [2] Copy footage -> SSD       (live %/speed/ETA, 'q' to cancel)
  [3] Delete / wipe the cards
  [4] Start / stop the manager
```

- **[1] Recording** opens the ROS console (`menu.sh`): [1] start, [2] stop,
  [3] settings. Safe to quit and re-open — it never stops the manager or an
  in-progress take, and on reconnect shows the live state (e.g. RECORDING 2/2).
- **[2] Copy** mounts the SSD and offloads with a resumable downloader that
  survives a camera hiccup. Works with the manager running.
- **[3] Delete** removes media from the cards (selective or all; refuses a
  recording camera). The Open GoPro API has no real format — "all" wipes media.
- **[4] Manager** toggles the manager container (it auto-starts at boot anyway).

## Why the manager is decoupled

The manager owns the cameras and must run for the whole mission — underwater and
after SSH drops — so it runs as a detached, systemd-managed Docker container that
survives the session ending. If it restarts while filming it **adopts** the
in-progress recording (no re-arm, no beep), and the in-recording guard means
pressing [1] again never double-shutters a recording camera.

## Low-level scripts (normally not needed)

`manager_up.sh` / `manager_down.sh` — manual manager control (also the service's
Exec hooks); `manager_log.sh` — manager logs; `menu.sh` — the ROS console;
`revive.sh` — power-cycle a camera confirmed off the bus.
