"""System tray icon (pystray).

pystray runs its own Win32 message loop on a separate thread and menu callbacks
run on that thread. Tk is not thread-safe, so:

* the menu is built only from a plain :class:`TraySnapshot` that the UI thread
  keeps up to date (never from Tk widgets), and
* every action is handed back to the UI thread through ``post``.

On Windows the menu is built ahead of time, not when it is opened, so the UI
calls :meth:`TrayIcon.refresh` whenever something shown in the menu changes.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pystray
from PIL import Image, ImageDraw

_LOGGER = logging.getLogger(__name__)

ICON_PNG = Path(__file__).with_name("assets") / "icon.png"
VOLUME_STEPS = (10, 20, 30, 40, 50, 60, 70, 80, 90, 100)

# Status dot colors on the tray icon (None = no dot)
DOT_COLORS = {
    "idle": None,
    "connecting": (255, 159, 10),
    "reconnecting": (255, 159, 10),
    "stopping": (255, 159, 10),
    "streaming": (48, 209, 88),
    "error": (255, 69, 58),
}


@dataclass
class TraySnapshot:
    """Everything the tray menu shows, written by the UI thread only."""

    state: str = "idle"  # controller.State value
    status: str = "Ready"
    active: bool = False  # connecting / streaming / reconnecting / stopping
    can_start: bool = False
    devices: list[str] = field(default_factory=list)
    device: str | None = None
    sources: list[str] = field(default_factory=list)
    source: str | None = None
    volume: int = 30


@dataclass
class TrayActions:
    """UI-thread callbacks (each is invoked via ``post``)."""

    toggle_stream: Callable[[], None]
    select_device: Callable[[str], None]
    select_source: Callable[[str], None]
    set_volume: Callable[[int], None]
    show_window: Callable[[], None]
    exit_app: Callable[[], None]


def _icon_image(state: str, size: int = 64) -> Image.Image:
    """App icon with a colored status dot in the bottom-right corner."""
    base = Image.open(ICON_PNG).convert("RGBA").resize((size, size), Image.LANCZOS)
    color = DOT_COLORS.get(state)
    if color:
        draw = ImageDraw.Draw(base)
        r = size * 0.22
        cx, cy = size - r - 1, size - r - 1
        draw.ellipse((cx - r - 3, cy - r - 3, cx + r + 3, cy + r + 3), fill=(255, 255, 255, 255))
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(*color, 255))
    return base


class TrayIcon:
    """Tray icon with a Start/Stop, Speaker, Audio source and Volume menu."""

    def __init__(self, title: str, actions: TrayActions, post: Callable[[Callable[[], None]], None]) -> None:
        self.title = title
        self.snapshot = TraySnapshot()
        self._actions = actions
        self._post = post
        self._images = {state: _icon_image(state) for state in DOT_COLORS}
        self._shown_state = "idle"
        self._icon = pystray.Icon("HomePodStreamer", self._images["idle"], title, menu=self._build_menu())
        self._started = False
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ menu
    def _do(self, fn: Callable[[], None]) -> Callable:
        """Wrap a UI action so it runs on the UI thread."""
        return lambda icon, item: self._post(fn)

    def _build_menu(self) -> pystray.Menu:
        s = self.snapshot
        item = pystray.MenuItem
        sep = pystray.Menu.SEPARATOR

        def devices_menu():
            if not s.devices:
                return [item("No speakers found", None, enabled=False)]
            return [item(name, self._do(lambda n=name: self._actions.select_device(n)),
                         checked=lambda _i, n=name: s.device == n, radio=True,
                         enabled=lambda _i: not s.active)
                    for name in s.devices]

        def sources_menu():
            return [item(name, self._do(lambda n=name: self._actions.select_source(n)),
                         checked=lambda _i, n=name: s.source == n, radio=True,
                         enabled=lambda _i: not s.active)
                    for name in s.sources]

        def volume_menu():
            steps = [item(f"{v}%", self._do(lambda v=v: self._actions.set_volume(v)),
                          checked=lambda _i, v=v: s.volume == v, radio=True)
                     for v in VOLUME_STEPS]
            return [
                item("Volume up (+5)", self._do(lambda: self._actions.set_volume(min(100, s.volume + 5)))),
                item("Volume down (-5)", self._do(lambda: self._actions.set_volume(max(0, s.volume - 5)))),
                sep, *steps,
            ]

        return pystray.Menu(
            item(lambda _i: s.status, None, enabled=False),
            sep,
            item(lambda _i: "Stop" if s.active else "Start streaming",
                 self._do(self._actions.toggle_stream),
                 enabled=lambda _i: s.active or s.can_start),
            item("Speaker", pystray.Menu(devices_menu)),
            item("Audio source", pystray.Menu(sources_menu)),
            item(lambda _i: f"Volume: {s.volume}%", pystray.Menu(volume_menu)),
            sep,
            item("Show window", self._do(self._actions.show_window), default=True),
            item("Exit", self._do(self._actions.exit_app)),
        )

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        """Show the icon (its message loop runs on its own thread)."""
        self._icon.run_detached()
        self._started = True

    def stop(self) -> None:
        if self._started:
            self._started = False
            try:
                self._icon.stop()
            except Exception:
                _LOGGER.debug("Tray icon stop failed", exc_info=True)

    def refresh(self) -> None:
        """Rebuild the menu, tooltip and icon from the current snapshot (UI thread)."""
        if not self._started:
            return
        with self._lock:
            try:
                if self.snapshot.state != self._shown_state:
                    self._shown_state = self.snapshot.state
                    self._icon.icon = self._images.get(self.snapshot.state, self._images["idle"])
                self._icon.title = f"{self.title} — {self.snapshot.status}"[:127]
                self._icon.update_menu()
            except Exception:
                _LOGGER.debug("Tray refresh failed", exc_info=True)

    def notify(self, message: str, title: str | None = None) -> None:
        """Show a Windows notification from the tray icon."""
        if self._started and self._icon.HAS_NOTIFICATION:
            try:
                self._icon.notify(message, title or self.title)
            except Exception:
                _LOGGER.debug("Tray notification failed", exc_info=True)
