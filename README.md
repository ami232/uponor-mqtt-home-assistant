# Uponor MQTT Bridge – Home Assistant custom integration

Connects Uponor Smatrix floor-heating thermostats to Home Assistant through MQTT. Each thermostat shows up as a `climate` entity, with its current temperature, target temperature, humidity, heating/cooling mode, ECO preset and action, plus separate sensors for its readings. Target temperatures set in Home Assistant are sent back to the thermostats.

The integration also keeps the thermostat clock in sync with Home Assistant's time zone, including DST changes.

### ESPHome or this integration?

[ESPHome's `uponor_smatrix` component](https://esphome.io/components/uponor_smatrix/) does the same job on an ESP32/ESP8266 wired to the bus. This integration follows the same protocol handling and offers the same entities. It's meant for people who already have, or prefer, an off-the-shelf RS485-to-MQTT gateway and don't want to flash an ESP.

| | ESPHome `uponor_smatrix` | This integration |
| --- | --- | --- |
| Hardware | ESP + RS485-to-TTL converter | Any transparent RS485-to-MQTT gateway |
| Thermostats | Addresses listed in YAML | Discovered automatically |
| Cooling | Mode and action only; setpoint is off by the heating/cooling offset while cooling ([esphome#10924](https://github.com/esphome/esphome/issues/10924)) | Mode, action and correct setpoint |
| ECO preset, min/max setpoint, clock sync | Yes | Yes |

## How it works

```
Uponor Smatrix controller ──RS485 bus──► RS485-to-Wi-Fi gateway ──MQTT──► Home Assistant
   (thermostats on the bus)               (e.g. Elfin EW41B)                (this integration)

  bus bytes  ──► read topic  (default: uponor_read)  ──► parsed → climate entities + sensors (MQTT discovery)
  bus bytes  ◄── write topic (default: uponor_write) ◄── target temperature / clock sync
```

The integration never talks to the bus directly. It needs a gateway that:

1. publishes the **raw bytes** it receives from the RS485 bus to an MQTT topic, and
2. writes the raw bytes it receives on another MQTT topic to the bus.

Any gateway that does this works. If you use other topic names than the defaults, enter them when you set up the integration.

## Gateway setup

- **Elfin EW41B** (reference setup): see [docs/elfin-ew41b.md](docs/elfin-ew41b.md) for wiring, serial and MQTT settings, and how to secure the gateway.

Whatever gateway you use, give it its own MQTT user and keep it off networks it doesn't need. Anyone who can reach it, or publish to the write topic, can send raw bytes to your heating bus.

## Installation

### HACS (recommended)

1. In Home Assistant, open **HACS**, use the three-dot menu and choose **Custom repositories**.
2. Add `https://github.com/ami232/uponor-mqtt-home-assistant` with the category **Integration**.
3. Search for **Uponor MQTT Bridge**, install it and restart Home Assistant.

### Manual

Copy `custom_components/uponor_mqtt` into your Home Assistant `config/custom_components/` folder and restart.

### Requirements

- Home Assistant 2025.10 or newer.
- The [MQTT integration](https://www.home-assistant.io/integrations/mqtt/) set up and connected to the same broker as your gateway.

## Configuration

Go to **Settings → Devices & Services → Add Integration** and choose **Uponor MQTT Bridge**.

| Field | Default | Description |
| --- | --- | --- |
| Name | `Uponor MQTT Bridge` | Name of the integration entry. |
| Read topic | `uponor_read` | Topic where the gateway publishes raw bus bytes. |
| Write topic | `uponor_write` | Topic the gateway subscribes to and writes to the bus. |
| MQTT discovery prefix | `homeassistant` | Change it only if your MQTT integration uses a different prefix. |
| System ID | *(blank)* | Your controller's 2-byte bus address in hex (e.g. `110B`). Leave it blank and the integration detects it from bus traffic. |

You can change all of these later with **Configure** on the integration. The integration reloads when you save.

### System ID

Every Uponor controller has its own system ID, and every bus frame starts with it. When you leave the field blank, the integration takes the ID from the first valid frame it receives and logs it:

```
Detected Uponor system ID 0x110B
```

Until the ID is known, temperature changes for thermostats that haven't sent any data yet are rejected. They are never sent to a guessed address. If you want commands to work right after a restart, set the ID explicitly.

## Entities

Each thermostat gets a device with a climate entity named `climate.uponor_<device id>` (for example `climate.uponor_00a1`). They're created through MQTT discovery under `<discovery prefix>/climate/uponor_<ID>/…`. A thermostat appears once it reports its room temperature on the bus. Other bus devices, such as the controller itself, get no entities. The device's serial number is the full 32-bit bus address (system ID + device ID, e.g. `110BDE13`), the same address ESPHome uses.

The climate entity shows:

- **Target temperature** as displayed on the thermostat. While cooling, the thermostat adds the controller's heating/cooling offset to the setpoint it reports on the bus. In ECO it subtracts the ECO setback when heating and adds it when cooling. The integration applies the same offsets, so Home Assistant and the thermostat agree. Setting a temperature reverses them.
- **Mode** (`heat` / `cool`) and **ECO preset**, both read-only. Heating/cooling is switched at the controller and ECO by its schedule. If you change them in Home Assistant, the change is logged and reverted.
- **Action** (heating, cooling or idle), **current temperature** and **humidity**.
- **Min/max setpoint**, taken from the thermostat's limits.

Each thermostat also gets these sensors, created once it reports the value:

| Sensor | Entity |
| --- | --- |
| Room temperature | `sensor.uponor_<id>_current_temperature` |
| External (floor/outdoor) temperature | `sensor.uponor_<id>_external_temperature` |
| Humidity | `sensor.uponor_<id>_current_humidity` |
| Target temperature | `sensor.uponor_<id>_temperature` |

## Virtual thermostat (experimental)

The controller only opens a channel when the thermostat registered to it asks for heat. A virtual thermostat lets Home Assistant be that thermostat. It registers on the bus like a T-148 and reports the temperature of any Home Assistant sensor, for example a Zigbee thermometer in a room that has no Uponor thermostat. The controller then switches the channel's actuators itself, using that temperature and the setpoint you set in Home Assistant.

It has been tested on a Smatrix Base Pulse X-245 with T-148 thermostats and an Elfin EW41B. **Heat only.** While the controller is in cooling mode, and whenever the sensor is unavailable or hasn't reported for 30 minutes, it reports a temperature that doesn't ask for heat.

**Why it needs a TCP socket.** The controller expects an answer within a few tens of milliseconds of polling a thermostat. Through the EW41B's MQTT client the round trip measured 100–270 ms, which is too slow. Through its raw TCP socket it measured about 45 ms, which the controller accepts. The virtual thermostat therefore talks to the gateway over TCP; everything else stays on MQTT.

### Setup

1. On the gateway, add a **Tcp Server** socket on port 8899 with route **Uart**. The gateway guide has [the details and the security trade-off](docs/elfin-ew41b.md#raw-tcp-socket-for-the-virtual-thermostat).
2. Under **Configure**, fill in the virtual thermostat fields:
   - the gateway's IP address and TCP port
   - an **unused** 4-digit hex device ID, e.g. `42F0`; check it isn't one of your thermostats
   - the room temperature sensor, and optionally a humidity sensor
3. Register it like a real thermostat. On the controller, hold **OK** until a channel LED flashes red, move to the channel with **< / >** and press **OK** (the LED flashes green). Within a minute, run the action `uponor_mqtt.register_virtual_thermostat`. The channel LED turns steady green when registration succeeds. Hold **OK** to leave registration mode.

The virtual thermostat then appears as `climate.uponor_<device id>`, like the real ones. Its setpoint is stored in Home Assistant and survives restarts.

**If Home Assistant stops answering**, during a restart or with `uponor_mqtt.stop_virtual_thermostat`, the controller treats the thermostat as lost. As with a real thermostat, it doesn't close the channel; it runs its heating fallback until the virtual thermostat answers again. `uponor_mqtt.start_virtual_thermostat` resumes answering. To remove it, unregister the channel on the controller and clear the fields under **Configure**.

**Measuring latency.** `uponor_mqtt.measure_latency` times poll → reply round trips to a real thermostat through MQTT. It transmits on the bus, but only the controller's own poll and only while the bus is idle.

## Troubleshooting

**No entities appear**

- Check that bus data reaches the broker:
  ```
  mosquitto_sub -h <broker> -u <user> -P <pass> -t uponor_read -F %x
  ```
  You should see hex frames that all start with the same two bytes (your system ID).
- If nothing arrives, check the wiring (try swapping A/B), the serial settings and the gateway's MQTT settings (see [the gateway guide](docs/elfin-ew41b.md)).
- Frames that fail the CRC check are dropped. A lot of them usually means a wrong baud rate.

**Temperature changes do nothing**

- Check that the gateway is subscribed to the write topic and has Wi-Fi/MQTT connectivity.
- Look for `Cannot set temperature: Uponor system ID not known yet` in the log (see [System ID](#system-id)).

**Debug logging**

```yaml
logger:
  logs:
    custom_components.uponor_mqtt: debug
```

**Raw bus capture**

For protocol research, turn on **Capture raw bus traffic** under **Configure**. Every payload received from the gateway, and every frame the integration writes, is appended with a timestamp to `<config>/uponor_capture.jsonl`. The `uponor_mqtt.capture_note` action adds a marker line. Analyze the file with `tools/analyze_capture.py` (see [docs/bus-protocol-notes.md](docs/bus-protocol-notes.md)). Turn it off afterwards, because the file keeps growing.

## Limitations

- One Uponor controller (one system ID) per Home Assistant instance.
- Only target temperature can be set. Mode (heat/cool) and the ECO preset are read-only.
- Temperatures are in °C.

## Upgrading to 1.2.0

- **New sensors.** Each thermostat gets room temperature, external temperature, humidity and target temperature sensors.
- **Mode and ECO are read-only.** Changes made in Home Assistant used to look like they worked. Now they're logged and reverted.
- **Only thermostats get entities.** Devices that never report a room temperature, such as the controller, no longer get a climate entity. If an older version created one, its discovery message is still retained on the broker and the entity stays. Delete it from the device page (**Settings → Devices & Services → MQTT**).

## Upgrading from 1.0.x

- **Manual MQTT broker mode has been removed.** The integration now always uses Home Assistant's MQTT integration. Existing entries keep working and log a warning.
- **Existing entries use the default topics** (`uponor_read` / `uponor_write`) and auto-detect the system ID. To change this, use **Configure**.

## Development

The protocol tests don't need Home Assistant:

```
pip install pytest
pytest tests/
```
