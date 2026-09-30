"""Sending audio to a HomePod (RAOP) via pyatv."""

from __future__ import annotations

import asyncio
import io
import logging
import shutil
import socket
import struct
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pyatv
from pyatv import exceptions
from pyatv.const import Protocol
from pyatv.interface import AppleTV, AudioListener, MediaMetadata

from capture import AudioFormat, FfmpegResampler, LoopbackCapture, display_name, output_monitor
from discovery import AirPlayDevice, find_device, pairing_blocked

_LOGGER = logging.getLogger(__name__)


class SessionLostError(ConnectionError):
    """The HomePod closed the AirPlay session (e.g. another source connected); not retried."""


class ConnectionLostError(ConnectionError):
    """The connection to the HomePod broke (network drop, HomePod restart); retried."""


class AlreadyStreamingError(RuntimeError):
    """A stream is already running on this computer."""


class StreamLock:
    """Machine-wide single-stream lock (Windows named mutex).

    If two senders (e.g. GUI + terminal) connect to the same HomePod at once,
    one can silently turn into a "zombie" and keep the HomePod busy; this
    prevents that. Windows releases the lock automatically if the process dies.
    """

    NAME = "Local\\HomePodStreamer.Stream"

    def __init__(self) -> None:
        self._handle = None

    def __enter__(self) -> "StreamLock":
        if sys.platform == "win32":
            self._handle = acquire_named_mutex(self.NAME)
            if self._handle is None:
                raise AlreadyStreamingError(
                    "Another HomePod Streamer stream is already running on this computer (another window or terminal)."
                )
        return self

    def __exit__(self, *exc) -> None:
        if self._handle:
            release_named_mutex(self._handle)
            self._handle = None


def acquire_named_mutex(name: str) -> int | None:
    """Create a named mutex; returns None if it already exists in another process."""
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel32.CreateMutexW(None, False, name)
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        return None
    return handle


def release_named_mutex(handle: int) -> None:
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle(handle)


RAOP_RATE = 44100  # pyatv RAOP always sends 44.1 kHz / 16-bit / stereo

# miniaudio ends the stream early with "unknown length" (0xFFFFFFFF) or 0;
# a very large finite value works (~6.2 hours at 48 kHz, then the stream restarts).
_WAV_DATA_SIZE = 0xFFFFFFF0 - 36

ACCESS_HINT = (
    "In the Home app, set Home Settings > Speakers & TV > Allow Speaker & TV Access to "
    "'Everyone on the Same Network' with no password."
)


async def connect(device: AirPlayDevice) -> AppleTV:
    """Connect to the device, logging connection errors clearly."""
    if pairing_blocked(device):
        _LOGGER.warning(
            "%s reports an access restriction (pairing=%s). The connection may be refused. %s",
            device.name, device.pairing, ACCESS_HINT,
        )
    if device.requires_password:
        _LOGGER.warning("%s requires a password; no password is configured in pyatv.", device.name)

    loop = asyncio.get_running_loop()
    _LOGGER.info("Connecting: %s", device)
    try:
        return await pyatv.connect(device.config, loop)
    except exceptions.AuthenticationError as ex:
        _LOGGER.error("Authentication error (pairing/password may be required): %s. %s", ex, ACCESS_HINT)
        raise
    except (exceptions.ConnectionFailedError, OSError) as ex:
        _LOGGER.error("Connection failed: %s (is the HomePod on and on the same network?)", ex)
        raise


async def play_file(device: AirPlayDevice, path: str, volume: float | None = None) -> None:
    """Play a local audio file (or http(s) URL) on the device from start to finish."""
    if not path.startswith(("http://", "https://")) and not Path(path).is_file():
        raise FileNotFoundError(path)

    atv = await connect(device)
    try:
        if volume is not None:
            _LOGGER.info("Setting volume to %s%%", volume)
            await atv.audio.set_volume(volume)
        _LOGGER.info("Playing: %s", path)
        await atv.stream.stream_file(path)
        _LOGGER.info("Playback finished.")
    except exceptions.AuthenticationError as ex:
        _LOGGER.error("Authorization refused while streaming: %s. %s", ex, ACCESS_HINT)
        raise
    except exceptions.NotSupportedError as ex:
        _LOGGER.error("Device does not support file streaming: %s", ex)
        raise
    finally:
        atv.close()


@dataclass
class LiveSettings:
    """Live streaming settings."""

    loopback: str | None = None  # loopback device name fragment; None = default output
    chunk_ms: int = 20  # capture period
    prebuffer_ms: int = 100  # amount to accumulate after an underrun before handing out data again
    max_buffer_ms: int = 300  # if the queue exceeds this it is trimmed to prebuffer level (latency cap)
    resampler: str = "miniaudio"  # "miniaudio" (inside pyatv) or "ffmpeg" (soxr)
    stats_interval: float = 10.0
    auto_reconnect: bool = True  # reconnect after a network drop / HomePod restart


def wav_header(fmt: AudioFormat) -> bytes:
    """Build a PCM WAV header for a (practically) endless stream."""
    return (
        b"RIFF" + struct.pack("<I", _WAV_DATA_SIZE + 36) + b"WAVE"
        + b"fmt " + struct.pack("<IHHIIHH", 16, 1, fmt.channels, fmt.rate,
                                fmt.rate * fmt.frame_bytes, fmt.frame_bytes, fmt.sample_width * 8)
        + b"data" + struct.pack("<I", _WAV_DATA_SIZE)
    )


def _peak(data: bytes) -> int:
    samples = memoryview(data).cast("h")
    return max(-min(samples), max(samples)) if samples else 0


class LiveWavStream(io.BufferedIOBase):
    """Turn captured PCM into an endless WAV stream that pyatv can read.

    ``push()`` is called from the capture thread, ``read()`` from pyatv's
    executor thread. pyatv paces reads against real time itself and may request
    very small chunks (a few bytes), so ``read()`` never blocks. If there is no
    data it returns silence (WASAPI loopback produces no data while nothing is
    playing; an empty return means end-of-stream to pyatv). After an underrun it
    keeps returning silence until ``prebuffer_ms`` of data has accumulated
    (jitter buffer).
    """

    def __init__(self, fmt: AudioFormat, settings: LiveSettings) -> None:
        self.fmt = fmt
        self._header = wav_header(fmt)
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._stopped = False
        self._prebuffering = True
        self._prebuffer_bytes = fmt.bytes_for_ms(settings.prebuffer_ms)
        self._max_bytes = max(fmt.bytes_for_ms(settings.max_buffer_ms), 2 * self._prebuffer_bytes)
        self.stats = {"in": 0, "out": 0, "dropped": 0, "silence": 0, "underruns": 0, "peak": 0}
        self.last_peak = 0  # peak of the last push() (read without locking)
        self.last_push = 0.0
        self.total_out = 0  # total PCM bytes handed to pyatv (for the WAV limit check)

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def push(self, data: bytes) -> None:
        """Append new PCM; if the latency cap is exceeded, trim the queue to prebuffer level."""
        if not data:
            return
        peak = _peak(data)
        self.last_peak = peak
        self.last_push = time.monotonic()
        with self._lock:
            if self._stopped:
                return
            self._buf += data
            self.stats["in"] += len(data)
            self.stats["peak"] = max(self.stats["peak"], peak)
            if len(self._buf) > self._max_bytes:
                excess = len(self._buf) - self._prebuffer_bytes
                excess -= excess % self.fmt.frame_bytes
                del self._buf[:excess]
                self.stats["dropped"] += excess

    def read(self, size: int | None = -1) -> bytes:
        if self._header:
            size = len(self._header) if size is None or size < 0 else size
            out, self._header = self._header[:size], self._header[size:]
            return out
        fb = self.fmt.frame_bytes
        want = self._prebuffer_bytes if size is None or size < 0 else max(fb, size - size % fb)
        with self._lock:
            if self._stopped:
                return b""
            if self._prebuffering and len(self._buf) >= self._prebuffer_bytes:
                self._prebuffering = False
            # the ffmpeg pipe may deliver partial frames; only hand out whole frames
            n = min(want, len(self._buf) - len(self._buf) % fb)
            if not self._prebuffering and n:
                out = bytes(self._buf[:n])
                del self._buf[:n]
            else:
                if not self._prebuffering:
                    self._prebuffering = True
                    self.stats["underruns"] += 1
                out = bytes(want)
                self.stats["silence"] += want
            self.stats["out"] += len(out)
            self.total_out += len(out)
            return out

    def stop(self) -> None:
        """End the stream; the next read() returns b"" (EOF)."""
        with self._lock:
            self._stopped = True

    def take_stats(self) -> dict:
        """Return and reset the stats; adds queue fill level in ms."""
        with self._lock:
            s = dict(self.stats)
            s["queued_ms"] = len(self._buf) / self.fmt.frame_bytes / self.fmt.rate * 1000
            self.stats = dict.fromkeys(self.stats, 0)
        return s


class LivePipeline:
    """Manage the loopback capture -> (ffmpeg) -> LiveWavStream chain.

    When following the system default (``settings.loopback is None``) the
    capture switches to the new default output mid-stream. If an explicitly
    selected device disappears, it falls back to the system default. The WAV
    stream handed to pyatv keeps its sample rate; if the new device's rate
    differs, an ffmpeg resampler is inserted. Only if that is impossible is
    ``restart_needed`` set so the session can restart the pipeline.
    """

    def __init__(self, settings: LiveSettings, on_source_changed: Callable[[str], None] | None = None) -> None:
        self.settings = settings
        self.on_source_changed = on_source_changed
        self.stream: LiveWavStream | None = None
        self.restart_needed = threading.Event()
        self._capture = LoopbackCapture(self._on_capture, settings.chunk_ms, settings.loopback)
        self._resampler: FfmpegResampler | None = None
        self._switch_lock = threading.RLock()  # _switch is also called under it
        self._following_default = settings.loopback is None
        self._stopped = False

    def _on_capture(self, data: bytes) -> None:
        if self._resampler:
            self._resampler.write(data)
        elif self.stream:
            self.stream.push(data)

    def _on_resampled(self, data: bytes) -> None:
        if self.stream:
            self.stream.push(data)

    def _make_resampler(self, in_fmt: AudioFormat, out_rate: int) -> AudioFormat:
        self._resampler = FfmpegResampler(in_fmt, out_rate, self._on_resampled, self.settings.chunk_ms)
        return self._resampler.start()

    def start(self) -> LiveWavStream:
        """Start capturing and return the stream to hand to pyatv."""
        cap_fmt = self._capture.start()
        out_fmt = cap_fmt
        try:
            if self.settings.resampler == "ffmpeg" and cap_fmt.rate != RAOP_RATE:
                out_fmt = self._make_resampler(cap_fmt, RAOP_RATE)
            elif cap_fmt.rate != RAOP_RATE:
                _LOGGER.info("Resampling: miniaudio %d -> %d Hz", cap_fmt.rate, RAOP_RATE)
        except Exception:
            self._capture.stop()
            raise
        self.stream = LiveWavStream(out_fmt, self.settings)
        output_monitor().subscribe(self._on_output_poll)
        return self.stream

    def _on_output_poll(self, default_name: str | None, changed: bool) -> None:
        """Called by the output monitor on every poll (monitor thread)."""
        # Check and switch atomically: while a switch is in progress the old capture is
        # already stopped, and an unlocked "capture inactive" check would switch back.
        with self._switch_lock:
            self._handle_output_poll(default_name, changed)

    def _handle_output_poll(self, default_name: str | None, changed: bool) -> None:
        if self._stopped:
            return
        if self._following_default and changed and default_name:
            self._switch(default_name, f"default output changed to {default_name}")
        elif not self._capture.active:
            if self._following_default:
                target = default_name
                reason = "capture stopped, reopening the default output"
            else:
                self._following_default = True
                target = default_name
                reason = f"{self.settings.loopback} is no longer available, falling back to the system default"
            if target:
                self._switch(target, reason)

    def _switch(self, device_name: str, reason: str) -> None:
        """Swap the capture device without interrupting the AirPlay session."""
        with self._switch_lock:
            if self._stopped or self.stream is None:
                return
            current = self._capture.device
            if current and display_name(current) == device_name and self._capture.active:
                return
            _LOGGER.info("Switching audio source: %s", reason)
            old_rate = int(current["defaultSampleRate"]) if current else None
            self._capture.stop()
            new_capture = LoopbackCapture(self._on_capture, self.settings.chunk_ms, device_name)
            try:
                new_fmt = new_capture.start()
            except Exception as ex:
                _LOGGER.warning("Could not open %s (%s); will retry", device_name, ex)
                self._capture = new_capture  # inactive; the next poll retries
                return
            self._capture = new_capture
            target_rate = self.stream.fmt.rate
            try:
                if self._resampler and new_fmt.rate != old_rate:
                    # Keep the output rate, restart ffmpeg with the new input rate
                    self._resampler.stop()
                    self._resampler = None
                    if new_fmt.rate != target_rate:
                        self._make_resampler(new_fmt, target_rate)
                elif not self._resampler and new_fmt.rate != target_rate:
                    if shutil.which("ffmpeg"):
                        self._make_resampler(new_fmt, target_rate)
                    else:
                        _LOGGER.info("Sample rate changed (%d -> %d Hz); restarting the pipeline",
                                     target_rate, new_fmt.rate)
                        self.restart_needed.set()
                        return
            except Exception:
                _LOGGER.exception("Resampler switch failed; restarting the pipeline")
                self.restart_needed.set()
                return
        if self.on_source_changed:
            self.on_source_changed(self.description)

    def stop(self) -> None:
        self._stopped = True
        output_monitor().unsubscribe(self._on_output_poll)
        with self._switch_lock:
            if self.stream:
                self.stream.stop()
            self._capture.stop()
            if self._resampler:
                self._resampler.stop()
                self._resampler = None

    @property
    def description(self) -> str:
        """Short description of the source and conversion chain (for the UI)."""
        dev = self._capture.device
        if not dev:
            return ""
        rate = int(dev["defaultSampleRate"])
        if rate == RAOP_RATE:
            conv = "no conversion"
        else:
            conv = "soxr" if self._resampler else "miniaudio"
        return f"{dev['name']} · {rate / 1000:g} kHz → 44.1 kHz ({conv})"


class _VolumeListener(AudioListener):
    """Forward volume changes made on the device side (e.g. from an iPhone)."""

    def __init__(self, callback: Callable[[float], None]) -> None:
        self._callback = callback

    def volume_update(self, old_level: float, new_level: float) -> None:
        self._callback(new_level)

    def volume_device_update(self, output_device, old_level: float, new_level: float) -> None:
        pass

    def outputdevices_update(self, old_devices, new_devices) -> None:
        pass




# Errors that end a session for good (retrying cannot help)
_FATAL_ERRORS = (SessionLostError, AlreadyStreamingError, exceptions.AuthenticationError, LookupError, FileNotFoundError)
# Errors that mean "the connection broke" and are worth a reconnect
_RECOVERABLE_ERRORS = (ConnectionError, OSError, TimeoutError, asyncio.TimeoutError,
                       exceptions.ConnectionFailedError, exceptions.ProtocolError)

PROBE_INTERVAL = 2.0  # s between reachability probes while streaming
PROBE_FAILURES = 2  # consecutive failed probes before the connection counts as lost
MAX_BACKOFF = 30.0  # s


class LiveSession:
    """A live streaming session to one device: connect, stream, volume, stats, reconnect.

    ``run()`` lasts until cancelled (Ctrl+C / Stop), until ``duration`` elapses,
    or until a non-recoverable error (e.g. another device took over the HomePod).
    """

    def __init__(
        self,
        device: AirPlayDevice,
        settings: LiveSettings,
        volume: float | None = None,
        on_stats: Callable[[dict], None] | None = None,
        on_volume: Callable[[float], None] | None = None,
        on_started: Callable[[str], None] | None = None,
        on_reconnecting: Callable[[int, float, BaseException], None] | None = None,
        on_source_changed: Callable[[str], None] | None = None,
    ) -> None:
        self.device = device
        self.settings = settings
        self.volume_level = volume
        self.on_stats = on_stats
        self.on_volume = on_volume
        self.on_started = on_started
        self.on_reconnecting = on_reconnecting
        self.on_source_changed = on_source_changed
        self.atv: AppleTV | None = None
        self.pipeline: LivePipeline | None = None
        self._ever_streamed = False
        self._flowed_this_session = False

    # ------------------------------------------------------------------ status
    @property
    def level(self) -> float:
        """Peak level of the last captured packet (0..1), for the level meter."""
        stream = self.pipeline.stream if self.pipeline else None
        if not stream or time.monotonic() - stream.last_push > 0.15:
            return 0.0  # loopback produces no data while nothing is playing
        return stream.last_peak / 32768

    @property
    def volume(self) -> float | None:
        return self.atv.audio.volume if self.atv else None

    async def set_volume(self, level: float) -> None:
        """Change the volume (0-100) while streaming; also re-applied after a reconnect."""
        self.volume_level = max(0.0, min(100.0, level))
        if self.atv:
            await self.atv.audio.set_volume(self.volume_level)

    def _on_device_volume(self, level: float) -> None:
        self.volume_level = level
        if self.on_volume:
            self.on_volume(level)

    # ----------------------------------------------------------------- helpers
    async def _report_stats(self, stream: LiveWavStream) -> None:
        fmt = stream.fmt

        def sec(nbytes: int) -> float:
            return nbytes / fmt.frame_bytes / fmt.rate

        while True:
            await asyncio.sleep(self.settings.stats_interval)
            s = stream.take_stats()
            stats = {
                "captured_s": sec(s["in"]), "sent_s": sec(s["out"]),
                "silence_ms": sec(s["silence"]) * 1000, "underruns": s["underruns"],
                "dropped_ms": sec(s["dropped"]) * 1000, "queued_ms": s["queued_ms"],
                "peak": s["peak"] / 32768,
            }
            _LOGGER.info(
                "captured %.1fs | sent %.1fs | silence %.0f ms (%d underruns) | dropped %.0f ms | queue %.0f ms | peak %.0f%%",
                stats["captured_s"], stats["sent_s"], stats["silence_ms"], stats["underruns"],
                stats["dropped_ms"], stats["queued_ms"], stats["peak"] * 100,
            )
            if self.on_stats:
                self.on_stats(stats)

    def _raop_port(self) -> int:
        service = self.device.config.get_service(Protocol.RAOP)
        return service.port if service and service.port else 7000

    async def _reachable(self, timeout: float = 2.0) -> bool:
        """Can a TCP connection be opened to the HomePod's AirPlay port?"""
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(self.device.address, self._raop_port()), timeout)
        except (OSError, asyncio.TimeoutError):
            return False
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
        return True

    async def _loss_error(self, what: str) -> Exception:
        """Classify a lost session: HomePod still reachable = taken over, otherwise network loss."""
        if await self._reachable():
            return SessionLostError(
                "The HomePod closed the session. Another device may have connected to it."
            )
        return ConnectionLostError(f"Lost connection to the HomePod ({what}).")

    def _rtsp_transport_open(self) -> bool | None:
        """Is the RTSP control connection open? None if unknown.

        When the HomePod closes the session (another source connected, the
        device restarted), pyatv keeps sending UDP audio and does not report it.
        The connection state only lives in an internal object, so we look there
        (pyatv 0.18.0).
        """
        try:
            raop = self.atv.stream.get(Protocol.RAOP) if self.atv else None
            conn = raop.playback_manager._connection  # noqa: SLF001
        except AttributeError:
            return None
        if conn is None:
            return None
        return conn.transport is not None

    async def _watchdog(self) -> None:
        """Raise when the RTSP control connection closes."""
        seen_open = False
        warned = False
        while True:
            await asyncio.sleep(1.0)
            state = self._rtsp_transport_open()
            if state is None:
                if not seen_open and not warned:
                    warned = True
                    _LOGGER.debug("Cannot read RTSP connection state; watchdog inactive")
                continue
            if state:
                seen_open = True
            elif seen_open:
                raise await self._loss_error("control connection closed")

    async def _health(self) -> None:
        """Raise ConnectionLostError when the HomePod stops answering.

        On a network drop the TCP connection is not closed and pyatv ignores
        feedback errors, so without this probe the stream would keep sending
        into the void.
        """
        failures = 0
        while True:
            await asyncio.sleep(PROBE_INTERVAL)
            if await self._reachable():
                failures = 0
                continue
            failures += 1
            _LOGGER.debug("HomePod reachability probe failed (%d/%d)", failures, PROBE_FAILURES)
            if failures >= PROBE_FAILURES:
                raise ConnectionLostError("Lost connection to the HomePod (it stopped responding).")

    async def _wait_restart(self, pipeline: LivePipeline) -> None:
        while not pipeline.restart_needed.is_set():
            await asyncio.sleep(0.5)

    async def _announce_when_flowing(self, source: LiveWavStream) -> None:
        """Call on_started once audio is actually flowing (SETUP/RECORD complete).

        pyatv reads ~0.5 s of prebuffer from the source even before SETUP, so the
        threshold is kept above that. If SETUP hangs, the threshold is never reached.
        """
        threshold = source.fmt.bytes_for_ms(1500)
        while source.total_out < threshold:
            await asyncio.sleep(0.2)
        self._ever_streamed = self._flowed_this_session = True
        _LOGGER.info("Live stream started -> %s", self.device.name)
        if self.on_started:
            self.on_started(self.pipeline.description if self.pipeline else "")

    # --------------------------------------------------------------------- run
    async def run(self, duration: float | None = None) -> None:
        """Connect and stream system audio, reconnecting after connection loss."""
        with StreamLock():
            deadline = time.monotonic() + duration if duration else None
            attempt = 0
            while True:
                try:
                    await self._session(deadline)
                    return
                except _FATAL_ERRORS:
                    raise
                except _RECOVERABLE_ERRORS as ex:
                    # The first connection must succeed on its own (e.g. a firewall
                    # problem should be reported, not retried forever).
                    if not (self.settings.auto_reconnect and self._ever_streamed):
                        raise
                    attempt = 1 if self._flowed_this_session else attempt + 1
                    if deadline is not None and time.monotonic() >= deadline:
                        return
                    delay = min(2.0 ** (attempt - 1), MAX_BACKOFF)
                    _LOGGER.warning("%s Reconnecting in %.0f s (attempt %d)", ex, delay, attempt)
                    if self.on_reconnecting:
                        self.on_reconnecting(attempt, delay, ex)
                    await asyncio.sleep(delay)
                    if attempt >= 2:
                        await self._refresh_device()

    async def _refresh_device(self) -> None:
        """Rescan in case the HomePod came back with a different IP address."""
        try:
            dev = await find_device(self.device.identifier, timeout=3)
        except Exception as ex:
            _LOGGER.debug("Rescan failed: %s", ex)
            return
        if dev and dev.address != self.device.address:
            _LOGGER.info("HomePod address changed: %s -> %s", self.device.address, dev.address)
        if dev:
            self.device = dev

    async def _session(self, deadline: float | None) -> None:
        """One connection: connect, stream (restarting the pipeline as needed), disconnect."""
        self._flowed_this_session = False
        self.atv = await connect(self.device)
        self.atv.audio.listener = _VolumeListener(self._on_device_volume)
        metadata = MediaMetadata(title="System audio", artist=socket.gethostname())
        try:
            if self.volume_level is not None:
                await self.atv.audio.set_volume(self.volume_level)
            while True:
                self.pipeline = LivePipeline(self.settings, self.on_source_changed)
                source = self.pipeline.start()
                play = asyncio.create_task(self.atv.stream.stream_file(source, metadata=metadata))
                monitors = {
                    asyncio.create_task(self._watchdog()),
                    asyncio.create_task(self._health()),
                    asyncio.create_task(self._wait_restart(self.pipeline)),
                }
                helpers = {
                    asyncio.create_task(self._report_stats(source)),
                    asyncio.create_task(self._announce_when_flowing(source)),
                }
                _LOGGER.info("Setting up HomePod session -> %s", self.device.name)
                try:
                    timeout = None if deadline is None else max(0.0, deadline - time.monotonic())
                    done, _ = await asyncio.wait({play, *monitors}, timeout=timeout,
                                                 return_when=asyncio.FIRST_COMPLETED)
                    if not done:
                        _LOGGER.info("Duration elapsed, stopping")
                        return
                    for task in done - {play}:
                        task.result()  # watchdog/health raise; restart waiter returns
                    if play not in done:
                        _LOGGER.info("Restarting the audio pipeline")
                        continue
                    play.result()  # raise on error
                    # A normal end is only expected when the WAV length limit is reached
                    if source.total_out < _WAV_DATA_SIZE * 0.95:
                        raise await self._loss_error("the stream ended unexpectedly")
                    _LOGGER.info("Stream reached the WAV length limit, restarting")
                finally:
                    for task in (*monitors, *helpers):
                        task.cancel()
                    self.pipeline.stop()  # releases pending read() calls
                    if not play.done():
                        play.cancel()
                    await asyncio.gather(play, *monitors, *helpers, return_exceptions=True)
        finally:
            self.atv.close()
            self.atv = None
            _LOGGER.info("Stream stopped")


async def stream_live(
    device: AirPlayDevice, settings: LiveSettings, volume: float | None = None, duration: float | None = None
) -> None:
    """Stream system audio live to the device (ends on Ctrl+C or after ``duration``)."""
    await LiveSession(device, settings, volume).run(duration)
