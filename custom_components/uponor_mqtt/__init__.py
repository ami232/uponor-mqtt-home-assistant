"""Uponor MQTT Bridge integration for Home Assistant.

This integration reads raw Uponor Smatrix bus packets from an MQTT topic
(published by an RS485-to-MQTT gateway), exposes the thermostats as climate
entities via MQTT discovery, and publishes temperature commands back to the
bus through a second MQTT topic. It requires the `mqtt` integration to be set
up.
"""

from __future__ import annotations

import logging

from homeassistant.components import mqtt
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .bridge import HAUponorBridge
from .const import (
    CONF_DISCOVERY_PREFIX,
    CONF_READ_TOPIC,
    CONF_SYSTEM_ID,
    CONF_WRITE_TOPIC,
    DEFAULT_READ_TOPIC,
    DEFAULT_WRITE_TOPIC,
    DOMAIN,
    HA_DISCOVERY_PREFIX,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the Uponor MQTT Bridge integration.

    This integration does not need any YAML config; the bridge is started from
    the config entry.
    """
    # keep minimal setup; real start occurs in config entries
    hass.data.setdefault(DOMAIN, {})
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
    )
    _LOGGER.info("Setting up Uponor entry %s", entry.entry_id)
    await bridge.async_start()
    hass.data[DOMAIN]["bridge"] = bridge
    hass.data[DOMAIN]["entry_id"] = entry.entry_id

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry so new topics / system ID take effect."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    bridge: HAUponorBridge | None = hass.data.get(DOMAIN, {}).get("bridge")
    if bridge is not None:
        await bridge.async_stop()
    hass.data.pop(DOMAIN, None)
    return True
