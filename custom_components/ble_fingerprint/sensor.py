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
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later, async_track_state_change_event

from .const import (
    CONF_CONFIRMATION_SECONDS,
    CONF_DEVICE,
    CONF_MAX_DISTANCE,
    CONF_MIN_COMMON_PROXIES,
    CONF_NAME,
    CONF_SENSORS,
    CONF_TOP_K,
    CONF_UNKNOWN_TIMEOUT,
    DEFAULT_CONFIRMATION_SECONDS,
    DEFAULT_MAX_DISTANCE,
    DEFAULT_MIN_COMMON_PROXIES,
    DEFAULT_NAME,
    DEFAULT_TOP_K,
    DEFAULT_UNKNOWN_TIMEOUT,
    DOMAIN,
    SIGNAL_MAP_UPDATED,
    UNKNOWN_ROOM,
)
from .fingerprint import (
    DISTANCE_TO_MARKER,
    estimate_position,
    normalize_distances,
    proxy_key,
    score_vertices,
)

_LOGGER = logging.getLogger(__name__)


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
        for entity in er.async_entries_for_device(registry, device_id):
            if (
                entity.domain == "sensor"
                and DISTANCE_TO_MARKER in entity.entity_id
                and "unfiltered" not in entity.entity_id
            ):
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
    """Crea il sensore di posizione della config entry."""
    config: dict[str, Any] = {**entry.data, **entry.options}

    proxy_sensors = async_resolve_proxy_sensors(
        hass, config.get(CONF_DEVICE), config.get(CONF_SENSORS)
    )
    if not proxy_sensors:
        _LOGGER.warning(
            "%s: nessun sensore di distanza trovato, la posizione resterà sconosciuta",
            entry.title,
        )

    async_add_entities(
        [
            BLEFingerprintSensor(
                entry_id=entry.entry_id,
                name=config.get(CONF_NAME) or entry.title or DEFAULT_NAME,
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
            )
        ]
    )


class BLEFingerprintSensor(SensorEntity):
    """Stanza e coordinate stimate confrontando le distanze con la mappa."""

    _attr_should_poll = False
    _attr_icon = "mdi:map-marker-radius"

    def __init__(
        self,
        *,
        entry_id: str,
        name: str,
        proxy_sensors: list[str],
        confirmation_seconds: float,
        max_distance: float,
        top_k: int,
        min_common: int,
        unknown_timeout: float,
    ) -> None:
        self._entry_id = entry_id
        self._attr_name = name
        self._attr_unique_id = f"{entry_id}_position"
        self._proxy_sensors = proxy_sensors
        self._known_proxies = {proxy_key(entity_id) for entity_id in proxy_sensors}

        self._confirmation_seconds = confirmation_seconds
        self._max_distance = max_distance
        self._top_k = top_k
        self._min_common = min_common
        self._unknown_timeout = unknown_timeout

        self._attr_native_value = UNKNOWN_ROOM
        self._attr_extra_state_attributes = {
            "x": None,
            "y": None,
            "score": None,
            "proxies": 0,
        }
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

    async def async_added_to_hass(self) -> None:
        """Legge lo stato iniziale dei proxy e si iscrive ai cambiamenti."""
        for proxy in self._proxy_sensors:
            val = _parse_distance(self.hass.states.get(proxy), self._max_distance)
            if val is not None:
                self._current_distances[proxy] = val

        self.hass.data[DOMAIN]["entities"][self._entry_id] = self

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
        self.hass.data.get(DOMAIN, {}).get("entities", {}).pop(self._entry_id, None)

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

        rooms = self.hass.data.get(DOMAIN, {}).get("map_data", {}).get("rooms", {})
        if not rooms:
            return

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

        # --- Aggiorna le coordinate subito (movimento fluido sulla mappa) ---
        new_attrs = {
            "x": estimate.x,
            "y": estimate.y,
            "score": estimate.score,
            "proxies": len(current),
        }
        attrs_changed = new_attrs != self._attr_extra_state_attributes
        self._attr_extra_state_attributes = new_attrs

        # --- Filtro di conferma per la stanza ---
        room_changed = False
        if estimate.room == self._attr_native_value:
            # Siamo tornati alla stanza corrente: annulla qualsiasi cambio pendente
            self._cancel_confirmation()
            self._pending_room = None

        elif self._confirmation_seconds <= 0:
            # Filtro di conferma disattivato: cambio immediato
            self._cancel_confirmation()
            self._pending_room = None
            self._attr_native_value = estimate.room
            room_changed = True

        elif estimate.room != self._pending_room:
            # Nuova stanza diversa dalla corrente e dalla pendente: (ri)avvia il timer
            self._pending_room = estimate.room
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
        self._attr_extra_state_attributes = {
            "x": None,
            "y": None,
            "score": None,
            "proxies": 0,
        }
        self.async_write_ha_state()
