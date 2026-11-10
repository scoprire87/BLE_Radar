"""Test di base per BLE Fingerprint."""
from datetime import timedelta

from homeassistant.config_entries import ConfigEntryState
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.ble_fingerprint.const import DOMAIN

CUC = "sensor.samsung_ale_distance_to_cucina"
SAL = "sensor.samsung_ale_distance_to_salotto"
STU = "sensor.samsung_ale_distance_to_studio"


def _entity(hass):
    states = [s for s in hass.states.async_all("sensor") if s.entity_id.startswith("sensor.posizione")]
    assert len(states) == 1
    return states[0]


async def _move(hass, cuc, sal, stu="unavailable"):
    hass.states.async_set(CUC, str(cuc))
    hass.states.async_set(SAL, str(sal))
    hass.states.async_set(STU, str(stu))
    await hass.async_block_till_done()


async def _wait(hass, seconds):
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


async def test_position_and_room_change(hass, freezer):
    await _move(hass, 1.0, 5.0)
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert _entity(hass).state == "Sconosciuta"

    for room, vid, d, x, y in (
        ("Cucina", "c1", {CUC: 1.0, SAL: 5.0}, 1, 1),
        ("Cucina", "c2", {CUC: 1.5, SAL: 4.5}, 2, 1),
        ("Salotto", "s1", {CUC: 5.0, SAL: 1.0, STU: "12"}, 8, 1),
    ):
        await hass.services.async_call(
            DOMAIN, "save_vertex",
            {"room": room, "vertex_id": vid, "distances": d, "x": x, "y": y},
            blocking=True,
        )
    # la mappa ignora la distanza >= 10
    assert STU not in hass.data[DOMAIN]["map_data"]["rooms"]["Salotto"]["s1"]["distances"]

    # il salvataggio fa ricalcolare: coordinate subito, stanza dopo 5 s
    state = _entity(hass)
    assert state.attributes["x"] is not None and state.attributes["x"] < 2
    freezer.tick(timedelta(seconds=6))
    await _wait(hass, 6)
    assert _entity(hass).state == "Cucina"

    # spostamento in salotto: la stanza cambia solo dopo la conferma
    await _move(hass, 5.0, 1.0)
    assert _entity(hass).state == "Cucina"
    assert _entity(hass).attributes["x"] > 7
    freezer.tick(timedelta(seconds=6))
    await _wait(hass, 6)
    assert _entity(hass).state == "Salotto"

    # rimbalzo breve in cucina (< 5 s) non cambia stanza
    await _move(hass, 1.0, 5.0)
    freezer.tick(timedelta(seconds=2))
    await _wait(hass, 2)
    await _move(hass, 5.0, 1.0)
    freezer.tick(timedelta(seconds=6))
    await _wait(hass, 6)
    assert _entity(hass).state == "Salotto"

    # valori non validi ignorati senza errori
    await _move(hass, "abc", "nan", "-3")

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
