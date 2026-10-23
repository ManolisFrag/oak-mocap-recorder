"""File writers used by camera workers.

All writers are fed from a worker thread and must never block the DepthAI
output queues for long. Heavy work (FFV1 encoding) runs in its own thread with
a bounded buffer so a short disk stall does not drop frames.
"""

from __future__ import annotations

import csv
import queue
import threading
from fractions import Fraction
from pathlib import Path

import av
import numpy as np

US = Fraction(1, 1_000_000)  # all pts are expressed in microseconds

# Annex-B NAL unit types that carry decoder configuration.
_H264_CONFIG_NALS = {7, 8}          # SPS, PPS
_HEVC_CONFIG_NALS = {32, 33, 34}    # VPS, SPS, PPS


def _split_annexb(data: bytes) -> list[bytes]:
    """Split an Annex-B bitstream into NAL units (without start codes)."""
    nals, i, n = [], 0, len(data)
    starts = []
    while i < n - 3:
        if data[i] == 0 and data[i + 1] == 0:
            if data[i + 2] == 1:
                starts.append((i, 3)); i += 3; continue
            if data[i + 2] == 0 and i + 3 < n and data[i + 3] == 1:
                starts.append((i, 4)); i += 4; continue
        i += 1
    for k, (pos, sc) in enumerate(starts):
        end = starts[k + 1][0] if k + 1 < len(starts) else n
        nals.append(data[pos + sc:end])
    return nals


def _config_extradata(codec: str, data: bytes) -> bytes | None:
    """Collect SPS/PPS(/VPS) from a keyframe as Annex-B extradata."""
    out = []
    for nal in _split_annexb(data):
        if not nal:
            continue
        t = (nal[0] & 0x1F) if codec == "h264" else ((nal[0] >> 1) & 0x3F)
        if t in (_H264_CONFIG_NALS if codec == "h264" else _HEVC_CONFIG_NALS):
            out.append(b"\x00\x00\x00\x01" + nal)
    return b"".join(out) or None


class EncodedVideoWriter:
    """Muxes on-device encoded frames (H.264 / H.265 / MJPEG) into an MP4.

    No re-encoding happens on the host; packets are copied as-is with
    presentation times taken from the device timestamps.
    """

    def __init__(self, path: Path, codec: str, width: int, height: int, fps: float):
        assert codec in ("h264", "hevc", "mjpeg")
        self.path, self.codec = Path(path), codec
        self.width, self.height, self.fps = width, height, fps
        self._container = None
        self._stream = None
        self._last_pts = -1
        self.frames = 0

    def _open(self, first_frame: bytes):
        self._container = av.open(str(self.path), "w", format="mp4",
                                  options={"movflags": "+faststart"})
        s = self._container.add_stream(self.codec, rate=Fraction(self.fps).limit_denominator(1000))
        s.width, s.height = self.width, self.height
        s.time_base = US
        if self.codec == "mjpeg":
            s.pix_fmt = "yuvj420p"
        else:
            s.pix_fmt = "yuv420p"
            extradata = _config_extradata(self.codec, first_frame)
            if extradata:
                s.codec_context.extradata = extradata
            if self.codec == "hevc":
                s.codec_tag = "hvc1"  # plays in QuickTime / Apple frameworks
        self._stream = s

    def write(self, data: bytes, pts_us: int, keyframe: bool):
        if self._container is None:
            if self.codec != "mjpeg" and not keyframe:
                return False  # wait for the first IDR so the file is decodable
            self._open(data)
        pts = max(int(pts_us), self._last_pts + 1)
        self._last_pts = pts
        pkt = av.Packet(data)
        pkt.stream = self._stream
        pkt.time_base = US
        pkt.pts = pkt.dts = pts
        pkt.is_keyframe = bool(keyframe)
        self._container.mux(pkt)
        self.frames += 1
        return True

    def close(self):
        if self._container is not None:
            self._container.close()
            self._container = None


class Ffv1DepthWriter:
    """Lossless 16-bit depth (millimetres) into an FFV1 .mkv, encoded in a background thread."""

    def __init__(self, path: Path, width: int, height: int, fps: float, buffer_frames: int = 120):
        self.path = Path(path)
        self.width, self.height = width, height
        self._q: queue.Queue = queue.Queue(maxsize=buffer_frames)
        self._container = av.open(str(self.path), "w", format="matroska")
        s = self._container.add_stream("ffv1", rate=Fraction(fps).limit_denominator(1000),
                                       options={"level": "3", "slices": "16", "slicecrc": "1", "g": "1"})
        s.width, s.height, s.pix_fmt = width, height, "gray16le"
        s.time_base = US
        s.thread_type = "SLICE"
        s.thread_count = 4
        self._stream = s
        self._last_pts = -1
        self.frames = 0
        self.dropped = 0
        self.error: Exception | None = None
        self._thread = threading.Thread(target=self._run, name=f"ffv1-{self.path.name}", daemon=True)
        self._thread.start()

    def write(self, depth: np.ndarray, pts_us: int) -> bool:
        """Queue one uint16 frame. Returns False if the buffer was full (frame dropped)."""
        try:
            self._q.put_nowait((depth, int(pts_us)))
            return True
        except queue.Full:
            self.dropped += 1
            return False

    def backlog(self) -> int:
        return self._q.qsize()

    def _run(self):
        while True:
            item = self._q.get()
            if item is None:
                break
            depth, pts = item
            try:
                if depth.dtype != np.uint16:
                    depth = depth.astype(np.uint16)
                frame = av.VideoFrame.from_ndarray(np.ascontiguousarray(depth), format="gray16le")
                pts = max(pts, self._last_pts + 1)
                self._last_pts = pts
                frame.pts, frame.time_base = pts, US
                for pkt in self._stream.encode(frame):
                    self._container.mux(pkt)
                self.frames += 1
            except Exception as e:  # keep draining so close() never deadlocks
                self.error = e
        try:
            for pkt in self._stream.encode(None):
                self._container.mux(pkt)
        finally:
            self._container.close()

    def close(self):
        self._q.put(None)
        self._thread.join()


class CsvWriter:
    """Small line-buffered CSV writer."""

    def __init__(self, path: Path, header: list[str]):
        self._f = open(path, "w", newline="", buffering=1024 * 64)
        self._w = csv.writer(self._f)
        self._w.writerow(header)

    def row(self, *values):
        self._w.writerow(values)

    def close(self):
        self._f.close()


TIMESTAMP_HEADER = ["frame_idx", "sequence_num", "device_ts_us", "host_ts_us", "written"]
