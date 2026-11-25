"""Find nearby Bluetooth devices to track with Bermuda.

Three kinds of devices can be tracked:

* **iBeacon**: a fixed identifier (UUID, major, minor), for example the
  *BLE Transmitter* sensor of the Home Assistant Android app, or a tag.
  Bermuda tracks it as ``<uuid>_<major>_<minor>`` whatever MAC it uses.
* **Fixed address**: devices that keep the same MAC (many tags, watches,
  bands, some earbuds while their case is open).

The user adds iBeacons and addresses in Bermuda's own options (BLE Radar only
shows what to add); phones by IRK go through Private BLE Device's config flow.
* **Phones with a private address** (iPhone, most Android phones) change MAC
  every few minutes: they can only be followed with their IRK, through the
  Private BLE Device integration, which Bermuda picks up by itself.
"""
from __future__ import annotations

import base64
import binascii
import re
import time
from dataclasses import dataclass
from typing import Any

from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

BERMUDA_DOMAIN = "bermuda"
BERMUDA_DEVICES = "configured_devices"
PRIVATE_BLE_DOMAIN = "private_ble_device"

APPLE = 0x004C
# Seconds since the last advertisement for a device to be "nearby"
NEARBY_SECONDS = 300

MANUFACTURERS = {
    0x0006: "Microsoft",
    0x004C: "Apple",
    0x0059: "Nordic",
    0x0075: "Samsung",
    0x0087: "Garmin",
    0x00E0: "Google",
    0x012D: "Sony",
    0x0157: "Huami / Amazfit",
    0x027D: "Huawei",
    0x038F: "Xiaomi",
    0x0499: "Ruuvi",
    0x05A7: "Sonos",
    0x0822: "Bose",
}

KIND_IBEACON = "ibeacon"
KIND_ADDRESS = "address"
KIND_PRIVATE = "private"



@dataclass
class Advert:
    """What a scanner heard from one address."""

    address: str
    name: str | None
    rssi: int
    age: float  # seconds since last heard
    manufacturer_data: dict[int, bytes]


def ibeacon_id(manufacturer_data: dict[int, bytes]) -> tuple[str, str, int, int] | None:
    """Bermuda id, UUID, major and minor of an iBeacon advertisement."""
    data = manufacturer_data.get(APPLE)
    if not data or len(data) < 22 or data[0] != 0x02 or data[1] != 0x15:
        return None
    uuid = data[2:18].hex()
    major = int.from_bytes(data[18:20], "big")
    minor = int.from_bytes(data[20:22], "big")
    pretty = f"{uuid[:8]}-{uuid[8:12]}-{uuid[12:16]}-{uuid[16:20]}-{uuid[20:]}"
    return f"{uuid}_{major}_{minor}".upper(), pretty, major, minor


def is_private_address(address: str) -> bool:
    """Resolvable private address: the two top bits are 01 (it rotates)."""
    try:
        return int(address[:2], 16) >> 6 == 0b01
    except ValueError:
        return False


def classify(adverts: list[Advert], tracked: set[str]) -> dict[str, Any]:
    """Nearby devices that can be tracked, closest first.

    Devices with a private address are only counted: their MAC changes
    within minutes, an IRK is needed to follow them.
    """
    found: dict[str, dict[str, Any]] = {}
    private = 0
    for advert in adverts:
        if advert.age > NEARBY_SECONDS:
            continue
        address = advert.address.upper()
        manufacturer = next(
            (MANUFACTURERS[m] for m in advert.manufacturer_data if m in MANUFACTURERS),
            None,
        )
        beacon = ibeacon_id(advert.manufacturer_data)
        if beacon is not None:
            key, uuid, major, minor = beacon
            item = {
                "kind": KIND_IBEACON,
                "id": key,
                "name": advert.name or "iBeacon",
                "detail": f"{uuid} · {major} / {minor}",
            }
        elif is_private_address(address):
            private += 1
            continue
        else:
            key = address
            item = {
                "kind": KIND_ADDRESS,
                "id": address,
                "name": advert.name or manufacturer or address,
                "detail": " · ".join(x for x in (address, manufacturer) if x),
            }
        item.update(rssi=advert.rssi, age=round(advert.age), tracked=key in tracked)
        if key not in found or advert.rssi > found[key]["rssi"]:
            found[key] = item
    devices = sorted(
        found.values(),
        key=lambda d: (d["tracked"], d["kind"] != KIND_IBEACON, -d["rssi"]),
    )
    return {"devices": devices, "private": private}


# ---------------------------------------------------------------------------
# Home Assistant side
# ---------------------------------------------------------------------------


def bermuda_entry(hass: HomeAssistant):
    """The loaded Bermuda config entry, if any."""
    return next(
        (
            e
            for e in hass.config_entries.async_entries(BERMUDA_DOMAIN)
            if e.state is ConfigEntryState.LOADED
        ),
        None,
    )


def bermuda_tracked(hass: HomeAssistant) -> set[str]:
    entry = bermuda_entry(hass)
    if entry is None:
        return set()
    return {str(a).upper() for a in entry.options.get(BERMUDA_DEVICES) or []}


def nearby(hass: HomeAssistant) -> dict[str, Any]:
    """Nearby devices heard by Home Assistant's Bluetooth scanners."""
    result: dict[str, Any] = {
        "bluetooth": "bluetooth" in hass.config.components,
        "bermuda": bermuda_entry(hass) is not None,
        "devices": [],
        "private": 0,
    }
    if not result["bluetooth"]:
        return result
    now = time.monotonic()
    adverts = [
        Advert(
            address=info.address,
            name=info.name if info.name and info.name != info.address else None,
            rssi=info.rssi,
            age=max(0.0, now - info.time),
            manufacturer_data=dict(info.manufacturer_data),
        )
        for info in _service_infos(hass)
    ]
    result.update(classify(adverts, bermuda_tracked(hass)))
    return result


def _service_infos(hass: HomeAssistant) -> list[Any]:
    """Latest advertisement of every address heard by the scanners."""
    # pylint: disable-next=import-outside-toplevel
    from homeassistant.components import bluetooth

    return list(bluetooth.async_discovered_service_info(hass, connectable=False))


class TrackError(Exception):
    """Cannot start tracking; ``code`` is shown translated by the page."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def normalize_irk(irk: str) -> str | None:
    """IRK as 32 hex digits, from hex (with or without separators) or base64."""
    text = irk.strip().removeprefix("irk:")
    if text.endswith("="):
        try:
            # base64 IRKs (as exported by Apple) are in reversed byte order
            raw = bytes(reversed(base64.b64decode(text)))
        except binascii.Error:
            return None
    else:
        text = re.sub(r"[\s:\-]", "", text)
        try:
            raw = binascii.unhexlify(text)
        except binascii.Error:
            return None
    return raw.hex() if len(raw) == 16 else None


async def private_ble_add(hass: HomeAssistant, irk: str) -> str:
    """Create a Private BLE Device entry for the IRK; returns its title.

    Bermuda tracks Private BLE Devices by itself.
    """
    if (key := normalize_irk(irk)) is None:
        raise TrackError("irk_not_valid")
    result = await hass.config_entries.flow.async_init(
        PRIVATE_BLE_DOMAIN, context={"source": SOURCE_USER}, data={"irk": key}
    )
    if result["type"] == FlowResultType.CREATE_ENTRY:
        return result["title"]
    if result["type"] == FlowResultType.ABORT:
        raise TrackError(result["reason"])
    # form shown again: an error on the IRK (not valid / not seen nearby)
    hass.config_entries.flow.async_abort(result["flow_id"])
    errors = result.get("errors") or {}
    raise TrackError(next(iter(errors.values()), "unknown"))
