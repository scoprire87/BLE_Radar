"""Test dell'integrazione BLE Fingerprint (BLE Radar)."""
import asyncio
import base64
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

from custom_components.ble_fingerprint.const import DOMAIN

CUC = "sensor.phone_distance_to_cucina"
SAL = "sensor.phone_distance_to_salotto"
STU = "sensor.phone_distance_to_studio"
ENTITY = "sensor.phone_position"

# PNG 1x1 trasparente
PNG = "data:image/png;base64," + base64.b64encode(bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
)).decode()


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
    """Config entry con un telefono tracciato tramite tre sensori di distanza."""
    entry = MockConfigEntry(
        domain=DOMAIN, version=3, title="BLE Radar", unique_id=DOMAIN,
        options={"trackers": [{"id": "t1", "name": "Phone position", "sensors": [CUC, SAL, STU]}]},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


async def _ws(client, type_, **data):
    await client.send_json_auto_id({"type": f"ble_fingerprint/{type_}", **data})
    response = await client.receive_json()
    return response


async def test_migration_from_version_1(hass):
    """La versione 1 non salvava nulla: il dispositivo si sceglie nella pagina."""
    entry = MockConfigEntry(domain=DOMAIN, data={}, version=1, title="BLE Radar")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.version == 3
    assert entry.data == {}
    assert entry.options["trackers"] == []
    assert hass.data[DOMAIN]["entities"] == {}


async def test_migration_from_version_2(hass):
    entry = MockConfigEntry(
        domain=DOMAIN, version=2, title="Posizione Tel",
        data={"name": "Posizione Tel", "device_id": None, "sensors": [CUC, SAL]},
        options={"confirmation_seconds": 9, "sensors": [CUC]},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.version == 3
    assert entry.options["confirmation_seconds"] == 9
    [tracker] = entry.options["trackers"]
    # le opzioni avevano la precedenza sui dati
    assert tracker["sensors"] == [CUC] and tracker["name"] == "Posizione Tel"
    # l'entità esistente (storico, automazioni) viene mantenuta
    assert tracker["unique_id"] == f"{entry.entry_id}_position"
    assert er.async_get(hass).async_get("sensor.posizione_tel").unique_id == f"{entry.entry_id}_position"
    assert hass.states.get("sensor.posizione_tel").state == "unknown"


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
    rooms = hass.data[DOMAIN]["map"].rooms
    assert STU not in rooms["Salotto"]["s1"]["distances"]  # >= 10 m scartata

    state = hass.states.get(ENTITY)
    assert state.state == "unknown"
    assert state.attributes["x"] < 2 and state.attributes["proxies"] == 2
    await _wait(hass, freezer, 6)
    assert hass.states.get(ENTITY).state == "Cucina"

    # spostamento in salotto: coordinate subito, stanza solo dopo la conferma
    await _move(hass, 5.0, 1.0)
    state = hass.states.get(ENTITY)
    assert state.state == "Cucina" and state.attributes["x"] > 5
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
    assert state.state == "unknown" and state.attributes["x"] is None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert hass.data[DOMAIN]["entities"] == {}


async def test_map_services(hass, freezer):
    await _move(hass, 1.2, 4.8)
    await _setup_legacy(hass)

    result = await _call(
        hass, "record_vertex",
        {"room": "Cucina", "vertex_id": "c1", "x": 1, "y": 2, "duration": 0},
        response=True,
    )
    assert result["distances"] == {"cucina": 1.2, "salotto": 4.8}
    assert result["coords"] == {"x": 1.0, "y": 2.0} and result["samples"] == 1

    # registrazione su 4 s: mediana, e proxy visto in meno di metà campioni scartato
    readings = iter([(1.0, 5.0, 3.0), (9.0, 5.2, "unknown"), (1.2, 5.4, "unknown"), (1.1, 5.1, "unknown")])
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay != 1:
            return await real_sleep(delay)
        r = next(readings)
        hass.states.async_set(CUC, str(r[0]))
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
    assert (await _call(hass, "get_map", response=True))["rooms"] == {}


async def test_config_and_options_flow(hass):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] == FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == "BLE Radar"
    entry = result["result"]
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert entry.options == {"trackers": []}

    # un'unica installazione
    again = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert again["type"] == FlowResultType.ABORT and again["reason"] == "single_instance_allowed"

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.FORM
    trackers = [{"id": "t1", "name": "Tel", "device_id": None, "sensors": [CUC]}]
    hass.config_entries.async_update_entry(entry, options={**entry.options, "trackers": trackers})
    await hass.async_block_till_done()
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"show_panel": False, "confirmation_seconds": 0, "unknown_timeout": 0, "smoothing": False,
         "advanced": {"trilateration": True, "map_scale": 1, "max_distance": 8,
                      "top_k": 1, "min_common_proxies": 1}},
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options["top_k"] == 1 and entry.options["show_panel"] is False
    # le opzioni non toccano i dispositivi tracciati
    assert entry.options["trackers"] == trackers


async def test_websocket_map_editing(hass, hass_ws_client, freezer, tmp_path):
    hass.config.config_dir = str(tmp_path)
    await _move(hass, 1.0, 5.0)
    entry = await _setup_legacy(hass)
    client = await hass_ws_client(hass)

    # piano con planimetria e scala (100 pixel = 1 m)
    resp = await _ws(client, "save_floor", name="Terra", image=PNG, width=1000, height=600)
    assert resp["success"], resp
    floor_id = resp["result"]["floor_id"]
    assert (tmp_path / "ble_fingerprint" / f"{floor_id}.png").is_file()
    resp = await _ws(client, "save_floor", floor_id=floor_id, name="Piano terra", scale=100)
    assert resp["success"]
    resp = await _ws(client, "floor_image", floor_id=floor_id)
    assert resp["result"]["image"] == PNG
    bad = await _ws(client, "save_floor", name="X", image="data:text/plain;base64,AAAA")
    assert not bad["success"] and bad["error"]["code"] == "invalid_image"

    # stanze disegnate: cucina a sinistra, salotto a destra
    cucina = [[0, 0], [400, 0], [400, 600], [0, 600]]
    salotto = [[400, 0], [1000, 0], [1000, 600], [400, 600]]
    for name, poly in (("Cucina", cucina), ("Salotto", salotto)):
        resp = await _ws(client, "save_zone", name=name, floor_id=floor_id, polygon=poly)
        assert resp["success"], resp
    dup = await _ws(client, "save_zone", name="Cucina", floor_id=floor_id, polygon=cucina)
    assert not dup["success"]

    # punti guidati: centro e 4 angoli rientrati di 0,5 m (50 px)
    resp = await _ws(client, "guided_points", room="Cucina")
    points = resp["result"]["points"]
    assert [(p["kind"], p["index"]) for p in points] == [
        ("center", 0), ("corner", 1), ("corner", 2), ("corner", 3), ("corner", 4)
    ]
    assert points[0]["x"] == pytest.approx(200) and points[0]["y"] == pytest.approx(300)
    corner = points[1]
    # 50 px (0,5 m) dall'angolo (0, 0) verso il centro della stanza
    assert (corner["x"] ** 2 + corner["y"] ** 2) ** 0.5 == pytest.approx(50, abs=0.2)
    assert not any(p["recorded"] for p in points)

    # registrazione guidata dei punti (come fa il pannello)
    for room, vid, x, y, reading in (
        ("Cucina", "p0", 200, 300, (1.0, 5.0)),
        ("Cucina", "p1", 50, 50, (1.2, 5.5)),
        ("Salotto", "p0", 700, 300, (5.0, 1.0)),
        ("Salotto", "p1", 950, 50, (5.5, 1.2)),
    ):
        await _move(hass, *reading)
        await _call(hass, "record_vertex", {
            "room": room, "vertex_id": vid, "x": x, "y": y, "floor": floor_id, "duration": 0,
        })
    resp = await _ws(client, "guided_points", room="Cucina")
    assert [p["recorded"] for p in resp["result"]["points"]][:2] == [True, True]

    # la stanza è quella disegnata che contiene la posizione
    await _move(hass, 4.9, 1.1)
    await _wait(hass, freezer, 6)
    state = hass.states.get(ENTITY)
    assert state.state == "Salotto"
    assert state.attributes["floor"] == floor_id
    assert state.attributes["floor_name"] == "Piano terra"
    assert state.attributes["x"] >= 400

    # configurazione per il pannello
    resp = await _ws(client, "config")
    config = resp["result"]
    assert set(config["zones"]) == {"Cucina", "Salotto"}
    assert config["rooms"]["Cucina"]["p0"] == {"coords": {"x": 200.0, "y": 300.0}, "floor": floor_id, "proxies": 2}
    assert config["trackers"][0]["entity_id"] == ENTITY

    # correzione manuale di un proxy e ritorno alla stima
    resp = await _ws(client, "set_proxy", proxy="sensor.x_distance_to_cucina", floor_id=floor_id, x=10, y=20)
    assert resp["success"]
    assert hass.data[DOMAIN]["map"].proxies["cucina"] == {"x": 10.0, "y": 20.0, "floor": floor_id}
    resp = await _ws(client, "set_proxy", proxy="cucina", reset=True)
    assert "cucina" not in hass.data[DOMAIN]["map"].proxies

    # rinomina: poligono e punti seguono il nuovo nome
    resp = await _ws(client, "save_zone", name="Soggiorno", old_name="Salotto", floor_id=floor_id, polygon=salotto)
    assert resp["success"]
    map_store = hass.data[DOMAIN]["map"]
    assert "Soggiorno" in map_store.zones and set(map_store.rooms["Soggiorno"]) == {"p0", "p1"}

    # eliminare una stanza toglie anche i suoi punti
    resp = await _ws(client, "delete_zone", name="Soggiorno")
    assert resp["success"] and "Soggiorno" not in map_store.rooms

    # eliminare il piano toglie stanze, punti e planimetria
    resp = await _ws(client, "delete_floor", floor_id=floor_id)
    assert resp["success"]
    assert map_store.floors == {} and map_store.zones == {} and map_store.rooms == {}
    assert not (tmp_path / "ble_fingerprint" / f"{floor_id}.png").exists()
    assert entry.state is ConfigEntryState.LOADED


async def test_websocket_trackers(hass, hass_ws_client):
    bermuda = MockConfigEntry(domain="bermuda")
    bermuda.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=bermuda.entry_id, identifiers={("bermuda", "tel")}, name="Telefono"
    )
    ent_reg = er.async_get(hass)
    for suffix in ("distance_to_cucina", "distance_to_salotto", "unfiltered_distance_to_cucina", "area"):
        ent_reg.async_get_or_create(
            "sensor", "bermuda", f"tel_{suffix}", device_id=device.id,
            suggested_object_id=f"telefono_{suffix}", config_entry=bermuda,
        )

    entry = MockConfigEntry(domain=DOMAIN, version=3, title="BLE Radar", options={"trackers": []})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    client = await hass_ws_client(hass)

    resp = await _ws(client, "devices")
    [candidate] = resp["result"]["devices"]
    assert candidate["device_id"] == device.id and candidate["bermuda"] is True
    assert sorted(candidate["sensors"]) == [
        "sensor.telefono_distance_to_cucina", "sensor.telefono_distance_to_salotto"
    ]

    resp = await _ws(client, "save_trackers", trackers=[{"name": "Posizione Tel", "device_id": device.id}])
    assert resp["success"], resp
    await hass.async_block_till_done()
    [tracker] = entry.options["trackers"]
    state = hass.states.get("sensor.posizione_tel")
    assert state is not None
    # il sensore ha un suo dispositivo, collegato a quello di Bermuda
    reg_entry = ent_reg.async_get("sensor.posizione_tel")
    own_device = dr.async_get(hass).async_get(reg_entry.device_id)
    assert own_device.via_device_id == device.id

    # rinomina mantenendo id ed entità
    resp = await _ws(client, "save_trackers", trackers=[{"id": tracker["id"], "name": "Tel di Ale", "device_id": device.id}])
    assert resp["success"]
    await hass.async_block_till_done()
    assert entry.options["trackers"][0]["id"] == tracker["id"]
    assert ent_reg.async_get("sensor.posizione_tel").unique_id == reg_entry.unique_id

    # senza dispositivo né sensori: errore
    resp = await _ws(client, "save_trackers", trackers=[{"name": "Vuoto"}])
    assert not resp["success"]

    # un nuovo sensore di distanza di Bermuda ricarica da solo
    ent_reg.async_get_or_create(
        "sensor", "bermuda", "tel_distance_to_studio", device_id=device.id,
        suggested_object_id="telefono_distance_to_studio", config_entry=bermuda,
    )
    await hass.async_block_till_done()
    tracker_entity = hass.data[DOMAIN]["entities"][tracker["id"]]
    assert "sensor.telefono_distance_to_studio" in tracker_entity.proxy_sensors

    # rimozione
    resp = await _ws(client, "save_trackers", trackers=[])
    assert resp["success"]
    await hass.async_block_till_done()
    assert hass.data[DOMAIN]["entities"] == {}


async def test_websocket_requires_admin(hass, hass_ws_client, hass_read_only_access_token):
    await _setup_legacy(hass)
    client = await hass_ws_client(hass, hass_read_only_access_token)
    resp = await _ws(client, "config")
    assert resp["success"]  # la card funziona anche per gli utenti normali
    resp = await _ws(client, "save_floor", name="X")
    assert not resp["success"] and resp["error"]["code"] == "unauthorized"


async def test_removed_tracker_leaves_no_orphans(hass, hass_ws_client):
    entry = MockConfigEntry(
        domain=DOMAIN, version=3, title="BLE Radar",
        options={"trackers": [
            {"id": "a", "name": "Uno", "sensors": [CUC]},
            {"id": "b", "name": "Due", "sensors": [SAL]},
        ]},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    ent_reg, dev_reg = er.async_get(hass), dr.async_get(hass)
    assert len(er.async_entries_for_config_entry(ent_reg, entry.entry_id)) == 2
    assert len(dr.async_entries_for_config_entry(dev_reg, entry.entry_id)) == 2

    client = await hass_ws_client(hass)
    resp = await _ws(client, "save_trackers", trackers=[{"id": "a", "name": "Uno", "sensors": [CUC]}])
    assert resp["success"]
    await hass.async_block_till_done()
    assert [e.entity_id for e in er.async_entries_for_config_entry(ent_reg, entry.entry_id)] == ["sensor.uno"]
    assert len(dr.async_entries_for_config_entry(dev_reg, entry.entry_id)) == 1
    assert hass.states.get("sensor.due") is None
