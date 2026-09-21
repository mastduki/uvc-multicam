# uvc-multicam

Live preview from many USB (UVC) cameras on one USB 2.0 bus, plus an occasional high-resolution still from one of them,
with nothing but the Python standard library: V4L2 ioctls and `mmap`, no numpy, no OpenCV, no ffmpeg, no third-party module.

## Why the obvious approach is slow

The obvious way to preview N cameras on a bus that cannot stream them all at full resolution is to grab one frame per
camera in turn. It does not scale, for two reasons that are both outside your code:

1. **Starting a UVC stream is camera firmware time.** After `VIDIOC_STREAMON` the camera reprograms its sensor and
   pipeline and delivers the first frame roughly half a second to a second later. The cost is the same at every
   resolution, keeping the file descriptor open does not remove it, and starting several cameras in parallel does not
   overlap it much because the requests serialise in the USB stack. A round over N cameras is therefore N times that.
2. **Bandwidth is reserved, not measured.** For every stream the kernel reserves isochronous bandwidth according to the
   *alternate setting the camera asks for*, which the firmware derives from the resolution, not from the real data rate.
   USB 2.0 hands out about 80 % of each 125 µs microframe to such reservations. Once the reservations do not fit, the
   next `STREAMON` fails with `ENOSPC` ("Not enough bandwidth for altsetting N") no matter how little data actually flows.

## The method

- **Never stop the streams.** Open every camera once, at the largest resolution whose reservation still lets all of
  them fit on the bus, and keep them streaming. The preview is then live and the firmware start-up cost is paid once.
- **Keep only the newest frame.** Use the kernel's memory-mapped streaming I/O (`V4L2_MEMORY_MMAP`): a few buffers per
  camera, a `select()` loop over all descriptors, and on each wake-up dequeue everything that is waiting and keep the
  last buffer. With MJPEG the buffer already is a JPEG, so serving it over HTTP is a copy, not a decode.
- **Hand the bus over for the still.** `VIDIOC_STREAMOFF` returns the interface to alternate setting 0 and frees its
  reservation immediately. Close every camera (a few hundred milliseconds for six), take the still with whatever tool you
  like, then open the streams again. If nobody is watching the preview, close them as well.

In our case that meant six cameras at 640x480 MJPEG streaming continuously at 30 fps for a few percent of one CPU core
on a 1 GB Raspberry Pi, a preview that feels live, and the same six cameras idle within half a second whenever one of
them has to take a 12 MP still.

## Usage

```python
import select
from uvc_stream import Camera

cams = [Camera(f"/dev/video{n}") for n in (0, 2, 4)]
for c in cams:
    c.open(640, 480, "MJPG")          # S_FMT, REQBUFS, mmap, QBUF, STREAMON
latest = [None] * len(cams)
while True:
    ready, _, _ = select.select([c.fd for c in cams], [], [], 0.5)
    for i, c in enumerate(cams):
        if c.fd in ready:
            latest[i] = c.latest() or latest[i]   # newest JPEG as bytes, serve as image/jpeg

# before a high-resolution still:
for c in cams:
    c.close()                          # STREAMOFF + close: bus is free now
# ... take the still ...
for c in cams:
    c.open(640, 480, "MJPG")
```

Find the format that fits your cameras and bus before hard-coding one:

```
python3 bench_uvc.py fit w=640 h=480 pf=MJPG     # how many cameras start, which alternate setting each one uses
python3 bench_uvc.py stages                       # per-stage cost of one capture: open, S_FMT, STREAMON, first frame
python3 bench_uvc.py continuous w=640 h=480       # all cameras at once: fps per camera, CPU, time to close them all
```

## Files

- `uvc_stream.py` — the module (`Camera.open / latest / close`, ~80 lines) with a self-check: `python3 uvc_stream.py /dev/video0 /dev/video2 ...`
- `bench_uvc.py` — the measurements: `stages`, `fit`, `rounds`, `continuous` (see its docstring)

Struct layouts are the 64-bit Linux ones (`v4l2_format` 208 B, `v4l2_requestbuffers` 20 B, `v4l2_buffer` 88 B); tested on aarch64 with Python 3.13, needs Python 3.8 or newer.

## References

- V4L2 Streaming I/O (Memory Mapping): https://docs.kernel.org/userspace-api/media/v4l/mmap.html
- `uvc_video.c`, alternate setting selection at start and `usb_set_interface(..., 0)` at stop: https://github.com/torvalds/linux/blob/master/drivers/media/usb/uvc/uvc_video.c
- Why USB isochronous bandwidth errors occur: https://www.thegoodpenguin.co.uk/blog/understanding-why-usb-isochronous-bandwidth-errors-occur/

MIT license.
