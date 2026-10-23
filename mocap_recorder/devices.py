"""Device discovery, camera naming and the persistent app config."""

from __future__ import annotations

import json
import re
from pathlib import Path

CONFIG_DIR = Path.home() / "Library" / "Application Support" / "MocapRecorder"
CONFIG_PATH = CONFIG_DIR / "config.json"


def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text())
    except Exception:
        return {}


def save_config(cfg: dict):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2))
    tmp.replace(CONFIG_PATH)


def safe_name(name: str) -> str:
    """File-system friendly camera / trial name."""
    return re.sub(r"[^A-Za-z0-9_-]+", "_", name.strip()).strip("_")


def discover_devices() -> list[str]:
    """Device ids (MxIds) of OAK devices that are visible and not yet in use."""
    import depthai as dai
    return [info.getDeviceId() for info in dai.Device.getAllAvailableDevices()]


def camera_name(cfg: dict, device_id: str, index: int) -> str:
    names = cfg.setdefault("camera_names", {})
    if device_id not in names:
        used = set(names.values())
        n = index + 1
        while f"cam{n}" in used:
            n += 1
        names[device_id] = f"cam{n}"
    return names[device_id]
