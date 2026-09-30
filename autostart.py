"""Start with Windows via the per-user Run registry key (no admin rights needed)."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from config import APP_NAME, FROZEN

_LOGGER = logging.getLogger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def launch_command() -> str:
    """Command line that starts the app hidden in the tray."""
    if FROZEN:
        return f'"{sys.executable}" --minimized'
    # From source: use pythonw next to the running interpreter so no console opens
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    main = Path(__file__).resolve().with_name("main.py")
    return f'"{pythonw if pythonw.exists() else exe}" "{main}" --minimized'


def _read() -> str | None:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, APP_NAME)
            return str(value)
    except FileNotFoundError:
        return None


def is_enabled() -> bool:
    return sys.platform == "win32" and _read() is not None


def set_enabled(enabled: bool) -> None:
    """Add or remove the Run entry."""
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, launch_command())
            _LOGGER.info("Start with Windows enabled: %s", launch_command())
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
                _LOGGER.info("Start with Windows disabled")
            except FileNotFoundError:
                pass


def repair() -> None:
    """If enabled but pointing elsewhere (e.g. the exe was moved), point it at this copy.

    Only the exe repairs the entry; running from source must not hijack it.
    """
    if sys.platform != "win32" or not FROZEN:
        return
    current = _read()
    if current is not None and current != launch_command():
        _LOGGER.info("Updating the Start with Windows entry (was: %s)", current)
        set_enabled(True)
