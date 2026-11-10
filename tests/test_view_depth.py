"""view_depth.py: near = red, far = blue, invalid = black; RGB placed alongside."""
import csv, subprocess, sys, tempfile
from pathlib import Path
import av
import numpy as np
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
from mocap_recorder.writers import Ffv1DepthWriter, TIMESTAMP_HEADER
from test_review import make_take


def test_view_depth():
    with tempfile.TemporaryDirectory() as d:
        cam = Path(d) / "front_1844"
        make_take(cam, n=45)                                  # RGB 640x360, ts 1_000_000 + i*33_333
        depth = np.zeros((360, 640), np.uint16)
        depth[:, :300] = 800                                  # near
        depth[:, 340:] = 2500                                 # far; 300..340 = no depth
        wr = Ffv1DepthWriter(cam / "ΓΕΙΑ_front_depth.mkv", 640, 360, 30)
        with open(cam / "ΓΕΙΑ_front_depth_timestamps.csv", "w", newline="") as f:
            c = csv.writer(f); c.writerow(TIMESTAMP_HEADER)
            for i in range(30):
                ts = 1_000_000 + (i + 15) * 33_333            # depth starts later than RGB (pre-roll)
                wr.write(depth, ts); c.writerow([i, i, ts, ts, 1])
        wr.close()
        r = subprocess.run([sys.executable, str(ROOT / "tools" / "view_depth.py"), str(cam)],
                           capture_output=True, text=True)
        print(r.stdout.strip(), r.stderr.strip()[-500:])
        assert r.returncode == 0
        out = cam / "ΓΕΙΑ_front_depth_preview.mp4"
        with av.open(str(out)) as c:
            frames = [f.to_ndarray(format="rgb24") for f in c.decode(video=0)]
        assert len(frames) == 30 and frames[0].shape == (360, 1280, 3)
        f = frames[10].astype(int)
        near, gap, far = f[180, 640 + 150], f[180, 640 + 320], f[180, 640 + 500]
        assert near[0] > near[2] + 60, near          # red-ish
        assert far[2] > far[0] + 60, far             # blue-ish
        assert gap.max() < 30, gap                   # black
        print("view_depth ok: near", near, "far", far, "no-depth", gap)


if __name__ == "__main__":
    test_view_depth()
