"""End-to-end sign-list flow in the real MainWindow with a simulated camera worker (offscreen)."""
import json, os, queue, sys, tempfile, time
from pathlib import Path
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
from PySide6.QtWidgets import QApplication, QMessageBox
from mocap_recorder.app import CameraTile, MainWindow

PROMPTS = []
def _no_modal(*a, **k):
    raise AssertionError(f"unexpected message box: {a[1:3]}")
QMessageBox.question = staticmethod(_no_modal)
QMessageBox.warning = staticmethod(_no_modal)
QMessageBox.critical = staticmethod(_no_modal)
QMessageBox.exec = lambda self: PROMPTS.append(self.text()) or 0   # resume prompt -> "Not now"
from mocap_recorder.review import TakeReviewDialog
from mocap_recorder.signlist import SignSession
from test_review import make_take

SHOTS = Path(sys.argv[1]) if len(sys.argv) > 1 else None


class FakeProc:
    exitcode = None
    alive = True
    def is_alive(self): return self.alive
    def join(self, timeout=None): pass
    def terminate(self): self.alive = False
    kill = terminate


def pump(app, secs):
    t = time.time()
    while time.time() - t < secs:
        app.processEvents(); time.sleep(0.02)


def take(app, win, did):
    """Press record, act as the worker, press stop; returns the open review dialog."""
    win.toggle_recording()
    assert win.recording, "recording did not start"
    cmd, arg = win.workers[did]["cmd_q"].get_nowait()
    while cmd != "record":  # skip the previous take's "stop"
        cmd, arg = win.workers[did]["cmd_q"].get_nowait()
    assert cmd == "record" and arg["file_prefix"].endswith("_front_")
    cam_dir = Path(arg["cam_dir"])
    # write a take shaped like the real worker output, with the sign prefix
    tmp = Path(tempfile.mkdtemp()) / "x"
    make_take(tmp)
    cam_dir.mkdir(parents=True)
    for f in tmp.iterdir():
        f.rename(cam_dir / f.name.replace("ΓΕΙΑ_front_", arg["file_prefix"]))
    arg_start = arg["start_us"]
    win._handle_event(did, "recording", {"dir": str(cam_dir), "part": 1})
    win.toggle_recording()  # stop
    win._handle_event(did, "stopped", {"dir": str(cam_dir), "written": {"rgb": 45}, "dropped": {"rgb": 2}})
    pump(app, 0.3)
    dlg = next(w for w in app.topLevelWidgets() if isinstance(w, TakeReviewDialog) and w.isVisible())
    # the take's real start_us is the GUI's; override the video timing for playback only
    dlg.start_us = 0
    return dlg, cam_dir


def main():
    app = QApplication([])
    root = Path(tempfile.mkdtemp())
    lst = root / "signs.txt"
    lst.write_text("ΓΕΙΑ ΣΟΥ\nΝΕΡΟ\nΨΩΜΙ\n", encoding="utf-8")

    win = MainWindow(); win.show(); win.resize(1400, 850)
    win.folder_edit.setText(str(root / "rec"))
    did = "1844FAKE"
    tile = CameraTile(did, "front")
    win.workers[did] = {"proc": FakeProc(), "cmd_q": queue.Queue(), "evt_q": queue.Queue(),
                        "preview_q": queue.Queue(), "tile": tile, "name": "front", "online": True, "stats": {}}
    win._relayout()
    win.discover_timer.stop()

    sess, _ = SignSession.create(root / "rec", "P01", lst)
    win._set_sign_session(sess)
    pump(app, 0.2)
    assert win.flash.isVisible() and "ΓΕΙΑ ΣΟΥ" in win.flash.text() and not win.rec_btn.isEnabled()
    if SHOTS: win.grab().save(str(SHOTS / "1_flash.png"))
    pump(app, 2.1)
    assert not win.flash.isVisible() and win.rec_btn.isEnabled() and win.banner.isVisible()
    if SHOTS: win.grab().save(str(SHOTS / "2_banner.png"))

    # sign 1: record -> accept
    dlg, cam_dir = take(app, win, did)
    assert "_pending" in str(cam_dir)
    pump(app, 1.0)
    if SHOTS: dlg.grab().save(str(SHOTS / "3_review.png"))
    dlg._decide(TakeReviewDialog.ACCEPT); pump(app, 0.3)
    final = sess.dir / "001_ΓΕΙΑ_ΣΟΥ"
    files = sorted(p.name for p in (final / f"front_{did}").iterdir())
    assert "ΓΕΙΑ_ΣΟΥ_front_rgb.mp4" in files, files
    tj = json.loads((final / "take.json").read_text(encoding="utf-8"))
    assert tj["sign"] == "ΓΕΙΑ ΣΟΥ" and "_pending" not in json.dumps(tj, ensure_ascii=False), tj
    assert win.signs.current == 1 and win.flash.isVisible() and "ΝΕΡΟ" in win.flash.text()
    print("accept ok:", files)

    # sign 2: record -> discard -> same sign flashed again
    pump(app, 2.1)
    dlg, _ = take(app, win, did)
    dlg._decide(TakeReviewDialog.DISCARD); pump(app, 0.3)
    assert win.signs.current == 1 and "ΝΕΡΟ" in win.flash.text()
    assert len(list((sess.dir / "_discarded").iterdir())) == 1
    print("discard ok")

    # interrupt: close window, reopen and resume
    win.workers.clear(); win.close()
    win2 = MainWindow(); win2.discover_timer.stop()
    pump(app, 0.6)
    assert len(PROMPTS) == 1 and "sign 2 / 3" in PROMPTS[0] and "ΝΕΡΟ" in PROMPTS[0], PROMPTS
    assert win2.signs is None  # "Not now" keeps free mode
    win2._open_sign_session(str(sess.dir))
    assert win2.signs.current == 1 and win2.signs.n_accepted == 1
    items = [win2.sign_list.item(i).text() for i in range(win2.sign_list.count())]
    assert items[0].startswith("✓") and items[1].startswith("▶"), items
    if SHOTS:
        pump(app, 2.1); win2.grab().save(str(SHOTS / "4_resumed.png"))
    print("resume ok:", items)
    win2.close()
    print("ALL OK")


if __name__ == "__main__":
    main()
