"""Diagnostics: everything needed to debug, without secrets or personal data.

Diagnostics end up in public GitHub issues, so:
- secrets (tokens) and personal data (card UID/EMP number/name, e-mail,
  customer id, addresses) are redacted by key;
- charge point ids (EVSE ids) are replaced by `charge_point_1`, `charge_point_2`,
  everywhere, also inside other strings;
- messages the integration does not understand are reduced to their shape
  (keys and value types), never their values.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_API_TOKEN
from homeassistant.core import HomeAssistant

from .const import CONF_PORTAL_TOKEN
from .coordinator import BlueCurrentConfigEntry

REDACT = {
    CONF_API_TOKEN,
    CONF_PORTAL_TOKEN,
    "Authorization",
    "token",
    "session_token",
    # charge cards
    "uid",
    "card_uid",
    "id",
    "name",
    "preferred_card",
    "default_card",
    # account / location
    "login",
    "email",
    "customer_id",
    "address",
    "street",
    "housenumber",
    "postal_code",
    "zipcode",
    "city",
    "latitude",
    "longitude",
    "location",
}

MAX_SHAPE_DEPTH = 4


def _anonymize(value: Any, ids: dict[str, str]) -> Any:
    """Replace every occurrence of a charge point id (keys, values, substrings)."""
    if isinstance(value, dict):
        return {_anonymize(k, ids): _anonymize(v, ids) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_anonymize(v, ids) for v in value]
    if isinstance(value, str):
        for real, alias in ids.items():
            value = value.replace(real, alias)
    return value


def _shape(value: Any, depth: int = 0) -> Any:
    """Structure of a payload without its values."""
    if depth >= MAX_SHAPE_DEPTH:
        return "..."
    if isinstance(value, dict):
        return {str(k): _shape(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [_shape(value[0], depth + 1), f"({len(value)} items)"] if value else []
    return type(value).__name__


def _message_summary(message: dict[str, Any]) -> dict[str, Any]:
    return {
        "object": message.get("object"),
        "success": message.get("success"),
        "error": message.get("error"),
        "shape": _shape({k: v for k, v in message.items() if k not in ("object", "success", "error")}),
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: BlueCurrentConfigEntry
) -> dict[str, Any]:
    c = entry.runtime_data
    ids = {evse: f"charge_point_{n}" for n, evse in enumerate(c.data.charge_points, start=1)}
    data = {
        "entry": dict(entry.data),
        "options_set": sorted(entry.options),
        "connected": c.data.connected,
        "requests_today": c.api.requests.today,
        "charge_points": {
            evse: {**asdict(cp), "power_kw": cp.power_kw, "current_left": cp.current_left}
            for evse, cp in c.data.charge_points.items()
        },
        "grid": c.data.grid,
        "cards": [{"has_name": bool(card.get("name"))} for card in c.data.cards.values()],
        "schedules": list(c.data.schedules.values()),
        "unhandled_messages": [_message_summary(m) for m in c.raw_messages],
    }
    return _anonymize(async_redact_data(data, REDACT), ids)
