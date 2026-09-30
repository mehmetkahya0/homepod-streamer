"""System audio capture via WASAPI loopback, with optional ffmpeg resampling."""

from __future__ import annotations

import array
import logging
import shutil
import subprocess
import threading
from dataclasses import dataclass
from typing import Callable

import pyaudiowpatch as pyaudio

_LOGGER = logging.getLogger(__name__)

DataCallback = Callable[[bytes], None]


@dataclass(frozen=True)
class AudioFormat:
    """16-bit signed PCM format."""

    rate: int
    channels: int = 2
    sample_width: int = 2

    @property
    def frame_bytes(self) -> int:
        return self.channels * self.sample_width

    def bytes_for_ms(self, ms: float) -> int:
        """Frame-aligned byte count for the given duration."""
        return int(self.rate * ms / 1000) * self.frame_bytes


def list_loopback_devices() -> list[dict]:
    """Return all WASAPI loopback devices; the default one has ``is_default`` set to True."""
    pa = pyaudio.PyAudio()
    try:
        try:
            default_index = pa.get_default_wasapi_loopback()["index"]
        except (OSError, LookupError):
            default_index = None
        devices = list(pa.get_loopback_device_info_generator())
        for dev in devices:
            dev["is_default"] = dev["index"] == default_index
        return devices
    finally:
        pa.terminate()


def display_name(dev: dict) -> str:
    """'Speakers (Realtek(R) Audio) [Loopback]' -> 'Speakers (Realtek(R) Audio)'."""
    return dev["name"].removesuffix(" [Loopback]")


def _find_loopback(pa: pyaudio.PyAudio, name: str | None) -> dict:
    """Find a loopback device by name fragment, or the default output's loopback."""
    if name:
        for dev in pa.get_loopback_device_info_generator():
            if name.lower() in dev["name"].lower():
                return dev
        raise LookupError(f"no loopback device matching '{name}'")
    return pa.get_default_wasapi_loopback()


class LoopbackCapture:
    """Capture the default (or named) output device as stereo s16le.

    Data is delivered via ``on_data`` on PortAudio's own thread. Each ``start()``
    opens a fresh PyAudio instance so the device list is up to date.
    """

    def __init__(self, on_data: DataCallback, chunk_ms: int = 20, device_name: str | None = None) -> None:
        self.on_data = on_data
        self.chunk_ms = chunk_ms
        self.device_name = device_name
        self.device: dict | None = None
        self._pa: pyaudio.PyAudio | None = None
        self._stream = None
        self._in_channels = 2

    def start(self) -> AudioFormat:
        """Start capturing and return the output format (always stereo)."""
        self._pa = pyaudio.PyAudio()
        try:
            dev = _find_loopback(self._pa, self.device_name)
            rate = int(dev["defaultSampleRate"])
            self._in_channels = int(dev["maxInputChannels"])
            self._stream = self._pa.open(
                format=pyaudio.paInt16,
                channels=self._in_channels,
                rate=rate,
                input=True,
                input_device_index=dev["index"],
                frames_per_buffer=int(rate * self.chunk_ms / 1000),
                stream_callback=self._callback,
            )
        except Exception:
            self.stop()
            raise
        self.device = dev
        _LOGGER.info("Capturing: %s (%d Hz, %d channels)", dev["name"], rate, self._in_channels)
        if self._in_channels > 2:
            _LOGGER.warning("%d-channel device: only front left/right will be sent", self._in_channels)
        return AudioFormat(rate=rate)

    def _callback(self, in_data: bytes, frame_count: int, time_info: dict, status: int):
        if status:
            _LOGGER.debug("PortAudio status flag: %s", status)
        if self._in_channels > 2:
            src = array.array("h", in_data)
            out = array.array("h", bytes(frame_count * 4))
            out[0::2] = src[0 :: self._in_channels]
            out[1::2] = src[1 :: self._in_channels]
            in_data = out.tobytes()
        elif self._in_channels == 1:
            src = array.array("h", in_data)
            out = array.array("h", bytes(frame_count * 4))
            out[0::2] = src
            out[1::2] = src
            in_data = out.tobytes()
        try:
            self.on_data(in_data)
        except Exception:  # log so it isn't swallowed on the callback thread
            _LOGGER.exception("Failed to process captured audio")
        return None, pyaudio.paContinue

    @property
    def active(self) -> bool:
        return bool(self._stream and self._stream.is_active())

    def stop(self) -> None:
        """Close the stream and release PortAudio."""
        if self._stream is not None:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except OSError as ex:
                _LOGGER.debug("Error while closing stream: %s", ex)
            self._stream = None
        if self._pa is not None:
            self._pa.terminate()
            self._pa = None


class FfmpegResampler:
    """Resample PCM to the target rate through ffmpeg (soxr)."""

    def __init__(self, in_fmt: AudioFormat, out_rate: int, on_data: DataCallback, chunk_ms: int = 20) -> None:
        self.in_fmt = in_fmt
        self.out_fmt = AudioFormat(rate=out_rate)
        self.on_data = on_data
        self._chunk = self.out_fmt.bytes_for_ms(chunk_ms)
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> AudioFormat:
        exe = shutil.which("ffmpeg")
        if not exe:
            raise FileNotFoundError("ffmpeg not found in PATH (use --resampler miniaudio)")
        cmd = [
            exe, "-hide_banner", "-loglevel", "error", "-fflags", "nobuffer",
            "-f", "s16le", "-ar", str(self.in_fmt.rate), "-ac", "2", "-i", "pipe:0",
            "-af", "aresample=resampler=soxr", "-ar", str(self.out_fmt.rate), "-ac", "2",
            "-f", "s16le", "-flush_packets", "1", "pipe:1",
        ]
        _LOGGER.debug("ffmpeg: %s", " ".join(cmd))
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self._thread = threading.Thread(target=self._pump, args=(self._proc,), name="ffmpeg-out", daemon=True)
        self._thread.start()
        _LOGGER.info("Resampling: ffmpeg/soxr %d -> %d Hz", self.in_fmt.rate, self.out_fmt.rate)
        return self.out_fmt

    def write(self, data: bytes) -> None:
        """Write captured PCM to ffmpeg (called from the capture thread)."""
        proc = self._proc
        if proc and proc.stdin:
            try:
                proc.stdin.write(data)
            except (BrokenPipeError, OSError, ValueError):
                pass

    def _pump(self, proc: subprocess.Popen) -> None:
        assert proc.stdout
        while chunk := proc.stdout.read(self._chunk):
            self.on_data(chunk)
        _LOGGER.debug("ffmpeg output closed")

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
            proc.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            proc.kill()
        if self._thread:
            self._thread.join(timeout=2)
