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
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_DEVICE,
    CONF_NAME,
    CONF_SENSORS,
    CONF_TRACKERS,
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
    TRACKER_ID,
    TRACKER_UNIQUE_ID,
)
from .fingerprint import proxy_key
from .mapstore import MapStore

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

    map_store = MapStore(hass)
    await map_store.async_load()
    domain_data["map"] = map_store
    # sensori di posizione attivi, per id del dispositivo tracciato
    domain_data.setdefault("entities", {})

    rooms: dict[str, dict[str, Any]] = map_store.rooms
    proxy_overrides: dict[str, dict[str, Any]] = map_store.proxies

    async def _async_save_map() -> None:
        # i sensori ricalcolano la posizione con la mappa aggiornata
        await map_store.async_save()

    # pylint: disable-next=import-outside-toplevel
    from .panel import async_setup_frontend
    from .websocket import async_setup_websocket

    await async_setup_frontend(hass)
    async_setup_websocket(hass)

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
        if not rooms[room] and room not in map_store.zones:
            del rooms[room]
        await _async_save_map()
        _LOGGER.info("Eliminato vertice %s da %s", vertex_id, room)

    async def handle_delete_room(call: ServiceCall) -> None:
        room: str = call.data[ATTR_ROOM]

        if not map_store.delete_room(room):
            raise ServiceValidationError(f"Stanza {room} non trovata nella mappa")

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
            **map_store.snapshot(),
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
    """Migra le config entry delle versioni precedenti.

    - versione 1: nessun dato, nome e sensori erano fissi nel codice;
    - versione 2: un dispositivo per config entry (nome, dispositivo, sensori);
    - versione 3: un'unica integrazione con l'elenco dei dispositivi tracciati
      nelle opzioni, gestito dalla pagina laterale.
    """
    if entry.version > 3:
        return False

    data = dict(entry.data)
    if entry.version == 1:
        data = {CONF_NAME: LEGACY_NAME, CONF_SENSORS: list(LEGACY_PROXY_SENSORS), **data}

    if entry.version < 3:
        options = dict(entry.options)
        tracker = {
            TRACKER_ID: entry.entry_id,
            # mantiene l'entità esistente (storico, automazioni)
            TRACKER_UNIQUE_ID: f"{entry.entry_id}_position",
            CONF_NAME: data.get(CONF_NAME) or entry.title,
            CONF_DEVICE: options.pop(CONF_DEVICE, data.get(CONF_DEVICE)),
            CONF_SENSORS: options.pop(CONF_SENSORS, data.get(CONF_SENSORS)) or [],
        }
        options[CONF_TRACKERS] = [tracker]
        hass.config_entries.async_update_entry(
            entry, data={}, options=options, version=3, unique_id=DOMAIN
        )
        _LOGGER.info("Config entry BLE Fingerprint migrata alla versione 3")

    return True


def get_trackers(entry: ConfigEntry) -> list[dict[str, Any]]:
    """Dispositivi tracciati configurati nella entry."""
    trackers = entry.options.get(CONF_TRACKERS) or []
    return [t for t in trackers if isinstance(t, dict) and t.get(TRACKER_ID)]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Crea un sensore di posizione per ogni dispositivo tracciato."""
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    for tracker in get_trackers(entry):
        if device_id := tracker.get(CONF_DEVICE):
            entry.async_on_unload(_async_watch_device_sensors(hass, entry, device_id))

    # pylint: disable-next=import-outside-toplevel
    from .panel import async_register_panel

    async_register_panel(hass, entry)
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
    """Ricarica la config entry quando cambiano le opzioni o i dispositivi."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Scarica una config entry, rimuovendo i sensori e il pannello."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        # pylint: disable-next=import-outside-toplevel
        from .panel import async_remove_panel

        async_remove_panel(hass, entry)
    return unloaded
