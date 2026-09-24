"""Config flow for Blue Current Plus."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.config_entries import SOURCE_REAUTH, ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_API_TOKEN
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import voluptuous as vol

from .api import (
    AlreadyConnected,
    BlueCurrentApi,
    BlueCurrentError,
    CannotConnect,
    InvalidToken,
    RequestLimitReached,
)
from .const import DOMAIN, LOGGER

SCHEMA = vol.Schema({vol.Required(CONF_API_TOKEN): str})


class BlueCurrentPlusConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow."""

    VERSION = 1

    async def _validate(self, token: str) -> tuple[str, str]:
        api = BlueCurrentApi(async_get_clientsession(self.hass))
        customer_id = await api.validate_token(token)
        try:
            email = await api.get_email()
        except BlueCurrentError:
            email = ""
        return customer_id, email

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                customer_id, email = await self._validate(user_input[CONF_API_TOKEN])
            except InvalidToken:
                errors["base"] = "invalid_token"
            except RequestLimitReached:
                errors["base"] = "limit_reached"
            except AlreadyConnected:
                errors["base"] = "already_connected"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                LOGGER.exception("Unexpected error validating token")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(customer_id)
                if self.source == SOURCE_REAUTH:
                    self._abort_if_unique_id_mismatch(reason="wrong_account")
                    return self.async_update_reload_and_abort(
                        self._get_reauth_entry(), data_updates=user_input
                    )
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=email or f"Blue Current {customer_id}", data=user_input)
        return self.async_show_form(step_id="user", data_schema=SCHEMA, errors=errors)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_user()
