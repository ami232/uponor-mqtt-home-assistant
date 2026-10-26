"""Tests for the Uponor bus protocol helpers.

`uponor_protocol.py` only uses the standard library, so it is loaded by file
path to avoid importing the integration package (which needs Home Assistant).
"""

import datetime as dt
import importlib.util
import struct
from pathlib import Path

import pytest

_PATH = (
    Path(__file__).parent.parent
    / "custom_components"
    / "uponor_mqtt"
    / "uponor_protocol.py"
)
_spec = importlib.util.spec_from_file_location("uponor_protocol", _PATH)
proto = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(proto)

SYSTEM_IDS = [0x1108, 0x110B, 0x2201]


def _frame(sys_id: int, dev_id: int, fields: list[tuple[int, int]]) -> bytes:
    body = struct.pack(">HH", sys_id, dev_id)
    for fid, value in fields:
        body += struct.pack(">BH", fid, value)
    return body + struct.pack("<H", proto.crc16_modbus(body))


def _request_frame(sys_id: int, dev_id: int) -> bytes:
    body = struct.pack(">HHB", sys_id, dev_id, 0xFF)
    return body + struct.pack("<H", proto.crc16_modbus(body))


@pytest.mark.parametrize("sys_id", SYSTEM_IDS)
def test_command_packet_round_trip(sys_id):
    pkt = proto.build_command_packet(sys_id, "00a1", 21.5)
    assert proto.split_packets(pkt) == [pkt]
    parsed = proto.parse_uponor_packet(pkt)
    assert parsed["sys_id"] == sys_id
    assert parsed["device_id"] == 0x00A1
    assert proto.uponor_to_temp(parsed["temperature"]) == 21.5


@pytest.mark.parametrize("sys_id", SYSTEM_IDS)
def test_time_packet_round_trip(sys_id):
    when = dt.datetime(2026, 10, 26, 14, 37, 12)
    pkt = proto.build_time_command_packet(sys_id, "00a1", when)
    parsed = proto.parse_uponor_packet(pkt)
    assert parsed["sys_id"] == sys_id
    assert parsed["datetime1"] == (26 << 7) | (10 << 3) | when.weekday()
    assert parsed["datetime2"] == (26 << 11) | (14 << 6) | 37
    assert parsed["datetime3"] == 12


@pytest.mark.parametrize("sys_id", SYSTEM_IDS)
def test_split_concatenated_frames(sys_id):
    frames = [
        _frame(sys_id, 0x00A1, [(proto.UponorFields.CURRENT_TEMPERATURE, 700)]),
        _request_frame(sys_id, 0x00A2),
        _frame(
            sys_id,
            0x00A3,
            [
                (proto.UponorFields.TEMPERATURE, 700),
                (proto.UponorFields.CURRENT_HUMIDITY, 45),
            ],
        ),
    ]
    assert proto.split_packets(b"".join(frames)) == frames


def test_split_resyncs_on_known_system_id():
    frame = _frame(0x110B, 0x00A1, [(proto.UponorFields.CURRENT_TEMPERATURE, 700)])
    data = b"\x42\x17" + frame + frame
    assert proto.split_packets(data) == []
    assert proto.split_packets(data, 0x110B) == [frame, frame]


def test_corrupted_crc_rejected():
    pkt = bytearray(proto.build_command_packet(0x110B, "00a1", 21.0))
    pkt[-1] ^= 0xFF
    assert proto.split_packets(bytes(pkt)) == []
    assert proto.parse_uponor_packet(bytes(pkt)) is None


def test_request_frame():
    pkt = _request_frame(0x110B, 0x00A1)
    assert proto.split_packets(pkt) == [pkt]
    parsed = proto.parse_uponor_packet(pkt)
    assert parsed["special_ff"] is True
    assert parsed["sys_id"] == 0x110B


def test_poll_packet_matches_controller():
    # Poll the X-245 sent to thermostat 42A5 (system 1108), from a bus capture
    assert proto.build_poll_packet(0x1108, "42a5").hex() == "110842a5ff4167"


def test_registration_packets_match_t148():
    # What T-148 42A5 sent after the X-245's registration broadcast
    assert [f.hex() for f in proto.build_registration_packets(0x1108, "42A5")] == [
        "110842a5fa3f0c030fe5",
        "110842a5fb40a4",
        "110842a5fc0166",
    ]


def test_reply_packet_matches_t148():
    # A T-148 reply to a poll: room 25.2 C, preset 0, mode2, humidity 36 %
    fields = [(0x40, 0x0305), (0x3E, 0x0000), (0x3F, 0x0C03), (0x42, 0x9624)]
    assert (
        proto.build_fields_packet(0x1108, "42A5", fields).hex()
        == "110842a54003053e00003f0c0342962468a3"
    )


def _thermostat(*frames: list[tuple[int, int]]) -> "proto.UponorThermostat":
    thermostat = proto.UponorThermostat()
    for fields in frames:
        thermostat.update(proto.parse_uponor_packet(_frame(0x110B, 0x00A1, fields)))
    return thermostat


F = proto.UponorFields


def test_thermostat_state():
    thermostat = _thermostat(
        [
            (F.CURRENT_TEMPERATURE, proto.temp_to_uponor(20.0)),
            (F.TEMPERATURE, proto.temp_to_uponor(22.0)),
            (F.CURRENT_HUMIDITY, 48),
            (F.MODE2, 0x0000),
            (F.ACTION, 0x0040),
        ]
    )
    assert thermostat.state() == {
        "current_temperature": 20.0,
        "temperature": 22.0,
        "current_humidity": 48,
        "mode": "heat",
        "preset_mode": "none",
        "action": "heating",
    }


def test_thermostat_fields_accumulate_across_frames():
    thermostat = _thermostat(
        [(F.TEMPERATURE, proto.temp_to_uponor(22.0))],
        [(F.CURRENT_TEMPERATURE, proto.temp_to_uponor(20.0))],
        [
            (F.TEMPERATURE_LOW, proto.temp_to_uponor(10.0)),
            (F.TEMPERATURE_HIGH, proto.temp_to_uponor(28.0)),
            (F.EXTERNAL_TEMPERATURE, proto.temp_to_uponor(24.5)),
        ],
    )
    state = thermostat.state()
    assert state["temperature"] == 22.0
    assert state["current_temperature"] == 20.0
    assert state["external_temperature"] == 24.5
    assert (state["min_temp"], state["max_temp"]) == (10.0, 28.0)


def test_setpoint_request_value_ignored():
    thermostat = _thermostat(
        [(F.TEMPERATURE, proto.temp_to_uponor(22.0))],
        [(F.TEMPERATURE, proto.UPONOR_INVALID_VALUE)],
    )
    assert thermostat.target_temperature == 22.0


def test_invalid_sensor_values_left_out():
    thermostat = _thermostat(
        [
            (F.CURRENT_TEMPERATURE, proto.UPONOR_INVALID_VALUE),
            (F.EXTERNAL_TEMPERATURE, proto.UPONOR_INVALID_VALUE),
        ]
    )
    state = thermostat.state()
    assert "current_temperature" not in state
    assert "external_temperature" not in state


# (fields, setpoint shown on the thermostat) for a bus setpoint of 21 °C,
# a 2 °C heating/cooling offset and a 4 °C ECO setback
OFFSET_CASES = {
    "heating": ([(F.MODE2, 0x0000)], 21.0),
    "heating eco": ([(F.MODE2, 0x0000), (F.PRESET_MODE, 0x0008)], 17.0),
    "cooling": ([(F.MODE2, 0x1000)], 23.0),
    "cooling eco": ([(F.MODE2, 0x1000), (F.PRESET_MODE, 0x0008)], 27.0),
    "cooling from demand": ([(F.ACTION, 0x1040)], 23.0),
}


@pytest.mark.parametrize("case", OFFSET_CASES)
def test_setpoint_offsets(case):
    fields, shown = OFFSET_CASES[case]
    thermostat = _thermostat(
        [
            (F.HEATING_COOLING_OFFSET, 36),
            (F.ECO_SETBACK, 72),
            (F.TEMPERATURE, proto.temp_to_uponor(21.0)),
            *fields,
        ]
    )
    assert thermostat.target_temperature == shown
    # Setting the shown value writes the original bus value back
    assert thermostat.setpoint_raw(shown) == proto.temp_to_uponor(21.0)


def test_cooling_action_and_mode():
    thermostat = _thermostat([(F.MODE2, 0x1000), (F.ACTION, 0x1040)])
    assert thermostat.state()["mode"] == "cool"
    assert thermostat.state()["action"] == "cooling"
    thermostat = _thermostat([(F.ACTION, 0x0000)])
    assert thermostat.state()["action"] == "idle"


@pytest.mark.parametrize("celsius", [5.0, 18.5, 21.0, 35.0])
def test_temperature_conversion_round_trip(celsius):
    assert proto.uponor_to_temp(proto.temp_to_uponor(celsius)) == celsius


def test_delta_conversion():
    # 2 °C offset is 36 in tenths of °F
    assert proto.uponor_delta_to_c(36) == 2.0
