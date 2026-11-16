"""Push coordinator: owns the websocket link and all charge point state."""

from __future__ import annotations

import asyncio
from collections import deque
import contextlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_TOKEN
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .api import (
    AlreadyConnected,
    BlueCurrentApi,
    BlueCurrentError,
    InvalidToken,
    PortalAuthFailed,
    RequestLimitReached,
)
from .const import (
    BACKOFF_MAX,
    BACKOFF_START,
    BC_TZ,
    CONF_PORTAL_TOKEN,
    DAY_CODES,
    DEFAULT_VAT_FACTOR,
    DOMAIN,
    LOGGER,
    NO_CARD_UID,
    OVERRIDE_DEBOUNCE,
    RAW_BUFFER_SIZE,
    STORAGE_VERSION,
    TARIFF_MAX,
    TARIFF_MIN,
    VEHICLE_STATUS,
)

EVENT_SESSION_FINISHED = f"{DOMAIN}_session_finished"
EVENT_COMMAND_RESULT = f"{DOMAIN}_command_result"

type BlueCurrentConfigEntry = ConfigEntry[BlueCurrentCoordinator]


# --------------------------------------------------------------------- parsing
def to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_datetime(value: Any) -> datetime | None:
    """Blue Current sends 'YYYYMMDD HH:MM:SS' (local time) or with a +offset."""
    if not value or not isinstance(value, str):
        return None
    for fmt in ("%Y%m%d %H:%M:%S%z", "%Y%m%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(value, fmt)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=BC_TZ)
        return parsed
    parsed = dt_util.parse_datetime(value)
    if parsed and parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BC_TZ)
    return parsed


def _setting_value(value: Any) -> bool | None:
    """Settings arrive as {'value': bool, ...} or as a bare bool."""
    if isinstance(value, dict):
        value = value.get("value")
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() == "true"
    return None


def _find_list(message: dict[str, Any], *keys: str) -> list[dict[str, Any]]:
    """Return the first list payload found under one of `keys` (defensive)."""
    for key in keys:
        value = message.get(key)
        if isinstance(value, list):
            return [v for v in value if isinstance(v, dict)]
        if isinstance(value, dict):
            for inner in value.values():
                if isinstance(inner, list):
                    return [v for v in inner if isinstance(v, dict)]
    return []


def _days(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str):
        return [d for d in value.strip("[]").replace(" ", "").split(",") if d]
    return []


# ------------------------------------------------------------------ data model
@dataclass
class ChargePoint:
    """State of one charge point."""

    evse_id: str
    name: str = ""
    model: str = ""
    info: dict[str, Any] = field(default_factory=dict)
    status: dict[str, Any] = field(default_factory=dict)
    settings: dict[str, Any] = field(default_factory=dict)
    # energy bookkeeping (persisted)
    energy_base_kwh: float = 0.0
    reimbursement_base_eur: float = 0.0
    session_key: str | None = None
    session_kwh: float = 0.0
    finished_key: str | None = None
    session_seen_open: bool = False
    # user selections (persisted)
    card_uid: str = NO_CARD_UID
    # optimistic override value while a request is in flight
    pending_override: float | None = None

    # -- raw values
    def _phase(self, prefix: str) -> list[float | None]:
        return [to_float(self.status.get(f"{prefix}{i}")) for i in (1, 2, 3)]

    @property
    def voltages(self) -> list[float | None]:
        return self._phase("actual_v")

    @property
    def currents(self) -> list[float | None]:
        return self._phase("actual_p")

    # -- derived values
    @property
    def phase_power_w(self) -> list[float | None]:
        return [
            None if v is None or i is None else round(v * i, 1)
            for v, i in zip(self.voltages, self.currents, strict=True)
        ]

    @property
    def power_kw(self) -> float | None:
        """Real power: sum of U*I per phase (not avg*avg*sqrt(3))."""
        powers = [p for p in self.phase_power_w if p is not None]
        if not powers:
            return None
        return round(sum(powers) / 1000, 3)

    @property
    def active_phases(self) -> int:
        return sum(1 for i in self.currents if i is not None and i >= 0.5)

    @property
    def smart_charging(self) -> bool:
        return bool(self.settings.get("smart_charging", self.info.get("smart_charging")))

    @property
    def current_limit(self) -> float | None:
        key = "smartcharging_max_usage" if self.smart_charging else "max_usage"
        return to_float(self.status.get(key))

    @property
    def current_left(self) -> float | None:
        """Headroom on the busiest phase (not the average)."""
        limit = self.current_limit
        if limit is None:
            return None
        used = max((i for i in self.currents if i is not None), default=0.0)
        return round(limit - used, 1)

    @property
    def session_start(self) -> datetime | None:
        return self.status.get("start_datetime")

    @property
    def session_stop(self) -> datetime | None:
        return self.status.get("stop_datetime")

    @property
    def session_duration_min(self) -> float | None:
        start = self.session_start
        if start is None:
            return None
        end = self.session_stop or dt_util.now()
        return round(max((end - start).total_seconds(), 0) / 60, 1)

    @property
    def tariff(self) -> dict[str, Any]:
        tariff = self.settings.get("tariff") or self.info.get("tariff")
        return tariff if isinstance(tariff, dict) else {}

    @property
    def tariff_price(self) -> float | None:
        """Price per kWh as set on my.bluecurrent.nl (ex VAT, what you get reimbursed)."""
        return to_float(self.tariff.get("price_ex_vat"))

    @property
    def session_energy_kwh(self) -> float | None:
        """kWh of the current/last session. The backend resets actual_kwh to 0 on
        unplug; keep showing what we counted for that same session."""
        kwh = to_float(self.status.get("actual_kwh"))
        start = self.session_start
        if start is not None and self.session_key == start.isoformat():
            return max(kwh or 0.0, self.session_kwh)
        return kwh

    @property
    def session_tariff_value(self) -> float | None:
        """kWh x tariff (+ start fee): what this session is worth at your tariff."""
        price = self.tariff_price
        kwh = self.session_energy_kwh
        if price is None or kwh is None:
            return None
        start_fee = to_float(self.tariff.get("start_price_ex_vat")) or 0.0
        return round(kwh * price + start_fee, 2)

    @property
    def session_finished(self) -> bool:
        start, stop = self.session_start, self.session_stop
        return start is not None and stop is not None and stop >= start

    @property
    def session_reimbursement(self) -> tuple[float | None, bool]:
        """(amount, final). Live estimate while charging; Blue Current's own
        amount (total_cost) once the session has ended."""
        estimate = self.session_tariff_value
        cost = to_float(self.status.get("total_cost"))
        if self.session_finished and cost is not None and (cost > 0 or not estimate):
            return round(cost, 2), True
        return estimate, False

    @property
    def energy_total_kwh(self) -> float:
        return round(self.energy_base_kwh + self.session_kwh, 3)

    @property
    def activity(self) -> str | None:
        return self.status.get("activity")

    @property
    def vehicle_status(self) -> str | None:
        return self.status.get("vehicle_status")

    def as_storage(self) -> dict[str, Any]:
        return {
            "energy_base_kwh": self.energy_base_kwh,
            "reimbursement_base_eur": self.reimbursement_base_eur,
            "session_key": self.session_key,
            "session_kwh": self.session_kwh,
            "finished_key": self.finished_key,
            "session_seen_open": self.session_seen_open,
            "card_uid": self.card_uid,
        }


@dataclass
class HubData:
    """Everything the entities need."""

    charge_points: dict[str, ChargePoint] = field(default_factory=dict)
    grid: dict[str, Any] = field(default_factory=dict)
    cards: dict[str, dict[str, Any]] = field(default_factory=dict)
    schedules: dict[str, dict[str, Any]] = field(default_factory=dict)
    connected: bool = False
    last_message: datetime | None = None


# ----------------------------------------------------------------- coordinator
class BlueCurrentCoordinator(DataUpdateCoordinator[HubData]):
    """Holds the websocket connection and pushes updates to entities."""

    config_entry: BlueCurrentConfigEntry

    def __init__(self, hass: HomeAssistant, entry: BlueCurrentConfigEntry, api: BlueCurrentApi) -> None:
        super().__init__(hass, LOGGER, config_entry=entry, name=DOMAIN, update_interval=None)
        self.api = api
        self.data = HubData()
        self.raw_messages: deque[dict[str, Any]] = deque(maxlen=RAW_BUFFER_SIZE)
        self.first_charge_points = asyncio.Event()
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self._stored: dict[str, Any] = {}
        self._override_timers: dict[str, CALLBACK_TYPE] = {}
        self._override_targets: dict[str, float] = {}
        self._stopping = False

    async def _async_update_data(self) -> HubData:
        # Push based: nothing to poll.
        return self.data

    # ------------------------------------------------------------- storage
    async def async_load(self) -> None:
        self._stored = await self._store.async_load() or {}

    @callback
    def _schedule_save(self) -> None:
        self._store.async_delay_save(
            lambda: {evse: cp.as_storage() for evse, cp in self.data.charge_points.items()},
            30,
        )

    async def async_shutdown(self) -> None:
        self._stopping = True
        for cancel in self._override_timers.values():
            cancel()
        await self._store.async_save({evse: cp.as_storage() for evse, cp in self.data.charge_points.items()})
        await self.api.close()
        await super().async_shutdown()

    # ------------------------------------------------------------ run loop
    async def run(self) -> None:
        """Keep the websocket alive forever (runs as background task)."""
        backoff = BACKOFF_START
        while not self._stopping:
            started = time.monotonic()
            delay: float = backoff
            try:
                await self.api.run(self._on_message, self._on_open)
            except asyncio.CancelledError:
                raise
            except RequestLimitReached:
                now = datetime.now(BC_TZ)
                reset = (now + timedelta(days=1)).replace(hour=0, minute=1, second=0)
                delay = (reset - now).total_seconds()
                LOGGER.warning(
                    "Blue Current request limit reached (%s requests today); reconnecting after midnight",
                    self.api.requests.today,
                )
                self._create_issue("request_limit")
            except AlreadyConnected:
                LOGGER.warning(
                    "Blue Current reports another active connection for this account "
                    "(is the original Blue Current integration still enabled?)"
                )
                self._create_issue("already_connected")
                delay = BACKOFF_MAX
            except InvalidToken:
                try:
                    await self.api.validate_token(self.config_entry.data[CONF_API_TOKEN])
                    delay = 1
                except InvalidToken:
                    LOGGER.error("Blue Current API token rejected, starting re-auth")
                    self._set_connected(False)
                    self.config_entry.async_start_reauth(self.hass)
                    return
                except BlueCurrentError as err:
                    LOGGER.debug("Token refresh failed: %s", err)
            except BlueCurrentError as err:
                LOGGER.debug("Websocket disconnected: %s", err)
            except Exception:  # noqa: BLE001 - never let the loop die
                LOGGER.exception("Unexpected error in Blue Current websocket loop")

            self._set_connected(False)
            if time.monotonic() - started > 120:
                backoff = BACKOFF_START  # connection was healthy, start over
            else:
                backoff = min(backoff * 2, BACKOFF_MAX)
            LOGGER.debug("Reconnecting in %.0f s", delay)
            await asyncio.sleep(delay)

    @callback
    def _set_connected(self, value: bool) -> None:
        if self.data.connected != value:
            self.data.connected = value
            self.async_set_updated_data(self.data)

    def _create_issue(self, key: str) -> None:
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            key,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=key,
        )

    async def _on_open(self) -> None:
        await self.api.send("GET_CHARGE_POINTS")

    # ------------------------------------------------------------ messages
    async def _on_message(self, message: dict[str, Any]) -> None:
        obj = str(message.get("object", ""))
        self.data.last_message = dt_util.utcnow()
        if not self.data.connected:
            self.data.connected = True
            for key in ("request_limit", "already_connected"):
                ir.async_delete_issue(self.hass, DOMAIN, key)

        handled = True
        if obj == "CHARGE_POINTS":
            await self._handle_charge_points(message)
        elif obj == "CH_STATUS":
            evse_id = (message.get("data") or {}).get("evse_id")
            cp = self.data.charge_points.get(evse_id)
            before = cp.vehicle_status if cp else None
            self._handle_status(message)
            # Unplugged: pushes don't carry the stop time, fetch it once.
            if cp and before not in (None, "standby") and cp.vehicle_status == "standby":
                await self.api.send("GET_CH_STATUS", evse_id=evse_id)
        elif obj == "CH_SETTINGS":
            self._handle_settings(message)
        elif "GRID" in obj:
            self._handle_grid(message)
        elif obj in ("STATUS_SET_PUBLIC_CHARGING", "STATUS_SET_PLUG_AND_CHARGE"):
            self._handle_setting_change(message)
        elif obj == "CHARGE_CARDS":
            self._handle_cards(message)
        elif obj == "LIST_OVERRIDE_CURRENT":
            self.data.schedules = {}
            for schedule in _find_list(message, "data", "schedules", "result"):
                self._store_schedule(schedule)
        elif obj in ("POST_SET_OVERRIDE_CURRENT", "POST_EDIT_OVERRIDE_CURRENT"):
            schedule = message.get("data")
            if isinstance(schedule, dict):
                self._store_schedule(schedule)
            else:
                # Unknown shape: ask for the list again rather than guessing.
                self.raw_messages.append(message)
                await self.api.send("LIST_OVERRIDE_CURRENT")
            self._clear_pending_overrides()
        elif obj == "POST_CLEAR_OVERRIDE_CURRENT":
            self._clear_pending_overrides()
        elif (
            any(key in obj for key in ("START_SESSION", "STOP_SESSION", "SOFT_RESET", "REBOOT"))
            or "OPERATIVE" in obj
            or obj in ("HELLO",)
        ):
            self._handle_command_result(obj, message)
        else:
            handled = False

        if not handled or message.get("error"):
            LOGGER.debug("Unhandled/erroneous message %s: %s", obj, message)
            self.raw_messages.append(message)

        self.async_set_updated_data(self.data)

    async def _handle_charge_points(self, message: dict[str, Any]) -> None:
        points = _find_list(message, "data")
        for info in points:
            evse_id = info.get("evse_id")
            if not evse_id:
                continue
            cp = self.data.charge_points.get(evse_id)
            if cp is None:
                cp = ChargePoint(evse_id=evse_id)
                stored = self._stored.get(evse_id, {})
                cp.energy_base_kwh = float(stored.get("energy_base_kwh", 0.0))
                cp.reimbursement_base_eur = float(stored.get("reimbursement_base_eur", 0.0))
                cp.session_key = stored.get("session_key")
                cp.session_kwh = float(stored.get("session_kwh", 0.0))
                cp.finished_key = stored.get("finished_key")
                cp.session_seen_open = bool(stored.get("session_seen_open", False))
                preferred = info.get("preferred_card") or info.get("default_card") or {}
                cp.card_uid = stored.get("card_uid") or preferred.get("uid") or NO_CARD_UID
                self.data.charge_points[evse_id] = cp
            cp.info = info
            cp.name = info.get("name") or evse_id
            cp.model = info.get("model_type", "")

        for evse_id in self.data.charge_points:
            await self.api.send("GET_CH_STATUS", evse_id=evse_id)
            await self.api.send("GET_CH_SETTINGS", evse_id=evse_id)
        if self.data.charge_points:
            await self.api.send("GET_GRID_STATUS", evse_id=next(iter(self.data.charge_points)))
        await self.api.send("GET_CHARGE_CARDS", limit=100)
        await self.api.send("LIST_OVERRIDE_CURRENT")
        self.first_charge_points.set()

    def _handle_status(self, message: dict[str, Any]) -> None:
        data = message.get("data")
        if not isinstance(data, dict):
            return
        evse_id = data.get("evse_id") or message.get("evse_id")
        cp = self.data.charge_points.get(evse_id)
        if cp is None:
            return
        status = dict(data)
        if "offline_since" in status:
            status["offline_since"] = parse_datetime(status["offline_since"])
        # Live pushes send start/stop as null/"" - only the initial GET_CH_STATUS
        # carries them. Keep the last known value instead of wiping it.
        for key in ("start_datetime", "stop_datetime"):
            if key in status:
                parsed = parse_datetime(status[key])
                if parsed is None:
                    status.pop(key)
                else:
                    status[key] = parsed
        new_start = status.get("start_datetime")
        if (
            new_start is not None
            and new_start != cp.status.get("start_datetime")
            and "stop_datetime" not in status
        ):
            status["stop_datetime"] = None  # a new session has no end yet
        if "vehicle_status" in status:
            status["vehicle_status"] = VEHICLE_STATUS.get(
                str(status["vehicle_status"]), status["vehicle_status"]
            )
        cp.status.update(status)
        self._account_energy(cp)

    def _account_energy(self, cp: ChargePoint) -> None:
        """Build a lifetime, never-decreasing kWh counter from per-session values."""
        kwh = to_float(cp.status.get("actual_kwh"))
        start = cp.session_start
        if kwh is None or start is None:
            return
        key = start.isoformat()
        if cp.session_key is not None and key != cp.session_key:
            known = datetime.fromisoformat(cp.session_key)
            if start < known:
                # An older session than the one we already counted: ignore.
                return
        if key != cp.session_key:
            if cp.session_key is not None:
                cp.energy_base_kwh = round(cp.energy_base_kwh + cp.session_kwh, 3)
            cp.session_key = key
            cp.session_kwh = kwh
            cp.session_seen_open = False
        elif kwh > cp.session_kwh:
            cp.session_kwh = kwh
        stop = cp.session_stop
        if stop is None or stop < start:
            cp.session_seen_open = True
        # A stop time on the current session means it is finished. Only report
        # sessions we saw running - not an old one found at startup.
        elif cp.finished_key != key:
            cp.finished_key = key
            if cp.session_seen_open:
                amount, _final = cp.session_reimbursement
                cp.reimbursement_base_eur = round(cp.reimbursement_base_eur + (amount or 0.0), 2)
                self._fire_finished(cp, start, stop)
        self._schedule_save()

    def _fire_finished(self, cp: ChargePoint, start: datetime, stop: datetime) -> None:
        self.hass.bus.async_fire(
            EVENT_SESSION_FINISHED,
            {
                "evse_id": cp.evse_id,
                "start": start.isoformat(),
                "stop": stop.isoformat(),
                "energy_kwh": cp.session_kwh,
                "reimbursement": cp.session_reimbursement[0],
                "reimbursement_final": cp.session_reimbursement[1],
                "duration_min": cp.session_duration_min,
            },
        )

    def _handle_settings(self, message: dict[str, Any]) -> None:
        data = message.get("data")
        if not isinstance(data, dict):
            return
        cp = self.data.charge_points.get(data.get("evse_id") or message.get("evse_id"))
        if cp is None:
            return
        settings = dict(data)
        settings["plug_and_charge"] = _setting_value(data.get("plug_and_charge"))
        public = _setting_value(data.get("public_charging"))
        settings["linked_charge_cards_only"] = None if public is None else not public
        cp.settings.update(settings)

    def _handle_setting_change(self, message: dict[str, Any]) -> None:
        cp = self.data.charge_points.get(message.get("evse_id"))
        if cp is None:
            return
        result = message.get("result")
        setting = result.get("setting") if isinstance(result, dict) else result
        value = "true" in str(setting).lower()
        if message["object"] == "STATUS_SET_PUBLIC_CHARGING":
            cp.settings["linked_charge_cards_only"] = not value
        else:
            cp.settings["plug_and_charge"] = value

    def _handle_grid(self, message: dict[str, Any]) -> None:
        data = message.get("data") if isinstance(message.get("data"), dict) else message
        grid = {k: to_float(data.get(k)) for k in ("grid_actual_p1", "grid_actual_p2", "grid_actual_p3")}
        if all(v is None for v in grid.values()):
            return
        self.data.grid = grid

    def _handle_cards(self, message: dict[str, Any]) -> None:
        cards: dict[str, dict[str, Any]] = {}
        for card in _find_list(message, "data", "cards"):
            uid = card.get("uid")
            if not uid:
                continue
            cards[str(uid)] = card
        self.data.cards = cards

    def _handle_command_result(self, obj: str, message: dict[str, Any]) -> None:
        if obj == "HELLO" or ("RECEIVED" in obj and not message.get("error")):
            return
        if "OPERATIVE" in obj:
            return
        success = message.get("success")
        error = message.get("error")
        if error or success is False:
            LOGGER.warning("Blue Current command %s failed: %s", obj, error or message)
        self.hass.bus.async_fire(
            EVENT_COMMAND_RESULT,
            {
                "evse_id": message.get("evse_id"),
                "command": obj.replace("STATUS_", "").replace("RECEIVED_", ""),
                "success": bool(success) and not error,
                "error": error,
            },
        )

    # ----------------------------------------------------------- overrides
    def _store_schedule(self, schedule: dict[str, Any]) -> None:
        schedule_id = schedule.get("schedule_id")
        if schedule_id is None:
            return
        schedule = dict(schedule)
        schedule["charge_points"] = list(schedule.get("charge_points") or schedule.get("chargepoints") or [])
        schedule["override_start_days"] = _days(schedule.get("override_start_days"))
        schedule["override_end_days"] = _days(schedule.get("override_end_days"))
        self.data.schedules[str(schedule_id)] = schedule

    def schedules_for(self, evse_id: str) -> list[dict[str, Any]]:
        return [s for s in self.data.schedules.values() if evse_id in s["charge_points"]]

    def override_current(self, evse_id: str) -> float | None:
        cp = self.data.charge_points[evse_id]
        if cp.pending_override is not None:
            return cp.pending_override
        for schedule in self.schedules_for(evse_id):
            value = to_float(schedule.get("override_current", schedule.get("overridevalue")))
            if value is not None:
                return value
        return None

    @callback
    def _clear_pending_overrides(self) -> None:
        for evse_id, cp in self.data.charge_points.items():
            if evse_id not in self._override_timers:
                cp.pending_override = None

    @callback
    def async_request_max_current(self, evse_id: str, amps: float) -> None:
        """Debounced: the last value within OVERRIDE_DEBOUNCE seconds wins."""
        cp = self.data.charge_points[evse_id]
        cp.pending_override = amps
        self._override_targets[evse_id] = amps
        if cancel := self._override_timers.pop(evse_id, None):
            cancel()

        @callback
        def _fire(_now: Any) -> None:
            self._override_timers.pop(evse_id, None)
            target = self._override_targets.pop(evse_id)
            self.config_entry.async_create_background_task(
                self.hass, self._apply_override(evse_id, target), f"{DOMAIN}_override"
            )

        self._override_timers[evse_id] = async_call_later(self.hass, OVERRIDE_DEBOUNCE, _fire)
        self.async_set_updated_data(self.data)

    def _override_payload(self, evse_ids: list[str], amps: float, base: dict | None = None) -> dict[str, Any]:
        if base is None:
            # Whole-day window, every day. Keeps the timezone question out of play.
            return {
                "chargepoints": evse_ids,
                "overridestarttime": "00:00",
                "overridestartdays": DAY_CODES,
                "overridestoptime": "23:59",
                "overridestopdays": DAY_CODES,
                "overridevalue": amps,
            }
        return {
            "chargepoints": evse_ids,
            "overridestarttime": base.get("start_time", "00:00"),
            "overridestartdays": base.get("override_start_days", DAY_CODES),
            "overridestoptime": base.get("stop_time", "23:59"),
            "overridestopdays": base.get("override_end_days", DAY_CODES),
            "overridevalue": base.get("override_current", amps),
        }

    async def _apply_override(self, evse_id: str, amps: float) -> None:
        cp = self.data.charge_points[evse_id]
        limit = to_float(cp.status.get("max_usage"))
        existing = self.schedules_for(evse_id)
        clear = limit is not None and amps >= limit
        try:
            own = [s for s in existing if s["charge_points"] == [evse_id]]
            shared = [s for s in existing if s["charge_points"] != [evse_id]]
            # Take this charge point out of schedules shared with others.
            for schedule in shared:
                schedule["charge_points"] = [e for e in schedule["charge_points"] if e != evse_id]
                await self.api.send(
                    "POST_EDIT_OVERRIDE_CURRENT",
                    schedule_id=schedule["schedule_id"],
                    **self._override_payload(schedule["charge_points"], amps, schedule),
                )
            if clear:
                for schedule in own:
                    self.data.schedules.pop(str(schedule["schedule_id"]), None)
                    await self.api.send(
                        "POST_CLEAR_OVERRIDE_CURRENT", schedule_id=int(schedule["schedule_id"])
                    )
                cp.pending_override = None
            elif own:
                schedule = own[0]
                schedule["override_current"] = amps
                await self.api.send(
                    "POST_EDIT_OVERRIDE_CURRENT",
                    schedule_id=schedule["schedule_id"],
                    **self._override_payload([evse_id], amps),
                )
                for extra in own[1:]:
                    self.data.schedules.pop(str(extra["schedule_id"]), None)
                    await self.api.send("POST_CLEAR_OVERRIDE_CURRENT", schedule_id=int(extra["schedule_id"]))
            else:
                await self.api.send("POST_SET_OVERRIDE_CURRENT", **self._override_payload([evse_id], amps))
        except BlueCurrentError as err:
            cp.pending_override = None
            LOGGER.error("Setting max current on %s failed: %s", evse_id, err)
        self.async_set_updated_data(self.data)

    # ------------------------------------------------------------ commands
    async def async_send(self, command: str, **kwargs: Any) -> None:
        await self.api.send(command, **kwargs)

    async def async_start_session(self, evse_id: str, card_uid: str | None = None) -> None:
        uid = card_uid or self.data.charge_points[evse_id].card_uid or NO_CARD_UID
        await self.api.send("START_SESSION", evse_id=evse_id, session_token=uid)

    async def async_set_tariff(self, evse_id: str, amount: float) -> None:
        """Set the reimbursement tariff (EUR/kWh ex VAT) via the portal REST API."""
        cp = self.data.charge_points[evse_id]
        tariff = cp.tariff
        if tariff.get("permission") not in (None, "write"):
            raise HomeAssistantError("This account may not change the tariff of this charge point")
        if not TARIFF_MIN <= amount <= TARIFF_MAX:
            raise ServiceValidationError(f"Tariff must be between {TARIFF_MIN} and {TARIFF_MAX} EUR/kWh")
        portal_token = self.config_entry.options.get(CONF_PORTAL_TOKEN) or None
        try:
            result = await self.api.edit_reimbursement_tariff(
                evse_id, amount, str(tariff.get("currency") or "EUR"), portal_token
            )
        except PortalAuthFailed as err:
            raise HomeAssistantError(
                "Blue Current rejected the token for changing the tariff. Set a portal "
                "token in the integration options (Configure)."
            ) from err
        except BlueCurrentError as err:
            raise HomeAssistantError(f"Changing the tariff failed: {err}") from err

        new = to_float(result.get("amount"))
        new = round(amount, 2) if new is None else new
        old_ex, old_in = to_float(tariff.get("price_ex_vat")), to_float(tariff.get("price_in_vat"))
        factor = old_in / old_ex if old_ex and old_in else DEFAULT_VAT_FACTOR
        cp.settings["tariff"] = {**tariff, "price_ex_vat": new, "price_in_vat": round(new * factor, 4)}
        LOGGER.info("Tariff of %s set to %.2f EUR/kWh ex VAT", evse_id, new)
        self.async_set_updated_data(self.data)
        # Confirm with the backend's own view (also refreshes tariff_id).
        with contextlib.suppress(BlueCurrentError):
            await self.api.send("GET_CH_SETTINGS", evse_id=evse_id)

    @callback
    def async_select_card(self, evse_id: str, uid: str) -> None:
        self.data.charge_points[evse_id].card_uid = uid
        self._schedule_save()
        self.async_set_updated_data(self.data)

    @callback
    def async_set_setting_optimistic(self, evse_id: str, key: str, value: bool) -> None:
        self.data.charge_points[evse_id].settings[key] = value
        self.async_set_updated_data(self.data)
