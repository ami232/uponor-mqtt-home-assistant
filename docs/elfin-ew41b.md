# Gateway setup: Elfin EW41B

The [Elfin EW41B](http://www.hi-flying.com/elfin-iot/elfin-ew40-ew41-ew42) is a small RS485-to-Wi-Fi converter with a built-in MQTT client. It's the reference gateway for this integration. Any gateway that passes raw bus bytes to and from MQTT works the same way.

## Wiring

Connect the EW41B's RS485 **A/B** terminals to the bus terminals on the Uponor Smatrix controller (A to A, B to B). Power the EW41B from its supported supply range (see its datasheet). If the gateway receives nothing, swap A and B.

## Serial settings

Open the EW41B web UI. Out of the box the gateway runs its own Wi-Fi access point, with the web UI at `10.10.100.254` and the login `admin` / `admin`. Once it's on your network, use the IP your router gives it. Under **Serial Port Settings**:

| Setting | Value |
| --- | --- |
| Baud rate | 19200 |
| Data bits / parity / stop bits | 8 / None / 1 |
| Flow control | Half duplex (RS485 direction control) |
| Buffer size | 256 |
| Gap time | 10 ms |
| Protocol | None (transparent) |

The 10 ms gap time makes the EW41B publish each bus frame as soon as the line goes quiet, so most MQTT messages contain a single frame. The integration also copes with several frames in one message, because it finds frame boundaries using the CRC.

## MQTT settings

Under **Communication Settings**, add a socket with protocol **MQTT** that points at the same broker Home Assistant uses:

| Setting | Value |
| --- | --- |
| Server / port | your broker (e.g. the Mosquitto add-on, port 1883) |
| MQTT version | 3.1 (the default; 3.1.1 also works with Mosquitto) |
| Client ID | any unique name (e.g. `Elfin`) |
| Username / password | a dedicated MQTT user for the gateway (see [below](#give-the-gateway-its-own-mqtt-user)) |
| Publish topic / QoS | `uponor_read` / 0 |
| Subscribe topic / QoS | `uponor_write` / 0 |
| Route | `uart` |
| Connect mode | Always |

After you save, the gateway reconnects to the broker with the new settings. You don't need to restart it.

If you use other topic names, enter them when you set up the integration.

## Securing the gateway

Anyone who can reach the gateway, or publish to the write topic, can send raw bytes to your heating bus.

### Change the admin password

The web UI is plain HTTP. It shows the MQTT password in clear text, and its config API returns it in plain text too. Anyone with the admin login also has the gateway's MQTT credentials.

### Close what you don't use

- **The `netp` socket.** Delete it under **Communication Settings** if you only use MQTT. It's a TCP server on port 8899 that gives unauthenticated raw access to the bus.
- **The built-in access point.** Once the gateway is on your network, set **WiFi mode** to **STA** so the access point is off. If you keep it as a fallback, give it a WPA2 password.
- **Telnet.** Keep it disabled under **System Settings**.

### Give the gateway its own MQTT user

Don't reuse your own MQTT login: the gateway stores it in plain text. With the Mosquitto add-on, add a login under the add-on's **Configuration → Logins** (for example `elfin`), restart the add-on, and enter the login in the gateway's MQTT socket.

To limit that user to the two bus topics, set `customize: active: true` in the add-on configuration and add `/share/mosquitto/acl.conf`:

```
user elfin
topic write uponor_read
topic read uponor_write

user homeassistant
topic readwrite #

user addons
topic readwrite #
```

Once an ACL file is active, users it doesn't list get no access. Add your other MQTT clients (Zigbee2MQTT, ESPHome devices and so on) before enabling it.

### Isolate it on the network

- Give the gateway a fixed IP with a DHCP reservation.
- Put it on an IoT VLAN or guest network that can reach only the broker (port 1883), with no internet access.

### Check what's exposed

```
nmap -p 1-1024,8899 <gateway IP>
```

Only port 80 (the web UI) should be open.
