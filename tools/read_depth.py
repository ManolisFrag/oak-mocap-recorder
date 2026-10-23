#!/usr/bin/env python3
"""Read depth.mkv (FFV1, uint16 millimetres) and look up depth at RGB pixels.

    python tools/read_depth.py <camera_dir> [--frame 0] [--u 960 --v 540]

Library use:
    from tools.read_depth import iter_depth, depth_at_rgb_pixel
"""
import argparse
import json
from pathlib import Path

import av
import numpy as np


def iter_depth(path):
    """Yield uint16 depth frames (H, W) in millimetres, 0 = invalid."""
    with av.open(str(path)) as c:
        for frame in c.decode(video=0):
            yield frame.to_ndarray()  # gray16le -> uint16


def depth_at_rgb_pixel(depth: np.ndarray, u: float, v: float, rgb_size, win: int = 2) -> float:
    """Median valid depth (mm) around RGB pixel (u, v). Depth is aligned to RGB, only scaled."""
    dh, dw = depth.shape
    x = int(round(u * dw / rgb_size[0])); y = int(round(v * dh / rgb_size[1]))
    patch = depth[max(0, y - win):y + win + 1, max(0, x - win):x + win + 1]
    vals = patch[patch > 0]
    return float(np.median(vals)) if vals.size else float("nan")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("camera_dir", type=Path)
    ap.add_argument("--frame", type=int, default=0)
    ap.add_argument("--u", type=float); ap.add_argument("--v", type=float)
    a = ap.parse_args()
    intr_file = next(f for f in a.camera_dir.glob("*intrinsics.json") if "_part" not in f.name)
    intr = json.loads(intr_file.read_text())
    rgb_size = (intr["rgb"]["width"], intr["rgb"]["height"]) if "rgb" in intr else None
    for i, d in enumerate(iter_depth(next(f for f in a.camera_dir.glob("*depth.mkv") if "_part" not in f.name))):
        if i == a.frame:
            valid = d[d > 0]
            print(f"frame {i}: {d.shape[1]}x{d.shape[0]} uint16, valid {valid.size / d.size:.0%}, "
                  f"range {valid.min() if valid.size else 0}-{valid.max() if valid.size else 0} mm")
            if a.u is not None and rgb_size:
                print(f"depth at RGB ({a.u},{a.v}) = {depth_at_rgb_pixel(d, a.u, a.v, rgb_size):.0f} mm")
            break
