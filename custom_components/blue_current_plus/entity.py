"""Base entities."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import BlueCurrentCoordinator, ChargePoint


class BlueCurrentEntity(CoordinatorEntity[BlueCurrentCoordinator]):
    """Entity bound to one charge point."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: BlueCurrentCoordinator, evse_id: str, key: str) -> None:
        super().__init__(coordinator)
        self.evse_id = evse_id
        self._attr_unique_id = f"{evse_id}_{key}"
        self._attr_translation_key = key
        cp = coordinator.data.charge_points[evse_id]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, evse_id)},
            name=cp.name if cp.name and cp.name != evse_id else f"Laadpaal {evse_id}",
            manufacturer="Blue Current",
            model=cp.model or None,
            serial_number=evse_id,
        )

    @property
    def cp(self) -> ChargePoint:
        return self.coordinator.data.charge_points[self.evse_id]

    @property
    def available(self) -> bool:
        return self.coordinator.data.connected and self.evse_id in self.coordinator.data.charge_points


class BlueCurrentControlEntity(BlueCurrentEntity):
    """Entity that sends commands: unavailable while the charge point is offline."""

    @property
    def available(self) -> bool:
        return super().available and self.cp.activity != "offline"
