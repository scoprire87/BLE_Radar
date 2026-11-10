"""Costanti per l'integrazione BLE Fingerprint."""

DOMAIN = "ble_fingerprint"
CONF_TRACKER_ENTITY = "tracker_entity"

STORAGE_KEY = "ble_fingerprint_map"
STORAGE_VERSION = 1

SERVICE_SAVE_VERTEX = "save_vertex"

# Distanze (m) pari o superiori a questa soglia vengono ignorate
MAX_DISTANCE = 10.0

# Secondi di conferma prima di cambiare stanza
CONFIRMATION_SECONDS = 5

UNKNOWN_ROOM = "Sconosciuta"

SIGNAL_MAP_UPDATED = f"{DOMAIN}_map_updated"
