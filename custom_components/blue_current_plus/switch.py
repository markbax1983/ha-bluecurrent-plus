"""Switches."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import BlueCurrentError
from .coordinator import BlueCurrentConfigEntry, BlueCurrentCoordinator, ChargePoint
from .entity import BlueCurrentControlEntity

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class BCSwitchDescription(SwitchEntityDescription):
    value_fn: Callable[[ChargePoint], bool | None]
    command_fn: Callable[[bool], tuple[str, dict[str, Any]]]
    setting_key: str | None = None


SWITCHES = (
    BCSwitchDescription(
        key="plug_and_charge",
        entity_category=EntityCategory.CONFIG,
        setting_key="plug_and_charge",
        value_fn=lambda cp: cp.settings.get("plug_and_charge"),
        command_fn=lambda on: ("SET_PLUG_AND_CHARGE", {"value": on}),
    ),
    BCSwitchDescription(
        key="linked_charge_cards_only",
        entity_category=EntityCategory.CONFIG,
        setting_key="linked_charge_cards_only",
        value_fn=lambda cp: cp.settings.get("linked_charge_cards_only"),
        # The backend setting is 'public charging', the inverse.
        command_fn=lambda on: ("SET_PUBLIC_CHARGING", {"value": not on}),
    ),
    BCSwitchDescription(
        key="block",
        value_fn=lambda cp: None if cp.activity is None else cp.activity == "unavailable",
        command_fn=lambda on: ("SET_INOPERATIVE" if on else "SET_OPERATIVE", {}),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BlueCurrentConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        BlueCurrentSwitch(coordinator, evse_id, desc)
        for evse_id in coordinator.data.charge_points
        for desc in SWITCHES
    )


class BlueCurrentSwitch(BlueCurrentControlEntity, SwitchEntity):
    entity_description: BCSwitchDescription

    def __init__(
        self, coordinator: BlueCurrentCoordinator, evse_id: str, description: BCSwitchDescription
    ) -> None:
        super().__init__(coordinator, evse_id, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.value_fn(self.cp)

    async def _set(self, on: bool) -> None:
        command, kwargs = self.entity_description.command_fn(on)
        try:
            await self.coordinator.async_send(command, evse_id=self.evse_id, **kwargs)
        except BlueCurrentError as err:
            raise HomeAssistantError(f"Blue Current: {err}") from err
        if self.entity_description.setting_key:
            self.coordinator.async_set_setting_optimistic(
                self.evse_id, self.entity_description.setting_key, on
            )

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)
