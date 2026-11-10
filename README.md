# BLE Fingerprint Positioning

Integrazione custom per Home Assistant che stima la **stanza** e le **coordinate (x, y)**
di un dispositivo confrontando le distanze BLE misurate dai proxy (es. i sensori
`sensor.<dispositivo>_distance_to_<proxy>` di Bermuda) con una mappa di punti di
riferimento ("vertici") registrati in precedenza.

## Installazione

1. HACS → Integrazioni → Repository personalizzati → aggiungi questo repository.
2. Installa **BLE Fingerprint Positioning** e riavvia Home Assistant.
3. Impostazioni → Dispositivi e servizi → Aggiungi integrazione → *BLE Fingerprint Positioning*.
4. Scegli un nome e il **dispositivo** da tracciare: vengono usati automaticamente
   tutti i suoi sensori `..._distance_to_<proxy>` attivi. In alternativa (o in
   aggiunta) puoi selezionare a mano i sensori di distanza.

Puoi aggiungere l'integrazione più volte per tracciare più dispositivi: la mappa è
condivisa, perché i proxy vengono riconosciuti dalla parte finale dell'entity_id
(`sensor.telefono_distance_to_cucina` → `cucina`).

> Se aggiungi un nuovo proxy, ricarica l'integrazione per includere il nuovo sensore.

## Creare la mappa

Mettiti in un punto della stanza, resta fermo e chiama `ble_fingerprint.record_vertex`:
le distanze vengono lette da sole (mediana su `duration` secondi).

```yaml
action: ble_fingerprint.record_vertex
data:
  room: Cucina
  vertex_id: cucina_1
  x: 3.5        # opzionale
  y: 1.2        # opzionale
  duration: 5   # secondi di campionamento (0 = lettura istantanea)
  # entity_id: sensor.posizione_alessandro   # solo se hai più sensori di posizione
```

Registra più vertici per stanza (angoli, centro, punti dove sosti di solito).

| Servizio | Cosa fa |
|---|---|
| `record_vertex` | Registra un vertice leggendo le distanze attuali |
| `save_vertex` | Salva un vertice passando le distanze a mano (`distances: {sensore: metri}`) |
| `delete_vertex` | Elimina un vertice (`room`, `vertex_id`) |
| `delete_room` | Elimina una stanza con tutti i suoi vertici |
| `get_map` | Restituisce la mappa salvata (risposta del servizio) |

Salvare un vertice con lo stesso `vertex_id` nella stessa stanza lo sovrascrive.
La mappa è salvata in `.storage/ble_fingerprint_map`.

## Il sensore

- **Stato**: la stanza stimata, oppure `Sconosciuta`.
- **Attributi**: `x`, `y` (coordinate stimate), `score` (scarto in metri dal vertice
  più simile: più è basso, più la stima è affidabile), `proxies` (proxy validi ora).

## Come funziona

1. Per ogni vertice si calcola lo scarto medio (RMS, in metri) tra le distanze
   attuali e quelle salvate. Un proxy visto da una sola parte conta come se fosse
   alla distanza massima: sentire vicino un proxy che il vertice non vedeva è un
   indizio *contro* quel vertice.
2. Si prendono i *k* vertici più simili: la **stanza** è scelta con un voto pesato
   (peso = 1 / scarto), le **coordinate** sono la loro media pesata.
3. Le coordinate si aggiornano subito; la stanza cambia solo se la nuova resta la
   migliore per il tempo di conferma.
4. Se per un certo tempo non c'è alcun proxy valido, la posizione torna `Sconosciuta`.

## Opzioni

Da *Configura* sull'integrazione:

| Opzione | Predefinito | Descrizione |
|---|---|---|
| Conferma cambio stanza | 5 s | Tempo per cui la nuova stanza deve restare la migliore (0 = immediato) |
| Ritorno a «Sconosciuta» | 60 s | Tempo senza alcun proxy valido (0 = mai) |
| Distanza massima | 10 m | Distanze pari o superiori vengono ignorate |
| Vertici più vicini (k) | 3 | Vertici usati per stanza e coordinate |
| Proxy in comune minimi | 2 | Proxy che misura e vertice devono condividere |

## Limiti noti

- Un proxy spento o offline viene trattato come "lontano": finché non torna, le
  stanze vicine a quel proxy risultano penalizzate.
- La precisione dipende dalla quantità e qualità dei vertici registrati.

## Sviluppo

```bash
pip install pytest-homeassistant-custom-component
pytest
```
