"""AirPlay (RAOP) device discovery."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import pyatv
from pyatv.const import PairingRequirement, Protocol
from pyatv.interface import BaseConfig

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class AirPlayDevice:
    """Summary of a discovered AirPlay/RAOP device."""

    name: str
    address: str
    identifier: str
    model: str
    pairing: str
    requires_password: bool
    config: BaseConfig

    def __str__(self) -> str:
        return f"{self.name} ({self.model}) @ {self.address}"


async def scan_devices(timeout: int = 5, host: str | None = None) -> list[AirPlayDevice]:
    """Scan the network for RAOP-capable devices, sorted by name.

    If ``host`` is given, that IP is queried directly via unicast instead of
    multicast (useful on networks where mDNS is blocked).
    """
    loop = asyncio.get_running_loop()
    _LOGGER.debug("Starting scan (timeout=%ss, host=%s)", timeout, host)
    configs = await pyatv.scan(
        loop,
        timeout=timeout,
        protocol=Protocol.RAOP,
        hosts=[host] if host else None,
    )

    devices: list[AirPlayDevice] = []
    for conf in configs:
        raop = conf.get_service(Protocol.RAOP)
        if raop is None:
            continue
        _LOGGER.debug("Found: %s %s props=%s", conf.name, conf.address, raop.properties)
        devices.append(
            AirPlayDevice(
                name=conf.name,
                address=str(conf.address),
                identifier=conf.identifier or str(conf.address),
                model=conf.device_info.model_str,
                pairing=raop.pairing.name,
                requires_password=raop.requires_password,
                config=conf,
            )
        )
    devices.sort(key=lambda d: d.name.lower())
    return devices


async def find_device(identifier: str, timeout: int = 5) -> AirPlayDevice | None:
    """Find a single device by identifier, name or IP address."""
    for dev in await scan_devices(timeout):
        if identifier in (dev.identifier, dev.address) or identifier.lower() == dev.name.lower():
            return dev
    return None


def pairing_blocked(dev: AirPlayDevice) -> bool:
    """True if the device reports an access restriction pyatv does not support."""
    return dev.pairing in (PairingRequirement.Disabled.name, PairingRequirement.Unsupported.name)


def choose_device(devices: list[AirPlayDevice]) -> AirPlayDevice:
    """Print the device list to the console and let the user pick one."""
    for i, dev in enumerate(devices, 1):
        print(f"  [{i}] {dev}")
    while True:
        raw = input(f"Select a device (1-{len(devices)}): ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(devices):
            return devices[int(raw) - 1]
        print("Invalid selection.")
