# PiDP-11 Infrastructure Status Panel

Turn a [PiDP-11](https://obsolescence.wixsite.com/obsolescence/pidp-11) PDP-11/70
front-panel replica into a live network health board. Instead of emulating the
PDP-11, every LED on the panel represents one system on your network.

![The status panel polling systems: each flash is a health check](docs/demo.gif)

* While a system is being polled its LED lights (at least 200 ms, longer if
  the poll is slow). Polling is fast and slightly randomised, so the panel
  flickers like a busy minicomputer, but every flicker means something.
* A healthy result turns the LED off until the next poll.
* An unhealthy system makes its LED **blink fast** until it recovers.
* Status, history and a live map of the panel are served on a built-in web
  page, where checks can be added and edited.
* Everything is published to **Home Assistant** via MQTT discovery.

An operator who notices a blinking LED can check a printed map, open the web
page, or look in Home Assistant.

## Panel conventions

| Indicator | Meaning |
|---|---|
| Brief flash | Poll in progress |
| Fast blink | That system is unhealthy |
| RUN steady | Monitor software running |
| ADRS ERR steady | At least one system is down |
| Whole panel flashing slowly | Monitor software has stopped (watchdog in `ledmux`) |
| LAMP TEST switch | Light every LED |
| START switch | Poll everything now |

The indicator LEDs and switches are configurable.

## Hardware

* A Raspberry Pi with the PiDP-11 board. Tested on a Pi 4 running Raspberry Pi OS (Debian 11).
* That's all. The PiDP-11 emulator software is not used. The installer
  disables it because both programs drive the same GPIO pins.

64 LEDs are usable: address 0–21, data 0–15, the 12 status lights, parity
high/low and the 12 rotary-knob indicators.

## Install

```sh
git clone https://github.com/<you>/pidp11-statuspanel.git
cd pidp11-statuspanel
sudo ./install.sh
```

Then open `http://<pi-hostname>.local/`, click **Edit**, and replace the
example checks with your own. The installer creates
`/opt/statuspanel/config.json` from `statuspanel/config.example.json` the
first time it runs, and never overwrites it afterwards.

## Configuring checks

All configuration lives in one JSON file, `/opt/statuspanel/config.json`.
Normally you edit it from the web page:

* **Edit → + Add system**, or click any free LED on the map.
* Pick a check type, fill in the target, press **Test** to try it, then **Save**.
  Changes take effect immediately, without a restart, and each system's history is kept.
* **Settings** covers polling defaults, blink rate, indicator LEDs, Home Assistant and the edit password.

| Type | Healthy when | Fields |
|---|---|---|
| `ping` | host answers ICMP echo | `host` |
| `tcp` | port accepts a connection | `host`, `port` |
| `http` | status code in range (and optional body text present) | `url`, `expect_status` (e.g. `200-399`), `contains`, `verify_tls` |
| `dns` | name resolves | `query`, `server` (blank = system resolver) |
| `json` | a field equals one of the given values, or a counter keeps increasing | `url`, `field` (dotted path), `equals`, `increasing`, `stale_s`, `detail_field` |
| `agw` | an AGWPE server (Direwolf, soundmodem) answers a version request | `host`, `port` |
| `command` | shell command exits 0 | `command` |

Every check also takes `name`, `led`, and optional `interval`, `timeout`,
`fail_after` (consecutive failures before the system counts as down),
`location` and `group`.

Examples:

* **PiAware / FlightAware:** `json` on `http://piaware/status.json` with field
  `radio.status` (or `piaware`, `adept`, `mlat`) equal to `green`. Use
  `increasing` on `messages` in `:8080/data/aircraft.json` to prove the
  receiver is still decoding.
* **Security cameras:** `tcp` on port 554 (RTSP).
* **Direwolf:** `agw` on port 8000. Poll it every 10 s or so, because Direwolf logs every client connection.

LED names: `A0`–`A21`, `D0`–`D15`, `PAR_ERR ADRS_ERR RUN PAUSE MASTER USER
SUPER KERNEL DATA ADDR16 ADDR18 ADDR22`, `PAR_HIGH PAR_LOW`, `USER_D SUPER_D
KERNEL_D CONS_PHY PROG_PHY KERNEL_I SUPER_I USER_I`, `DATA_PATHS BUS_REG UADRS
DISP_REG`, or a raw matrix position like `r2c9`.

### Security

The web page is unauthenticated for viewing. To protect editing, set an
**edit password** in Settings. `command` checks run shell commands, so they
can only be added in `config.json` unless you set
`"allow_command_checks_from_web": true`. `config.json` contains your network
layout and any MQTT password: it is git-ignored and stored with mode 600.

Avoid fast polling of SSH ports. It floods auth logs and can trip intrusion
detection.

## Home Assistant

Enable it in **Settings → Home Assistant** with your MQTT broker's details
(for example the Mosquitto add-on and an HA user). This creates a device,
*Infrastructure Status Panel*, containing:

* `binary_sensor.infra_<id>`, one per check (device class *problem*; on = unhealthy, with
  target, latency and detail as attributes)
* `sensor.infra_<id>_latency`
* `binary_sensor.infrastructure_problem` and `sensor.infrastructure_systems_down`

Healthy results are published at most once a minute (`history_sample_s`).
Status changes are published immediately.

## How it works

| Part | Role |
|---|---|
| `ledmux/ledmux.c` | Small C program that runs at real-time priority. It multiplexes the 6×12 LED matrix and scans the switches, sharing state through `/dev/shm/statuspanel`. If the Python side stops updating, it flashes the whole panel. |
| `statuspanel/statuspanel.py` | Health checks, LED behaviour, SQLite history (`/var/lib/statuspanel/history.db`), web UI and JSON API, MQTT. Uses only the standard library, plus `paho-mqtt` for Home Assistant. |
| `statuspanel/web/index.html` | Single-page status and editor UI. |

JSON API: `GET /api/status`, `GET /api/events?limit=N`,
`GET /api/history?id=<id>&hours=24`, `GET /api/config`, `POST /api/poll[?id=]`,
and `POST /api/config` and `POST /api/test` (both need the `X-Edit-Password` header if a password is set).

Logs: `journalctl -u statuspanel -f`, `journalctl -u ledmux`.

## Going back to the PDP-11 emulator

```sh
sudo systemctl disable --now statuspanel ledmux
sudo update-rc.d pidp11 enable && sudo /etc/init.d/pidp11 start
```

## Credits

The PiDP-11 hardware and the original GPIO multiplexing scheme are by Oscar
Vermeulen (obsolescence.wixsite.com), with the Blinkenlight API by Jörg Hoppe.
This project uses only their pin assignments and timing; the code is new.

## License

MIT; see [LICENSE](LICENSE).
