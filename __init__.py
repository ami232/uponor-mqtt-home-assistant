"""Uponor MQTT Bridge integration for Home Assistant.

This integration subscribes to `uponor_read` and listens for Home Assistant
temperature set commands, then publishes `uponor_write` byte payloads using
the Home Assistant MQTT helper.

Place this directory under `custom_components/uponor_mqtt` and restart Home
Assistant. The integration requires the `mqtt` integration to be set up.
"""

from __future__ import annotations

import logging
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .bridge import HAUponorBridge

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the Uponor MQTT Bridge integration.

    This integration does not need any YAML config; it hooks into the existing
    broker configured by the `mqtt` integration. On setup it starts the bridge
    which subscribes to `uponor_read` and the HA temperature set topic.
    """
    _LOGGER.info("Setting up Uponor MQTT Bridge integration")

    # keep minimal setup; real start occurs in config entries
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry) -> bool:
    """Set up integration from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    # If the user provided manual MQTT settings in the config entry, pass them
    # to the bridge so it can create its own paho MQTT client. Otherwise the
    # bridge will use Home Assistant's `mqtt` helper.
    manual = entry.data.get("manual_mqtt")
    discovery_prefix = entry.data.get("discovery_prefix")
    bridge = HAUponorBridge(hass, manual_mqtt=manual, discovery_prefix=discovery_prefix)
    await bridge.async_start()
    hass.data[DOMAIN]["bridge"] = bridge
    hass.data[DOMAIN]["entry_id"] = entry.entry_id
    return True


async def async_unload_entry(hass: HomeAssistant, entry) -> bool:
    """Unload a config entry."""
    bridge: HAUponorBridge | None = hass.data.get(DOMAIN, {}).get("bridge")
    if bridge is not None:
        # stop background tasks if supported
        stop = getattr(bridge, "async_stop", None)
        if stop is not None:
            await stop()
        # if bridge created a manual paho client, stop it
        p = getattr(bridge, "_paho_client", None)
        if p is not None:
            try:
                p.loop_stop()
                p.disconnect()
            except Exception:
                _LOGGER.debug("Failed to stop manual MQTT client")
    hass.data.pop(DOMAIN, None)
    return True
