# Uponor MQTT Bridge - Home Assistant custom integration

Small integration that bridges Uponor Smatrix floor-heating thermostats to MQTT and to native Home Assistant `climate` entities.

# Install via HACS and setup

Prerequisites
- Home Assistant with HACS (Home Assistant Community Store) already installed and configured.
- Your MQTT broker configured in Home Assistant (recommended) or credentials available if you plan to use the integration's manual broker mode.

Add and install the integration via HACS

1. Open Home Assistant and go to **HACS → Integrations**.
2. If this repository is already published to GitHub and available in HACS, search for "Uponor MQTT Bridge" and click **Install**.
3. If the repository is not in HACS core listings, add it as a custom repository:
  - In HACS go to the three‑dot menu (top right) → **Custom repositories**.
  - Add the repository URL (example): `https://github.com/ami232/uponor-mqtt-home-assistant`.
  - Set **Category** to **Integration**, then click **Add**.
  - Return to **HACS → Integrations**, search for the integration and click **Install**.
4. After installation, restart Home Assistant when prompted.

Add the integration in Home Assistant

1. Go to **Settings → Devices & Services → Add Integration**.
2. Search for "Uponor MQTT Bridge" and follow the configuration flow.

Configuration options (UI)
- **Name**: Friendly name for the integration (default: "Uponor MQTT Bridge").
- **Use core MQTT**: Recommended (checked by default). When enabled the integration will use Home Assistant's `mqtt` integration and its configured broker.
- **Discovery prefix**: Defaults to `homeassistant`. Change only if your broker uses a different discovery prefix.

Manual broker mode

If you uncheck **Use core MQTT**, the integration will ask for manual broker details. Provide:
- Host
- Port
- Username
- Password

The integration will create and manage a `paho-mqtt` client in this mode. Using Home Assistant's `mqtt` integration is recommended to avoid duplicate clients.

Verify installation

- After setup the integration will create `climate` entities for detected thermostats (for example `climate.uponor_00A1`).
- Check the Home Assistant logs for `custom_components.uponor_mqtt` messages if entities do not appear.

Troubleshooting (brief)
- If entities do not appear, confirm `uponor_read` packets are arriving at your MQTT broker (the integration subscribes to raw/binary payloads).
- If manual broker mode fails to connect, ensure the `paho-mqtt` dependency is present for Home Assistant and that credentials are correct.

**Topics & Payloads**

- `uponor_read` (binary): Input topic for raw Uponor packet bytes. The integration subscribes without forcing UTF‑8 decoding so it receives binary payloads.
- `uponor_write` (binary): Output topic where the integration publishes command packets to be sent to the bus.
- `homeassistant/climate/uponor_<ID>/...`: MQTT discovery and state/control topics created by the integration using the configured discovery prefix.

You normally do not need to publish to these topics manually - the integration handles reading and writing packets for discovered devices.

**Using the discovered entities**

- After discovery the integration creates `climate` entities (for example `climate.uponor_00A1`).
- Use the Home Assistant UI or automations to set target temperatures; the integration translates those actions into Uponor bus command packets and publishes them to `uponor_write`.

**Troubleshooting**

- Entities don't appear: verify that `uponor_read` messages are arriving on your MQTT broker and contain valid Uponor packets. The bridge validates packets (CRC) and will ignore invalid frames.
- Check Home Assistant logs for messages from `custom_components.uponor_mqtt` for hints and errors.
- If using manual broker mode, ensure the `paho-mqtt` Python package is available to Home Assistant (the integration manifest requests it). If `paho` cannot be imported the integration will log the failure and attempt to fall back to the HA MQTT helper if available.
