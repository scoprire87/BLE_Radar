import asyncio
import logging
import math

from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Secondi di conferma prima di cambiare stanza.
# La stanza nuova deve essere predetta ininterrottamente per questo tempo.
# Max ritardo effettivo = CONFIRMATION_SECONDS = 5s
CONFIRMATION_SECONDS = 5


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
):
    proxy_sensors = [
        "sensor.samsung_ale_distance_to_bagno",
        "sensor.samsung_ale_distance_to_bagno_taverna",
        "sensor.samsung_ale_distance_to_camerina",
        "sensor.samsung_ale_distance_to_consumo_pozzo_nero",
        "sensor.samsung_ale_distance_to_corridoio",
        "sensor.samsung_ale_distance_to_corridoio_1",
        "sensor.samsung_ale_distance_to_cucina",
        "sensor.samsung_ale_distance_to_gateway_bluetooth",
        "sensor.samsung_ale_distance_to_interruttore_corridoio",
        "sensor.samsung_ale_distance_to_laterali_taverna",
        "sensor.samsung_ale_distance_to_raspberry_ha",
        "sensor.samsung_ale_distance_to_salotto",
        "sensor.samsung_ale_distance_to_studio",
        "sensor.samsung_ale_distance_to_tenda",
        "sensor.samsung_ale_distance_to_veranda",
    ]
    sensor = BLEFingerprintSensor(hass, "Posizione Alessandro", proxy_sensors)
    async_add_entities([sensor])


class BLEFingerprintSensor(SensorEntity):

    def __init__(self, hass: HomeAssistant, name: str, proxy_sensors: list):
        self.hass = hass
        self._attr_name = name
        self._proxy_sensors = proxy_sensors
        self._attr_native_value = "Sconosciuta"
        self._attr_extra_state_attributes = {"x": None, "y": None}
        self._current_distances = {}

        # --- Filtro di stabilizzazione ---
        # Stanza candidata al cambio (in attesa di conferma)
        self._pending_room: str | None = None
        # Task asyncio che gestisce il timer di conferma
        self._confirmation_task: asyncio.Task | None = None

    async def async_added_to_hass(self):
        # Legge lo stato iniziale di tutti i proxy
        for proxy in self._proxy_sensors:
            state_obj = self.hass.states.get(proxy)
            if state_obj and state_obj.state not in ["unknown", "unavailable"]:
                try:
                    val = float(state_obj.state)
                    if val < 10.0:
                        self._current_distances[proxy] = val
                except ValueError:
                    pass

        # Cancella il task di conferma quando l'entità viene rimossa
        def _cancel_task():
            if self._confirmation_task and not self._confirmation_task.done():
                self._confirmation_task.cancel()

        self.async_on_remove(_cancel_task)

        self.async_on_remove(
            async_track_state_change_event(
                self.hass, self._proxy_sensors, self._async_distance_changed
            )
        )

    async def _async_distance_changed(self, event):
        entity_id = event.data.get("entity_id")
        new_state = event.data.get("new_state")

        if new_state is None or new_state.state in ["unknown", "unavailable"]:
            self._current_distances.pop(entity_id, None)
        else:
            try:
                val = float(new_state.state)
                if val < 10.0:
                    self._current_distances[entity_id] = val
                else:
                    self._current_distances.pop(entity_id, None)
            except ValueError:
                self._current_distances.pop(entity_id, None)

        self._calculate_position()

    def _calculate_position(self):
        map_data = (
            self.hass.data.get(DOMAIN, {}).get("map_data", {}).get("rooms", {})
        )
        if not map_data:
            return

        scored_vertices = []

        for room_name, vertices in map_data.items():
            for vertex_id, data in vertices.items():
                saved_dist = data.get("distances", {})
                coords = data.get("coords", {})

                # Matrice sparsa: considera solo i proxy in comune
                common_proxies = (
                    set(self._current_distances.keys()) & set(saved_dist.keys())
                )
                if len(common_proxies) == 0:
                    continue

                # Distanza Euclidea normalizzata sul numero di proxy comuni
                euclidean_dist = math.sqrt(
                    sum(
                        (self._current_distances[s] - saved_dist[s]) ** 2
                        for s in common_proxies
                    )
                )
                normalized_dist = euclidean_dist / len(common_proxies)

                scored_vertices.append(
                    {
                        "room": room_name,
                        "dist": normalized_dist,
                        "x": coords.get("x"),
                        "y": coords.get("y"),
                    }
                )

        if not scored_vertices:
            return

        # k-NN: ordina e prendi i 3 vertici più vicini
        scored_vertices.sort(key=lambda v: v["dist"])
        top_k = scored_vertices[:3]

        best_room = top_k[0]["room"]

        # IDW per le coordinate
        sum_weights = 0.0
        sum_x = 0.0
        sum_y = 0.0
        for v in top_k:
            if v["x"] is not None and v["y"] is not None:
                weight = 1.0 / (v["dist"] + 0.0001)
                sum_weights += weight
                sum_x += v["x"] * weight
                sum_y += v["y"] * weight

        calc_x = round(sum_x / sum_weights, 2) if sum_weights > 0 else None
        calc_y = round(sum_y / sum_weights, 2) if sum_weights > 0 else None

        # --- Aggiorna le coordinate subito (movimento fluido sulla mappa) ---
        coords_changed = (
            self._attr_extra_state_attributes.get("x") != calc_x
            or self._attr_extra_state_attributes.get("y") != calc_y
        )
        if coords_changed:
            self._attr_extra_state_attributes["x"] = calc_x
            self._attr_extra_state_attributes["y"] = calc_y
            self.async_write_ha_state()

        # --- Filtro di conferma per la stanza ---
        if best_room == self._attr_native_value:
            # Siamo tornati alla stanza corrente: annulla qualsiasi cambio pendente
            if self._confirmation_task and not self._confirmation_task.done():
                self._confirmation_task.cancel()
                self._confirmation_task = None
            self._pending_room = None

        elif best_room != self._pending_room:
            # Nuova stanza diversa dalla corrente e dalla pendente: (ri)avvia il timer
            self._pending_room = best_room
            if self._confirmation_task and not self._confirmation_task.done():
                self._confirmation_task.cancel()
            self._confirmation_task = self.hass.async_create_task(
                self._confirm_room(best_room)
            )
        # else: stessa stanza pendente già in attesa di conferma → timer in corso, non fare nulla

    async def _confirm_room(self, room: str):
        """Attende CONFIRMATION_SECONDS, poi conferma il cambio di stanza."""
        await asyncio.sleep(CONFIRMATION_SECONDS)
        if self._pending_room == room:
            _LOGGER.debug("BLE Radar: cambio stanza confermato → %s", room)
            self._attr_native_value = room
            self._pending_room = None
            self._confirmation_task = None
            self.async_write_ha_state()

    @property
    def native_value(self):
        return self._attr_native_value
