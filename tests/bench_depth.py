"""Benchmark: 3 concurrent FFV1 depth writers (one per camera process)."""
import multiprocessing as mp, sys, tempfile, time
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

def job(i, n=150):
    from mocap_recorder.writers import Ffv1DepthWriter
    h, w = 720, 1280
    yy, xx = np.mgrid[0:h, 0:w]
    rng = np.random.default_rng(i)
    base = (800 + 2 * xx + yy).astype(np.float32)
    frames = []
    for _ in range(10):
        f = np.clip(base + rng.normal(0, 8, (h, w)), 0, 65535).astype(np.uint16)
        f[rng.random((h, w)) < 0.05] = 0
        frames.append(f)
    d = tempfile.mkdtemp(); wr = Ffv1DepthWriter(Path(d) / "d.mkv", w, h, 30)
    t = time.perf_counter()
    for k in range(n):
        while wr.backlog() > 100: time.sleep(0.001)
        wr.write(frames[k % 10], k * 33333)
    wr.close(); dt = time.perf_counter() - t
    return n / dt, (Path(d) / "d.mkv").stat().st_size / n

if __name__ == "__main__":
    with mp.get_context("spawn").Pool(3) as p:
        for fps, sz in p.map(job, range(3)):
            print(f"{fps:.0f} fps per writer, {sz/1e6:.2f} MB/frame")
