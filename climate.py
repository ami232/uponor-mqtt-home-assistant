"""Climate platform for Uponor MQTT Bridge.

This platform creates `climate` entities from devices discovered by the
`HAUponorBridge`. It listens for dispatcher signals named
`"uponor_mqtt_update_<DEVICEID>"` and updates entity state accordingly.

Note: To use this platform, add to `configuration.yaml`:

climate:
  - platform: uponor_mqtt

Alternatively, entities are created dynamically for devices already present
in the bridge cache when the platform is set up.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import async_generate_entity_id
from homeassistant.const import TEMP_CELSIUS
from homeassistant.components.climate import ClimateEntity, SUPPORT_TARGET_TEMPERATURE
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    CoordinatorEntity,
)

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_platform(
    hass: HomeAssistant, config, async_add_entities, discovery_info=None
):
    bridge = hass.data.get(DOMAIN, {}).get("bridge")
    if bridge is None:
        _LOGGER.error("Uponor bridge not initialized; ensure the integration is loaded")
        return

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].setdefault("coordinators", {})

    entities: list[UponorClimate] = []

    def _create_coordinator(device_id: str) -> DataUpdateCoordinator:
        coordinators = hass.data[DOMAIN]["coordinators"]
        if device_id in coordinators:
            return coordinators[device_id]

        async def _async_update_data():
            # return current cached state for device
            return bridge.device_state_cache.get(device_id, {})

        coordinator = DataUpdateCoordinator(
            hass,
            _LOGGER,
            name=f"uponor_{device_id}",
            update_method=_async_update_data,
        )
        coordinators[device_id] = coordinator
        # prime coordinator with current data
        hass.async_create_task(coordinator.async_refresh())
        return coordinator

    # Create entities for devices already in cache
    for device_id in list(bridge.device_state_cache.keys()):
        coordinator = _create_coordinator(device_id)
        entities.append(UponorClimate(bridge, device_id, coordinator))

    async_add_entities(entities)

    # listen for new devices and add entities dynamically
    @callback
    def _async_new_device(device_id: str) -> None:
        coordinator = _create_coordinator(device_id)
        entity = UponorClimate(bridge, device_id, coordinator)
        async_add_entities([entity])

    async_dispatcher_connect(hass, f"{DOMAIN}_device_new", _async_new_device)


class UponorClimate(CoordinatorEntity, ClimateEntity):
    _attr_supported_features = SUPPORT_TARGET_TEMPERATURE

    def __init__(
        self, bridge: Any, device_id: str, coordinator: DataUpdateCoordinator
    ) -> None:
        CoordinatorEntity.__init__(self, coordinator)
        self.bridge = bridge
        self.device_id = device_id
        self._name = f"Uponor {device_id}"
        self._unique_id = f"uponor_{device_id}"
        self._entity_id = async_generate_entity_id(
            "climate.{}", self._unique_id, hass=bridge.hass
        )

        # initial data
        data = coordinator.data or {}
        self._target_temperature = data.get("temperature")
        self._current_temperature = data.get("current_temperature")
        self._hvac_mode = data.get("mode", "heat")

    @property
    def name(self) -> str:
        return self._name

    @property
    def unique_id(self) -> str:
        return self._unique_id

    @property
    def temperature_unit(self) -> str:
        return TEMP_CELSIUS

    @property
    def target_temperature(self) -> float | None:
        return (
            self.coordinator.data.get("temperature") if self.coordinator.data else None
        )

    @property
    def current_temperature(self) -> float | None:
        return (
            self.coordinator.data.get("current_temperature")
            if self.coordinator.data
            else None
        )

    @property
    def hvac_mode(self) -> str:
        return (
            self.coordinator.data.get("mode", "heat")
            if self.coordinator.data
            else "heat"
        )

    @property
    def available(self) -> bool:
        return True

    async def async_added_to_hass(self) -> None:
        # register dispatcher to forward updates into the coordinator
        async_dispatcher_connect(
            self.bridge.hass,
            f"{DOMAIN}_update_{self.device_id}",
            lambda state: self.hass.async_create_task(
                self.coordinator.async_set_updated_data(state)
            ),
        )

    async def async_set_temperature(self, **kwargs) -> None:
        """Handle set temperature from Home Assistant UI."""
        if "temperature" not in kwargs:
            return
        temp = float(kwargs["temperature"])
        await self.bridge.async_set_temperature(self.device_id, temp)
        # optimistic update in coordinator
        self.coordinator.data = {**(self.coordinator.data or {}), "temperature": temp}
        self.async_write_ha_state()
