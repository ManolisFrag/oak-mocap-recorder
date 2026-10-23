# Entry point for the frozen .app. freeze_support() must run first so that
# spawned camera worker processes start the worker instead of a second GUI.
import multiprocessing
import sys


def selftest():
    """Spawn one camera worker for a non-existent device and expect an 'offline' status."""
    import time
    from mocap_recorder.settings import Settings
    from mocap_recorder.worker import worker_main
    ctx = multiprocessing.get_context("spawn")
    cmd, evt, prev = ctx.Queue(), ctx.Queue(), ctx.Queue()
    p = ctx.Process(target=worker_main, args=("SELFTEST", "selftest", Settings().to_dict(), cmd, evt, prev))
    p.start()
    deadline, ok = time.time() + 20, False
    while time.time() < deadline and not ok:
        try:
            _, kind, payload = evt.get(timeout=1)
            print(kind, payload)
            ok = kind == "status" and payload.get("state") == "offline"
        except Exception:
            pass
    cmd.put(("quit", None)); p.join(10)
    import av, depthai
    print("depthai", depthai.__version__, "av", av.__version__, "SELFTEST", "OK" if ok else "FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    multiprocessing.set_start_method("spawn", force=True)
    if "--selftest" in sys.argv:
        selftest()
    from mocap_recorder.app import main
    main()
