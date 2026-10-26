"""Uponor MQTT bridge for Home Assistant.

Lightweight bridge that adapts Uponor protocol helpers to HA's MQTT API.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

from homeassistant.components import mqtt, persistent_notification
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util

from .const import (
    DEFAULT_READ_TOPIC,
    DEFAULT_WRITE_TOPIC,
    DOMAIN,
    HA_DISCOVERY_PREFIX,
)
from .uponor_protocol import (
    build_command_packet,
    build_time_command_packet,
    parse_state,
    parse_uponor_packet,
    split_packets,
    uponor_delta_to_c,
)

_LOGGER = logging.getLogger(__name__)


class HAUponorBridge:
    """Home Assistant wrapper for Uponor MQTT bridge functionality.

    Raw bus bytes arrive on `read_topic` and command packets are published to
    `write_topic`, both through Home Assistant's `mqtt` integration. The
    controller's system ID is taken from `system_id` or learned from the first
    valid packet seen on the bus.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        read_topic: str = DEFAULT_READ_TOPIC,
        write_topic: str = DEFAULT_WRITE_TOPIC,
        discovery_prefix: str | None = None,
        system_id: int | None = None,
    ) -> None:
        self.hass = hass
        self._time_device_id: str | None = None
        self._last_time_sync: float | None = None
        self._last_time_sync_offset = None
        self._time_task: asyncio.Task | None = None
        # device caches
        self.discovered_devices: set[str] = set()
        self.device_state_cache: dict[str, dict[str, Any]] = {}
        self._read_topic = read_topic or DEFAULT_READ_TOPIC
        self._write_topic = write_topic or DEFAULT_WRITE_TOPIC
        # per-instance discovery prefix (do not mutate imported module constant)
        self._discovery_prefix = discovery_prefix or HA_DISCOVERY_PREFIX
        self._system_id = system_id
        self._warned_sys_ids: set[int] = set()
        self._unsubscribers: list[Callable[[], None]] = []

    async def async_start(self) -> None:
        """Start subscriptions and background tasks."""
        _LOGGER.debug(
            "Starting Uponor bridge (read=%s, write=%s, system_id=%s)",
            self._read_topic,
            self._write_topic,
            f"0x{self._system_id:04X}" if self._system_id is not None else "auto",
        )
        temp_cmd = f"{self._discovery_prefix}/climate/+/temperature/set"
        try:
            # Do not force MQTT payload decoding so callbacks receive raw bytes
            self._unsubscribers.append(
                await mqtt.async_subscribe(
                    self.hass, self._read_topic, self._on_message_reader, encoding=None
                )
            )
            self._unsubscribers.append(
                await mqtt.async_subscribe(self.hass, temp_cmd, self._on_message_writer)
            )
            _LOGGER.info(
                "Subscribed to MQTT topics: '%s' and '%s'", self._read_topic, temp_cmd
            )
        except HomeAssistantError as exc:
            _LOGGER.warning(
                "Failed to subscribe to MQTT topics (MQTT not ready): %s", exc
            )
            try:
                persistent_notification.async_create(
                    self.hass,
                    (
                        "Uponor MQTT Bridge could not subscribe to MQTT topics. "
                        "Ensure the MQTT integration is configured and connected."
                    ),
                    "Uponor MQTT Bridge: MQTT subscription failed",
                )
            except Exception:
                _LOGGER.debug(
                    "Failed to create persistent notification for MQTT subscribe failure"
                )
            self._unsubscribe_all()
            # Defer setup so Home Assistant will retry later
            raise ConfigEntryNotReady("MQTT integration not ready") from exc

        # Start time sync background task
        loop = asyncio.get_running_loop()
        self._time_task = loop.create_task(self._time_sync_loop())
        _LOGGER.info("Uponor bridge started and subscribed to MQTT topics")

    def _unsubscribe_all(self) -> None:
        while self._unsubscribers:
            unsub = self._unsubscribers.pop()
            try:
                unsub()
            except Exception:
                _LOGGER.debug("Failed to unsubscribe MQTT topic")

    async def async_stop(self) -> None:
        """Stop background tasks and clean up."""
        self._unsubscribe_all()
        if self._time_task:
            self._time_task.cancel()
            try:
                await self._time_task
            except asyncio.CancelledError:
                pass
            except Exception:
                _LOGGER.debug("Time sync task ended with an error", exc_info=True)
            self._time_task = None
        # keep caches for now; nothing else to clean up

    async def _publish(
        self, topic: str, payload: Any, qos: int = 1, retain: bool = True
    ) -> None:
        """Helper to publish via Home Assistant MQTT."""
        try:
            await mqtt.async_publish(self.hass, topic, payload, qos=qos, retain=retain)
        except Exception as exc:
            _LOGGER.exception("Failed to publish %s: %s", topic, exc)

    async def _on_message_reader(self, msg) -> None:  # callback from mqtt
        """Handle incoming raw bus packets from the read topic."""
        payload: bytes = msg.payload
        try:
            plen = len(payload)
        except Exception:
            plen = 0
        _LOGGER.debug(
            "Received bus message (len=%s) topic=%s",
            plen,
            getattr(msg, "topic", self._read_topic),
        )

        packets = split_packets(payload)
        if not packets and self._system_id is not None:
            # buffer may start mid-frame; resync on the known system ID
            packets = split_packets(payload, self._system_id)
        if not packets:
            _LOGGER.debug("No valid UPONOR packets in payload")
            return

        for raw_pkt in packets:
            packet = parse_uponor_packet(raw_pkt)
            if not packet:
                _LOGGER.debug("Failed to parse uponor packet: %s", raw_pkt.hex())
                continue

            sys_id = packet["sys_id"]
            device_id = packet["device_id"]
            device_id_str = f"{device_id:04X}"

            if self._system_id is None:
                self._system_id = sys_id
                _LOGGER.info("Detected Uponor system ID 0x%04X", sys_id)
            elif sys_id != self._system_id and sys_id not in self._warned_sys_ids:
                self._warned_sys_ids.add(sys_id)
                _LOGGER.warning(
                    "Received packet from system ID 0x%04X, expected 0x%04X; "
                    "check the System ID setting if this is unexpected",
                    sys_id,
                    self._system_id,
                )

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
            base_topic = f"{self._discovery_prefix}/climate/{unique_id}"

            # Publish availability and state fields
            await self._publish(f"{base_topic}/availability", "online")
            for k, v in ha_state.items():
                try:
                    await self._publish(f"{base_topic}/{k}", v)
                except Exception:
                    _LOGGER.exception(
                        "Failed publishing state field %s for %s", k, device_id_str
                    )

            # store sys_id for writes
            await self._publish(f"{base_topic}/sys_id", sys_id)

            # Publish discovery config if not already
            if device_id_str not in self.discovered_devices:
                try:
                    cfg = self._build_discovery_config(device_id, sys_id)
                    await self._publish(
                        f"{self._discovery_prefix}/climate/{unique_id}/config", cfg
                    )
                    _LOGGER.info("Published discovery config for %s", device_id_str)
                except Exception:
                    _LOGGER.exception(
                        "Failed to publish discovery config for %s", device_id_str
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
                _LOGGER.exception(
                    "Failed to send dispatcher update for %s", device_id_str
                )

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
            # payload may be bytes or string
            raw_payload = msg.payload
            if isinstance(raw_payload, bytes):
                payload = raw_payload.decode()
            else:
                payload = str(raw_payload)
            temp_from_ha = float(payload)
            _LOGGER.debug("Received temperature set for %s -> %s", dev_id, temp_from_ha)
        except Exception as exc:
            _LOGGER.error(
                "Invalid payload on %s: %s (raw=%s)",
                msg.topic,
                exc,
                getattr(msg, "payload", None),
            )
            return

        await self.async_set_temperature(dev_id, temp_from_ha)

    async def async_set_temperature(self, device_id: str, temperature: float) -> None:
        """Set temperature for device via the Uponor bus.

        Args:
            device_id: 4-hex uppercase device id string (e.g. '00A1')
            temperature: temperature in Celsius as float
        """
        _LOGGER.debug(
            "API set temperature request for %s -> %s", device_id, temperature
        )
        device_state = self.device_state_cache.get(device_id.upper())
        if device_state:
            sys_id = device_state["sys_id"]
            is_cooling = device_state["is_cooling"]
            heating_cooling_offset = device_state["heating_cooling_offset"]
            if is_cooling:
                temp_to_send = temperature - heating_cooling_offset
            else:
                temp_to_send = temperature
        elif self._system_id is not None:
            _LOGGER.warning(
                "[%s] Device not in cache, using system ID 0x%04X",
                device_id,
                self._system_id,
            )
            sys_id = self._system_id
            temp_to_send = temperature
        else:
            _LOGGER.error(
                "[%s] Cannot set temperature: Uponor system ID not known yet. "
                "Wait for bus traffic or set the System ID in the integration options",
                device_id,
            )
            return

        try:
            buffer = build_command_packet(sys_id, device_id.lower(), temp_to_send)
            # Bus writes must not be retained, or the gateway replays them on reconnect
            await self._publish(self._write_topic, buffer, retain=False)
            _LOGGER.info(
                "[%s] Set temperature: HA=%.1f°C, bus=%.1f°C (sys_id=0x%04X)",
                device_id,
                temperature,
                temp_to_send,
                sys_id,
            )
            base_topic = f"{self._discovery_prefix}/climate/uponor_{device_id}"
            await self._publish(f"{base_topic}/temperature", str(temperature))
        except Exception as exc:
            _LOGGER.exception("Failed to build/send command for %s: %s", device_id, exc)

    def _build_discovery_config(self, device_id: int, sys_id: int) -> str:
        device_id_str = f"{device_id:04X}"
        unique_id = f"uponor_{device_id_str}"
        base_topic = f"{self._discovery_prefix}/climate/{unique_id}"
        config = {
            "name": f"Uponor {device_id_str}",
            "unique_id": unique_id,
            "default_entity_id": f"climate.uponor_{device_id_str.lower()}",
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
            # Use HA's configured time zone, not the container's
            now = dt_util.now()
            buf = build_time_command_packet(sys_id, dev_id.lower(), now)
            await self._publish(self._write_topic, buf, retain=False)
            self._last_time_sync = asyncio.get_event_loop().time()
            self._last_time_sync_offset = now.utcoffset()
            _LOGGER.info("[%s] Sent time synchronization (%s)", dev_id, now.isoformat())
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
            elif dt_util.now().utcoffset() != self._last_time_sync_offset:
                # DST transition: resync right away instead of waiting up to 24h
                await self._send_time_if_ready()
            await asyncio.sleep(30)
