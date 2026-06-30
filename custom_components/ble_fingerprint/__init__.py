import logging
import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.storage import Store

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
STORAGE_KEY = "ble_fingerprint_map"

SERVICE_SAVE_VERTEX_SCHEMA = vol.Schema({
    vol.Required("room"): cv.string,
    vol.Required("vertex_id"): cv.string,
    vol.Required("distances"): dict,
    vol.Optional("x"): vol.Any(vol.Coerce(float), None),
    vol.Optional("y"): vol.Any(vol.Coerce(float), None),
})


async def async_setup(hass: HomeAssistant, config: dict):
    hass.data.setdefault(DOMAIN, {})

    store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    map_data = await store.async_load()
    if map_data is None:
        map_data = {"rooms": {}}

    hass.data[DOMAIN]["map_data"] = map_data

    async def handle_save_vertex(call):
        room = call.data.get("room")
        vertex_id = str(call.data.get("vertex_id"))
        raw_distances = call.data.get("distances")
        x = call.data.get("x")
        y = call.data.get("y")

        # Filtro Sparso: teniamo solo distanze valide < 10.0
        valid_distances = {k: float(v) for k, v in raw_distances.items() if float(v) < 10.0}

        rooms = hass.data[DOMAIN]["map_data"]["rooms"]
        if room not in rooms:
            rooms[room] = {}

        rooms[room][vertex_id] = {
            "distances": valid_distances,
            "coords": {"x": x, "y": y}
        }

        await store.async_save(hass.data[DOMAIN]["map_data"])
        _LOGGER.info(
            f"Salvato vertice {vertex_id} in {room} (X:{x}, Y:{y}) con {len(valid_distances)} proxy attivi."
        )

    hass.services.async_register(
        DOMAIN, "save_vertex", handle_save_vertex, schema=SERVICE_SAVE_VERTEX_SCHEMA
    )

    return True


async def async_setup_entry(hass: HomeAssistant, entry) -> bool:
    """Imposta una config entry inoltrando il setup alla piattaforma sensor."""
    await hass.config_entries.async_forward_entry_setups(entry, ["sensor"])
    return True


async def async_unload_entry(hass: HomeAssistant, entry) -> bool:
    """Scarica una config entry, rimuovendo la piattaforma sensor."""
    return await hass.config_entries.async_unload_platforms(entry, ["sensor"])
