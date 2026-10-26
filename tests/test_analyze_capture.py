"""Tests for tools/analyze_capture.py on a synthetic capture."""

import importlib.util
import json
import struct
import sys
from pathlib import Path

_PATH = Path(__file__).parent.parent / "tools" / "analyze_capture.py"
_spec = importlib.util.spec_from_file_location("analyze_capture", _PATH)
ac = importlib.util.module_from_spec(_spec)
# dataclasses look up their module in sys.modules
sys.modules[_spec.name] = ac
_spec.loader.exec_module(ac)
proto = ac.proto

SYS = 0x110B


def _crc(body: bytes) -> bytes:
    return body + struct.pack("<H", proto.crc16_modbus(body))


def _frame(dev: int, fields: list[tuple[int, int]]) -> bytes:
    body = struct.pack(">HH", SYS, dev)
    for fid, value in fields:
        body += struct.pack(">BH", fid, value)
    return _crc(body)


def _poll(dev: int) -> bytes:
    return _crc(struct.pack(">HHB", SYS, dev, 0xFF))


ROOM_20C = proto.temp_to_uponor(20.0)
SET_22C = proto.temp_to_uponor(22.0)
REPLY = _frame(0x0A01, [(0x40, ROOM_20C), (0x3B, SET_22C), (0x3D, 0x0040)])


def _write_capture(path: Path) -> None:
    lines = [
        {"mono": 100.0, "ts": 1.0, "dir": "note", "text": "capture started"},
        # poll and reply in one MQTT message
        {"mono": 100.1, "ts": 1.1, "dir": "rx", "hex": (_poll(0x0A01) + REPLY).hex()},
        # poll to an address that never answers
        {"mono": 100.2, "ts": 1.2, "dir": "rx", "hex": _poll(0x0B02).hex()},
        # poll and reply in separate messages, 40 ms apart
        {"mono": 105.1, "ts": 6.1, "dir": "rx", "hex": _poll(0x0A01).hex()},
        {"mono": 105.14, "ts": 6.14, "dir": "rx", "hex": REPLY.hex()},
        {"mono": 106.0, "ts": 7.0, "dir": "note", "text": "registration"},
        # CRC-valid frame without the system ID prefix
        {"mono": 106.5, "ts": 7.5, "dir": "rx", "hex": _crc(bytes.fromhex("0000beef01")).hex()},
        # our write, echoed back 80 ms later
        {"mono": 107.0, "ts": 8.0, "dir": "tx", "hex": _frame(0x0A01, [(0x3B, SET_22C)]).hex()},
        {"mono": 107.08, "ts": 8.08, "dir": "rx", "hex": _frame(0x0A01, [(0x3B, SET_22C)]).hex()},
    ]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))


def test_load_splits_frames_and_notes(tmp_path):
    cap = tmp_path / "c.jsonl"
    _write_capture(cap)
    frames, notes, unknown, sys_id = ac.load(cap)
    assert sys_id == SYS
    assert [n.text for n in notes] == ["capture started", "registration"]
    assert not unknown
    assert sum(f.is_poll for f in frames) == 3
    assert any(f.packet is None for f in frames)  # the non-standard frame
    assert frames[0].t > 0  # relative to the first entry


def test_report(tmp_path, capsys):
    cap = tmp_path / "c.jsonl"
    _write_capture(cap)
    ac.report(*ac.load(cap))
    out = capsys.readouterr().out
    assert "0B02: first seen" in out and "polled, never answers" in out
    assert "same MQTT message 1/2" in out
    assert "fields [40 3B 3D] x2" in out
    assert "demand=on below_setpoint=yes x2" in out
    assert "echoed back on the read topic: 1" in out
    assert "CRC ok 0000beef01" in out


def test_timeline_decodes_temperatures(tmp_path, capsys):
    cap = tmp_path / "c.jsonl"
    _write_capture(cap)
    frames, notes, _, _ = ac.load(cap)
    ac.timeline(frames, notes, 0, 10, device="0a01")
    out = capsys.readouterr().out
    assert "0A01 POLL" in out
    assert "current_temperature=" in out and "(20.0C)" in out
    assert "NOTE registration" in out
    assert "0B02" not in out


def test_crc_scan_finds_embedded_frame():
    frame = _crc(bytes.fromhex("0102030405"))
    assert ac.crc_scan(b"\x99" + frame) == [frame]
