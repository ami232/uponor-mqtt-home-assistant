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
            # First step: ask only for name, whether to use core MQTT and discovery prefix.
            schema = vol.Schema(
                {
                    vol.Optional("name", default="Uponor MQTT Bridge"): str,
                    vol.Optional("use_core_mqtt", default=True): bool,
                    vol.Optional("discovery_prefix", default=HA_DISCOVERY_PREFIX): str,
                }
            )
            return self.async_show_form(step_id="user", data_schema=schema)

        # Build base config entry data from the first step
        self._base_data = {
            "name": user_input.get("name", "Uponor MQTT Bridge"),
            "use_core_mqtt": bool(user_input.get("use_core_mqtt", True)),
            "discovery_prefix": user_input.get("discovery_prefix", HA_DISCOVERY_PREFIX),
        }

        # Log what the user selected (avoid logging secrets)
        _LOGGER.info(
            "Config flow submission: name=%s use_core_mqtt=%s discovery_prefix=%s",
            self._base_data["name"],
            self._base_data["use_core_mqtt"],
            self._base_data["discovery_prefix"],
        )

        if self._base_data["use_core_mqtt"]:
            # Nothing else required, create the entry
            return self.async_create_entry(
                title=self._base_data["name"], data=self._base_data
            )

        # User chose manual MQTT: proceed to a second step to collect broker details
        return await self.async_step_manual()

    async def async_step_manual(self, user_input: dict[str, Any] | None = None):
        """Second step to collect manual broker settings when core MQTT is not used."""
        if user_input is None:
            schema = vol.Schema(
                {
                    vol.Required("host", default=""): str,
                    vol.Optional("port", default=1883): int,
                    vol.Optional("username", default=""): str,
                    vol.Optional("password", default=""): str,
                }
            )
            return self.async_show_form(step_id="manual", data_schema=schema)

        # Merge manual broker settings into base data and create entry
        data = dict(self._base_data)
        data["manual_mqtt"] = {
            "host": user_input.get("host", ""),
            "port": int(user_input.get("port", 1883)),
            "username": user_input.get("username") or None,
            # Do not log password
            "password": user_input.get("password") or None,
        }

        return self.async_create_entry(title=data["name"], data=data)
