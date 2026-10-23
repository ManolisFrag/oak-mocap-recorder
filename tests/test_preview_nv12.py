"""NV12 -> RGB preview conversion (full-range BT.601, with row stride padding)."""
import sys
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mocap_recorder.worker import nv12_to_rgb


def rgb_to_nv12(rgb, stride, plane_h):
    r, g, b = [rgb[..., i].astype(np.float32) for i in range(3)]
    y = 0.299 * r + 0.587 * g + 0.114 * b
    u = (b - y) / 1.772 + 128
    v = (r - y) / 1.402 + 128
    h, w = y.shape
    buf = np.zeros(stride * plane_h + stride * h // 2, np.uint8)
    buf[:stride * h].reshape(h, stride)[:, :w] = np.clip(np.round(y), 0, 255)
    uv = buf[stride * plane_h:].reshape(h // 2, stride)
    uv[:, 0:w:2] = np.clip(np.round(u[::2, ::2]), 0, 255)
    uv[:, 1:w:2] = np.clip(np.round(v[::2, ::2]), 0, 255)
    return buf


def test_colours():
    h, w = 360, 640
    img = np.zeros((h, w, 3), np.uint8)
    colours = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255), (128, 128, 128), (30, 200, 120)]
    for k, c in enumerate(colours):
        img[:, k * 100:(k + 1) * 100] = c
    out = nv12_to_rgb(rgb_to_nv12(img, stride=704, plane_h=368), w, h, 704, 368)
    assert out.shape == (h, w, 3)
    for k, c in enumerate(colours):
        got = out[h // 2, k * 100 + 50]
        assert np.all(np.abs(got.astype(int) - c) <= 3), (c, got)
    print("nv12 -> rgb ok (padded stride, 6 colours within ±3)")


if __name__ == "__main__":
    test_colours()
