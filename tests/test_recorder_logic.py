"""Hardware-free test of StreamRecorder using fake DepthAI messages."""
import csv, json, subprocess, sys, tempfile, threading
from datetime import timedelta
from pathlib import Path

import av
import depthai as dai
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
from mocap_recorder.settings import Settings
from mocap_recorder.worker import StreamRecorder
from test_writers import _fake_device_bitstream

FT = dai.EncodedFrame.FrameType


class Msg:
    def __init__(self, seq, ts_us, data=None, key=True, depth=None):
        self.seq, self.ts, self.data, self.key, self.depth = seq, ts_us, data, key, depth
    def getSequenceNum(self): return self.seq
    def getTimestamp(self): return timedelta(microseconds=self.ts)
    def getTimestampDevice(self): return timedelta(microseconds=self.ts - 5000)
    def getTransformation(self): raise RuntimeError("no transform in fake")
    def getFrameType(self): return FT.I if self.key else FT.P
    def getData(self): return np.frombuffer(self.data, np.uint8)
    def getFrame(self): return self.depth
    def getWidth(self): return self.depth.shape[1]
    def getHeight(self): return self.depth.shape[0]


class Vec:
    def __init__(self, seq, ts): self.seq, self.ts, self.x, self.y, self.z = seq, ts, 0.1, 0.2, 9.8
    def getSequenceNum(self): return self.seq
    def getTimestamp(self): return timedelta(microseconds=self.ts)
    def getTimestampDevice(self): return timedelta(microseconds=self.ts)


class Pkt:
    def __init__(self, seq, ts): self.acceleroMeter = Vec(seq, ts); self.gyroscope = Vec(seq, ts + 100)


class ImuMsg:
    def __init__(self, pkts, ts): self.packets, self.ts = pkts, ts
    def getTimestamp(self): return timedelta(microseconds=self.ts)


class FakeWorker:
    def __init__(self, s): self.settings, self.finished, self.msgs = s, [], []
    def emit(self, k, p=None): self.msgs.append((k, p))
    def stream_finished(self, n): self.finished.append(n)


def rows(p):
    with open(p) as f: return list(csv.DictReader(f))


def run_cam(cam_dir: Path, t_offset: int):
    s = Settings(); s.codec = "H.264"; s.rgb_resolution = "720p (1280x720)"; s.fps = 30
    w = FakeWorker(s)
    fps, T0 = 30, 1_000_000_000 + t_offset
    pkts = _fake_device_bitstream("h264", n=150, w=1280, h=720)       # keyframe every 30
    rgb, depth, imu = (StreamRecorder(n, None, w) for n in ("rgb", "depth", "imu"))
    rng = np.random.default_rng(1)
    dframes = [rng.integers(0, 5000, (360, 640), dtype=np.uint16) for _ in range(150)]
    start, stop = T0 + 45 * 33_333, T0 + 120 * 33_333   # start mid-GOP (frame 45)
    cam_dir.mkdir(parents=True)
    armed = False
    for i, (data, key) in enumerate(pkts):
        ts = T0 + i * 33_333
        if not armed and ts >= start - 300_000:  # GUI arms 300 ms ahead
            for r in (rgb, depth, imu): r.begin(cam_dir, start, 1)
            armed = True
        if i == 120:  # stop clicked at frame 120 time
            for r in (rgb, depth, imu): r.end(stop)
        if i == 77:
            continue  # simulate a dropped frame (sequence gap)
        rgb._handle_frame(Msg(i, ts, data, key), dai)
        depth._handle_frame(Msg(i, ts, depth=dframes[i]), dai)
        imu._handle_imu(ImuMsg([Pkt(i * 2, ts), Pkt(i * 2 + 1, ts + 16_000)], ts + 16_000))
    assert sorted(w.finished) == ["depth", "imu", "rgb"], w.finished
    return start, stop, dframes


def test_recorder():
    with tempfile.TemporaryDirectory() as d:
        sess = Path(d)
        res = [run_cam(sess / f"cam{k+1}_SERIAL{k}", off) for k, off in enumerate((0, 4_000, 9_000))]
        start, stop, dframes = res[0]  # checks below are for cam1
        cam = sess / "cam1_SERIAL0"
        r = rows(cam / "rgb_timestamps.csv")
        with av.open(str(cam / "rgb.mp4")) as c:
            n_dec = sum(1 for _ in c.decode(video=0))
        assert n_dec == len(r), (n_dec, len(r))
        first_ts = int(r[0]["host_ts_us"])
        assert first_ts <= start and start - first_ts <= 1_000_000, "pre-roll must begin at last keyframe"
        assert int(r[0]["sequence_num"]) == 30, r[0]          # keyframe before frame 45
        assert all(int(x["host_ts_us"]) < stop for x in r)
        assert "77" not in [x["sequence_num"] for x in r]
        print(f"rgb: {n_dec} frames decoded == csv rows, pre-roll from seq 30, drop recorded")

        dr = rows(cam / "depth_timestamps.csv")
        with av.open(str(cam / "depth.mkv")) as c:
            back = [f.to_ndarray() for f in c.decode(video=0)]
        assert len(back) == len(dr) and int(dr[0]["sequence_num"]) == 45
        for row, frm in zip(dr, back):
            assert np.array_equal(frm, dframes[int(row["sequence_num"])])
        print(f"depth: {len(back)} frames lossless, starts exactly at start_us")

        ir = rows(cam / "imu.csv")
        assert ir and all(start <= int(x["host_ts_us"]) for x in ir)
        print(f"imu: {len(ir)} rows")

        (sess / "session.json").write_text(json.dumps({"settings": {"fps": 30}}))
        out = subprocess.run([sys.executable, str(ROOT / "tools" / "sync_index.py"), str(sess)],
                             capture_output=True, text=True)
        print(out.stdout.strip(), out.stderr.strip())
        assert out.returncode == 0
        sr = rows(sess / "sync_rgb.csv")
        assert all(float(x["spread_ms"]) <= 16.7 for x in sr) and len(sr) > 60




def test_prefix():
    """Sign-list takes: exact cam_dir + '<sign>_<cam>_' prefix on every file."""
    s = Settings(); s.codec = "H.264"; s.rgb_resolution = "720p (1280x720)"
    w = FakeWorker(s)
    pkts = _fake_device_bitstream("h264", n=40, w=1280, h=720)
    with tempfile.TemporaryDirectory() as d:
        cam = Path(d) / "001_ΓΕΙΑ" / "front_1844"; cam.mkdir(parents=True)
        recs = [StreamRecorder(n, None, w) for n in ("rgb", "depth", "imu")]
        for r in recs:
            r.begin(cam, 0, 1, prefix="ΓΕΙΑ_front_")
        for i, (data, key) in enumerate(pkts):
            ts = i * 33_333
            if i == 35:
                for r in recs: r.end(ts)
            recs[0]._handle_frame(Msg(i, ts, data, key), dai)
            recs[1]._handle_frame(Msg(i, ts, depth=np.zeros((360, 640), np.uint16)), dai)
            recs[2]._handle_imu(ImuMsg([Pkt(i, ts)], ts))
        names = sorted(p.name for p in cam.iterdir())
        assert names == ["ΓΕΙΑ_front_depth.mkv", "ΓΕΙΑ_front_depth_timestamps.csv", "ΓΕΙΑ_front_imu.csv",
                         "ΓΕΙΑ_front_rgb.mp4", "ΓΕΙΑ_front_rgb_timestamps.csv"], names
        assert recs[0].take["written"] == 35
        print("prefix ok:", names)


if __name__ == "__main__":
    test_recorder()
    test_prefix()
    print("ALL OK")
