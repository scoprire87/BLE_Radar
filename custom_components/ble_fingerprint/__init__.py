"""Integrazione BLE Fingerprint: posizionamento indoor tramite fingerprint BLE."""
from __future__ import annotations

import logging
import math

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .const import (
    DOMAIN,
    MAX_DISTANCE,
    SIGNAL_MAP_UPDATED,
    SERVICE_SAVE_VERTEX,
    STORAGE_KEY,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SERVICE_SAVE_VERTEX_SCHEMA = vol.Schema({
    vol.Required("room"): vol.All(cv.string, vol.Strip, vol.Length(min=1)),
    vol.Required("vertex_id"): vol.All(cv.string, vol.Strip, vol.Length(min=1)),
    vol.Required("distances"): vol.Schema({cv.string: vol.Coerce(float)}),
    vol.Optional("x"): vol.Any(None, vol.Coerce(float)),
    vol.Optional("y"): vol.Any(None, vol.Coerce(float)),
})


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Carica la mappa salvata e registra il servizio save_vertex."""
    hass.data.setdefault(DOMAIN, {})

    store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    map_data = await store.async_load()
    if not isinstance(map_data, dict):
        map_data = {}
    map_data.setdefault("rooms", {})

    hass.data[DOMAIN]["map_data"] = map_data

    async def handle_save_vertex(call: ServiceCall) -> None:
        room: str = call.data["room"]
        vertex_id: str = call.data["vertex_id"]
        raw_distances: dict[str, float] = call.data["distances"]
        x = call.data.get("x")
        y = call.data.get("y")

        # Filtro Sparso: teniamo solo distanze valide (0 <= d < MAX_DISTANCE)
        valid_distances = {
            k: v
            for k, v in raw_distances.items()
            if math.isfinite(v) and 0.0 <= v < MAX_DISTANCE
        }

        rooms = hass.data[DOMAIN]["map_data"]["rooms"]
        if room not in rooms:
            rooms[room] = {}

        rooms[room][vertex_id] = {
            "distances": valid_distances,
            "coords": {"x": x, "y": y}
        }

        await store.async_save(hass.data[DOMAIN]["map_data"])
        # ricalcola subito la posizione con la mappa aggiornata
        async_dispatcher_send(hass, SIGNAL_MAP_UPDATED)
        _LOGGER.info(
            "Salvato vertice %s in %s (X:%s, Y:%s) con %d proxy attivi",
            vertex_id,
            room,
            x,
            y,
            len(valid_distances),
        )

    hass.services.async_register(
        DOMAIN, SERVICE_SAVE_VERTEX, handle_save_vertex, schema=SERVICE_SAVE_VERTEX_SCHEMA
    )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Imposta una config entry inoltrando il setup alla piattaforma sensor."""
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Scarica una config entry, rimuovendo la piattaforma sensor."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
