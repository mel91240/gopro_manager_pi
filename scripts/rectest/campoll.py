#!/usr/bin/env python3
"""Poll both GoPros' state every 10 s and print one timestamped line per camera.
Used by the recording-stability test harness. Key status IDs:
  10 = encoding (1 = actively recording), 8 = busy, 35 = video time remaining (s),
  54 = SD free space (MB). A camera that stops recording shows enc=0; one that
  drops off / leaves control mode shows UNREACHABLE (the thing we hunt)."""
import datetime
import json
import time
import urllib.request

CAMS = ["172.24.163.51", "172.26.185.51"]
PERIOD = 10


def state(ip):
    try:
        with urllib.request.urlopen(f"http://{ip}:8080/gopro/camera/state", timeout=5) as r:
            d = json.load(r)["status"]
        return (f"enc={d.get('10')} busy={d.get('8')} "
                f"rem_s={d.get('35')} sdfreeMB={d.get('54')}")
    except Exception as e:
        return f"UNREACHABLE {e}"


while True:
    ts = datetime.datetime.now().isoformat(timespec="seconds")
    for ip in CAMS:
        print(f"{ts} {ip} {state(ip)}", flush=True)
    time.sleep(PERIOD)
