# uvc-multicam

Live preview from **six USB (UVC) cameras on one USB 2.0 bus** with nothing but the Python standard library.
Measured on a Raspberry Pi CM4 (1 GB), Debian Trixie, kernel 6.18, six Arducam B030401 12 MP modules (`0c40:0304`, MJPEG only)
behind two 4-port hubs.

`uvc_stream.py` is ~80 lines: V4L2 ioctls + `mmap`, no numpy, no OpenCV, no ffmpeg, no third-party module. Six 640x480 MJPEG streams
run at 30 fps each for **4.5 % CPU and 11 MB RAM**, and the newest JPEG per camera is always in memory.

## The problem

We needed a six-cell operator preview, plus a full-resolution still from one camera on demand.

- Grabbing one frame per camera in turn (`ffmpeg -frames:v 4`, or fswebcam, or v4l2-ctl) took ~1.2 s per camera, so a round over six cameras took 8-12 s.
- More than two 1280x720 streams at once failed with `Not enough bandwidth for altsetting 5`.
- `uvcvideo quirks=128` (FIX_BANDWIDTH) did nothing: it only applies to uncompressed formats.
- Lowering the frame rate did nothing: the camera advertises only 30 fps.

## What the measurements showed

Per-stage cost of one 720p MJPEG capture, measured at the ioctl level (`bench_uvc.py stages`):

| Stage | Time |
|---|---|
| `open()` | 0 ms |
| `VIDIOC_S_FMT` (UVC probe) | 225 ms |
| `REQBUFS` + `mmap` | 5 ms |
| `VIDIOC_STREAMON` (UVC commit, firmware starts the sensor) | 470 ms |
| first frame after STREAMON | 240-365 ms |
| `VIDIOC_STREAMOFF` | 80 ms |

STREAMON plus first frame is ~0.85 s of **camera firmware time**, and it is the same for 720p, 480p and YUYV 320x240.
Keeping the file descriptor open saves only the S_FMT. Starting streams in parallel threads does not overlap much either
(STREAMON calls serialise in the kernel/USB stack). So a "round" over six cameras cannot get below ~3-5 s. The only way
out is to never stop the streams.

Whether six streams fit depends on the **alternate setting the camera firmware asks for**, not on the actual data rate.
USB 2.0 reserves at most 80 % of a 125 us microframe for isochronous traffic, about 6000 bytes (`bench_uvc.py fit`):

| Format | Alt setting | Bytes per microframe | Streams that fit |
|---|---|---|---|
| MJPEG 1280x720 | 5 | 2400 | 2 |
| MJPEG 1920x1080, MJPEG 4608x2592, YUYV 640x480 | 6 | 3072 | 1 |
| YUYV 320x240 | 4 | 1600 | 3 |
| **MJPEG 640x480** | **3** | **800** | **6** (7 would fit) |

The kernel picks the smallest alt setting whose packet size covers the firmware's `dwMaxPayloadTransferSize`; for
compressed formats it cannot second-guess that number (Arducam's answer on their forum: "a customized firmware is required").
So the fix is to pick the resolution the firmware happens to price cheaply.

Exposure: on a warmed-up camera the first frame after STREAMON is already fully exposed, even after 5 s of STREAMOFF,
so skipping frames is unnecessary. Only the very first stream after power-up ramps over ~12 frames.

## The method

1. Open all cameras once at 640x480 MJPEG (`Camera.open()`), keep them streaming.
2. In a `select()` loop call `Camera.latest()` on whichever descriptor is readable. It drains the queue and returns the newest JPEG; serve that over HTTP.
3. Before a high-resolution still, `Camera.close()` every camera. `VIDIOC_STREAMOFF` returns the interface to alt setting 0, so the bus is free at once (six cameras: 0.46 s). Run your still capture (we use `fswebcam -r 4608x2592`), then `open()` again (six cameras in parallel threads: 2.9 s; sequential: 4.2 s).
4. If nobody is watching, close the streams; reopen on the first request (first frames after ~3 s).

This is the kernel's ordinary "Streaming I/O (Memory Mapping)" method (`V4L2_MEMORY_MMAP`) with a latest-frame drain loop.
It is what µStreamer and mjpg-streamer do internally; the difference is that here it lives inside your own process, so
stopping and restarting the streams around a still capture is two plain ioctls under your own lock.

```python
import select
from uvc_stream import Camera

cams = [Camera(f"/dev/video{n}") for n in (0, 2, 4, 6, 8, 17)]
for c in cams:
    c.open()                      # 640x480 MJPG, 3 buffers
latest = [None] * len(cams)
while True:
    ready, _, _ = select.select([c.fd for c in cams], [], [], 0.5)
    for i, c in enumerate(cams):
        if c.fd in ready:
            latest[i] = c.latest() or latest[i]   # JPEG bytes, serve as image/jpeg
```

## Files

- `uvc_stream.py` — the module (`Camera.open / latest / close`), with a self-check: `python3 uvc_stream.py /dev/video0 /dev/video2 ...`
- `bench_uvc.py` — the measurements above: `stages`, `fit`, `rounds`, `continuous` (see the docstring)

Struct layouts are the 64-bit Linux ones (`v4l2_format` 208 B, `v4l2_requestbuffers` 20 B, `v4l2_buffer` 88 B). Tested on aarch64 with Python 3.13.

## Things that did not help

- `uvcvideo quirks=128`: uncompressed formats only.
- Frame rate: the camera only offers 30 fps.
- Keeping descriptors open and toggling STREAMON/STREAMOFF per camera: saves 225 ms per camera, round still 5.7 s.
- Parallel STREAMON: 2 x 720p waves 4.2 s, 6 x 480p wave 3.3 s.
- Third-party wrappers (linuxpy, v4l2py, python-v4l2capture, OpenCV `VideoCapture`, pyuvc, µStreamer): none change the firmware's
  bandwidth request or the STREAMON cost; pyuvc even takes the device away from `uvcvideo`, so other tools cannot use it.

## References

- V4L2 Streaming I/O (Memory Mapping): https://docs.kernel.org/userspace-api/media/v4l/mmap.html
- `uvc_video.c` alt setting selection and `usb_set_interface(..., 0)` on stop: https://github.com/torvalds/linux/blob/master/drivers/media/usb/uvc/uvc_video.c
- Why USB isochronous bandwidth errors occur (80 % rule): https://www.thegoodpenguin.co.uk/blog/understanding-why-usb-isochronous-bandwidth-errors-occur/
- Multiple UVC cameras on Linux: https://www.thegoodpenguin.co.uk/blog/multiple-uvc-cameras-on-linux/
- Arducam forum, UVC bandwidth over-reporting: https://forum.arducam.com/t/linux-uvc-driver-bandwidth-issues/6731

MIT license.
