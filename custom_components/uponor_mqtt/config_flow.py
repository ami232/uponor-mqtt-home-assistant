"""Config flow for Uponor MQTT Bridge integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries

from .const import DOMAIN, HA_DISCOVERY_PREFIX

_LOGGER = logging.getLogger(__name__)


class UponorMQTTConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for the Uponor MQTT Bridge."""

    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_POLL

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        """Initial step which asks the user for manual broker settings or to use core MQTT."""
        if user_input is None:
            schema = vol.Schema(
                {
                    vol.Optional("name", default="Uponor MQTT Bridge"): str,
                    vol.Optional("use_core_mqtt", default=True): bool,
                    vol.Optional("host", default=""): str,
                    vol.Optional("port", default=1883): int,
                    vol.Optional("username", default=""): str,
                    vol.Optional("password", default=""): str,
                    vol.Optional("discovery_prefix", default=HA_DISCOVERY_PREFIX): str,
                }
            )
            return self.async_show_form(step_id="user", data_schema=schema)

        # Build config entry data
        data: dict[str, Any] = {
            "name": user_input.get("name", "Uponor MQTT Bridge"),
            "use_core_mqtt": bool(user_input.get("use_core_mqtt", True)),
            "discovery_prefix": user_input.get("discovery_prefix", HA_DISCOVERY_PREFIX),
        }

        # Log what the user selected (avoid logging secrets)
        _LOGGER.info(
            "Config flow submission: name=%s use_core_mqtt=%s discovery_prefix=%s",
            data["name"],
            data["use_core_mqtt"],
            data["discovery_prefix"],
        )

        if not data["use_core_mqtt"]:
            # save manual broker settings
            data["manual_mqtt"] = {
                "host": user_input.get("host", "192.168.1.31"),
                "port": int(user_input.get("port", 1883)),
                "username": user_input.get("username") or None,
                # Do not log password
                "password": user_input.get("password") or None,
            }

        return self.async_create_entry(title=data["name"], data=data)
