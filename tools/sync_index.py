#!/usr/bin/env python3
"""Build a cross-camera frame-matching table for one recording session.

For every frame of the reference camera's stream, finds the nearest frame (by
host-synced timestamp) in each other camera and keeps rows where all cameras
are within the tolerance (default: half a frame interval).

    python tools/sync_index.py <session_or_take_dir> [--stream rgb] [--ref cam1] [--tol-ms 16]

Writes <session_dir>/sync_<stream>.csv with one column of frame indices per
camera (frame index = frame number inside that camera's video file) plus the
reference timestamp and the max spread in ms.
"""
import argparse
import csv
import json
from pathlib import Path

import numpy as np


def load_ts(path: Path):
    idx, ts = [], []
    with open(path) as f:
        for row in csv.DictReader(f):
            if row["written"] == "1":
                idx.append(int(row["frame_idx"]))
                ts.append(int(row["host_ts_us"]))
    return np.array(idx), np.array(ts, dtype=np.int64)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("session_dir", type=Path)
    ap.add_argument("--stream", default="rgb", help="rgb | depth | left | right")
    ap.add_argument("--ref", default=None, help="reference camera folder name prefix")
    ap.add_argument("--tol-ms", type=float, default=None)
    a = ap.parse_args()

    meta = a.session_dir / "session.json"
    if not meta.exists():
        meta = a.session_dir / "take.json"          # sign-list take folder
    sess = json.loads(meta.read_text(encoding="utf-8"))
    fps = sess["settings"]["fps"]
    tol_us = (a.tol_ms * 1000) if a.tol_ms else 0.5e6 / fps

    cams = {}
    for d in sorted(p for p in a.session_dir.iterdir() if p.is_dir()):
        # plain "rgb_timestamps.csv" or sign-take "<sign>_<cam>_rgb_timestamps.csv"
        found = [f for f in d.glob(f"*{a.stream}_timestamps.csv") if "_part" not in f.name]
        if found:
            cams[d.name] = load_ts(found[0])
        if list(d.glob(f"*{a.stream}_part*_timestamps.csv")):
            print(f"note: {d.name} has reconnect parts; only part 1 is indexed")
    if len(cams) < 2:
        raise SystemExit(f"need >=2 cameras with {a.stream}_timestamps.csv, found {list(cams)}")
    names = list(cams)
    ref = next((n for n in names if a.ref and n.startswith(a.ref)), names[0])

    ref_idx, ref_ts = cams[ref]
    cols = {ref: ref_idx}
    spread = np.zeros(len(ref_ts))
    ok = np.ones(len(ref_ts), bool)
    lo, hi = ref_ts.copy(), ref_ts.copy()
    for n in names:
        if n == ref:
            continue
        idx, ts = cams[n]
        j = np.clip(np.searchsorted(ts, ref_ts), 1, len(ts) - 1)
        j = np.where(np.abs(ts[j - 1] - ref_ts) <= np.abs(ts[j] - ref_ts), j - 1, j)
        ok &= np.abs(ts[j] - ref_ts) <= tol_us
        cols[n] = idx[j]
        lo, hi = np.minimum(lo, ts[j]), np.maximum(hi, ts[j])
    spread = (hi - lo) / 1000

    out = a.session_dir / f"sync_{a.stream}.csv"
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ref_host_ts_us", *names, "spread_ms"])
        for k in np.flatnonzero(ok):
            w.writerow([ref_ts[k], *[cols[n][k] for n in names], f"{spread[k]:.3f}"])
    print(f"{ok.sum()} / {len(ref_ts)} reference frames matched across {len(names)} cameras "
          f"(tol {tol_us/1000:.1f} ms, median spread {np.median(spread[ok]) if ok.any() else float('nan'):.2f} ms)"
          f" -> {out}")


if __name__ == "__main__":
    main()
