"""
Copied subset of repository `uponor_protocol.py` so the integration is self-contained.

This file is a verbatim copy (with no external dependencies) of the protocol
helpers used by the bridge. Keeping this inside the integration avoids import
errors when the repository layout differs from Home Assistant's `custom_components`.
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


def split_packets(data: bytes) -> list[bytes]:
    packets = []
    i = 0
    while i + 7 <= len(data):
        if data[i] != 0x11 or data[i + 1] != 0x08:
            i += 1
            continue
        j = i + 2
        while j + 5 <= len(data):
            if data[j] == 0x11 and data[j + 1] == 0x08:
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
    if len(raw) < 7 or raw[0] != 0x11 or raw[1] != 0x08:
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


def parse_state(packet: dict[str, Any]) -> dict[str, Any]:
    state = {}
    is_cooling = False
    heating_cooling_offset_raw = packet.get("heating_cooling_offset", 0x24)
    heating_cooling_offset = (
        uponor_delta_to_c(heating_cooling_offset_raw)
        if heating_cooling_offset_raw is not None
        else 0.0
    )

    mode2 = packet.get("mode2")
    if mode2 is not None:
        is_cooling = bool(mode2 & 0x1000)

    d = packet.get("action")
    if d is not None:
        if d & 0x0040:
            state["action"] = "cooling" if (d & 0x1000) else "heating"
        else:
            state["action"] = "idle"

    h = packet.get("current_humidity")
    if h is not None:
        state["current_humidity"] = h & 0xFF

    rt = packet.get("current_temperature")
    if rt is not None and rt != UPONOR_INVALID_VALUE:
        state["current_temperature"] = uponor_to_temp(rt)

    t = packet.get("temperature")
    if t is not None:
        base_temp = uponor_to_temp(t, round_to_half=True)
        if is_cooling:
            state["temperature"] = base_temp + heating_cooling_offset
        else:
            state["temperature"] = base_temp

    m1 = packet.get("preset_mode")
    if m1 is not None:
        state["preset_mode"] = "eco" if (m1 & 0x0008) else None

    if mode2 is not None:
        if is_cooling:
            state["mode"] = "cool"
        else:
            state["mode"] = "heat"

    ext_temp = packet.get("external_temperature")
    if ext_temp is not None and ext_temp != UPONOR_INVALID_VALUE:
        state["external_temperature"] = uponor_to_temp(ext_temp)

    temp_low = packet.get("temperature_low")
    if temp_low is not None and temp_low != UPONOR_INVALID_VALUE:
        state["temperature_low"] = uponor_to_temp(temp_low)

    temp_high = packet.get("temperature_high")
    if temp_high is not None and temp_high != UPONOR_INVALID_VALUE:
        state["temperature_high"] = uponor_to_temp(temp_high)

    eco = packet.get("eco_setback")
    if eco is not None and eco != UPONOR_INVALID_VALUE:
        state["eco_setback"] = uponor_delta_to_c(eco)

    outdoor = packet.get("outdoor_temperature")
    if outdoor is not None and outdoor != UPONOR_INVALID_VALUE:
        state["outdoor_temperature"] = uponor_to_temp(outdoor)

    return state


def build_command_packet(
    sys_id: int, dev_id: str, temp: float, unit: str = "C"
) -> bytes:
    try:
        dev_id_int = int(dev_id, 16)
    except ValueError as exc:
        raise ValueError(f"Invalid device id '{dev_id}': {exc}") from None

    tmp_val = temp_to_uponor(temp=temp, unit=unit)

    payload = struct.pack(
        ">HHBHBH",
        sys_id,
        dev_id_int,
        UponorFields.TEMPERATURE,
        0,
        UponorFields.TEMPERATURE,
        tmp_val,
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
