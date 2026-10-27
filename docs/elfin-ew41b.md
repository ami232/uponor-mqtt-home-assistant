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

- **The `netp` socket.** Delete it under **Communication Settings** unless you use the [virtual thermostat](#raw-tcp-socket-for-the-virtual-thermostat). It's a TCP server on port 8899 that gives unauthenticated raw access to the bus.
- **The built-in access point.** Once the gateway is on your network, set **WiFi mode** to **STA** so the access point is off. If you keep it as a fallback, give it a WPA2 password.
- **Telnet.** Keep it disabled under **System Settings**.

### Give the gateway its own MQTT user

Don't reuse your own MQTT login: the gateway stores it in plain text. With the Mosquitto add-on, add a login under the add-on's **Configuration → Logins** (for example `elfin`), restart the add-on, and enter the login in the gateway's MQTT socket.

To limit that user to the two bus topics, use an access control list (ACL). The add-on loads every `.conf` file in `/share/mosquitto` as broker configuration, so it takes two files: a `.conf` file that points to the ACL, and the ACL itself.

The add-on doesn't create `/share/mosquitto`, so create it first. From the Terminal & SSH add-on it's `/share`. Over the Samba add-on it's the `share` network share. The File editor only shows your config folder unless you set `enforce_basepath: false`.

`/share/mosquitto/acl.conf`:

```
acl_file /share/mosquitto/accesscontrollist
```

`/share/mosquitto/accesscontrollist`:

```
user homeassistant
topic readwrite #

user addons
topic readwrite #

user elfin
topic write uponor_read
topic read uponor_write
```

Then set `customize: active: true` (folder `mosquitto`) in the add-on configuration and restart it. Don't put the ACL rules in the `.conf` file itself, or the broker won't start.

Once the ACL is active, logins it doesn't list can still connect but get no access to any topic, and the broker reports no error. Add a `topic readwrite #` entry for each of your other MQTT clients (Node-RED, Zigbee2MQTT, ESPHome devices and so on) before enabling it. Their logins show up in the add-on log as `New client connected ... u'<user>'`.

### Isolate it on the network

- Give the gateway a fixed IP with a DHCP reservation.
- Put it on an IoT VLAN or guest network that can reach only the broker (port 1883), with no internet access.

### Check what's exposed

```
nmap -p 1-1024,8899 <gateway IP>
```

Only port 80 (the web UI) should be open.

## Raw TCP socket for the virtual thermostat

The [virtual thermostat](../README.md#virtual-thermostat-experimental) has to answer the controller faster than the gateway's MQTT client can. Under **Communication Settings → +Add**, add:

| Setting | Value |
| --- | --- |
| Name | e.g. `netp` |
| Protocol | Tcp Server |
| Local port | 8899 |
| Buffer size / Keep alive / Timeout | 512 / 60 / 0 |
| Max accept | 1 |
| Security / Route | Disable / Uart |

Leave the serial gap time at 10 ms. It works, and it keeps MQTT messages aligned with bus frames.

This socket has no authentication. Anyone who can reach it can read the bus and send raw bytes to it. **Max accept 1 doesn't make it exclusive:** in testing, the gateway still accepted a second client while Home Assistant was connected. Keep the gateway on a network segment that only Home Assistant can reach, or firewall port 8899 to the Home Assistant host.
