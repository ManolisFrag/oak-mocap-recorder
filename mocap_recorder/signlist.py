"""Sign-list sessions: parse the researcher's .txt list and track per-sign progress.

Session folder layout:
    <root>/<participant>_<YYYYmmdd_HHMMSS>/
        signs.txt  progress.json
        001_<sign>/     accepted take
        _pending/       take awaiting Accept / Discard
        _discarded/     discarded takes and takes replaced by a re-record
"""

from __future__ import annotations

import json
import re
import shutil
import unicodedata
from datetime import datetime
from pathlib import Path

PROGRESS_FILE = "progress.json"
PENDING_DIR = "_pending"
DISCARDED_DIR = "_discarded"

_UNSAFE = re.compile(r'[/\\:*?"<>|\x00-\x1f\x7f]+')
_SPACES = re.compile(r"\s+")


def safe_filename(name: str, max_len: int = 80) -> str:
    """Filesystem-safe name that keeps Unicode letters (Greek, accents, ...)."""
    s = unicodedata.normalize("NFC", name)
    s = _UNSAFE.sub("_", s)
    s = _SPACES.sub("_", s.strip())
    s = re.sub(r"_+", "_", s).strip("._")
    return s[:max_len].rstrip("._") or "sign"


def parse_sign_file(path: Path) -> tuple[list[str], list[str]]:
    """One sign per line; blank lines and '#' comments ignored; duplicates get _2, _3."""
    raw = Path(path).read_bytes()
    warnings = []
    for enc in ("utf-8-sig", "cp1253", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if enc != "utf-8-sig":
        warnings.append(f"File is not UTF-8; read it as {enc}. Check that the sign names look right.")
    signs, seen = [], {}
    for line in text.splitlines():
        s = unicodedata.normalize("NFC", line.strip())
        if not s or s.startswith("#"):
            continue
        key = s.casefold()
        if key in seen:
            seen[key] += 1
            new = f"{s}_{seen[key]}"
            warnings.append(f"Duplicate sign '{s}' renamed to '{new}'.")
            s = new
        else:
            seen[key] = 1
        signs.append(s)
    return signs, warnings


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    k = 2
    while path.with_name(f"{path.name}_{k}").exists():
        k += 1
    return path.with_name(f"{path.name}_{k}")


class SignSession:
    """Progress of one participant through one sign list (persisted in progress.json)."""

    def __init__(self, directory: Path, data: dict):
        self.dir = Path(directory)
        self.data = data

    # ---- construction -----------------------------------------------------
    @classmethod
    def create(cls, root: Path, participant: str, list_path: Path) -> tuple["SignSession", list[str]]:
        signs, warnings = parse_sign_file(list_path)
        if not signs:
            raise ValueError("The list contains no signs.")
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        pid = safe_filename(participant) if participant.strip() else "session"
        d = _unique(Path(root) / f"{pid}_{stamp}")
        d.mkdir(parents=True)
        shutil.copyfile(list_path, d / "signs.txt")
        width = max(3, len(str(len(signs))))
        data = {
            "version": 1,
            "participant": participant.strip(),
            "created": datetime.now().isoformat(timespec="seconds"),
            "list_source": str(list_path),
            "current": 0,
            "signs": [{"name": s, "folder": f"{i + 1:0{width}d}_{safe_filename(s)}",
                       "status": "pending", "attempts": 0, "accepted_at": None}
                      for i, s in enumerate(signs)],
        }
        sess = cls(d, data)
        sess.save()
        return sess, warnings

    @classmethod
    def open(cls, directory: Path) -> "SignSession":
        d = Path(directory)
        data = json.loads((d / PROGRESS_FILE).read_text(encoding="utf-8"))
        sess = cls(d, data)
        if sess.status(sess.current) == "accepted":
            nxt = sess.next_pending(sess.current)
            if nxt is not None:
                sess.data["current"] = nxt
        return sess

    def save(self):
        tmp = self.dir / (PROGRESS_FILE + ".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.dir / PROGRESS_FILE)

    # ---- queries ----------------------------------------------------------
    @property
    def participant(self) -> str:
        return self.data.get("participant", "")

    @property
    def signs(self) -> list[str]:
        return [s["name"] for s in self.data["signs"]]

    def __len__(self):
        return len(self.data["signs"])

    @property
    def current(self) -> int:
        return int(self.data["current"])

    @current.setter
    def current(self, i: int):
        self.data["current"] = int(i)
        self.save()

    def status(self, i: int) -> str:
        return self.data["signs"][i]["status"]

    def folder(self, i: int) -> str:
        return self.data["signs"][i]["folder"]

    def name(self, i: int) -> str:
        return self.data["signs"][i]["name"]

    def file_tag(self, i: int) -> str:
        """Sign name as used inside file names."""
        return safe_filename(self.name(i))

    @property
    def n_accepted(self) -> int:
        return sum(1 for s in self.data["signs"] if s["status"] == "accepted")

    @property
    def complete(self) -> bool:
        return self.n_accepted == len(self)

    @property
    def progress_text(self) -> str:
        return f"{self.n_accepted} / {len(self)} recorded"

    def next_pending(self, from_idx: int) -> int | None:
        n = len(self)
        for k in range(1, n + 1):
            i = (from_idx + k) % n
            if self.status(i) != "accepted":
                return i
        return None

    # ---- take handling ----------------------------------------------------
    def pending_take_dir(self, i: int) -> Path:
        stamp = datetime.now().strftime("%H%M%S")
        return _unique(self.dir / PENDING_DIR / f"{self.folder(i)}_{stamp}")

    def _to_discarded(self, src: Path, name: str) -> Path:
        dst = _unique(self.dir / DISCARDED_DIR / name)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return dst

    def accept(self, i: int, pending: Path) -> Path:
        """Move the pending take into place; an older accepted take goes to _discarded."""
        final = self.dir / self.folder(i)
        if final.exists():
            self._to_discarded(final, f"{self.folder(i)}_replaced_{datetime.now():%H%M%S}")
        Path(pending).rename(final)
        s = self.data["signs"][i]
        s["status"] = "accepted"
        s["attempts"] += 1
        s["accepted_at"] = datetime.now().isoformat(timespec="seconds")
        self.save()
        return final

    def discard(self, i: int, pending: Path) -> Path | None:
        self.data["signs"][i]["attempts"] += 1
        self.save()
        if Path(pending).exists():
            return self._to_discarded(Path(pending), Path(pending).name)
        return None

    def cleanup_stale_pending(self) -> list[Path]:
        """Takes left undecided by a crash/quit are moved to _discarded."""
        moved = []
        p = self.dir / PENDING_DIR
        if p.exists():
            for d in sorted(p.iterdir()):
                if d.is_dir():
                    moved.append(self._to_discarded(d, d.name))
        return moved
