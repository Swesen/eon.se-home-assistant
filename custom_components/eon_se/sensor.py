"""Sensor platform for E.ON Sweden."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    KEY_DATA_DELAY_HOURS,
    KEY_ENERGY_LAST_24H,
    KEY_ENERGY_YESTERDAY,
    KEY_FACILITY_ADDRESS,
    KEY_FACILITY_ID,
    KEY_FACILITY_NAME,
    KEY_LATEST_INTERVAL_KWH,
    KEY_LATEST_POWER_W,
    KEY_LATEST_TIMESTAMP,
    MANUFACTURER,
    SENSOR_TYPE_DATA_DELAY,
    SENSOR_TYPE_DATA_TIMESTAMP,
    SENSOR_TYPE_ENERGY_LAST_24H,
    SENSOR_TYPE_ENERGY_YESTERDAY,
    SENSOR_TYPE_LATEST_INTERVAL_ENERGY,
    SENSOR_TYPE_LATEST_POWER,
)
from .coordinator import EonCoordinator


@dataclass(frozen=True, kw_only=True)
class EonSensorEntityDescription(SensorEntityDescription):
    """Describes an E.ON sensor."""

    data_key: str  # key in facility data dict


SENSOR_DESCRIPTIONS: tuple[EonSensorEntityDescription, ...] = (
    EonSensorEntityDescription(
        key=SENSOR_TYPE_ENERGY_YESTERDAY,
        data_key=KEY_ENERGY_YESTERDAY,
        name="Energy Yesterday",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:lightning-bolt-outline",
    ),
    EonSensorEntityDescription(
        key=SENSOR_TYPE_ENERGY_LAST_24H,
        data_key=KEY_ENERGY_LAST_24H,
        name="Energy Last 24 Hours",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:clock-time-twenty-four",
    ),
    EonSensorEntityDescription(
        key=SENSOR_TYPE_LATEST_POWER,
        data_key=KEY_LATEST_POWER_W,
        name="Latest Average Power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:transmission-tower",
    ),
    EonSensorEntityDescription(
        key=SENSOR_TYPE_LATEST_INTERVAL_ENERGY,
        data_key=KEY_LATEST_INTERVAL_KWH,
        name="Latest 15-min Energy",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:meter-electric",
    ),
    EonSensorEntityDescription(
        key=SENSOR_TYPE_DATA_TIMESTAMP,
        data_key=KEY_LATEST_TIMESTAMP,
        name="Latest Data Timestamp",
        device_class=SensorDeviceClass.TIMESTAMP,
        state_class=None,
        icon="mdi:clock-check",
    ),
    EonSensorEntityDescription(
        key=SENSOR_TYPE_DATA_DELAY,
        data_key=KEY_DATA_DELAY_HOURS,
        name="Data Delay",
        native_unit_of_measurement="h",
        device_class=None,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:clock-alert-outline",
        entity_registry_enabled_default=False,  # hidden by default; enable in UI if wanted
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up E.ON sensor entities."""
    coordinator: EonCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    entities: list[EonSensor] = []
    for facility_id, facility_data in (coordinator.data or {}).items():
        for description in SENSOR_DESCRIPTIONS:
            entities.append(EonSensor(coordinator, facility_id, description, entry.entry_id))

    async_add_entities(entities)


class EonSensor(CoordinatorEntity[EonCoordinator], SensorEntity):
    """A single sensor for one metric of one E.ON facility."""

    entity_description: EonSensorEntityDescription

    def __init__(
        self,
        coordinator: EonCoordinator,
        facility_id: str,
        description: EonSensorEntityDescription,
        entry_id: str,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._facility_id = facility_id
        self._attr_unique_id = f"{entry_id}_{facility_id}_{description.key}"
        self._attr_has_entity_name = True

    @property
    def _facility_data(self) -> dict[str, Any] | None:
        if self.coordinator.data:
            return self.coordinator.data.get(self._facility_id)
        return None

    @property
    def device_info(self) -> DeviceInfo:
        data = self._facility_data or {}
        name = data.get(KEY_FACILITY_NAME) or self._facility_id
        address = data.get(KEY_FACILITY_ADDRESS) or ""
        return DeviceInfo(
            identifiers={(DOMAIN, self._facility_id)},
            name=name,
            manufacturer=MANUFACTURER,
            model="Smart Meter",
            configuration_url="https://www.eon.se/mitt-e-on/din-forbrukning#/",
            suggested_area=address or None,
        )

    @property
    def native_value(self) -> float | datetime | None:
        data = self._facility_data
        if data is None:
            return None
        value = data.get(self.entity_description.data_key)
        if value is None:
            return None

        # Timestamp sensor: ensure it is timezone-aware
        if self.entity_description.device_class is SensorDeviceClass.TIMESTAMP:
            if isinstance(value, datetime):
                if value.tzinfo is None:
                    from homeassistant.util import dt as dt_util
                    value = dt_util.as_local(value)
                return value
            return None

        return value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self._facility_data
        if data is None:
            return {}
        return {
            "facility_id": self._facility_id,
            "facility_address": data.get(KEY_FACILITY_ADDRESS, ""),
            "latest_data_timestamp": str(data.get(KEY_LATEST_TIMESTAMP, "")),
        }

    @property
    def available(self) -> bool:
        return super().available and self._facility_data is not None
