"""Test del motore combinato: fingerprint + trilaterazione + Kalman."""
import math
import random

from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from datetime import timedelta

from custom_components.ble_fingerprint import geometry as g
from custom_components.ble_fingerprint.const import DOMAIN

# casa 8 x 6 m: proxy con posizione e attenuazione diverse (scatole a muro)
PROXIES = {
    "cucina": (0.3, 0.3, 1.0),
    "salotto": (7.7, 0.5, 1.3),
    "camera": (7.5, 5.7, 0.9),
    "bagno": (0.4, 5.5, 1.2),
    "corridoio": (4.0, 3.0, 1.0),
}
ROOMS = {"Cucina": (0, 4), "Salotto": (4, 8)}  # stanza in base alla x


def _reading(x, y, rng, noise=0.05):
    return {
        f"sensor.telefono_distance_to_{name}": round(
            f * math.hypot(x - px, y - py) * rng.uniform(1 - noise, 1 + noise), 2
        )
        for name, (px, py, f) in PROXIES.items()
        if f * math.hypot(x - px, y - py) < 10
    }


def _room(x):
    return next(r for r, (a, b) in ROOMS.items() if a <= x < b)


def test_fit_proxy_recovers_position_and_factor():
    rng = random.Random(3)
    samples = []
    for x in (1, 3, 5, 7):
        for y in (1, 3, 5):
            true = math.hypot(x - 6, y - 2)
            wall = 1.5 if rng.random() < 0.2 else 0.0  # i muri allungano soltanto
            samples.append((x, y, 1.4 * true * rng.uniform(0.95, 1.05) + wall))
    est = g.fit_proxy(samples)
    assert est is not None
    assert math.hypot(est.x - 6, est.y - 2) < 0.6
    assert abs(est.factor - 1.4) < 0.2

    # vertici allineati: posizione non determinabile
    assert g.fit_proxy([(x, 0.0, 2.0) for x in range(6)]) is None
    assert g.fit_proxy(samples[:3]) is None


def test_trilaterate_bounds_and_jump_weight():
    circles = [(0, 0, 5, 1), (8, 0, 5, 1), (4, 6, 3, 1)]
    fix = g.trilaterate(circles)
    assert math.hypot(fix.x - 4, fix.y - 3) < 0.1 and fix.rms < 0.1
    # limitata all'area della mappa
    far = g.trilaterate([(0, 0, 30, 1), (1, 0, 30, 1), (0, 1, 30, 1)], bounds=(0, 0, 5, 5))
    assert 0 <= far.x <= 5 and 0 <= far.y <= 5
    assert g.trilaterate(circles[:2]) is None
    assert g.jump_weight(2.0, None) == 1.0
    assert g.jump_weight(2.0, 2.0) == 1.0
    assert g.jump_weight(6.0, 2.0) < 0.1


def test_kalman_smooths_and_resets():
    rng = random.Random(5)
    kf = g.PositionFilter()
    raw_err = flt_err = 0.0
    for i in range(80):
        z = (3 + rng.gauss(0, 1), 2 + rng.gauss(0, 1))
        fx, fy = kf.update(z, i * 2.0, "Terra")
        if i > 10:
            raw_err += math.hypot(z[0] - 3, z[1] - 2)
            flt_err += math.hypot(fx - 3, fy - 2)
    assert flt_err < raw_err * 0.85
    # cambio piano o lunga assenza: nessuna inerzia dal passato
    assert kf.update((10, 10), 162.0, "Taverna") == (10, 10)
    assert kf.update((20, 20), 1000.0, "Taverna") == (20, 20)


async def test_combined_engine(hass, freezer):
    rng = random.Random(7)
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        title="Posizione Telefono",
        data={"name": "Posizione Telefono", "device_id": None,
              "sensors": [f"sensor.telefono_distance_to_{n}" for n in PROXIES]},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # mappa: vertici ogni 1,5 m con coordinate in centimetri (scala 100)
    hass.config_entries.async_update_entry(entry, options={"map_scale": 100})
    await hass.async_block_till_done()
    n = 0
    for x in (0.75, 2.25, 3.75, 5.25, 6.75):
        for y in (0.75, 2.25, 3.75, 5.25):
            n += 1
            await hass.services.async_call(
                DOMAIN, "save_vertex",
                {"room": _room(x), "vertex_id": f"v{n}", "x": x * 100, "y": y * 100,
                 "floor": "Terra", "distances": _reading(x, y, rng)},
                blocking=True,
            )

    entity = "sensor.posizione_telefono"
    diag = (await hass.services.async_call(DOMAIN, "get_map", {}, blocking=True, return_response=True))
    proxies = diag["estimated_proxies"][entity]["Terra"]
    assert set(proxies) == set(PROXIES)
    for name, (px, py, _f) in PROXIES.items():
        assert math.hypot(proxies[name]["x"] / 100 - px, proxies[name]["y"] / 100 - py) < 1.2, name

    # il telefono si ferma in punti NON registrati: la posizione combinata è precisa
    errors = []
    for tx, ty in ((1.5, 1.2), (3.0, 4.5), (5.8, 1.7), (6.6, 4.9), (4.4, 2.6)):
        for _ in range(6):  # alcune letture da fermo
            for eid, d in _reading(tx, ty, rng, noise=0.1).items():
                hass.states.async_set(eid, str(d))
            await hass.async_block_till_done()
            freezer.tick(timedelta(seconds=2))
            async_fire_time_changed(hass, dt_util.utcnow())
            await hass.async_block_till_done()
        state = hass.states.get(entity)
        assert state.attributes["method"] == "combinato"
        assert state.attributes["floor"] == "Terra"
        assert state.attributes["trilateration_proxies"] >= 3
        assert 0 < state.attributes["confidence"] <= 1
        errors.append(math.hypot(state.attributes["x"] / 100 - tx, state.attributes["y"] / 100 - ty))
        freezer.tick(timedelta(seconds=10))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
        assert hass.states.get(entity).state == _room(tx)
    errors.sort()
    assert errors[len(errors) // 2] < 0.8, errors
    assert max(errors) < 1.5, errors

    # trilaterazione disattivata: solo fingerprint
    hass.config_entries.async_update_entry(
        entry, options={"map_scale": 100, "trilateration": False, "smoothing": False}
    )
    await hass.async_block_till_done()
    for eid, d in _reading(2.0, 2.0, rng).items():
        hass.states.async_set(eid, str(d + 0.01))
    await hass.async_block_till_done()
    state = hass.states.get(entity)
    assert state.attributes["method"] == "fingerprint"
    assert state.attributes["trilateration_proxies"] == 0
