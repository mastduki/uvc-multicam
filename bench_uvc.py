#!/usr/bin/env python3
"""Measure what a UVC stream start really costs and how many streams fit on one USB bus.
Standard library only (fcntl/mmap ioctls). Usage:

  python3 bench_uvc.py stages  [w=1280 h=720 pf=MJPG]      per-stage timing: open, S_FMT, REQBUFS, STREAMON, first frame, STREAMOFF
  python3 bench_uvc.py fit     w=640 h=480 pf=MJPG         how many cameras can stream concurrently in this format (+ alt setting used)
  python3 bench_uvc.py rounds  [w= h= pf= k=1]             keep fds open; per camera STREAMON -> k frames -> STREAMOFF; round time
  python3 bench_uvc.py continuous [w= h= pf=]              all cameras streaming at once; fps per camera, CPU %, close time

Devices default to every USB camera in /dev/v4l/by-path (*-usb-0:*-video-index0); pass devs=/dev/video0,/dev/video2 to override.
"""
import errno
import fcntl
import glob
import mmap
import os
import resource
import select
import struct
import sys
import time

S_FMT, REQBUFS, QUERYBUF, QBUF, DQBUF, STREAMON, STREAMOFF = (
    0xC0D05605, 0xC0145608, 0xC0585609, 0xC058560F, 0xC0585611, 0x40045612, 0x40045613)
CAP, MMAP = 1, 1


def fourcc(s):
    return struct.unpack("<I", s.encode())[0]


def alt_setting(dev):
    """Active alternate setting of the video streaming interface (sysfs), -1 if unknown."""
    v = os.path.basename(os.path.realpath(dev))
    p = os.path.realpath(f"/sys/class/video4linux/{v}/device")  # .../1-1.2.2:1.0
    try:
        return int(open(p[:-4] + ":1.1/bAlternateSetting").read())
    except OSError:
        return -1


def vbuf(i=0):
    b = bytearray(88)
    struct.pack_into("<II", b, 0, i, CAP)
    struct.pack_into("<I", b, 60, MMAP)
    return b


class Cam:
    def __init__(self, dev):
        self.dev, self.fd, self.maps, self.t = dev, None, [], {}

    def _timed(self, key, fn):
        t0 = time.perf_counter()
        r = fn()
        self.t[key] = self.t.get(key, 0) + time.perf_counter() - t0
        return r

    def open(self):
        self.fd = self._timed("open", lambda: os.open(self.dev, os.O_RDWR))

    def s_fmt(self, w, h, pf):
        b = bytearray(208)
        struct.pack_into("<I", b, 0, CAP)
        struct.pack_into("<IIII", b, 8, w, h, fourcc(pf), 1)
        self._timed("s_fmt", lambda: fcntl.ioctl(self.fd, S_FMT, b))

    def reqbufs(self, n):
        b = bytearray(20)
        struct.pack_into("<III", b, 0, n, CAP, MMAP)
        self._timed("reqbufs", lambda: fcntl.ioctl(self.fd, REQBUFS, b))
        for m in self.maps:
            m.close()
        self.maps = []
        for i in range(struct.unpack_from("<I", b)[0]):
            q = vbuf(i)
            fcntl.ioctl(self.fd, QUERYBUF, q)
            off, ln = struct.unpack_from("<I", q, 64)[0], struct.unpack_from("<I", q, 72)[0]
            self.maps.append(mmap.mmap(self.fd, ln, mmap.MAP_SHARED, mmap.PROT_READ, offset=off))

    def queue_all(self):
        for i in range(len(self.maps)):
            fcntl.ioctl(self.fd, QBUF, vbuf(i))

    def streamon(self):
        self._timed("streamon", lambda: fcntl.ioctl(self.fd, STREAMON, struct.pack("<i", CAP)))

    def streamoff(self):
        self._timed("streamoff", lambda: fcntl.ioctl(self.fd, STREAMOFF, struct.pack("<i", CAP)))

    def dqbuf(self, timeout=3.0):
        if not select.select([self.fd], [], [], timeout)[0]:
            raise TimeoutError("no frame")
        b = vbuf()
        fcntl.ioctl(self.fd, DQBUF, b)
        i, _, used, _ = struct.unpack_from("<IIII", b)
        fcntl.ioctl(self.fd, QBUF, vbuf(i))
        return used

    def frames(self, n):
        t0 = time.perf_counter()
        out = []
        for k in range(n):
            used = self.dqbuf()
            if k == 0:
                self.t["first_frame"] = time.perf_counter() - t0
            out.append(used)
        return out

    def close(self):
        for m in self.maps:
            m.close()
        self.maps = []
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def ms(t):
    return " ".join(f"{k}={v * 1000:.0f}ms" for k, v in t.items())


def stages(devs, w=1280, h=720, pf="MJPG", frames=5):
    total = time.perf_counter()
    for dev in devs:
        c = Cam(dev)
        t0 = time.perf_counter()
        c.open(); c.s_fmt(w, h, pf); c.reqbufs(4); c.queue_all(); c.streamon()
        sizes = c.frames(frames)
        c.streamoff(); c.close()
        print(f"{dev}: total {time.perf_counter() - t0:.2f}s | {ms(c.t)} | frame bytes {sizes}")
    print(f"sequential open..close over {len(devs)} cameras: {time.perf_counter() - total:.2f}s")


def fit(devs, w=640, h=480, pf="MJPG"):
    ok, cams = [], []
    for dev in devs:
        c = Cam(dev); cams.append(c)
        try:
            c.open(); c.s_fmt(w, h, pf); c.reqbufs(4); c.queue_all(); c.streamon()
            ok.append(c); print(f"  {dev}: STREAMON ok, alt setting {alt_setting(dev)}")
        except OSError as e:
            print(f"  {dev}: {errno.errorcode.get(e.errno, e.errno)} ({e})")
    if ok:
        count = {c.dev: 0 for c in ok}; t0 = time.perf_counter()
        while time.perf_counter() - t0 < 2:
            ready, _, _ = select.select([c.fd for c in ok], [], [], 1.0)
            for c in ok:
                if c.fd in ready:
                    c.dqbuf(); count[c.dev] += 1
        print("  frames in 2 s:", count)
        for c in ok:
            c.streamoff()
    for c in cams:
        c.close()
    print(f"{pf} {w}x{h}: {len(ok)}/{len(devs)} concurrent streams")


def rounds(devs, w=1280, h=720, pf="MJPG", k=1, n=3):
    cams = [Cam(d) for d in devs]
    for c in cams:
        c.open(); c.s_fmt(w, h, pf); c.reqbufs(4)
    for r in range(n):
        t0 = time.perf_counter(); per = []
        for c in cams:
            t1 = time.perf_counter()
            c.queue_all(); c.streamon(); c.frames(k); c.streamoff()
            per.append(f"{time.perf_counter() - t1:.2f}")
        print(f"round {r}: per camera {per} s => {time.perf_counter() - t0:.2f}s")
    for c in cams:
        c.close()


def continuous(devs, w=640, h=480, pf="MJPG", seconds=5):
    cams = [Cam(d) for d in devs]
    t0 = time.perf_counter()
    for c in cams:
        c.open(); c.s_fmt(w, h, pf); c.reqbufs(3); c.queue_all(); c.streamon()
    print(f"open all: {time.perf_counter() - t0:.2f}s, alt settings {[alt_setting(c.dev) for c in cams]}")
    count = {c.dev: 0 for c in cams}
    ru0 = resource.getrusage(resource.RUSAGE_SELF); t0 = time.perf_counter()
    while time.perf_counter() - t0 < seconds:
        ready, _, _ = select.select([c.fd for c in cams], [], [], 0.5)
        for c in cams:
            if c.fd in ready:
                c.dqbuf(); count[c.dev] += 1
    ru1 = resource.getrusage(resource.RUSAGE_SELF)
    cpu = (ru1.ru_utime - ru0.ru_utime + ru1.ru_stime - ru0.ru_stime) / seconds * 100
    print(f"frames in {seconds} s: {count}  CPU {cpu:.1f}%")
    t0 = time.perf_counter()
    for c in cams:
        c.streamoff(); c.close()
    print(f"close all: {time.perf_counter() - t0:.2f}s")


if __name__ == "__main__":
    args = sys.argv[1:] or ["stages"]
    kw = dict(a.split("=", 1) for a in args[1:])
    devs = kw.pop("devs", "").split(",") if "devs" in kw else sorted(glob.glob("/dev/v4l/by-path/*-usb-0:*-video-index0"))
    kw = {k: (v if k == "pf" else int(v)) for k, v in kw.items()}
    globals()[args[0]](devs, **kw)
