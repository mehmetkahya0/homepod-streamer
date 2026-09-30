"""Read/write config.json (shared by the CLI and the GUI)."""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Any

APP_NAME = "HomePod Streamer"
APP_VERSION = "1.0.0"
FROZEN = getattr(sys, "frozen", False)  # running as the PyInstaller exe


def app_data_dir() -> Path:
    """Where settings and logs live.

    The one-file exe unpacks itself into a temporary folder that is deleted on
    exit, so its data goes to %APPDATA%. From source, the project folder is used.
    """
    if FROZEN:
        base = Path(os.environ.get("APPDATA") or Path.home()) / APP_NAME
    else:
        base = Path(__file__).resolve().parent
    base.mkdir(parents=True, exist_ok=True)
    return base


CONFIG_PATH = app_data_dir() / "config.json"
LOG_PATH = app_data_dir() / "homepod-streamer.log"
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
    "close_to_tray": True,  # closing the window keeps the app running in the tray
    "tray_hint_shown": False,  # the "still running in the tray" notification was shown once
    "auto_stream": False,  # start streaming to the last speaker as soon as the app opens
}


def load_config() -> dict[str, Any]:
    """Read config.json merged over the defaults."""
    try:
        # utf-8-sig: tolerate a BOM (Notepad / PowerShell 5 add one when the file is edited by hand)
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
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
