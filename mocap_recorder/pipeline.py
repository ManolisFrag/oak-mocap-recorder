"""DepthAI v3 pipeline for one OAK-D Pro W (RVC2), built from global Settings.

Graph (only the parts needed by the enabled streams are created):

  CAM_A ─ requestOutput(rgb_size, NV12, undistorted) ─ VideoEncoder ─▶ "rgb"
        ├ requestOutput(depth_size, NV12, undistorted) ─▶ stereo.inputAlignTo
        └ requestOutput(PREVIEW_SIZE, NV12)           ─▶ "preview" (same fps, throttled on host)
  CAM_B ─ requestOutput(mono_size) ┬▶ stereo.left
                                   └▶ VideoEncoder ─▶ "left"
  CAM_C ─ requestOutput(mono_size) ┬▶ stereo.right
                                   └▶ VideoEncoder ─▶ "right"
  stereo.depth (uint16 mm, aligned to CAM_A, depth_size) ─▶ "depth"
  IMU (accel + gyro raw) ─▶ "imu"

Aligning through `inputAlignTo` with an RGB output that uses the same crop
(16:9, default CROP resize) and undistortion as the recorded RGB output is the
official RVC2 method (depthai-core examples/python/ImageAlign/depth_align.py):
depth pixel (u, v) corresponds to RGB pixel (u * rgb_w / depth_w, v * rgb_h / depth_h).
"""

from __future__ import annotations

import depthai as dai

from .settings import ANTI_BANDING, IMU_RATE_HZ, PREVIEW_SIZE, Settings

_PROFILES = {
    "hevc": dai.VideoEncoderProperties.Profile.H265_MAIN,
    "h264": dai.VideoEncoderProperties.Profile.H264_MAIN,
    "mjpeg": dai.VideoEncoderProperties.Profile.MJPEG,
}


def _encoder(pipeline: dai.Pipeline, src: dai.Node.Output, s: Settings, bitrate_kbps: int):
    profile = _PROFILES[s.codec_name]
    enc = pipeline.create(dai.node.VideoEncoder).build(src, frameRate=float(s.fps), profile=profile)
    if s.codec_name == "mjpeg":
        enc.setQuality(95)
    else:
        enc.setBitrateKbps(bitrate_kbps)
        enc.setKeyframeFrequency(int(s.fps))   # 1 s GOP bounds the pre-roll at record start
        enc.setNumBFrames(0)                   # pts == dts, simple muxing
    return enc


def build_pipeline(device: dai.Device, s: Settings) -> tuple[dai.Pipeline, dict[str, dai.MessageQueue]]:
    """Create (but do not start) the pipeline; returns it and the host output queues."""
    p = dai.Pipeline(device)
    queues: dict[str, dai.MessageQueue] = {}
    fps = float(s.fps)
    qsize = max(int(s.fps) * 2, 30)   # ~2 s of host-side buffering per stream

    anti_banding = getattr(dai.CameraControl.AntiBandingMode, ANTI_BANDING[s.anti_banding])
    cam_rgb = p.create(dai.node.Camera).build(dai.CameraBoardSocket.CAM_A)
    cam_rgb.initialControl.setAntiBandingMode(anti_banding)

    if s.streams.get("rgb"):
        rgb_out = cam_rgb.requestOutput(s.rgb_size, dai.ImgFrame.Type.NV12, fps=fps,
                                        enableUndistortion=s.undistort_rgb)
        enc = _encoder(p, rgb_out, s, s.bitrate_kbps())
        queues["rgb"] = enc.out.createOutputQueue(maxSize=qsize, blocking=False)

    # All outputs of a camera use the same fps: mixing rates on one RVC2 sensor (e.g. a 15 fps
    # preview next to a 30 fps recording) degraded the image. The preview is throttled on the host.
    queues["preview"] = cam_rgb.requestOutput(PREVIEW_SIZE, dai.ImgFrame.Type.NV12, fps=fps
                                              ).createOutputQueue(maxSize=1, blocking=False)

    if s.needs_stereo:
        left = p.create(dai.node.Camera).build(dai.CameraBoardSocket.CAM_B)
        right = p.create(dai.node.Camera).build(dai.CameraBoardSocket.CAM_C)
        for cam in (left, right):
            cam.initialControl.setAntiBandingMode(anti_banding)
        left_out = left.requestOutput(s.mono_size, fps=fps)
        right_out = right.requestOutput(s.mono_size, fps=fps)

        if s.streams.get("depth"):
            stereo = p.create(dai.node.StereoDepth)
            stereo.setDefaultProfilePreset(getattr(dai.node.StereoDepth.PresetMode, s.depth_preset))
            stereo.setLeftRightCheck(True)
            stereo.setSubpixel(True)
            left_out.link(stereo.left)
            right_out.link(stereo.right)
            align_out = cam_rgb.requestOutput(s.depth_size, dai.ImgFrame.Type.NV12, fps=fps,
                                              enableUndistortion=s.undistort_rgb)
            align_out.link(stereo.inputAlignTo)
            queues["depth"] = stereo.depth.createOutputQueue(maxSize=qsize, blocking=False)

        mono_kbps = s.mono_bitrate_kbps()
        if s.streams.get("left"):
            queues["left"] = _encoder(p, left_out, s, mono_kbps).out.createOutputQueue(
                maxSize=qsize, blocking=False)
        if s.streams.get("right"):
            queues["right"] = _encoder(p, right_out, s, mono_kbps).out.createOutputQueue(
                maxSize=qsize, blocking=False)

    if s.streams.get("imu") and device.getConnectedIMU() not in ("", "NONE"):
        imu = p.create(dai.node.IMU)
        imu.enableIMUSensor([dai.IMUSensor.ACCELEROMETER_RAW, dai.IMUSensor.GYROSCOPE_RAW], IMU_RATE_HZ)
        imu.setBatchReportThreshold(1)
        imu.setMaxBatchReports(10)
        queues["imu"] = imu.out.createOutputQueue(maxSize=200, blocking=False)

    return p, queues


def apply_ir(device: dai.Device, s: Settings):
    """Live IR control (no pipeline restart needed). Silently ignored on devices without IR."""
    try:
        device.setIrLaserDotProjectorIntensity(s.ir_dot_intensity if s.ir_dot_enabled else 0.0)
        device.setIrFloodLightIntensity(s.ir_flood_intensity if s.ir_flood_enabled else 0.0)
    except Exception:
        pass
