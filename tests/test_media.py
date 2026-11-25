"""Reference media lookup, looping video and the participant window states (offscreen)."""
import os, sys, tempfile, threading, time, unicodedata
from pathlib import Path
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
import numpy as np
from PySide6.QtGui import QImage, QColor
from PySide6.QtWidgets import QApplication
from mocap_recorder.media import LoopingVideo, find_media, index_media
from mocap_recorder.participant import ParticipantWindow
from test_review import make_take


def make_image(path: Path, left=(255, 0, 0), right=(0, 0, 255)):
    img = QImage(200, 100, QImage.Format_RGB888)
    img.fill(QColor(*right))
    for x in range(100):
        for y in range(100):
            img.setPixelColor(x, y, QColor(*left))
    assert img.save(str(path))


def test_index():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "house.jpg").write_bytes(b"x"); (d / "HOUSE.mp4").write_bytes(b"x")
        (d / unicodedata.normalize("NFD", "ΣΠΊΤΙ.png")).write_bytes(b"x")   # macOS-style decomposed name
        (d / "notes.txt").write_bytes(b"x"); (d / ".DS_Store").write_bytes(b"x")
        idx = index_media(d)
        assert find_media(idx, "House").name == "HOUSE.mp4"            # case-insensitive, video wins
        assert find_media(idx, "HOUSE_2").name == "HOUSE.mp4"          # _2 falls back to the base gloss
        assert find_media(idx, "ΣΠΊΤΙ") is not None                    # NFC gloss matches NFD file name
        assert find_media(idx, "ΝΕΡΟ") is None and len(idx) == 2
        assert index_media(None) == {} and index_media(d / "missing") == {}
    print("media index ok")


def test_looping_video():
    with tempfile.TemporaryDirectory() as d:
        make_take(Path(d) / "c", n=30)
        video = next((Path(d) / "c").glob("*.mp4"))
        got, done = [], threading.Event()
        def on_frame(rgb):
            got.append(time.monotonic())
            if len(got) >= 40: done.set()
        v = LoopingVideo(video, on_frame, lambda m: (print("ERR", m), done.set()))
        done.wait(6); v.stop()
        assert len(got) >= 40, len(got)                     # 30 frames, then it looped
        dur = got[29] - got[0]
        assert 0.8 < dur < 1.3, dur                         # real speed
    print(f"looping video ok ({dur:.2f}s for 30 frames)")


def test_participant_window():
    app = QApplication.instance() or QApplication([])
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        make_image(d / "HOUSE.png")
        make_take(d / "c", n=30)
        video = next((d / "c").glob("*.mp4"))
        w = ParticipantWindow(); w.resize(1280, 720); w.show(); app.processEvents()

        w.show_sign("HOUSE", "Sign 1 / 3", d / "HOUSE.png")
        app.processEvents()
        assert w.state == "sign" and w.media.isVisible() and not w.media.pixmap().isNull()
        assert w.gloss.text() == "HOUSE" and w.progress.text() == "Sign 1 / 3" and not w.rec.isVisible()
        left = w._pix.toImage().pixelColor(10, 50)
        w.show_sign("HOUSE", "Sign 1 / 3", d / "HOUSE.png", mirror=True); app.processEvents()
        mirrored = w._pix.toImage().pixelColor(10, 50)
        assert left.red() > 200 and mirrored.blue() > 200, (left.getRgb(), mirrored.getRgb())

        w.show_sign("ΝΕΡΟ", "Sign 2 / 3", video)
        t = time.time()
        while time.time() - t < 0.8:
            app.processEvents(); time.sleep(0.02)
        assert w._pix is not None, "video frames must reach the window"

        w.show_countdown(3, "ΝΕΡΟ", "Sign 2 / 3"); app.processEvents()
        assert w.big.isVisible() and ">3<" in w.big.text() and not w.media.isVisible() and w._video is None
        w.show_recording("ΝΕΡΟ", "Sign 2 / 3"); app.processEvents()
        assert w.rec.isVisible() and not w.big.isVisible() and not w.media.isVisible() and w.gloss.text() == "ΝΕΡΟ"
        w.show_wait("ΝΕΡΟ", "Sign 2 / 3"); app.processEvents()
        assert "Please wait" in w.media.text() and not w.rec.isVisible()
        w.show_sign("ΨΩΜΙ", "Sign 3 / 3", None); app.processEvents()
        assert not w.media.isVisible() and w.gloss.text() == "ΨΩΜΙ"     # no media: gloss only
        w.show_message("✓ Thank you!"); app.processEvents()
        assert "Thank you" in w.gloss.text()
        w.close()
    print("participant window states ok")


if __name__ == "__main__":
    test_index()
    test_looping_video()
    test_participant_window()
    print("ALL OK")
