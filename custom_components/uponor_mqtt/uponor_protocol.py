"""Protocol helpers for parsing and building Uponor bus packets.

This module provides minimal helpers used by the bridge implementation.
"""

import struct
import datetime as _dt
import logging
from enum import IntEnum
from typing import Any

log = logging.getLogger(__name__)

UPONOR_INVALID_VALUE = 0x7FFF


class UponorFields(IntEnum):
    DATETIME1 = 0x08
    DATETIME2 = 0x09
    DATETIME3 = 0x0A
    HEATING_COOLING_OFFSET = 0x0C
    OUTDOOR_TEMPERATURE = 0x2D
    UNKNOWN2 = 0x35
    TEMPERATURE_LOW = 0x37
    TEMPERATURE_HIGH = 0x38
    TEMPERATURE = 0x3B
    ECO_SETBACK = 0x3C
    ACTION = 0x3D
    PRESET_MODE = 0x3E
    MODE2 = 0x3F
    CURRENT_TEMPERATURE = 0x40
    EXTERNAL_TEMPERATURE = 0x41
    CURRENT_HUMIDITY = 0x42
    REQUEST = 0xFF


def uponor_to_temp(value: float, unit: str = "C", round_to_half: bool = False) -> float:
    temp = value / 10.0
    if unit == "C":
        temp = (temp - 32.0) / 1.8
    if round_to_half:
        temp = round(2 * temp) / 2
    return round(temp, 1)


def uponor_delta_to_c(value: float) -> float:
    delta_c = value / 18.0
    return round(delta_c * 2) / 2


def temp_to_uponor(temp: float, unit: str = "C") -> int:
    if unit == "C":
        temp = temp * 1.8 + 32.0
    return int(round(10 * temp))


def crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc & 0xFFFF


def _is_valid_frame(pkt: bytes) -> bool:
    # header (4) + payload (1 request byte or N 3-byte fields) + CRC (2)
    if len(pkt) != 7 and (len(pkt) < 9 or (len(pkt) - 6) % 3 != 0):
        return False
    return crc16_modbus(pkt[:-2]) == int.from_bytes(pkt[-2:], "little")


def split_packets(data: bytes, sys_id: int | None = None) -> list[bytes]:
    """Split a raw bus buffer into CRC-valid packets.

    Every frame starts with the 2-byte system ID of the controller. If
    `sys_id` is not known yet, the first two bytes of the buffer are used.
    """
    if len(data) < 7:
        return []
    if _is_valid_frame(data):
        return [data]
    marker = sys_id.to_bytes(2, "big") if sys_id is not None else data[0:2]
    packets = []
    i = 0
    while i + 7 <= len(data):
        if data[i : i + 2] != marker:
            i += 1
            continue
        j = i + 2
        while j + 5 <= len(data):
            if data[j : j + 2] == marker:
                break
            j += 1
        pkt_end = j if j + 5 <= len(data) else len(data)
        pkt = data[i:pkt_end]
        if crc16_modbus(pkt[:-2]) == int.from_bytes(pkt[-2:], "little"):
            packets.append(pkt)
            i = pkt_end
        else:
            i += 1
    return packets


def parse_uponor_packet(raw: bytes) -> dict[str, Any] | None:
    if len(raw) < 7:
        return None
    sys_id = int.from_bytes(raw[0:2], "big")
    device_id = int.from_bytes(raw[2:4], "big")
    payload = raw[4:-2]
    crc_recv = int.from_bytes(raw[-2:], "little")
    if crc16_modbus(raw[:-2]) != crc_recv:
        log.warning("CRC mismatch: expected %s", f"{crc_recv:#06x}")
        return None
    result = {
        "sys_id": sys_id,
        "device_id": device_id,
        "raw_payload": payload.hex(),
    }
    if len(payload) == 1 and payload[0] == 0xFF:
        result["special_ff"] = True
        return result
    if len(payload) % 3 != 0:
        log.warning("Payload length not a multiple of 3")
        return None
    for i in range(0, len(payload), 3):
        fid = payload[i]
        try:
            name = UponorFields(fid).name.lower()
        except ValueError:
            name = f"unknown_{fid:02X}"
        raw_val = int.from_bytes(payload[i + 1 : i + 3], "big")
        result[name] = raw_val
    return result


class UponorThermostat:
    """Accumulated state of one thermostat.

    A thermostat's fields arrive spread over several frames, and the setpoint
    on the bus does not include the offsets the thermostat applies itself, so
    the displayed setpoint can only be worked out from the latest value of
    every field. Mirrors ESPHome's `uponor_smatrix` climate component, plus
    the heating/cooling offset from esphome/esphome#11129.
    """

    def __init__(self) -> None:
        self.target_raw: int | None = None
        self.eco_setback_raw = 0x0048
        self.heating_cooling_offset_raw = 0x0024
        self.cooling = False
        self.eco = False
        self.demand: int | None = None
        self.current_temperature: float | None = None
        self.external_temperature: float | None = None
        self.current_humidity: int | None = None
        self.min_temp: float | None = None
        self.max_temp: float | None = None

    def update(self, packet: dict[str, Any]) -> None:
        """Apply the fields of a packet from `parse_uponor_packet`."""

        def valid(field: str) -> int | None:
            value = packet.get(field)
            return None if value == UPONOR_INVALID_VALUE else value

        if (v := packet.get("heating_cooling_offset")) is not None:
            self.heating_cooling_offset_raw = v
        if (v := valid("temperature_low")) is not None:
            self.min_temp = uponor_to_temp(v, round_to_half=True)
        if (v := valid("temperature_high")) is not None:
            self.max_temp = uponor_to_temp(v, round_to_half=True)
        # 0x7FFF is the controller asking the thermostat for its setpoint
        if (v := valid("temperature")) is not None:
            self.target_raw = v
        if (v := valid("eco_setback")) is not None:
            self.eco_setback_raw = v
        if (v := packet.get("action")) is not None:
            self.demand = v
            self.cooling = bool(v & 0x1000)
        if (v := packet.get("mode2")) is not None:
            self.cooling = bool(v & 0x1000)
        if (v := packet.get("preset_mode")) is not None:
            self.eco = bool(v & 0x0008)
        if "current_temperature" in packet:
            v = valid("current_temperature")
            self.current_temperature = None if v is None else uponor_to_temp(v)
        if "external_temperature" in packet:
            v = valid("external_temperature")
            self.external_temperature = None if v is None else uponor_to_temp(v)
        if (v := packet.get("current_humidity")) is not None:
            self.current_humidity = v & 0xFF

    def _offset_raw(self) -> int:
        """Offset the thermostat adds to the bus setpoint in the current mode."""
        if self.cooling:
            offset = self.heating_cooling_offset_raw
            return offset + self.eco_setback_raw if self.eco else offset
        return -self.eco_setback_raw if self.eco else 0

    @property
    def target_temperature(self) -> float | None:
        """Setpoint as shown on the thermostat."""
        if self.target_raw is None:
            return None
        return uponor_to_temp(self.target_raw + self._offset_raw(), round_to_half=True)

    def setpoint_raw(self, temperature: float) -> int:
        """Bus value that makes the thermostat show `temperature`."""
        return temp_to_uponor(temperature) - self._offset_raw()

    def state(self) -> dict[str, Any]:
        """Home Assistant state; fields not seen yet are left out."""
        state: dict[str, Any] = {
            "mode": "cool" if self.cooling else "heat",
            "preset_mode": "eco" if self.eco else "none",
        }
        if self.demand is not None:
            if self.demand & 0x0040:
                state["action"] = "cooling" if self.cooling else "heating"
            else:
                state["action"] = "idle"
        values = {
            "temperature": self.target_temperature,
            "current_temperature": self.current_temperature,
            "external_temperature": self.external_temperature,
            "current_humidity": self.current_humidity,
            "min_temp": self.min_temp,
            "max_temp": self.max_temp,
        }
        state.update({k: v for k, v in values.items() if v is not None})
        return state


def build_poll_packet(sys_id: int, dev_id: str) -> bytes:
    """Build the controller's poll (`FF`) for a device."""
    body = struct.pack(">HHB", sys_id, int(dev_id, 16), UponorFields.REQUEST)
    return body + struct.pack("<H", crc16_modbus(body))


def build_fields_packet(sys_id: int, dev_id: str, fields: list[tuple[int, int]]) -> bytes:
    """Build a frame carrying `(field, value)` pairs."""
    body = struct.pack(">HH", sys_id, int(dev_id, 16))
    for field, value in fields:
        body += struct.pack(">BH", field, value)
    return body + struct.pack("<H", crc16_modbus(body))


# The controller broadcasts this every ~200 ms while a channel waits for a
# thermostat to register
REGISTRATION_BROADCAST = bytes.fromhex("19000000fab981")
# A T-148 reports this mode2 value, both in its replies and when registering
THERMOSTAT_MODE2 = 0x0C03
# High byte a T-148 sends with its humidity reading
HUMIDITY_FLAGS = 0x9600


def build_registration_packets(sys_id: int, dev_id: str) -> list[bytes]:
    """Frames a T-148 sends after a registration broadcast (seen on an X-245)."""
    dev = int(dev_id, 16)
    return [
        body + struct.pack("<H", crc16_modbus(body))
        for body in (
            struct.pack(">HHBBH", sys_id, dev, 0xFA, UponorFields.MODE2, THERMOSTAT_MODE2),
            struct.pack(">HHB", sys_id, dev, 0xFB),
            struct.pack(">HHB", sys_id, dev, 0xFC),
        )
    ]


def build_command_packet(
    sys_id: int, dev_id: str, temp: float, unit: str = "C"
) -> bytes:
    return build_setpoint_packet(sys_id, dev_id, temp_to_uponor(temp=temp, unit=unit))


def build_setpoint_packet(sys_id: int, dev_id: str, raw: int) -> bytes:
    """Build a setpoint command from a raw bus value (tenths of °F)."""
    try:
        dev_id_int = int(dev_id, 16)
    except ValueError as exc:
        raise ValueError(f"Invalid device id '{dev_id}': {exc}") from None

    # The thermostat only reacts if a null setpoint is sent first
    payload = struct.pack(
        ">HHBHBH",
        sys_id,
        dev_id_int,
        UponorFields.TEMPERATURE,
        0,
        UponorFields.TEMPERATURE,
        raw,
    )
    crc = crc16_modbus(payload)
    return payload + struct.pack("<H", crc)


def build_time_command_packet(
    sys_id: int, dev_id: str, when: _dt.datetime | None = None
) -> bytes:
    try:
        dev_id_int = int(dev_id, 16)
    except ValueError as exc:
        raise ValueError(f"Invalid device id '{dev_id}': {exc}") from None

    if when is None:
        when = _dt.datetime.now()

    year = max(0, min(127, when.year - 2000))
    month = max(1, min(12, when.month))
    day_of_week = when.weekday() & 0x07
    day_of_month = max(1, min(31, when.day))
    hour = max(0, min(23, when.hour))
    minute = max(0, min(59, when.minute))
    second = max(0, min(59, when.second))

    time1 = ((year & 0x7F) << 7) | ((month & 0x0F) << 3) | (day_of_week & 0x07)
    time2 = ((day_of_month & 0x1F) << 11) | ((hour & 0x1F) << 6) | (minute & 0x3F)
    time3 = second & 0xFFFF

    payload = struct.pack(
        ">HHBHBHBH",
        sys_id,
        dev_id_int,
        UponorFields.DATETIME1,
        time1,
        UponorFields.DATETIME2,
        time2,
        UponorFields.DATETIME3,
        time3,
    )

    crc = crc16_modbus(payload)
    return payload + struct.pack("<H", crc)
