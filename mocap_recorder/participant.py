"""Full-screen window for the signer on a second display.

States (driven by the main window):
    show_sign(gloss, progress, media, mirror)   before recording: reference picture / looping video + gloss
    show_countdown(n, gloss, progress)          3-2-1 before recording starts
    show_recording(gloss, progress)             gloss + red REC; reference media hidden
    show_wait(gloss, progress)                  "Please wait…" while the take is reviewed
    show_message(text)                          free recording / complete / idle
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QFontMetrics, QImage, QPixmap, QTransform
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from .media import LoopingVideo, is_video


class ParticipantWindow(QWidget):
    _frame = Signal(QImage)

    def __init__(self):
        super().__init__(None, Qt.Window | Qt.FramelessWindowHint)
        self.setWindowTitle("Participant")
        self.setStyleSheet("background:#000; color:white;")
        self.setCursor(Qt.BlankCursor)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(40, 24, 40, 32)

        top = QWidget()
        tl = QHBoxLayout(top)
        tl.setContentsMargins(0, 0, 0, 0)
        self.progress = QLabel("")
        self.progress.setStyleSheet("color:#999; font-size:26px;")
        self.rec = QLabel("● REC")
        self.rec.setStyleSheet("color:#ff3b30; font-size:34px; font-weight:700;")
        self.rec.hide()
        tl.addWidget(self.progress)
        tl.addStretch(1)
        tl.addWidget(self.rec)
        lay.addWidget(top)

        self.media = QLabel("")
        self.media.setAlignment(Qt.AlignCenter)
        self.media.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        lay.addWidget(self.media, 4)

        self.gloss = QLabel("")
        self.gloss.setAlignment(Qt.AlignCenter)
        self.gloss.setWordWrap(True)
        lay.addWidget(self.gloss, 1)

        self.big = QLabel("", self)          # countdown overlay
        self.big.setAlignment(Qt.AlignCenter)
        self.big.setStyleSheet("color:white; background:rgba(0,0,0,200);")
        self.big.hide()

        self._video: LoopingVideo | None = None
        self._pix: QPixmap | None = None
        self._mirror = False
        self.state = "message"
        self.media_path: Path | None = None
        self._frame.connect(self._set_frame)

    # ---- helpers ------------------------------------------------------------
    def _gloss_font(self, text: str, large: bool):
        size = max(28, int(self.height() * (0.16 if large else 0.09))) if self.height() > 100 else 64
        f = QFont(); f.setBold(True); f.setPixelSize(size)
        while size > 28 and QFontMetrics(f).horizontalAdvance(text) > self.width() * 0.9:
            size = int(size * 0.9); f.setPixelSize(size)
        self.gloss.setFont(f)
        self.gloss.setText(text)

    def _stop_media(self):
        if self._video:
            self._video.stop()
            self._video = None
        self._pix = None
        self.media.clear()
        self.media_path = None

    def _set_frame(self, img: QImage):
        if self.state != "sign":
            return
        pix = QPixmap.fromImage(img)
        if self._mirror:
            pix = pix.transformed(QTransform().scale(-1, 1))
        self._pix = pix
        self._rescale()

    def _rescale(self):
        if self._pix is not None:
            self.media.setPixmap(self._pix.scaled(self.media.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.big.setGeometry(self.rect())
        self._rescale()

    def _common(self, state: str, gloss: str, progress: str):
        self.state = state
        self.big.hide()
        self.progress.setText(progress)

    # ---- states ---------------------------------------------------------------
    def show_sign(self, gloss: str, progress: str, media: Path | None, mirror: bool = False):
        same = media is not None and media == self.media_path and self.state == "sign" and mirror == self._mirror
        self._common("sign", gloss, progress)
        self.rec.hide()
        self.media.show()
        self._gloss_font(gloss, large=media is None)
        if same:
            return
        self._stop_media()
        self._mirror = mirror
        self.media_path = media
        if media is None:
            self.media.hide()
        elif is_video(media):
            self.media.setText("")
            self._video = LoopingVideo(media, self._emit_rgb, lambda m: None)
        else:
            img = QImage(str(media))
            if img.isNull():
                self.media.setText(f"(cannot open {media.name})")
            else:
                self._set_frame(img)

    def _emit_rgb(self, rgb: np.ndarray):
        h, w, _ = rgb.shape
        self._frame.emit(QImage(rgb.data, w, h, 3 * w, QImage.Format_RGB888).copy())

    def show_countdown(self, n: int, gloss: str, progress: str):
        self._stop_media()
        self._common("countdown", gloss, progress)
        self.media.hide()
        self._gloss_font(gloss, large=True)
        self.big.setGeometry(self.rect())
        self.big.setText(f"<div style='font-size:{max(80, int(self.height() * 0.5))}px; font-weight:800'>{n}</div>")
        self.big.show()
        self.big.raise_()

    def show_recording(self, gloss: str, progress: str):
        self._stop_media()
        self._common("recording", gloss, progress)
        self.media.hide()
        self.rec.show()
        self._gloss_font(gloss or "● REC", large=True)

    def show_wait(self, gloss: str, progress: str):
        self._stop_media()
        self._common("wait", gloss, progress)
        self.rec.hide()
        self.media.show()
        self.media.setText("<span style='color:#888; font-size:40px'>Please wait…</span>")
        self._gloss_font(gloss, large=False)

    def show_message(self, text: str, progress: str = ""):
        self._stop_media()
        self._common("message", "", progress)
        self.rec.hide()
        self.media.hide()
        self._gloss_font(text, large=False)

    def closeEvent(self, e):
        self._stop_media()
        super().closeEvent(e)
