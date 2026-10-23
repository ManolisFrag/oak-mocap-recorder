"""Recommended stable-30-fps defaults, warnings, and the one-time prompt for existing installs."""
import json, os, sys, tempfile, time
from pathlib import Path
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mocap_recorder.settings import SETTINGS_VERSION, Settings


def test_defaults():
    s = Settings()
    assert (s.fps, s.codec, s.rgb_size, s.mono_size, s.depth_size) == (30, "H.265", (1920, 1080), (640, 400), (640, 360))
    assert s.depth_preset == "FAST_ACCURACY" and s.anti_banding.startswith("50 Hz")
    errors, warnings = s.problems(n_cameras=3)
    assert not errors and not warnings, warnings
    full = Settings(mono_resolution="1280x800")
    assert full.depth_size == (1280, 720)
    assert any("640x400 is recommended" in w for w in full.problems(n_cameras=3)[1])
    assert not full.problems(n_cameras=1)[1]
    old = Settings.from_dict({"mono_resolution": "1280x800", "depth_preset": "DEFAULT", "ir_dot_intensity": 0.3})
    assert old.mono_resolution == "1280x800" and old.anti_banding.startswith("50 Hz")  # old configs still load
    rec = Settings.recommended(keep_ir_from=old)
    assert rec.mono_resolution == "640x400" and rec.ir_dot_intensity == 0.3
    print("defaults / warnings / migration helpers ok")


def test_prompt():
    home = Path(tempfile.mkdtemp())
    os.environ["HOME"] = str(home)
    cfgdir = home / "Library" / "Application Support" / "MocapRecorder"
    cfgdir.mkdir(parents=True)
    (cfgdir / "config.json").write_text(json.dumps({"settings": {"mono_resolution": "1280x800",
                                                                 "depth_preset": "DEFAULT",
                                                                 "ir_dot_intensity": 0.3}}))
    from PySide6.QtWidgets import QApplication, QMessageBox
    import mocap_recorder.devices as devices
    devices.CONFIG_DIR, devices.CONFIG_PATH = cfgdir, cfgdir / "config.json"
    import mocap_recorder.app as appmod
    appmod.load_config = devices.load_config; appmod.save_config = devices.save_config
    asked = []
    QMessageBox.question = staticmethod(lambda *a, **k: asked.append(a[2]) or QMessageBox.Yes)
    app = QApplication.instance() or QApplication([])

    def run_window():
        w = appmod.MainWindow(); w.discover_timer.stop()
        t = time.time()
        while time.time() - t < 0.6:
            app.processEvents(); time.sleep(0.02)
        return w
    w = run_window()
    assert len(asked) == 1 and "640x400" in asked[0], asked
    assert w.settings.mono_resolution == "640x400" and w.settings.ir_dot_intensity == 0.3
    assert w.mono_cb.currentText() == "640x400" and w.flicker_cb.currentText().startswith("50 Hz")
    cfg = json.loads((cfgdir / "config.json").read_text())
    assert cfg["settings_version"] == SETTINGS_VERSION and cfg["settings"]["mono_resolution"] == "640x400"
    w.close()
    w2 = run_window()
    assert len(asked) == 1, "prompt must only appear once"
    # the button restores recommended values after a manual change
    w2.mono_cb.setCurrentText("1280x800"); w2._apply_settings()
    assert w2.settings.mono_resolution == "1280x800"
    w2._use_recommended()
    assert w2.settings.mono_resolution == "640x400" and w2.mono_cb.currentText() == "640x400"
    w2.close()
    print("one-time prompt + recommended button ok")


if __name__ == "__main__":
    test_defaults()
    test_prompt()
    print("ALL OK")
