"""Sensore di posizione basato su fingerprint BLE."""
from __future__ import annotations

from datetime import datetime
import logging
import math
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.util import dt as dt_util

from . import get_trackers
from .const import (
    CONF_CONFIRMATION_SECONDS,
    CONF_DEVICE,
    CONF_MAP_SCALE,
    CONF_MAX_DISTANCE,
    CONF_MIN_COMMON_PROXIES,
    CONF_NAME,
    CONF_SENSORS,
    CONF_SMOOTHING,
    CONF_TOP_K,
    CONF_TRILATERATION,
    CONF_UNKNOWN_TIMEOUT,
    DEFAULT_CONFIRMATION_SECONDS,
    DEFAULT_MAP_SCALE,
    DEFAULT_MAX_DISTANCE,
    DEFAULT_MIN_COMMON_PROXIES,
    DEFAULT_NAME,
    DEFAULT_SMOOTHING,
    DEFAULT_TOP_K,
    DEFAULT_TRILATERATION,
    DEFAULT_UNKNOWN_TIMEOUT,
    DOMAIN,
    SIGNAL_MAP_UPDATED,
    TRACKER_ID,
    TRACKER_UNIQUE_ID,
    UNKNOWN_ROOM,
)
from .fingerprint import (
    DISTANCE_TO_MARKER,
    estimate_position,
    normalize_distances,
    proxy_key,
    room_floors,
    score_vertices,
    vertices_by_floor,
)
from .geometry import (
    PositionFilter,
    ProxyEstimate,
    estimate_proxies,
    fingerprint_quality,
    fuse,
    jump_weight,
    locate_in_zones,
    map_bounds,
    trilaterate,
)

_LOGGER = logging.getLogger(__name__)


def is_distance_sensor(entity: er.RegistryEntry) -> bool:
    """Sensore `..._distance_to_<proxy>` (come quelli di Bermuda), non "unfiltered"."""
    return (
        entity.domain == "sensor"
        and DISTANCE_TO_MARKER in entity.entity_id
        and "unfiltered" not in entity.entity_id
    )


@callback
def async_resolve_proxy_sensors(
    hass: HomeAssistant, device_id: str | None, extra_sensors: list[str] | None
) -> list[str]:
    """Elenco dei sensori di distanza: quelli del dispositivo più quelli manuali.

    Per il dispositivo vengono presi i sensori `..._distance_to_<proxy>` attivi
    (come quelli creati da Bermuda), escludendo le varianti "unfiltered".
    """
    sensors: list[str] = []

    if device_id:
        registry = er.async_get(hass)
        # solo entità attive (le disabilitate non hanno stato)
        for entity in er.async_entries_for_device(registry, device_id):
            if is_distance_sensor(entity):
                sensors.append(entity.entity_id)

    for entity_id in extra_sensors or []:
        if entity_id not in sensors:
            sensors.append(entity_id)

    return sorted(sensors)


def _parse_distance(state: State | None, max_distance: float) -> float | None:
    """Restituisce la distanza valida (0 <= d < max_distance) o None."""
    if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
        return None
    try:
        val = float(state.state)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(val) or not 0.0 <= val < max_distance:
        return None
    return val


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Crea un sensore di posizione per ogni dispositivo tracciato."""
    config: dict[str, Any] = entry.options
    entities: list[BLEFingerprintSensor] = []

    for tracker in get_trackers(entry):
        device_id = tracker.get(CONF_DEVICE)
        name = tracker.get(CONF_NAME) or DEFAULT_NAME
        proxy_sensors = async_resolve_proxy_sensors(
            hass, device_id, tracker.get(CONF_SENSORS)
        )
        if not proxy_sensors:
            _LOGGER.warning(
                "%s: nessun sensore di distanza trovato, la posizione resterà sconosciuta",
                name,
            )

        entities.append(
            BLEFingerprintSensor(
                tracker_id=tracker[TRACKER_ID],
                unique_id=tracker.get(TRACKER_UNIQUE_ID)
                or f"{tracker[TRACKER_ID]}_position",
                name=name,
                proxy_sensors=proxy_sensors,
                confirmation_seconds=float(
                    config.get(CONF_CONFIRMATION_SECONDS, DEFAULT_CONFIRMATION_SECONDS)
                ),
                max_distance=float(config.get(CONF_MAX_DISTANCE, DEFAULT_MAX_DISTANCE)),
                top_k=int(config.get(CONF_TOP_K, DEFAULT_TOP_K)),
                min_common=int(
                    config.get(CONF_MIN_COMMON_PROXIES, DEFAULT_MIN_COMMON_PROXIES)
                ),
                unknown_timeout=float(
                    config.get(CONF_UNKNOWN_TIMEOUT, DEFAULT_UNKNOWN_TIMEOUT)
                ),
                smoothing=bool(config.get(CONF_SMOOTHING, DEFAULT_SMOOTHING)),
                trilateration=bool(
                    config.get(CONF_TRILATERATION, DEFAULT_TRILATERATION)
                ),
                map_scale=float(config.get(CONF_MAP_SCALE, DEFAULT_MAP_SCALE) or 1.0),
                device_info=_device_info(hass, tracker[TRACKER_ID], name, device_id),
            )
        )

    _async_remove_orphans(hass, entry, entities)
    async_add_entities(entities)


@callback
def _async_remove_orphans(
    hass: HomeAssistant, entry: ConfigEntry, entities: list[BLEFingerprintSensor]
) -> None:
    """Rimuove sensori e dispositivi dei dispositivi non più tracciati."""
    wanted_entities = {entity.unique_id for entity in entities}
    entity_registry = er.async_get(hass)
    for reg_entry in er.async_entries_for_config_entry(entity_registry, entry.entry_id):
        if reg_entry.domain == "sensor" and reg_entry.unique_id not in wanted_entities:
            entity_registry.async_remove(reg_entry.entity_id)

    wanted_devices = {entity.tracker_id for entity in entities}
    device_registry = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(device_registry, entry.entry_id):
        if not any(
            domain == DOMAIN and identifier in wanted_devices
            for domain, identifier in device.identifiers
        ):
            device_registry.async_update_device(
                device.id, remove_config_entry_id=entry.entry_id
            )


def _device_info(
    hass: HomeAssistant, tracker_id: str, name: str, device_id: str | None
) -> DeviceInfo:
    """Dispositivo del sensore, collegato a quello tracciato (es. Bermuda)."""
    info = DeviceInfo(
        identifiers={(DOMAIN, tracker_id)},
        name=name,
        manufacturer="BLE Fingerprint",
        model="Posizionamento BLE",
        entry_type=dr.DeviceEntryType.SERVICE,
    )
    if device_id and (device := dr.async_get(hass).async_get(device_id)):
        if device.identifiers:
            info["via_device"] = next(iter(device.identifiers))
    return info


class BLEFingerprintSensor(SensorEntity):
    """Stanza e coordinate stimate confrontando le distanze con la mappa."""

    _attr_should_poll = False
    _attr_icon = "mdi:map-marker-radius"

    def __init__(
        self,
        *,
        tracker_id: str,
        unique_id: str,
        name: str,
        proxy_sensors: list[str],
        confirmation_seconds: float,
        max_distance: float,
        top_k: int,
        min_common: int,
        unknown_timeout: float,
        smoothing: bool = DEFAULT_SMOOTHING,
        trilateration: bool = DEFAULT_TRILATERATION,
        map_scale: float = DEFAULT_MAP_SCALE,
        device_info: DeviceInfo | None = None,
    ) -> None:
        self._tracker_id = tracker_id
        self._attr_name = name
        self._attr_unique_id = unique_id
        self._proxy_sensors = proxy_sensors
        self._known_proxies = {proxy_key(entity_id) for entity_id in proxy_sensors}

        self._confirmation_seconds = confirmation_seconds
        self._max_distance = max_distance
        self._top_k = top_k
        self._min_common = min_common
        self._unknown_timeout = unknown_timeout
        self._smoothing = smoothing
        self._trilateration = trilateration
        self._scale = map_scale if map_scale > 0 else 1.0
        if device_info is not None:
            self._attr_device_info = device_info

        # --- Motore geometrico (calcolato dalla mappa, per piano) ---
        self._room_floors: dict[str, str | None] = {}
        self._proxy_model: dict[str | None, dict[str, ProxyEstimate]] = {}
        self._floor_bounds: dict[str | None, tuple[float, float, float, float]] = {}
        # stanze disegnate, per piano, in metri: [(nome, poligono)]
        self._zones: dict[str | None, list[tuple[str, list[tuple[float, float]]]]] = {}
        self._floor_names: dict[str, str] = {}
        self._map_version: int | None = None
        self._last_radii: dict[str, float] = {}
        self._filter = PositionFilter()

        self._attr_native_value = UNKNOWN_ROOM
        self._attr_extra_state_attributes = _empty_attributes()
        # distanze valide attuali, per entity_id del sensore
        self._current_distances: dict[str, float] = {}

        # --- Filtro di stabilizzazione ---
        # Stanza candidata al cambio (in attesa di conferma)
        self._pending_room: str | None = None
        self._cancel_confirmation_timer: CALLBACK_TYPE | None = None
        # Timer che riporta a "Sconosciuta" quando non c'è più alcun proxy
        self._cancel_unknown_timer: CALLBACK_TYPE | None = None

    @property
    def current_distances(self) -> dict[str, float]:
        """Distanze valide attuali per chiave proxy (copia)."""
        return normalize_distances(self._current_distances)

    @property
    def tracker_id(self) -> str:
        """Id del dispositivo tracciato."""
        return self._tracker_id

    @property
    def proxy_sensors(self) -> list[str]:
        """Sensori di distanza usati."""
        return list(self._proxy_sensors)

    def _floor_scale(self, floor: str | None) -> float:
        """Unità della mappa per metro del piano (pixel/m se calibrato)."""
        map_store = self.hass.data.get(DOMAIN, {}).get("map")
        if map_store is not None and (scale := map_store.floor_scale(floor)):
            return scale
        return self._scale

    @property
    def proxy_diagnostics(self) -> dict[str, dict[str, dict]]:
        """Proxy stimati per piano, nelle unità della mappa (servizio get_map)."""
        self._async_refresh_model()
        result: dict[str, dict[str, dict]] = {}
        for floor, proxies in self._proxy_model.items():
            floor_result = result.setdefault(str(floor) if floor is not None else "", {})
            scale = self._floor_scale(floor)
            for proxy, estimate in proxies.items():
                data = estimate.as_dict()
                data["x"] = round(estimate.x * scale, 2)
                data["y"] = round(estimate.y * scale, 2)
                # residuo in metri, come le distanze
                floor_result[proxy] = data
        return result

    @callback
    def _async_refresh_model(self) -> None:
        """Ricalcola piani, proxy e limiti della mappa se la mappa è cambiata."""
        map_store = self.hass.data.get(DOMAIN, {}).get("map")
        if map_store is None:
            return
        if map_store.version == self._map_version:
            return
        self._map_version = map_store.version

        rooms = map_store.rooms
        overrides = map_store.proxies
        self._room_floors = room_floors(rooms)
        # una stanza disegnata appartiene al piano su cui è disegnata
        for name, zone in map_store.zones.items():
            self._room_floors[name] = zone.get("floor")
        self._floor_names = {
            floor_id: floor.get("name") or floor_id
            for floor_id, floor in map_store.floors.items()
        }

        self._zones = {}
        for name, zone in map_store.zones.items():
            floor = zone.get("floor")
            scale = self._floor_scale(floor)
            polygon = [
                (float(p[0]) / scale, float(p[1]) / scale)
                for p in zone.get("polygon") or []
                if isinstance(p, (list, tuple)) and len(p) == 2
            ]
            if len(polygon) >= 3:
                self._zones.setdefault(floor, []).append((name, polygon))

        by_floor = vertices_by_floor(rooms, self._floor_scale)
        floors = (
            set(by_floor)
            | {(data.get("floor") or None) for data in overrides.values()}
            | set(self._zones)
        )

        self._proxy_model = {}
        self._floor_bounds = {}
        for floor in floors:
            vertices = by_floor.get(floor, [])
            scale = self._floor_scale(floor)
            floor_overrides = {
                proxy: {**data, "x": data["x"] / scale, "y": data["y"] / scale}
                for proxy, data in overrides.items()
                if (data.get("floor") or None) == floor
                and isinstance(data.get("x"), (int, float))
                and isinstance(data.get("y"), (int, float))
            }
            proxies = estimate_proxies(vertices, floor_overrides)
            self._proxy_model[floor] = proxies
            points = (
                [(v[0], v[1]) for v in vertices]
                + [(p.x, p.y) for p in proxies.values()]
                + [pt for _, poly in self._zones.get(floor, []) for pt in poly]
            )
            if (bounds := map_bounds(points)) is not None:
                self._floor_bounds[floor] = bounds

        self._last_radii = {}
        self._filter.reset()

    async def async_added_to_hass(self) -> None:
        """Legge lo stato iniziale dei proxy e si iscrive ai cambiamenti."""
        for proxy in self._proxy_sensors:
            val = _parse_distance(self.hass.states.get(proxy), self._max_distance)
            if val is not None:
                self._current_distances[proxy] = val

        self.hass.data[DOMAIN]["entities"][self._tracker_id] = self

        self.async_on_remove(self._async_cleanup)
        if self._proxy_sensors:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, self._proxy_sensors, self._async_distance_changed
                )
            )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_MAP_UPDATED, self._calculate_position
            )
        )

        # posizione iniziale con i valori già disponibili
        self._calculate_position()

    @callback
    def _async_cleanup(self) -> None:
        """Annulla i timer e rimuove il sensore dall'elenco di quelli attivi."""
        self._cancel_confirmation()
        self._cancel_unknown()
        entities = self.hass.data.get(DOMAIN, {}).get("entities", {})
        if entities.get(self._tracker_id) is self:
            entities.pop(self._tracker_id)

    @callback
    def _cancel_confirmation(self) -> None:
        """Annulla il timer di conferma in corso, se presente."""
        if self._cancel_confirmation_timer is not None:
            self._cancel_confirmation_timer()
            self._cancel_confirmation_timer = None

    @callback
    def _cancel_unknown(self) -> None:
        """Annulla il timer di ritorno a "Sconosciuta", se presente."""
        if self._cancel_unknown_timer is not None:
            self._cancel_unknown_timer()
            self._cancel_unknown_timer = None

    @callback
    def _async_distance_changed(self, event: Event[EventStateChangedData]) -> None:
        entity_id = event.data["entity_id"]

        val = _parse_distance(event.data.get("new_state"), self._max_distance)
        if val is not None:
            self._current_distances[entity_id] = val
        else:
            self._current_distances.pop(entity_id, None)

        self._calculate_position()

    @callback
    def _calculate_position(self) -> None:
        current = self.current_distances

        # --- Nessun proxy valido: dopo unknown_timeout la posizione è sconosciuta ---
        if not current:
            if (
                self._cancel_unknown_timer is None
                and self._unknown_timeout > 0
                and self._attr_native_value != UNKNOWN_ROOM
            ):
                self._cancel_unknown_timer = async_call_later(
                    self.hass, self._unknown_timeout, self._async_set_unknown
                )
            return
        self._cancel_unknown()

        map_store = self.hass.data.get(DOMAIN, {}).get("map")
        rooms = map_store.rooms if map_store is not None else {}
        if not rooms:
            return
        self._async_refresh_model()

        # --- 1. Fingerprint: stanza e coordinate dai vertici più simili ---
        scored = score_vertices(
            current,
            rooms,
            known_proxies=self._known_proxies,
            max_distance=self._max_distance,
            min_common=self._min_common,
        )
        estimate = estimate_position(scored, self._top_k)
        if estimate is None:
            return

        floor = self._room_floors.get(estimate.room)
        bounds = self._floor_bounds.get(floor)
        scale = self._floor_scale(floor)

        fingerprint = None
        if estimate.x is not None and estimate.y is not None:
            fingerprint = (
                (estimate.x / scale, estimate.y / scale),
                fingerprint_quality(estimate.score),
            )

        # --- 2. Trilaterazione con i proxy stimati dalla mappa (BPS) ---
        fix = None
        if self._trilateration:
            circles = []
            radii: dict[str, float] = {}
            for proxy, distance in current.items():
                model = self._proxy_model.get(floor, {}).get(proxy)
                if model is None:
                    continue
                radius = distance / model.factor
                radii[proxy] = radius
                weight = jump_weight(radius, self._last_radii.get(proxy))
                circles.append((model.x, model.y, radius, weight))
            self._last_radii = radii
            fix = trilaterate(
                circles,
                start=fingerprint[0] if fingerprint else None,
                bounds=bounds,
            )

        # --- 3. Fusione pesata per qualità e filtro di Kalman ---
        fused = fuse(
            fingerprint,
            ((fix.x, fix.y), fix.quality) if fix is not None else None,
        )
        x = y = None
        confidence = None
        method = None
        # stanza: quella del fingerprint, oppure quella disegnata sulla mappa
        # che contiene la posizione calcolata
        room = estimate.room
        if fused is not None:
            (fx, fy), confidence, method = fused
            if self._smoothing:
                fx, fy = self._filter.update(
                    (fx, fy), dt_util.utcnow().timestamp(), floor
                )
                if bounds is not None:
                    fx = min(max(fx, bounds[0]), bounds[2])
                    fy = min(max(fy, bounds[1]), bounds[3])
            if located := locate_in_zones((fx, fy), self._zones.get(floor, [])):
                # posizione fuori da tutte le stanze (es. nel muro): agganciata
                # alla stanza più vicina, come in BPS
                room, (fx, fy) = located
            x, y = round(fx * scale, 2), round(fy * scale, 2)

        new_attrs = {
            "x": x,
            "y": y,
            "floor": floor,
            "floor_name": self._floor_names.get(floor, floor) if floor else None,
            "fingerprint_room": estimate.room,
            "confidence": None if confidence is None else round(confidence, 2),
            "method": method,
            "score": estimate.score,
            "proxies": len(current),
            "trilateration_proxies": fix.proxies if fix else 0,
            "trilateration_rms": round(fix.rms, 2) if fix else None,
        }
        attrs_changed = new_attrs != self._attr_extra_state_attributes
        self._attr_extra_state_attributes = new_attrs

        # --- Filtro di conferma per la stanza ---
        room_changed = False
        if room == self._attr_native_value:
            # Siamo tornati alla stanza corrente: annulla qualsiasi cambio pendente
            self._cancel_confirmation()
            self._pending_room = None

        elif self._confirmation_seconds <= 0:
            # Filtro di conferma disattivato: cambio immediato
            self._cancel_confirmation()
            self._pending_room = None
            self._attr_native_value = room
            room_changed = True

        elif room != self._pending_room:
            # Nuova stanza diversa dalla corrente e dalla pendente: (ri)avvia il timer
            self._pending_room = room
            self._cancel_confirmation()
            self._cancel_confirmation_timer = async_call_later(
                self.hass, self._confirmation_seconds, self._async_confirm_room
            )
        # else: stessa stanza pendente già in attesa di conferma → timer in corso

        if attrs_changed or room_changed:
            self.async_write_ha_state()

    @callback
    def _async_confirm_room(self, _now: datetime) -> None:
        """Conferma il cambio di stanza allo scadere del timer."""
        self._cancel_confirmation_timer = None
        if self._pending_room is None:
            return
        _LOGGER.debug("BLE Radar: cambio stanza confermato → %s", self._pending_room)
        self._attr_native_value = self._pending_room
        self._pending_room = None
        self.async_write_ha_state()

    @callback
    def _async_set_unknown(self, _now: datetime) -> None:
        """Nessun proxy valido per unknown_timeout secondi: posizione sconosciuta."""
        self._cancel_unknown_timer = None
        if self._current_distances:
            return
        _LOGGER.debug("BLE Radar: nessun proxy rilevato, posizione sconosciuta")
        self._cancel_confirmation()
        self._pending_room = None
        self._attr_native_value = UNKNOWN_ROOM
        self._attr_extra_state_attributes = _empty_attributes()
        self._last_radii = {}
        self._filter.reset()
        self.async_write_ha_state()


def _empty_attributes() -> dict[str, Any]:
    """Attributi del sensore quando la posizione è sconosciuta."""
    return {
        "x": None,
        "y": None,
        "floor": None,
        "floor_name": None,
        "fingerprint_room": None,
        "confidence": None,
        "method": None,
        "score": None,
        "proxies": 0,
        "trilateration_proxies": 0,
        "trilateration_rms": None,
    }
