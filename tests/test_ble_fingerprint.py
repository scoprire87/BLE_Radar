"""Test dell'integrazione BLE Fingerprint."""
import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.ble_fingerprint.const import DOMAIN, LEGACY_PROXY_SENSORS

CUC = "sensor.samsung_ale_distance_to_cucina"
SAL = "sensor.samsung_ale_distance_to_salotto"
STU = "sensor.samsung_ale_distance_to_studio"
ENTITY = "sensor.posizione_alessandro"


async def _move(hass, cuc, sal, stu="unavailable"):
    hass.states.async_set(CUC, str(cuc))
    hass.states.async_set(SAL, str(sal))
    hass.states.async_set(STU, str(stu))
    await hass.async_block_till_done()


async def _wait(hass, freezer, seconds):
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()


async def _call(hass, service, data=None, response=False):
    return await hass.services.async_call(
        DOMAIN, service, data or {}, blocking=True, return_response=response
    )


async def _setup_legacy(hass):
    """Config entry versione 1 (senza dati), come nelle installazioni esistenti."""
    entry = MockConfigEntry(domain=DOMAIN, data={}, version=1, title="BLE Radar Fingerprint")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


async def test_migration_keeps_legacy_setup(hass):
    entry = await _setup_legacy(hass)
    assert entry.version == 2
    assert entry.data["sensors"] == LEGACY_PROXY_SENSORS
    assert hass.states.get(ENTITY).state == "Sconosciuta"


async def test_position_room_change_and_unknown(hass, freezer):
    await _move(hass, 1.0, 5.0)
    entry = await _setup_legacy(hass)

    for room, vid, d, x, y in (
        ("Cucina", "c1", {CUC: 1.0, SAL: 5.0}, 1, 1),
        ("Cucina", "c2", {CUC: 1.5, SAL: 4.5}, 2, 1),
        ("Salotto", "s1", {CUC: 5.0, SAL: 1.0, STU: "12"}, 8, 1),
        ("Salotto", "s2", {CUC: 5.5, SAL: 1.5}, 9, 1),
    ):
        await _call(hass, "save_vertex", {"room": room, "vertex_id": vid, "distances": d, "x": x, "y": y})
    rooms = hass.data[DOMAIN]["map_data"]["rooms"]
    assert STU not in rooms["Salotto"]["s1"]["distances"]  # >= 10 m scartata

    # coordinate subito, stanza dopo la conferma
    state = hass.states.get(ENTITY)
    assert state.state == "Sconosciuta"
    assert state.attributes["x"] < 2 and state.attributes["proxies"] == 2
    await _wait(hass, freezer, 6)
    assert hass.states.get(ENTITY).state == "Cucina"

    # spostamento in salotto: coordinate subito, stanza solo dopo la conferma
    await _move(hass, 5.0, 1.0)
    state = hass.states.get(ENTITY)
    assert state.state == "Cucina" and state.attributes["x"] > 7
    await _wait(hass, freezer, 4)
    assert hass.states.get(ENTITY).state == "Cucina"
    await _wait(hass, freezer, 2)
    assert hass.states.get(ENTITY).state == "Salotto"

    # rimbalzo breve in cucina (< 5 s) non cambia stanza
    await _move(hass, 1.0, 5.0)
    await _wait(hass, freezer, 2)
    await _move(hass, 5.0, 1.0)
    await _wait(hass, freezer, 6)
    assert hass.states.get(ENTITY).state == "Salotto"

    # valori non validi ignorati; senza proxy dopo 60 s diventa sconosciuta
    await _move(hass, "abc", "nan", "-3")
    await _wait(hass, freezer, 30)
    assert hass.states.get(ENTITY).state == "Salotto"
    await _wait(hass, freezer, 31)
    state = hass.states.get(ENTITY)
    assert state.state == "Sconosciuta" and state.attributes["x"] is None

    # se il segnale torna prima del timeout la stanza non si perde
    await _move(hass, 5.0, 1.0)
    await _wait(hass, freezer, 6)
    assert hass.states.get(ENTITY).state == "Salotto"
    await _move(hass, "unknown", "unknown")
    await _wait(hass, freezer, 30)
    await _move(hass, 5.0, 1.0)
    await _wait(hass, freezer, 40)
    assert hass.states.get(ENTITY).state == "Salotto"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert hass.data[DOMAIN]["entities"] == {}


async def test_map_services(hass, freezer):
    await _move(hass, 1.2, 4.8)
    await _setup_legacy(hass)

    # registrazione istantanea
    result = await _call(
        hass, "record_vertex",
        {"room": "Cucina", "vertex_id": "c1", "x": 1, "y": 2, "duration": 0},
        response=True,
    )
    assert result["distances"] == {"cucina": 1.2, "salotto": 4.8}
    assert result["coords"] == {"x": 1.0, "y": 2.0} and result["samples"] == 1
    await _wait(hass, freezer, 6)
    assert hass.states.get(ENTITY).state == "Cucina"

    # registrazione su 4 s: mediana, e proxy visto in meno di metà campioni scartato
    readings = iter([(1.0, 5.0, 3.0), (9.0, 5.2, "unknown"), (1.2, 5.4, "unknown"), (1.1, 5.1, "unknown")])

    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay != 1:  # asyncio.sleep(0) interni di Home Assistant
            return await real_sleep(delay)
        hass.states.async_set(CUC, str((r := next(readings))[0]))
        hass.states.async_set(SAL, str(r[1]))
        hass.states.async_set(STU, str(r[2]))
        await real_sleep(0)

    with patch("custom_components.ble_fingerprint.asyncio.sleep", AsyncMock(side_effect=fake_sleep)):
        result = await _call(
            hass, "record_vertex",
            {"room": "Cucina", "vertex_id": "c2", "duration": 4, "entity_id": ENTITY},
            response=True,
        )
    assert result["samples"] == 5
    assert result["distances"] == {"cucina": 1.2, "salotto": 5.1}

    with pytest.raises(ServiceValidationError):
        await _call(hass, "record_vertex", {"room": "X", "vertex_id": "x", "entity_id": "sensor.altro", "duration": 0})

    # nessuna distanza valida → errore, niente salvato
    await _move(hass, "unknown", "unknown")
    with pytest.raises(HomeAssistantError):
        await _call(hass, "record_vertex", {"room": "Vuota", "vertex_id": "v", "duration": 0})

    data = await _call(hass, "get_map", response=True)
    assert set(data["rooms"]) == {"Cucina"} and set(data["rooms"]["Cucina"]) == {"c1", "c2"}

    await _call(hass, "delete_vertex", {"room": "Cucina", "vertex_id": "c2"})
    with pytest.raises(ServiceValidationError):
        await _call(hass, "delete_vertex", {"room": "Cucina", "vertex_id": "c2"})
    await _call(hass, "save_vertex", {"room": "Studio", "vertex_id": "s", "distances": {STU: 1}})
    await _call(hass, "delete_room", {"room": "Studio"})
    with pytest.raises(ServiceValidationError):
        await _call(hass, "delete_room", {"room": "Studio"})
    await _call(hass, "delete_vertex", {"room": "Cucina", "vertex_id": "c1"})
    assert (await _call(hass, "get_map", response=True)) == {"rooms": {}}


async def test_config_and_options_flow(hass, freezer):
    # dispositivo con due sensori "distance to" + uno "unfiltered" da ignorare
    source = MockConfigEntry(domain="bermuda")
    source.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=source.entry_id, identifiers={("bermuda", "tel")}, name="Telefono"
    )
    ent_reg = er.async_get(hass)
    for suffix in ("distance_to_cucina", "distance_to_salotto", "unfiltered_distance_to_cucina", "area"):
        ent_reg.async_get_or_create(
            "sensor", "bermuda", f"tel_{suffix}", device_id=device.id,
            suggested_object_id=f"telefono_{suffix}", config_entry=source,
        )

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] == FlowResultType.FORM

    # né dispositivo né sensori → errore
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"name": "Posizione Tel"})
    assert result["errors"] == {"base": "no_sensors"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Posizione Tel", "device_id": device.id}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    entry = result["result"]
    assert entry.state is ConfigEntryState.LOADED

    # la mappa salvata con gli entity_id di un altro telefono vale anche per questo
    await _call(hass, "save_vertex", {"room": "Cucina", "vertex_id": "c", "distances": {CUC: 1.0, SAL: 5.0}})
    await _call(hass, "save_vertex", {"room": "Salotto", "vertex_id": "s", "distances": {CUC: 5.0, SAL: 1.0}})
    hass.states.async_set("sensor.telefono_distance_to_cucina", "5.1")
    hass.states.async_set("sensor.telefono_distance_to_salotto", "0.9")
    hass.states.async_set("sensor.telefono_unfiltered_distance_to_cucina", "0.1")
    await hass.async_block_till_done()
    await _wait(hass, freezer, 6)
    assert hass.states.get("sensor.posizione_tel").state == "Salotto"

    # opzioni: conferma disattivata → cambio stanza immediato dopo il reload
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"device_id": device.id, "sensors": [], "confirmation_seconds": 0, "unknown_timeout": 0,
         "max_distance": 8, "top_k": 1, "min_common_proxies": 1},
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get("sensor.posizione_tel").state == "Salotto"
    hass.states.async_set("sensor.telefono_distance_to_cucina", "1.0")
    hass.states.async_set("sensor.telefono_distance_to_salotto", "5.0")
    await hass.async_block_till_done()
    assert hass.states.get("sensor.posizione_tel").state == "Cucina"
