"""Diagnostics: everything needed to debug, with secrets redacted."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_API_TOKEN
from homeassistant.core import HomeAssistant

from .coordinator import BlueCurrentConfigEntry

REDACT = {
    CONF_API_TOKEN,
    "Authorization",
    "token",
    "uid",
    "session_token",
    "card_uid",
    "login",
    "customer_id",
    "portal_token",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: BlueCurrentConfigEntry
) -> dict[str, Any]:
    c = entry.runtime_data
    return async_redact_data(
        {
            "entry": dict(entry.data),
            "connected": c.data.connected,
            "requests_today": c.api.requests.today,
            "charge_points": {
                evse: {**asdict(cp), "power_kw": cp.power_kw, "current_left": cp.current_left}
                for evse, cp in c.data.charge_points.items()
            },
            "grid": c.data.grid,
            "cards": list(c.data.cards.values()),
            "schedules": list(c.data.schedules.values()),
            "unhandled_messages": list(c.raw_messages),
        },
        REDACT,
    )
