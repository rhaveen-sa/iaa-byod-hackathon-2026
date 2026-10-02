"""Talking to the BYOD camera over plain HTTP.

The hackathon camera exposes an MJPEG stream at /video_feed. MJPEG is a
multipart/x-mixed-replace response: a boundary line, part headers, JPEG bytes,
repeat. We parse it with `requests` and no image library at all, which matters
because the runtime pip-installs app/requirements.txt on every single run -
keeping OpenCV out of the hot path turns a two-minute startup into a two-second
one.

Framing uses the multipart boundary, not a scan for JPEG markers. Scanning for
the first 0xFFD9 after 0xFFD8 looks simpler and is wrong: that byte pair occurs
inside EXIF/APP1 embedded thumbnails (which are themselves complete JPEGs) and
inside COM/APPn segment payloads, so a camera emitting EXIF thumbnails yields
silently TRUNCATED frames. Marker scanning is kept only as a fallback for
servers that send no usable boundary, and it is labelled as such.
"""

import logging
import re
import threading
import time

import requests

import config

logger = logging.getLogger(__name__)

_SOI = b"\xff\xd8"  # JPEG start-of-image marker
_EOI = b"\xff\xd9"  # JPEG end-of-image marker


class CameraError(RuntimeError):
    pass


def _boundary_from(content_type):
    """Pull the multipart boundary out of a Content-Type header."""
    if not content_type:
        return None
    match = re.search(r"boundary=([^;]+)", content_type, re.IGNORECASE)
    if not match:
        return None
    token = match.group(1).strip().strip('"')
    return token.encode("latin-1", "replace") if token else None


def _content_length_from(headers_blob):
    match = re.search(rb"content-length:\s*(\d+)", headers_blob, re.IGNORECASE)
    return int(match.group(1)) if match else None


class MjpegCamera:
    """Streams JPEG frames from an MJPEG endpoint.

    Usage:
        for jpeg_bytes in MjpegCamera().frames():
            ...
    """

    def __init__(self, url=None, sample_interval=None, stop_event=None):
        self.url = url or config.CAM["video_feed"]
        self.sample_interval = (
            config.SAMPLE_INTERVAL if sample_interval is None else sample_interval
        )
        # Shared with the owner so a shutdown breaks the reconnect loop. Without
        # it, frames() retries forever and the reader thread never exits.
        self.stop_event = stop_event or threading.Event()

    def frames(self, max_buffer=8_000_000):
        """Yield one JPEG frame at a time, throttled to sample_interval.

        Reconnects on its own: ngrok tunnels drop, and a demo that dies because
        the tunnel blinked is a demo that does not get a score.
        """
        while not self.stop_event.is_set():
            try:
                yield from self._stream_once(max_buffer)
            except (requests.RequestException, CameraError) as exc:
                if self.stop_event.is_set():
                    return
                logger.warning("camera stream dropped (%s); reconnecting in 2s", exc)
                # Interruptible sleep, so shutdown does not wait out the retry.
                if self.stop_event.wait(2):
                    return

    def _stream_once(self, max_buffer):
        logger.info("connecting to camera %s", self.url)
        with requests.get(self.url, stream=True, timeout=config.HTTP_TIMEOUT) as response:
            response.raise_for_status()
            boundary = _boundary_from(response.headers.get("Content-Type"))
            if boundary:
                yield from self._parse_multipart(response, boundary, max_buffer)
            else:
                logger.warning(
                    "no multipart boundary in Content-Type; falling back to JPEG "
                    "marker scanning, which truncates frames carrying an embedded EOI"
                )
                yield from self._parse_markers(response, max_buffer)
        raise CameraError("stream ended")

    def _throttle(self, last_yield):
        """Return the new last_yield time, or None if this frame is dropped."""
        now = time.monotonic()
        if last_yield is not None and now - last_yield < self.sample_interval:
            return None
        return now

    def _parse_multipart(self, response, boundary, max_buffer):
        delimiter = b"--" + boundary
        buffer = bytearray()
        last_yield = None

        for chunk in response.iter_content(chunk_size=8192):
            if self.stop_event.is_set():
                return
            if not chunk:
                continue
            buffer.extend(chunk)
            if len(buffer) > max_buffer:
                raise CameraError("part exceeded max_buffer; boundary may be wrong")

            while True:
                start = buffer.find(delimiter)
                if start < 0:
                    break
                header_end = buffer.find(b"\r\n\r\n", start)
                if header_end < 0:
                    break
                headers = bytes(buffer[start:header_end])
                body_start = header_end + 4

                length = _content_length_from(headers)
                if length is not None:
                    body_end = body_start + length
                    if len(buffer) < body_end:
                        break  # wait for the rest of this part
                else:
                    # No Content-Length: the part runs to the next boundary.
                    nxt = buffer.find(delimiter, body_start)
                    if nxt < 0:
                        break
                    body_end = nxt
                    while body_end > body_start and buffer[body_end - 1] in (0x0A, 0x0D):
                        body_end -= 1

                part = bytes(buffer[body_start:body_end])
                del buffer[:body_end]

                if not part.startswith(_SOI):
                    continue  # not an image part (some servers send a preamble)

                stamp = self._throttle(last_yield)
                if stamp is None:
                    continue  # sampling: drop this frame
                last_yield = stamp
                yield part

    def _parse_markers(self, response, max_buffer):
        """Fallback only. See the module docstring for why this is not the default."""
        buffer = bytearray()
        last_yield = None

        for chunk in response.iter_content(chunk_size=8192):
            if self.stop_event.is_set():
                return
            if not chunk:
                continue
            buffer.extend(chunk)
            if len(buffer) > max_buffer:
                raise CameraError("no JPEG frame found; is this really an MJPEG feed?")

            while True:
                start = buffer.find(_SOI)
                if start < 0:
                    break
                end = buffer.find(_EOI, start + 2)
                if end < 0:
                    break
                frame = bytes(buffer[start : end + 2])
                del buffer[: end + 2]

                stamp = self._throttle(last_yield)
                if stamp is None:
                    continue
                last_yield = stamp
                yield frame

    def grab_one(self) -> bytes:
        """Single frame, for a quick connectivity check."""
        for frame in self.frames():
            return frame
        raise CameraError("no frame received")


def upload_snapshot(jpeg_bytes: bytes, filename: str = "event.jpg") -> bool:
    """POST an evidence frame to the camera's /upload_snapshot endpoint.

    The guidelines say the uploaded image then shows up at the /view URL, which
    is what you embed in the playground dashboard. Failure is never fatal:
    losing the evidence image must not take the vehicle response down with it.
    """
    url = config.CAM.get("upload_snapshot")
    if not url:
        logger.warning("no upload_snapshot URL configured; skipping evidence upload")
        return False
    try:
        response = requests.post(
            url,
            files={"file": (filename, jpeg_bytes, "image/jpeg")},
            timeout=config.HTTP_TIMEOUT,
        )
        response.raise_for_status()
        logger.info("evidence uploaded, visible at %s", config.CAM.get("view"))
        return True
    except requests.RequestException as exc:
        logger.warning("evidence upload failed: %s", exc)
        return False
