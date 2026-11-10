#!/usr/bin/env python3
"""Make a watchable preview of a recorded take: RGB | colourised depth, side by side.

depth.mkv stores raw distances (uint16 millimetres), which a video player shows as an
almost black image. This tool maps distance to colour (red = near, blue = far,
black = no depth) and writes a normal H.264 MP4 that QuickTime can play.

    python tools/view_depth.py <camera_dir> [--near 500 --far 3000] [--no-rgb] [-o out.mp4]

<camera_dir> is one camera folder of a take, e.g. <session>/001_HELLO/front_1844…/
The data files are not changed; the preview is a separate, lossy file for viewing only.
"""
import argparse
import csv
from fractions import Fraction
from pathlib import Path

import av
import numpy as np


def _find(cam_dir: Path, suffix: str) -> Path | None:
    found = [f for f in sorted(cam_dir.glob(f"*{suffix}")) if "_part" not in f.name and "preview" not in f.name]
    return found[0] if found else None


def _timestamps(path: Path) -> np.ndarray:
    with open(path, newline="") as f:
        return np.array([int(r["host_ts_us"]) for r in csv.DictReader(f) if r["written"] == "1"], np.int64)


def turbo(x: np.ndarray) -> np.ndarray:
    """Turbo colormap (Google's polynomial approximation), x in [0, 1] -> uint8 RGB."""
    r = 0.13572138 + x * (4.61539260 + x * (-42.66032258 + x * (132.13108234 + x * (-152.94239396 + x * 59.28637943))))
    g = 0.09140261 + x * (2.19418839 + x * (4.84296658 + x * (-14.18503333 + x * (4.27729857 + x * 2.82956604))))
    b = 0.10667330 + x * (12.64194608 + x * (-60.58204836 + x * (110.36276771 + x * (-89.90310912 + x * 27.34824973))))
    return (np.clip(np.stack((r, g, b), -1), 0, 1) * 255).astype(np.uint8)


def colorize(depth: np.ndarray, near: float, far: float) -> np.ndarray:
    valid = depth > 0
    t = np.clip((depth.astype(np.float32) - near) / max(far - near, 1), 0, 1)
    # near -> red (1.0), far -> blue (0.12); the bottom of turbo is near-black and would look like "no depth"
    x = 1.0 - 0.88 * t
    rgb = turbo(x)
    rgb[~valid] = 0
    return rgb


def auto_range(depth_path: Path, frames: int = 30) -> tuple[float, float]:
    vals = []
    with av.open(str(depth_path)) as c:
        for i, f in enumerate(c.decode(video=0)):
            d = f.to_ndarray()
            vals.append(d[d > 0][::7])
            if i + 1 >= frames:
                break
    v = np.concatenate(vals) if vals else np.array([])
    if v.size == 0:
        return 300.0, 3000.0
    return float(np.percentile(v, 2)), float(np.percentile(v, 98))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("camera_dir", type=Path)
    ap.add_argument("--near", type=float, help="distance shown as red, mm (default: automatic)")
    ap.add_argument("--far", type=float, help="distance shown as blue, mm (default: automatic)")
    ap.add_argument("--no-rgb", action="store_true", help="depth only, no RGB next to it")
    ap.add_argument("-o", "--output", type=Path, help="default: <camera_dir>/<name>_depth_preview.mp4")
    a = ap.parse_args()

    depth_path = _find(a.camera_dir, "depth.mkv")
    if depth_path is None:
        raise SystemExit(f"No depth.mkv in {a.camera_dir} (was the Depth stream enabled?)")
    rgb_path = None if a.no_rgb else _find(a.camera_dir, "rgb.mp4")
    out = a.output or depth_path.with_name(depth_path.name.replace("depth.mkv", "depth_preview.mp4"))

    near, far = auto_range(depth_path)
    near, far = a.near or near, a.far or far
    print(f"colour range: {near:.0f} mm (red) … {far:.0f} mm (blue); black = no depth")

    d_ts = _timestamps(depth_path.with_name(depth_path.stem + "_timestamps.csv"))
    rgb_frames, rgb_ts = None, None
    if rgb_path is not None:
        rgb_ts = _timestamps(rgb_path.with_name(rgb_path.stem + "_timestamps.csv"))
        rgb_container = av.open(str(rgb_path))
        rgb_frames = rgb_container.decode(video=0)

    rgb_idx, rgb_img = -1, None
    fps = 30
    with av.open(str(depth_path)) as dc:
        dstream = dc.streams.video[0]
        if dstream.average_rate:
            fps = round(float(dstream.average_rate))
        out_c = av.open(str(out), "w")
        enc = None
        n = 0
        for i, frame in enumerate(dc.decode(dstream)):
            depth = frame.to_ndarray()
            h, w = depth.shape
            tile = colorize(depth, near, far)
            if rgb_frames is not None:
                # advance the RGB video to the frame closest in time to this depth frame
                target = int(np.argmin(np.abs(rgb_ts - d_ts[min(i, len(d_ts) - 1)]))) if len(rgb_ts) else 0
                while rgb_idx < target:
                    try:
                        f = next(rgb_frames)
                    except StopIteration:
                        break
                    rgb_idx += 1
                    if rgb_idx == target:
                        rgb_img = f.reformat(width=w, height=h, format="rgb24").to_ndarray()
                if rgb_img is not None:
                    tile = np.concatenate((rgb_img, tile), axis=1)
                else:
                    tile = np.concatenate((np.zeros_like(tile), tile), axis=1)
            if enc is None:
                enc = out_c.add_stream("libx264", rate=Fraction(fps))
                enc.width, enc.height, enc.pix_fmt = tile.shape[1], tile.shape[0], "yuv420p"
                enc.options = {"crf": "18"}
            vf = av.VideoFrame.from_ndarray(np.ascontiguousarray(tile), format="rgb24")
            for pkt in enc.encode(vf):
                out_c.mux(pkt)
            n += 1
        if enc is not None:
            for pkt in enc.encode(None):
                out_c.mux(pkt)
        out_c.close()
    if rgb_frames is not None:
        rgb_container.close()
    print(f"wrote {n} frames -> {out}")


if __name__ == "__main__":
    main()
