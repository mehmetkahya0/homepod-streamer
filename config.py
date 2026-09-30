"""Read/write config.json (shared by the CLI and the GUI)."""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any

CONFIG_PATH = Path(__file__).with_name("config.json")
_LOGGER = logging.getLogger(__name__)

DEFAULTS: dict[str, Any] = {
    "last_device": None,  # AirPlay device identifier
    "loopback": None,  # loopback device name; None = system default
    "volume": 30.0,
    "resampler": "ffmpeg" if shutil.which("ffmpeg") else "miniaudio",
    "prebuffer_ms": 100,
    "max_buffer_ms": 300,
    "theme": "system",  # system | light | dark
    "debug": False,
}


def load_config() -> dict[str, Any]:
    """Read config.json merged over the defaults."""
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    except json.JSONDecodeError as ex:
        _LOGGER.warning("config.json is corrupt, using defaults: %s", ex)
        data = {}
    return {**DEFAULTS, **data}


def save_config(cfg: dict[str, Any]) -> None:
    """Write config.json."""
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def update_config(**changes: Any) -> dict[str, Any]:
    """Update the given keys and save."""
    cfg = load_config()
    cfg.update(changes)
    save_config(cfg)
    return cfg
