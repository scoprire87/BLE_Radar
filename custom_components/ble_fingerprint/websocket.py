"""API WebSocket per la pagina laterale e la card."""
from __future__ import annotations

import secrets
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    entity_registry as er,
)

from .const import (
    CONF_DEVICE,
    CONF_NAME,
    CONF_SENSORS,
    CONF_TRACKERS,
    DOMAIN,
    GUIDED_CORNER_INSET_M,
    TRACKER_ID,
    TRACKER_UNIQUE_ID,
)
from .fingerprint import proxy_key
from .geometry import guided_points
from .mapstore import get_map_store

BERMUDA_DOMAIN = "bermuda"

_POINT = vol.All(
    vol.ExactSequence([vol.Coerce(float), vol.Coerce(float)]),
    lambda p: [round(p[0], 1), round(p[1], 1)],
)
_NAME = vol.All(cv.string, vol.Strip, vol.Length(min=1, max=60))


@callback
def async_setup_websocket(hass: HomeAssistant) -> None:
    """Registra i comandi WebSocket."""
    for command in (
        ws_get_config,
        ws_get_floor_image,
        ws_save_floor,
        ws_delete_floor,
        ws_save_zone,
        ws_delete_zone,
        ws_set_proxy,
        ws_get_devices,
        ws_save_trackers,
        ws_guided_points,
    ):
        websocket_api.async_register_command(hass, command)


def _entries(hass: HomeAssistant):
    return hass.config_entries.async_entries(DOMAIN)


def _trackers_info(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Dispositivi tracciati con il relativo sensore di posizione."""
    entities = hass.data.get(DOMAIN, {}).get("entities", {})
    result = []
    for entry in _entries(hass):
        for tracker in entry.options.get(CONF_TRACKERS) or []:
            sensor = entities.get(tracker.get(TRACKER_ID))
            result.append(
                {
                    "id": tracker.get(TRACKER_ID),
                    "name": tracker.get(CONF_NAME),
                    "device_id": tracker.get(CONF_DEVICE),
                    "sensors": tracker.get(CONF_SENSORS) or [],
                    "entity_id": sensor.entity_id if sensor else None,
                    "proxy_sensors": sensor.proxy_sensors if sensor else [],
                }
            )
    return result


def _estimated_proxies(hass: HomeAssistant) -> dict[str, dict[str, Any]]:
    """Proxy stimati per piano, uniti tra i sensori attivi."""
    merged: dict[str, dict[str, Any]] = {}
    for sensor in hass.data.get(DOMAIN, {}).get("entities", {}).values():
        for floor, proxies in sensor.proxy_diagnostics.items():
            target = merged.setdefault(floor, {})
            for proxy, data in proxies.items():
                # a parità tiene la stima basata su più campioni
                if proxy not in target or data["samples"] > target[proxy]["samples"]:
                    target[proxy] = data
    return merged


@websocket_api.websocket_command({vol.Required("type"): "ble_fingerprint/config"})
@callback
def ws_get_config(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Mappa completa, proxy stimati e dispositivi tracciati."""
    map_store = get_map_store(hass)
    data = map_store.snapshot()
    # per il pannello bastano coordinate e numero di proxy dei vertici
    data["rooms"] = {
        room: {
            vertex_id: {
                "coords": vertex.get("coords") or {},
                "floor": vertex.get("floor"),
                "proxies": len(vertex.get("distances") or {}),
            }
            for vertex_id, vertex in vertices.items()
        }
        for room, vertices in map_store.rooms.items()
    }
    data["estimated_proxies"] = _estimated_proxies(hass)
    data["trackers"] = _trackers_info(hass)
    data["inset_m"] = GUIDED_CORNER_INSET_M
    connection.send_result(msg["id"], data)


@websocket_api.websocket_command(
    {vol.Required("type"): "ble_fingerprint/floor_image", vol.Required("floor_id"): str}
)
@websocket_api.async_response
async def ws_get_floor_image(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Planimetria del piano come data URL."""
    image = await get_map_store(hass).async_get_floor_image(msg["floor_id"])
    connection.send_result(msg["id"], {"image": image})


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "ble_fingerprint/save_floor",
        vol.Optional("floor_id"): vol.Any(None, str),
        vol.Required("name"): _NAME,
        vol.Optional("scale"): vol.Any(None, vol.All(vol.Coerce(float), vol.Range(min=0.01))),
        vol.Optional("image"): str,
        vol.Optional("width"): vol.All(vol.Coerce(int), vol.Range(min=1)),
        vol.Optional("height"): vol.All(vol.Coerce(int), vol.Range(min=1)),
    }
)
@websocket_api.async_response
async def ws_save_floor(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Crea o modifica un piano (nome, planimetria, scala)."""
    map_store = get_map_store(hass)
    floor_id = msg.get("floor_id") or map_store.new_floor_id()
    floor = dict(map_store.floors.get(floor_id, {}))
    floor["name"] = msg["name"]

    if "scale" in msg:
        floor["scale"] = msg["scale"]
    if image := msg.get("image"):
        try:
            floor["image"] = await map_store.async_set_floor_image(floor_id, image)
        except ValueError as err:
            connection.send_error(msg["id"], "invalid_image", str(err))
            return
        floor["width"] = msg.get("width")
        floor["height"] = msg.get("height")

    map_store.floors[floor_id] = floor
    await map_store.async_save()
    connection.send_result(msg["id"], {"floor_id": floor_id, "floor": floor})


@websocket_api.require_admin
@websocket_api.websocket_command(
    {vol.Required("type"): "ble_fingerprint/delete_floor", vol.Required("floor_id"): str}
)
@websocket_api.async_response
async def ws_delete_floor(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Elimina un piano con stanze, punti registrati e proxy manuali."""
    map_store = get_map_store(hass)
    if msg["floor_id"] not in map_store.floors:
        connection.send_error(msg["id"], "not_found", "Floor not found")
        return
    await map_store.async_delete_floor(msg["floor_id"])
    await map_store.async_save()
    connection.send_result(msg["id"])


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "ble_fingerprint/save_zone",
        vol.Required("name"): _NAME,
        vol.Optional("old_name"): vol.Any(None, str),
        vol.Required("floor_id"): str,
        vol.Required("polygon"): vol.All([_POINT], vol.Length(min=3, max=64)),
    }
)
@websocket_api.async_response
async def ws_save_zone(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Crea, modifica o rinomina una stanza disegnata."""
    map_store = get_map_store(hass)
    name, old_name = msg["name"], msg.get("old_name")
    if msg["floor_id"] not in map_store.floors:
        connection.send_error(msg["id"], "not_found", "Floor not found")
        return
    try:
        if old_name and old_name != name:
            map_store.rename_room(old_name, name)
        elif not old_name and name in map_store.zones:
            raise ValueError(f"A room called {name} already exists")
    except ValueError as err:
        connection.send_error(msg["id"], "invalid_name", str(err))
        return

    map_store.zones[name] = {"floor": msg["floor_id"], "polygon": msg["polygon"]}
    # i punti già registrati della stanza seguono il piano della stanza
    for vertex in map_store.rooms.get(name, {}).values():
        vertex["floor"] = msg["floor_id"]
    await map_store.async_save()
    connection.send_result(msg["id"])


@websocket_api.require_admin
@websocket_api.websocket_command(
    {vol.Required("type"): "ble_fingerprint/delete_zone", vol.Required("name"): str}
)
@websocket_api.async_response
async def ws_delete_zone(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Elimina una stanza (poligono e punti registrati)."""
    map_store = get_map_store(hass)
    if not map_store.delete_room(msg["name"]):
        connection.send_error(msg["id"], "not_found", "Room not found")
        return
    await map_store.async_save()
    connection.send_result(msg["id"])


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "ble_fingerprint/set_proxy",
        vol.Required("proxy"): str,
        vol.Optional("floor_id"): vol.Any(None, str),
        vol.Optional("x"): vol.Coerce(float),
        vol.Optional("y"): vol.Coerce(float),
        vol.Optional("reset", default=False): bool,
    }
)
@websocket_api.async_response
async def ws_set_proxy(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Corregge (o riporta alla stima) la posizione di un proxy."""
    map_store = get_map_store(hass)
    proxy = proxy_key(msg["proxy"])
    if msg["reset"]:
        map_store.proxies.pop(proxy, None)
    elif "x" in msg and "y" in msg:
        override: dict[str, Any] = {"x": round(msg["x"], 1), "y": round(msg["y"], 1)}
        if msg.get("floor_id"):
            override["floor"] = msg["floor_id"]
        if factor := map_store.proxies.get(proxy, {}).get("factor"):
            override["factor"] = factor
        map_store.proxies[proxy] = override
    else:
        connection.send_error(msg["id"], "invalid_format", "Set x and y, or reset")
        return
    await map_store.async_save()
    connection.send_result(msg["id"])


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "ble_fingerprint/devices"})
@callback
def ws_get_devices(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Dispositivi che hanno sensori di distanza (prima quelli di Bermuda)."""
    # pylint: disable-next=import-outside-toplevel
    from .sensor import is_distance_sensor

    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    devices: dict[str, dict[str, Any]] = {}
    for entity in entity_registry.entities.values():
        if entity.device_id is None or entity.disabled_by or not is_distance_sensor(entity):
            continue
        device = devices.get(entity.device_id)
        if device is None:
            dev = device_registry.async_get(entity.device_id)
            if dev is None:
                continue
            device = devices[entity.device_id] = {
                "device_id": entity.device_id,
                "name": dev.name_by_user or dev.name or entity.device_id,
                "bermuda": entity.platform == BERMUDA_DOMAIN,
                "sensors": [],
            }
        device["sensors"].append(entity.entity_id)

    result = sorted(
        devices.values(), key=lambda d: (not d["bermuda"], d["name"].lower())
    )
    connection.send_result(msg["id"], {"devices": result})


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "ble_fingerprint/save_trackers",
        vol.Required("trackers"): [
            vol.Schema(
                {
                    vol.Optional("id"): vol.Any(None, str),
                    vol.Required("name"): _NAME,
                    vol.Optional("device_id"): vol.Any(None, str),
                    vol.Optional("sensors", default=[]): [cv.entity_id],
                }
            )
        ],
    }
)
@callback
def ws_save_trackers(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Salva l'elenco dei dispositivi tracciati (ricarica l'integrazione)."""
    entries = _entries(hass)
    if not entries:
        connection.send_error(msg["id"], "not_found", "The integration is not set up")
        return

    existing: dict[str, tuple[Any, dict[str, Any]]] = {
        tracker.get(TRACKER_ID): (entry, tracker)
        for entry in entries
        for tracker in entry.options.get(CONF_TRACKERS) or []
    }
    new_by_entry: dict[str, list[dict[str, Any]]] = {e.entry_id: [] for e in entries}

    for item in msg["trackers"]:
        tracker_id = item.get("id")
        entry, old = existing.get(tracker_id, (entries[0], {}))
        tracker = {
            TRACKER_ID: tracker_id if old else secrets.token_hex(6),
            CONF_NAME: item["name"],
            CONF_DEVICE: item.get("device_id"),
            CONF_SENSORS: item.get("sensors") or [],
        }
        if old.get(TRACKER_UNIQUE_ID):
            tracker[TRACKER_UNIQUE_ID] = old[TRACKER_UNIQUE_ID]
        if not tracker[CONF_DEVICE] and not tracker[CONF_SENSORS]:
            connection.send_error(
                msg["id"], "invalid_format", f"{item['name']}: choose a device"
            )
            return
        new_by_entry[entry.entry_id].append(tracker)

    for entry in entries:
        trackers = new_by_entry[entry.entry_id]
        if trackers != (entry.options.get(CONF_TRACKERS) or []):
            hass.config_entries.async_update_entry(
                entry, options={**entry.options, CONF_TRACKERS: trackers}
            )
    connection.send_result(msg["id"])


@websocket_api.websocket_command(
    {vol.Required("type"): "ble_fingerprint/guided_points", vol.Required("room"): str}
)
@callback
def ws_guided_points(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Punti da registrare per una stanza: centro e angoli, rientrati dai muri."""
    map_store = get_map_store(hass)
    zone = map_store.zones.get(msg["room"])
    if zone is None:
        connection.send_error(msg["id"], "not_found", "Room not found")
        return

    polygon = [(float(p[0]), float(p[1])) for p in zone["polygon"]]
    if scale := map_store.floor_scale(zone.get("floor")):
        inset = GUIDED_CORNER_INSET_M * scale
    else:
        # piano senza scala: il 10% della dimensione della stanza
        xs, ys = [p[0] for p in polygon], [p[1] for p in polygon]
        inset = 0.1 * min(max(xs) - min(xs), max(ys) - min(ys))

    recorded = map_store.rooms.get(msg["room"], {})
    points = []
    for number, (kind, index, (x, y)) in enumerate(guided_points(polygon, inset)):
        vertex_id = f"p{number}"
        points.append(
            {
                "vertex_id": vertex_id,
                "kind": kind,
                "index": index,
                "x": round(x, 1),
                "y": round(y, 1),
                "recorded": vertex_id in recorded,
            }
        )
    connection.send_result(msg["id"], {"floor_id": zone.get("floor"), "points": points})
