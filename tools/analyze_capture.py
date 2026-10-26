"""Analyze a raw Uponor bus capture written by the integration.

Enable "Capture raw bus traffic" in the integration options; frames are
appended to `<config>/uponor_capture.jsonl`. Copy that file here and run:

    python tools/analyze_capture.py uponor_capture.jsonl
    python tools/analyze_capture.py uponor_capture.jsonl --around-notes 90
    python tools/analyze_capture.py uponor_capture.jsonl --from 600 --to 660

The report answers the questions needed to emulate a thermostat: how the
controller polls, how fast thermostats reply, which fields each side sends,
who decides heating demand, and what happens on the bus during registration.
Timestamps are taken when Home Assistant receives each MQTT message, so they
include the gateway's gap time and Wi-Fi/MQTT latency; frames that arrive in
the same message were less than one gap time apart on the bus.
"""

from __future__ import annotations

import argparse
import importlib.util
import itertools
import json
import logging
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_PATH = (
    Path(__file__).parent.parent
    / "custom_components"
    / "uponor_mqtt"
    / "uponor_protocol.py"
)
_spec = importlib.util.spec_from_file_location("uponor_protocol", _PATH)
proto = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(proto)

DEMAND_BIT = 0x0040
COOLING_BIT = 0x1000


@dataclass
class Frame:
    t: float  # seconds since capture start (monotonic)
    msg: int  # index of the MQTT message the frame arrived in
    dir: str  # rx / tx
    raw: bytes
    packet: dict[str, Any] | None  # parse_uponor_packet result, None if unknown

    @property
    def dev(self) -> str:
        return f"{int.from_bytes(self.raw[2:4], 'big'):04X}"

    @property
    def is_poll(self) -> bool:
        return bool(self.packet and self.packet.get("special_ff"))

    @property
    def field_ids(self) -> tuple[int, ...]:
        payload = self.raw[4:-2]
        if self.is_poll or len(payload) % 3:
            return ()
        return tuple(payload[i] for i in range(0, len(payload), 3))


@dataclass
class Note:
    t: float
    text: str


def crc_scan(data: bytes) -> list[bytes]:
    """CRC-valid chunks that don't follow the usual frame layout.

    Registration may use frames that don't start with the system ID or don't
    have a payload of 3-byte fields, which `split_packets` skips.
    """
    found = []
    i = 0
    while i + 5 <= len(data):
        for j in range(len(data), i + 4, -1):
            chunk = data[i:j]
            if proto.crc16_modbus(chunk[:-2]) == int.from_bytes(chunk[-2:], "little"):
                found.append(chunk)
                i = j
                break
        else:
            i += 1
    return found


def detect_sys_id(payloads: list[bytes]) -> int | None:
    counts = Counter(
        p[:2] for p in payloads if len(p) >= 7 and proto._is_valid_frame(p)
    )
    if not counts:
        return None
    return int.from_bytes(counts.most_common(1)[0][0], "big")


def load(path: Path) -> tuple[list[Frame], list[Note], list[tuple[float, bytes]], int | None]:
    """Read a capture file into frames, notes and unparseable chunks."""
    entries = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    if not entries:
        return [], [], [], None
    t0 = entries[0]["mono"]
    sys_id = detect_sys_id([bytes.fromhex(e["hex"]) for e in entries if "hex" in e])

    frames: list[Frame] = []
    notes: list[Note] = []
    unknown: list[tuple[float, bytes]] = []
    for msg, e in enumerate(entries):
        t = e["mono"] - t0
        if e["dir"] == "note":
            notes.append(Note(t, e.get("text", "")))
            continue
        data = bytes.fromhex(e["hex"])
        chunks = proto.split_packets(data)
        if not chunks and sys_id is not None:
            chunks = proto.split_packets(data, sys_id)
        rest = data
        for c in chunks:
            rest = rest.replace(c, b"", 1)
        for c in chunks:
            frames.append(Frame(t, msg, e["dir"], c, proto.parse_uponor_packet(c)))
        if rest:
            odd = crc_scan(rest)
            for c in odd:
                frames.append(Frame(t, msg, e["dir"], c, None))
            if not odd:
                unknown.append((t, rest))
    return frames, notes, unknown, sys_id


def fmt_temp(raw: int) -> str:
    if raw == proto.UPONOR_INVALID_VALUE:
        return "----"
    return f"{proto.uponor_to_temp(raw):.1f}C"


def describe(frame: Frame) -> str:
    """One-line decoding of a frame for timelines."""
    if frame.packet is None:
        return f"?? non-standard {frame.raw.hex()}"
    if frame.is_poll:
        return f"{frame.dev} POLL"
    payload = frame.raw[4:-2]
    parts = []
    for i in range(0, len(payload), 3):
        fid = payload[i]
        val = int.from_bytes(payload[i + 1 : i + 3], "big")
        try:
            name = proto.UponorFields(fid).name.lower()
        except ValueError:
            name = f"{fid:02X}"
        text = f"{name}={val:04X}"
        if fid in (0x3B, 0x40, 0x41, 0x37, 0x38, 0x2D):
            text += f"({fmt_temp(val)})"
        parts.append(text)
    return f"{frame.dev} " + " ".join(parts)


def stats(values: list[float]) -> str:
    if not values:
        return "-"
    return (
        f"min {min(values) * 1000:.0f} / median {statistics.median(values) * 1000:.0f}"
        f" / max {max(values) * 1000:.0f} ms (n={len(values)})"
    )


def report(frames: list[Frame], notes: list[Note], unknown, sys_id: int | None) -> None:
    rx = [f for f in frames if f.dir == "rx"]
    duration = frames[-1].t if frames else 0
    print("== Overview")
    print(f"duration {duration / 3600:.2f} h, {len(rx)} rx frames, "
          f"{len(frames) - len(rx)} tx frames, {len(notes)} notes")
    print(f"system ID {sys_id:04X}" if sys_id is not None else "system ID unknown")
    sys_ids = Counter(f"{int.from_bytes(f.raw[:2], 'big'):04X}" for f in rx if f.packet)
    if len(sys_ids) > 1:
        print(f"WARNING several system IDs seen: {dict(sys_ids)}")
    non_std = [f for f in rx if f.packet is None]
    print(f"non-standard CRC-valid frames: {len(non_std)}, unparseable chunks: {len(unknown)}")

    # Poll -> reply pairing. A reply is the next rx frame for the same device.
    polls: dict[str, int] = Counter()
    answered: dict[str, list[float]] = defaultdict(list)
    same_msg: Counter = Counter()
    unanswered_next: dict[str, Counter] = defaultdict(Counter)
    after_poll: dict[str, Counter] = defaultdict(Counter)
    unsolicited: dict[str, Counter] = defaultdict(Counter)
    poll_times: dict[str, list[float]] = defaultdict(list)
    first_seen: dict[str, float] = {}

    for i, f in enumerate(rx):
        first_seen.setdefault(f.dev, f.t)
        prev = rx[i - 1] if i else None
        if f.is_poll:
            polls[f.dev] += 1
            poll_times[f.dev].append(f.t)
            nxt = rx[i + 1] if i + 1 < len(rx) else None
            if nxt is not None and nxt.dev == f.dev and not nxt.is_poll:
                answered[f.dev].append(nxt.t - f.t)
                if nxt.msg == f.msg:
                    same_msg[f.dev] += 1
            else:
                unanswered_next[f.dev][describe(nxt) if nxt else "end of capture"] += 1
        elif f.packet is not None:
            if prev is not None and prev.is_poll and prev.dev == f.dev:
                after_poll[f.dev][f.field_ids] += 1
            else:
                unsolicited[f.dev][f.field_ids] += 1

    print("\n== Devices (first seen, polls, replies)")
    for dev in sorted(first_seen):
        n_poll = polls[dev]
        n_ans = len(answered.get(dev, []))
        flag = "  <- polled, never answers" if n_poll and not n_ans else ""
        print(f"{dev}: first seen +{first_seen[dev]:.1f}s, polls {n_poll}, "
              f"answered {n_ans}{flag}")

    print("\n== Poll -> reply delay (as seen by Home Assistant)")
    for dev in sorted(answered):
        print(f"{dev}: {stats(answered[dev])}; same MQTT message {same_msg[dev]}"
              f"/{len(answered[dev])}")
    for dev in sorted(unanswered_next):
        top = ", ".join(f"{k} x{v}" for k, v in unanswered_next[dev].most_common(3))
        print(f"{dev}: unanswered polls followed by: {top}")

    print("\n== Poll interval per device")
    for dev in sorted(poll_times):
        gaps = [b - a for a, b in zip(poll_times[dev], poll_times[dev][1:])]
        if gaps:
            print(f"{dev}: min {min(gaps):.1f} / median {statistics.median(gaps):.1f}"
                  f" / max {max(gaps):.1f} s")
    order = [f.dev for f in rx if f.is_poll][:40]
    print("first polls in order:", " ".join(order) or "-")

    def show(title: str, table: dict[str, Counter]) -> None:
        print(f"\n== {title}")
        for dev in sorted(table):
            for ids, n in table[dev].most_common():
                names = " ".join(f"{i:02X}" for i in ids) or "(none)"
                print(f"{dev}: fields [{names}] x{n}")

    show("Field sets right after a poll (thermostat replies)", after_poll)
    show("Field sets not preceded by a poll (likely controller -> thermostat)", unsolicited)

    demand_report(rx)
    tx_report(frames)

    if non_std or unknown:
        print("\n== Non-standard data (registration candidates)")
        for f in non_std[:50]:
            print(f"+{f.t:.3f}s CRC ok {f.raw.hex()}")
        for t, chunk in unknown[:50]:
            print(f"+{t:.3f}s no CRC {chunk.hex()}")

    if notes:
        print("\n== Notes")
        for n in notes:
            print(f"+{n.t:.1f}s {n.text}")


def demand_report(rx: list[Frame]) -> None:
    """Compare the demand bit with room temperature vs setpoint.

    If demand always follows room < setpoint (heating) the controller could be
    the one deciding; the poll/field tables show which side sends `action`.
    """
    room: dict[str, int] = {}
    target: dict[str, int] = {}
    table: dict[str, Counter] = defaultdict(Counter)
    for f in rx:
        p = f.packet
        if not p or f.is_poll:
            continue
        if (v := p.get("current_temperature")) not in (None, proto.UPONOR_INVALID_VALUE):
            room[f.dev] = v
        if (v := p.get("temperature")) not in (None, proto.UPONOR_INVALID_VALUE):
            target[f.dev] = v
        if (v := p.get("action")) is not None and f.dev in room and f.dev in target:
            cooling = bool(v & COOLING_BIT)
            wants = room[f.dev] > target[f.dev] if cooling else room[f.dev] < target[f.dev]
            table[f.dev][(bool(v & DEMAND_BIT), wants, cooling)] += 1
    if not table:
        return
    print("\n== Demand bit vs room/setpoint (raw bus setpoint, no offsets)")
    for dev in sorted(table):
        rows = ", ".join(
            f"demand={'on' if d else 'off'} below_setpoint={'yes' if w else 'no'}"
            f"{' cooling' if c else ''} x{n}"
            for (d, w, c), n in sorted(table[dev].items())
        )
        print(f"{dev}: {rows}")


def tx_report(frames: list[Frame]) -> None:
    """Latency of our own writes: echo on the read topic and next frame from the device."""
    tx = [(i, f) for i, f in enumerate(frames) if f.dir == "tx"]
    if not tx:
        return
    echo, reply = [], []
    for i, f in tx:
        later = []
        for g in itertools.islice(frames, i + 1, None):
            if g.t - f.t >= 120:
                break
            if g.dir == "rx":
                later.append(g)
        if (e := next((g for g in later if g.raw == f.raw), None)) is not None:
            echo.append(e.t - f.t)
        if (r := next((g for g in later if g.dev == f.dev and not g.is_poll), None)) is not None:
            reply.append(r.t - f.t)
    print("\n== Our writes")
    print(f"{len(tx)} writes; echoed back on the read topic: {len(echo)}")
    print(f"write -> echo: {stats(echo)}")
    print(f"write -> next frame from that device: {stats(reply)}")


def timeline(frames: list[Frame], notes: list[Note], start: float, end: float,
             device: str | None = None) -> None:
    items: list[tuple[float, str]] = [(n.t, f"NOTE {n.text}") for n in notes if start <= n.t <= end]
    for f in frames:
        if start <= f.t <= end and (device is None or f.dev == device.upper()):
            items.append((f.t, f"{f.dir} {describe(f)}"))
    for t, text in sorted(items, key=lambda x: x[0]):
        print(f"+{t:10.3f}s {text}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("capture", type=Path)
    parser.add_argument("--around-notes", type=float, metavar="SECONDS",
                        help="print a decoded timeline from each note to SECONDS after it")
    parser.add_argument("--from", dest="start", type=float, metavar="SECONDS",
                        help="print a decoded timeline starting here (seconds from start)")
    parser.add_argument("--to", dest="end", type=float, metavar="SECONDS")
    parser.add_argument("--device", help="limit timelines to one device ID, e.g. 0A1B")
    args = parser.parse_args()

    # parse_uponor_packet warns on frames it can't decode; the report covers those
    logging.disable(logging.WARNING)
    frames, notes, unknown, sys_id = load(args.capture)
    if not frames:
        raise SystemExit("No bus frames in capture")

    if args.start is not None:
        timeline(frames, notes, args.start, args.end or float("inf"), args.device)
        return
    report(frames, notes, unknown, sys_id)
    if args.around_notes:
        for n in notes:
            print(f"\n== Timeline after note '{n.text}'")
            timeline(frames, notes, n.t - 2, n.t + args.around_notes, args.device)


if __name__ == "__main__":
    main()
