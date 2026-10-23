"""Post-take review: loop one camera's recorded video and let the researcher accept or discard."""

from __future__ import annotations

import csv
import threading
import time
from pathlib import Path

import av
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QImage, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (QComboBox, QDialog, QHBoxLayout, QLabel, QPushButton, QSizePolicy,
                               QVBoxLayout)

MAX_W = 960


def find_video(cam_dir: Path) -> tuple[Path, Path] | None:
    """(video, timestamps csv) for the RGB stream of a camera folder, falling back to mono."""
    for stream in ("rgb", "left", "right"):
        vids = [v for v in cam_dir.glob(f"*{stream}.mp4") if "_part" not in v.name]
        for v in vids:
            ts = v.with_name(v.stem + "_timestamps.csv")
            if ts.exists():
                return v, ts
    return None


def _read_ts(path: Path) -> list[int]:
    with open(path, newline="") as f:
        return [int(r["host_ts_us"]) for r in csv.DictReader(f) if r["written"] == "1"]


class _Player:
    """Decodes a video in a background thread and hands paced frames to a callback."""

    def __init__(self, video: Path, ts_csv: Path, start_us: int, on_frame, on_error):
        self.video, self.start_us = video, start_us
        self.ts = _read_ts(ts_csv)
        self.on_frame, self.on_error = on_frame, on_error
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        first = next((i for i, t in enumerate(self.ts) if t >= self.start_us), 0)
        while not self._stop.is_set():
            shown = 0
            try:
                with av.open(str(self.video)) as c:
                    st = c.streams.video[0]
                    st.thread_type = "AUTO"
                    t0_wall = t0_ts = None
                    for i, frame in enumerate(c.decode(st)):
                        if self._stop.is_set():
                            return
                        if i < first:
                            continue
                        ts = self.ts[i] if i < len(self.ts) else (self.ts[-1] if self.ts else 0)
                        w, h = frame.width, frame.height
                        if w > MAX_W:
                            w, h = MAX_W, int(h * MAX_W / w) // 2 * 2
                        rgb = frame.reformat(width=w, height=h, format="rgb24").to_ndarray()
                        img = QImage(rgb.data, w, h, 3 * w, QImage.Format_RGB888).copy()
                        if t0_wall is None:
                            t0_wall, t0_ts = time.monotonic(), ts
                        delay = (ts - t0_ts) / 1e6 - (time.monotonic() - t0_wall)
                        if delay > 0:
                            time.sleep(min(delay, 0.5))
                        if self._stop.is_set():
                            return
                        try:
                            self.on_frame(img, (ts - self.ts[first]) / 1e6 if self.ts else 0.0)
                        except RuntimeError:  # dialog already closed and deleted
                            return
                        shown += 1
            except Exception as e:
                if not self._stop.is_set():
                    try:
                        self.on_error(str(e))
                    except RuntimeError:
                        pass
                return
            if shown == 0:
                self.on_error("the take contains no frames")
                return
            time.sleep(0.4)  # short pause before looping


class TakeReviewDialog(QDialog):
    """Result: ACCEPT or DISCARD. The dialog cannot be dismissed without a decision."""

    ACCEPT, DISCARD = 1, 2
    _frame = Signal(QImage, float)
    _error = Signal(str)

    def __init__(self, parent, sign: str, title: str, cameras: list[tuple[str, Path]],
                 start_us: int, duration_s: float, warnings: list[str]):
        super().__init__(parent)
        self.setWindowTitle(f"Review take — {sign}")
        self.setWindowModality(Qt.WindowModal)
        self.decision = 0
        self._player: _Player | None = None
        self.start_us = start_us
        self.cameras = [(n, d, find_video(d)) for n, d in cameras]
        self.cameras = [c for c in self.cameras if c[2] is not None]

        lay = QVBoxLayout(self)
        head = QLabel(f"{title}<br><span style='font-size:30px; font-weight:600'>{sign}</span>")
        head.setTextFormat(Qt.RichText)
        lay.addWidget(head)
        row = QHBoxLayout()
        row.addWidget(QLabel("Camera:"))
        self.cam_cb = QComboBox()
        self.cam_cb.addItems([n for n, _, _ in self.cameras])
        self.cam_cb.currentIndexChanged.connect(self._play)
        row.addWidget(self.cam_cb)
        row.addStretch(1)
        self.time_lbl = QLabel("")
        self.time_lbl.setFont(QFont("Menlo", 13))
        row.addWidget(QLabel(f"Duration {duration_s:.1f} s"))
        row.addSpacing(12)
        row.addWidget(self.time_lbl)
        lay.addLayout(row)

        self.view = QLabel("loading…")
        self.view.setAlignment(Qt.AlignCenter)
        self.view.setMinimumSize(640, 360)
        self.view.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.view.setStyleSheet("background:#000; color:#aaa;")
        lay.addWidget(self.view, 1)

        if warnings:
            w = QLabel("⚠ " + "<br>⚠ ".join(warnings))
            w.setStyleSheet("color:#ff9500;")
            w.setWordWrap(True)
            lay.addWidget(w)

        btns = QHBoxLayout()
        self.discard_btn = QPushButton("✕ Discard — re-record  (⌫)")
        self.accept_btn = QPushButton("✓ Accept — next sign  (↩)")
        for b, color in ((self.discard_btn, "#ff3b30"), (self.accept_btn, "#34c759")):
            b.setMinimumHeight(48)
            f = QFont(); f.setPointSize(15); f.setBold(True)
            b.setFont(f)
            b.setStyleSheet(f"background:{color}; color:white; border-radius:8px; padding:0 18px;")
            btns.addWidget(b)
        self.discard_btn.clicked.connect(lambda: self._decide(self.DISCARD))
        self.accept_btn.clicked.connect(lambda: self._decide(self.ACCEPT))
        self.accept_btn.setDefault(True)
        lay.addLayout(btns)
        QShortcut(QKeySequence(Qt.Key_Backspace), self, activated=lambda: self._decide(self.DISCARD))
        QShortcut(QKeySequence(Qt.Key_Delete), self, activated=lambda: self._decide(self.DISCARD))
        QShortcut(QKeySequence(Qt.Key_Return), self, activated=lambda: self._decide(self.ACCEPT))
        QShortcut(QKeySequence(Qt.Key_Enter), self, activated=lambda: self._decide(self.ACCEPT))

        self._frame.connect(self._show_frame)
        self._error.connect(lambda m: self.view.setText(f"Cannot play this take: {m}"))
        self.resize(1000, 720)
        if self.cameras:
            self._play(0)
        else:
            self.view.setText("No video was recorded for this take (RGB and mono streams disabled?).")

    def _play(self, idx: int):
        if self._player:
            self._player.stop()
        if not self.cameras:
            return
        _, _, (video, ts) = self.cameras[idx]
        self.view.setText("loading…")
        self._player = _Player(video, ts, self.start_us,
                               lambda img, t: self._frame.emit(img, t), lambda m: self._error.emit(m))

    def _show_frame(self, img: QImage, t: float):
        self.view.setPixmap(QPixmap.fromImage(img).scaled(self.view.size(), Qt.KeepAspectRatio,
                                                          Qt.SmoothTransformation))
        self.time_lbl.setText(f"{t:5.2f} s")

    def _decide(self, decision: int):
        if self.decision:
            return
        self.decision = decision
        if self._player:
            self._player.stop()
        self.done(decision)

    # A decision is required: Esc / close button do nothing.
    def reject(self):
        pass

    def closeEvent(self, e):
        if not self.decision:
            e.ignore()
        else:
            super().closeEvent(e)
