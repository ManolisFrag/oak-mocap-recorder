"""Review player: starts at start_us (skips pre-roll), plays at real speed, loops."""
import csv, os, sys, tempfile, threading, time
from pathlib import Path
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
from PySide6.QtWidgets import QApplication
from mocap_recorder.review import TakeReviewDialog, _Player, find_video
from mocap_recorder.writers import EncodedVideoWriter, TIMESTAMP_HEADER
from test_writers import _fake_device_bitstream


def make_take(cam_dir: Path, n=45, preroll=15):
    cam_dir.mkdir(parents=True)
    w = EncodedVideoWriter(cam_dir / "ΓΕΙΑ_front_rgb.mp4", "h264", 640, 360, 30)
    with open(cam_dir / "ΓΕΙΑ_front_rgb_timestamps.csv", "w", newline="") as f:
        c = csv.writer(f); c.writerow(TIMESTAMP_HEADER)
        for i, (data, key) in enumerate(_fake_device_bitstream("h264", n=n)):
            ts = 1_000_000 + i * 33_333
            w.write(data, ts, key); c.writerow([i, i, ts, ts, 1])
    w.close()
    return 1_000_000 + preroll * 33_333   # start_us


def test_player():
    with tempfile.TemporaryDirectory() as d:
        cam = Path(d) / "front_1844"
        start = make_take(cam)
        video, ts = find_video(cam)
        got, done = [], threading.Event()
        def on_frame(img, t):
            got.append((t, img.width(), time.monotonic()))
            if len(got) >= 35: done.set()
        p = _Player(video, ts, start, on_frame, lambda m: (print("ERR", m), done.set()))
        done.wait(10); p.stop()
        assert got[0][0] == 0.0, got[0]                       # first shown frame == start_us
        first_loop = [g for g in got if g[0] >= 0]
        assert len(got) >= 35 and got[30][0] == 0.0, [g[0] for g in got[:33]]  # 30 frames, then loops
        dur = got[29][2] - got[0][2]
        assert 0.8 < dur < 1.3, dur                            # ~real speed (29 intervals at 30 fps)
        print(f"player ok: 30 frames after start in {dur:.2f}s, loops")


def test_dialog():
    app = QApplication.instance() or QApplication([])
    with tempfile.TemporaryDirectory() as d:
        cam = Path(d) / "front_1844"
        start = make_take(cam)
        dlg = TakeReviewDialog(None, "ΓΕΙΑ", "Sign 1 / 3", [("front", cam)], start, 1.0, ["front: dropped frames (rgb 2)"])
        dlg.show()
        t = time.time()
        while time.time() - t < 1.5:
            app.processEvents(); time.sleep(0.02)
        assert dlg.view.pixmap() is not None and not dlg.view.pixmap().isNull()
        dlg.reject(); app.processEvents()
        assert dlg.isVisible(), "Esc must not close without a decision"
        dlg._decide(TakeReviewDialog.ACCEPT)
        assert dlg.result() == TakeReviewDialog.ACCEPT
        print("dialog ok")


if __name__ == "__main__":
    test_player()
    test_dialog()
    print("ALL OK")
