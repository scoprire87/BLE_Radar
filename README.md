# BLE Fingerprint Positioning

Integrazione custom per Home Assistant che stima la **stanza** e le **coordinate (x, y)**
di un dispositivo confrontando le distanze BLE misurate dai proxy (es. sensori
`sensor.<dispositivo>_distance_to_<proxy>` di Bermuda) con una mappa di punti di
riferimento ("vertici") registrati in precedenza.

## Installazione

1. HACS → Integrazioni → Repository personalizzati → aggiungi questo repository.
2. Installa **BLE Fingerprint Positioning** e riavvia Home Assistant.
3. Impostazioni → Dispositivi e servizi → Aggiungi integrazione → *BLE Fingerprint Positioning*.

> Nota: l'elenco dei sensori di distanza e il nome del sensore sono attualmente
> definiti in `custom_components/ble_fingerprint/sensor.py`.

## Creare la mappa

Posizionati in un punto della stanza e chiama il servizio `ble_fingerprint.save_vertex`
con le distanze lette in quel momento:

```yaml
action: ble_fingerprint.save_vertex
data:
  room: Cucina
  vertex_id: cucina_1
  x: 3.5
  y: 1.2
  distances:
    sensor.samsung_ale_distance_to_cucina: 1.2
    sensor.samsung_ale_distance_to_salotto: 4.8
```

Le distanze ≥ 10 m vengono scartate. Salvare un vertice con lo stesso `vertex_id`
nella stessa stanza lo sovrascrive. La mappa è salvata in `.storage/ble_fingerprint_map`.

## Come funziona

- Per ogni vertice si calcola la distanza tra le misure attuali e quelle salvate,
  considerando solo i proxy in comune.
- La stanza è quella del vertice più simile; il cambio di stanza viene confermato
  solo se la nuova stanza resta la migliore per 5 secondi consecutivi.
- Le coordinate sono la media pesata (IDW) dei 3 vertici più simili e vengono
  aggiornate subito (attributi `x` e `y`).
