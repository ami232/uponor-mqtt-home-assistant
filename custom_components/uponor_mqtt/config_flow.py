"""Config flow for Uponor MQTT Bridge integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
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
    DEFAULT_NAME,
    DEFAULT_READ_TOPIC,
    DEFAULT_VIRTUAL_PORT,
    DEFAULT_WRITE_TOPIC,
    DOMAIN,
    HA_DISCOVERY_PREFIX,
)
from .virtual import parse_device_id

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


def _gateway_tcp_schema(defaults: dict[str, Any]) -> dict:
    """Gateway raw TCP socket, used by the virtual thermostats."""
    return {
        vol.Optional(
            CONF_VIRTUAL_HOST,
            description={"suggested_value": defaults.get(CONF_VIRTUAL_HOST)},
        ): str,
        vol.Optional(
            CONF_VIRTUAL_PORT,
            default=defaults.get(CONF_VIRTUAL_PORT, DEFAULT_VIRTUAL_PORT),
        ): vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
    }


def _next_device_id(used: set[str]) -> str:
    """First free ID from 42F0 up, in the same range as the T-148s seen so far."""
    dev = 0x42F0
    while f"{dev:04X}" in used:
        dev += 1
    return f"{dev:04X}"


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
    """Change the bus settings and manage virtual thermostats."""

    @property
    def _current(self) -> dict[str, Any]:
        return {**self.config_entry.data, **self.config_entry.options}

    def _virtuals(self) -> list[dict[str, Any]]:
        return list(self._current.get(CONF_VIRTUAL_THERMOSTATS) or [])

    def _save(self, **changes: Any):
        options = {**self.config_entry.options, **changes}
        return self.async_create_entry(data=options)

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        menu = ["settings", "add_virtual"]
        if self._virtuals():
            menu.append("remove_virtual")
        return self.async_show_menu(step_id="init", menu_options=menu)

    async def async_step_settings(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            data = _validate_bus_input(user_input, errors)
            data[CONF_CAPTURE] = user_input.get(CONF_CAPTURE, False)
            data[CONF_VIRTUAL_HOST] = (user_input.get(CONF_VIRTUAL_HOST) or "").strip() or None
            data[CONF_VIRTUAL_PORT] = user_input.get(CONF_VIRTUAL_PORT, DEFAULT_VIRTUAL_PORT)
            if not errors:
                return self._save(**data)

        current = self._current
        schema = {
            **_bus_schema(current),
            vol.Optional(CONF_CAPTURE, default=current.get(CONF_CAPTURE, False)): bool,
            **_gateway_tcp_schema(current),
        }
        return self.async_show_form(
            step_id="settings", data_schema=vol.Schema(schema), errors=errors
        )

    async def async_step_add_virtual(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        virtuals = self._virtuals()
        # Never reuse the address of a real thermostat or another virtual one
        bridge = self.hass.data.get(DOMAIN, {}).get("bridge")
        used = {v[CONF_DEVICE_ID] for v in virtuals}
        if bridge is not None:
            used |= set(bridge.discovered_devices)
        if not self._current.get(CONF_VIRTUAL_HOST):
            errors["base"] = "no_gateway_tcp"
        elif user_input is not None:
            try:
                device_id = parse_device_id(user_input[CONF_DEVICE_ID])
            except ValueError:
                errors[CONF_DEVICE_ID] = "invalid_device_id"
            else:
                if device_id in used:
                    errors[CONF_DEVICE_ID] = "device_id_in_use"
            if not errors:
                item = {
                    CONF_DEVICE_ID: device_id,
                    CONF_TEMPERATURE_ENTITY: user_input[CONF_TEMPERATURE_ENTITY],
                    CONF_HUMIDITY_ENTITY: user_input.get(CONF_HUMIDITY_ENTITY),
                }
                return self._save(**{CONF_VIRTUAL_THERMOSTATS: [*virtuals, item]})

        schema = vol.Schema(
            {
                vol.Required(CONF_DEVICE_ID, default=_next_device_id(used)): str,
                vol.Required(CONF_TEMPERATURE_ENTITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain="sensor", device_class="temperature"
                    )
                ),
                vol.Optional(CONF_HUMIDITY_ENTITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor", device_class="humidity")
                ),
            }
        )
        return self.async_show_form(
            step_id="add_virtual", data_schema=schema, errors=errors
        )

    async def async_step_remove_virtual(self, user_input: dict[str, Any] | None = None):
        virtuals = self._virtuals()
        if user_input is not None:
            remove = set(user_input[CONF_DEVICE_ID])
            return self._save(
                **{
                    CONF_VIRTUAL_THERMOSTATS: [
                        v for v in virtuals if v[CONF_DEVICE_ID] not in remove
                    ]
                }
            )
        options = [
            selector.SelectOptionDict(
                value=v[CONF_DEVICE_ID],
                label=f"{v[CONF_DEVICE_ID]} ({v[CONF_TEMPERATURE_ENTITY]})",
            )
            for v in virtuals
        ]
        schema = vol.Schema(
            {
                vol.Required(CONF_DEVICE_ID): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=options, multiple=True)
                )
            }
        )
        return self.async_show_form(step_id="remove_virtual", data_schema=schema)
