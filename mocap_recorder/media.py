"""Reference media for glosses: find a picture/video per gloss and play videos in a loop."""

from __future__ import annotations

import re
import threading
import time
import unicodedata
from pathlib import Path

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}


def _key(name: str) -> str:
    # macOS may store file names decomposed (NFD); lists are NFC. Compare in NFC, ignoring case.
    return unicodedata.normalize("NFC", name.strip()).casefold()


def index_media(folder: Path | None) -> dict[str, Path]:
    """{gloss key: file} for every image/video in `folder` (not recursive). Videos win over images."""
    out: dict[str, Path] = {}
    if not folder or not Path(folder).is_dir():
        return out
    for f in sorted(Path(folder).iterdir()):
        ext = f.suffix.lower()
        if f.name.startswith(".") or ext not in IMAGE_EXT | VIDEO_EXT:
            continue
        k = _key(f.stem)
        if k not in out or (ext in VIDEO_EXT and out[k].suffix.lower() in IMAGE_EXT):
            out[k] = f
    return out


def find_media(index: dict[str, Path], gloss: str) -> Path | None:
    """HOUSE -> HOUSE.*; HOUSE_2 -> HOUSE_2.* or else HOUSE.*"""
    hit = index.get(_key(gloss))
    if hit is None:
        base = re.sub(r"_\d+$", "", gloss.strip())
        if base != gloss.strip():
            hit = index.get(_key(base))
    return hit


def is_video(path: Path | None) -> bool:
    return path is not None and path.suffix.lower() in VIDEO_EXT


class LoopingVideo:
    """Decodes a video in a background thread and calls on_frame(np.ndarray RGB) at real speed, looping."""

    def __init__(self, path: Path, on_frame, on_error, max_w: int = 1920):
        self.path, self.on_frame, self.on_error, self.max_w = Path(path), on_frame, on_error, max_w
        self._stop = threading.Event()
        threading.Thread(target=self._run, daemon=True, name=f"media-{self.path.name}").start()

    def stop(self):
        self._stop.set()

    def _run(self):
        import av
        while not self._stop.is_set():
            shown = 0
            try:
                with av.open(str(self.path)) as c:
                    st = c.streams.video[0]
                    st.thread_type = "AUTO"
                    t0 = None
                    for frame in c.decode(st):
                        if self._stop.is_set():
                            return
                        w, h = frame.width, frame.height
                        if w > self.max_w:
                            w, h = self.max_w, int(h * self.max_w / w) // 2 * 2
                        rgb = frame.reformat(width=w, height=h, format="rgb24").to_ndarray()
                        t = float(frame.time or 0.0)
                        if t0 is None:
                            t0 = (time.monotonic(), t)
                        delay = (t - t0[1]) - (time.monotonic() - t0[0])
                        if delay > 0:
                            time.sleep(min(delay, 0.5))
                        if self._stop.is_set():
                            return
                        try:
                            self.on_frame(rgb)
                        except RuntimeError:   # receiver already deleted
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
                return
            time.sleep(0.3)  # brief pause before the loop restarts
