"""Sensors."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import ACTIVITY_OPTIONS, DOMAIN, VEHICLE_STATUS_OPTIONS
from .coordinator import BlueCurrentConfigEntry, BlueCurrentCoordinator, ChargePoint, to_float
from .entity import BlueCurrentEntity

PARALLEL_UPDATES = 0

# Sensors that no longer exist; removed from the entity registry on setup.
# 0.1.7: "tariff" is replaced by the number entity "reimbursement_tariff".
REMOVED_KEYS = ("tariff",)


@dataclass(frozen=True, kw_only=True)
class BCSensorDescription(SensorEntityDescription):
    """Sensor description with a value function."""

    value_fn: Callable[[ChargePoint, BlueCurrentCoordinator], Any]


def _status(key: str) -> Callable[[ChargePoint, BlueCurrentCoordinator], Any]:
    return lambda cp, _c: to_float(cp.status.get(key))


def _enum(key: str, options: list[str]) -> Callable[[ChargePoint, BlueCurrentCoordinator], Any]:
    def fn(cp: ChargePoint, _c: BlueCurrentCoordinator) -> str | None:
        value = cp.status.get(key)
        return value if value in options else None

    return fn


def _phase(attr: str, idx: int) -> Callable[[ChargePoint, BlueCurrentCoordinator], Any]:
    return lambda cp, _c: getattr(cp, attr)[idx]


AMP = UnitOfElectricCurrent.AMPERE

SENSORS: tuple[BCSensorDescription, ...] = (
    BCSensorDescription(
        key="power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        suggested_display_precision=2,
        value_fn=lambda cp, _c: cp.power_kw,
    ),
    *(
        BCSensorDescription(
            key=f"power_phase_{n}",
            device_class=SensorDeviceClass.POWER,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=UnitOfPower.WATT,
            suggested_display_precision=0,
            value_fn=_phase("phase_power_w", n - 1),
        )
        for n in (1, 2, 3)
    ),
    *(
        BCSensorDescription(
            key=f"current_phase_{n}",
            device_class=SensorDeviceClass.CURRENT,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=AMP,
            suggested_display_precision=1,
            value_fn=_phase("currents", n - 1),
        )
        for n in (1, 2, 3)
    ),
    *(
        BCSensorDescription(
            key=f"voltage_phase_{n}",
            device_class=SensorDeviceClass.VOLTAGE,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=UnitOfElectricPotential.VOLT,
            suggested_display_precision=1,
            value_fn=_phase("voltages", n - 1),
        )
        for n in (1, 2, 3)
    ),
    BCSensorDescription(
        key="active_phases",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda cp, _c: cp.active_phases,
    ),
    BCSensorDescription(
        key="energy_total",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=lambda cp, _c: cp.energy_total_kwh if cp.session_key else None,
    ),
    BCSensorDescription(
        key="session_energy",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=lambda cp, _c: cp.session_energy_kwh,
    ),
    BCSensorDescription(
        key="session_reimbursement",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="EUR",
        suggested_display_precision=2,
        value_fn=lambda cp, _c: cp.session_reimbursement[0],
    ),
    BCSensorDescription(
        key="total_reimbursement",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement="EUR",
        suggested_display_precision=2,
        value_fn=lambda cp, _c: cp.reimbursement_base_eur,
    ),
    BCSensorDescription(
        key="session_start",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda cp, _c: cp.session_start,
    ),
    BCSensorDescription(
        key="session_duration",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        suggested_display_precision=0,
        value_fn=lambda cp, _c: cp.session_duration_min,
    ),
    BCSensorDescription(
        key="activity",
        device_class=SensorDeviceClass.ENUM,
        options=ACTIVITY_OPTIONS,
        value_fn=_enum("activity", ACTIVITY_OPTIONS),
    ),
    BCSensorDescription(
        key="vehicle_status",
        device_class=SensorDeviceClass.ENUM,
        options=VEHICLE_STATUS_OPTIONS,
        value_fn=_enum("vehicle_status", VEHICLE_STATUS_OPTIONS),
    ),
    BCSensorDescription(
        key="max_usage",
        device_class=SensorDeviceClass.CURRENT,
        native_unit_of_measurement=AMP,
        value_fn=_status("max_usage"),
    ),
    BCSensorDescription(
        key="smartcharging_max_usage",
        device_class=SensorDeviceClass.CURRENT,
        native_unit_of_measurement=AMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_status("smartcharging_max_usage"),
    ),
    BCSensorDescription(
        key="max_offline",
        device_class=SensorDeviceClass.CURRENT,
        native_unit_of_measurement=AMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_status("max_offline"),
    ),
    BCSensorDescription(
        key="current_left",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=AMP,
        value_fn=lambda cp, _c: cp.current_left,
    ),
    BCSensorDescription(
        key="offline_since",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda cp, _c: cp.status.get("offline_since"),
    ),
)

# Account / installation wide sensors, attached to the first charge point.
GRID_SENSORS: tuple[BCSensorDescription, ...] = (
    *(
        BCSensorDescription(
            key=f"grid_current_phase_{n}",
            device_class=SensorDeviceClass.CURRENT,
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=AMP,
            suggested_display_precision=1,
            value_fn=lambda _cp, c, n=n: c.data.grid.get(f"grid_actual_p{n}"),
        )
        for n in (1, 2, 3)
    ),
    BCSensorDescription(
        key="grid_max_current",
        device_class=SensorDeviceClass.CURRENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=AMP,
        suggested_display_precision=1,
        value_fn=lambda _cp, c: max((v for v in c.data.grid.values() if v is not None), default=None),
    ),
    BCSensorDescription(
        key="requests_today",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda _cp, c: c.api.requests.today,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BlueCurrentConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    evse_ids = list(coordinator.data.charge_points)
    registry = er.async_get(hass)
    for evse_id in evse_ids:
        for old_key in REMOVED_KEYS:
            if entity_id := registry.async_get_entity_id("sensor", DOMAIN, f"{evse_id}_{old_key}"):
                registry.async_remove(entity_id)
    entities: list[BlueCurrentSensor] = [
        BlueCurrentSensor(coordinator, evse_id, desc) for evse_id in evse_ids for desc in SENSORS
    ]
    if evse_ids:
        entities += [BlueCurrentSensor(coordinator, evse_ids[0], desc) for desc in GRID_SENSORS]
    async_add_entities(entities)


class BlueCurrentSensor(BlueCurrentEntity, SensorEntity):
    """Blue Current sensor."""

    entity_description: BCSensorDescription

    def __init__(
        self, coordinator: BlueCurrentCoordinator, evse_id: str, description: BCSensorDescription
    ) -> None:
        super().__init__(coordinator, evse_id, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        if self.entity_description.key == "requests_today":
            return True
        return super().available

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.key != "session_reimbursement":
            return None
        _amount, final = self.cp.session_reimbursement
        return {
            "final": final,
            "blue_current_total_cost": to_float(self.cp.status.get("total_cost")),
            "estimate": self.cp.session_tariff_value,
        }

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.cp, self.coordinator)
