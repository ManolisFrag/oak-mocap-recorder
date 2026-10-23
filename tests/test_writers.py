"""Writer tests: run with `python -m pytest tests` or `python tests/test_writers.py`."""
import sys, tempfile, time
from fractions import Fraction
from pathlib import Path

import av
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mocap_recorder.writers import EncodedVideoWriter, Ffv1DepthWriter


def _fake_device_bitstream(codec, n=60, w=640, h=360, fps=30):
    """Produce Annex-B / JPEG packets like the OAK VideoEncoder does."""
    enc = av.CodecContext.create({"h264": "libx264", "hevc": "libx265", "mjpeg": "mjpeg"}[codec], "w")
    enc.width, enc.height, enc.time_base = w, h, Fraction(1, fps)
    enc.pix_fmt = "yuvj420p" if codec == "mjpeg" else "yuv420p"
    if codec != "mjpeg":
        enc.options = {"bf": "0", "g": str(fps)}
        enc.max_b_frames = 0
    out = []
    for i in range(n):
        img = np.full((h, w, 3), (i * 4) % 255, np.uint8)
        f = av.VideoFrame.from_ndarray(img, format="rgb24").reformat(format=enc.pix_fmt)
        f.pts = i
        out += [(bytes(p), p.is_keyframe) for p in enc.encode(f)]
    out += [(bytes(p), p.is_keyframe) for p in enc.encode(None)]
    return out


def test_encoded_mux():
    for codec in ("h264", "hevc", "mjpeg"):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / f"v_{codec}.mp4"
            w = EncodedVideoWriter(path, codec, 640, 360, 30)
            t0 = 123_456_789
            pkts = _fake_device_bitstream(codec)
            for i, (data, key) in enumerate(pkts):
                w.write(data, t0 + i * 33_333, key)
            w.close()
            with av.open(str(path)) as c:
                frames = sum(1 for _ in c.decode(video=0))
            assert frames == len(pkts), (codec, frames, len(pkts))
            print(f"ok {codec}: {frames} frames, {path.stat().st_size} bytes")


def test_ffv1_roundtrip():
    rng = np.random.default_rng(0)
    w, h, n = 1280, 720, 90
    frames = [rng.integers(0, 65535, (h, w), dtype=np.uint16) for _ in range(5)]
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "depth.mkv"
        wr = Ffv1DepthWriter(path, w, h, 30)
        t = time.perf_counter()
        for i in range(n):
            # realistic depth: smooth-ish values + noise
            assert wr.write(frames[i % 5] if i < 5 else (frames[i % 5] // 16), i * 33_333)
        wr.close()
        dt = time.perf_counter() - t
        assert wr.error is None and wr.frames == n
        with av.open(str(path)) as c:
            back = [f.to_ndarray() for f in c.decode(video=0)]
        assert len(back) == n
        for i in range(n):
            ref = frames[i % 5] if i < 5 else frames[i % 5] // 16
            assert back[i].dtype == np.uint16 and np.array_equal(back[i], ref), i
        print(f"ok ffv1 lossless: {n} frames 1280x720 in {dt:.2f}s ({n/dt:.0f} fps, worst-case noise)")


if __name__ == "__main__":
    test_encoded_mux()
    test_ffv1_roundtrip()
