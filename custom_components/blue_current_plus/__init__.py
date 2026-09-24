"""Blue Current Plus - a smarter Home Assistant integration for Blue Current chargers."""

from __future__ import annotations

import asyncio

from homeassistant.const import CONF_API_TOKEN, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import BlueCurrentApi, BlueCurrentError, InvalidToken
from .const import DOMAIN
from .coordinator import BlueCurrentConfigEntry, BlueCurrentCoordinator

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]

FIRST_DATA_TIMEOUT = 30


async def async_setup_entry(hass: HomeAssistant, entry: BlueCurrentConfigEntry) -> bool:
    """Set up Blue Current Plus from a config entry."""
    api = BlueCurrentApi(async_get_clientsession(hass))
    try:
        await api.validate_token(entry.data[CONF_API_TOKEN])
    except InvalidToken as err:
        raise ConfigEntryAuthFailed("API token rejected") from err
    except BlueCurrentError as err:
        raise ConfigEntryNotReady(str(err)) from err

    coordinator = BlueCurrentCoordinator(hass, entry, api)
    await coordinator.async_load()

    task = entry.async_create_background_task(hass, coordinator.run(), f"{DOMAIN}_websocket")
    try:
        async with asyncio.timeout(FIRST_DATA_TIMEOUT):
            await coordinator.first_charge_points.wait()
    except TimeoutError as err:
        task.cancel()
        await api.close()
        raise ConfigEntryNotReady(
            "No charge points received from Blue Current (is the original "
            "Blue Current integration still connected?)"
        ) from err

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: BlueCurrentConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_shutdown()
    return unloaded
