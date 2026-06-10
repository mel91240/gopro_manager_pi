import threading, time, sys
import rclpy
from rclpy.executors import SingleThreadedExecutor
from rclpy.node import Node
from std_srvs.srv import SetBool
from gopro_msgs.srv import GoProSettings
from gopro_msgs.msg import GoProSystem

class T(Node):
    def __init__(self):
        super().__init__('test_all')
        self.rec = self.create_client(SetBool, '/gopro_manager/record')
        self.setg = self.create_client(GoProSettings, '/gopro_manager/settings')
        self.sys = None; self.results = []
        self.create_subscription(GoProSystem, '/gopro_manager/system', self._cb, 10)
    def _cb(self, m): self.sys = m
    def _call(self, cli, req, t=25):
        if not cli.wait_for_service(timeout_sec=8): return None
        f = cli.call_async(req); end = time.time()+t
        while not f.done() and time.time() < end: time.sleep(0.05)
        return f.result()
    def record(self, on): return self._call(self.rec, SetBool.Request(data=on))
    def settings(self, **k): return self._call(self.setg, GoProSettings.Request(**k))
    def state(self): return self.sys.state if self.sys else None
    def check(self, name, ok, detail=""):
        self.results.append((name, ok))
        print("  [%s] %-44s %s" % ("PASS" if ok else "FAIL", name, detail))
    def info(self, name, r):
        ok = bool(r and r.success)
        self.results.append((name, ok))
        print("  [%s] %-44s %s" % ("OK  " if ok else "no  ", name, "" if ok else ("-> %s" % (r.message if r else "NO RESP"))))

def main():
    rclpy.init(); n = T()
    ex = SingleThreadedExecutor(); ex.add_node(n)
    threading.Thread(target=ex.spin, daemon=True).start()
    print("Arming cameras (14s)..."); time.sleep(14)

    print("\n=== 1. STARTUP ===")
    n.check("state READY at boot", n.state() == "READY", "(got %s)" % n.state())

    print("\n=== 2. SETTINGS VALIDATION (invalid combos must be refused) ===")
    for name, kw, exp in [
        ("reject 5.3K + 120fps", dict(resolution="5.3K", fps="120"), "60fps"),
        ("reject 4K + 240fps",   dict(resolution="4K", fps="240"), "120fps"),
        ("reject HyperView+1080p", dict(resolution="1080p", fov="HyperView"), "4K or 5.3K"),
        ("reject unknown fps=99", dict(fps="99"), "unknown value")]:
        r = n.settings(**kw)
        ok = bool(r and not r.success and exp in r.message)
        n.check(name, ok, "-> %s" % (r.message if r else "NO RESP"))

    print("\n=== 3. SETTINGS MATRIX (informative: what the Hero 12 accepts) ===")
    matrix = [dict(camera_mode="Video", resolution=res, fps="30", fov="Wide", hypersmooth="Off", wind_reduction="Off")
              for res in ["1080p", "2.7K", "4K", "5.3K"]]
    matrix += [
        dict(camera_mode="Video", resolution="4K", fps="60", fov="Wide", hypersmooth="On", wind_reduction="Auto"),
        dict(camera_mode="Video", resolution="4K", fps="120", fov="Wide", hypersmooth="Off", wind_reduction="On"),
        dict(camera_mode="Video", resolution="5.3K", fps="60", fov="Wide", hypersmooth="On", wind_reduction="Off"),
        dict(camera_mode="Video", resolution="4K", fps="24", fov="HyperView", hypersmooth="AutoBoost", wind_reduction="Off")]
    for kw in matrix:
        lab = "%s/%s/%s/HS=%s/W=%s" % (kw["resolution"], kw["fps"], kw["fov"], kw["hypersmooth"], kw["wind_reduction"])
        n.info(lab, n.settings(**kw))

    print("\n=== 4. RECORD LOGIC ===")
    r = n.record(True); time.sleep(1.5)
    n.check("start when READY -> success", bool(r and r.success), "-> %s" % (r.message if r else "NO RESP"))
    n.check("state -> RECORDING", n.state() == "RECORDING", "(got %s)" % n.state())
    r = n.record(True)
    n.check("start again -> refused (no beep)", bool(r and not r.success and "Already recording" in r.message), "-> %s" % (r.message if r else "NO RESP"))
    r = n.settings(resolution="4K", fps="30")
    n.check("settings while recording -> refused", bool(r and not r.success and "while recording" in r.message), "-> %s" % (r.message if r else "NO RESP"))
    r = n.record(False); time.sleep(1.5)
    n.check("stop -> success", bool(r and r.success), "-> %s" % (r.message if r else "NO RESP"))
    n.check("state -> READY", n.state() == "READY", "(got %s)" % n.state())
    r = n.record(False)
    n.check("stop when not recording -> allowed", r is not None, "-> %s" % (r.message if r else "NO RESP"))

    print("\n=== SUMMARY ===")
    p = sum(1 for _, ok in n.results if ok)
    print("  %d / %d checks OK" % (p, len(n.results)))
    fails = [name for name, ok in n.results if not ok]
    if fails: print("  Not OK:", "; ".join(fails))
    if n.state() == "RECORDING": n.record(False)   # leave cameras stopped
    sys.stdout.flush(); time.sleep(0.5)
    ex.shutdown(); rclpy.try_shutdown()

main()
