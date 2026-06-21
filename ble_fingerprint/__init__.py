import logging
import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN, STORAGE_KEY, STORAGE_VERSION

_LOGGER = logging.getLogger(__name__)

# Definiamo cosa deve ricevere il servizio dalla piantina
SERVICE_SAVE_VERTEX = "save_vertex"
SERVICE_SAVE_VERTEX_SCHEMA = vol.Schema({
    vol.Required("room"): cv.string,
    vol.Required("vertex_id"): cv.string,
    vol.Required("distances"): dict,
})

async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Inizializza il componente BLE Fingerprint."""
    # Crea lo spazio in memoria per la nostra integrazione
    hass.data.setdefault(DOMAIN, {})

    # Inizializza lo storage nascosto di Home Assistant
    store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    stored_data = await store.async_load()
    
    # Se è la prima volta che lo avviamo, creiamo la struttura vuota
    if stored_data is None:
        stored_data = {"rooms": {}}
        
    hass.data[DOMAIN]["map_data"] = stored_data
    hass.data[DOMAIN]["store"] = store

    async def handle_save_vertex(call: ServiceCall):
        """Gestisce il salvataggio di un vertice quando clicchi sulla piantina."""
        room = call.data["room"]
        vertex_id = call.data["vertex_id"]
        distances = call.data["distances"] # Il vettore di distanze lette dagli Shelly in quell'istante

        _LOGGER.info(f"Salvataggio vertice {vertex_id} per la stanza: {room}")

        # Aggiorna i dati in memoria RAM
        rooms = hass.data[DOMAIN]["map_data"]["rooms"]
        if room not in rooms:
            rooms[room] = {}
        rooms[room][vertex_id] = distances

        # Salva permanentemente su disco
        await store.async_save(hass.data[DOMAIN]["map_data"])

    # Registra il servizio in Home Assistant
    hass.services.async_register(
        DOMAIN, SERVICE_SAVE_VERTEX, handle_save_vertex, schema=SERVICE_SAVE_VERTEX_SCHEMA
    )

    return True
