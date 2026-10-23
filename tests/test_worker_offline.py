"""Worker with an absent device: must report offline, retry, and quit cleanly."""
import multiprocessing as mp, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mocap_recorder.settings import Settings
from mocap_recorder.worker import worker_main

if __name__ == "__main__":
    ctx = mp.get_context("spawn")
    cmd, evt, prev = ctx.Queue(), ctx.Queue(), ctx.Queue()
    p = ctx.Process(target=worker_main, args=("1844301000000000XX", "camX", Settings().to_dict(), cmd, evt, prev))
    p.start()
    t, kinds = time.time(), []
    while time.time() - t < 12:
        try:
            _, k, pl = evt.get(timeout=0.5); kinds.append(k)
            print(k, str(pl)[:120])
        except Exception:
            pass
        if kinds.count("status") >= 3:
            break
    cmd.put(("quit", None)); p.join(10)
    print("alive after quit:", p.is_alive(), "exitcode:", p.exitcode)
    assert not p.is_alive() and "status" in kinds
