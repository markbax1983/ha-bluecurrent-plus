"""Buttons."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.components.button import (
    ButtonDeviceClass,
    ButtonEntity,
    ButtonEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import BlueCurrentError
from .coordinator import BlueCurrentConfigEntry, BlueCurrentCoordinator
from .entity import BlueCurrentControlEntity

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class BCButtonDescription(ButtonEntityDescription):
    press_fn: Callable[[BlueCurrentCoordinator, str], Awaitable[None]]


BUTTONS = (
    BCButtonDescription(
        key="start_session",
        press_fn=lambda c, e: c.async_start_session(e),
    ),
    BCButtonDescription(
        key="stop_session",
        press_fn=lambda c, e: c.async_send("STOP_SESSION", evse_id=e),
    ),
    BCButtonDescription(
        key="reset",
        device_class=ButtonDeviceClass.RESTART,
        entity_category=EntityCategory.CONFIG,
        press_fn=lambda c, e: c.async_send("SOFT_RESET", evse_id=e),
    ),
    BCButtonDescription(
        key="reboot",
        device_class=ButtonDeviceClass.RESTART,
        entity_category=EntityCategory.CONFIG,
        press_fn=lambda c, e: c.async_send("REBOOT", evse_id=e),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BlueCurrentConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        BlueCurrentButton(coordinator, evse_id, desc)
        for evse_id in coordinator.data.charge_points
        for desc in BUTTONS
    )


class BlueCurrentButton(BlueCurrentControlEntity, ButtonEntity):
    entity_description: BCButtonDescription

    def __init__(
        self, coordinator: BlueCurrentCoordinator, evse_id: str, description: BCButtonDescription
    ) -> None:
        super().__init__(coordinator, evse_id, description.key)
        self.entity_description = description

    async def async_press(self) -> None:
        try:
            await self.entity_description.press_fn(self.coordinator, self.evse_id)
        except BlueCurrentError as err:
            raise HomeAssistantError(f"Blue Current: {err}") from err
