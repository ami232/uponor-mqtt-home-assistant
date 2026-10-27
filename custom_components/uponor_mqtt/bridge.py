"""Uponor MQTT bridge for Home Assistant.

Lightweight bridge that adapts Uponor protocol helpers to HA's MQTT API.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.components import mqtt, persistent_notification
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import (
    DEFAULT_READ_TOPIC,
    DEFAULT_WRITE_TOPIC,
    DOMAIN,
    HA_DISCOVERY_PREFIX,
)
from .uponor_protocol import (
    REGISTRATION_BROADCAST,
    UponorThermostat,
    build_poll_packet,
    build_setpoint_packet,
    build_time_command_packet,
    parse_uponor_packet,
    split_packets,
    temp_to_uponor,
)

if TYPE_CHECKING:
    from .virtual import VirtualThermostat

_LOGGER = logging.getLogger(__name__)

# State field -> (entity name, device class, unit) for the per-thermostat sensors
SENSORS: dict[str, tuple[str, str, str]] = {
    "current_temperature": ("Temperature", "temperature", "°C"),
    "external_temperature": ("External temperature", "temperature", "°C"),
    "current_humidity": ("Humidity", "humidity", "%"),
    "temperature": ("Target temperature", "temperature", "°C"),
}

CAPTURE_FLUSH_INTERVAL = timedelta(seconds=5)

# Bus silence after a poll cycle before a latency probe may transmit; the
# controller polls every 10 s and a cycle takes about 1 s
PROBE_QUIET = 3.0
PROBE_TIMEOUT = 2.0


def _append_lines(path: str, lines: list[str]) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.writelines(lines)


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
        capture_path: str | None = None,
    ) -> None:
        self.hass = hass
        self._time_device_id: str | None = None
        self._last_time_sync: float | None = None
        self._last_time_sync_offset = None
        self._time_task: asyncio.Task | None = None
        # device caches
        self.discovered_devices: set[str] = set()
        self.device_state_cache: dict[str, dict[str, Any]] = {}
        self._thermostats: dict[str, UponorThermostat] = {}
        self._published: dict[str, dict[str, Any]] = {}
        self._discovery_configs: dict[str, str] = {}
        self._read_topic = read_topic or DEFAULT_READ_TOPIC
        self._write_topic = write_topic or DEFAULT_WRITE_TOPIC
        # per-instance discovery prefix (do not mutate imported module constant)
        self._discovery_prefix = discovery_prefix or HA_DISCOVERY_PREFIX
        self._system_id = system_id
        self._warned_sys_ids: set[int] = set()
        self._unsubscribers: list[Callable[[], None]] = []
        # Raw bus capture for protocol analysis (tools/analyze_capture.py)
        self._capture_path = capture_path
        self._capture_lines: list[str] = []
        self._capture_unsub: Callable[[], None] | None = None
        # Bus timing, for latency probes
        self._last_rx = 0.0
        self._last_registration_broadcast = 0.0
        self._probe: tuple[str, asyncio.Future[float]] | None = None
        self.virtuals: dict[str, VirtualThermostat] = {}

    @property
    def system_id(self) -> int | None:
        return self._system_id

    @property
    def capturing(self) -> bool:
        return self._capture_path is not None

    def capture(self, direction: str, raw: bytes | None = None, **extra: Any) -> None:
        """Queue one capture line: `rx`/`tx` bus bytes or a `note`."""
        if self._capture_path is None:
            return
        entry: dict[str, Any] = {
            "ts": round(time.time(), 6),
            "mono": round(time.monotonic(), 6),
            "dir": direction,
        }
        if raw is not None:
            entry["hex"] = raw.hex()
        entry.update(extra)
        self._capture_lines.append(json.dumps(entry) + "\n")

    async def _async_flush_capture(self, _now: Any = None) -> None:
        if self._capture_path is None or not self._capture_lines:
            return
        lines, self._capture_lines = self._capture_lines, []
        try:
            await self.hass.async_add_executor_job(
                _append_lines, self._capture_path, lines
            )
        except OSError as exc:
            _LOGGER.error("Failed to write bus capture %s: %s", self._capture_path, exc)

    async def async_start(self) -> None:
        """Start subscriptions and background tasks."""
        _LOGGER.debug(
            "Starting Uponor bridge (read=%s, write=%s, system_id=%s)",
            self._read_topic,
            self._write_topic,
            f"0x{self._system_id:04X}" if self._system_id is not None else "auto",
        )
        temp_cmd = f"{self._discovery_prefix}/climate/+/temperature/set"
        mode_cmd = f"{self._discovery_prefix}/climate/+/mode/set"
        preset_cmd = f"{self._discovery_prefix}/climate/+/preset_mode/set"
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
            for topic in (mode_cmd, preset_cmd):
                self._unsubscribers.append(
                    await mqtt.async_subscribe(
                        self.hass, topic, self._on_read_only_command
                    )
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

        if self._capture_path is not None:
            self._capture_unsub = async_track_time_interval(
                self.hass, self._async_flush_capture, CAPTURE_FLUSH_INTERVAL
            )
            self.capture("note", text="capture started")
            _LOGGER.warning("Capturing raw bus traffic to %s", self._capture_path)

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
        if self._capture_unsub is not None:
            self._capture_unsub()
            self._capture_unsub = None
            self.capture("note", text="capture stopped")
            await self._async_flush_capture()
        # keep caches for now; nothing else to clean up

    async def _publish(
        self, topic: str, payload: Any, qos: int = 1, retain: bool = True
    ) -> None:
        """Helper to publish via Home Assistant MQTT."""
        try:
            await mqtt.async_publish(self.hass, topic, payload, qos=qos, retain=retain)
        except Exception as exc:
            _LOGGER.exception("Failed to publish %s: %s", topic, exc)

    async def _write_bus(self, buffer: bytes, qos: int = 1) -> None:
        """Send raw bytes to the bus through the gateway."""
        self.capture("tx", buffer)
        # Bus writes must not be retained, or the gateway replays them on reconnect
        await self._publish(self._write_topic, buffer, qos=qos, retain=False)

    async def _async_wait_bus_idle(self) -> None:
        """Wait for the next poll cycle to finish and the bus to go quiet."""
        start = time.monotonic()
        while self._last_rx <= start or time.monotonic() - self._last_rx < PROBE_QUIET:
            if self._last_registration_broadcast > start - 10:
                raise HomeAssistantError("Controller is in registration mode")
            if time.monotonic() - start > 60:
                raise HomeAssistantError("No bus traffic for 60 s")
            await asyncio.sleep(0.05)

    async def async_measure_latency(self, device_id: str, count: int) -> dict[str, Any]:
        """Time poll -> reply round trips to a thermostat through the gateway.

        Sends the controller's own poll to `device_id` while the bus is idle
        between poll cycles and waits for the thermostat's reply on the read
        topic. This is the path a reply from Home Assistant would take, so it
        shows whether HA can answer the controller in time.
        """
        if self._system_id is None:
            raise HomeAssistantError("Uponor system ID not known yet")
        if self._probe is not None:
            raise HomeAssistantError("A latency measurement is already running")
        device_id = device_id.upper()
        poll = build_poll_packet(self._system_id, device_id)
        loop = asyncio.get_running_loop()
        round_trips: list[float | None] = []
        for _ in range(count):
            await self._async_wait_bus_idle()
            future: asyncio.Future[float] = loop.create_future()
            self._probe = (device_id, future)
            sent = time.monotonic()
            try:
                await self._write_bus(poll, qos=0)
                received = await asyncio.wait_for(future, PROBE_TIMEOUT)
                round_trips.append(round((received - sent) * 1000, 1))
            except TimeoutError:
                round_trips.append(None)
            finally:
                self._probe = None

        replies = sorted(r for r in round_trips if r is not None)
        result: dict[str, Any] = {
            "device_id": device_id,
            "polls": count,
            "replies": len(replies),
            "round_trips_ms": round_trips,
        }
        if replies:
            result.update(
                min_ms=replies[0],
                median_ms=replies[len(replies) // 2],
                p90_ms=replies[max(0, int(len(replies) * 0.9) - 1)],
                max_ms=replies[-1],
            )
        _LOGGER.info("Latency measurement: %s", result)
        return result

    def _base_topic(self, device_id_str: str) -> str:
        return f"{self._discovery_prefix}/climate/uponor_{device_id_str}"

    async def _on_message_reader(self, msg) -> None:  # callback from mqtt
        """Handle incoming raw bus packets from the read topic."""
        payload: bytes = msg.payload
        received = time.monotonic()
        self._last_rx = received
        if isinstance(payload, bytes):
            # Before parsing, so frames the parser rejects are kept too
            self.capture("rx", payload)
            if REGISTRATION_BROADCAST in payload:
                self._last_registration_broadcast = received
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
            if (
                self._probe is not None
                and f"{packet['device_id']:04X}" == self._probe[0]
                and "current_temperature" in packet
                and not self._probe[1].done()
            ):
                self._probe[1].set_result(received)
            await self._handle_packet(packet)

    async def _handle_packet(self, packet: dict[str, Any]) -> None:
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

        if packet.get("special_ff"):
            # Controller polling a device; carries no data
            return

        if (virtual := self.virtuals.get(device_id_str)) and not packet.get("virtual"):
            # The controller's frame to our virtual thermostat. It echoes the
            # setpoint it last heard from us, which may lag a change made here.
            virtual.on_controller_packet(packet)
            packet = {k: v for k, v in packet.items() if k != "temperature"}

        thermostat = self._thermostats.setdefault(device_id_str, UponorThermostat())
        thermostat.update(packet)
        ha_state = thermostat.state()
        self.device_state_cache[device_id_str] = {"sys_id": sys_id, **ha_state}

        # Only thermostats report a room temperature; other bus devices
        # (e.g. the controller itself) do not get entities
        if device_id_str not in self.discovered_devices:
            if "current_temperature" not in packet:
                return
            _LOGGER.info("Found thermostat at address 0x%04X%04X", sys_id, device_id)
            self.discovered_devices.add(device_id_str)
            # notify listeners that a new device appeared
            try:
                async_dispatcher_send(self.hass, f"{DOMAIN}_device_new", device_id_str)
            except Exception:
                _LOGGER.debug(
                    "Failed to send new-device dispatcher for %s", device_id_str
                )

        await self._publish_discovery(device_id_str, sys_id, thermostat)

        base_topic = self._base_topic(device_id_str)
        published = self._published.setdefault(device_id_str, {})
        if not published:
            await self._publish(f"{base_topic}/availability", "online")
        changed = {k: v for k, v in ha_state.items() if published.get(k) != v}
        for k, v in changed.items():
            await self._publish(f"{base_topic}/{k}", v)
            published[k] = v
        if changed:
            _LOGGER.debug("Published state for %s: %s", device_id_str, changed)

        # Notify any local entities about the update
        try:
            async_dispatcher_send(
                self.hass,
                f"{DOMAIN}_update_{device_id_str}",
                self.device_state_cache[device_id_str],
            )
        except Exception:
            _LOGGER.exception("Failed to send dispatcher update for %s", device_id_str)

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

    async def async_virtual_state(self, packet: dict[str, Any]) -> None:
        """Publish the virtual thermostat's own readings like a real thermostat's."""
        await self._handle_packet({**packet, "virtual": True})

    async def _publish_discovery(
        self, device_id_str: str, sys_id: int, thermostat: UponorThermostat
    ) -> None:
        """Publish discovery configs that are new or changed (e.g. min/max)."""
        configs = {
            f"{self._discovery_prefix}/climate/uponor_{device_id_str}/config": (
                self._build_discovery_config(int(device_id_str, 16), sys_id, thermostat)
            )
        }
        state = thermostat.state()
        for key in SENSORS:
            if key in state:
                configs[
                    f"{self._discovery_prefix}/sensor/uponor_{device_id_str}_{key}/config"
                ] = self._build_sensor_config(device_id_str, sys_id, key)

        for topic, cfg in configs.items():
            if self._discovery_configs.get(topic) == cfg:
                continue
            await self._publish(topic, cfg)
            self._discovery_configs[topic] = cfg
            _LOGGER.info("Published discovery config %s", topic)

    async def _on_message_writer(self, msg) -> None:
        """Handle incoming temperature set messages from Home Assistant topics."""
        dev_id = self._device_from_command_topic(msg.topic, "temperature")
        if dev_id is None:
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

    def _device_from_command_topic(self, topic: str, field: str) -> str | None:
        try:
            parts = topic.split("/")
            if len(parts) < 5 or parts[-2] != field or parts[-1] != "set":
                raise ValueError("Unexpected topic structure")
            unique_id = parts[-3]
            if not unique_id.startswith("uponor_"):
                raise ValueError(f"Unexpected unique_id '{unique_id}'")
            return unique_id[len("uponor_") :].upper()
        except ValueError as exc:
            _LOGGER.error("Unexpected topic format (%s): %s", topic, exc)
            return None

    async def _on_read_only_command(self, msg) -> None:
        """Reject mode/preset changes and restore the thermostat's real state.

        Heating/cooling is switched at the controller and ECO by its schedule,
        so neither can be changed over the bus.
        """
        field = msg.topic.split("/")[-2]
        dev_id = self._device_from_command_topic(msg.topic, field)
        if dev_id is None:
            return
        _LOGGER.warning(
            "[%s] %s is read-only; change it on the thermostat or controller",
            dev_id,
            field,
        )
        value = self._published.get(dev_id, {}).get(field)
        if value is not None:
            await self._publish(f"{self._base_topic(dev_id)}/{field}", value)

    async def async_set_temperature(self, device_id: str, temperature: float) -> None:
        """Set temperature for device via the Uponor bus.

        Args:
            device_id: 4-hex uppercase device id string (e.g. '00A1')
            temperature: temperature in Celsius as float, as shown on the
                thermostat (the bus value is adjusted for cooling/ECO offsets)
        """
        device_id = device_id.upper()
        _LOGGER.debug(
            "API set temperature request for %s -> %s", device_id, temperature
        )
        if virtual := self.virtuals.get(device_id):
            # Our own thermostat: it announces the new setpoint in its replies
            virtual.set_target(temperature)
            await self._publish(
                f"{self._base_topic(device_id)}/temperature", str(temperature)
            )
            self._published.setdefault(device_id, {})["temperature"] = temperature
            _LOGGER.info("[%s] Virtual thermostat setpoint %.1f°C", device_id, temperature)
            return
        device_state = self.device_state_cache.get(device_id)
        thermostat = self._thermostats.get(device_id)
        if device_state and thermostat:
            sys_id = device_state["sys_id"]
            raw = thermostat.setpoint_raw(temperature)
        elif self._system_id is not None:
            _LOGGER.warning(
                "[%s] Device not in cache, using system ID 0x%04X",
                device_id,
                self._system_id,
            )
            sys_id = self._system_id
            raw = temp_to_uponor(temperature)
        else:
            _LOGGER.error(
                "[%s] Cannot set temperature: Uponor system ID not known yet. "
                "Wait for bus traffic or set the System ID in the integration options",
                device_id,
            )
            return

        try:
            buffer = build_setpoint_packet(sys_id, device_id.lower(), raw)
            await self._write_bus(buffer)
            _LOGGER.info(
                "[%s] Set temperature: HA=%.1f°C, bus raw=%d (sys_id=0x%04X)",
                device_id,
                temperature,
                raw,
                sys_id,
            )
            await self._publish(
                f"{self._base_topic(device_id)}/temperature", str(temperature)
            )
            self._published.setdefault(device_id, {})["temperature"] = temperature
        except Exception as exc:
            _LOGGER.exception("Failed to build/send command for %s: %s", device_id, exc)

    def _device_info(self, device_id_str: str, sys_id: int) -> dict[str, Any]:
        return {
            "identifiers": [f"uponor_{device_id_str}"],
            "name": f"Uponor Thermostat {device_id_str}",
            "manufacturer": "Uponor",
            "model": "Smatrix thermostat",
            # Full 32-bit bus address, as used by ESPHome's uponor_smatrix
            "serial_number": f"{sys_id:04X}{device_id_str}",
        }

    def _build_discovery_config(
        self, device_id: int, sys_id: int, thermostat: UponorThermostat
    ) -> str:
        device_id_str = f"{device_id:04X}"
        unique_id = f"uponor_{device_id_str}"
        base_topic = f"{self._discovery_prefix}/climate/{unique_id}"
        config = {
            "name": f"Uponor {device_id_str}",
            "unique_id": unique_id,
            "default_entity_id": f"climate.uponor_{device_id_str.lower()}",
            "device": self._device_info(device_id_str, sys_id),
            "temperature_unit": "C",
            "temp_step": 0.5,
            "precision": 0.1,
            "min_temp": thermostat.min_temp if thermostat.min_temp is not None else 5,
            "max_temp": thermostat.max_temp if thermostat.max_temp is not None else 35,
            "current_temperature_topic": f"{base_topic}/current_temperature",
            "temperature_state_topic": f"{base_topic}/temperature",
            "action_topic": f"{base_topic}/action",
            "mode_state_topic": f"{base_topic}/mode",
            "mode_command_topic": f"{base_topic}/mode/set",
            "temperature_command_topic": f"{base_topic}/temperature/set",
            "modes": ["heat", "cool"],
            "preset_modes": ["eco"],
            "preset_mode_state_topic": f"{base_topic}/preset_mode",
            "preset_mode_command_topic": f"{base_topic}/preset_mode/set",
            "current_humidity_topic": f"{base_topic}/current_humidity",
            "availability_topic": f"{base_topic}/availability",
            "payload_available": "online",
            "payload_not_available": "offline",
        }
        # Return JSON string for MQTT discovery
        return json.dumps(config)

    def _build_sensor_config(self, device_id_str: str, sys_id: int, key: str) -> str:
        name, device_class, unit = SENSORS[key]
        base_topic = self._base_topic(device_id_str)
        config = {
            "name": name,
            "unique_id": f"uponor_{device_id_str}_{key}",
            "default_entity_id": f"sensor.uponor_{device_id_str.lower()}_{key}",
            "device": self._device_info(device_id_str, sys_id),
            "state_topic": f"{base_topic}/{key}",
            "device_class": device_class,
            "unit_of_measurement": unit,
            "state_class": "measurement",
            "availability_topic": f"{base_topic}/availability",
        }
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
            await self._write_bus(buf)
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
