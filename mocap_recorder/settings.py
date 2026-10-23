"""Recording settings shared by the GUI and camera workers (global for all cameras)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

RGB_RESOLUTIONS = {
    "720p (1280x720)": (1280, 720),
    "1080p (1920x1080)": (1920, 1080),
    "4K (3840x2160)": (3840, 2160),
}
FPS_OPTIONS = [15, 20, 25, 30, 45, 60]
CODECS = {"H.265": "hevc", "H.264": "h264", "MJPEG": "mjpeg"}
MONO_RESOLUTIONS = {"1280x800": (1280, 800), "640x400": (640, 400)}
DEPTH_PRESETS = ["FAST_ACCURACY", "FAST_DENSITY", "DEFAULT", "HIGH_DETAIL", "ROBOTICS", "FACE",
                 "DENSITY", "ACCURACY"]
# Anti-flicker: auto-exposure uses multiples of half the mains period, so lights don't flicker/band.
ANTI_BANDING = {"50 Hz (Europe, Asia, Africa)": "MAINS_50_HZ", "60 Hz (Americas)": "MAINS_60_HZ",
                "Auto": "AUTO", "Off": "OFF"}
SETTINGS_VERSION = 2   # bump when the recommended defaults change (triggers a one-time prompt)
STREAMS = ["rgb", "depth", "left", "right", "imu"]

PREVIEW_SIZE = (640, 360)
PREVIEW_FPS = 15
IMU_RATE_HZ = 200

# Approximate RVC2 (OAK-D Pro W) hardware limits, used for warnings only.
H26X_BUDGET_MPS = 3840 * 2160 * 30 / 1e6     # ~249 MP/s shared by all H.26x streams
MJPEG_BUDGET_MPS = 450.0
DEPTH_BYTES_PER_PIXEL = 0.9                   # measured FFV1 size on realistic depth
MJPEG_BYTES_PER_PIXEL = 0.25


@dataclass
class Settings:
    """Defaults = the recommended stable 30 fps setup for 3 cameras on one USB hub:
    1080p H.265 RGB (cheap: encoded on the camera), depth from 640x400 mono sensors saved at
    640x360 (4x less USB / camera / laptop load than 1280x800), fast depth preset, 50 Hz anti-flicker."""
    rgb_resolution: str = "1080p (1920x1080)"
    fps: int = 30
    codec: str = "H.265"
    mono_resolution: str = "640x400"
    depth_preset: str = "FAST_ACCURACY"
    undistort_rgb: bool = True
    anti_banding: str = "50 Hz (Europe, Asia, Africa)"
    streams: dict = field(default_factory=lambda: {"rgb": True, "depth": True, "left": False,
                                                   "right": False, "imu": True})
    ir_dot_enabled: bool = True
    ir_dot_intensity: float = 0.7
    ir_flood_enabled: bool = False
    ir_flood_intensity: float = 0.5

    # ---- derived values -------------------------------------------------
    @property
    def rgb_size(self) -> tuple[int, int]:
        return RGB_RESOLUTIONS[self.rgb_resolution]

    @property
    def mono_size(self) -> tuple[int, int]:
        return MONO_RESOLUTIONS[self.mono_resolution]

    @property
    def codec_name(self) -> str:
        return CODECS[self.codec]

    @property
    def needs_stereo(self) -> bool:
        return any(self.streams.get(s) for s in ("depth", "left", "right"))

    @property
    def depth_size(self) -> tuple[int, int]:
        """Depth aligned to RGB: RGB aspect (16:9), capped at 1280x720.

        Above 30 fps or with 640x400 mono sensors it drops to 640x360 so
        lossless encoding of 3 cameras keeps up in real time.
        """
        if self.fps > 30 or self.mono_size[0] <= 640 or self.rgb_size[0] < 1280:
            return (640, 360)
        return (1280, 720)

    def bitrate_kbps(self) -> int:
        w, h = self.rgb_size
        return int(min(w * h * self.fps * 0.3 / 1000, 50_000))

    def mono_bitrate_kbps(self) -> int:
        w, h = self.mono_size
        return int(min(w * h * self.fps * 0.25 / 1000, 20_000))

    # ---- validation -----------------------------------------------------
    def capture_dict(self) -> dict:
        """Settings that need a camera restart (everything except the live IR controls)."""
        return {k: v for k, v in self.to_dict().items() if not k.startswith("ir_")}

    @classmethod
    def recommended(cls, keep_ir_from: "Settings | None" = None) -> "Settings":
        r = cls()
        if keep_ir_from is not None:
            for k, v in keep_ir_from.to_dict().items():
                if k.startswith("ir_"):
                    setattr(r, k, v)
        return r

    def problems(self, n_cameras: int = 1) -> tuple[list[str], list[str]]:
        """Return (errors, warnings). Errors block applying the settings."""
        errors, warnings = [], []
        w, h = self.rgb_size
        if not any(self.streams.values()):
            errors.append("Select at least one stream to record.")
        if w >= 3840 and self.fps > 30:
            errors.append("4K is limited to 30 fps on the OAK-D Pro W.")
        enc_mps = 0.0
        if self.streams.get("rgb"):
            enc_mps += w * h * self.fps / 1e6
        mw, mh = self.mono_size
        for s in ("left", "right"):
            if self.streams.get(s):
                enc_mps += mw * mh * self.fps / 1e6
        budget = MJPEG_BUDGET_MPS if self.codec == "MJPEG" else H26X_BUDGET_MPS
        if enc_mps > budget:
            warnings.append(f"Encoder load {enc_mps:.0f} MP/s exceeds the ~{budget:.0f} MP/s "
                            "on-device encoder budget; expect dropped frames.")
        if self.needs_stereo and self.mono_size == (1280, 800) and self.fps > 45:
            warnings.append("Stereo depth at 1280x800 above 45 fps may drop frames; "
                            "consider 640x400 mono resolution.")
        if (self.streams.get("depth") and self.depth_size == (1280, 720) and self.fps >= 30
                and n_cameras >= 2):
            warnings.append(f"Full-resolution depth (1280x720) on {n_cameras} cameras at {self.fps} fps is "
                            "near the USB-hub and laptop limits; mono 640x400 is recommended for a "
                            "stable 30 fps.")
        if self.depth_preset in ("HIGH_DETAIL", "ACCURACY", "DENSITY") and self.fps >= 30 and self.needs_stereo:
            warnings.append(f"The {self.depth_preset} depth preset does more processing on the camera; "
                            "use FAST_ACCURACY if frames drop.")
        if w >= 3840 and self.needs_stereo:
            warnings.append("4K + depth is heavy for the device; verify there are no drops.")
        return errors, warnings

    def estimated_mb_per_s(self, n_cameras: int) -> float:
        """Rough disk throughput estimate for all cameras together."""
        per = 0.0
        w, h = self.rgb_size
        if self.streams.get("rgb"):
            per += (w * h * self.fps * MJPEG_BYTES_PER_PIXEL / 1e6 if self.codec == "MJPEG"
                    else self.bitrate_kbps() / 8000)
        mw, mh = self.mono_size
        for s in ("left", "right"):
            if self.streams.get(s):
                per += (mw * mh * self.fps * MJPEG_BYTES_PER_PIXEL / 1e6 if self.codec == "MJPEG"
                        else self.mono_bitrate_kbps() / 8000)
        if self.streams.get("depth"):
            dw, dh = self.depth_size
            per += dw * dh * self.fps * DEPTH_BYTES_PER_PIXEL / 1e6
        return per * max(n_cameras, 1)

    # ---- (de)serialisation --------------------------------------------
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Settings":
        s = cls()
        for k, v in (d or {}).items():
            if hasattr(s, k):
                setattr(s, k, v)
        # tolerate stale config values
        if s.rgb_resolution not in RGB_RESOLUTIONS:
            s.rgb_resolution = cls.rgb_resolution
        if s.codec not in CODECS:
            s.codec = cls.codec
        if s.mono_resolution not in MONO_RESOLUTIONS:
            s.mono_resolution = cls.mono_resolution
        if s.fps not in FPS_OPTIONS:
            s.fps = 30
        if s.depth_preset not in DEPTH_PRESETS:
            s.depth_preset = cls.depth_preset
        if s.anti_banding not in ANTI_BANDING:
            s.anti_banding = cls.anti_banding
        s.streams = {k: bool(s.streams.get(k, False)) for k in STREAMS}
        return s
