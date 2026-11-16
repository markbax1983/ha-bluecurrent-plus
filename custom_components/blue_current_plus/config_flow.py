"""Config flow for Blue Current Plus."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_API_TOKEN
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType
import voluptuous as vol

from .api import (
    AlreadyConnected,
    BlueCurrentApi,
    BlueCurrentError,
    CannotConnect,
    InvalidToken,
    RequestLimitReached,
)
from .const import CONF_CLEAR_PORTAL_TOKEN, CONF_PORTAL_TOKEN, DOMAIN, LOGGER

SCHEMA = vol.Schema(
    {vol.Required(CONF_API_TOKEN): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))}
)


class BlueCurrentPlusConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return BlueCurrentPlusOptionsFlow()

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


class BlueCurrentPlusOptionsFlow(OptionsFlow):
    """Optional portal token for the my.bluecurrent.nl REST API (tariff).

    The stored token is never shown again: an empty field keeps it, the
    checkbox removes it. The portal token is a full web session token, so it
    should not be visible to everyone who opens this form.
    """

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        current = self.config_entry.options.get(CONF_PORTAL_TOKEN, "")
        if user_input is not None:
            if user_input.get(CONF_CLEAR_PORTAL_TOKEN):
                return self.async_create_entry(data={})
            token = (user_input.get(CONF_PORTAL_TOKEN) or "").strip()
            if token.lower().startswith("token "):
                token = token[6:].strip()
            token = token or current
            return self.async_create_entry(data={CONF_PORTAL_TOKEN: token} if token else {})
        schema = vol.Schema(
            {
                vol.Optional(CONF_PORTAL_TOKEN): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD)
                ),
                vol.Optional(CONF_CLEAR_PORTAL_TOKEN, default=False): bool,
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            description_placeholders={"status": "\u2713" if current else "\u2013"},
        )
