"""Logica pura di confronto tra misure attuali e mappa (nessuna dipendenza da HA)."""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

DISTANCE_TO_MARKER = "_distance_to_"


def proxy_key(entity_id: str) -> str:
    """Chiave del proxy ricavata dall'entity_id del sensore di distanza.

    `sensor.telefono_distance_to_cucina` -> `cucina`. In questo modo la stessa
    mappa vale per dispositivi diversi. Se l'entity_id non segue questo schema
    viene usato così com'è.
    """
    if DISTANCE_TO_MARKER in entity_id:
        return entity_id.split(DISTANCE_TO_MARKER, 1)[1]
    return entity_id


def normalize_distances(distances: dict[str, float]) -> dict[str, float]:
    """Converte {entity_id: distanza} in {proxy: distanza} (minimo se duplicati)."""
    result: dict[str, float] = {}
    for entity_id, value in distances.items():
        key = proxy_key(entity_id)
        if key not in result or value < result[key]:
            result[key] = value
    return result


@dataclass(frozen=True)
class ScoredVertex:
    """Vertice della mappa con il relativo scarto rispetto alla misura attuale."""

    room: str
    vertex_id: str
    dist: float
    x: float | None
    y: float | None


@dataclass(frozen=True)
class Estimate:
    """Risultato della stima di posizione."""

    room: str
    x: float | None
    y: float | None
    score: float


def score_vertices(
    current: dict[str, float],
    rooms: dict[str, dict[str, Any]],
    *,
    known_proxies: set[str] | None = None,
    max_distance: float = 10.0,
    min_common: int = 2,
) -> list[ScoredVertex]:
    """Calcola lo scarto (RMS, in metri) tra la misura attuale e ogni vertice.

    - `current` e le distanze salvate sono confrontate per chiave proxy.
    - Un proxy presente da una sola parte è trattato come se fosse a
      `max_distance`: vedere un proxy vicino che il vertice non vedeva (o
      viceversa) è un indizio contro quel vertice, non un dato da ignorare.
    - `known_proxies` limita il confronto ai proxy che questo dispositivo può
      effettivamente misurare.
    - Servono almeno `min_common` proxy in comune (meno se il vertice o la
      misura ne hanno di meno in totale).
    """
    scored: list[ScoredVertex] = []

    if not current:
        return scored

    for room_name, vertices in rooms.items():
        for vertex_id, data in vertices.items():
            saved = normalize_distances(
                {
                    k: float(v)
                    for k, v in (data.get("distances") or {}).items()
                    if isinstance(v, (int, float))
                }
            )
            if known_proxies is not None:
                saved = {k: v for k, v in saved.items() if k in known_proxies}
            if not saved:
                continue

            common = current.keys() & saved.keys()
            required = max(1, min(min_common, len(saved), len(current)))
            if len(common) < required:
                continue

            union = current.keys() | saved.keys()
            squared = sum(
                (current.get(p, max_distance) - saved.get(p, max_distance)) ** 2
                for p in union
            )
            coords = data.get("coords") or {}
            scored.append(
                ScoredVertex(
                    room=room_name,
                    vertex_id=vertex_id,
                    dist=math.sqrt(squared / len(union)),
                    x=coords.get("x"),
                    y=coords.get("y"),
                )
            )

    scored.sort(key=lambda v: v.dist)
    return scored


def estimate_position(scored: list[ScoredVertex], top_k: int = 3) -> Estimate | None:
    """Stima stanza e coordinate dai `top_k` vertici più simili.

    La stanza è scelta con un voto pesato (peso = 1 / scarto) tra i top_k; le
    coordinate sono la media pesata (IDW) dei top_k che hanno coordinate.
    """
    if not scored:
        return None

    top = scored[: max(1, top_k)]

    votes: dict[str, float] = {}
    sum_weights = sum_x = sum_y = 0.0
    for vertex in top:
        weight = 1.0 / (vertex.dist + 0.0001)
        votes[vertex.room] = votes.get(vertex.room, 0.0) + weight
        if vertex.x is not None and vertex.y is not None:
            sum_weights += weight
            sum_x += vertex.x * weight
            sum_y += vertex.y * weight

    # a parità di voti vince la stanza del vertice più vicino (ordine di `top`)
    best_room = max(votes, key=lambda room: votes[room])

    return Estimate(
        room=best_room,
        x=round(sum_x / sum_weights, 2) if sum_weights > 0 else None,
        y=round(sum_y / sum_weights, 2) if sum_weights > 0 else None,
        score=round(top[0].dist, 2),
    )


def vertex_floor(data: dict[str, Any]) -> str | None:
    """Piano di un vertice (None se non indicato)."""
    floor = data.get("floor")
    return str(floor) if floor not in (None, "") else None


def room_floors(rooms: dict[str, dict[str, Any]]) -> dict[str, str | None]:
    """Piano di ogni stanza: quello più frequente tra i suoi vertici."""
    result: dict[str, str | None] = {}
    for room, vertices in rooms.items():
        counts: dict[str | None, int] = {}
        for data in vertices.values():
            floor = vertex_floor(data)
            counts[floor] = counts.get(floor, 0) + 1
        # a parità vince un piano indicato rispetto a "nessun piano"
        result[room] = max(counts, key=lambda f: (counts[f], f is not None)) if counts else None
    return result


def vertices_by_floor(
    rooms: dict[str, dict[str, Any]], scale: float = 1.0
) -> dict[str | None, list[tuple[float, float, dict[str, float]]]]:
    """Vertici con coordinate, raggruppati per piano, in metri.

    Restituisce {piano: [(x_m, y_m, {proxy: distanza})]}.
    """
    result: dict[str | None, list[tuple[float, float, dict[str, float]]]] = {}
    for vertices in rooms.values():
        for data in vertices.values():
            coords = data.get("coords") or {}
            x, y = coords.get("x"), coords.get("y")
            if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
                continue
            distances = normalize_distances(
                {
                    k: float(v)
                    for k, v in (data.get("distances") or {}).items()
                    if isinstance(v, (int, float))
                }
            )
            if distances:
                result.setdefault(vertex_floor(data), []).append(
                    (x / scale, y / scale, distances)
                )
    return result
