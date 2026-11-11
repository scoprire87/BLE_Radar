"""Test della logica pura di confronto."""
from custom_components.ble_fingerprint.fingerprint import (
    estimate_position,
    normalize_distances,
    proxy_key,
    score_vertices,
)

P = "sensor.tel_distance_to_"


def _vertex(distances, x=None, y=None):
    return {"distances": distances, "coords": {"x": x, "y": y}}


def test_proxy_key():
    assert proxy_key("sensor.phone_distance_to_cucina") == "cucina"
    assert proxy_key("sensor.altro") == "sensor.altro"
    assert normalize_distances({P + "a": 2.0, "sensor.x_distance_to_a": 1.0}) == {"a": 1.0}


def test_single_proxy_vertex_does_not_win():
    """Un vertice con un solo proxy simile non batte uno coerente su tutti."""
    current = {"cucina": 1.0, "salotto": 5.0, "studio": 3.0}
    rooms = {
        "Cucina": {"c1": _vertex({P + "cucina": 2.0, P + "salotto": 6.0, P + "studio": 4.0})},
        "Studio": {"s1": _vertex({P + "salotto": 5.3})},
    }
    scored = score_vertices(current, rooms, min_common=1)
    assert [v.room for v in scored] == ["Cucina", "Studio"]
    assert round(scored[0].dist, 2) == 1.0
    # con min_common=2 il vertice a un solo proxy resta valido (ne ha solo uno)
    # ma viene comunque penalizzato per i proxy mancanti
    assert estimate_position(score_vertices(current, rooms)).room == "Cucina"


def test_min_common_and_known_proxies():
    current = {"a": 1.0, "b": 2.0, "c": 3.0}
    rooms = {"R": {"v": _vertex({"a": 1.0, "x": 2.0, "y": 3.0})}}
    assert score_vertices(current, rooms, min_common=2) == []
    # limitando ai proxy noti il vertice ha un solo proxy utile: ammesso
    scored = score_vertices(current, rooms, known_proxies={"a", "b", "c"}, min_common=2)
    assert len(scored) == 1
    assert score_vertices({}, rooms) == []


def test_weighted_vote_and_coords():
    current = {"a": 1.0, "b": 5.0}
    rooms = {
        "A": {"a1": _vertex({"a": 1.1, "b": 5.0}, 0, 0)},
        "B": {
            "b1": _vertex({"a": 2.0, "b": 4.0}, 10, 0),
            "b2": _vertex({"a": 2.0, "b": 4.2}, 10, 2),
        },
    }
    est = estimate_position(score_vertices(current, rooms), top_k=3)
    assert est.room == "A"  # un vertice molto vicino batte due lontani
    assert 0 < est.x < 5 and est.score == 0.07

    # due vertici vicini della stessa stanza battono uno appena più vicino
    rooms["A"]["a1"] = _vertex({"a": 1.5, "b": 4.5}, 0, 0)
    rooms["B"]["b1"] = _vertex({"a": 1.6, "b": 4.4}, 10, 0)
    rooms["B"]["b2"] = _vertex({"a": 1.6, "b": 4.5}, 10, 2)
    scored = score_vertices(current, rooms)
    assert scored[0].room == "A"
    assert estimate_position(scored, top_k=3).room == "B"
    assert estimate_position(scored, top_k=1).room == "A"
    assert estimate_position([], 3) is None
