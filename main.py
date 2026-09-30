"""HomePod Streamer entry point: GUI with no arguments, CLI with subcommands."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from capture import list_loopback_devices
from config import load_config, update_config
from discovery import AirPlayDevice, choose_device, scan_devices
from streamer import LiveSettings, play_file, stream_live

_LOGGER = logging.getLogger("homepod")


async def resolve_device(args: argparse.Namespace) -> AirPlayDevice | None:
    """Pick the device via --device, the last device in config.json, or interactively."""
    devices = await scan_devices(args.timeout, args.host)
    if not devices:
        _LOGGER.error("No AirPlay devices found. Are you on the same network? The firewall may be "
                      "blocking mDNS (UDP 5353); try --host <IP>.")
        return None

    wanted = args.device or load_config().get("last_device")
    if wanted:
        for dev in devices:
            if wanted in (dev.identifier, dev.address) or wanted.lower() == dev.name.lower():
                return dev
        _LOGGER.warning("'%s' not found, pick one from the list.", wanted)

    dev = devices[0] if len(devices) == 1 else choose_device(devices)
    update_config(last_device=dev.identifier)
    return dev


async def cmd_scan(args: argparse.Namespace) -> int:
    """Stage 1: list devices."""
    devices = await scan_devices(args.timeout, args.host)
    if not devices:
        print("No devices found.")
        return 1
    print(f"{'Name':<24} {'IP':<16} {'Model':<16} {'Pairing':<14} Identifier")
    for d in devices:
        print(f"{d.name:<24} {d.address:<16} {d.model:<16} {d.pairing:<14} {d.identifier}")
    return 0


async def cmd_play(args: argparse.Namespace) -> int:
    """Stage 2: play a local file on the selected device."""
    dev = await resolve_device(args)
    if dev is None:
        return 1
    try:
        await play_file(dev, args.file, args.volume)
    except FileNotFoundError as ex:
        _LOGGER.error("File not found: %s", ex)
        return 1
    except Exception as ex:  # already explained in streamer; log type + trace here
        _LOGGER.debug("Details", exc_info=True)
        _LOGGER.error("Playback failed: %s: %s", type(ex).__name__, ex)
        return 1
    return 0


async def cmd_loopbacks(args: argparse.Namespace) -> int:
    """List WASAPI loopback devices."""
    for dev in list_loopback_devices():
        print(f"[{dev['index']:>3}] {dev['name']}  ({int(dev['defaultSampleRate'])} Hz, {dev['maxInputChannels']} channels)")
    return 0


async def cmd_live(args: argparse.Namespace) -> int:
    """Stage 3: stream system audio live."""
    dev = await resolve_device(args)
    if dev is None:
        return 1
    settings = LiveSettings(
        loopback=args.loopback,
        chunk_ms=args.chunk_ms,
        prebuffer_ms=args.prebuffer_ms,
        max_buffer_ms=args.max_buffer_ms,
        resampler=args.resampler,
        stats_interval=args.stats_interval,
    )
    try:
        await stream_live(dev, settings, args.volume, args.duration)
    except Exception as ex:
        _LOGGER.debug("Details", exc_info=True)
        _LOGGER.error("Live stream failed: %s: %s", type(ex).__name__, ex)
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="homepod-streamer", description="Stream Windows audio to a HomePod via AirPlay")
    p.add_argument("--debug", action="store_true", help="verbose logging")
    p.add_argument("--timeout", type=int, default=5, help="scan duration (s)")
    p.add_argument("--host", help="query this IP directly instead of mDNS")
    sub = p.add_subparsers(dest="command")
    sub.add_parser("gui", help="open the graphical interface (default)")

    sub.add_parser("scan", help="list AirPlay devices")

    play = sub.add_parser("play", help="play a local audio file")
    play.add_argument("file", help="audio file path (wav/mp3/flac/ogg) or http(s) URL")
    play.add_argument("--device", help="device name, IP or identifier (default: last used)")
    play.add_argument("--volume", type=float, help="volume 0-100")

    sub.add_parser("loopbacks", help="list WASAPI loopback (capture) devices")

    live = sub.add_parser("live", help="stream system audio live")
    live.add_argument("--device", help="device name, IP or identifier (default: last used)")
    live.add_argument("--volume", type=float, help="volume 0-100")
    live.add_argument("--loopback", help="name fragment of the output device to capture (default: system default)")
    live.add_argument("--chunk-ms", type=int, default=20, help="capture period in ms (default 20)")
    live.add_argument("--prebuffer-ms", type=int, default=100, help="jitter buffer in ms (default 100); increase if you hear crackling")
    live.add_argument("--max-buffer-ms", type=int, default=300, help="latency cap in ms; the queue is trimmed above this (default 300)")
    live.add_argument("--resampler", choices=["miniaudio", "ffmpeg"], default=load_config()["resampler"],
                      help="44.1 kHz resampler: miniaudio (built in) or ffmpeg (soxr, higher quality)")
    live.add_argument("--stats-interval", type=float, default=10.0, help="stats log interval in s")
    live.add_argument("--duration", type=float, help="stop automatically after N seconds (for testing)")
    return p


def main() -> int:
    args = build_parser().parse_args()
    if args.command in (None, "gui"):
        from gui import run_gui

        return run_gui(debug=args.debug)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    if not args.debug:
        logging.getLogger("pyatv").setLevel(logging.WARNING)

    handler = {"scan": cmd_scan, "play": cmd_play, "loopbacks": cmd_loopbacks, "live": cmd_live}[args.command]
    try:
        return asyncio.run(handler(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
