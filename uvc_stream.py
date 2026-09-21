"""Keep several UVC (USB) cameras streaming MJPEG at once and hand out the latest JPEG, using only the
Python standard library (V4L2 ioctl + mmap, the kernel's "Streaming I/O (Memory Mapping)" method).

Why: on a single USB 2.0 bus the isochronous budget is ~6000 B per 125 us microframe. A UVC camera's firmware
decides how much of it a stream reserves (dwMaxPayloadTransferSize -> alternate setting). On Arducam
B030401 (0c40:0304) 1280x720 MJPEG reserves alt 5 = 2400 B (2 streams fit), 640x480 MJPEG reserves
alt 3 = 800 B (6 fit, 7 possible). Starting a stream (STREAMON) costs ~0.5 s of firmware time regardless
of format, so streams are kept open and only closed when the bus must be handed to a high-resolution still.

Struct layouts are for 64-bit Linux (aarch64 and x86-64 share them). Python >= 3.8.
"""
import mmap
import os
import struct

try:
    import fcntl
except ImportError:  # keeps the module importable on non-Linux for tests; open() raises OSError
    fcntl = None

_S_FMT, _REQBUFS, _QUERYBUF, _QBUF, _DQBUF = 0xC0D05605, 0xC0145608, 0xC0585609, 0xC058560F, 0xC0585611
_STREAMON, _STREAMOFF = 0x40045612, 0x40045613
_CAPTURE, _MMAP = 1, 1  # V4L2_BUF_TYPE_VIDEO_CAPTURE, V4L2_MEMORY_MMAP
_BUF_FLAG_ERROR = 0x40


def fourcc(code):
    return struct.unpack("<I", code.encode())[0]


def _v4l2_buffer(index=0):
    b = bytearray(88)  # struct v4l2_buffer, 64-bit layout
    struct.pack_into("<II", b, 0, index, _CAPTURE)
    struct.pack_into("<I", b, 60, _MMAP)
    return b


class Camera:
    """open() -> latest() -> close(). While closed the device is free for another process (e.g. fswebcam)."""

    def __init__(self, device):
        self.device = device
        self.fd = None
        self.buffers = []

    @property
    def is_open(self):
        return self.fd is not None

    def open(self, width=640, height=480, pixel_format="MJPG", nbuffers=3):
        if fcntl is None:
            raise OSError("V4L2 needs Linux")
        self.fd = os.open(self.device, os.O_RDWR | os.O_NONBLOCK)
        try:
            f = bytearray(208)  # struct v4l2_format; pix at offset 8
            struct.pack_into("<I", f, 0, _CAPTURE)
            struct.pack_into("<IIII", f, 8, width, height, fourcc(pixel_format), 1)
            fcntl.ioctl(self.fd, _S_FMT, f)  # UVC probe; the driver's format is per device, so set it on every open
            r = bytearray(20)  # struct v4l2_requestbuffers
            struct.pack_into("<III", r, 0, nbuffers, _CAPTURE, _MMAP)
            fcntl.ioctl(self.fd, _REQBUFS, r)
            for i in range(struct.unpack_from("<I", r)[0]):
                q = _v4l2_buffer(i)
                fcntl.ioctl(self.fd, _QUERYBUF, q)
                offset, length = struct.unpack_from("<I", q, 64)[0], struct.unpack_from("<I", q, 72)[0]
                self.buffers.append(mmap.mmap(self.fd, length, mmap.MAP_SHARED, mmap.PROT_READ, offset=offset))
                fcntl.ioctl(self.fd, _QBUF, _v4l2_buffer(i))
            fcntl.ioctl(self.fd, _STREAMON, struct.pack("<i", _CAPTURE))  # ENOSPC here = no USB bandwidth left
        except OSError:
            self.close()
            raise

    def latest(self):
        """Drain queued frames, return the newest one as bytes (a JPEG for MJPG), or None if nothing arrived."""
        newest = None
        while self.fd is not None:
            b = _v4l2_buffer()
            try:
                fcntl.ioctl(self.fd, _DQBUF, b)
            except BlockingIOError:
                break
            index, _, used, flags = struct.unpack_from("<IIII", b)
            if used and not flags & _BUF_FLAG_ERROR:
                newest = bytes(self.buffers[index][:used])  # copy: the mmap slot is requeued right away
            fcntl.ioctl(self.fd, _QBUF, _v4l2_buffer(index))
        return newest

    def close(self):
        if self.fd is None:
            return
        try:
            fcntl.ioctl(self.fd, _STREAMOFF, struct.pack("<i", _CAPTURE))  # frees the USB bandwidth (alt setting 0)
        except OSError:
            pass
        for m in self.buffers:
            m.close()
        self.buffers = []
        os.close(self.fd)  # releases buffer ownership (same as REQBUFS 0)
        self.fd = None


if __name__ == "__main__":  # self-check: python3 uvc_stream.py /dev/video0 /dev/video2 ...
    import select
    import sys
    import time

    cams = [Camera(d) for d in sys.argv[1:]] or [Camera("/dev/video0")]
    t0 = time.perf_counter()
    for c in cams:
        c.open()
    print(f"open {len(cams)} camera(s): {time.perf_counter() - t0:.2f} s")
    counts = [0] * len(cams)
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 3:
        ready, _, _ = select.select([c.fd for c in cams], [], [], 0.5)
        for j, c in enumerate(cams):
            if c.fd in ready and c.latest():
                counts[j] += 1
    print("frames in 3 s per camera:", counts)
    t0 = time.perf_counter()
    for c in cams:
        c.close()
    print(f"close all: {time.perf_counter() - t0:.2f} s")
    assert all(n > 30 for n in counts), "expected >10 fps from every camera"
    print("OK")
