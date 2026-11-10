# BLE Fingerprint Positioning

Integrazione custom per Home Assistant che stima la **stanza**, il **piano** e le
**coordinate (x, y)** di un dispositivo a partire dalle distanze BLE misurate dai proxy
(es. i sensori `sensor.<dispositivo>_distance_to_<proxy>` di Bermuda, anche con proxy
Shelly).

Combina due metodi:
- **fingerprint**: confronto con una mappa di punti di riferimento ("vertici")
  registrati in precedenza, robusto a muri e proxy montati in scatole a muro;
- **trilaterazione**: la posizione e la correzione di ogni proxy vengono **stimate da
  sole** dai vertici registrati con coordinate, senza disegnare una planimetria.

Non richiede librerie esterne (niente numpy/scipy).

## Installazione

1. HACS → Integrazioni → Repository personalizzati → aggiungi questo repository.
2. Installa **BLE Fingerprint Positioning** e riavvia Home Assistant.
3. Impostazioni → Dispositivi e servizi → Aggiungi integrazione → *BLE Fingerprint Positioning*.
4. Scegli il **dispositivo** da tracciare: vengono usati automaticamente tutti i suoi
   sensori `..._distance_to_<proxy>` attivi e il nome diventa «Posizione <dispositivo>».
   Nella sezione *Avanzate* puoi aggiungere sensori di distanza a mano.

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
  x: 3.5          # coordinate: opzionali ma necessarie per la trilaterazione
  y: 1.2
  floor: Terra    # opzionale, solo se hai più piani
  duration: 15    # secondi di campionamento (0 = lettura istantanea)
  # entity_id: sensor.posizione_alessandro   # solo se hai più sensori di posizione
```

Consigli:
- registra più vertici per stanza (angoli, centro, punti dove sosti di solito),
  tenendo il telefono come lo porti di solito;
- con proxy Shelly usa `duration: 15` o più, per filtrare il rumore;
- per la **trilaterazione** dai le coordinate ai vertici (un unico sistema di
  riferimento per piano, ad esempio in metri con l'origine in un angolo della casa):
  ogni proxy deve essere visto da almeno 4 vertici con coordinate, non tutti in fila.

| Servizio | Cosa fa |
|---|---|
| `record_vertex` | Registra un vertice leggendo le distanze attuali |
| `save_vertex` | Salva un vertice passando le distanze a mano (`distances: {sensore: metri}`) |
| `delete_vertex` | Elimina un vertice (`room`, `vertex_id`) |
| `delete_room` | Elimina una stanza con tutti i suoi vertici |
| `set_proxy` | Imposta a mano posizione (ed eventualmente correzione) di un proxy; senza `x`/`y` rimuove l'impostazione |
| `get_map` | Restituisce la mappa, i proxy impostati a mano e quelli **stimati** (posizione, fattore di correzione, residuo) |

Salvare un vertice con lo stesso `vertex_id` nella stessa stanza lo sovrascrive.
La mappa è salvata in `.storage/ble_fingerprint_map`.

## Il sensore

- **Stato**: la stanza stimata, oppure `Sconosciuta`.
- **Attributi**:
  - `x`, `y`: coordinate stimate (nelle unità della mappa);
  - `floor`: piano;
  - `confidence`: affidabilità della stima, da 0 a 1;
  - `method`: `fingerprint`, `trilaterazione` o `combinato`;
  - `score`: scarto in metri dal vertice più simile (più è basso, meglio è);
  - `proxies`: proxy validi ora; `trilateration_proxies` e `trilateration_rms`:
    proxy usati dalla trilaterazione e residuo in metri.

Il sensore compare sotto un proprio dispositivo, collegato a quello tracciato.

## Come funziona

1. **Fingerprint.** Per ogni vertice si calcola lo scarto medio (RMS, in metri) tra
   le distanze attuali e quelle salvate. Un proxy visto da una sola parte conta come
   se fosse alla distanza massima. Tra i *k* vertici più simili la **stanza** è scelta
   con un voto pesato e le coordinate sono la loro media pesata. La stanza determina
   anche il **piano**.
2. **Proxy stimati dalla mappa.** Dai vertici con coordinate si ricava, per ogni piano,
   dove si trova ogni proxy e il suo **fattore di correzione** (distanza misurata /
   distanza reale), che assorbe l'attenuazione di quel proxy. Il fit usa una perdita
   asimmetrica: i muri possono solo allungare le distanze. Le misure sono quelle del
   tuo telefono, quindi la correzione è calibrata proprio sul dispositivo tracciato.
3. **Trilaterazione.** Con almeno 3 proxy stimati, la posizione è il fit ai minimi
   quadrati delle distanze corrette (peso 1/r², i proxy vicini contano di più),
   partendo dal punto del fingerprint e limitato all'area della mappa. Le distanze
   cambiate bruscamente vengono attenuate invece che scartate.
4. **Fusione.** Le due stime vengono mediate in base alla loro qualità (residuo e
   numero di proxy).
5. **Movimento fluido.** Un filtro di Kalman a velocità costante riduce i salti; si
   azzera dopo un'assenza o un cambio di piano.
6. La stanza cambia solo se la nuova resta la migliore per il tempo di conferma; se
   per un certo tempo non c'è alcun proxy valido, la posizione torna `Sconosciuta`.

## Opzioni

Da *Configura* sull'integrazione:

| Opzione | Predefinito | Descrizione |
|---|---|---|
| Conferma cambio stanza | 5 s | Tempo per cui la nuova stanza deve restare la migliore (0 = immediato) |
| Ritorno a «Sconosciuta» | 60 s | Tempo senza alcun proxy valido (0 = mai) |
| Movimento fluido | attivo | Filtro di Kalman sulle coordinate |

Nella sezione **Avanzate**:

| Opzione | Predefinito | Descrizione |
|---|---|---|
| Sensori di distanza aggiuntivi | — | In aggiunta o al posto di quelli del dispositivo |
| Trilaterazione | attiva | Usa i proxy stimati dalla mappa per affinare le coordinate |
| Unità della mappa per metro | 1 | 1 se le coordinate sono in metri, 100 se in centimetri, ecc. |
| Distanza massima | 10 m | Distanze pari o superiori vengono ignorate |
| Vertici più vicini (k) | 3 | Vertici usati per stanza e coordinate |
| Proxy in comune minimi | 2 | Proxy che misura e vertice devono condividere |

## Limiti noti

- Un proxy spento o offline viene trattato come "lontano": finché non torna, le
  stanze vicine a quel proxy risultano penalizzate.
- La precisione dipende dalla quantità e qualità dei vertici registrati.
- La trilaterazione funziona solo con vertici dotati di coordinate; un proxy fuori
  dall'area dei vertici (es. in giardino) viene stimato con meno precisione: in quel
  caso puoi impostarne la posizione con `set_proxy`.
- Le coordinate di ogni piano sono indipendenti: usa lo stesso riferimento per tutti
  i vertici dello stesso piano.

## Riconoscimenti

Il motore di trilaterazione, il punteggio di qualità, il filtro di Kalman e la
correzione per proxy si ispirano a [BPS – BLE Positioning System](https://github.com/Hogster/BPS)
di Hogster (licenza MIT, vedi [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)).

## Sviluppo

```bash
pip install pytest-homeassistant-custom-component
pytest
```
