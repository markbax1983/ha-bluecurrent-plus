"""End-to-end tests against the fake backend."""

from __future__ import annotations

from datetime import timedelta

from homeassistant import config_entries
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
import pytest
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
    # reported grid current: diagnostic, disabled by default
    grid = er.async_get(hass).async_get(eid(hass, "sensor", "grid_max_current"))
    assert grid.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert state(hass, "sensor", "tariff").attributes["price_ex_vat"] == 0.3
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


async def test_real_world_null_timestamps(hass: HomeAssistant, setup_entry, backend) -> None:
    """Live pushes carry start/stop as null (seen on a real U:MOVE)."""
    events = async_capture_events(hass, f"{DOMAIN}_session_finished")
    s = dict(backend.status)
    await backend.push(
        {
            "object": "CH_STATUS",
            "data": {**s, "actual_kwh": 2.28, "start_datetime": None, "stop_datetime": None},
        }
    )
    await hass.async_block_till_done()
    assert state(hass, "sensor", "session_start").state.startswith("2026-09-24T12:00:00")
    assert float(state(hass, "sensor", "energy_total").state) == 5.0  # max(5, 2.28) same session
    await backend.push({"object": "CH_STATUS", "data": {**s, "actual_kwh": 6.0, "start_datetime": None}})
    await hass.async_block_till_done()
    assert float(state(hass, "sensor", "energy_total").state) == 6.0
    assert float(state(hass, "sensor", "session_reimbursement").state) == 1.8  # live: 6.0 kWh x 0.30
    assert state(hass, "sensor", "session_reimbursement").attributes["final"] is False
    assert float(state(hass, "sensor", "tariff").state) == 0.3
    assert state(hass, "sensor", "tariff").attributes["price_in_vat"] == 0.363

    # unplug: push without stop -> integration fetches status once, which has the stop
    sent = len(backend.sent)
    backend.status = {
        **s,
        "actual_kwh": 6.0,
        "vehicle_status": "A",
        "activity": "available",
        "stop_datetime": "20260924 17:00:00",
    }
    await backend.push(
        {
            "object": "CH_STATUS",
            "data": {
                **s,
                "actual_kwh": 6.0,
                "vehicle_status": "A",
                "activity": "available",
                "start_datetime": None,
                "stop_datetime": None,
            },
        }
    )
    await hass.async_block_till_done()
    assert [c["command"] for c in backend.sent[sent:]] == ["GET_CH_STATUS"]
    assert len(events) == 1 and events[0].data["energy_kwh"] == 6.0
    assert events[0].data["stop"].startswith("2026-09-24T17:00:00")
    # ended: Blue Current's own amount (1.5 in the fake) replaces the estimate
    assert float(state(hass, "sensor", "session_reimbursement").state) == 1.5
    assert state(hass, "sensor", "session_reimbursement").attributes["final"] is True
    assert float(state(hass, "sensor", "total_reimbursement").state) == 1.5


async def test_no_event_for_old_session_at_startup(hass: HomeAssistant, fake_session, backend) -> None:
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    backend.status = {**backend.status, "stop_datetime": "20260924 15:00:00", "activity": "available"}
    events = async_capture_events(hass, f"{DOMAIN}_session_finished")
    entry = MockConfigEntry(domain=DOMAIN, data={"api_token": "good"}, unique_id="cust1")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert events == []
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_session_values_survive_unplug(hass: HomeAssistant, setup_entry, backend) -> None:
    """Real backend resets actual_kwh to 0 on unplug (seen 24-09 16:50)."""
    s = dict(backend.status)
    await backend.push({"object": "CH_STATUS", "data": {**s, "actual_kwh": 5.26, "start_datetime": None}})
    await hass.async_block_till_done()
    backend.status = {
        **s,
        "actual_kwh": 0.0,
        "vehicle_status": "A",
        "activity": "available",
        "stop_datetime": "20260924 16:50:37",
        "total_cost": 1.57,
    }
    await backend.push(
        {
            "object": "CH_STATUS",
            "data": {**backend.status, "start_datetime": None, "stop_datetime": None, "vehicle_status": "A"},
        }
    )
    await hass.async_block_till_done()
    assert float(state(hass, "sensor", "session_energy").state) == 5.26
    assert float(state(hass, "sensor", "session_reimbursement").state) == 1.57  # Blue Current's figure
    assert float(state(hass, "sensor", "total_reimbursement").state) == 1.57
    assert float(state(hass, "sensor", "energy_total").state) == 5.26


async def test_removed_entities_are_cleaned_up(hass: HomeAssistant, fake_session, backend) -> None:
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(domain=DOMAIN, data={"api_token": "good"}, unique_id="cust1")
    entry.add_to_hass(hass)
    reg = er.async_get(hass)
    for key in ("session_stop", "session_cost", "session_tariff_value"):
        reg.async_get_or_create("sensor", DOMAIN, f"{EVSE}_{key}", config_entry=entry)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    for key in ("session_stop", "session_cost", "session_tariff_value"):
        assert reg.async_get_entity_id("sensor", DOMAIN, f"{EVSE}_{key}") is None
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_set_tariff(hass: HomeAssistant, setup_entry, backend) -> None:
    entity_id = eid(hass, "number", "reimbursement_tariff")
    assert float(hass.states.get(entity_id).state) == 0.3
    assert er.async_get(hass).async_get(entity_id).entity_category is EntityCategory.CONFIG

    await hass.services.async_call(
        "number", "set_value", {"entity_id": entity_id, "value": 0.41}, blocking=True
    )
    await hass.async_block_till_done()

    url, body, headers = backend.rest[-1]
    assert url == "https://api.bluecurrent.nl/app/bc_api/api/v2.0/editreimbursementtariff"
    assert body == {"evse_id": EVSE, "amount": "0.41", "currency": "EUR"}
    # websocket session token is reused
    assert headers["Authorization"] == "Token sess"
    st = hass.states.get(entity_id)
    assert float(st.state) == 0.41
    assert st.attributes["price_in_vat"] == round(0.41 * 1.21, 4)
    # backend view refreshed over the websocket
    assert backend.sent[-1] == {"command": "GET_CH_SETTINGS", "evse_id": EVSE, "Authorization": "Token sess"}
    assert state(hass, "sensor", "tariff").attributes["price_ex_vat"] == 0.41


async def test_set_tariff_out_of_range(hass: HomeAssistant, setup_entry, backend) -> None:
    entity_id = eid(hass, "number", "reimbursement_tariff")
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "number", "set_value", {"entity_id": entity_id, "value": 4.0}, blocking=True
        )
    assert backend.rest == []


async def test_set_tariff_rejected_then_portal_token(hass: HomeAssistant, setup_entry, backend) -> None:
    entity_id = eid(hass, "number", "reimbursement_tariff")
    backend.rest_status = 401
    with pytest.raises(HomeAssistantError, match="portal"):
        await hass.services.async_call(
            "number", "set_value", {"entity_id": entity_id, "value": 0.35}, blocking=True
        )
    assert float(hass.states.get(entity_id).state) == 0.3
    # rejected tariff call must not trigger a re-auth of the integration
    assert not list(setup_entry.async_get_active_flows(hass, {config_entries.SOURCE_REAUTH}))

    result = await hass.config_entries.options.async_init(setup_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"portal_token": "Token portal123 "}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert setup_entry.options == {"portal_token": "portal123"}

    backend.rest_status = 200
    await hass.services.async_call(
        "number", "set_value", {"entity_id": entity_id, "value": 0.35}, blocking=True
    )
    assert backend.rest[-1][2]["Authorization"] == "Token portal123"
    assert float(hass.states.get(entity_id).state) == 0.35
