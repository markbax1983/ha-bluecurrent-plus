"""Binary sensors."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import BlueCurrentConfigEntry, BlueCurrentCoordinator, ChargePoint
from .entity import BlueCurrentEntity


@dataclass(frozen=True, kw_only=True)
class BCBinaryDescription(BinarySensorEntityDescription):
    value_fn: Callable[[ChargePoint], bool | None]


BINARY_SENSORS = (
    BCBinaryDescription(
        key="charging",
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
        value_fn=lambda cp: None if cp.activity is None else cp.activity == "charging",
    ),
    BCBinaryDescription(
        key="vehicle_connected",
        device_class=BinarySensorDeviceClass.PLUG,
        value_fn=lambda cp: None if cp.vehicle_status is None else cp.vehicle_status not in ("standby",),
    ),
    BCBinaryDescription(
        key="override_active",
        value_fn=lambda cp: None,  # filled in by the entity (needs coordinator)
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BlueCurrentConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[BinarySensorEntity] = [
        BlueCurrentBinarySensor(coordinator, evse_id, desc)
        for evse_id in coordinator.data.charge_points
        for desc in BINARY_SENSORS
    ]
    if coordinator.data.charge_points:
        entities.append(ConnectionSensor(coordinator, next(iter(coordinator.data.charge_points))))
    async_add_entities(entities)


class BlueCurrentBinarySensor(BlueCurrentEntity, BinarySensorEntity):
    entity_description: BCBinaryDescription

    def __init__(
        self, coordinator: BlueCurrentCoordinator, evse_id: str, description: BCBinaryDescription
    ) -> None:
        super().__init__(coordinator, evse_id, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        if self.entity_description.key == "override_active":
            return self.coordinator.override_current(self.evse_id) is not None
        return self.entity_description.value_fn(self.cp)


class ConnectionSensor(BlueCurrentEntity, BinarySensorEntity):
    """Is the websocket to Blue Current up?"""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: BlueCurrentCoordinator, evse_id: str) -> None:
        super().__init__(coordinator, evse_id, "cloud_connection")

    @property
    def available(self) -> bool:
        return True

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.connected

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        last = self.coordinator.data.last_message
        return {
            "last_message": last.isoformat() if last else None,
            "requests_today": self.coordinator.api.requests.today,
        }
