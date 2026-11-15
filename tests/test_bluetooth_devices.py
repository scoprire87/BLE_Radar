"""Finding nearby Bluetooth devices and tracking them with Bermuda."""
from __future__ import annotations

import base64
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ble_fingerprint.bluetooth_devices import (
    Advert,
    TrackError,
    bermuda_add,
    classify,
    ibeacon_id,
    normalize_irk,
)
from custom_components.ble_fingerprint.const import DOMAIN

UUID = bytes.fromhex("fda50693a4e24fb1afcfc6eb07647825")
BEACON = bytes([0x02, 0x15]) + UUID + (100).to_bytes(2, "big") + (1).to_bytes(2, "big") + b"\xc5"
BEACON_ID = "FDA50693A4E24FB1AFCFC6EB07647825_100_1"


def _advert(address, rssi=-60, age=5, name=None, data=None):
    return Advert(address, name, rssi, age, data or {})


def test_ibeacon_id():
    key, uuid, major, minor = ibeacon_id({0x004C: BEACON})
    assert key == BEACON_ID
    assert uuid == "fda50693-a4e2-4fb1-afcf-c6eb07647825" and (major, minor) == (100, 1)
    assert ibeacon_id({0x004C: b"\x10\x05\x01"}) is None  # Apple "nearby", not a beacon
    assert ibeacon_id({0x0075: BEACON}) is None


def test_classify_nearby():
    result = classify(
        [
            # the phone app beacon heard from two rotating MACs: one entry
            _advert("5A:11:22:33:44:55", rssi=-80, data={0x004C: BEACON}),
            _advert("6B:11:22:33:44:55", rssi=-55, data={0x004C: BEACON}),
            # earbuds case with a fixed address
            _advert("C4:30:18:AA:BB:CC", rssi=-70, name="Buds", data={0x0075: b"\x01"}),
            # a tag already tracked
            _advert("D0:00:00:00:00:01", rssi=-40),
            # phones with private addresses: only counted
            _advert("4F:12:34:56:78:9A", data={0x004C: b"\x10\x05"}),
            _advert("71:12:34:56:78:9A"),
            # not heard for a while
            _advert("C0:00:00:00:00:02", age=900),
        ],
        tracked={"D0:00:00:00:00:01"},
    )
    devices = result["devices"]
    assert [d["id"] for d in devices] == [BEACON_ID, "C4:30:18:AA:BB:CC", "D0:00:00:00:00:01"]
    assert devices[0]["kind"] == "ibeacon" and devices[0]["rssi"] == -55
    assert devices[1] == {
        "kind": "address", "id": "C4:30:18:AA:BB:CC", "name": "Buds",
        "detail": "C4:30:18:AA:BB:CC · Samsung", "rssi": -70, "age": 5, "tracked": False,
    }
    assert devices[2]["tracked"] is True
    assert result["private"] == 2


def test_normalize_irk():
    key = "0123456789abcdef0123456789abcdef"
    assert normalize_irk(key.upper()) == key
    assert normalize_irk("01:23:45:67:89:AB:CD:EF:01:23:45:67:89:AB:CD:EF") == key
    assert normalize_irk(f"irk:{key}") == key
    # base64, as exported by Apple, is in reversed byte order
    encoded = base64.b64encode(bytes(reversed(bytes.fromhex(key)))).decode()
    assert normalize_irk(encoded) == key
    assert normalize_irk("1234") is None and normalize_irk("zz" * 16) is None


def _bermuda(hass, devices=None):
    entry = MockConfigEntry(
        domain="bermuda", options={"configured_devices": devices or []},
        state=ConfigEntryState.LOADED,
    )
    entry.add_to_hass(hass)
    return entry


async def test_bermuda_add(hass):
    with pytest.raises(TrackError) as err:
        bermuda_add(hass, "C4:30:18:AA:BB:CC")
    assert err.value.code == "no_bermuda"

    entry = _bermuda(hass, ["D0:00:00:00:00:01"])
    bermuda_add(hass, "c4:30:18:aa:bb:cc")
    bermuda_add(hass, BEACON_ID.lower())
    bermuda_add(hass, "C4:30:18:AA:BB:CC")  # already there
    assert entry.options["configured_devices"] == [
        "D0:00:00:00:00:01", "C4:30:18:AA:BB:CC", BEACON_ID,
    ]
    with pytest.raises(TrackError) as err:
        bermuda_add(hass, "kitchen")
    assert err.value.code == "invalid_id"


async def _ws(client, command, **data):
    await client.send_json_auto_id({"type": f"ble_fingerprint/{command}", **data})
    return await client.receive_json()


async def test_websocket_nearby_and_track(hass, hass_ws_client):
    entry = MockConfigEntry(domain=DOMAIN, version=3, title="BLE Radar", options={"trackers": []})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    client = await hass_ws_client(hass)

    resp = await _ws(client, "nearby")
    assert resp["result"] == {"bluetooth": False, "bermuda": False, "devices": [], "private": 0}

    bermuda = _bermuda(hass)
    hass.config.components.add("bluetooth")
    now = time.monotonic()
    infos = [
        SimpleNamespace(address="C4:30:18:AA:BB:CC", name="Buds", rssi=-62, time=now - 3,
                        manufacturer_data={}),
        SimpleNamespace(address="6B:11:22:33:44:55", name="6B:11:22:33:44:55", rssi=-50,
                        time=now - 1, manufacturer_data={0x004C: BEACON}),
    ]
    with patch(
        "custom_components.ble_fingerprint.bluetooth_devices._service_infos", return_value=infos
    ):
        resp = await _ws(client, "nearby")
    result = resp["result"]
    assert result["bluetooth"] and result["bermuda"]
    assert [d["id"] for d in result["devices"]] == [BEACON_ID, "C4:30:18:AA:BB:CC"]
    assert result["devices"][0]["name"] == "iBeacon"

    resp = await _ws(client, "track_bluetooth", kind="address", device="C4:30:18:AA:BB:CC")
    assert resp["success"]
    assert bermuda.options["configured_devices"] == ["C4:30:18:AA:BB:CC"]

    # phones by IRK go through Private BLE Device
    key = "0123456789abcdef0123456789abcdef"
    calls = []

    async def flow_init(domain, context=None, data=None):
        calls.append((domain, data))
        if data["irk"] == key:
            return {"type": FlowResultType.CREATE_ENTRY, "title": "Pixel"}
        return {"type": FlowResultType.FORM, "flow_id": "x", "errors": {"irk": "irk_not_found"}}

    with patch.object(hass.config_entries.flow, "async_init", side_effect=flow_init), \
            patch.object(hass.config_entries.flow, "async_abort"):
        resp = await _ws(client, "track_bluetooth", kind="irk", device=key.upper())
        assert resp["success"] and resp["result"] == {"title": "Pixel"}
        resp = await _ws(client, "track_bluetooth", kind="irk", device="ff" * 16)
        assert resp["error"]["code"] == "irk_not_found"
        resp = await _ws(client, "track_bluetooth", kind="irk", device="nope")
        assert resp["error"]["code"] == "irk_not_valid"
    assert calls == [("private_ble_device", {"irk": key}), ("private_ble_device", {"irk": "ff" * 16})]
