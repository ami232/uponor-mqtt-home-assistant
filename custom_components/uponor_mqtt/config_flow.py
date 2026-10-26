"""Config flow for Uponor MQTT Bridge integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback

from .const import (
    CONF_DISCOVERY_PREFIX,
    CONF_READ_TOPIC,
    CONF_SYSTEM_ID,
    CONF_WRITE_TOPIC,
    DEFAULT_NAME,
    DEFAULT_READ_TOPIC,
    DEFAULT_WRITE_TOPIC,
    DOMAIN,
    HA_DISCOVERY_PREFIX,
)

_LOGGER = logging.getLogger(__name__)


def _parse_system_id(value: str | None) -> int | None:
    """Parse a hex system ID such as '110B' or '0x110B'; blank means auto-detect."""
    value = (value or "").strip()
    if not value:
        return None
    system_id = int(value, 16)
    if not 0 <= system_id <= 0xFFFF:
        raise ValueError("System ID out of range")
    return system_id


def _format_system_id(value: int | str | None) -> str:
    if isinstance(value, int):
        return f"{value:04X}"
    return value or ""


def _bus_schema(defaults: dict[str, Any]) -> dict:
    """Schema fields shared by the config and options flows."""
    return {
        vol.Optional(
            CONF_READ_TOPIC, default=defaults.get(CONF_READ_TOPIC, DEFAULT_READ_TOPIC)
        ): str,
        vol.Optional(
            CONF_WRITE_TOPIC,
            default=defaults.get(CONF_WRITE_TOPIC, DEFAULT_WRITE_TOPIC),
        ): str,
        vol.Optional(
            CONF_DISCOVERY_PREFIX,
            default=defaults.get(CONF_DISCOVERY_PREFIX, HA_DISCOVERY_PREFIX),
        ): str,
        vol.Optional(
            CONF_SYSTEM_ID,
            description={
                "suggested_value": _format_system_id(defaults.get(CONF_SYSTEM_ID))
            },
        ): str,
    }


def _validate_bus_input(
    user_input: dict[str, Any], errors: dict[str, str]
) -> dict[str, Any]:
    """Normalize bus settings, recording any validation errors."""
    data = {
        CONF_READ_TOPIC: (user_input.get(CONF_READ_TOPIC) or DEFAULT_READ_TOPIC).strip(),
        CONF_WRITE_TOPIC: (
            user_input.get(CONF_WRITE_TOPIC) or DEFAULT_WRITE_TOPIC
        ).strip(),
        CONF_DISCOVERY_PREFIX: (
            user_input.get(CONF_DISCOVERY_PREFIX) or HA_DISCOVERY_PREFIX
        ).strip(),
        CONF_SYSTEM_ID: None,
    }
    try:
        data[CONF_SYSTEM_ID] = _parse_system_id(user_input.get(CONF_SYSTEM_ID))
    except ValueError:
        errors[CONF_SYSTEM_ID] = "invalid_system_id"
    return data


class UponorMQTTConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for the Uponor MQTT Bridge."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> UponorMQTTOptionsFlow:
        return UponorMQTTOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        """Ask for the MQTT topics used by the RS485 gateway and the system ID."""
        # Entries created before 1.1.0 have no unique_id, so check entries directly
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        await self.async_set_unique_id(DOMAIN)

        errors: dict[str, str] = {}
        if user_input is not None:
            data = _validate_bus_input(user_input, errors)
            data["name"] = user_input.get("name") or DEFAULT_NAME
            if not errors:
                _LOGGER.info(
                    "Config flow submission: read_topic=%s write_topic=%s "
                    "discovery_prefix=%s system_id=%s",
                    data[CONF_READ_TOPIC],
                    data[CONF_WRITE_TOPIC],
                    data[CONF_DISCOVERY_PREFIX],
                    _format_system_id(data[CONF_SYSTEM_ID]) or "auto",
                )
                return self.async_create_entry(title=data["name"], data=data)

        defaults = user_input or {}
        schema = vol.Schema(
            {
                vol.Optional("name", default=defaults.get("name", DEFAULT_NAME)): str,
                **_bus_schema(defaults),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)


class UponorMQTTOptionsFlow(config_entries.OptionsFlow):
    """Allow changing topics and system ID after setup."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            data = _validate_bus_input(user_input, errors)
            if not errors:
                return self.async_create_entry(data=data)

        current = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(_bus_schema(current)),
            errors=errors,
        )
