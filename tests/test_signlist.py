"""Sign-list model tests (no hardware, no Qt)."""
import json, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mocap_recorder.signlist import SignSession, parse_sign_file, safe_filename


def test_parse_and_names():
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "signs.txt"
        f.write_text("# comment\nΓΕΙΑ ΣΟΥ\n\n  ευχαριστώ  \nHELLO\nhello\nA/B: c?\n", encoding="utf-8")
        signs, warns = parse_sign_file(f)
        assert signs == ["ΓΕΙΑ ΣΟΥ", "ευχαριστώ", "HELLO", "hello_2", "A/B: c?"], signs
        assert any("Duplicate" in w for w in warns)
        g = Path(d) / "legacy.txt"
        g.write_bytes("ΝΕΡΟ\nΨΩΜΙ\n".encode("cp1253"))
        signs, warns = parse_sign_file(g)
        assert signs == ["ΝΕΡΟ", "ΨΩΜΙ"] and any("cp1253" in w for w in warns)
    assert safe_filename("ΓΕΙΑ ΣΟΥ") == "ΓΕΙΑ_ΣΟΥ"
    assert safe_filename("A/B: c?") == "A_B_c"
    assert safe_filename("  ...  ") == "sign"
    assert safe_filename("café") == "café"
    print("parse / safe_filename ok")


def _fake_take(dir_: Path, sign_tag: str):
    cam = dir_ / "front_1844"
    cam.mkdir(parents=True)
    (cam / f"{sign_tag}_front_rgb.mp4").write_bytes(b"x")
    (dir_ / "take.json").write_text("{}")


def test_session_flow():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        lst = root / "list.txt"
        lst.write_text("ΓΕΙΑ\nΝΕΡΟ\nΨΩΜΙ\n", encoding="utf-8")
        s, _ = SignSession.create(root / "rec", "P01", lst)
        assert s.dir.name.startswith("P01_") and (s.dir / "signs.txt").exists()
        assert s.current == 0 and s.folder(0) == "001_ΓΕΙΑ"

        # discard then accept sign 0
        t = s.pending_take_dir(0); _fake_take(t, s.file_tag(0))
        s.discard(0, t)
        assert not t.exists() and len(list((s.dir / "_discarded").iterdir())) == 1
        t = s.pending_take_dir(0); _fake_take(t, s.file_tag(0))
        final = s.accept(0, t)
        assert final == s.dir / "001_ΓΕΙΑ" and (final / "front_1844" / "ΓΕΙΑ_front_rgb.mp4").exists()
        s.current = s.next_pending(0)
        assert s.current == 1

        # "interrupt": a take left in _pending, then reopen
        t = s.pending_take_dir(1); _fake_take(t, s.file_tag(1))
        s2 = SignSession.open(s.dir)
        assert s2.current == 1 and s2.progress_text == "1 / 3 recorded"
        moved = s2.cleanup_stale_pending()
        assert len(moved) == 1 and not any((s.dir / "_pending").iterdir())

        # accept 2 then 1 -> complete; wrap-around
        s2.current = 2
        t = s2.pending_take_dir(2); _fake_take(t, s2.file_tag(2)); s2.accept(2, t)
        assert s2.next_pending(2) == 1
        t = s2.pending_take_dir(1); _fake_take(t, s2.file_tag(1)); s2.accept(1, t)
        assert s2.complete and s2.next_pending(1) is None

        # re-record sign 0: old take moved to _discarded on accept
        t = s2.pending_take_dir(0); _fake_take(t, s2.file_tag(0))
        (t / "new").write_text("new")
        s2.accept(0, t)
        assert (s2.dir / "001_ΓΕΙΑ" / "new").exists()
        assert any("replaced" in p.name for p in (s2.dir / "_discarded").iterdir())
        data = json.loads((s2.dir / "progress.json").read_text(encoding="utf-8"))
        assert data["signs"][0]["attempts"] == 3 and data["signs"][0]["name"] == "ΓΕΙΑ"
        print("session flow ok:", sorted(p.name for p in s2.dir.iterdir()))




def test_add_and_refresh():
    import shutil
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        lst = root / "list.txt"
        lst.write_bytes("HOUSE\nΝΕΡΟ\nΨΩΜΙ\n".encode("cp1253"))      # legacy-encoded list
        s, _ = SignSession.create(root / "rec", "P01", lst)
        t = s.pending_take_dir(0); _fake_take(t, s.file_tag(0)); s.accept(0, t)   # HOUSE recorded
        s.current = 0

        # add HOUSE_2 right after HOUSE; it becomes current; folder numbers unchanged
        i = s.add_sign("HOUSE_2")
        assert i == 1 and s.current == 1 and s.signs == ["HOUSE", "HOUSE_2", "ΝΕΡΟ", "ΨΩΜΙ"]
        assert s.folder(1) == "004_HOUSE_2" and s.folder(2) == "002_ΝΕΡΟ"
        assert (s.dir / "signs.txt").read_text(encoding="utf-8").splitlines()[-1] == "HOUSE_2"
        try:
            s.add_sign("house"); raise AssertionError("duplicate accepted")
        except ValueError:
            pass
        assert s.suggest_name("HOUSE") == "HOUSE_3"
        t = s.pending_take_dir(1); _fake_take(t, s.file_tag(1)); s.accept(1, t)

        # user deletes 001_HOUSE and 004_HOUSE_2, adds a line to signs.txt, restores nothing
        shutil.rmtree(s.dir / "001_HOUSE"); shutil.rmtree(s.dir / "004_HOUSE_2")
        with open(s.dir / "signs.txt", "a", encoding="utf-8") as f:
            f.write("ΣΠΙΤΙ\n")
        rep = s.refresh()
        assert rep == {"unmarked": ["HOUSE"], "removed": ["HOUSE_2"], "marked": [], "added": ["ΣΠΙΤΙ"]}, rep
        assert s.signs == ["HOUSE", "ΝΕΡΟ", "ΨΩΜΙ", "ΣΠΙΤΙ"] and s.status(0) == "pending"
        assert s.folder(3) == "005_ΣΠΙΤΙ"
        assert "HOUSE_2" not in (s.dir / "signs.txt").read_text(encoding="utf-8")
        # a second refresh changes nothing (signs.txt and list agree)
        assert s.refresh() == {"unmarked": [], "removed": [], "marked": [], "added": []}
        # a folder restored by hand is picked up as recorded
        (s.dir / "002_ΝΕΡΟ").mkdir()
        assert s.refresh()["marked"] == ["ΝΕΡΟ"] and s.status(1) == "accepted"
        s2 = SignSession.open(s.dir)
        assert s2.signs == s.signs
        print("add / refresh ok:", s.signs)


if __name__ == "__main__":
    test_parse_and_names()
    test_session_flow()
    test_add_and_refresh()
    print("ALL OK")
