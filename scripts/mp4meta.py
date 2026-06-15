#!/usr/bin/env python3
"""Read GoPro MP4 start time + embedded data tracks (incl. the GPMF telemetry
that carries the IMU: ACCL/GYRO) WITHOUT ffmpeg -- a minimal MP4 box walker.

  python3 mp4meta.py clip1.MP4 clip2.MP4 ...
  python3 mp4meta.py /mnt/ssd/gopro/*/*.MP4

For each file prints: the recording start time from the movie header (mvhd
creation_time, UTC, 1-second resolution -- GoPro sets it from the camera clock),
the duration, the track handlers present, and whether the GPMF/IMU stream is
embedded (GoPro always puts ACCL+GYRO in that 'gpmd' stream)."""
import datetime
import struct
import sys

MP4_EPOCH = datetime.datetime(1904, 1, 1, tzinfo=datetime.timezone.utc)


def _children(buf, start, end):
    """Yield (type, data_start, data_end) for boxes directly between start..end."""
    p = start
    while p + 8 <= end:
        size, typ = struct.unpack_from(">I4s", buf, p)
        ds = p + 8
        if size == 1:
            size = struct.unpack_from(">Q", buf, p + 8)[0]
            ds = p + 16
        elif size == 0:
            size = end - p
        if size < 8 or p + size > end:
            break
        yield typ.decode("latin1", "replace"), ds, p + size
        p += size


def _find(buf, start, end, want):
    for typ, ds, de in _children(buf, start, end):
        if typ == want:
            return ds, de
    return None


def parse(path):
    with open(path, "rb") as fh:
        buf = fh.read()
    info = {"start": None, "dur": None, "handlers": [], "gpmf": False, "err": None}
    moov = _find(buf, 0, len(buf), "moov")
    if not moov:
        info["err"] = "no moov box"
        return info
    m0, m1 = moov

    mvhd = _find(buf, m0, m1, "mvhd")
    if mvhd:
        d, _ = mvhd
        ver = buf[d]
        if ver == 1:
            ct = struct.unpack_from(">Q", buf, d + 4)[0]
            ts = struct.unpack_from(">I", buf, d + 20)[0]
            dur = struct.unpack_from(">Q", buf, d + 24)[0]
        else:
            ct = struct.unpack_from(">I", buf, d + 4)[0]
            ts = struct.unpack_from(">I", buf, d + 12)[0]
            dur = struct.unpack_from(">I", buf, d + 16)[0]
        if ct:
            info["start"] = MP4_EPOCH + datetime.timedelta(seconds=ct)
        info["dur"] = (dur / ts) if ts else None

    # per-track handler (vide/soun/meta...) + name
    for typ, ds, de in _children(buf, m0, m1):
        if typ != "trak":
            continue
        mdia = _find(buf, ds, de, "mdia")
        if not mdia:
            continue
        hdlr = _find(buf, mdia[0], mdia[1], "hdlr")
        if not hdlr:
            continue
        hd, he = hdlr
        htype = buf[hd + 8:hd + 12].decode("latin1", "replace")
        name = buf[hd + 24:he].split(b"\x00")[0].decode("latin1", "replace")
        info["handlers"].append((htype, name))

    # GoPro telemetry / IMU stream -> 'gpmd' fourcc (and "GoPro MET" handler name)
    info["gpmf"] = (b"gpmd" in buf[m0:m1]) or (b"GoPro MET" in buf[m0:m1])
    return info


def main(argv):
    if not argv:
        print("usage: mp4meta.py file.MP4 [...]")
        return 1
    print(f"{'file':<34} {'start (UTC, 1s res)':<21} {'dur':>7}  IMU/GPMF  tracks")
    print("-" * 92)
    for path in argv:
        m = parse(path)
        base = path.rsplit("/", 1)[-1]
        if m["err"]:
            print(f"{base:<34} ERROR: {m['err']}")
            continue
        start = m["start"].strftime("%Y-%m-%d %H:%M:%S") if m["start"] else "(none)"
        dur = f"{m['dur']:.1f}s" if m["dur"] else "?"
        imu = "yes" if m["gpmf"] else "NO"
        tr = ",".join(h for h, _ in m["handlers"])
        print(f"{base:<34} {start:<21} {dur:>7}  {imu:<8}  {tr}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
