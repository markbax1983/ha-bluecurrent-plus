"""End-to-end tests against the fake backend."""

from __future__ import annotations

from datetime import timedelta

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_capture_events, async_fire_time_changed

from custom_components.blue_current_plus.const import DOMAIN
from custom_components.blue_current_plus.diagnostics import async_get_config_entry_diagnostics

from .conftest import EVSE


def eid(hass: HomeAssistant, platform: str, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{EVSE}_{key}")
    assert entity_id, f"{platform}.{key} not registered"
    return entity_id


def state(hass: HomeAssistant, platform: str, key: str):
    return hass.states.get(eid(hass, platform, key))


async def test_sensors(hass: HomeAssistant, setup_entry) -> None:
    # Real power: sum U*I per phase = (230+231+229)*16 = 11040 W
    assert float(state(hass, "sensor", "power").state) == 11.04
    assert float(state(hass, "sensor", "power_phase_1").state) == 3680.0
    assert state(hass, "sensor", "active_phases").state == "3"
    assert state(hass, "sensor", "activity").state == "charging"
    assert state(hass, "sensor", "vehicle_status").state == "ready"
    assert float(state(hass, "sensor", "energy_total").state) == 5.0
    assert float(state(hass, "sensor", "session_energy").state) == 5.0
    # headroom on busiest phase: 32 - 16
    assert float(state(hass, "sensor", "current_left").state) == 16.0
    assert float(state(hass, "sensor", "grid_max_current").state) == 12.0
    assert state(hass, "binary_sensor", "charging").state == "on"
    assert state(hass, "binary_sensor", "vehicle_connected").state == "on"
    assert state(hass, "binary_sensor", "cloud_connection").state == "on"
    assert state(hass, "binary_sensor", "override_active").state == "off"
    assert state(hass, "switch", "plug_and_charge").state == "on"
    assert state(hass, "switch", "linked_charge_cards_only").state == "off"
    assert state(hass, "switch", "block").state == "off"
    # no override -> number shows max_usage
    assert float(state(hass, "number", "max_current").state) == 32


async def test_energy_counter_and_session_event(hass: HomeAssistant, setup_entry, backend) -> None:
    events = async_capture_events(hass, f"{DOMAIN}_session_finished")
    s = dict(backend.status)

    await backend.push({"object": "CH_STATUS", "data": {**s, "actual_kwh": 7.5}})
    await hass.async_block_till_done()
    assert float(state(hass, "sensor", "energy_total").state) == 7.5

    # session ends: kWh stays, stop time set -> one event
    await backend.push(
        {
            "object": "CH_STATUS",
            "data": {**s, "actual_kwh": 7.5, "activity": "available", "stop_datetime": "20260924 16:30:00"},
        }
    )
    await backend.push(
        {
            "object": "CH_STATUS",
            "data": {**s, "actual_kwh": 7.5, "activity": "available", "stop_datetime": "20260924 16:30:00"},
        }
    )
    await hass.async_block_till_done()
    assert len(events) == 1
    assert events[0].data["energy_kwh"] == 7.5
    assert events[0].data["duration_min"] == 150.0

    # new session starts from 0 -> lifetime counter keeps growing
    await backend.push(
        {"object": "CH_STATUS", "data": {**s, "actual_kwh": 1.25, "start_datetime": "20260925 08:00:00"}}
    )
    await hass.async_block_till_done()
    assert float(state(hass, "sensor", "energy_total").state) == 8.75
    assert float(state(hass, "sensor", "session_energy").state) == 1.25


async def test_max_current_override_debounced(hass: HomeAssistant, setup_entry, backend) -> None:
    number = eid(hass, "number", "max_current")
    for value in (12, 11, 10):  # slider drag
        await hass.services.async_call(
            "number", "set_value", {"entity_id": number, "value": value}, blocking=True
        )
    assert float(hass.states.get(number).state) == 10  # optimistic
    assert not [c for c in backend.sent if c["command"].startswith("POST_")]

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=6))
    await hass.async_block_till_done()
    posts = [c for c in backend.sent if c["command"].startswith("POST_")]
    assert len(posts) == 1
    assert posts[0]["command"] == "POST_SET_OVERRIDE_CURRENT"
    assert posts[0]["overridevalue"] == 10
    assert posts[0]["chargepoints"] == [EVSE]
    assert posts[0]["overridestartdays"] == ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
    assert float(hass.states.get(number).state) == 10
    assert state(hass, "binary_sensor", "override_active").state == "on"

    # change -> edit the same schedule
    await hass.services.async_call("number", "set_value", {"entity_id": number, "value": 8}, blocking=True)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=12))
    await hass.async_block_till_done()
    edit = backend.sent[-1]
    assert edit["command"] == "POST_EDIT_OVERRIDE_CURRENT"
    assert edit["schedule_id"] == 7 and edit["overridevalue"] == 8

    # back to max -> clear
    await hass.services.async_call("number", "set_value", {"entity_id": number, "value": 32}, blocking=True)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=18))
    await hass.async_block_till_done()
    assert backend.sent[-1] == {
        "command": "POST_CLEAR_OVERRIDE_CURRENT",
        "schedule_id": 7,
        "Authorization": "Token sess",
    }
    assert float(hass.states.get(number).state) == 32
    assert state(hass, "binary_sensor", "override_active").state == "off"


async def test_card_select_and_start(hass: HomeAssistant, setup_entry, backend) -> None:
    select = state(hass, "select", "charge_card")
    assert select.state == "Zonder laadpas"
    mark = next(o for o in select.attributes["options"] if "Mark" in o)
    await hass.services.async_call(
        "select", "select_option", {"entity_id": select.entity_id, "option": mark}, blocking=True
    )
    await hass.services.async_call(
        "button", "press", {"entity_id": eid(hass, "button", "start_session")}, blocking=True
    )
    await hass.async_block_till_done()
    start = next(c for c in backend.sent if c["command"] == "START_SESSION")
    assert start["session_token"] == "04AABBCCDDEE01"
    assert start["evse_id"] == EVSE


async def test_switch_and_unknown_message(hass: HomeAssistant, setup_entry, backend) -> None:
    sw = eid(hass, "switch", "linked_charge_cards_only")
    await hass.services.async_call("switch", "turn_on", {"entity_id": sw}, blocking=True)
    await hass.async_block_till_done()
    cmd = next(c for c in backend.sent if c["command"] == "SET_PUBLIC_CHARGING")
    assert cmd["value"] is False
    assert hass.states.get(sw).state == "on"

    await backend.push({"object": "SESSION_HISTORY", "data": [1, 2]})
    await hass.async_block_till_done()
    diag = await async_get_config_entry_diagnostics(hass, setup_entry)
    assert any(m.get("object") == "SESSION_HISTORY" for m in diag["unhandled_messages"])
    assert diag["entry"]["api_token"] == "**REDACTED**"


async def test_disconnect_marks_unavailable(hass: HomeAssistant, setup_entry, backend) -> None:
    await backend.live.close()
    await hass.async_block_till_done()
    assert state(hass, "sensor", "power").state == "unavailable"
    assert state(hass, "binary_sensor", "cloud_connection").state == "off"


async def test_config_flow(hass: HomeAssistant, fake_session) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"api_token": "bad"})
    assert result["errors"] == {"base": "invalid_token"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"api_token": "good"})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "mark@example.nl"
    assert result["result"].unique_id == "cust1"
    assert result["result"].state is config_entries.ConfigEntryState.LOADED
    await hass.config_entries.async_unload(result["result"].entry_id)
    await hass.async_block_till_done()


async def test_reconnects_after_drop(hass: HomeAssistant, setup_entry, backend, fake_session) -> None:
    connects = fake_session.connects
    await backend.live.close()
    await hass.async_block_till_done()
    assert state(hass, "binary_sensor", "cloud_connection").state == "off"
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=11))
    await hass.async_block_till_done()
    # sleep() in the loop runs on the real clock in tests; give it a moment
    import asyncio

    for _ in range(40):
        if fake_session.connects > connects:
            break
        await asyncio.sleep(0.5)
    await hass.async_block_till_done()
    assert fake_session.connects > connects
    assert state(hass, "binary_sensor", "cloud_connection").state == "on"
    assert float(state(hass, "sensor", "power").state) == 11.04


async def test_request_limit_creates_issue(hass: HomeAssistant, setup_entry, backend) -> None:
    from homeassistant.helpers import issue_registry as ir

    await backend.push({"object": "ERROR", "error": 42, "message": "Request limit reached"})
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, "request_limit") is not None
    assert state(hass, "sensor", "power").state == "unavailable"
    # counter itself stays available so you can see why
    assert state(hass, "sensor", "requests_today").state != "unavailable"


async def test_energy_survives_restart(hass: HomeAssistant, setup_entry, backend) -> None:
    s = dict(backend.status)
    await backend.push({"object": "CH_STATUS", "data": {**s, "actual_kwh": 9.0}})
    await backend.push(
        {"object": "CH_STATUS", "data": {**s, "actual_kwh": 2.0, "start_datetime": "20260925 08:00:00"}}
    )
    await hass.async_block_till_done()
    assert float(state(hass, "sensor", "energy_total").state) == 11.0

    await hass.config_entries.async_reload(setup_entry.entry_id)
    await hass.async_block_till_done()
    # backend still reports the 14:00 session (5 kWh) - a *different* session key,
    # so base (11) + 5 -> must not go backwards
    assert float(state(hass, "sensor", "energy_total").state) == 11.0
