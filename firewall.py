"""Windows Firewall check.

During AirPlay 2 setup (RTSP SETUP) the HomePod connects back to this computer
(timing/event channels). If inbound connections to the running program are
blocked, SETUP gets no response and times out after ~10 s. Rules are stored per
program path, so python.exe may be allowed while pythonw.exe (the GUI) is not.
"""

from __future__ import annotations

import base64
import ctypes
import enum
import logging
import subprocess
import sys

_LOGGER = logging.getLogger(__name__)

RULE_NAME = "HomePod Streamer"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class FirewallState(enum.Enum):
    ALLOWED = "allowed"
    BLOCKED = "blocked"  # an explicit block rule exists
    MISSING = "missing"  # no allow rule (blocked by default)
    UNKNOWN = "unknown"  # could not be queried


def process_image_path() -> str:
    """Real exe path of the running process (the actual interpreter, not the venv launcher)."""
    buf = ctypes.create_unicode_buffer(1024)
    ctypes.windll.kernel32.GetModuleFileNameW(None, buf, len(buf))
    return buf.value


def _ps_quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def check(program: str | None = None) -> FirewallState:
    """Return whether inbound connections to the program are allowed (takes ~2 s)."""
    if sys.platform != "win32":
        return FirewallState.ALLOWED
    program = program or process_image_path()
    script = (
        "@(Get-NetFirewallApplicationFilter | Where-Object { $_.Program -eq " + _ps_quote(program) + " } | "
        "Get-NetFirewallRule | Where-Object { $_.Direction -eq 'Inbound' -and $_.Enabled -eq 'True' } | "
        "ForEach-Object { $_.Action.ToString() }) -join ','"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=20, creationflags=_NO_WINDOW,
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as ex:
        _LOGGER.debug("Could not query the firewall: %s", ex)
        return FirewallState.UNKNOWN
    actions = [a for a in out.split(",") if a]
    _LOGGER.debug("Firewall rules (%s): %s", program, actions or "none")
    if "Block" in actions:  # on Windows, block rules take precedence over allow rules
        return FirewallState.BLOCKED
    if "Allow" in actions:
        return FirewallState.ALLOWED
    return FirewallState.MISSING


def allow(program: str | None = None) -> bool:
    """With admin approval (UAC), allow inbound connections from the local network to the program.

    The rule only covers the local subnet (LocalSubnet); any block rules for the
    same program are disabled. Returns False if the user declines UAC.
    """
    program = program or process_image_path()
    inner = (
        f"$p = {_ps_quote(program)}; "
        f"Get-NetFirewallRule -DisplayName {_ps_quote(RULE_NAME)} -ErrorAction SilentlyContinue | Remove-NetFirewallRule; "
        "Get-NetFirewallApplicationFilter | Where-Object { $_.Program -eq $p } | Get-NetFirewallRule | "
        "Where-Object { $_.Direction -eq 'Inbound' -and $_.Action -eq 'Block' } | Disable-NetFirewallRule; "
        f"New-NetFirewallRule -DisplayName {_ps_quote(RULE_NAME)} -Direction Inbound -Action Allow "
        "-Program $p -Profile Any -RemoteAddress LocalSubnet | Out-Null"
    )
    encoded = base64.b64encode(inner.encode("utf-16-le")).decode()
    outer = (
        "try { $pr = Start-Process powershell -Verb RunAs -Wait -PassThru -WindowStyle Hidden "
        f"-ArgumentList '-NoProfile','-EncodedCommand','{encoded}'; exit $pr.ExitCode }} catch {{ exit 1223 }}"
    )
    try:
        result = subprocess.run(["powershell", "-NoProfile", "-Command", outer],
                                capture_output=True, text=True, timeout=120, creationflags=_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as ex:
        _LOGGER.error("Could not add firewall rule: %s", ex)
        return False
    if result.returncode == 1223:
        _LOGGER.warning("Administrator approval was declined; firewall rule not added")
        return False
    if result.returncode != 0:
        _LOGGER.error("Could not add firewall rule (code %s): %s", result.returncode, result.stderr.strip())
        return False
    _LOGGER.info("Firewall rule added: %s", program)
    return check(program) == FirewallState.ALLOWED
