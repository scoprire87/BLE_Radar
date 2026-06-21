import logging
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.storage import Store

from .const import DOMAIN, STORAGE_KEY, STORAGE_VERSION

_LOGGER = logging.getLogger(__name__)

SERVICE_SAVE_VERTEX = "save_vertex"
SERVICE_SAVE_VERTEX_SCHEMA = vol.Schema({
    vol.Required("room"): cv.string,
    vol.Required("vertex_id"): cv.string,
    vol.Required("distances"): dict,
})

# Le piattaforme che la nostra integrazione caricherà (il sensore)
PLATFORMS = ["sensor"]

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Configura l'integrazione dalla UI."""
    hass.data.setdefault(DOMAIN, {})

    store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    stored_data = await store.async_load()
    
    if stored_data is None:
        stored_data = {"rooms": {}}
        
    hass.data[DOMAIN]["map_data"] = stored_data
    hass.data[DOMAIN]["store"] = store

    async def handle_save_vertex(call: ServiceCall):
        room = call.data["room"]
        vertex_id = call.data["vertex_id"]
        distances = call.data["distances"]

        _LOGGER.info(f"Salvataggio vertice {vertex_id} per la stanza: {room}")

        rooms = hass.data[DOMAIN]["map_data"]["rooms"]
        if room not in rooms:
            rooms[room] = {}
        rooms[room][vertex_id] = distances

        await store.async_save(hass.data[DOMAIN]["map_data"])

    hass.services.async_register(
        DOMAIN, SERVICE_SAVE_VERTEX, handle_save_vertex, schema=SERVICE_SAVE_VERTEX_SCHEMA
    )

    # Inoltra il setup al file sensor.py
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Gestisce la disinstallazione dell'integrazione dalla UI."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop("map_data", None)
        hass.data[DOMAIN].pop("store", None)
    return unload_ok
