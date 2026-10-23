"""Apply settings / cameras off-on while 3 real worker processes are busy connecting:
the GUI thread must never block, and workers must be restarted with the new settings."""
import os, sys, time
from pathlib import Path
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtWidgets import QApplication
from mocap_recorder.app import MainWindow


def pump(app, win, secs, worst):
    t = time.time()
    while time.time() - t < secs:
        t0 = time.perf_counter()
        app.processEvents()
        worst[0] = max(worst[0], time.perf_counter() - t0)
        time.sleep(0.01)


def main():
    app = QApplication([])
    win = MainWindow(); win.show()
    win.discover_timer.stop()
    ids = ["FAKE0000000000001", "FAKE0000000000002", "FAKE0000000000003"]
    started = []
    win._discover = lambda: [win._start_worker(d) or started.append(d)
                             for d in ids if d not in win.workers and d not in win._closing
                             and win.cameras_on and not win._restart_pending]
    win._discover()
    worst = [0.0]
    pump(app, win, 2.0, worst)          # workers are now inside dai.Device(...) searches
    assert len(win.workers) == 3

    # 1. apply new fps while cameras are on
    win.fps_cb.setCurrentText("15")
    t0 = time.perf_counter(); win._apply_settings(); blocked = time.perf_counter() - t0
    assert blocked < 0.3, f"apply blocked the GUI for {blocked:.2f}s"
    assert win._restart_pending and not win.rec_btn.isEnabled()
    t = time.time()
    while (win._restart_pending or win._closing) and time.time() - t < 20:
        pump(app, win, 0.1, worst)
    assert not win._closing and not win._restart_pending, "workers never finished"
    assert len(win.workers) == 3 and len(started) == 6
    print(f"apply: GUI blocked {blocked*1000:.0f} ms; restart took {time.time()-t:.1f}s")

    # 2. cameras off -> settings apply without restart -> on
    win.cams_btn.setChecked(False)
    assert not win.workers and not win.rec_btn.isEnabled()
    t = time.time()
    while win._closing and time.time() - t < 20:
        pump(app, win, 0.1, worst)
    assert not win._closing and "off" in win.empty_lbl.text().lower()
    win.fps_cb.setCurrentText("30"); win._apply_settings()
    assert not win.workers and win.settings.fps == 30
    win.cams_btn.setChecked(True)
    assert len(win.workers) == 3
    print(f"on/off ok; worst single event-loop iteration {worst[0]*1000:.0f} ms")
    assert worst[0] < 0.5
    t0 = time.perf_counter(); win.close(); print(f"close took {time.perf_counter()-t0:.1f}s")
    print("ALL OK")


if __name__ == "__main__":
    main()
