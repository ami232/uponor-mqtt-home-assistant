# Bus protocol notes: towards a virtual thermostat

Goal: let Home Assistant act as a thermostat for an Uponor channel that has no thermostat (e.g. a bathroom), using a Zigbee room sensor. The integration would answer on the RS485 bus like a real bus thermostat, so the controller (tested with a Smatrix Base Pulse X-245) opens and closes that channel's actuator itself.

This is research. Nothing here transmits anything new on the bus yet. This file collects what the capture shows before any emulation is written.

## What is already known

- Frame: `sys_id(2) dev_id(2) [field(1) value(2)]* crc(2)`, where the CRC is CRC-16/Modbus, little-endian. Temperatures are °F × 10, and `0x7FFF` means invalid or "please send".
- A 1-byte `0xFF` payload is the controller polling a device. The thermostat's data frames follow.
- Frames carry a single address, so a frame doesn't say whether the controller or the thermostat sent it. The analysis infers that from the order of frames.

## Capture procedure

1. **Settings → Devices & Services → Uponor MQTT Bridge → Configure**, then turn on **Capture raw bus traffic**. The integration reloads and appends every received MQTT payload (`rx`) and everything it writes (`tx`) to `<config>/uponor_capture.jsonl`.
2. **Normal traffic:** leave it alone for a few minutes. Then force one thermostat into and out of heating demand by raising and lowering its setpoint, so the demand bit changes. A longer run, such as overnight, only adds rare periodic frames.
3. **Latency probe:** set one thermostat to the setpoint it already has, through its climate entity. The write and any echo or reply show up in the report under "Our writes".
4. **Registration:** re-register **one existing thermostat to its own channel**, following the controller manual (Smatrix Base Pulse: §10.7 to unregister a channel, §6.x to register a thermostat). Before each step, call the note service so the step can be matched to the bus traffic. For example, in Developer Tools → Actions:
   ```yaml
   action: uponor_mqtt.capture_note
   data:
     text: "Controller: entering registration mode, channel 3"
   ```
   Do this for each step (unregister, registration mode, thermostat button press, confirm/exit). Note which channel the thermostat is on, and its device ID (from its entity name).
5. Turn capture off again in the options, then copy the file off the Home Assistant host (Samba / SSH / File editor).

## Analysis

```
python tools/analyze_capture.py uponor_capture.jsonl                     # full report
python tools/analyze_capture.py uponor_capture.jsonl --around-notes 120  # + timelines after each note
python tools/analyze_capture.py uponor_capture.jsonl --from 3600 --to 3700 --device 0A1B
```

The report answers the questions below.

### Findings: capture of 2026-10-26

Setup: X-245 (system ID `1108`), five T-148s (`42A5`, `42C1`, `429E`, `02CA`, `4299`), no R-208, EW41B with a 10 ms gap time. 35 min, about 2,500 frames. Test thermostat: `42A5` (Dormitorio entrada, channel 3).

| Question | Finding |
| --- | --- |
| Poll pattern | Every thermostat is polled every **10.0 s**, in a fixed order (`42A5 42C1 429E 02CA 4299`). Each gets a ~200 ms slot, so the whole cycle takes ~0.8 s and the bus is idle for the other ~9 s. No address is polled unless it's registered. |
| One slot | Controller `FF` poll → thermostat reply within the gateway's 10 ms gap (same MQTT message in 164/170 replies; Salón's longer reply arrives 20–28 ms after) → controller frame to that thermostat **~30 ms after the poll** (p95 50 ms) → next poll ~170 ms later. |
| Thermostat reply | `40` room temperature, `3E` preset, `3F` mode2 (`0C03`), `42` humidity. It adds `3C 3B` (eco setback, setpoint) only when the setpoint was changed on the thermostat or written from the bus. Salón adds `08 09 0A` (its clock) every time, and `42A5` did so briefly after re-registering. |
| Controller frame | `2D` outdoor (7FFF), `3D` action, `0C` heat/cool offset, `37 38` min/max, `3B` setpoint, `3C` eco setback, `35` (8000), and usually `08 09 0A` (clock). |
| Who decides demand | **The controller.** `3D` bit `0x0040` (actuator on) is in the controller's frame, and it follows the thermostat's reported room temperature against the setpoint, one cycle behind. Bit `0x0004` also appears (Salón at baseline, `42A5` after setpoint writes); its meaning is unknown. |
| Who owns the setpoint | **The thermostat.** A setpoint written to `42A5` from HA was taken by the thermostat even while it was unregistered. The thermostat reports it in its next reply (`3B`), and the controller then copies it. |
| Our writes | The EW41B does **not** echo what we write, so this capture doesn't measure our round trip. |
| Our round trip (measured 2026-10-26) | `uponor_mqtt.measure_latency` sent the controller's own poll to `42A5` 20 times during the idle window, and **20/20 replies** came back. HA → bus → thermostat → HA took **min 97 / median 218 / p90 270 / max 272 ms**, rising in ~50 ms steps (≈170 → ≈215 → ≈270). Pinging the EW41B from the HA host averages ~5 ms (max 26 ms), so nearly all the delay is inside the EW41B's MQTT ↔ UART path. Its raw TCP socket (8899, `netp`) is disabled. |
| Round trip over raw TCP (measured 2026-10-26) | A `netp` TCP server socket was added on the EW41B (port 8899, route UART, max 1 client). Same probe, run from the HA host: **20/20 replies, min 43.8 / median 45.3 / p90 48.8 / max 55.2 ms**, very steady. About 30 ms of that is the bus itself: our poll on the wire (~4 ms at 19200 baud), the thermostat's reply (~14 ms for 27 bytes) and the EW41B's 10 ms gap. That leaves **~15 ms of network and gateway overhead**. A reply from HA would start on the bus roughly **25–35 ms after the poll**, which is about when the controller sends its own frame (p50 30 ms). Lowering the gap time (10 → 3 ms) would save ~7 ms. |
| Unregistered | The controller stops polling the address (`42A5` had no polls for ~460 s) and nothing else is sent to it. Other thermostats keep their normal cycle. |
| Registration | With the channel armed (green blink), normal polling **stops** and the controller broadcasts `19 00 00 00 FA` + CRC (`19000000fab981`) every ~200 ms; we saw 194 of them over 38 s. When the T-148 shows CnF, it answers **within the 10 ms gap after a broadcast** with three frames from its own address: `1108 42A5 FA 3F 0C03`, `1108 42A5 FB`, `1108 42A5 FC` (+CRC). The broadcasts stop, and polling of the new address starts right away (two polls 0.3 s apart, then every 10 s). Nothing that looks like an acknowledgement from the controller was seen. |
| Other | A lone 2-byte `a5a5` appeared when leaving registration mode (3×). Probably harmless line noise or a mode-change marker. |
| Lost thermostat | Pulse manual §3.5: the channel is **not** closed. The controller runs a heating fallback that opens the actuators at fixed intervals until the thermostat comes back. A virtual thermostat therefore must keep answering (reporting no demand); going quiet doesn't close the channel. |

### What this means for a virtual thermostat

- **The message format is simple.** The reply is four fields. The controller does the regulation from the room temperature we report. Registration is three fixed frames from a new device ID, and the only thermostat-specific value in them is `3F 0C03`.
- **Result: over the EW41B's MQTT path, Home Assistant answers 20–30× too slowly.** A median of 218 ms against a ~10 ms reply window and a ~200 ms slot means a reply would usually land in the next thermostat's slot and collide with its poll. Answering polls as they arrive is not viable on this hardware. What's left is in "Options" below.
- **Timing is the problem.** A real thermostat replies within ~10 ms of the poll, and the controller sends its own frame ~30 ms after the poll. Our path is: poll → EW41B gap (10 ms) → Wi-Fi → MQTT broker → HA → broker → Wi-Fi → EW41B → bus. That very likely takes longer than 30 ms. The open questions:
  1. What is our real round trip? To measure it, inject an `FF` poll to a real thermostat during the 9 s idle window and time its reply on the read topic.
  2. Does the controller accept a reply that arrives later in the slot (between its own frame at ~30 ms and the next poll at ~200 ms)? And does it accept registration frames that come long after a broadcast? It sends a broadcast every 200 ms, so retries are cheap. Testing this means registering a virtual ID on the **free** channel, which is harmless while no actuator is fitted.

## Virtual thermostat test (2026-10-26)

- **Registration works over the TCP socket**, with the EW41B gap time left at 10 ms. The first attempt sent FA/FB/FC in a single TCP write, using ID `4F01`. The controller heard it, rebroadcast ~30 ms after each answer instead of every 200 ms, and never accepted it. What succeeded: the three frames sent **3 ms apart**, ID `42F0`, at most 25 answers per attempt. The likely cause is that frames sent back to back look like one invalid frame. The ID change can't be ruled out as a factor.
- **Polling works.** The controller polls `42F0` every 10 s in its own slot. HA answers 1–4 ms after the poll reaches it (about 25–35 ms after the poll on the bus). For `42F0` the controller sends its follow-up frame **~60 ms** after the poll, against ~30 ms for a real T-148, so it waits for the slower reply. It adopted the virtual thermostat's setpoint (`3B`).

## Options after the latency result

1. ~~Re-enable the EW41B's raw TCP socket (8899) and measure again.~~ Done: ~45 ms round trip, borderline rather than hopeless. It skips the gateway's MQTT client, and Wi-Fi is only ~5 ms, but the 10 ms UART gap stays. It's only worth it if the socket is firewalled to the HA host, because it is unauthenticated.
2. **Test whether the controller tolerates late replies** by registering a virtual ID on the free channel (harmless while no actuator is fitted). If a new ID is polled last in the cycle, a late reply lands in the ~9 s idle period rather than in another thermostat's slot. It's unknown whether the controller times out a reply after a few ms or accepts it later.
3. **A bus device that answers locally** (ESP32 + RS485), the reliable route, is ruled out by the no-new-hardware constraint.

## Decision gate

- Our round trip fits within what the controller tolerates → implement replies in the integration.
- It's too slow but the poll cycle is regular → try replies scheduled from the measured cycle.
- The registration needs data we can't reproduce, or replies can't be made reliable → stop. Control the actuator from Home Assistant without the controller instead.
