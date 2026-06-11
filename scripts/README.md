# Operating the GoPro rig on the AUV

The **manager** and the **menu** are deliberately two separate processes:

- The **manager** owns the cameras and must run for the **whole mission** -- while
  the AUV is underwater and after you close the menu or drop SSH. It runs as a
  detached Docker container, so it survives the SSH session ending.
- The **menu** is just a thin client. Open it, send commands, close it. Re-open
  it any time -- it never stops the manager or the recording.

## Mission flow

```bash
# --- before the dive (SSH on the Pi) ---
./manager_up.sh      # start the persistent manager; it arms the cameras
./manager_log.sh     # watch until both cameras report "armed & verified"
./menu.sh            # [1] start recording, then quit the menu ([0])

# ...put the AUV in the water, then just close SSH. The manager keeps running. ...

# --- after the dive (reconnect SSH) ---
./menu.sh            # shows RECORDING (2/2); press [2] to stop. Footage is safe.
./manager_down.sh    # optional: stop the manager once you are done
```

## Why this is safe

- If the manager is **restarted** while the cameras are recording (Pi reboot,
  crash, `--restart`), it **detects the in-progress recording and adopts it** --
  it does NOT re-arm or shutter-test a recording camera, which would beep and
  could stop the take.
- Because the manager knows it is recording, pressing **[1] Start** again is
  refused on the manager side, so it never sends a second shutter to a recording
  camera -- **no loud beep**.

## Quick local test (couples manager + menu in one container)

`tests/mission.sh` is only for a quick bench test; it kills the manager when you
quit. For real missions always use `manager_up.sh` + `menu.sh`.
