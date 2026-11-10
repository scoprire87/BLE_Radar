"""Integrazione BLE Fingerprint: posizionamento indoor tramite fingerprint BLE."""
from __future__ import annotations

import asyncio
import copy
import logging
import math
from statistics import median
from typing import TYPE_CHECKING, Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID, Platform
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .fingerprint import proxy_key

from .const import (
    CONF_DEVICE,
    CONF_NAME,
    CONF_SENSORS,
    DOMAIN,
    LEGACY_NAME,
    LEGACY_PROXY_SENSORS,
    MAX_DISTANCE,
    SERVICE_DELETE_ROOM,
    SERVICE_DELETE_VERTEX,
    SERVICE_GET_MAP,
    SERVICE_RECORD_VERTEX,
    SERVICE_SAVE_VERTEX,
    SERVICE_SET_PROXY,
    SIGNAL_MAP_UPDATED,
    STORAGE_KEY,
    STORAGE_VERSION,
)

if TYPE_CHECKING:
    from .sensor import BLEFingerprintSensor

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

ATTR_ROOM = "room"
ATTR_VERTEX_ID = "vertex_id"
ATTR_DISTANCES = "distances"
ATTR_X = "x"
ATTR_Y = "y"
ATTR_DURATION = "duration"
ATTR_FLOOR = "floor"
ATTR_PROXY = "proxy"
ATTR_FACTOR = "factor"

DEFAULT_RECORD_DURATION = 5
MAX_RECORD_DURATION = 60

_NON_EMPTY_STRING = vol.All(cv.string, vol.Strip, vol.Length(min=1))
_OPTIONAL_COORD = vol.Any(None, vol.Coerce(float))
_OPTIONAL_FLOOR = vol.Any(None, vol.All(cv.string, vol.Strip))

SERVICE_SAVE_VERTEX_SCHEMA = vol.Schema({
    vol.Required(ATTR_ROOM): _NON_EMPTY_STRING,
    vol.Required(ATTR_VERTEX_ID): _NON_EMPTY_STRING,
    vol.Required(ATTR_DISTANCES): vol.Schema({cv.string: vol.Coerce(float)}),
    vol.Optional(ATTR_X): _OPTIONAL_COORD,
    vol.Optional(ATTR_Y): _OPTIONAL_COORD,
    vol.Optional(ATTR_FLOOR): _OPTIONAL_FLOOR,
})

SERVICE_RECORD_VERTEX_SCHEMA = vol.Schema({
    vol.Required(ATTR_ROOM): _NON_EMPTY_STRING,
    vol.Required(ATTR_VERTEX_ID): _NON_EMPTY_STRING,
    vol.Optional(ATTR_ENTITY_ID): cv.entity_id,
    vol.Optional(ATTR_X): _OPTIONAL_COORD,
    vol.Optional(ATTR_Y): _OPTIONAL_COORD,
    vol.Optional(ATTR_FLOOR): _OPTIONAL_FLOOR,
    vol.Optional(ATTR_DURATION, default=DEFAULT_RECORD_DURATION): vol.All(
        vol.Coerce(int), vol.Range(min=0, max=MAX_RECORD_DURATION)
    ),
})

SERVICE_SET_PROXY_SCHEMA = vol.Schema({
    vol.Required(ATTR_PROXY): _NON_EMPTY_STRING,
    vol.Optional(ATTR_X): _OPTIONAL_COORD,
    vol.Optional(ATTR_Y): _OPTIONAL_COORD,
    vol.Optional(ATTR_FLOOR): _OPTIONAL_FLOOR,
    vol.Optional(ATTR_FACTOR): vol.Any(
        None, vol.All(vol.Coerce(float), vol.Range(min=0.3, max=3.0))
    ),
})

SERVICE_DELETE_VERTEX_SCHEMA = vol.Schema({
    vol.Required(ATTR_ROOM): _NON_EMPTY_STRING,
    vol.Required(ATTR_VERTEX_ID): _NON_EMPTY_STRING,
})

SERVICE_DELETE_ROOM_SCHEMA = vol.Schema({
    vol.Required(ATTR_ROOM): _NON_EMPTY_STRING,
})


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Carica la mappa salvata e registra i servizi."""
    domain_data = hass.data.setdefault(DOMAIN, {})

    store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    map_data = await store.async_load()
    if not isinstance(map_data, dict):
        map_data = {}
    if not isinstance(map_data.get("rooms"), dict):
        map_data["rooms"] = {}
    if not isinstance(map_data.get("proxies"), dict):
        map_data["proxies"] = {}

    domain_data["map_data"] = map_data
    domain_data["map_version"] = 1
    # sensori di posizione attivi, per entry_id (usati da record_vertex)
    domain_data.setdefault("entities", {})

    rooms: dict[str, dict[str, Any]] = map_data["rooms"]
    proxy_overrides: dict[str, dict[str, Any]] = map_data["proxies"]

    async def _async_save_map() -> None:
        await store.async_save(map_data)
        # i sensori ricalcolano proxy e piani quando la versione cambia
        domain_data["map_version"] = domain_data.get("map_version", 0) + 1
        # ricalcola subito la posizione con la mappa aggiornata
        async_dispatcher_send(hass, SIGNAL_MAP_UPDATED)

    async def _async_store_vertex(
        room: str,
        vertex_id: str,
        distances: dict[str, float],
        x: float | None,
        y: float | None,
        floor: str | None = None,
    ) -> dict[str, Any]:
        vertex: dict[str, Any] = {"distances": distances, "coords": {"x": x, "y": y}}
        if floor:
            vertex["floor"] = floor
        rooms.setdefault(room, {})[vertex_id] = vertex
        await _async_save_map()
        _LOGGER.info(
            "Salvato vertice %s in %s (X:%s, Y:%s) con %d proxy attivi",
            vertex_id,
            room,
            x,
            y,
            len(distances),
        )
        return vertex

    def _get_tracker(entity_id: str | None) -> BLEFingerprintSensor:
        trackers: list[BLEFingerprintSensor] = list(domain_data["entities"].values())
        if entity_id is not None:
            for tracker in trackers:
                if tracker.entity_id == entity_id:
                    return tracker
            raise ServiceValidationError(
                f"{entity_id} non è un sensore di posizione BLE Fingerprint attivo"
            )
        if len(trackers) == 1:
            return trackers[0]
        if not trackers:
            raise ServiceValidationError(
                "Nessun sensore di posizione BLE Fingerprint configurato"
            )
        raise ServiceValidationError(
            "Sono configurati più sensori di posizione: indica entity_id"
        )

    async def handle_save_vertex(call: ServiceCall) -> None:
        raw_distances: dict[str, float] = call.data[ATTR_DISTANCES]

        # Filtro Sparso: teniamo solo distanze valide (0 <= d < MAX_DISTANCE)
        valid_distances = {
            k: v
            for k, v in raw_distances.items()
            if math.isfinite(v) and 0.0 <= v < MAX_DISTANCE
        }

        await _async_store_vertex(
            call.data[ATTR_ROOM],
            call.data[ATTR_VERTEX_ID],
            valid_distances,
            call.data.get(ATTR_X),
            call.data.get(ATTR_Y),
            call.data.get(ATTR_FLOOR),
        )

    async def handle_record_vertex(call: ServiceCall) -> ServiceResponse:
        """Legge le distanze attuali (mediana su `duration` secondi) e le salva."""
        tracker = _get_tracker(call.data.get(ATTR_ENTITY_ID))
        duration: int = call.data[ATTR_DURATION]

        samples: list[dict[str, float]] = [tracker.current_distances]
        for _ in range(duration):
            await asyncio.sleep(1)
            samples.append(tracker.current_distances)

        # un proxy conta solo se visto in almeno metà dei campioni
        min_seen = math.ceil(len(samples) / 2)
        values: dict[str, list[float]] = {}
        for sample in samples:
            for proxy, value in sample.items():
                values.setdefault(proxy, []).append(value)
        distances = {
            proxy: round(median(seen), 2)
            for proxy, seen in values.items()
            if len(seen) >= min_seen
        }

        if not distances:
            raise HomeAssistantError(
                "Nessuna distanza valida rilevata: vertice non salvato"
            )

        room: str = call.data[ATTR_ROOM]
        vertex_id: str = call.data[ATTR_VERTEX_ID]
        vertex = await _async_store_vertex(
            room,
            vertex_id,
            distances,
            call.data.get(ATTR_X),
            call.data.get(ATTR_Y),
            call.data.get(ATTR_FLOOR),
        )

        if call.return_response:
            return {
                ATTR_ROOM: room,
                ATTR_VERTEX_ID: vertex_id,
                "samples": len(samples),
                **copy.deepcopy(vertex),
            }
        return None

    async def handle_delete_vertex(call: ServiceCall) -> None:
        room: str = call.data[ATTR_ROOM]
        vertex_id: str = call.data[ATTR_VERTEX_ID]

        if vertex_id not in rooms.get(room, {}):
            raise ServiceValidationError(
                f"Vertice {vertex_id} non trovato nella stanza {room}"
            )

        del rooms[room][vertex_id]
        if not rooms[room]:
            del rooms[room]
        await _async_save_map()
        _LOGGER.info("Eliminato vertice %s da %s", vertex_id, room)

    async def handle_delete_room(call: ServiceCall) -> None:
        room: str = call.data[ATTR_ROOM]

        if room not in rooms:
            raise ServiceValidationError(f"Stanza {room} non trovata nella mappa")

        del rooms[room]
        await _async_save_map()
        _LOGGER.info("Eliminata stanza %s dalla mappa", room)

    async def handle_set_proxy(call: ServiceCall) -> None:
        """Imposta (o rimuove) a mano posizione e correzione di un proxy."""
        proxy = proxy_key(call.data[ATTR_PROXY])
        x, y = call.data.get(ATTR_X), call.data.get(ATTR_Y)

        if x is None and y is None:
            if proxy_overrides.pop(proxy, None) is None:
                raise ServiceValidationError(
                    f"Nessuna posizione manuale impostata per il proxy {proxy}"
                )
            _LOGGER.info("Rimossa la posizione manuale del proxy %s", proxy)
        elif x is None or y is None:
            raise ServiceValidationError("Indica sia x sia y (oppure nessuno dei due)")
        else:
            override: dict[str, Any] = {"x": x, "y": y}
            if floor := call.data.get(ATTR_FLOOR):
                override["floor"] = floor
            if (factor := call.data.get(ATTR_FACTOR)) is not None:
                override["factor"] = factor
            proxy_overrides[proxy] = override
            _LOGGER.info("Posizione manuale del proxy %s: %s", proxy, override)

        await _async_save_map()

    async def handle_get_map(call: ServiceCall) -> ServiceResponse:
        return {
            "rooms": copy.deepcopy(rooms),
            "proxies": copy.deepcopy(proxy_overrides),
            # posizione e correzione dei proxy stimate da ogni sensore attivo
            "estimated_proxies": {
                tracker.entity_id: tracker.proxy_diagnostics
                for tracker in domain_data["entities"].values()
            },
        }

    hass.services.async_register(
        DOMAIN, SERVICE_SAVE_VERTEX, handle_save_vertex, schema=SERVICE_SAVE_VERTEX_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_RECORD_VERTEX,
        handle_record_vertex,
        schema=SERVICE_RECORD_VERTEX_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_DELETE_VERTEX,
        handle_delete_vertex,
        schema=SERVICE_DELETE_VERTEX_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_DELETE_ROOM, handle_delete_room, schema=SERVICE_DELETE_ROOM_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SET_PROXY, handle_set_proxy, schema=SERVICE_SET_PROXY_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_MAP,
        handle_get_map,
        schema=vol.Schema({}),
        supports_response=SupportsResponse.ONLY,
    )

    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migra le config entry create prima della configurazione da interfaccia."""
    if entry.version > 2:
        return False

    if entry.version == 1:
        # la versione 1 non aveva dati: nome e sensori erano fissi nel codice
        data = {
            CONF_NAME: LEGACY_NAME,
            CONF_SENSORS: list(LEGACY_PROXY_SENSORS),
            **entry.data,
        }
        hass.config_entries.async_update_entry(entry, data=data, version=2)
        _LOGGER.info("Config entry BLE Fingerprint migrata alla versione 2")

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Imposta una config entry inoltrando il setup alla piattaforma sensor."""
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    if device_id := {**entry.data, **entry.options}.get(CONF_DEVICE):
        entry.async_on_unload(_async_watch_device_sensors(hass, entry, device_id))

    return True


@callback
def _async_watch_device_sensors(
    hass: HomeAssistant, entry: ConfigEntry, device_id: str
) -> CALLBACK_TYPE:
    """Ricarica la entry quando il dispositivo ottiene un nuovo sensore di distanza.

    Succede quando Bermuda vede il telefono da un nuovo proxy (o quando un
    sensore viene riattivato): così non serve ricaricare l'integrazione a mano.
    """
    # pylint: disable-next=import-outside-toplevel
    from .sensor import async_resolve_proxy_sensors, is_distance_sensor

    known = set(async_resolve_proxy_sensors(hass, device_id, None))

    @callback
    def _async_registry_updated(
        event: Event[er.EventEntityRegistryUpdatedData],
    ) -> None:
        if event.data["action"] not in ("create", "update"):
            return
        entity = er.async_get(hass).async_get(event.data["entity_id"])
        if (
            entity is None
            or entity.device_id != device_id
            or entity.disabled_by is not None
            or not is_distance_sensor(entity)
            or entity.entity_id in known
        ):
            return
        known.add(entity.entity_id)
        _LOGGER.info(
            "%s: nuovo sensore di distanza %s, ricarico", entry.title, entity.entity_id
        )
        hass.config_entries.async_schedule_reload(entry.entry_id)

    return hass.bus.async_listen(
        er.EVENT_ENTITY_REGISTRY_UPDATED, _async_registry_updated
    )


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Ricarica la config entry quando cambiano le opzioni."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Scarica una config entry, rimuovendo la piattaforma sensor."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
