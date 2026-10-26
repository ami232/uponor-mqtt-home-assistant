# Uponor MQTT Bridge – Home Assistant custom integration

Connects Uponor Smatrix floor-heating thermostats to Home Assistant through MQTT. Each thermostat shows up as a `climate` entity, with its current temperature, target temperature, humidity, heating/cooling mode and action. Target temperatures set in Home Assistant are sent back to the thermostats.

The integration also keeps the thermostat clock in sync with Home Assistant's time zone, including DST changes.

## How it works

```
Uponor Smatrix controller ──RS485 bus──► RS485-to-Wi-Fi gateway ──MQTT──► Home Assistant
   (thermostats on the bus)               (e.g. Elfin EW41B)                (this integration)

  bus bytes  ──► read topic  (default: uponor_read)  ──► parsed → climate entities (MQTT discovery)
  bus bytes  ◄── write topic (default: uponor_write) ◄── target temperature / clock sync
```

The integration never talks to the bus directly. It needs a gateway that:

1. publishes the **raw bytes** it receives from the RS485 bus to an MQTT topic, and
2. writes the raw bytes it receives on another MQTT topic to the bus.

Any gateway that does this works. The setup below uses the Elfin EW41B.

## Hardware: Elfin EW41B (reference setup)

The [Elfin EW41B](http://www.hi-flying.com/) is a small RS485-to-Wi-Fi converter with a built-in MQTT client.

### Wiring

Connect the EW41B's RS485 **A/B** terminals to the bus terminals on the Uponor Smatrix controller (A to A, B to B). Power the EW41B from its supported supply range (see its datasheet). If the gateway receives nothing, swap A and B.

### Serial settings

Open the EW41B web UI. Out of the box it runs its own Wi-Fi access point at `10.10.100.254`, with the login `admin` / `admin`. Once it's on your network, use the IP your router gives it. Under **Serial Port Settings**:

| Setting | Value |
| --- | --- |
| Baud rate | 19200 |
| Data bits / parity / stop bits | 8 / None / 1 |
| Flow control | Half duplex (RS485 direction control) |
| Buffer size | 256 |
| Gap time | 10 ms |
| Protocol | None (transparent) |

### MQTT settings

Under **Communication Settings**, add a socket with protocol **MQTT** that points at the same broker Home Assistant uses:

| Setting | Value |
| --- | --- |
| Server / port | your broker (e.g. the Mosquitto add-on, port 1883) |
| MQTT version | 3.1 (the default; 3.1.1 also works with Mosquitto) |
| Client ID | any unique name (e.g. `Elfin`) |
| Username / password | an MQTT user on the broker |
| Publish topic / QoS | `uponor_read` / 0 |
| Subscribe topic / QoS | `uponor_write` / 0 |
| Route | `uart` |
| Connect mode | Always |

The 10 ms gap time makes the EW41B publish each bus frame as soon as the line goes quiet, so most MQTT messages contain a single frame. The integration also copes with several frames in one message, because it finds frame boundaries using the CRC.

Change the default `admin` password on the web UI. The page shows the MQTT password in plain text.

If you use other topic names, enter them when you set up the integration.

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

Each thermostat seen on the bus gets a climate entity named `climate.uponor_<device id>` (for example `climate.uponor_00a1`). It's created through MQTT discovery under `<discovery prefix>/climate/uponor_<ID>/…`. A thermostat appears once it has sent its first frame on the bus.

## Troubleshooting

**No entities appear**

- Check that bus data reaches the broker:
  ```
  mosquitto_sub -h <broker> -u <user> -P <pass> -t uponor_read -F %x
  ```
  You should see hex frames that all start with the same two bytes (your system ID).
- If nothing arrives, check the wiring (try swapping A/B), the serial settings and the gateway's MQTT settings.
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

## Limitations

- One Uponor controller (one system ID) per Home Assistant instance.
- Only target temperature can be set. Mode (heat/cool) and eco presets are read-only.
- Temperatures are in °C.

## Upgrading from 1.0.x

- **Manual MQTT broker mode has been removed.** The integration now always uses Home Assistant's MQTT integration. Existing entries keep working and log a warning.
- **Existing entries use the default topics** (`uponor_read` / `uponor_write`) and auto-detect the system ID. To change this, use **Configure**.

## Development

The protocol tests don't need Home Assistant:

```
pip install pytest
pytest tests/
```
