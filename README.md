# Uponor MQTT Bridge (Home Assistant custom integration)

Home Assistant custom integration that bridges Uponor Smatrix floor-heating thermostats to MQTT and exposes them as climate entities.

Features
- Publishes thermostat state via MQTT discovery and also exposes native `climate` entities.
- Supports two modes:
  - Use Home Assistant's `mqtt` integration (default).
  - Manual MQTT broker managed by the integration (via `paho-mqtt`).
- Time synchronization to the bus when a time-master thermostat is detected.

Topics used
- `uponor_read` (binary): input topic with raw Uponor packet bytes. The integration
  subscribes without forcing UTF-8 decoding so binary payloads are delivered as
  raw bytes to the bridge callback.
- `uponor_write` (binary): output topic where the integration publishes command packets.
- `homeassistant/climate/uponor_<ID>/...`: MQTT discovery and state topics (discovery prefix configurable).

Installation
1. Copy the `uponor_mqtt` folder into your Home Assistant `custom_components` directory so you have:

   `config/custom_components/uponor_mqtt/`

   (The repository already contains the integration files under `custom_components/uponor_mqtt`.)

2. Restart Home Assistant.

3. Configure the integration via UI: Settings → Devices & Services → Add Integration → search `Uponor MQTT Bridge`.

Configuration (Config Flow)
When adding the integration you will see a form with the following options:

- Name: Friendly name for the integration (default: "Uponor MQTT Bridge").
- Use core MQTT: If checked (default) the integration will use Home Assistant's `mqtt` integration and the configured broker. Leave this checked if you already have the `MQTT` integration set up.
- Discovery prefix: MQTT discovery prefix (default: `homeassistant`). Set this if your broker uses a different prefix.

Manual broker (optional)
If you uncheck `Use core MQTT` the flow will ask for manual broker settings that the integration will use directly:

- Host, Port, Username, Password - the integration will create a `paho-mqtt` client and manage its own connection.

Notes about modes
- Preferred mode: use Home Assistant's `mqtt` integration. That allows a single broker configuration and avoids duplicate broker clients.
- Manual mode is useful when you want the integration to manage a dedicated connection or when Home Assistant's core MQTT is not available.

Using the entities
- After discovery the integration creates `climate` entities like `climate.uponor_00A1`.
- You can set target temperature from the UI; entities call into the bridge which builds the proper Uponor bus packet and publishes it to `uponor_write`.

Developer notes
- Protocol helpers are bundled in `custom_components/uponor_mqtt/uponor_protocol.py`.
- Core bridge logic is in `custom_components/uponor_mqtt/bridge.py`.
- The climate entity implementation is in `custom_components/uponor_mqtt/climate.py` and uses `DataUpdateCoordinator`.

Project layout
- `custom_components/uponor_mqtt/` — integration code (bridge, protocol helpers, entities).
- `uponor_devices.json` — optional device metadata (if present).
- `uponor-mqtt/` — standalone Python helper and Docker compose for running a translator outside HA (kept for reference).

Notes
- This repository contains the Home Assistant custom integration; installation copies
  `custom_components/uponor_mqtt` into your HA `config/custom_components` directory.
- If you want, I can prepare a HACS package metadata update or add example scripts
  for generating test packets.

Troubleshooting
- If entities do not appear: verify that `uponor_read` messages are arriving on the broker and contain valid packets. The bridge uses CRC checking and will ignore invalid packets.
- Check Home Assistant logs for messages from the integration (logger name: `custom_components.uponor_mqtt`).
- If using manual broker mode, ensure the `paho-mqtt` dependency is installed by Home Assistant (it will be requested from the manifest). If the integration cannot import `paho`, it will log the failure and attempt to fall back to HA MQTT helper if available.

Testing
- Send sample packets to `uponor_read` on the broker (binary payloads). The original repository contains tooling and examples for building command packets in `uponor_protocol.py`.

License and contributions
- This project is provided as-is. If you want me to help polish this integration (add tests, CI, HACS metadata), say which task you'd like next.
