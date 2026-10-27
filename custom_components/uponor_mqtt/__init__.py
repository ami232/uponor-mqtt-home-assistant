"""Uponor MQTT Bridge integration for Home Assistant.

This integration reads raw Uponor Smatrix bus packets from an MQTT topic
(published by an RS485-to-MQTT gateway), exposes the thermostats as climate
entities via MQTT discovery, and publishes temperature commands back to the
bus through a second MQTT topic. It requires the `mqtt` integration to be set
up.
"""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.components import mqtt
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import ConfigEntryNotReady, ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .bridge import HAUponorBridge
from .const import (
    CAPTURE_FILE,
    CONF_CAPTURE,
    CONF_DEVICE_ID,
    CONF_DISCOVERY_PREFIX,
    CONF_HUMIDITY_ENTITY,
    CONF_READ_TOPIC,
    CONF_SYSTEM_ID,
    CONF_TEMPERATURE_ENTITY,
    CONF_VIRTUAL_HOST,
    CONF_VIRTUAL_PORT,
    CONF_VIRTUAL_THERMOSTATS,
    CONF_WRITE_TOPIC,
    DEFAULT_READ_TOPIC,
    DEFAULT_VIRTUAL_PORT,
    DEFAULT_WRITE_TOPIC,
    DOMAIN,
    HA_DISCOVERY_PREFIX,
)
from .virtual import REGISTRATION_WINDOW, GatewayConnection, VirtualThermostat

_LOGGER = logging.getLogger(__name__)

SERVICE_CAPTURE_NOTE = "capture_note"
CAPTURE_NOTE_SCHEMA = vol.Schema({vol.Required("text"): cv.string})
DEVICE_ID_FIELD = vol.Match(r"^[0-9A-Fa-f]{4}$")
VIRTUAL_SCHEMA = vol.Schema({vol.Optional("device_id"): DEVICE_ID_FIELD})
REGISTER_SCHEMA = VIRTUAL_SCHEMA.extend(
    {
        vol.Optional("seconds", default=REGISTRATION_WINDOW): vol.All(
            vol.Coerce(int), vol.Range(min=10, max=600)
        )
    }
)
SERVICE_MEASURE_LATENCY = "measure_latency"
MEASURE_LATENCY_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): DEVICE_ID_FIELD,
        vol.Optional("count", default=20): vol.All(int, vol.Range(min=1, max=50)),
    }
)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the Uponor MQTT Bridge integration.

    This integration does not need any YAML config; the bridge is started from
    the config entry.
    """
    # keep minimal setup; real start occurs in config entries
    hass.data.setdefault(DOMAIN, {})

    async def _capture_note(call: ServiceCall) -> None:
        bridge: HAUponorBridge | None = hass.data.get(DOMAIN, {}).get("bridge")
        if bridge is None or not bridge.capturing:
            raise ServiceValidationError(
                "Bus capture is off; enable it in the integration options"
            )
        bridge.capture("note", text=call.data["text"])

    hass.services.async_register(
        DOMAIN, SERVICE_CAPTURE_NOTE, _capture_note, schema=CAPTURE_NOTE_SCHEMA
    )

    async def _measure_latency(call: ServiceCall) -> ServiceResponse:
        bridge: HAUponorBridge | None = hass.data.get(DOMAIN, {}).get("bridge")
        if bridge is None:
            raise ServiceValidationError("Uponor MQTT Bridge is not loaded")
        return await bridge.async_measure_latency(
            call.data["device_id"], call.data["count"]
        )

    def _virtuals(call: ServiceCall, *, single: bool) -> list[VirtualThermostat]:
        """Virtual thermostats an action applies to: the given one, or all."""
        bridge: HAUponorBridge | None = hass.data.get(DOMAIN, {}).get("bridge")
        virtuals = bridge.virtuals if bridge else {}
        if not virtuals:
            raise ServiceValidationError(
                "No virtual thermostat; add one in the integration options"
            )
        if device_id := call.data.get("device_id"):
            if (virtual := virtuals.get(device_id.upper())) is None:
                raise ServiceValidationError(f"No virtual thermostat {device_id.upper()}")
            return [virtual]
        if single and len(virtuals) > 1:
            raise ServiceValidationError(
                "Several virtual thermostats; give the device_id to register"
            )
        return list(virtuals.values())

    async def _register_virtual(call: ServiceCall) -> None:
        (target,) = _virtuals(call, single=True)
        # Only one thermostat may answer the controller's registration broadcast
        for virtual in hass.data[DOMAIN]["bridge"].virtuals.values():
            virtual.close_registration()
        target.open_registration(call.data["seconds"])

    async def _start_virtual(call: ServiceCall) -> None:
        for virtual in _virtuals(call, single=False):
            virtual.set_answering(True)

    async def _stop_virtual(call: ServiceCall) -> None:
        for virtual in _virtuals(call, single=False):
            virtual.set_answering(False)

    for name, handler, schema in (
        ("register_virtual_thermostat", _register_virtual, REGISTER_SCHEMA),
        ("start_virtual_thermostat", _start_virtual, VIRTUAL_SCHEMA),
        ("stop_virtual_thermostat", _stop_virtual, VIRTUAL_SCHEMA),
    ):
        hass.services.async_register(DOMAIN, name, handler, schema=schema)

    hass.services.async_register(
        DOMAIN,
        SERVICE_MEASURE_LATENCY,
        _measure_latency,
        schema=MEASURE_LATENCY_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up integration from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    if not await mqtt.async_wait_for_mqtt_client(hass):
        raise ConfigEntryNotReady("MQTT integration is not available")

    if entry.data.get("manual_mqtt"):
        _LOGGER.warning(
            "Manual MQTT broker mode was removed in 1.1.0; the Home Assistant "
            "MQTT integration is used instead"
        )

    # Options override the values entered during initial setup
    config = {**entry.data, **entry.options}
    bridge = HAUponorBridge(
        hass,
        read_topic=config.get(CONF_READ_TOPIC) or DEFAULT_READ_TOPIC,
        write_topic=config.get(CONF_WRITE_TOPIC) or DEFAULT_WRITE_TOPIC,
        discovery_prefix=config.get(CONF_DISCOVERY_PREFIX) or HA_DISCOVERY_PREFIX,
        system_id=config.get(CONF_SYSTEM_ID),
        capture_path=hass.config.path(CAPTURE_FILE)
        if config.get(CONF_CAPTURE)
        else None,
    )
    _LOGGER.info("Setting up Uponor entry %s", entry.entry_id)
    await bridge.async_start()
    hass.data[DOMAIN]["bridge"] = bridge

    connection: GatewayConnection | None = None
    if config.get(CONF_VIRTUAL_HOST) and config.get(CONF_VIRTUAL_THERMOSTATS):
        connection = GatewayConnection(
            hass,
            bridge,
            config[CONF_VIRTUAL_HOST],
            config.get(CONF_VIRTUAL_PORT) or DEFAULT_VIRTUAL_PORT,
        )
        for item in config[CONF_VIRTUAL_THERMOSTATS]:
            virtual = VirtualThermostat(
                hass,
                bridge,
                connection,
                device_id=item[CONF_DEVICE_ID],
                temperature_entity=item[CONF_TEMPERATURE_ENTITY],
                humidity_entity=item.get(CONF_HUMIDITY_ENTITY),
            )
            connection.virtuals.append(virtual)
            bridge.virtuals[virtual.device_id] = virtual
        await connection.async_start()
        _LOGGER.warning(
            "Experimental virtual thermostats %s enabled (gateway TCP %s)",
            ", ".join(bridge.virtuals),
            config[CONF_VIRTUAL_HOST],
        )
    hass.data[DOMAIN]["connection"] = connection
    hass.data[DOMAIN]["entry_id"] = entry.entry_id

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry so new topics / system ID take effect."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    bridge: HAUponorBridge | None = hass.data.get(DOMAIN, {}).get("bridge")
    connection: GatewayConnection | None = hass.data.get(DOMAIN, {}).get("connection")
    if connection is not None:
        await connection.async_stop()
    if bridge is not None:
        await bridge.async_stop()
    hass.data.pop(DOMAIN, None)
    return True
