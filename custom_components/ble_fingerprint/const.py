"""Costanti per l'integrazione BLE Fingerprint."""

DOMAIN = "ble_fingerprint"

# --- Configurazione (config entry) ---
CONF_NAME = "name"
CONF_DEVICE = "device_id"
CONF_SENSORS = "sensors"

# --- Dispositivi tracciati (nelle opzioni della config entry) ---
CONF_TRACKERS = "trackers"
TRACKER_ID = "id"
TRACKER_UNIQUE_ID = "unique_id"

# --- Pannello e card ---
CONF_SHOW_PANEL = "show_panel"
DEFAULT_SHOW_PANEL = True
PANEL_URL_PATH = "ble-radar"
PANEL_TITLE = "BLE Radar"
PANEL_ICON = "mdi:radar"
STATIC_URL = "/ble_fingerprint_static"

# Punti della calibrazione guidata: distanza dagli angoli verso il centro (m)
GUIDED_CORNER_INSET_M = 0.5

# --- Opzioni ---
CONF_CONFIRMATION_SECONDS = "confirmation_seconds"
CONF_MAX_DISTANCE = "max_distance"
CONF_TOP_K = "top_k"
CONF_MIN_COMMON_PROXIES = "min_common_proxies"
CONF_UNKNOWN_TIMEOUT = "unknown_timeout"
CONF_SMOOTHING = "smoothing"
CONF_TRILATERATION = "trilateration"
CONF_MAP_SCALE = "map_scale"
# sezione comprimibile delle opzioni avanzate
SECTION_ADVANCED = "advanced"

DEFAULT_NAME = "Position"
# Secondi per cui la nuova stanza deve restare la migliore prima del cambio
DEFAULT_CONFIRMATION_SECONDS = 5
# Distanze (m) pari o superiori a questa soglia vengono ignorate
DEFAULT_MAX_DISTANCE = 10.0
# Numero di vertici più vicini usati per stanza e coordinate (k-NN)
DEFAULT_TOP_K = 3
# Numero minimo di proxy in comune tra misura attuale e vertice
DEFAULT_MIN_COMMON_PROXIES = 2
# Secondi senza alcun proxy valido prima di tornare a "Sconosciuta" (0 = mai)
DEFAULT_UNKNOWN_TIMEOUT = 60
# Filtro di Kalman sulle coordinate
DEFAULT_SMOOTHING = True
# Trilaterazione con i proxy stimati dalla mappa (fusa con il fingerprint)
DEFAULT_TRILATERATION = True
# Unità della mappa per metro (1 = coordinate in metri, 100 = centimetri, ...)
DEFAULT_MAP_SCALE = 1.0

# Compatibilità: soglia usata dal servizio save_vertex
MAX_DISTANCE = DEFAULT_MAX_DISTANCE

# Stanza sconosciuta: lo stato del sensore diventa "unknown", che Home
# Assistant mostra tradotto nella lingua dell'utente
UNKNOWN_ROOM = None

# --- Storage ---
STORAGE_KEY = "ble_fingerprint_map"
STORAGE_VERSION = 1

# --- Servizi ---
SERVICE_SAVE_VERTEX = "save_vertex"
SERVICE_RECORD_VERTEX = "record_vertex"
SERVICE_DELETE_VERTEX = "delete_vertex"
SERVICE_DELETE_ROOM = "delete_room"
SERVICE_GET_MAP = "get_map"
SERVICE_SET_PROXY = "set_proxy"

SIGNAL_MAP_UPDATED = f"{DOMAIN}_map_updated"
