"""Fixtures: a fake Blue Current websocket backend."""

from __future__ import annotations

import asyncio
import copy
import json
from typing import Any
from unittest.mock import patch

import aiohttp
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.blue_current_plus.const import DOMAIN

EVSE = "BCU000001"

STATUS = {
    "evse_id": EVSE,
    "actual_v1": 230.0,
    "actual_v2": 231.0,
    "actual_v3": 229.0,
    "actual_p1": 16.0,
    "actual_p2": 16.0,
    "actual_p3": 16.0,
    "actual_kwh": 5.0,
    "start_datetime": "20260924 14:00:00",
    "stop_datetime": "",
    "offline_since": "",
    "total_cost": 1.5,
    "vehicle_status": "C",
    "activity": "charging",
    "max_usage": 32,
    "smartcharging_max_usage": 6,
    "max_offline": 6,
}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


class FakeBackend:
    """Mimics wss://motown.bluecurrent.nl/haserver."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.live: FakeWS | None = None
        self.status = copy.deepcopy(STATUS)
        self.schedule_id = 7

    def reply(self, cmd: dict[str, Any]) -> list[dict[str, Any]]:
        c = cmd["command"]
        if c == "VALIDATE_API_TOKEN":
            if cmd["token"] != "good":
                return [{"object": "STATUS_API_TOKEN", "success": False}]
            return [{"object": "STATUS_API_TOKEN", "success": True, "token": "sess", "customer_id": "cust1"}]
        if c == "GET_ACCOUNT":
            return [{"object": "ACCOUNT", "login": "mark@example.nl"}]
        if c == "HELLO":
            return [{"object": "HELLO"}]
        if c == "GET_CHARGE_POINTS":
            return [
                {
                    "object": "CHARGE_POINTS",
                    "data": [
                        {"evse_id": EVSE, "model_type": "TEST-MODEL", "name": "", "smart_charging": False}
                    ],
                }
            ]
        if c == "GET_CH_STATUS":
            return [{"object": "CH_STATUS", "data": dict(self.status)}]
        if c == "GET_CH_SETTINGS":
            return [
                {
                    "object": "CH_SETTINGS",
                    "data": {
                        "evse_id": EVSE,
                        "plug_and_charge": {"value": True, "permission": "write"},
                        "public_charging": {"value": True, "permission": "write"},
                        "smart_charging": False,
                        "tariff": {
                            "tariff_id": "NLBCUT00",
                            "price_ex_vat": 0.3,
                            "price_in_vat": 0.363,
                            "start_price_ex_vat": 0,
                            "start_price_in_vat": 0,
                            "currency": "EUR",
                        },
                    },
                }
            ]
        if c == "GET_GRID_STATUS":
            return [
                {
                    "object": "GRID_STATUS",
                    "data": {"grid_actual_p1": 10, "grid_actual_p2": 12, "grid_actual_p3": 8},
                }
            ]
        if c == "GET_CHARGE_CARDS":
            return [
                {
                    "object": "CHARGE_CARDS",
                    "data": [{"uid": "04AABBCCDDEE01", "id": "NL-XXX-C0000000-0", "name": "Testpas"}],
                }
            ]
        if c == "LIST_OVERRIDE_CURRENT":
            # Real backend answers without 'data' when there are no schedules:
            # this is what crashes bluecurrent-api with KeyError: 'data'.
            return [{"object": "LIST_OVERRIDE_CURRENT", "success": True}]
        if c in ("POST_SET_OVERRIDE_CURRENT", "POST_EDIT_OVERRIDE_CURRENT"):
            sid = cmd.get("schedule_id", self.schedule_id)
            return [
                {
                    "object": c,
                    "data": {
                        "schedule_id": sid,
                        "charge_points": cmd["chargepoints"],
                        "start_time": cmd["overridestarttime"],
                        "stop_time": cmd["overridestoptime"],
                        "override_start_days": ",".join(cmd["overridestartdays"]),
                        "override_end_days": ",".join(cmd["overridestopdays"]),
                        "override_current": cmd["overridevalue"],
                    },
                }
            ]
        if c == "POST_CLEAR_OVERRIDE_CURRENT":
            return [{"object": c, "success": True}]
        if c in ("START_SESSION", "STOP_SESSION", "SOFT_RESET", "REBOOT"):
            return [
                {"object": f"RECEIVED_{c}", "success": True, "evse_id": cmd["evse_id"], "error": ""},
                {"object": f"STATUS_{c}", "success": True, "evse_id": cmd["evse_id"], "error": ""},
            ]
        if c.startswith("SET_"):
            return [
                {
                    "object": f"STATUS_{c}",
                    "evse_id": cmd.get("evse_id"),
                    "result": {"setting": str(cmd.get("value", "")).lower()},
                }
            ]
        return [{"object": "SOMETHING_NEW", "command": c}]

    async def push(self, message: dict[str, Any]) -> None:
        assert self.live is not None
        await self.live.queue.put(message)


class FakeWS:
    def __init__(self, backend: FakeBackend) -> None:
        self.backend = backend
        self.queue: asyncio.Queue = asyncio.Queue()
        self.closed = False
        self.close_code: int | None = None

    async def send_str(self, data: str) -> None:
        cmd = json.loads(data)
        self.backend.sent.append(cmd)
        for msg in self.backend.reply(cmd):
            await self.queue.put(msg)

    async def receive(self):
        msg = await self.queue.get()
        if msg is None:
            return aiohttp.WSMessage(aiohttp.WSMsgType.CLOSED, None, None)
        return aiohttp.WSMessage(aiohttp.WSMsgType.TEXT, json.dumps(msg), None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        msg = await self.receive()
        if msg.type == aiohttp.WSMsgType.CLOSED:
            raise StopAsyncIteration
        return msg

    async def close(self) -> None:
        self.closed = True
        await self.queue.put(None)

    def exception(self):
        return None


class _Ctx:
    def __init__(self, ws: FakeWS, live: bool) -> None:
        self.ws, self.live = ws, live

    async def __aenter__(self) -> FakeWS:
        if self.live:
            self.ws.backend.live = self.ws
        return self.ws

    async def __aexit__(self, *exc) -> None:
        self.ws.closed = True


class FakeSession:
    def __init__(self, backend: FakeBackend) -> None:
        self.backend = backend
        self.connects = 0

    def ws_connect(self, url: str, **kwargs):
        self.connects += 1
        # 1st/2nd connection of a setup are one-shots; mark every connection as
        # potentially live - the last one opened is the persistent one.
        return _Ctx(FakeWS(self.backend), live=True)


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def fake_session(backend: FakeBackend):
    session = FakeSession(backend)
    with (
        patch("custom_components.blue_current_plus.async_get_clientsession", return_value=session),
        patch(
            "custom_components.blue_current_plus.config_flow.async_get_clientsession", return_value=session
        ),
    ):
        yield session


@pytest.fixture
async def setup_entry(hass, fake_session, backend):
    entry = MockConfigEntry(domain=DOMAIN, data={"api_token": "good"}, unique_id="cust1", title="mark")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    yield entry
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
