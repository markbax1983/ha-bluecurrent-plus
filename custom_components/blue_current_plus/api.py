"""Thin websocket client for the Blue Current Home Assistant endpoint.

Deliberately independent of the `bluecurrent-api` PyPI package: it keeps every
message (also ones we don't understand yet), never crashes on an unexpected
payload shape and counts requests so we can stay under the daily limit.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import date, datetime
import json
from typing import Any

import aiohttp

from .const import BC_TZ, LOGGER, PORTAL_URL, URL


class BlueCurrentError(Exception):
    """Base error."""


class CannotConnect(BlueCurrentError):
    """Connection problem."""


class InvalidToken(BlueCurrentError):
    """Token rejected."""


class RequestLimitReached(BlueCurrentError):
    """Daily request limit reached."""


class AlreadyConnected(BlueCurrentError):
    """Another client is connected with this account."""


class PortalAuthFailed(BlueCurrentError):
    """The portal REST API rejected our token (does NOT mean re-auth is needed)."""


ERROR_CODES: dict[int, type[BlueCurrentError]] = {
    1: InvalidToken,
    2: InvalidToken,
    42: RequestLimitReached,
}


def error_from_message(message: dict[str, Any]) -> BlueCurrentError:
    """Map an ERROR object to an exception."""
    code = message.get("error")
    text = str(message.get("message", "unknown error"))
    exc = ERROR_CODES.get(code) if isinstance(code, int) else None
    return (exc or BlueCurrentError)(f"{code}: {text}")


class RequestCounter:
    """Counts requests per (Europe/Amsterdam) day - the limit resets at midnight."""

    def __init__(self) -> None:
        self.day: date = datetime.now(BC_TZ).date()
        self.count = 0

    def hit(self) -> None:
        today = datetime.now(BC_TZ).date()
        if today != self.day:
            self.day = today
            self.count = 0
        self.count += 1

    @property
    def today(self) -> int:
        if datetime.now(BC_TZ).date() != self.day:
            return 0
        return self.count


class BlueCurrentApi:
    """Websocket client."""

    def __init__(self, session: aiohttp.ClientSession, url: str | None = None) -> None:
        self._session = session
        self._url = url or URL
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self.auth: str | None = None
        self.requests = RequestCounter()

    @property
    def connected(self) -> bool:
        return self._ws is not None and not self._ws.closed

    # ------------------------------------------------------------------ helpers
    def _raise_from_handshake(self, err: aiohttp.WSServerHandshakeError) -> None:
        reason = str((err.headers or {}).get("x-websocket-reject-reason", ""))
        if "Request limit reached" in reason:
            raise RequestLimitReached(reason) from err
        if "Already connected" in reason:
            raise AlreadyConnected(reason) from err
        raise CannotConnect(f"handshake failed: {err.status} {reason}") from err

    async def _one_shot(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Open a connection, send one message, return the first reply."""
        self.requests.hit()
        try:
            async with self._session.ws_connect(self._url, heartbeat=30) as ws:
                await ws.send_str(json.dumps(payload))
                async with asyncio.timeout(10):
                    msg = await ws.receive()
        except aiohttp.WSServerHandshakeError as err:
            self._raise_from_handshake(err)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise CannotConnect(str(err)) from err
        if msg.type != aiohttp.WSMsgType.TEXT:
            raise CannotConnect(f"unexpected reply type {msg.type}")
        data: dict[str, Any] = json.loads(msg.data)
        if data.get("object") == "ERROR":
            raise error_from_message(data)
        return data

    # --------------------------------------------------------------------- auth
    async def validate_token(self, api_token: str) -> str:
        """Exchange the API token for a session token, return customer id."""
        res = await self._one_shot({"command": "VALIDATE_API_TOKEN", "token": api_token})
        if not res.get("success") or "token" not in res:
            raise InvalidToken("token not accepted")
        self.auth = "Token " + res["token"]
        return str(res.get("customer_id", ""))

    async def get_email(self) -> str:
        res = await self._one_shot({"command": "GET_ACCOUNT", "Authorization": self.auth})
        return str(res.get("login", ""))

    # --------------------------------------------------------------- live link
    async def run(
        self,
        on_message: Callable[[dict[str, Any]], Awaitable[None]],
        on_open: Callable[[], Awaitable[None]],
    ) -> None:
        """Connect and pump messages until the connection ends (raises on error)."""
        if not self.auth:
            raise InvalidToken("not validated")
        try:
            async with self._session.ws_connect(self._url, heartbeat=30) as ws:
                self._ws = ws
                LOGGER.debug("Websocket connected")
                await self.send("HELLO", header="homeassistant")
                await on_open()
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        try:
                            data = json.loads(msg.data)
                        except ValueError:
                            LOGGER.warning("Non-JSON message ignored: %.200s", msg.data)
                            continue
                        if not isinstance(data, dict):
                            continue
                        if data.get("object") == "ERROR":
                            err = error_from_message(data)
                            # Only auth and request-limit errors end the link;
                            # anything else is the answer to one command.
                            if isinstance(err, (InvalidToken, RequestLimitReached)):
                                raise err
                        await on_message(data)
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        raise CannotConnect(str(ws.exception()))
                if ws.close_code == 4001:
                    raise RequestLimitReached("closed with 4001")
                raise CannotConnect(f"connection closed ({ws.close_code})")
        except aiohttp.WSServerHandshakeError as err:
            self._raise_from_handshake(err)
        except aiohttp.ClientError as err:
            raise CannotConnect(str(err)) from err
        finally:
            self._ws = None

    async def send(self, command: str, **kwargs: Any) -> None:
        """Send a command on the live connection."""
        if not self.connected or self._ws is None:
            raise CannotConnect("not connected")
        payload: dict[str, Any] = {"command": command}
        payload.update({k: v for k, v in kwargs.items() if v is not None})
        payload["Authorization"] = self.auth
        self.requests.hit()
        LOGGER.debug("-> %s", command)
        await self._ws.send_str(json.dumps(payload))

    # ------------------------------------------------------- portal REST API
    async def edit_reimbursement_tariff(
        self,
        evse_id: str,
        amount: float,
        currency: str = "EUR",
        portal_token: str | None = None,
    ) -> dict[str, Any]:
        """Set the reimbursement tariff (per kWh, EXCLUDING VAT).

        Same call my.bluecurrent.nl makes. Authenticates with the websocket
        session token unless a separate portal token is given.
        """
        auth = f"Token {portal_token}" if portal_token else self.auth
        if not auth:
            raise PortalAuthFailed("no token available")
        body = {"evse_id": evse_id, "amount": f"{amount:.2f}", "currency": currency}
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Authorization": auth,
            "Origin": "https://my.bluecurrent.nl",
            "Referer": "https://my.bluecurrent.nl/",
        }
        LOGGER.debug("-> editreimbursementtariff %s", body)
        try:
            async with self._session.post(
                f"{PORTAL_URL}/editreimbursementtariff",
                json=body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status in (401, 403):
                    raise PortalAuthFailed(f"HTTP {resp.status}")
                if resp.status >= 400:
                    raise CannotConnect(f"HTTP {resp.status}")
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise CannotConnect(str(err)) from err
        if not isinstance(data, dict) or not data.get("success"):
            error = data.get("error") if isinstance(data, dict) else data
            raise BlueCurrentError(f"tariff not changed: {error or data}")
        result = data.get("result")
        return result if isinstance(result, dict) else {}

    async def close(self) -> None:
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
