"""Bridge that adapts the existing Uponor protocol utilities to Home Assistant's MQTT API.

This file keeps most of the parsing/packet building logic by importing
`uponor_protocol` from the repository and uses Home Assistant's
`mqtt.async_subscribe` / `mqtt.async_publish` for MQTT operations.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.components import mqtt
import types
import asyncio as _asyncio

try:
    import paho.mqtt.client as paho
except Exception:  # pragma: no cover - optional dependency
    paho = None

from .uponor_protocol import (
    parse_uponor_packet,
    parse_state,
    split_packets,
    uponor_delta_to_c,
    build_command_packet,
    build_time_command_packet,
)

from .const import HA_DISCOVERY_PREFIX, DOMAIN

_LOGGER = logging.getLogger(__name__)


class HAUponorBridge:
    """Home Assistant wrapper for Uponor MQTT bridge functionality.

    If `manual_mqtt` is supplied (a dict with host/port/username/password),
    the bridge will create its own paho MQTT client and use it directly.
    Otherwise it uses Home Assistant's `mqtt` helper functions.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        manual_mqtt: dict | None = None,
        discovery_prefix: str | None = None,
    ) -> None:
        self.hass = hass
        self._time_device_id: str | None = None
        self._last_time_sync: float | None = None
        self._time_task: asyncio.Task | None = None
        # device caches
        self.discovered_devices: set[str] = set()
        self.device_state_cache: dict[str, dict[str, Any]] = {}
        # manual mqtt support
        self._manual_mqtt = manual_mqtt is not None
        self._manual_config = manual_mqtt or {}
        self._paho_client = None
        if discovery_prefix:
            # allow overriding discovery prefix from config entry
            global HA_DISCOVERY_PREFIX
            HA_DISCOVERY_PREFIX = discovery_prefix

    async def async_start(self) -> None:
        """Start subscriptions and background tasks."""
        if self._manual_mqtt and paho is not None:
            # Create and start a paho client in background thread
            client_id = "uponor_translator"
            self._paho_client = paho.Client(client_id=client_id)
            if self._manual_config.get("username"):
                self._paho_client.username_pw_set(
                    self._manual_config.get("username"),
                    self._manual_config.get("password"),
                )

            # wire callbacks
            def _on_connect(client, userdata, flags, rc):
                _LOGGER.info("Manual MQTT client connected (rc=%s)", rc)
                # subscribe to topics on connect
                client.subscribe(
                    [
                        ("uponor_read", 0),
                        (f"{HA_DISCOVERY_PREFIX}/climate/+/temperature/set", 0),
                    ]
                )

            def _on_message(client, userdata, msg):
                # schedule coroutine handling in HA loop
                ns = types.SimpleNamespace(payload=msg.payload, topic=msg.topic)
                loop = self.hass.loop
                loop.call_soon_threadsafe(
                    _asyncio.create_task, self._dispatch_msg_from_thread(ns)
                )

            self._paho_client.on_connect = _on_connect
            self._paho_client.on_message = _on_message

            host = self._manual_config.get("host", "127.0.0.1")
            port = int(self._manual_config.get("port", 1883))
            try:
                self._paho_client.connect_async(host, port)
                self._paho_client.loop_start()
            except Exception as exc:
                _LOGGER.exception("Failed to connect manual MQTT client: %s", exc)
        else:
            # Subscribe to incoming Uponor packets using HA MQTT helper
            await mqtt.async_subscribe(
                self.hass, "uponor_read", self._on_message_reader
            )
            temp_cmd = f"{HA_DISCOVERY_PREFIX}/climate/+/temperature/set"
            await mqtt.async_subscribe(self.hass, temp_cmd, self._on_message_writer)

        # Start time sync background task
        loop = asyncio.get_running_loop()
        self._time_task = loop.create_task(self._time_sync_loop())
        _LOGGER.info("Uponor bridge started and subscribed to MQTT topics")

    async def _dispatch_msg_from_thread(self, ns: types.SimpleNamespace) -> None:
        """Helper scheduled from paho thread to dispatch incoming messages."""
        # determine handler by topic
        if ns.topic == "uponor_read":
            await self._on_message_reader(ns)
        elif ns.topic.endswith("/temperature/set") and ns.topic.startswith(
            f"{HA_DISCOVERY_PREFIX}/climate/"
        ):
            await self._on_message_writer(ns)
        else:
            _LOGGER.debug("Ignoring manual mqtt message on: %s", ns.topic)

    async def async_stop(self) -> None:
        """Stop background tasks and clean up."""
        if self._time_task:
            try:
                self._time_task.cancel()
                await asyncio.sleep(0)
            except Exception:
                _LOGGER.debug("Failed to cancel time task")
        # keep caches for now; nothing else to clean up

    async def _publish(
        self, topic: str, payload: Any, qos: int = 1, retain: bool = True
    ) -> None:
        """Helper to publish via Home Assistant MQTT."""
        try:
            if self._manual_mqtt and self._paho_client is not None:
                # paho publish is synchronous; run in executor
                await self.hass.async_add_executor_job(
                    self._paho_client.publish, topic, payload, qos, retain
                )
            else:
                await mqtt.async_publish(
                    self.hass, topic, payload, qos=qos, retain=retain
                )
        except Exception as exc:
            _LOGGER.exception("Failed to publish %s: %s", topic, exc)

    async def _on_message_reader(self, msg) -> None:  # callback from mqtt
        """Handle incoming `uponor_read` MQTT messages."""
        payload: bytes = msg.payload
        _LOGGER.debug("Received uponor_read %d bytes", len(payload))

        packets = split_packets(payload)
        if not packets:
            _LOGGER.debug("No valid UPONOR packets in payload")
            return

        for raw_pkt in packets:
            packet = parse_uponor_packet(raw_pkt)
            if not packet:
                continue

            sys_id = packet["sys_id"]
            device_id = packet["device_id"]
            device_id_str = f"{device_id:04X}"

            # Ensure cache exists
            if device_id_str not in self.device_state_cache:
                self.device_state_cache[device_id_str] = {
                    "sys_id": sys_id,
                    "is_cooling": False,
                    "heating_cooling_offset": 2.0,
                }
            else:
                self.device_state_cache[device_id_str]["sys_id"] = sys_id

            # Update cooling flag and offset if present
            mode2 = packet.get("mode2")
            if mode2 is not None:
                is_cooling = bool(mode2 & 0x1000)
                self.device_state_cache[device_id_str]["is_cooling"] = is_cooling

            heating_cooling_offset_raw = packet.get("heating_cooling_offset")
            if heating_cooling_offset_raw is not None:
                heating_cooling_offset = uponor_delta_to_c(heating_cooling_offset_raw)
                self.device_state_cache[device_id_str][
                    "heating_cooling_offset"
                ] = heating_cooling_offset

            ha_state = parse_state(packet)

            # Adjust temperature using cached offset if needed
            if "temperature" in ha_state and "mode" not in ha_state:
                cached = self.device_state_cache.get(device_id_str)
                if cached and cached.get("is_cooling"):
                    offset = float(cached.get("heating_cooling_offset", 2.0))
                    ha_state["temperature"] = round(ha_state["temperature"] + offset, 1)

            unique_id = f"uponor_{device_id_str}"
            base_topic = f"{HA_DISCOVERY_PREFIX}/climate/{unique_id}"

            # Publish availability and state fields
            await self._publish(f"{base_topic}/availability", "online")
            for k, v in ha_state.items():
                await self._publish(f"{base_topic}/{k}", v)

            # store sys_id for writes
            await self._publish(f"{base_topic}/sys_id", sys_id)

            # Publish discovery config if not already
            if device_id_str not in self.discovered_devices:
                await self._publish(
                    f"{HA_DISCOVERY_PREFIX}/climate/{unique_id}/config",
                    self._build_discovery_config(device_id, sys_id),
                )
                self.discovered_devices.add(device_id_str)
                # notify listeners that a new device appeared
                try:
                    async_dispatcher_send(
                        self.hass, f"{DOMAIN}_device_new", device_id_str
                    )
                except Exception:
                    _LOGGER.debug(
                        "Failed to send new-device dispatcher for %s", device_id_str
                    )

            _LOGGER.info("Published state for %s: %s", device_id_str, ha_state)

            # Notify any local entities about the update
            try:
                async_dispatcher_send(
                    self.hass, f"{DOMAIN}_update_{device_id_str}", ha_state
                )
            except Exception:
                _LOGGER.debug("Failed to send dispatcher update for %s", device_id_str)

            # Detect time master
            if (
                self._time_device_id is None
                and "datetime1" in packet
                and "current_temperature" in packet
            ):
                self._time_device_id = device_id_str
                _LOGGER.info(
                    "Detected time master thermostat at device 0x%s (sys_id=0x%04X)",
                    device_id_str,
                    sys_id,
                )
                await self._send_time_if_ready()

    async def _on_message_writer(self, msg) -> None:
        """Handle incoming temperature set messages from Home Assistant topics."""
        try:
            parts = msg.topic.split("/")
            if len(parts) < 5 or parts[-2] != "temperature" or parts[-1] != "set":
                raise ValueError("Unexpected topic structure")
            unique_id = parts[-3]
            if not unique_id.startswith("uponor_"):
                raise ValueError(f"Unexpected unique_id '{unique_id}'")
            dev_id = unique_id[len("uponor_") :].upper()
        except ValueError as exc:
            _LOGGER.error("Unexpected topic format (%s): %s", msg.topic, exc)
            return

        try:
            payload = msg.payload.decode()
            temp_from_ha = float(payload)
        except Exception as exc:
            _LOGGER.error("Invalid payload on %s: %s", msg.topic, exc)
            return

        device_state = self.device_state_cache.get(dev_id.upper())
        if device_state:
            sys_id = device_state["sys_id"]
            is_cooling = device_state["is_cooling"]
            heating_cooling_offset = device_state["heating_cooling_offset"]
            if is_cooling:
                temp_to_send = temp_from_ha - heating_cooling_offset
            else:
                temp_to_send = temp_from_ha
        else:
            _LOGGER.warning("[%s] Device not in cache, using defaults", dev_id)
            sys_id = 0x1108
            temp_to_send = temp_from_ha

        # Build command packet and publish to uponor_write (raw bytes)
        try:
            buffer = build_command_packet(sys_id, dev_id.lower(), temp_to_send)
            await self._publish("uponor_write", buffer)
            _LOGGER.info(
                "[%s] Set temperature: HA=%.1f°C, bus=%.1f°C (sys_id=0x%04X)",
                dev_id,
                temp_from_ha,
                temp_to_send,
                sys_id,
            )
            # Echo back
            base_topic = f"{HA_DISCOVERY_PREFIX}/climate/uponor_{dev_id}"
            await self._publish(f"{base_topic}/temperature", payload)
        except Exception as exc:
            _LOGGER.exception("Failed to build/send command for %s: %s", dev_id, exc)

    async def async_set_temperature(self, device_id: str, temperature: float) -> None:
        """Set temperature for device via the Uponor bus.

        Args:
            device_id: 4-hex uppercase device id string (e.g. '00A1')
            temperature: temperature in Celsius as float
        """
        device_state = self.device_state_cache.get(device_id.upper())
        if device_state:
            sys_id = device_state["sys_id"]
            is_cooling = device_state["is_cooling"]
            heating_cooling_offset = device_state["heating_cooling_offset"]
            if is_cooling:
                temp_to_send = temperature - heating_cooling_offset
            else:
                temp_to_send = temperature
        else:
            _LOGGER.warning("[%s] Device not in cache, using defaults", device_id)
            sys_id = 0x1108
            temp_to_send = temperature

        try:
            buffer = build_command_packet(sys_id, device_id.lower(), temp_to_send)
            await self._publish("uponor_write", buffer)
            _LOGGER.info(
                "[%s] Set temperature via API: HA=%.1f°C, bus=%.1f°C (sys_id=0x%04X)",
                device_id,
                temperature,
                temp_to_send,
                sys_id,
            )
            base_topic = f"{HA_DISCOVERY_PREFIX}/climate/uponor_{device_id}"
            await self._publish(f"{base_topic}/temperature", str(temperature))
        except Exception as exc:
            _LOGGER.exception("Failed to build/send command for %s: %s", device_id, exc)

    def _build_discovery_config(self, device_id: int, sys_id: int) -> str:
        device_id_str = f"{device_id:04X}"
        unique_id = f"uponor_{device_id_str}"
        base_topic = f"{HA_DISCOVERY_PREFIX}/climate/{unique_id}"
        config = {
            "name": f"Uponor {device_id_str}",
            "unique_id": unique_id,
            "object_id": f"uponor_{device_id_str.lower()}",
            "device": {
                "identifiers": [unique_id],
                "name": f"Uponor Thermostat {device_id_str}",
                "manufacturer": "Uponor",
                "model": "Floor Heating Thermostat",
            },
            "temperature_unit": "C",
            "temp_step": 0.5,
            "precision": 0.1,
            "min_temp": 5,
            "max_temp": 35,
            "current_temperature_topic": f"{base_topic}/current_temperature",
            "temperature_state_topic": f"{base_topic}/temperature",
            "action_topic": f"{base_topic}/action",
            "mode_state_topic": f"{base_topic}/mode",
            "temperature_command_topic": f"{base_topic}/temperature/set",
            "modes": ["heat", "cool"],
            "current_humidity_topic": f"{base_topic}/current_humidity",
            "availability_topic": f"{base_topic}/availability",
            "payload_available": "online",
            "payload_not_available": "offline",
        }
        # Return JSON string for MQTT discovery
        import json

        return json.dumps(config)

    async def _send_time_if_ready(self) -> None:
        dev_id = self._time_device_id
        if not dev_id:
            return
        st = self.device_state_cache.get(dev_id)
        if not st:
            return
        sys_id = st.get("sys_id")
        if sys_id is None:
            return
        try:
            buf = build_time_command_packet(sys_id, dev_id.lower())
            await self._publish("uponor_write", buf)
            self._last_time_sync = asyncio.get_event_loop().time()
            _LOGGER.info("[%s] Sent time synchronization", dev_id)
        except Exception as exc:
            _LOGGER.error("Failed to build/send time packet to %s: %s", dev_id, exc)

    async def _time_sync_loop(self) -> None:
        # Delay a bit to allow discovery
        await asyncio.sleep(10)
        while True:
            if self._last_time_sync is None:
                await self._send_time_if_ready()
            elif (asyncio.get_event_loop().time() - self._last_time_sync) >= 24 * 3600:
                await self._send_time_if_ready()
            await asyncio.sleep(30)
