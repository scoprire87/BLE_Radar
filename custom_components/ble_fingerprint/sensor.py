import logging
import math
from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback):
    """Configura il sensore BLE Fingerprint tramite l'interfaccia UI (Config Flow)."""
    
    # Sostituisci questi con gli entity_id reali che Bermuda genera per le distanze del tuo telefono
    proxy_sensors = [
        "sensor.distanza_shelly_salotto_alessandro",
        "sensor.distanza_shelly_corridoio_alessandro",
        "sensor.distanza_shelly_studio_alessandro"
    ]
    
    sensor = BLEFingerprintSensor(hass, "Posizione Alessandro Fingerprint", proxy_sensors)
    async_add_entities([sensor])

class BLEFingerprintSensor(SensorEntity):
    """Sensore che calcola la stanza basandosi sul fingerprinting vettoriale e la distanza euclidea."""

    def __init__(self, hass: HomeAssistant, name: str, proxy_sensors: list):
        self.hass = hass
        self._attr_name = name
        self._proxy_sensors = proxy_sensors
        self._attr_native_value = "Sconosciuta"
        self._current_distances = {}

    async def async_added_to_hass(self):
        """Quando il sensore viene avviato, inizia ad ascoltare in background i cambiamenti degli Shelly."""
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, self._proxy_sensors, self._async_distance_changed
            )
        )

    async def _async_distance_changed(self, event):
        """Si attiva istantaneamente ogni volta che un proxy aggiorna una distanza."""
        entity_id = event.data.get("entity_id")
        new_state = event.data.get("new_state")

        if new_state is None or new_state.state in ['unknown', 'unavailable']:
            return

        try:
            # Aggiorna il valore nel nostro vettore delle distanze correnti
            self._current_distances[entity_id] = float(new_state.state)
            self._calculate_room()
        except ValueError:
            pass

    def _calculate_room(self):
        """Il motore matematico: confronta il vettore attuale con i vertici salvati nel file .storage."""
        map_data = self.hass.data.get(DOMAIN, {}).get("map_data", {}).get("rooms", {})
        
        if not map_data or len(self._current_distances) < len(self._proxy_sensors):
            # Non abbiamo ancora mappato le stanze o mancano i dati di alcuni Shelly
            return

        best_room = "Sconosciuta"
        min_distance = float('inf')

        # Itera su tutte le stanze e i loro vertici salvati
        for room_name, vertices in map_data.items():
            for vertex_id, saved_distances in vertices.items():
                
                # Calcola la Distanza Euclidea nello spazio N-dimensionale
                euclidean_dist = 0.0
                for proxy in self._proxy_sensors:
                    d_current = self._current_distances.get(proxy, 10.0) # 10m di default se il segnale è perso
                    d_saved = saved_distances.get(proxy, 10.0)
                    
                    euclidean_dist += (d_current - d_saved) ** 2
                
                euclidean_dist = math.sqrt(euclidean_dist)

                # Trova il vertice con la "firma radio" più simile alla posizione attuale
                if euclidean_dist < min_distance:
                    min_distance = euclidean_dist
                    best_room = room_name

        # Se la stanza vincente è diversa dalla precedente, aggiorna lo stato in Home Assistant
        if self._attr_native_value != best_room:
            self._attr_native_value = best_room
            self.async_write_ha_state()

    @property
    def native_value(self):
        """Ritorna lo stato finale che vedrai nella dashboard."""
        return self._attr_native_value
