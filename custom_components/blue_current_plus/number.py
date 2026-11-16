"""Max charging current (backed by a Blue Current user override)."""

from __future__ import annotations

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.const import CURRENCY_EURO, EntityCategory, UnitOfElectricCurrent, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DEFAULT_MAX_CURRENT, MIN_CURRENT, TARIFF_MAX, TARIFF_MIN
from .coordinator import BlueCurrentConfigEntry, BlueCurrentCoordinator, to_float
from .entity import BlueCurrentControlEntity, BlueCurrentEntity

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BlueCurrentConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        entity
        for evse_id in coordinator.data.charge_points
        for entity in (MaxCurrentNumber(coordinator, evse_id), TariffNumber(coordinator, evse_id))
    )


class MaxCurrentNumber(BlueCurrentControlEntity, NumberEntity):
    """Maximum charging current.

    Setting a value below the charge point's max_usage creates (or edits) a
    whole-day user override. Setting it to the maximum removes the override.
    Changes are debounced, so dragging the slider costs one request.
    """

    _attr_device_class = NumberDeviceClass.CURRENT
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    _attr_native_min_value = MIN_CURRENT
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator: BlueCurrentCoordinator, evse_id: str) -> None:
        super().__init__(coordinator, evse_id, "max_current")

    @property
    def native_max_value(self) -> float:
        return to_float(self.cp.status.get("max_usage")) or DEFAULT_MAX_CURRENT

    @property
    def native_value(self) -> float | None:
        override = self.coordinator.override_current(self.evse_id)
        if override is not None:
            return override
        # No status yet: unknown rather than a made-up 32 A.
        return to_float(self.cp.status.get("max_usage"))

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        schedules = self.coordinator.schedules_for(self.evse_id)
        return {
            "override_active": bool(schedules) or self.cp.pending_override is not None,
            "schedules": [
                {
                    k: s.get(k)
                    for k in (
                        "schedule_id",
                        "start_time",
                        "stop_time",
                        "override_start_days",
                        "override_end_days",
                        "override_current",
                    )
                }
                for s in schedules
            ],
        }

    async def async_set_native_value(self, value: float) -> None:
        self.coordinator.async_request_max_current(self.evse_id, float(value))


class TariffNumber(BlueCurrentEntity, NumberEntity):
    """Reimbursement tariff per kWh, EXCLUDING VAT (as on my.bluecurrent.nl).

    Read from the charge point settings (websocket); written through the
    portal REST API. Works while the charge point itself is offline.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_unit_of_measurement = f"{CURRENCY_EURO}/{UnitOfEnergy.KILO_WATT_HOUR}"
    _attr_native_min_value = TARIFF_MIN
    _attr_native_max_value = TARIFF_MAX
    _attr_native_step = 0.01
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: BlueCurrentCoordinator, evse_id: str) -> None:
        super().__init__(coordinator, evse_id, "reimbursement_tariff")

    @property
    def available(self) -> bool:
        return super().available and self.cp.tariff_price is not None

    @property
    def native_value(self) -> float | None:
        return self.cp.tariff_price

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        t = self.cp.tariff
        return {
            "price_in_vat": t.get("price_in_vat"),
            "start_price_ex_vat": t.get("start_price_ex_vat"),
            "tariff_id": t.get("tariff_id"),
            "currency": t.get("currency"),
            "permission": t.get("permission"),
        }

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_tariff(self.evse_id, round(float(value), 2))
