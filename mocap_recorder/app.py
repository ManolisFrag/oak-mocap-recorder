"""PySide6 main window: live preview grid, settings, IR controls and recording."""

from __future__ import annotations

import json
import math
import multiprocessing as mp
import queue
import shutil
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QFontMetrics, QImage, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout, QFrame,
                               QGridLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit,
                               QPushButton, QScrollArea, QSizePolicy, QSlider, QSplitter, QVBoxLayout,
                               QWidget)

from . import __version__
from .devices import camera_name, discover_devices, load_config, safe_name, save_config
from .review import TakeReviewDialog
from .signlist import PROGRESS_FILE, SignSession
from .settings import (ANTI_BANDING, CODECS, DEPTH_PRESETS, FPS_OPTIONS, MONO_RESOLUTIONS, RGB_RESOLUTIONS,
                       SETTINGS_VERSION, STREAMS, Settings)
from .worker import worker_main

STREAM_LABELS = {"rgb": "RGB", "depth": "Depth (aligned to RGB)", "left": "Left mono",
                 "right": "Right mono", "imu": "IMU"}


def now_us() -> int:
    import depthai as dai
    from datetime import timedelta
    return dai.Clock.now() // timedelta(microseconds=1)


class _Bridge(QObject):
    discovered = Signal(list)


# --------------------------------------------------------------------------- tiles
class CameraTile(QFrame):
    renamed = Signal(str, str)  # device_id, new name

    def __init__(self, device_id: str, name: str):
        super().__init__()
        self.device_id = device_id
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet("CameraTile { background: #111; border: 2px solid #333; border-radius: 6px; }")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        top = QHBoxLayout()
        self.name_edit = QLineEdit(name)
        self.name_edit.setToolTip("Camera name (used for folder names). Saved per serial number.")
        self.name_edit.setMaximumWidth(180)
        self.name_edit.editingFinished.connect(
            lambda: self.renamed.emit(self.device_id, self.name_edit.text()))
        self.serial = QLabel(device_id)
        self.serial.setStyleSheet("color: #888;")
        self.rec = QLabel("")
        self.rec.setStyleSheet("color: #ff3b30; font-weight: bold;")
        top.addWidget(self.name_edit)
        top.addWidget(self.serial)
        top.addStretch(1)
        top.addWidget(self.rec)
        lay.addLayout(top)
        self.image = QLabel("connecting…")
        self.image.setAlignment(Qt.AlignCenter)
        self.image.setMinimumSize(320, 180)
        self.image.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.image.setStyleSheet("color: #aaa; background: #000;")
        lay.addWidget(self.image, 1)
        self.info = QLabel("")
        self.info.setStyleSheet("color: #ccc; font-family: Menlo, monospace; font-size: 11px;")
        self.info.setWordWrap(True)
        lay.addWidget(self.info)
        self._pix: QPixmap | None = None

    def set_frame(self, data: bytes, w: int, h: int):
        img = QImage(data, w, h, 3 * w, QImage.Format_RGB888)
        self._pix = QPixmap.fromImage(img)
        self._rescale()

    def _rescale(self):
        if self._pix is not None:
            self.image.setPixmap(self._pix.scaled(self.image.size(), Qt.KeepAspectRatio,
                                                  Qt.SmoothTransformation))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._rescale()

    def set_status(self, text: str, ok: bool = True):
        if not ok:
            self._pix = None
            self.image.setPixmap(QPixmap())
            self.image.setText(text)
        self.setStyleSheet("CameraTile { background: #111; border: 2px solid %s; border-radius: 6px; }"
                           % ("#333" if ok else "#ff9500"))

    def set_recording(self, on: bool):
        self.rec.setText("● REC" if on else "")
        self.name_edit.setEnabled(not on)


# --------------------------------------------------------------------------- main window
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"OAK Mocap Recorder {__version__}")
        self.resize(1500, 900)
        self.cfg = load_config()
        self.settings = Settings.from_dict(self.cfg.get("settings"))
        self.ctx = mp.get_context("spawn")
        self.workers: dict[str, dict] = {}
        # Workers that were told to quit and are still shutting down. Their queues are drained
        # until they exit; they are never joined on the GUI thread.
        self._closing: dict[str, dict] = {}
        self.cameras_on = True
        self._restart_pending = False
        self.recording = False
        self.session: dict | None = None
        self.stopping = False
        self._discovering = False
        self.signs: SignSession | None = None   # active sign-list session (None = free mode)
        self._flashing = False
        self._reviewing = False
        self._bridge = _Bridge()
        self._bridge.discovered.connect(self._on_discovered)

        self._build_ui()
        self._load_settings_into_ui()
        self._refresh_sign_ui()
        self._update_estimates()

        self.poll_timer = QTimer(self, interval=15, timeout=self._poll)
        self.poll_timer.start()
        self.discover_timer = QTimer(self, interval=3000, timeout=self._discover)
        self.discover_timer.start()
        self.disk_timer = QTimer(self, interval=2000, timeout=self._update_estimates)
        self.disk_timer.start()
        self.clock_timer = QTimer(self, interval=200, timeout=self._update_clock)
        self.clock_timer.start()
        self._started = False
        QTimer.singleShot(100, self._startup)
        QShortcut(QKeySequence(Qt.Key_Space), self, activated=self.toggle_recording)
        self.flash_timer = QTimer(self, singleShot=True, timeout=self._end_flash)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        central = QWidget()
        root = QVBoxLayout(central)

        # top bar: folder, trial, record
        bar = QHBoxLayout()
        self.folder_edit = QLineEdit(self.cfg.get("output_folder", str(Path.home() / "Movies" / "MocapRecordings")))
        browse = QPushButton("Choose folder…")
        browse.clicked.connect(self._choose_folder)
        self.trial_edit = QLineEdit(self.cfg.get("trial", ""))
        self.trial_edit.setPlaceholderText("subject / trial name (optional)")
        self.trial_edit.setMaximumWidth(260)
        self.rec_btn = QPushButton("● Start recording")
        self.rec_btn.setMinimumHeight(44)
        self.rec_btn.setMinimumWidth(200)
        f = QFont(); f.setPointSize(15); f.setBold(True)
        self.rec_btn.setFont(f)
        self.rec_btn.clicked.connect(self.toggle_recording)
        self._style_rec_btn()
        self.clock_lbl = QLabel("00:00:00")
        self.clock_lbl.setFont(QFont("Menlo", 18))
        bar.addWidget(QLabel("Save to:"))
        bar.addWidget(self.folder_edit, 2)
        bar.addWidget(browse)
        bar.addSpacing(12)
        self.trial_lbl = QLabel("Trial:")
        bar.addWidget(self.trial_lbl)
        bar.addWidget(self.trial_edit, 1)
        bar.addSpacing(12)
        bar.addWidget(self.clock_lbl)
        bar.addWidget(self.rec_btn)
        root.addLayout(bar)

        self.disk_lbl = QLabel("")
        self.disk_lbl.setStyleSheet("color: #888;")
        root.addWidget(self.disk_lbl)

        split = QSplitter(Qt.Horizontal)
        split.addWidget(self._build_side_panel())
        right = QSplitter(Qt.Vertical)
        self.stage = QWidget()
        stage_lay = QVBoxLayout(self.stage)
        stage_lay.setContentsMargins(0, 0, 0, 0)
        self.banner = QLabel("")
        self.banner.setAlignment(Qt.AlignCenter)
        self.banner.setStyleSheet("background:#1c1c1e; color:white; font-size:30px; font-weight:600;"
                                  "padding:8px; border-radius:6px;")
        self.banner.setTextFormat(Qt.RichText)
        self.banner.hide()
        stage_lay.addWidget(self.banner)
        self.grid_host = QWidget()
        stage_lay.addWidget(self.grid_host, 1)
        self.flash = QLabel(self.stage)
        self.flash.setAlignment(Qt.AlignCenter)
        self.flash.setTextFormat(Qt.RichText)
        self.flash.setWordWrap(True)
        self.flash.setStyleSheet("background: rgba(0,0,0,225); color: white; border-radius: 10px;")
        self.flash.hide()
        self.stage.installEventFilter(self)
        self.grid = QGridLayout(self.grid_host)
        self.grid.setSpacing(8)
        self.empty_lbl = QLabel("No OAK cameras detected.\nConnect cameras via USB3 — they appear automatically.")
        self.empty_lbl.setAlignment(Qt.AlignCenter)
        self.empty_lbl.setStyleSheet("color: #888; font-size: 16px;")
        self.grid.addWidget(self.empty_lbl, 0, 0)
        right.addWidget(self.stage)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        self.log.setStyleSheet("font-family: Menlo, monospace; font-size: 11px;")
        right.addWidget(self.log)
        right.setSizes([700, 140])
        split.addWidget(right)
        split.setSizes([360, 1140])
        root.addWidget(split, 1)
        self.setCentralWidget(central)

    def _build_side_panel(self) -> QWidget:
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.addWidget(self._build_sign_group())

        g = QGroupBox("Capture settings (all cameras)")
        form = QFormLayout(g)
        self.cams_btn = QPushButton()
        self.cams_btn.setCheckable(True)
        self.cams_btn.setChecked(True)
        self.cams_btn.setMinimumHeight(34)
        self.cams_btn.setToolTip("Turn the OAK cameras off, e.g. before changing resolution or frame rate")
        self.cams_btn.toggled.connect(self._set_cameras_on)
        self._style_cams_btn()
        form.addRow(self.cams_btn)
        self.res_cb = QComboBox(); self.res_cb.addItems(list(RGB_RESOLUTIONS))
        self.fps_cb = QComboBox(); self.fps_cb.addItems([str(f) for f in FPS_OPTIONS])
        self.codec_cb = QComboBox(); self.codec_cb.addItems(list(CODECS))
        self.mono_cb = QComboBox(); self.mono_cb.addItems(list(MONO_RESOLUTIONS))
        self.preset_cb = QComboBox(); self.preset_cb.addItems(DEPTH_PRESETS)
        self.flicker_cb = QComboBox(); self.flicker_cb.addItems(list(ANTI_BANDING))
        self.flicker_cb.setToolTip("Match your mains frequency so room lights don't flicker in the video")
        self.undist_chk = QCheckBox("Undistort RGB (recommended)")
        form.addRow("RGB resolution", self.res_cb)
        form.addRow("Frame rate", self.fps_cb)
        form.addRow("Video codec", self.codec_cb)
        form.addRow("Mono resolution", self.mono_cb)
        form.addRow("Depth preset", self.preset_cb)
        form.addRow("Anti-flicker", self.flicker_cb)
        form.addRow(self.undist_chk)
        self.stream_chks = {}
        for s in STREAMS:
            c = QCheckBox(STREAM_LABELS[s])
            self.stream_chks[s] = c
            form.addRow(c)
        for w in (self.res_cb, self.fps_cb, self.codec_cb, self.mono_cb, self.preset_cb, self.flicker_cb):
            w.currentIndexChanged.connect(self._settings_edited)
        for c in [self.undist_chk, *self.stream_chks.values()]:
            c.toggled.connect(self._settings_edited)
        self.problems_lbl = QLabel("")
        self.problems_lbl.setWordWrap(True)
        form.addRow(self.problems_lbl)
        self.apply_btn = QPushButton("Apply settings (restarts cameras)")
        self.apply_btn.clicked.connect(self._apply_settings)
        self.recommended_btn = QPushButton("Recommended settings (stable 30 fps)")
        self.recommended_btn.setToolTip("1080p H.265 RGB, 30 fps, depth from 640x400 mono at 640x360, "
                                        "FAST_ACCURACY preset, 50 Hz anti-flicker. IR settings are kept.")
        self.recommended_btn.clicked.connect(self._use_recommended)
        form.addRow(self.apply_btn)
        form.addRow(self.recommended_btn)
        lay.addWidget(g)

        ir = QGroupBox("IR illumination (live)")
        irl = QFormLayout(ir)
        self.dot_chk = QCheckBox("Dot projector")
        self.dot_sl = QSlider(Qt.Horizontal); self.dot_sl.setRange(0, 100)
        self.flood_chk = QCheckBox("Flood light")
        self.flood_sl = QSlider(Qt.Horizontal); self.flood_sl.setRange(0, 100)
        irl.addRow(self.dot_chk, self.dot_sl)
        irl.addRow(self.flood_chk, self.flood_sl)
        for w in (self.dot_chk, self.flood_chk):
            w.toggled.connect(self._ir_changed)
        for w in (self.dot_sl, self.flood_sl):
            w.valueChanged.connect(self._ir_changed)
        lay.addWidget(ir)
        lay.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(panel)
        scroll.setMinimumWidth(350)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        return scroll

    def _load_settings_into_ui(self):
        s = self.settings
        widgets = (self.res_cb, self.fps_cb, self.codec_cb, self.mono_cb, self.preset_cb, self.flicker_cb,
                   self.undist_chk, *self.stream_chks.values(), self.dot_chk, self.dot_sl,
                   self.flood_chk, self.flood_sl)
        for w in widgets:
            w.blockSignals(True)
        self.res_cb.setCurrentText(s.rgb_resolution)
        self.fps_cb.setCurrentText(str(s.fps))
        self.codec_cb.setCurrentText(s.codec)
        self.mono_cb.setCurrentText(s.mono_resolution)
        self.preset_cb.setCurrentText(s.depth_preset)
        self.flicker_cb.setCurrentText(s.anti_banding)
        self.undist_chk.setChecked(s.undistort_rgb)
        for k, c in self.stream_chks.items():
            c.setChecked(bool(s.streams.get(k)))
        self.dot_chk.setChecked(s.ir_dot_enabled)
        self.dot_sl.setValue(int(s.ir_dot_intensity * 100))
        self.flood_chk.setChecked(s.ir_flood_enabled)
        self.flood_sl.setValue(int(s.ir_flood_intensity * 100))
        for w in widgets:
            w.blockSignals(False)
        self._settings_edited()
        self.apply_btn.setEnabled(False)

    def _ui_settings(self) -> Settings:
        s = Settings.from_dict(self.settings.to_dict())
        s.rgb_resolution = self.res_cb.currentText()
        s.fps = int(self.fps_cb.currentText())
        s.codec = self.codec_cb.currentText()
        s.mono_resolution = self.mono_cb.currentText()
        s.depth_preset = self.preset_cb.currentText()
        s.undistort_rgb = self.undist_chk.isChecked()
        s.anti_banding = self.flicker_cb.currentText()
        s.streams = {k: c.isChecked() for k, c in self.stream_chks.items()}
        s.ir_dot_enabled = self.dot_chk.isChecked()
        s.ir_dot_intensity = self.dot_sl.value() / 100
        s.ir_flood_enabled = self.flood_chk.isChecked()
        s.ir_flood_intensity = self.flood_sl.value() / 100
        return s

    def _settings_edited(self):
        s = self._ui_settings()
        errors, warnings = s.problems(n_cameras=max(len(self.workers), 1))
        lines = [f"<span style='color:#ff3b30'>✖ {e}</span>" for e in errors]
        lines += [f"<span style='color:#ff9500'>⚠ {w}</span>" for w in warnings]
        if s.streams.get("depth"):
            dw, dh = s.depth_size
            lines.append(f"<span style='color:#888'>Depth saved at {dw}x{dh} (16-bit, lossless)</span>")
        self.problems_lbl.setText("<br>".join(lines))
        # wrapped labels in a QFormLayout don't grow by themselves
        width = self.problems_lbl.width() if self.problems_lbl.width() > 50 else 300
        self.problems_lbl.setMinimumHeight(self.problems_lbl.heightForWidth(width) if lines else 0)
        changed = {k: v for k, v in s.to_dict().items() if not k.startswith("ir_")} != \
                  {k: v for k, v in self.settings.to_dict().items() if not k.startswith("ir_")}
        self.apply_btn.setEnabled(changed and not errors and not self.recording)
        self._update_estimates(s)

    def _apply_settings(self):
        s = self._ui_settings()
        if s.problems()[0] or self.recording:
            return
        self.settings = s
        self._save_cfg()
        self.apply_btn.setEnabled(False)
        if not self.cameras_on:
            self.log_msg("Settings saved — they are used when the cameras are turned on")
            return
        self.log_msg("Applying settings — restarting cameras…")
        for did in list(self.workers):
            self._stop_worker(did)
        self._restart_pending = True
        self._update_rec_enabled()

    def _ir_changed(self):
        s = self._ui_settings()
        for k in ("ir_dot_enabled", "ir_dot_intensity", "ir_flood_enabled", "ir_flood_intensity"):
            setattr(self.settings, k, getattr(s, k))
        ir = {k: v for k, v in self.settings.to_dict().items() if k.startswith("ir_")}
        for w in self.workers.values():
            w["cmd_q"].put(("ir", ir))
        self._save_cfg()

    def _choose_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Choose recording folder", self.folder_edit.text())
        if d:
            self.folder_edit.setText(d)
            self._save_cfg()
            self._update_estimates()

    def _style_rec_btn(self):
        if self.recording:
            self.rec_btn.setText("■ Stop recording")
            self.rec_btn.setStyleSheet("background:#ff3b30; color:white; border-radius:8px;")
        else:
            self.rec_btn.setText("● Start recording")
            self.rec_btn.setStyleSheet("background:#2c2c2e; color:#ff3b30; border-radius:8px;")

    def _save_cfg(self):
        self.cfg["settings"] = self.settings.to_dict()
        self.cfg["output_folder"] = self.folder_edit.text()
        if self.signs is None:
            self.cfg["trial"] = self.trial_edit.text()
        try:
            save_config(self.cfg)
        except Exception as e:
            self.log_msg(f"Could not save config: {e}")

    def log_msg(self, text: str, level: str = "info"):
        color = {"info": None, "warn": "#ff9500", "error": "#ff3b30"}[level]
        ts = datetime.now().strftime("%H:%M:%S")
        style = f" style='color:{color}'" if color else ""   # info uses the theme's text colour
        self.log.appendHtml(f"<span{style}>[{ts}] {text}</span>")
        if self.session is not None and level in ("warn", "error"):
            self.session["events"].append({"time": datetime.now().isoformat(), "level": level, "msg": text})

    def _update_estimates(self, s: Settings | None = None):
        s = s or self.settings
        n = max(len(self.workers), 1)
        mbps = s.estimated_mb_per_s(n)
        try:
            folder = Path(self.folder_edit.text())
            probe = folder if folder.exists() else next(p for p in folder.parents if p.exists())
            free = shutil.disk_usage(probe).free / 1e9
            minutes = free * 1000 / mbps / 60 if mbps else math.inf
            self.disk_lbl.setText(f"Free disk: {free:.0f} GB   •   Estimated write rate for {n} camera(s): "
                                  f"{mbps:.0f} MB/s   •   ≈ {minutes:.0f} min of recording available")
            self.disk_lbl.setStyleSheet("color: %s;" % ("#ff3b30" if minutes < 10 else "#888"))
            self._free_minutes = minutes
        except Exception:
            self.disk_lbl.setText("Choose a valid output folder")
            self._free_minutes = 0

    # ------------------------------------------------------------------ sign list
    def _build_sign_group(self) -> QWidget:
        g = QGroupBox("Sign list")
        v = QVBoxLayout(g)
        row = QHBoxLayout()
        self.load_list_btn = QPushButton("Load list…")
        self.load_list_btn.setToolTip("Start a new session from a .txt file (one sign per line)")
        self.load_list_btn.clicked.connect(self._load_sign_list)
        self.open_sess_btn = QPushButton("Open session…")
        self.open_sess_btn.setToolTip("Resume an earlier session folder")
        self.open_sess_btn.clicked.connect(self._open_sign_session)
        self.clear_list_btn = QPushButton("Clear list")
        self.clear_list_btn.setToolTip("End this session and return to free recording (files are kept)")
        self.clear_list_btn.clicked.connect(self._clear_sign_list)
        for b in (self.load_list_btn, self.open_sess_btn, self.clear_list_btn):
            row.addWidget(b)
        v.addLayout(row)
        self.sign_progress = QLabel("No list loaded — free recording mode")
        self.sign_progress.setWordWrap(True)
        v.addWidget(self.sign_progress)
        self.sign_list = QListWidget()
        self.sign_list.setMinimumHeight(220)
        self.sign_list.setToolTip("Double-click a sign to record (or re-record) it")
        self.sign_list.itemDoubleClicked.connect(self._jump_to_sign)
        self.sign_list.hide()
        v.addWidget(self.sign_list)
        return g

    def _refresh_sign_ui(self):
        sess = self.signs
        sign_mode = sess is not None
        self.sign_list.setVisible(sign_mode)
        self.clear_list_btn.setEnabled(sign_mode)
        self.banner.setVisible(sign_mode)
        self.trial_lbl.setText("Participant:" if sign_mode else "Trial:")
        self.trial_edit.setReadOnly(sign_mode)
        if not sign_mode:
            self.sign_progress.setText("No list loaded — free recording mode")
            self.trial_edit.setText(self.cfg.get("trial", ""))
            self.sign_list.clear()
            return
        self.trial_edit.setText(sess.participant)
        self.sign_progress.setText(f"<b>{sess.participant or 'session'}</b> — {sess.progress_text}<br>"
                                   f"<span style='color:#888'>{sess.dir}</span>")
        self.sign_list.clear()
        for i, name in enumerate(sess.signs):
            accepted = sess.status(i) == "accepted"
            current = i == sess.current and not sess.complete
            mark = "▶" if current else ("✓" if accepted else "·")
            item = QListWidgetItem(f"{mark}  {i + 1:>3}  {name}")
            if accepted:
                item.setForeground(Qt.darkGreen)
            if current:
                f = item.font(); f.setBold(True); item.setFont(f)
            self.sign_list.addItem(item)
        self.sign_list.setCurrentRow(sess.current)
        self.sign_list.scrollToItem(self.sign_list.item(sess.current), QListWidget.PositionAtCenter)
        if sess.complete:
            self.banner.setText(f"✓ All {len(sess)} signs recorded — clear the list for a new session, "
                                "or double-click a sign to re-record it")
        else:
            redo = " <span style='color:#ff9500'>(re-record)</span>" if sess.status(sess.current) == "accepted" else ""
            self.banner.setText(f"<span style='color:#999; font-size:18px'>Sign {sess.current + 1} / "
                                f"{len(sess)}</span>&nbsp;&nbsp;{sess.name(sess.current)}{redo}")

    def eventFilter(self, obj, ev):
        if obj is self.stage and ev.type() == QEvent.Resize:
            self._layout_flash()
        return super().eventFilter(obj, ev)

    def _layout_flash(self):
        r = self.stage.rect()
        self.flash.setGeometry(r.adjusted(20, 20, -20, -20))

    def _flash_text(self, big: str, small: str = "", ms: int = 2000):
        """Show `big` across the camera grid for `ms` milliseconds."""
        self._layout_flash()
        w = max(self.flash.width() - 80, 200)
        size = max(24, min(int(self.flash.height() * 0.35), 220))
        f = QFont(); f.setBold(True)
        while size > 24:
            f.setPixelSize(size)
            if QFontMetrics(f).horizontalAdvance(big) <= w:
                break
            size = int(size * 0.9)
        small_html = f"<div style='font-size:{max(16, size // 5)}px; color:#bbb'>{small}</div>" if small else ""
        self.flash.setText(f"{small_html}<div style='font-size:{size}px; font-weight:700'>{big}</div>")
        self.flash.show()
        self.flash.raise_()
        self._flashing = True
        self._update_rec_enabled()
        self.flash_timer.start(ms)

    def _flash_current(self):
        sess = self.signs
        if sess is None:
            return
        if sess.complete:
            self._flash_text(f"✓ All {len(sess)} signs recorded", "Session complete", 3000)
        else:
            self._flash_text(sess.name(sess.current), f"Sign {sess.current + 1} / {len(sess)}")

    def _end_flash(self):
        self.flash.hide()
        self._flashing = False
        self._update_rec_enabled()

    def _update_rec_enabled(self):
        busy = self._flashing or self._reviewing or self.stopping or self._restart_pending \
            or not self.cameras_on
        complete = self.signs is not None and self.signs.complete and not self.recording
        self.rec_btn.setEnabled(not busy and not complete)
        idle = not (self.recording or self.stopping or self._reviewing)
        self.cams_btn.setEnabled(idle)
        for b in (self.load_list_btn, self.open_sess_btn):
            b.setEnabled(idle)
        self.clear_list_btn.setEnabled(idle and self.signs is not None)
        self.sign_list.setEnabled(idle)

    def _set_sign_session(self, sess: SignSession | None):
        self.signs = sess
        if sess is None:
            self.cfg.pop("sign_session", None)
        else:
            self.cfg["sign_session"] = str(sess.dir)
        self._save_cfg()
        self._refresh_sign_ui()
        self._update_rec_enabled()
        if sess is not None:
            self._flash_current()

    def _load_sign_list(self):
        if self.recording or self._reviewing:
            return
        if self.signs is not None and not self.signs.complete:
            if QMessageBox.question(self, "Replace session?",
                                    f"Session '{self.signs.participant}' is not finished "
                                    f"({self.signs.progress_text}). Start a new one anyway?\n"
                                    "Its recordings stay on disk and it can be resumed with 'Open session…'."
                                    ) != QMessageBox.Yes:
                return
        path, _ = QFileDialog.getOpenFileName(self, "Choose sign list", str(Path.home()),
                                              "Text files (*.txt);;All files (*)")
        if not path:
            return
        participant, ok = QInputDialog.getText(self, "New session", "Participant / session ID:")
        if not ok:
            return
        root = Path(self.folder_edit.text()).expanduser()
        try:
            sess, warnings = SignSession.create(root, participant, Path(path))
        except Exception as e:
            QMessageBox.critical(self, "Cannot load list", str(e))
            return
        for w in warnings:
            self.log_msg(w, "warn")
        if warnings:
            QMessageBox.warning(self, "Sign list", "\n".join(warnings[:15]))
        self.log_msg(f"Sign session started: {len(sess)} signs → {sess.dir}")
        self._set_sign_session(sess)

    def _open_sign_session(self, directory: str | None = None):
        if self.recording or self._reviewing:
            return
        d = directory or QFileDialog.getExistingDirectory(self, "Open session folder", self.folder_edit.text())
        if not d:
            return
        if not (Path(d) / PROGRESS_FILE).exists():
            QMessageBox.warning(self, "Not a session", f"No {PROGRESS_FILE} in\n{d}")
            return
        try:
            sess = SignSession.open(Path(d))
            for m in sess.cleanup_stale_pending():
                self.log_msg(f"Unreviewed take from the previous run moved to {m}", "warn")
        except Exception as e:
            QMessageBox.critical(self, "Cannot open session", str(e))
            return
        self.log_msg(f"Resumed session {sess.participant}: {sess.progress_text}")
        self._set_sign_session(sess)

    def _offer_resume(self):
        if self.signs is not None:  # a list was already loaded
            return
        d = self.cfg.get("sign_session")
        if not d or not (Path(d) / PROGRESS_FILE).exists():
            self.cfg.pop("sign_session", None)
            return
        try:
            sess = SignSession.open(Path(d))
        except Exception as e:
            self.log_msg(f"Could not read previous session {d}: {e}", "warn")
            return
        if sess.complete:
            self.log_msg(f"Previous session {sess.participant} is complete ({len(sess)} signs).")
            self.cfg.pop("sign_session", None)
            self._save_cfg()
            return
        box = QMessageBox(self)
        box.setWindowTitle("Resume session?")
        box.setText(f"Resume session <b>{sess.participant or Path(d).name}</b> at sign "
                    f"{sess.current + 1} / {len(sess)} (<b>{sess.name(sess.current)}</b>)?<br>"
                    f"{sess.progress_text}.")
        resume = box.addButton("Resume", QMessageBox.AcceptRole)
        box.addButton("Not now", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is resume:
            self._open_sign_session(d)

    def _clear_sign_list(self):
        if self.signs is None or self.recording or self._reviewing:
            return
        msg = (f"End session '{self.signs.participant}' ({self.signs.progress_text})?\n"
               "Recorded files stay on disk; the session can be reopened later with 'Open session…'.")
        if QMessageBox.question(self, "Clear list", msg) != QMessageBox.Yes:
            return
        self.log_msg(f"Session {self.signs.participant} cleared ({self.signs.progress_text})")
        self._set_sign_session(None)

    def _jump_to_sign(self, item: QListWidgetItem):
        sess = self.signs
        if sess is None or self.recording or self._reviewing:
            return
        i = self.sign_list.row(item)
        if sess.status(i) == "accepted":
            if QMessageBox.question(self, "Re-record sign?",
                                    f"'{sess.name(i)}' is already recorded. Record it again?\n"
                                    "The old take is moved to _discarded only when the new one is accepted."
                                    ) != QMessageBox.Yes:
                return
        sess.current = i
        self._refresh_sign_ui()
        self._update_rec_enabled()
        self._flash_current()

    def _review_take(self, s: dict):
        sess = self.signs
        i = s["sign_index"]
        warnings = []
        for name, info in sorted(s["cameras"].items()):
            dropped = {k: v for k, v in (info.get("frames_dropped") or {}).items() if v}
            if dropped:
                warnings.append(f"{name}: dropped frames ("
                                + ", ".join(f"{k} {v}" for k, v in dropped.items()) + ")")
        for name in s["gaps"]:
            warnings.append(f"{name}: camera disconnected during the take")
        missing = [w["name"] for w in self.workers.values() if w["name"] not in s["cameras"]]
        if missing:
            warnings.append("not recorded by: " + ", ".join(missing))
        cams = [(name, Path(info["dir"])) for name, info in sorted(s["cameras"].items()) if info.get("dir")]
        dlg = TakeReviewDialog(self, sess.name(i), f"Sign {i + 1} / {len(sess)}", cams,
                               s["start_us"], s["duration_s"], warnings)
        dlg.finished.connect(lambda result: self._review_done(result, s))
        self._reviewing = True
        self._update_rec_enabled()
        dlg.open()

    def _review_done(self, result: int, s: dict):
        sess = self.signs
        i, take_dir = s["sign_index"], Path(s["dir"])
        self._reviewing = False
        try:
            if result == TakeReviewDialog.ACCEPT:
                final = sess.accept(i, take_dir)
                tj = final / "take.json"
                if tj.exists():  # paths inside take.json pointed at the _pending folder
                    tj.write_text(tj.read_text(encoding="utf-8").replace(str(take_dir), str(final)),
                                  encoding="utf-8")
                self.log_msg(f"✓ Accepted '{sess.name(i)}' → {final.name}")
                nxt = sess.next_pending(i)
                if nxt is not None:
                    sess.current = nxt
            else:
                moved = sess.discard(i, take_dir)
                self.log_msg(f"✕ Discarded take of '{sess.name(i)}'" + (f" → {moved}" if moved else ""), "warn")
        except Exception as e:
            self.log_msg(f"Could not file the take: {e}", "error")
            QMessageBox.critical(self, "Could not file the take", f"{e}\n\nThe take is still in {take_dir}")
        self._refresh_sign_ui()
        self._update_rec_enabled()
        self._flash_current()

    # ------------------------------------------------------------------ devices
    def _startup(self):
        self._offer_recommended_settings()
        self._started = True
        self._discover()
        QTimer.singleShot(300, self._offer_resume)

    def _offer_recommended_settings(self):
        """Once per settings version: offer the recommended stable-30-fps defaults to existing installs."""
        if self.cfg.get("settings_version", 1) >= SETTINGS_VERSION:
            return
        self.cfg["settings_version"] = SETTINGS_VERSION
        rec = Settings.recommended(keep_ir_from=self.settings)
        if "settings" in self.cfg and self.settings.capture_dict() != rec.capture_dict():
            msg = ("This version has new recommended settings for a stable 30 fps with several cameras:\n\n"
                   f"• RGB {rec.rgb_resolution}, {rec.fps} fps, {rec.codec}\n"
                   f"• Depth from {rec.mono_resolution} mono sensors, saved at "
                   f"{rec.depth_size[0]}x{rec.depth_size[1]}\n"
                   f"• Depth preset {rec.depth_preset}\n"
                   f"• Anti-flicker {rec.anti_banding}\n\n"
                   "Switch to them now? (Your IR settings are kept. You can return to them any time "
                   "with “Recommended settings”.)")
            if QMessageBox.question(self, "Recommended settings", msg) == QMessageBox.Yes:
                self.settings = rec
                self._load_settings_into_ui()
                self.log_msg("Switched to the recommended settings")
        self._save_cfg()

    def _use_recommended(self):
        if self.recording:
            return
        rec = Settings.recommended(keep_ir_from=self._ui_settings())
        current, self.settings = self.settings, rec
        self._load_settings_into_ui()      # shows the recommended values in the panel
        self.settings = current
        self._settings_edited()
        if self.apply_btn.isEnabled():
            self._apply_settings()
        else:
            self.log_msg("Already using the recommended settings")

    def _discover(self):
        if not self._started:
            return
        if self._discovering or not self.cameras_on or self._restart_pending:
            return
        self._discovering = True

        def run():
            try:
                ids = discover_devices()
            except Exception:
                ids = []
            self._bridge.discovered.emit(ids)
        threading.Thread(target=run, daemon=True).start()

    def _on_discovered(self, ids: list):
        self._discovering = False
        if not self.cameras_on or self._restart_pending:
            return
        for did in ids:
            if did not in self.workers and did not in self._closing:
                self._start_worker(did)

    def _start_worker(self, did: str):
        name = camera_name(self.cfg, did, len(self.workers))
        self._save_cfg()
        cmd_q, evt_q, preview_q = self.ctx.Queue(), self.ctx.Queue(), self.ctx.Queue(maxsize=2)
        proc = self.ctx.Process(target=worker_main, name=f"cam-{did}",
                                args=(did, name, self.settings.to_dict(), cmd_q, evt_q, preview_q),
                                daemon=True)
        proc.start()
        tile = CameraTile(did, name)
        tile.renamed.connect(self._rename_camera)
        self.workers[did] = {"proc": proc, "cmd_q": cmd_q, "evt_q": evt_q, "preview_q": preview_q,
                             "tile": tile, "name": name, "online": False, "stats": {}}
        self._relayout()
        self.log_msg(f"Camera {name} ({did}) found — starting")

    def _stop_worker(self, did: str):
        """Ask a camera process to quit. Returns immediately; _poll() reaps it."""
        w = self.workers.pop(did, None)
        if not w:
            return
        try:
            w["cmd_q"].put(("quit", None))
        except Exception:
            pass
        w["deadline"] = time.monotonic() + 12
        self._closing[did] = w
        w["tile"].setParent(None)
        w["tile"].deleteLater()
        self._relayout()

    def _reap_closing(self):
        for did, w in list(self._closing.items()):
            # keep reading so the worker's queue feeder threads can flush and the process can exit
            self._drain(w["preview_q"], 8)
            for _, kind, payload in self._drain(w["evt_q"], 200):
                if kind == "error":
                    self.log_msg(f"{w['name']}: {payload}", "error")
            if not w["proc"].is_alive():
                w["proc"].join(0)
                del self._closing[did]
            elif time.monotonic() > w["deadline"]:
                self.log_msg(f"{w['name']}: camera did not shut down in time — forcing it", "warn")
                w["proc"].kill()
                w["proc"].join(1)
                del self._closing[did]
        if self._restart_pending and not self._closing:
            self._restart_pending = False
            self.log_msg("Cameras stopped — starting them with the new settings")
            self._update_rec_enabled()
            self._discover()
        if not self.cameras_on and not self._closing and not self.workers:
            self.empty_lbl.setText("Cameras are off.\nClick “Cameras: OFF” to turn them on.")

    def _style_cams_btn(self):
        on = self.cams_btn.isChecked()
        self.cams_btn.setText("Cameras: ON  (click to turn off)" if on else "Cameras: OFF  (click to turn on)")
        self.cams_btn.setStyleSheet("background:%s; color:white; font-weight:600; border-radius:6px;"
                                    % ("#34c759" if on else "#8e8e93"))

    def _set_cameras_on(self, on: bool):
        if not on and (self.recording or self.stopping):
            self.cams_btn.blockSignals(True); self.cams_btn.setChecked(True); self.cams_btn.blockSignals(False)
            return
        self.cameras_on = on
        self._style_cams_btn()
        if on:
            self.empty_lbl.setText("No OAK cameras detected.\nConnect cameras via USB3 — they appear automatically.")
            self.log_msg("Cameras on")
            self.discover_timer.start()
            self._discover()
        else:
            self.discover_timer.stop()
            self._restart_pending = False
            self.empty_lbl.setText("Turning cameras off…")
            for did in list(self.workers):
                self._stop_worker(did)
            self.log_msg("Cameras off — settings can be changed safely")
        self._settings_edited()
        self._update_rec_enabled()

    def _rename_camera(self, did: str, name: str):
        name = safe_name(name) or self.workers[did]["name"]
        if name in [n for k, n in self.cfg.get("camera_names", {}).items() if k != did]:
            self.log_msg(f"Name '{name}' already used by another camera", "warn")
            name = self.workers[did]["name"]
        self.workers[did]["name"] = name
        self.workers[did]["tile"].name_edit.setText(name)
        self.cfg.setdefault("camera_names", {})[did] = name
        self._save_cfg()

    def _relayout(self):
        while self.grid.count():
            self.grid.takeAt(0)
        tiles = [w["tile"] for _, w in sorted(self.workers.items(), key=lambda kv: kv[1]["name"])]
        self.empty_lbl.setVisible(not tiles)
        if not tiles:
            self.grid.addWidget(self.empty_lbl, 0, 0)
            return
        n = len(tiles)
        cols = n if n <= 3 else math.ceil(math.sqrt(n))
        for i, t in enumerate(tiles):
            self.grid.addWidget(t, i // cols, i % cols)
        self._update_estimates()
        if not self.recording:
            self._settings_edited()   # warnings depend on the number of cameras

    # ------------------------------------------------------------------ polling
    @staticmethod
    def _drain(q, limit: int) -> list:
        out = []
        try:
            for _ in range(limit):
                out.append(q.get_nowait())
        except (queue.Empty, OSError, ValueError, EOFError):
            pass
        return out

    def _poll(self):
        for did, w in list(self.workers.items()):
            frames = self._drain(w["preview_q"], 4)
            if frames:
                _, data, fw, fh = frames[-1]
                w["tile"].set_frame(data, fw, fh)
            for ev in self._drain(w["evt_q"], 200):
                self._handle_event(*ev)
        # crashed workers: drop them so discovery restarts them
        for did, w in list(self.workers.items()):
            if not w["proc"].is_alive():
                self.log_msg(f"{w['name']}: worker process exited (code {w['proc'].exitcode}) — restarting",
                             "error")
                self._stop_worker(did)
                if self.session is not None:
                    self.session["pending_stop"].discard(did)
        self._reap_closing()
        if self.stopping and self.session is not None and \
                (not self.session["pending_stop"] or time.monotonic() > self.session["stop_deadline"]):
            self._finish_session()

    def _handle_event(self, did: str, kind: str, payload):
        w = self.workers.get(did)
        if w is None:
            return
        tile, name = w["tile"], w["name"]
        if kind == "connected":
            w["online"] = True
            tile.set_status("", True)
            w["info"] = payload
            self.log_msg(f"{name}: connected — {payload['product']}, USB {payload['usb_speed']}, "
                         f"IMU {payload['imu']}, streams {', '.join(payload['streams'])}")
        elif kind == "status":
            if payload.get("state") == "offline":
                w["online"] = False
                tile.set_status(f"offline — retrying\n{payload.get('error', '')[:200]}", False)
        elif kind == "disconnected":
            w["online"] = False
            tile.set_status("DISCONNECTED — reconnecting…", False)
            self.log_msg(f"{name}: camera disconnected" + (" DURING RECORDING" if payload.get("recording") else ""),
                         "error")
            if self.session is not None:
                self.session["gaps"].setdefault(name, []).append({"lost_host_us": payload["t_us"]})
        elif kind == "reconnected":
            self.log_msg(f"{name}: reconnected, recording continues in part {payload['part']}", "warn")
            if self.session is not None and self.session["gaps"].get(name):
                self.session["gaps"][name][-1]["resumed_host_us"] = now_us()
                self.session["gaps"][name][-1]["part"] = payload["part"]
        elif kind == "stats":
            w["stats"] = payload
            parts = []
            for sname, st in payload.items():
                drop = f" drop {st['dropped']}" if st["dropped"] else ""
                back = f" buf {st['backlog']}" if st.get("backlog") else ""
                parts.append(f"{sname} {st['fps']:.1f}fps{drop}{back}")
            tile.info.setText("  |  ".join(parts))
        elif kind == "recording":
            tile.set_recording(True)
            if self.session is not None:
                self.session["cameras"][name] = {"device_id": did, "dir": payload["dir"]}
        elif kind == "stopped":
            tile.set_recording(False)
            if self.session is not None:
                self.session["cameras"].setdefault(name, {"device_id": did}).update(
                    {"frames_written": payload.get("written"), "frames_dropped": payload.get("dropped")})
                self.session["pending_stop"].discard(did)
        elif kind == "warning":
            self.log_msg(f"{name}: {payload}", "warn")
        elif kind == "error":
            self.log_msg(f"{name}: worker error\n{payload}", "error")

    # ------------------------------------------------------------------ recording
    def toggle_recording(self):
        if self.stopping or (not self.recording and not self.rec_btn.isEnabled()):
            return
        if self.recording:
            self.stop_recording()
        else:
            self.start_recording()

    def start_recording(self):
        online = {d: w for d, w in self.workers.items() if w["online"]}
        if not online:
            QMessageBox.warning(self, "No cameras", "No camera is streaming yet.")
            return
        if self.apply_btn.isEnabled():
            if QMessageBox.question(self, "Unapplied settings",
                                    "Settings were changed but not applied. Record with the current "
                                    "(applied) settings?") != QMessageBox.Yes:
                return
        sign_mode = self.signs is not None
        root = self.signs.dir if sign_mode else Path(self.folder_edit.text()).expanduser()
        try:
            root.mkdir(parents=True, exist_ok=True)
            probe = root / ".write_test"
            probe.write_text("ok"); probe.unlink()
        except Exception as e:
            QMessageBox.critical(self, "Folder not writable", f"{root}\n{e}")
            return
        self._update_estimates()
        if getattr(self, "_free_minutes", 0) < 5:
            if QMessageBox.question(self, "Low disk space",
                                    f"Only ~{self._free_minutes:.0f} min of recording fit on this disk. Continue?"
                                    ) != QMessageBox.Yes:
                return
        if len(online) < len(self.workers):
            self.log_msg(f"Recording with {len(online)} of {len(self.workers)} cameras online", "warn")

        if sign_mode:
            idx = self.signs.current
            sdir = self.signs.pending_take_dir(idx)
            tag = self.signs.file_tag(idx)
        else:
            trial = safe_name(self.trial_edit.text())
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            sdir = root / (f"{stamp}_{trial}" if trial else stamp)
        sdir.mkdir(parents=True, exist_ok=False)
        start_us = now_us() + 300_000  # arm all workers before the common start instant
        import depthai as dai
        self.session = {
            "dir": sdir, "start_us": start_us, "started_at": datetime.now().isoformat(),
            "trial": self.trial_edit.text(), "cameras": {}, "gaps": {}, "events": [],
            "pending_stop": set(), "settings": self.settings.to_dict(),
            "depthai_version": dai.__version__, "app_version": __version__,
        }
        if sign_mode:
            self.session.update({"sign": self.signs.name(idx), "sign_index": idx,
                                 "participant": self.signs.participant, "trial": self.signs.participant})
        for did, w in self.workers.items():
            cmd = {"session_dir": str(sdir), "start_us": start_us, "cam_name": w["name"]}
            if sign_mode:
                cmd.update({"cam_dir": str(sdir / f"{w['name']}_{did}"),
                            "file_prefix": f"{tag}_{w['name']}_", "sign": self.signs.name(idx)})
            w["cmd_q"].put(("record", cmd))
        self.recording = True
        self._rec_t0 = time.monotonic() + 0.3
        self._style_rec_btn()
        self._set_controls_enabled(False)
        self._save_cfg()
        self._write_session_json()
        self._update_rec_enabled()
        self.log_msg(f"Recording → {sdir}" + (f"  [sign: {self.session['sign']}]" if sign_mode else ""))

    def stop_recording(self):
        if not self.recording or self.session is None:
            return
        stop_us = now_us()
        self.session["stop_us"] = stop_us
        self.session["pending_stop"] = set(self.workers)
        self.session["stop_deadline"] = time.monotonic() + 10
        for w in self.workers.values():
            w["cmd_q"].put(("stop", {"stop_us": stop_us}))
        self.stopping = True
        self.rec_btn.setText("Finishing files…")
        self.rec_btn.setEnabled(False)

    def _finish_session(self):
        s = self.session
        if s["pending_stop"]:
            self.log_msg("Some cameras did not confirm stop in time: "
                         + ", ".join(self.workers[d]["name"] for d in s["pending_stop"] if d in self.workers),
                         "error")
        s["stopped_at"] = datetime.now().isoformat()
        s["duration_s"] = max(0.0, (s["stop_us"] - s["start_us"]) / 1e6)
        self._write_session_json()
        self.log_msg(f"Saved {s['duration_s']:.1f} s session to {s['dir']}")
        self.session = None
        self.recording = False
        self.stopping = False
        self.rec_btn.setEnabled(True)
        self._style_rec_btn()
        self._set_controls_enabled(True)
        for w in self.workers.values():
            w["tile"].set_recording(False)
        self._update_rec_enabled()
        if "sign_index" in s and self.signs is not None:
            self._review_take(s)

    def _write_session_json(self):
        s = dict(self.session)
        s.pop("pending_stop", None); s.pop("stop_deadline", None)
        s["dir"] = str(s["dir"])
        s["notes"] = ("Timestamps (host_ts_us) of all cameras share the dai.Clock domain; "
                      "match frames across cameras with tools/sync_index.py. Depth (depth.mkv) is FFV1 "
                      "gray16le in millimetres, aligned to the RGB image (scale pixel coords by "
                      "rgb_size/depth_size). H.26x files may begin up to 1 s before start_us "
                      "(pre-roll to the previous keyframe); always use *_timestamps.csv.")
        fname = "take.json" if "sign" in s else "session.json"
        try:
            (self.session["dir"] / fname).write_text(json.dumps(s, indent=2, default=str, ensure_ascii=False),
                                                     encoding="utf-8")
        except Exception as e:
            self.log_msg(f"Could not write session.json: {e}", "error")

    def _set_controls_enabled(self, on: bool):
        for w in (self.res_cb, self.fps_cb, self.codec_cb, self.mono_cb, self.preset_cb, self.flicker_cb,
                  self.recommended_btn, self.undist_chk,
                  *self.stream_chks.values(), self.folder_edit, self.trial_edit):
            w.setEnabled(on)
        if on:
            self._settings_edited()
        else:
            self.apply_btn.setEnabled(False)

    def _update_clock(self):
        if self.recording and not self.stopping:
            t = max(0, int(time.monotonic() - self._rec_t0))
            self.clock_lbl.setText(f"{t // 3600:02d}:{t // 60 % 60:02d}:{t % 60:02d}")
            self.clock_lbl.setStyleSheet("color: #ff3b30;")
        elif not self.recording:
            self.clock_lbl.setStyleSheet("")

    # ------------------------------------------------------------------ shutdown
    def closeEvent(self, e):
        if self._reviewing:
            if QMessageBox.question(self, "Take not reviewed",
                                    "A take is waiting for Accept / Discard. Quit anyway? "
                                    "(It will be moved to _discarded when the session is resumed.)"
                                    ) != QMessageBox.Yes:
                e.ignore()
                return
        if self.recording:
            if QMessageBox.question(self, "Recording in progress",
                                    "Stop the recording and quit?") != QMessageBox.Yes:
                e.ignore()
                return
            self.stop_recording()
            deadline = time.monotonic() + 10
            while self.session is not None and time.monotonic() < deadline:
                QApplication.processEvents()
                self._poll()
                time.sleep(0.05)
        self._save_cfg()
        for did in list(self.workers):
            self._stop_worker(did)
        deadline = time.monotonic() + 8
        while self._closing and time.monotonic() < deadline:
            self._reap_closing()
            time.sleep(0.05)
        for w in self._closing.values():
            w["proc"].kill()
        e.accept()


def main():
    mp.freeze_support()
    app = QApplication(sys.argv)
    app.setApplicationName("OAK Mocap Recorder")
    app.setStyle("Fusion")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())
