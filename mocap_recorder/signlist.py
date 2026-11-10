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


def _read_text(path: Path) -> tuple[str, str]:
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "cp1253", "latin-1"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", "replace"), "latin-1"


def gloss_key(name: str) -> str:
    """Search key: case- and accent-insensitive (ΣΠΊΤΙ matches σπιτι). Used by the list filter."""
    d = unicodedata.normalize("NFD", name.strip())
    return "".join(c for c in d if unicodedata.category(c) != "Mn").casefold()


def parse_sign_file(path: Path) -> tuple[list[str], list[str]]:
    """One sign per line; blank lines and '#' comments ignored; duplicates get _2, _3."""
    warnings = []
    text, enc = _read_text(path)
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
    # ---- editing the list ---------------------------------------------------
    def find(self, name: str) -> int | None:
        """Index of a gloss, ignoring case only (accents can distinguish glosses)."""
        key = unicodedata.normalize("NFC", name.strip()).casefold()
        return next((i for i, s in enumerate(self.data["signs"])
                     if unicodedata.normalize("NFC", s["name"]).casefold() == key), None)

    def suggest_name(self, name: str) -> str:
        """First free variant: HOUSE -> HOUSE_2 -> HOUSE_3 …"""
        base = re.sub(r"_\d+$", "", name.strip())
        k = 2
        while self.find(f"{base}_{k}") is not None:
            k += 1
        return f"{base}_{k}"

    def _new_entry(self, name: str, added: bool) -> dict:
        nums = [int(m.group(1)) for s in self.data["signs"] if (m := re.match(r"(\d+)_", s["folder"]))]
        width = len(self.data["signs"][0]["folder"].split("_")[0]) if self.data["signs"] else 3
        # numbers are never reused, even if a hand-added gloss was removed again
        num = max(max(nums, default=0), self.data.get("last_number", 0)) + 1
        self.data["last_number"] = num
        entry = {"name": name, "folder": f"{num:0{width}d}_{safe_filename(name)}",
                 "status": "pending", "attempts": 0, "accepted_at": None}
        if added:
            entry.update({"added": True, "added_at": datetime.now().isoformat(timespec="seconds")})
        return entry

    def _signs_txt_lines(self) -> list[str]:
        p = self.dir / "signs.txt"
        return _read_text(p)[0].splitlines() if p.exists() else []

    def _write_signs_txt(self, lines: list[str]):
        tmp = self.dir / "signs.txt.tmp"
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")   # always rewritten as UTF-8
        tmp.replace(self.dir / "signs.txt")

    def add_sign(self, name: str, after: int | None = None) -> int:
        """Insert a new gloss after index `after` (default: the current sign) and make it current.
        Existing folder numbers never change; the new one gets the next free number."""
        name = unicodedata.normalize("NFC", name.strip())
        if not name or name.startswith("#"):
            raise ValueError("Type a gloss first.")
        if self.find(name) is not None:
            raise ValueError(f"'{name}' is already in the list.")
        pos = (self.current if after is None else after) + 1
        self.data["signs"].insert(pos, self._new_entry(name, added=True))
        self.data["current"] = pos
        self._write_signs_txt(self._signs_txt_lines() + [name])
        self.save()
        return pos

    def refresh(self) -> dict:
        """Re-sync with the disk after manual changes. Returns what changed:
        unmarked: take folder deleted -> not recorded again   (hand-added glosses are removed instead)
        marked:   folder present but not marked recorded (e.g. restored from _discarded)
        added:    new lines found in signs.txt (appended at the end)"""
        report = {"unmarked": [], "removed": [], "marked": [], "added": []}
        cur_folder = self.folder(self.current) if len(self) else None
        txt = self._signs_txt_lines()
        keep = []
        for s in self.data["signs"]:
            exists = (self.dir / s["folder"]).is_dir()
            if s["status"] == "accepted" and not exists:
                if s.get("added"):
                    report["removed"].append(s["name"])
                    for k in range(len(txt) - 1, -1, -1):        # drop its line from signs.txt
                        if unicodedata.normalize("NFC", txt[k].strip()) == s["name"]:
                            del txt[k]
                            break
                    continue
                s["status"], s["accepted_at"] = "pending", None
                report["unmarked"].append(s["name"])
            elif s["status"] != "accepted" and exists:
                s["status"] = "accepted"
                s["accepted_at"] = s.get("accepted_at") or datetime.now().isoformat(timespec="seconds")
                report["marked"].append(s["name"])
            keep.append(s)
        self.data["signs"] = keep
        if report["removed"]:
            self._write_signs_txt(txt)
        # new lines typed into signs.txt
        if (self.dir / "signs.txt").exists():
            listed, _ = parse_sign_file(self.dir / "signs.txt")
            for name in listed:
                if self.find(name) is None:
                    self.data["signs"].append(self._new_entry(name, added=False))
                    report["added"].append(name)
        # keep the same current sign if it still exists
        idx = next((i for i, s in enumerate(self.data["signs"]) if s["folder"] == cur_folder), None)
        if idx is None:
            idx = min(self.current, len(self) - 1) if len(self) else 0
        self.data["current"] = max(idx, 0)
        if len(self) and self.status(self.current) == "accepted" and report["unmarked"] + report["removed"]:
            nxt = self.next_pending(self.current - 1 if self.current else len(self) - 1)
            if nxt is not None:
                self.data["current"] = nxt
        self.save()
        return report

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
