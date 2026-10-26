"""Virtual thermostat: a software T-148 for a channel without a real thermostat.

The controller only drives a channel from the demand of the thermostat
registered to it, and it computes that demand itself from the room temperature
the thermostat reports. This class registers on the bus like a T-148 and
answers the controller's polls with the temperature of a Home Assistant sensor.

Replies must reach the bus within a few tens of milliseconds of the poll, too
fast for the gateway's MQTT client, so this uses the gateway's raw TCP socket.
MQTT stays in use for everything else.

Experimental: heat only.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from typing import TYPE_CHECKING, Any

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .uponor_protocol import (
    HUMIDITY_FLAGS,
    REGISTRATION_BROADCAST,
    THERMOSTAT_MODE2,
    UPONOR_INVALID_VALUE,
    UponorFields,
    build_fields_packet,
    build_poll_packet,
    build_registration_packets,
    temp_to_uponor,
)

if TYPE_CHECKING:
    from .bridge import HAUponorBridge

_LOGGER = logging.getLogger(__name__)

DEFAULT_TARGET = 20.0
DEFAULT_HUMIDITY = 50
ECO_SETBACK_RAW = 0x0048
# A T-148 repeats a changed setpoint in a few replies; do the same
SETPOINT_ANNOUNCEMENTS = 3
# Margin used to report "no demand" (2 C, in tenths of F)
NO_DEMAND_MARGIN_RAW = 36
COOLING_BIT = 0x1000
RECONNECT_DELAY = 5
REGISTRATION_WINDOW = 60
STORAGE_VERSION = 1
SAVE_DELAY = 5
# Answers per registration window; the controller rebroadcasts right after a
# rejected answer, so this stops us from flooding the bus
REGISTRATION_MAX_ANSWERS = 25
# Pause between the three registration frames so the gateway sends them as
# separate frames rather than one run of bytes (one byte takes 0.52 ms)
REGISTRATION_FRAME_GAP = 0.003


class VirtualThermostat:
    """Answer the controller's polls for one device ID over the gateway's TCP socket."""

    def __init__(
        self,
        hass: HomeAssistant,
        bridge: HAUponorBridge,
        *,
        host: str,
        port: int,
        device_id: str,
        temperature_entity: str,
        humidity_entity: str | None = None,
    ) -> None:
        self.hass = hass
        self._bridge = bridge
        self._host = host
        self._port = port
        self.device_id = device_id.upper()
        self._temperature_entity = temperature_entity
        self._humidity_entity = humidity_entity
        self.target = DEFAULT_TARGET
        self._announce = SETPOINT_ANNOUNCEMENTS
        self._cooling = False
        self._answering = True
        self._register_until = 0.0
        self._register_answers = 0
        self._writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task | None = None
        self._poll: bytes | None = None
        self._reply: bytes | None = None
        self.replies = 0
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.virtual_{self.device_id.lower()}"
        )

    @property
    def answering(self) -> bool:
        return self._answering

    async def async_start(self) -> None:
        # The thermostat owns the setpoint, so keep it across restarts
        if (stored := await self._store.async_load()) and "target" in stored:
            self.target = stored["target"]
        self._task = self.hass.async_create_background_task(
            self._run(), f"uponor_mqtt virtual thermostat {self.device_id}"
        )

    async def async_stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._close()

    def set_answering(self, answering: bool) -> None:
        """Pause or resume replies, e.g. to see how the controller copes."""
        self._answering = answering
        _LOGGER.warning(
            "Virtual thermostat %s %s replying to polls",
            self.device_id,
            "resumed" if answering else "stopped",
        )

    def open_registration(self) -> None:
        """Answer registration broadcasts for the next minute."""
        self._register_until = time.monotonic() + REGISTRATION_WINDOW
        self._register_answers = 0
        _LOGGER.warning(
            "Virtual thermostat %s will register on the next broadcast (%d s)",
            self.device_id,
            REGISTRATION_WINDOW,
        )

    def set_target(self, temperature: float) -> None:
        self.target = temperature
        self._announce = SETPOINT_ANNOUNCEMENTS
        self._reply = None
        self._store.async_delay_save(lambda: {"target": self.target}, SAVE_DELAY)

    def on_controller_packet(self, packet: dict[str, Any]) -> None:
        """Track heating/cooling from the controller's frames to this device."""
        if (action := packet.get("action")) is not None:
            cooling = bool(action & COOLING_BIT)
            if cooling != self._cooling:
                self._cooling = cooling
                self._reply = None

    # Readings

    def _sensor_value(self, entity_id: str | None) -> float | None:
        if entity_id is None or (state := self.hass.states.get(entity_id)) is None:
            return None
        # Zigbee sensors often only report on change, so a stable room can go
        # hours without an update; rely on the sensor's integration to mark
        # it unavailable when it has really gone quiet
        if state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None
        try:
            return float(state.state)
        except ValueError:
            return None

    def room_temperature(self) -> float | None:
        return self._sensor_value(self._temperature_entity)

    def humidity(self) -> int:
        value = self._sensor_value(self._humidity_entity)
        return DEFAULT_HUMIDITY if value is None else max(0, min(100, round(value)))

    def _reported_room_raw(self) -> int:
        """Room temperature to report; never asks for cooling, or for heat on bad data."""
        target_raw = temp_to_uponor(self.target)
        room = self.room_temperature()
        if room is None:
            return target_raw + NO_DEMAND_MARGIN_RAW
        if self._cooling:
            return target_raw - NO_DEMAND_MARGIN_RAW
        return temp_to_uponor(room)

    def _build_reply(self, sys_id: int) -> bytes:
        fields = [
            (UponorFields.CURRENT_TEMPERATURE, self._reported_room_raw()),
            (UponorFields.PRESET_MODE, 0x0000),
            (UponorFields.MODE2, THERMOSTAT_MODE2),
        ]
        if self._announce > 0:
            fields += [
                (UponorFields.ECO_SETBACK, ECO_SETBACK_RAW),
                (UponorFields.TEMPERATURE, temp_to_uponor(self.target)),
            ]
        fields.append((UponorFields.CURRENT_HUMIDITY, HUMIDITY_FLAGS | self.humidity()))
        return build_fields_packet(sys_id, self.device_id, fields)

    # Bus

    async def _run(self) -> None:
        warned = False
        while True:
            try:
                reader, writer = await asyncio.open_connection(self._host, self._port)
                sock = writer.get_extra_info("socket")
                if sock is not None:
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self._writer = writer
                warned = False
                _LOGGER.info(
                    "Virtual thermostat %s connected to %s:%s",
                    self.device_id,
                    self._host,
                    self._port,
                )
                while data := await reader.read(512):
                    self._on_bus_data(data)
                raise ConnectionError("gateway closed the connection")
            except (OSError, ConnectionError) as exc:
                if not warned:
                    _LOGGER.warning(
                        "Virtual thermostat %s: gateway TCP %s:%s unavailable (%s); retrying",
                        self.device_id,
                        self._host,
                        self._port,
                        exc,
                    )
                    warned = True
                self._close()
                await asyncio.sleep(RECONNECT_DELAY)

    def _close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            self._writer = None

    def _send(self, data: bytes, what: str) -> None:
        if self._writer is None:
            return
        self._writer.write(data)
        self._bridge.capture("tx", data, via="tcp", what=what)

    def _on_bus_data(self, data: bytes) -> None:
        """Handle raw bus bytes; replies are written before anything else runs."""
        sys_id = self._bridge.system_id
        if sys_id is None:
            return
        now = time.monotonic()
        if REGISTRATION_BROADCAST in data and now < self._register_until:
            if self._register_answers >= REGISTRATION_MAX_ANSWERS:
                return
            self._register_answers += 1
            loop = asyncio.get_running_loop()
            for i, frame in enumerate(build_registration_packets(sys_id, self.device_id)):
                if i == 0:
                    self._send(frame, what="register")
                else:
                    loop.call_later(
                        i * REGISTRATION_FRAME_GAP, self._send, frame, "register"
                    )
            _LOGGER.warning(
                "Virtual thermostat %s answered registration broadcast %d",
                self.device_id,
                self._register_answers,
            )
            return
        if self._poll is None:
            self._poll = build_poll_packet(sys_id, self.device_id)
        if not self._answering or self._poll not in data:
            return
        if self._reply is None:
            self._reply = self._build_reply(sys_id)
        self._send(self._reply, "reply")
        self.replies += 1
        if self._announce > 0:
            self._announce -= 1
            self._reply = None
        # Prepare the next reply and update Home Assistant after replying
        self.hass.async_create_task(self._async_after_reply(sys_id))

    async def _async_after_reply(self, sys_id: int) -> None:
        self._reply = self._build_reply(sys_id)
        room = self.room_temperature()
        await self._bridge.async_virtual_state(
            {
                "sys_id": sys_id,
                "device_id": int(self.device_id, 16),
                "current_temperature": UPONOR_INVALID_VALUE
                if room is None
                else temp_to_uponor(room),
                "temperature": temp_to_uponor(self.target),
                "preset_mode": 0x0000,
                "mode2": THERMOSTAT_MODE2,
                "current_humidity": HUMIDITY_FLAGS | self.humidity(),
            }
        )


def parse_device_id(value: str) -> str:
    """Validate a 4-hex-digit bus address that is safe to use for a virtual device."""
    value = (value or "").strip().upper()
    dev = int(value, 16)
    if len(value) != 4 or dev in (0x0000, 0xFFFF):
        raise ValueError("Device ID must be 4 hex digits, not 0000 or FFFF")
    return value

