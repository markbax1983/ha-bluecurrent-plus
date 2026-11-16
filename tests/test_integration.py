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
    tariff = state(hass, "number", "reimbursement_tariff")
    assert float(tariff.state) == 0.3
    assert tariff.attributes["price_in_vat"] == 0.363
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
    assert select.state == "No charge card"
    card = next(o for o in select.attributes["options"] if "Testpas" in o)
    await hass.services.async_call(
        "select", "select_option", {"entity_id": select.entity_id, "option": card}, blocking=True
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
    """Live pushes carry start/stop as null (seen on a real charge point)."""
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
    reg.async_get_or_create("sensor", DOMAIN, f"{EVSE}_tariff", config_entry=entry)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert reg.async_get_entity_id("sensor", DOMAIN, f"{EVSE}_tariff") is None
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
    assert state(hass, "sensor", "session_reimbursement").attributes["estimate"] == round(5.0 * 0.41, 2)


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


async def test_device_name_and_language(hass: HomeAssistant, setup_entry) -> None:
    from homeassistant.helpers import device_registry as dr

    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, EVSE)})
    # charge point without its own name -> translated "Charge point <evse_id>"
    assert device.name == f"Charge point {EVSE}"

    hass.config.language = "nl"
    setup_entry.runtime_data.async_set_updated_data(setup_entry.runtime_data.data)
    await hass.async_block_till_done()
    assert state(hass, "select", "charge_card").state == "Zonder laadpas"


async def test_month_totals(hass: HomeAssistant, fake_session, backend, freezer) -> None:
    """Sessions count in the month they started in; the month rolls over on its own."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    freezer.move_to("2026-09-24 13:00:00+00:00")  # 15:00 in Amsterdam, session runs since 14:00
    entry = MockConfigEntry(domain=DOMAIN, data={"api_token": "good"}, unique_id="cust1")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    s = dict(backend.status)

    energy, money = state(hass, "sensor", "month_energy"), state(hass, "sensor", "month_reimbursement")
    assert float(energy.state) == 5.0  # running session, live
    assert float(money.state) == 1.5  # live estimate 5 kWh x 0.30
    assert energy.attributes["month"] == "2026-09"
    assert energy.attributes["sessions"] == 1
    assert energy.attributes["previous_month"] == "2026-08"
    assert energy.attributes["last_reset"].startswith("2026-09-01T00:00:00+02:00")

    # session ends at 7.5 kWh, Blue Current bills 1.5
    done = {**s, "actual_kwh": 7.5, "activity": "available", "stop_datetime": "20260924 16:30:00"}
    await backend.push({"object": "CH_STATUS", "data": done})
    await hass.async_block_till_done()
    assert float(state(hass, "sensor", "month_energy").state) == 7.5
    assert float(state(hass, "sensor", "month_reimbursement").state) == 1.5
    assert state(hass, "sensor", "month_energy").attributes["sessions"] == 1

    # a new session starts on the last evening of September...
    late = {**s, "actual_kwh": 2.0, "start_datetime": "20260930 23:00:00", "stop_datetime": ""}
    await backend.push({"object": "CH_STATUS", "data": late})
    await hass.async_block_till_done()
    assert float(state(hass, "sensor", "month_energy").state) == 9.5

    # ...the month rolls over while it is still charging
    freezer.move_to("2026-09-30 23:00:05+00:00")  # 01:00:05 on 1 October in Amsterdam
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    energy = state(hass, "sensor", "month_energy")
    assert float(energy.state) == 0.0
    assert energy.attributes["month"] == "2026-10"
    assert energy.attributes["previous_month"] == "2026-09"
    assert energy.attributes["previous_month_value"] == 7.5

    # it finishes in October, but is booked in September (the month it started in)
    end = {**late, "actual_kwh": 4.0, "total_cost": 1.2, "stop_datetime": "20261001 02:00:00"}
    await backend.push({"object": "CH_STATUS", "data": end})
    await hass.async_block_till_done()
    energy, money = state(hass, "sensor", "month_energy"), state(hass, "sensor", "month_reimbursement")
    assert float(energy.state) == 0.0
    assert energy.attributes["previous_month_value"] == 11.5
    assert energy.attributes["previous_month_sessions"] == 2
    assert money.attributes["previous_month_value"] == 2.7

    # totals survive a restart
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    energy = state(hass, "sensor", "month_energy")
    assert energy.attributes["month"] == "2026-10"
    assert energy.attributes["previous_month_value"] == 11.5
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


# ------------------------------------------------------------- security / privacy
CARD_UID = "04AABBCCDDEE01"
CARD_EMP = "NL-XXX-C0000000-0"


async def test_card_uid_not_exposed(hass: HomeAssistant, setup_entry) -> None:
    """The RFID UID must never reach the state machine (recorder, backups)."""
    select = state(hass, "select", "charge_card")
    blob = repr(select.attributes) + select.state
    assert CARD_UID not in blob
    assert CARD_EMP not in blob
    assert "uid" not in select.attributes
    assert any(o.startswith("Testpas (") and o.endswith("00-0)") for o in select.attributes["options"])
    for st in hass.states.async_all():
        assert CARD_UID not in repr(st.attributes) + st.state


async def test_duplicate_card_labels_stay_unique(hass: HomeAssistant, setup_entry, backend) -> None:
    await backend.push(
        {
            "object": "CHARGE_CARDS",
            "data": [
                {"uid": "04AAAAAAAAAA01", "id": "NL-XXX-C1111111-1", "name": "Pas"},
                {"uid": "04AAAAAAAAAA02", "id": "NL-XXX-C2221111-1", "name": "Pas"},
            ],
        }
    )
    await hass.async_block_till_done()
    options = state(hass, "select", "charge_card").attributes["options"]
    assert len(options) == len(set(options)) == 3


async def test_diagnostics_without_personal_data(hass: HomeAssistant, setup_entry, backend) -> None:
    await backend.push(
        {"object": "SECRET_THING", "data": {"evse_id": EVSE, "email": "x@example.nl", "note": "private"}}
    )
    await hass.async_block_till_done()
    diag = await async_get_config_entry_diagnostics(hass, setup_entry)
    blob = repr(diag)
    for secret in (CARD_UID, CARD_EMP, "Testpas", EVSE, "x@example.nl", "private", "cust1"):
        assert secret not in blob, secret
    for token in ("good", "sess"):  # api token / session token as whole values
        assert repr(token) not in blob, token
    assert "charge_point_1" in diag["charge_points"]
    unhandled = next(m for m in diag["unhandled_messages"] if m["object"] == "SECRET_THING")
    assert unhandled["shape"] == {"data": {"evse_id": "str", "email": "**REDACTED**", "note": "str"}}


async def test_options_portal_token_hidden_kept_and_cleared(hass: HomeAssistant, setup_entry) -> None:
    flow = hass.config_entries.options
    result = await flow.async_init(setup_entry.entry_id)
    result = await flow.async_configure(result["flow_id"], {"portal_token": "portal123"})
    assert setup_entry.options == {"portal_token": "portal123"}

    # the stored token is not shown in the form
    result = await flow.async_init(setup_entry.entry_id)
    assert "portal123" not in repr(result["data_schema"].schema)
    # empty field keeps it
    result = await flow.async_configure(result["flow_id"], {})
    assert setup_entry.options == {"portal_token": "portal123"}
    # checkbox removes it
    result = await flow.async_init(setup_entry.entry_id)
    result = await flow.async_configure(result["flow_id"], {"clear_portal_token": True})
    assert setup_entry.options == {}


async def test_command_error_keeps_connection(
    hass: HomeAssistant, setup_entry, fake_session, backend
) -> None:
    connects = fake_session.connects
    await backend.push({"object": "ERROR", "error": 9, "message": "Charge point busy"})
    await backend.push({"object": "CH_STATUS", "data": {**backend.status, "actual_p1": 10.0}})
    await hass.async_block_till_done()
    assert fake_session.connects == connects
    assert state(hass, "binary_sensor", "cloud_connection").state == "on"
    assert float(state(hass, "sensor", "power_phase_1").state) == 2300.0
