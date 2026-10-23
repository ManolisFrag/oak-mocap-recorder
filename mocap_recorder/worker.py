"""One process per OAK camera.

The worker owns the dai.Device, keeps the pipeline running between takes and
only opens/closes files on record start/stop. If the device disappears it
closes its files cleanly, reports the gap and keeps retrying the connection;
on reconnect during a recording it continues into new "_partN" files.

Commands (GUI → worker, cmd_q):
    ("record", {"session_dir": str, "start_us": int})
    ("stop",   {"stop_us": int})
    ("ir",     settings_dict)
    ("quit",   None)
Events (worker → GUI, evt_q):  (device_id, kind, payload)
Preview frames (worker → GUI, preview_q, lossy): (device_id, bytes, w, h)
"""

from __future__ import annotations

import json
import queue
import threading
import time
import traceback
from datetime import timedelta
from pathlib import Path

from .settings import PREVIEW_FPS, Settings

ENCODED_STREAMS = ("rgb", "left", "right")


def _us(td: timedelta) -> int:
    return td // timedelta(microseconds=1)


def _now_us() -> int:
    import depthai as dai
    return _us(dai.Clock.now())


def nv12_to_rgb(data, w: int, h: int, stride: int, plane_h: int):
    """NV12 (full-range BT.601, as produced by the OAK ISP) -> packed RGB uint8 (h, w, 3)."""
    import numpy as np
    buf = np.asarray(data, dtype=np.uint8).ravel()
    y = buf[:stride * h].reshape(h, stride)[:, :w].astype(np.float32)
    uv = buf[stride * plane_h:stride * plane_h + stride * (h // 2)].reshape(h // 2, stride)[:, :w]
    u = np.repeat(np.repeat(uv[:, 0::2].astype(np.float32) - 128, 2, 0), 2, 1)[:h, :w]
    v = np.repeat(np.repeat(uv[:, 1::2].astype(np.float32) - 128, 2, 0), 2, 1)[:h, :w]
    rgb = np.stack((y + 1.402 * v, y - 0.344136 * u - 0.714136 * v, y + 1.772 * u), axis=-1)
    return np.clip(rgb, 0, 255).astype(np.uint8)


def _transform_info(msg) -> dict | None:
    try:
        t = msg.getTransformation()
        if not t.isValid():
            return None
        w, h = t.getSize()
        return {
            "width": w, "height": h,
            "intrinsic_matrix": [list(map(float, r)) for r in t.getIntrinsicMatrix()],
            "distortion_model": str(t.getDistortionModel()),
            "distortion_coefficients": list(map(float, t.getDistortionCoefficients())),
            "source_size": list(t.getSourceSize()),
            "hfov_deg": float(t.getHFov()), "vfov_deg": float(t.getVFov()),
        }
    except Exception:
        return None


class StreamRecorder:
    """Pulls one DepthAI output queue in its own thread and writes it when recording."""

    def __init__(self, name: str, q, worker: "CameraWorker"):
        self.name, self.q, self.w = name, q, worker
        self.lock = threading.Lock()
        self.writer = None
        self.ts_csv = None
        self.recording = False
        self.start_us = self.stop_us = None
        self.out_dir: Path | None = None
        self.prefix = ""                 # e.g. "<sign>_<cam>_" in sign-list sessions
        self.part = 1
        self.frame_idx = 0
        self.gop: list = []          # pre-roll since last keyframe (H.26x)
        self.last_seq = None
        self.stats = {"frames": 0, "dropped": 0, "written": 0, "fps": 0.0}
        self.take = {"written": 0, "dropped": 0}   # counters for the current/last take
        self._fps_t, self._fps_n = time.monotonic(), 0
        self.transform: dict | None = None
        self.thread = threading.Thread(target=self._run, name=f"stream-{name}", daemon=True)

    # ---- control (called from the command thread) -------------------------
    def begin(self, out_dir: Path, start_us: int, part: int, prefix: str = ""):
        with self.lock:
            self.out_dir, self.start_us, self.stop_us, self.part = out_dir, start_us, None, part
            self.prefix = prefix
            self.frame_idx = 0
            self.take = {"written": 0, "dropped": 0}
            self.recording = True

    def end(self, stop_us: int):
        with self.lock:
            self.stop_us = stop_us

    def force_close(self):
        with self.lock:
            self._close_files()
            self.recording = False

    # ---- internals --------------------------------------------------------
    def _suffix(self) -> str:
        return "" if self.part == 1 else f"_part{self.part}"

    def _open_files(self, first_msg):
        from .writers import CsvWriter, EncodedVideoWriter, Ffv1DepthWriter, TIMESTAMP_HEADER
        s, sfx, pre = self.w.settings, self._suffix(), self.prefix
        d = self.out_dir
        if self.name == "imu":
            self.ts_csv = CsvWriter(d / f"{pre}imu{sfx}.csv",
                                    ["sensor", "sequence_num", "device_ts_us", "host_ts_us", "x", "y", "z"])
            return
        if self.name == "depth":
            dw, dh = first_msg.getWidth(), first_msg.getHeight()
            self.writer = Ffv1DepthWriter(d / f"{pre}depth{sfx}.mkv", dw, dh, s.fps)
        else:
            w, h = (s.rgb_size if self.name == "rgb" else s.mono_size)
            self.writer = EncodedVideoWriter(d / f"{pre}{self.name}{sfx}.mp4", s.codec_name, w, h, s.fps)
        self.ts_csv = CsvWriter(d / f"{pre}{self.name}{sfx}_timestamps.csv", TIMESTAMP_HEADER)

    def _close_files(self):
        if self.writer is not None:
            try:
                self.writer.close()
                if getattr(self.writer, "error", None):
                    self.w.emit("warning", f"{self.name}: writer error {self.writer.error}")
            finally:
                self.writer = None
        if self.ts_csv is not None:
            self.ts_csv.close()
            self.ts_csv = None

    def _tick_stats(self, seq: int):
        if self.last_seq is not None and seq > self.last_seq + 1:
            self.stats["dropped"] += seq - self.last_seq - 1
            if self.recording:
                self.take["dropped"] += seq - self.last_seq - 1
        self.last_seq = seq
        self.stats["frames"] += 1
        self._fps_n += 1
        now = time.monotonic()
        if now - self._fps_t >= 1.0:
            self.stats["fps"] = self._fps_n / (now - self._fps_t)
            self._fps_t, self._fps_n = now, 0

    def _run(self):
        import depthai as dai
        while not self.w.stop_event.is_set():
            try:
                msg = self.q.get(timedelta(milliseconds=500)) if hasattr(self.q, "get") else None
            except Exception:
                # queue closed => device lost / pipeline stopped
                self.w.device_lost.set()
                return
            if msg is None:
                continue
            try:
                if self.name == "imu":
                    self._handle_imu(msg)
                else:
                    self._handle_frame(msg, dai)
            except Exception as e:
                self.w.emit("warning", f"{self.name}: {e}")
                traceback.print_exc()

    def _handle_frame(self, msg, dai):
        seq = msg.getSequenceNum()
        self._tick_stats(seq)
        ts_us = _us(msg.getTimestamp())
        dev_us = _us(msg.getTimestampDevice())
        if self.transform is None:
            self.transform = _transform_info(msg)
        keyframe = True
        is_h26x = self.name in ENCODED_STREAMS and self.w.settings.codec_name != "mjpeg"
        if is_h26x:
            keyframe = msg.getFrameType() == dai.EncodedFrame.FrameType.I

        with self.lock:
            if not self.recording:
                if is_h26x:  # keep pre-roll from the latest keyframe on
                    if keyframe:
                        self.gop.clear()
                    if self.gop or keyframe:
                        self.gop.append((msg, seq, ts_us, dev_us, keyframe))
                return
            if self.stop_us is not None and ts_us >= self.stop_us:
                self._close_files()
                self.recording = False
                self.gop.clear()
                self.w.stream_finished(self.name)
                return
            if ts_us < self.start_us and not is_h26x:
                return
            pending = []
            if is_h26x and self.writer is None:
                # start at the last keyframe before start_us so the file is decodable
                pending = list(self.gop)
                self.gop.clear()
            pending.append((msg, seq, ts_us, dev_us, keyframe))
            for m, sq, t, dv, kf in pending:
                if self.writer is None:
                    if is_h26x and not kf:
                        continue
                    self._open_files(m)
                if self.name == "depth":
                    ok = self.writer.write(m.getFrame(), t - self.start_us)
                else:
                    ok = self.writer.write(bytes(m.getData()), t - self.start_us, kf)
                self.ts_csv.row(self.frame_idx if ok else -1, sq, dv, t, int(bool(ok)))
                if ok:
                    self.frame_idx += 1
                    self.stats["written"] += 1
                    self.take["written"] += 1
                else:
                    self.stats["dropped"] += 1
                    self.take["dropped"] += 1

    def _handle_imu(self, msg):
        with self.lock:
            rec = self.recording
            if rec and self.stop_us is not None and _us(msg.getTimestamp()) >= self.stop_us:
                self._close_files()
                self.recording = False
                self.w.stream_finished(self.name)
                return
        for pkt in msg.packets:
            for sensor, r in (("accel", pkt.acceleroMeter), ("gyro", pkt.gyroscope)):
                seq = r.getSequenceNum()
                if sensor == "accel":
                    self._tick_stats(seq)
                ts = _us(r.getTimestamp())
                if not rec or ts < self.start_us:
                    continue
                with self.lock:
                    if not self.recording:
                        return
                    if self.ts_csv is None:
                        self._open_files(None)
                    self.ts_csv.row(sensor, seq, _us(r.getTimestampDevice()), ts,
                                    f"{r.x:.6f}", f"{r.y:.6f}", f"{r.z:.6f}")
                if sensor == "accel":
                    self.stats["written"] += 1
                    self.take["written"] += 1


class CameraWorker:
    def __init__(self, device_id: str, name: str, settings: dict, cmd_q, evt_q, preview_q):
        self.device_id, self.name = device_id, name
        self.settings = Settings.from_dict(settings)
        self.cmd_q, self.evt_q, self.preview_q = cmd_q, evt_q, preview_q
        self.stop_event = threading.Event()
        self.device_lost = threading.Event()
        self.device = None
        self.pipeline = None
        self.streams: dict[str, StreamRecorder] = {}
        self.session: dict | None = None     # {"session_dir", "start_us", "stop_us"}
        self.part = 1
        self._finished: set[str] = set()
        self._lock = threading.Lock()

    def emit(self, kind: str, payload=None):
        try:
            self.evt_q.put_nowait((self.device_id, kind, payload))
        except Exception:
            pass

    # ---- device lifecycle -------------------------------------------------
    def _connect(self):
        import depthai as dai
        from .pipeline import apply_ir, build_pipeline
        info = dai.DeviceInfo(self.device_id)
        self.device = dai.Device(info)
        pipeline, queues = build_pipeline(self.device, self.settings)
        self.pipeline = pipeline
        pipeline.start()
        apply_ir(self.device, self.settings)
        self.device_lost.clear()
        self.preview_q_dai = queues.pop("preview")
        self.streams = {n: StreamRecorder(n, q, self) for n, q in queues.items()}
        for s in self.streams.values():
            s.thread.start()
        self.emit("connected", {
            "product": self.device.getProductName() or self.device.getDeviceName(),
            "usb_speed": str(self.device.getUsbSpeed()).split(".")[-1],
            "imu": self.device.getConnectedIMU(),
            "streams": list(self.streams),
        })
        if str(self.device.getUsbSpeed()).split(".")[-1] not in ("SUPER", "SUPER_PLUS"):
            self.emit("warning", f"USB speed is {self.device.getUsbSpeed()} — use a USB3 port/cable "
                                 "or the camera will drop frames.")

    def _disconnect(self):
        for s in self.streams.values():
            s.force_close()
        try:
            if self.pipeline is not None:
                self.pipeline.stop()
        except Exception:
            pass
        try:
            if self.device is not None:
                self.device.close()
        except Exception:
            pass
        self.device = None
        self.pipeline = None
        self.streams = {}

    # ---- recording --------------------------------------------------------
    def _cam_dir(self) -> Path:
        if self.session.get("cam_dir"):
            d = Path(self.session["cam_dir"])
        else:
            name = self.session.get("cam_name") or self.name
            d = Path(self.session["session_dir"]) / f"{name}_{self.device_id}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _write_metadata(self, d: Path):
        import depthai as dai
        sfx = "" if self.part == 1 else f"_part{self.part}"
        pre = self.session.get("file_prefix", "")
        try:
            self.device.readCalibration().eepromToJsonFile(str(d / f"{pre}calib.json"))
        except Exception as e:
            self.emit("warning", f"could not export calibration: {e}")
        info = {
            "device_id": self.device_id, "name": self.session.get("cam_name") or self.name,
            "product": self.device.getProductName(),
            "usb_speed": str(self.device.getUsbSpeed()),
            "imu": self.device.getConnectedIMU(),
            "depthai_version": dai.__version__,
            "settings": self.settings.to_dict(),
            "depth_size": list(self.settings.depth_size),
            "depth_units": "millimetres (uint16), 0 = invalid",
            "clock": "host_ts_us = dai.Clock.now() domain (steady clock shared by all cameras)",
        }
        if self.session.get("sign"):
            info["sign"] = self.session["sign"]
        (d / f"{pre}device{sfx}.json").write_text(json.dumps(info, indent=2))

    def _write_intrinsics(self, d: Path):
        sfx = "" if self.part == 1 else f"_part{self.part}"
        pre = (self.session or {}).get("file_prefix", "")
        intr = {n: s.transform for n, s in self.streams.items() if s.transform}
        if intr:
            (d / f"{pre}intrinsics{sfx}.json").write_text(json.dumps(intr, indent=2))

    def _begin_recording(self):
        d = self._cam_dir()
        self._write_metadata(d)
        self._finished.clear()
        for s in self.streams.values():
            s.begin(d, self.session["start_us"], self.part, self.session.get("file_prefix", ""))
        self.emit("recording", {"dir": str(d), "part": self.part})

    def stream_finished(self, name: str):
        with self._lock:
            self._finished.add(name)
            done = self._finished >= set(self.streams)
        if done and self.session is not None:
            self._write_intrinsics(self._cam_dir())
            self.emit("stopped", {"dir": str(self._cam_dir()),
                                  "written": {n: s.take["written"] for n, s in self.streams.items()},
                                  "dropped": {n: s.take["dropped"] for n, s in self.streams.items()}})
            self.session = None

    # ---- main loop --------------------------------------------------------
    def _handle_cmd(self, cmd, arg):
        if cmd == "quit":
            self.stop_event.set()
        elif cmd == "ir":
            self.settings = Settings.from_dict({**self.settings.to_dict(), **{
                k: v for k, v in arg.items() if k.startswith("ir_")}})
            if self.device is not None:
                from .pipeline import apply_ir
                apply_ir(self.device, self.settings)
        elif cmd == "record":
            self.session = dict(arg)
            self.part = 1
            if self.device is not None:
                self._begin_recording()
            else:
                self.emit("warning", "camera offline at record start; will join when it reconnects")
        elif cmd == "stop":
            if self.session is None:
                return
            self.session["stop_us"] = arg["stop_us"]
            if not self.streams:
                self.emit("stopped", {"dir": None})
                self.session = None
                return
            for s in self.streams.values():
                s.end(arg["stop_us"])
            # watchdog: if a stream stops producing, close it anyway
            threading.Timer(3.0, self._stop_watchdog).start()

    def _stop_watchdog(self):
        if self.session is None:
            return
        for n, s in list(self.streams.items()):
            if n not in self._finished:
                s.force_close()
                self.stream_finished(n)

    def _pump_preview(self, last: float) -> float:
        if time.monotonic() - last < 1.0 / PREVIEW_FPS:
            return last
        try:
            f = self.preview_q_dai.tryGet()
        except Exception:
            self.device_lost.set()
            return last
        if f is None:
            return last
        try:
            w, h = f.getWidth(), f.getHeight()
            rgb = nv12_to_rgb(f.getData(), w, h, f.getStride(), f.getPlaneHeight())
            self.preview_q.put_nowait((self.device_id, rgb.tobytes(), w, h))
        except Exception:
            pass  # GUI busy: drop preview frame, never block recording
        return time.monotonic()

    def run(self):
        last_preview = last_stats = 0.0
        retry_at = 0.0
        while not self.stop_event.is_set():
            # commands
            try:
                while True:
                    cmd, arg = self.cmd_q.get_nowait()
                    self._handle_cmd(cmd, arg)
            except queue.Empty:
                pass
            if self.stop_event.is_set():
                break

            if self.device is None:
                if time.monotonic() >= retry_at:
                    try:
                        self.emit("status", {"state": "connecting"})
                        self._connect()
                        if self.session is not None and self.session.get("stop_us") is None:
                            self.part += 1
                            self.emit("reconnected", {"part": self.part})
                            self._begin_recording()
                    except Exception as e:
                        self._disconnect()
                        self.emit("status", {"state": "offline", "error": str(e)})
                        retry_at = time.monotonic() + 2.0
                time.sleep(0.05)
                continue

            if self.device_lost.is_set() or self.device.isClosed():
                self.emit("disconnected", {"t_us": _now_us(), "recording": self.session is not None})
                self._disconnect()
                retry_at = time.monotonic() + 1.0
                continue

            last_preview = self._pump_preview(last_preview)
            now = time.monotonic()
            if now - last_stats >= 0.5:
                last_stats = now
                self.emit("stats", {n: dict(s.stats, backlog=getattr(s.writer, "backlog", lambda: 0)())
                                    for n, s in self.streams.items()})
            time.sleep(0.005)

        if self.session is not None:
            for s in self.streams.values():
                s.end(_now_us())
            time.sleep(0.5)
        self._disconnect()
        self.emit("exited", None)


def worker_main(device_id: str, name: str, settings: dict, cmd_q, evt_q, preview_q):
    """multiprocessing entry point."""
    # Unsent preview frames must never keep this process alive at exit (a full pipe would block
    # the queue's feeder thread until the GUI reads it).
    preview_q.cancel_join_thread()
    try:
        CameraWorker(device_id, name, settings, cmd_q, evt_q, preview_q).run()
    except Exception:
        evt_q.put((device_id, "error", traceback.format_exc()))
