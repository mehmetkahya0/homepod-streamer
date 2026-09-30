"""Bridge between the GUI and the asyncio engine.

The asyncio loop runs on a separate thread; events reach the UI through a
thread-safe queue (the UI drains it periodically on its own thread).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import enum
import logging
import queue
import threading
from dataclasses import dataclass
from typing import Any, Coroutine

from pyatv import exceptions

import firewall
from capture import list_loopback_devices
from discovery import AirPlayDevice, scan_devices
from streamer import AlreadyStreamingError, LiveSession, LiveSettings, SessionLostError

_LOGGER = logging.getLogger(__name__)


class State(enum.Enum):
    IDLE = "idle"
    CONNECTING = "connecting"
    STREAMING = "streaming"
    STOPPING = "stopping"
    ERROR = "error"


@dataclass
class Event:
    """Event sent to the UI: kind ∈ state | devices | loopbacks | stats | volume | started | firewall."""

    kind: str
    data: Any = None
    message: str = ""


def friendly_error(ex: BaseException) -> str:
    """Turn an exception into a message suitable for the user."""
    if isinstance(ex, (SessionLostError, AlreadyStreamingError)):
        return str(ex)
    if isinstance(ex, exceptions.AuthenticationError):
        return ("The HomePod refused the connection. In the Home app, set 'Allow Speaker & TV Access' "
                "to 'Everyone on the Same Network'.")
    if isinstance(ex, (TimeoutError, asyncio.TimeoutError)) and "no response" in str(ex):
        # Connected, but the HomePod did not answer an RTSP request (e.g. SETUP). Most common
        # cause: the firewall blocks the HomePod from connecting back to this computer.
        return ("The HomePod could not complete setup. Windows Firewall may be blocking it from "
                "connecting to this app (see the warning above), or another device is using the HomePod.")
    if isinstance(ex, (exceptions.ConnectionFailedError, ConnectionError, TimeoutError, asyncio.TimeoutError)):
        return "Could not reach the HomePod. Is it on and on the same network as this computer?"
    if isinstance(ex, LookupError):
        return f"Audio source not found: {ex}"
    if isinstance(ex, FileNotFoundError) and "ffmpeg" in str(ex):
        return "ffmpeg not found. Select the 'Standard' resampler in Settings."
    if isinstance(ex, OSError) and getattr(ex, "errno", None) in (-9996, -9997, -9998, -9999):
        return f"Could not open the audio device ({ex}). It may be in use or disconnected."
    return f"{type(ex).__name__}: {ex}"


class Controller:
    """Runs scanning, stream start/stop and volume in the background."""

    def __init__(self) -> None:
        self.events: queue.Queue[Event] = queue.Queue()
        self.state = State.IDLE
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="asyncio", daemon=True)
        self._thread.start()
        self._session: LiveSession | None = None
        self._task: asyncio.Task | None = None

    # --- helpers -----------------------------------------------------------------
    def _submit(self, coro: Coroutine) -> concurrent.futures.Future:
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def _emit(self, kind: str, data: Any = None, message: str = "") -> None:
        self.events.put(Event(kind, data, message))

    def _set_state(self, state: State, message: str = "") -> None:
        self.state = state
        self._emit("state", state, message)

    # --- discovery ---------------------------------------------------------------
    def refresh_devices(self, timeout: int = 4) -> None:
        """Scan for AirPlay devices; the result arrives as a 'devices' event."""
        async def job() -> None:
            try:
                devices = await scan_devices(timeout)
            except Exception as ex:  # network error etc.
                _LOGGER.exception("Scan failed")
                self._emit("devices", [], friendly_error(ex))
                return
            self._emit("devices", devices, "" if devices else "No AirPlay speakers found on the network.")
        self._submit(job())

    def refresh_loopbacks(self) -> None:
        """List loopback devices; the result arrives as a 'loopbacks' event."""
        async def job() -> None:
            try:
                devs = await self._loop.run_in_executor(None, list_loopback_devices)
            except Exception as ex:
                _LOGGER.exception("Could not list loopback devices")
                self._emit("loopbacks", [], friendly_error(ex))
                return
            self._emit("loopbacks", devs)
        self._submit(job())

    # --- firewall ----------------------------------------------------------------
    def check_firewall(self) -> None:
        """Query the firewall state; the result arrives as a 'firewall' event."""
        async def job() -> None:
            state = await self._loop.run_in_executor(None, firewall.check)
            self._emit("firewall", state)
        self._submit(job())

    def allow_firewall(self) -> None:
        """Add an allow rule via UAC; the result arrives as a 'firewall' event."""
        async def job() -> None:
            ok = await self._loop.run_in_executor(None, firewall.allow)
            state = await self._loop.run_in_executor(None, firewall.check)
            self._emit("firewall", state, "" if ok else "Permission was not granted or the rule could not be added.")
        self._submit(job())

    # --- streaming ---------------------------------------------------------------
    def start(self, device: AirPlayDevice, settings: LiveSettings, volume: float) -> None:
        """Start streaming (does nothing if already running)."""
        if self.state in (State.CONNECTING, State.STREAMING, State.STOPPING):
            return
        self._set_state(State.CONNECTING, f"Connecting to {device.name}…")
        self._submit(self._run(device, settings, volume))

    async def _run(self, device: AirPlayDevice, settings: LiveSettings, volume: float) -> None:
        def on_started(desc: str) -> None:
            self._set_state(State.STREAMING, f"Playing on {device.name}")
            self._emit("started", desc)

        self._session = LiveSession(
            device, settings, volume,
            on_stats=lambda s: self._emit("stats", s),
            on_volume=lambda v: self._emit("volume", v),
            on_started=on_started,
        )
        self._task = asyncio.current_task()
        try:
            await self._session.run()
        except asyncio.CancelledError:
            self._set_state(State.IDLE, "Streaming stopped")
        except Exception as ex:
            _LOGGER.debug("Stream error", exc_info=True)
            _LOGGER.error("Stream error: %s: %s", type(ex).__name__, ex)
            self._set_state(State.ERROR, friendly_error(ex))
        else:
            self._set_state(State.IDLE, "Stream ended")
        finally:
            self._session = None
            self._task = None

    def stop(self) -> concurrent.futures.Future | None:
        """Stop streaming; returns a Future to wait for completion."""
        if self._task is None:
            return None
        self._set_state(State.STOPPING, "Stopping…")

        async def job() -> None:
            task = self._task
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        return self._submit(job())

    def set_volume(self, level: float) -> None:
        """Change the volume while streaming (ignored if not streaming)."""
        session = self._session
        if session and self.state == State.STREAMING:
            fut = self._submit(session.set_volume(level))
            fut.add_done_callback(
                lambda f: f.exception() and _LOGGER.warning("Could not set volume: %s", f.exception())
            )

    @property
    def level(self) -> float:
        """Current input level (0..1)."""
        session = self._session
        return session.level if session else 0.0

    def shutdown(self, timeout: float = 4.0) -> None:
        """Stop streaming and shut down the background loop."""
        fut = self.stop()
        if fut:
            try:
                fut.result(timeout)
            except Exception:
                _LOGGER.debug("Timed out stopping during shutdown", exc_info=True)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=2)
