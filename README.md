# Auto-Blox

A dashboard, automation studio and ESP32 fleet manager for the
**Orange Pi Zero 2W**, in plain Python with no dependencies. Built to live in
a 3D-printed box, with its ESP32 field devices boxed up beside it.

- **Dashboard** — thermal, cores, memory, storage, network, the live journal,
  top processes and a terminal, on one screen, without a click.
- **Flow Studio** — a node editor for the board's GPIO, PWM, I2C and SPI,
  MQTT, webhooks, timers, shared tags and Bluetooth devices.
- **IoT** — the board runs its own Wi-Fi access point for ESP32 boards, flashes
  them over USB, and deploys flows that then run from the ESP32's own flash,
  with or without the console.
- **Cameras** — an ESP32-CAM streams JPEG to the console over TLS.
- **Bluetooth** — pair controllers and read, write or subscribe to any BLE
  device, from the console or from an ESP32.

Python 3 standard library only: no Node, no pip, no build step.

**What you need:** an Orange Pi Zero 2W running Armbian (or another
Debian/Ubuntu image) with Python 3. ESP32 boards are optional.

**Read [Security](#security--read-this-before-exposing-it) before you expose
it:** the terminal is a real shell behind one token.

[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) explains what this is and how the
parts fit together; [docs/CONTEXT.md](docs/CONTEXT.md) is every decision and
trap in detail. This file is the operational side: how to run it, what each
screen does, and what it cannot do.

## Install

On an Orange Pi Zero 2W running Armbian (or another Debian/Ubuntu image), as
the user the console should run as (not root):

```
curl -fsSL https://raw.githubusercontent.com/King-Jarvis/auto-blox/main/get.sh | bash -s -- --iot --mpy
```

That downloads Auto-Blox to `~/auto-blox` and runs its installer with the
options after `--`, asking sudo for your password. Leave the options off for
the console alone. Run the same command again to update: a git checkout is
pulled, a downloaded copy is replaced (keeping `backups/`, and the old copy in
`~/auto-blox.previous`). Your settings and flows live in
`~/.config/zero2w-console` and are never touched by an update.

Or by hand, from a clone:

```
git clone https://github.com/King-Jarvis/auto-blox.git ~/auto-blox
cd ~/auto-blox
sudo scripts/install.sh              # the console service, GPIO/I2C/Bluetooth access
sudo scripts/install.sh --iot        # also the access point for ESP32 boards
sudo scripts/install.sh --desktop    # also a desktop launcher
sudo scripts/install.sh --mpy        # also build mpy-cross (boards get compiled code)
```

The options combine: `sudo scripts/install.sh --iot --mpy` is the full set for
running ESP32 boards. Without `--mpy`, boards are sent source and compile it
themselves. That works, but on a board without PSRAM, importing the flow runner
takes about 78KB and half a second instead of 17KB and 70ms.

It installs the few packages the console calls (`bluez`, `gpiod`, `esptool`,
`openssl` for the camera stream's certificate,
and with `--iot` `hostapd`, `dnsmasq-base`, `iw`), adds you to the groups that
own the devices (`gpio`, `i2c`, `input`, `bluetooth`, `dialout`,
`systemd-journal`), and writes the systemd units from the templates in
`packaging/` with your user, group and this checkout's path. Log out and back
in once afterwards so the groups apply. Run it again after moving the
checkout. `scripts/install.sh --render DIR` writes the filled-in units to
`DIR` without installing anything, to check them before installing.

Then open `http://<board>:8787/?t=<token>` with the token from
`~/.config/zero2w-console/token`.

## Running it by hand

Run it from the repository root, where the package is importable:

```
python3 -m zero2w_console                 # 0.0.0.0:8787, token auth, shell enabled
python3 -m zero2w_console --no-exec       # read-only: the terminal refuses to run anything
python3 -m zero2w_console --no-auth       # no token — trusted LAN only, see SECURITY
python3 -m zero2w_console --interval 5    # sample every 5s instead of 2s
```

Stdlib only. There is no Node and no package manager on this device, so the
backend is a plain `http.server` package and there is nothing to install — no
virtualenv, no lockfile, no build step.


## Access

The token is generated on first run and stored `0600` at
`~/.config/zero2w-console/token`. Append it as `?t=<token>` once and the server
sets a cookie, so later visits need no query string.

| From | URL |
| --- | --- |
| The board's own desktop | `http://localhost:8787/` |
| Anything on the LAN | `http://<board's IP address>:8787/` |
| Anywhere else | over a VPN such as Tailscale or WireGuard, never a port-forward |

`scripts/open-console.sh` opens it in a chromeless Chromium window with the current
token filled in. The desktop launcher uses the same script, so the token can be
rotated without editing anything.

To rotate the token: delete the file and restart. Every existing cookie stops
working.

## The service

`scripts/install.sh` installs `zero2w-console.service`, which runs as the user
who installed it, with the `systemd-journal` group, which is what lets the log
widget read the system journal without root.

```
systemctl status zero2w-console
sudo systemctl restart zero2w-console
```

It is `WantedBy=multi-user.target` with `Restart=always`, so the dashboard is up
before anyone logs in and comes back by itself — plug a LAN cable in and it is
already serving.

## SECURITY — read this before exposing it

`POST /api/exec` runs commands as the console's user, who is usually in `sudo`.
**That endpoint is a remote shell.** The token is the only thing in front of it.

- Do **not** port-forward 8787 to the internet. Reach it over a VPN such as
  Tailscale instead.
- Run with `--no-exec` if you only want readouts. Everything except the terminal
  widget keeps working.
- `--no-auth` removes the token from *everything*, shell included. Only use it on
  a network you control, and preferably with `--no-exec`.
- The token lives in a `0600` file in your home directory. Anyone who can read
  your home directory can use the console.
- `/healthz` is the one unauthenticated route, and returns only `{"ok": true}`.

## Layout

The screen follows the design system's main-screen guidelines: health strip
first, thermal next because this board is passively cooled and thermal is the
first thing to degrade, then network, then logs (the widget people actually
watch, so it gets height), then processes, cores, storage and the terminal.

Dock items scroll to their panel. `Ctrl`/`Cmd` + `` ` `` focuses the terminal.
The theme toggle is in the top bar and persists per browser.

## Where the numbers come from

Every reading is parsed from `/proc` and `/sys` directly — no `top`, no
`sensors`, no polling of shell tools.

| Widget | Source |
| --- | --- |
| Thermal | `/sys/class/thermal/thermal_zone*/{type,temp,trip_point_*_temp}` |
| Cores | `/proc/stat` deltas between samples |
| Memory, swap | `/proc/meminfo` |
| Storage | `/proc/mounts` + `statvfs` |
| Network | `/proc/net/dev` deltas, `/sys/class/net/*/operstate`, `ip addr` |
| Load, uptime | `/proc/loadavg`, `/proc/uptime` |
| Processes | `/proc/<pid>/stat` utime+stime deltas |
| Logs | `journalctl -f -o json` |

**Thermal thresholds are read from the kernel, never assumed.** On this board
`cpu-thermal` reports trip points at 60, 70 and 100 °C, so those become warn,
serious and critical. The other three zones expose a single 100 °C trip, so they
get a critical point and no warn band — the gauge shows no warn arc rather than
inventing one.

## Flow Studio — GPIO and network automation

`/flows` is a node editor in the N8N idiom: drag a trigger onto the canvas, wire
it to an action, save. The runtime is in `zero2w_console/flows.py`; the editor
renders its palette, ports and inspector fields straight from `flows.REGISTRY`,
so adding a node type there adds it to the UI too.

| Group | Nodes |
| --- | --- |
| Triggers | GPIO edge · Interval · Webhook · Metric threshold · Manual · MQTT subscribe · Tag change · Device event · BLE notify |
| Logic | If · Delay · Throttle · Toggle · Set value · Write tag · Read tag · Controller axis · Controller button · Timer · On change · Counter · Hysteresis · Latch · Step · Watchdog |
| Maths | Formula · Scale · Smooth · Dead zone · Ramp · Sign split |
| Actions | GPIO write · PWM output · I2C write · I2C read · I2C scan · SPI transfer · MQTT publish · Call flow · HTTP request · Shell command · Tell the host · Device command · Controller · BLE device · BLE read · BLE write · Camera feed · Camera to screen · Camera capture · Log |

Each node's own help is in the inspector; `docs/NODE-LIBRARY.md` covers the
design. **Toggle** has two inputs, each set to flip, force on or force off — one
trigger to start something and another to stop it. **Step** nodes wired
*next → go* make a sequence: each is active from *go* until *done*, the canvas
highlights the active one, and a *done* that arrives while a step is not active
does nothing.

**PWM latches.** Its action is start / stop / **toggle** — once started the
waveform keeps running (a live `gpioset` process holds it), so the node does
not need re-firing to stay on. Toggle stops it if it is already running.

**GPIO pulse has a polarity.** With action `pulse`, *Pulse to* chooses high
(drive high, return low) or low (drive low, return high) for active-low
hardware. That field, and the pulse width, only appear when the action is
`pulse` — fields that cannot apply are hidden rather than shown greyed out.

Editor: `Ctrl`/`Cmd`+`Z` undoes and `Ctrl`+`Shift`+`Z` (or `Ctrl`+`Y`) redoes, up
to 60 steps. Undo snapshots the whole document, so it is exact rather than an
inverse-operation guess.

**Call flow** runs another flow's trigger node with the current message, which
is how you factor a shared sequence out of several flows. Recursion is capped at
a depth of 8.

**MQTT** speaks 3.1.1 over a stdlib socket (`zero2w_console/mqtt.py`) because
paho is not installed — QoS 0, publish and subscribe, with automatic reconnect and
exponential backoff. One client is shared per broker; topic filters support
`+` and `#`.

**PWM** has two modes. *Software* works on any free GPIO today with no overlay:
it uses `gpioset --toggle`, which libgpiod v2 repeats until the process is
killed, so the waveform costs no Python in the loop (1–5000 Hz). *Hardware* uses
`/sys/class/pwm` and needs both an overlay and group access — see below.

The **Buses** panel in the right-hand pane probes what this board can do right
now and names exactly what is missing for anything unavailable.

Flows are stored in `~/.config/zero2w-console/flows.json` and written
atomically. Saving reloads the engine, which re-arms every trigger.

**Concurrent edits cannot silently overwrite each other.** The document carries
a monotonic `rev`; the editor posts the revision it loaded, and a save that
names a stale one is refused with `409` plus the document that is actually on
disk. The page then offers *Load theirs* or *Keep mine* — nothing is merged
behind your back and nothing is discarded without being asked for. A POST with
no `rev` at all is trusted, which is how restoring a file from `backups/`
overwrites whatever is there.

### Variables

Any text field in a node can carry `{{variable}}` templates, and every such
field has an `{x}` button that lists the whole library grouped by kind. Each
entry shows its **live value right now**, its type, and what it does once it is
inside a node — so `{{cpu.temp}}` reads `47.3` rather than a made-up sample. Picking one inserts it at the cursor; you can always
just type instead. The inspector also carries a browsable **Variable library**
that inserts into the field you last used.

| Group | Examples |
| --- | --- |
| Message | `{{payload}}` `{{meta.edge}}` `{{meta.gpio}}` `{{meta.topic}}` `{{meta.status}}` |
| Context | `{{flow.name}}` `{{flow.id}}` `{{node.id}}` |
| Readings | `{{cpu.temp}}` `{{load1}}` `{{mem.used}}` `{{root.percent}}` `{{net.ip}}` |
| Board | `{{host}}` `{{time}}` `{{date}}` `{{uptime}}` `{{epoch}}` |
| Device | `{{device.name}}` `{{device.rssi}}` `{{device.free}}` `{{device.online}}` |
| Controller | `{{pad.lx}}` `{{pad.ly}}` `{{pad.rt}}` `{{pad.connected}}` |
| Other devices | `{{dev.esp32_motor.online}}` `{{dev.esp32_motor.rssi}}` `{{dev.esp32_motor.pin.27}}` — any device, by its name lowercased with `_` for anything else, or by its id; this host only |
| Tags | `{{tag.<name>}}` — any tag in the table |

`flows.VARIABLES` is the single source of truth: the editor builds its
dropdowns from it and `flows.resolve_variable` answers it, so a variable cannot
exist in one and not the other.

Numeric fields take variables too. Picking one switches the field from a number
box to a formula box — a delay of `{{payload}}` ms is legal. Clear it to go
back to a plain number.

An unknown name is left in the output exactly as written rather than becoming
an empty string, so a typo shows up in the log instead of vanishing.

`/flows` opens on an **index** of every flow — not the editor. Each card shows
its triggers, node and link counts and which header pins it claims, and carries
the controls you reach for most: enable/disable, Run (when the flow has a manual
trigger), rename in place, duplicate and delete. Filter by name or trigger, and
sort by title, trigger, enabled-first or size. The editor is somewhere you go
deliberately, via **Edit**.

### Connectors

A node's *logical port* (`in`, `out`, or `true`/`false`) is separate from the
*anchors* it can be reached from. Inputs may be entered from the **left or the
top**; outputs may leave from the **right or the bottom**. Every port gets an
anchor on each side its direction allows, so:

- a trigger shows **no input anchors at all** — it cannot be a destination;
- a node with one input can be entered from left or top, whichever routes
  better, and the two are the same port;
- a node with one output can leave rightwards or downwards;
- a node with two outputs (If) shows both on the right, stacked and labelled,
  **and** both along the bottom — so a branch can fan sideways or downwards.

While you drag a connection, anchors that cannot legally accept it disappear:
outputs (they never receive), the source node's own inputs (no self-link), and
any input that would duplicate an existing edge. What remains is ringed green
as a valid target.

Anchors are presentation only. An edge records `fromSide` and `toSide` so the
drawing matches what you connected, but the runtime walks ports and ignores
geometry entirely — rerouting a link never changes what a flow does. An edge
with no sides recorded draws right-to-left.

Link curves leave each end along that end's own side, so a link out of a
node's bottom dives downward instead of shooting sideways.

Editor controls: drag from the palette (or double-click a palette item), drag a
node to move it, drag an output port onto an input port to connect, click a link
to select it and a ✕ appears at its midpoint to remove it (there is no
Delete key on a phone, so the shortcut cannot be the only way), `Delete`
removes the selection, `Ctrl`/`Cmd`+`S` saves,
`Ctrl`/`Cmd`+`Z` undoes, wheel zooms, drag the background to pan, `Esc` goes
back to the index. Nodes snap to the 4px grid. The editor deep-links as
`#edit/<flow id>`.

**On a phone** the editor keeps the full width for the canvas and moves the
palette and inspector into drawers, opened from the **Nodes** and **Inspect**
buttons. Each drawer carries its own **✕**, because a full-height drawer covers
the button that opened it. Tapping the scrim or pressing `Esc` also closes one.

**Tap a palette item to add that node** at the centre of the view. Touch devices
never fire `dragstart`, so drag-from-palette is desktop-only — without a tap
path the palette is unusable on a phone. The toolbar is one sideways-scrolling
row on narrow screens rather than wrapping to two.

The drawers are `position: fixed` and `visibility: hidden` when closed, so
pinching out never reveals them parked in the margin. Every gesture goes
through pointer events in one map — one pointer pans or drags, two pinch — and
the canvas sets `touch-action: none` so the browser does not zoom the page on
top of it. Form controls are 16px on touch devices, below which a phone zooms
on focus. Auto-fit stops at 0.55 so nodes stay readable; manual zoom reaches
0.3.

A webhook node listens at `POST /api/hook/<path>` and needs the console token
like every other route:

```bash
curl -H "X-Console-Token: $(cat ~/.config/zero2w-console/token)" \
     -H 'Content-Type: application/json' \
     -d '{"payload":"hello"}' http://localhost:8787/api/hook/ping
```

Guards that are enforced in code, not just documented: a line already claimed by
a kernel driver is never driven, a pin not on the header is rejected, graph
cycles stop after 40 hops, and every driven pin is released when the console
exits. `--no-gpio-write` makes the whole surface read-only.

## The 40-pin header

The pin map in the right-hand pane shows all 40 pins: free, bound to a flow,
reserved by a kernel driver, power or ground — plus which flow claims each pin
and whether this console is currently driving it. Clicking a GPIO pin shows its
identity and lets you drive it high/low for bring-up testing.

**Pin data is not guesswork.** `zero2w_console/data/pinout-zero2w.json` is
generated by `scripts/build_pinout.py` from `physToGpio_ZERO_2_W` and
`pinToGpio_ZERO_2_W` in the vendor's own wiringOP source, which is the table wiringOP itself uses for
`BOARD=orangepizero2w`. 28 GPIO across banks PC, PH and PI; the device tree on
this board exposes no `gpio-line-names`, so nothing can be derived from the
kernel alone. Live line state (direction, consumer, in-use) comes from
`gpioinfo` at runtime.

### Enabling I2C, SPI and hardware PWM

A stock Armbian image on this board muxes **none** of these onto the 40-pin
header: `/boot/armbianEnv.txt` has no `overlays=` line at all. Until one is
added the flow nodes exist but report the bus as unavailable, which the Buses
panel shows.

The easy way is **Put a bus on the header**, under Buses in the studio's
right-hand pane. It lists every overlay that reaches the header, with its pins;
choosing one lights those pins on the pin map and warns about a pin a flow
already uses or two buses sharing one. It then gives the one command to run —
editing `/boot` needs root — which rewrites the `overlays=` line and reboots:

```
# /boot/armbianEnv.txt
overlays=i2c3-ph pwm1-ph3
```

The table behind it, `data/overlays-zero2w.json`, is generated by
`scripts/build_overlays.py` from the overlays in `/boot/dtb/allwinner/overlay/`
(prefix `sun50i-h616-`) and this board's own device tree, read with `dtc`. That
is how `i2c3-ph` is known to land on pins 18 and 24. It is also why SPI is not
offered: `spidev1_0/1/2` switch SPI1 on, but neither they nor this board's tree
assign it any pins, so nothing would reach the header.

Note that `/dev/i2c-0` and `/dev/i2c-1` already exist without any overlay:
`i2c-0` is an internal controller and `i2c-1` is HDMI DDC. Neither is a header
bus.

After a reboot, `python3 -m zero2w_console.buses` prints exactly what is available.

### GPIO permissions

`/dev/gpiochip*` ships root-only, so the console can neither read nor drive a
pin until a group owns them. `scripts/install.sh` runs this for you; on its own
it is:

```
sudo scripts/setup-gpio.sh
```

That creates `gpio` and `i2c` groups, installs udev rules for
`/dev/gpiochip*`, `/dev/i2c-*` and `/sys/class/pwm`, and adds you to both.

**Supplementary groups are fixed when a process starts**, so a console already
running when you ran the script keeps its old credentials even though the
device permissions are now correct. Restart it:

```
sudo systemctl restart zero2w-console
```

The installed service declares `SupplementaryGroups=systemd-journal gpio i2c`,
so it always starts with the right credentials regardless of your login session.
Running it by hand instead requires a shell you have logged into *since* the
group was added. `python3 -m zero2w_console.gpio` reports which of these
situations you are in.

## Editing the design tokens

```
# edit design/tokens.json, then
python3 scripts/build_tokens.py
```

That regenerates `zero2w_console/static/tokens.css`. Refresh the browser;
nothing else changes.
Colours and contrast in that file were solved numerically — if you change a
value, re-check text against its grounds in both themes before trusting it.

## Repository layout

```
zero2w_console/                 the application package (stdlib only)
  __main__.py                   `python3 -m zero2w_console`
  server.py                     HTTP, SSE, auth, routing
  gpio.py                       pin inventory and libgpiod v2 access
  buses.py                      I2C / SPI / PWM, plus the capability probe
  mqtt.py                       minimal MQTT 3.1.1 client
  flows.py                      node registry and the host's flow runtime
  examples.py                   the example flows
  tags.py                       the tag table
  iot.py                        the radio, board profiles, device configs
  fleet.py                      devices: enrolment, manifests, commands
  link.py                       the optional held-open socket per device
  serialport.py                 a serial port and the raw REPL, from termios
  pixels.py                     sensor frames into PNG
  bluetooth.py                  the host's adapter: status, scan, pair
  gatt.py                       any BLE device's GATT, through BlueZ D-Bus
  gvariant.py                   a parser for what gdbus prints
  pad.py                        a gamepad on this host, from /dev/input
  agent/                        MicroPython: the device agent, its runner, and
                                the modules a flow pulls
  data/boards/*.json            GENERATED — ESP32 board profiles
  data/pinout-zero2w.json       GENERATED — vendor-derived pin table
  data/overlays-zero2w.json     GENERATED — which header pins each overlay takes
  static/                       index, flows, iot, cameras and login pages, their
                                js and css, nav.js (the shared nav bar)
  static/bundle.js|.css         design system components
  static/tokens.css             GENERATED — design tokens
design/tokens.json              design system tokens (source of truth)
scripts/build_tokens.py         design/tokens.json -> static/tokens.css
scripts/build_pinout.py         vendor pin table -> data/pinout-zero2w.json
scripts/build_boards.py         vendor tables -> data/boards/*.json
scripts/build_overlays.py       this board's overlays + device tree -> data/overlays-zero2w.json
scripts/iot-flash.py            flash a board and hand it its identity
scripts/iot-bringup.py          network up, flash, deploy, watch
scripts/sim-device.py           a fake device speaking the real protocol
scripts/build-mpy-cross.sh      builds the compiler that makes device .mpy files
scripts/check-js.py|check-pages.py  compile the JS / load every page in a browser
scripts/*probe*.py, ble-*.py    Bluetooth probes (see Bluetooth below)
scripts/open-console.sh         launcher helper (used by the desktop entry)
get.sh                          one command: download or update, then install.sh
scripts/install.sh              installs everything for the user running it (sudo)
scripts/setup-gpio.sh           udev rule + gpio group (install.sh runs it)
packaging/                      templates for the service units, desktop launcher
                                and fallback sudoers, and the iot-netctl root
                                helper; install.sh fills in user, group and path
docs/                           ARCHITECTURE (start here), CONTEXT (every trap),
                                IOT-PLAN, NODE-LIBRARY, RADIO
tests/                          stdlib unittest checks, no hardware needed
backups/                        flow snapshots — see backups/README.md
```

Four sets of files are generated and must never be hand-edited:
`static/tokens.css`, `data/pinout-zero2w.json`, `data/overlays-zero2w.json` and
`data/boards/*.json`. The tests fail if any has drifted from its generator.

## Tests

```
python3 -m unittest discover -s tests
```

No count is given here because it changes on almost every commit — run it. What
it covers:

- **Structure.** The package imports with nothing but the standard library, the
  entry point runs, the pin table is internally consistent, both generated files
  still match their generators, every asset the pages reference exists, and
  `NODE_W` in `flows.js` equals `.node { width }` in `flows.css` — that last is
  computed geometry, so a mismatch silently drifts every link on the canvas.
- **The flow engine and the node library**, every node type against its declared
  shape, and every shipped example executed rather than merely parsed.
- **The device agent**, including a whole pass of its main loop against a stubbed
  host and a stubbed board: the boot order, what happens when a flow will not
  start, and what a report says. A mistake in `run()` does not show up as a
  failing feature, it shows up as a board that has to be recovered over a cable.
- **The link protocol**, host and device ends, over a loopback socket.
- **Agreement.** That the screens render a fact one way
  (`test_screen_congruency`), that the documents' counts match the registry
  (`test_docs_agree`), and that `agent.py` stays inside its memory budget
  (`test_agent_budget`) — on a board that compiles its own source, a comment is
  heap.

They touch no hardware and open no ports, so they are safe to run while the
service is up. Some take a few minutes: the agent tests drive real loops.

## IOT — hosting a network for small devices

`/iot` is the third page. It reports what the radio can actually do, configures
one access point for ESP32-class devices, and lists the devices that have
enrolled.

What this board's radio allows, measured rather than assumed:

- **AP or station, never both.** `wlan0` advertises AP mode, but its only
  interface combination is `#{ managed, AP } <= 1`. Running the IoT network
  means this board cannot also join a WiFi network — fine while the uplink is
  USB ethernet.
- **2.4 GHz only, channels 1, 6 and 11.** The radio has 5 GHz channels; no
  ESP32 can see them, so they are not offered.
- The radio ships **soft-blocked in rfkill**. The Radio widget has a
  **Turn on / Turn off** button for exactly that.

### The radio switch

The button writes one `struct rfkill_event` to `/dev/rfkill` — the same thing
`rfkill unblock wifi` does, in eight bytes, with nothing to shell out to. That
file is root-owned, but logind puts an ACL on it for whoever is logged in at the
seat, so the button usually works with no privilege at all.

When nobody is logged in locally that ACL does not exist — which is the headless
case, and the one that matters — so it falls back to `iot-netctl radio-on` /
`radio-off`. If neither route works the error names the command to run. Turning
the radio off through the helper stops the access point first, because an AP
cannot outlive its radio.

### Boards, configs and flows

Three things, kept separate on purpose:

| | What it is | Where it lives |
| --- | --- | --- |
| **Board profile** | a chip's pinout and peripherals, generated | `zero2w_console/data/boards/*.json` |
| **Device config** | one physical board you own | `~/.config/zero2w-console/iot.json` |
| **Flow** | drawn **for a profile**, deployed **to a config** | `flows.json` |

**Plug in, scan, configure.** The Devices widget scans `/dev/ttyUSB*` and
`/dev/ttyACM*`, asks esptool what each board is, and matches the chip to a
profile. A config is made from that answer — chip, MAC and flash are recorded as
the scan saw them, so a later mismatch is visible rather than silently
overwritten. Only the name, board, note and deployed flow are editable
afterwards. `esptool` is the one prerequisite, and `scripts/install.sh`
installs it; the USB-serial drivers are already in the kernel.

**Profiles are generated, never typed** — `python3 scripts/build_boards.py`,
from vendor tables transcribed and cited in the generator. Each pin carries what
it can do and what will otherwise cost an evening: GPIO 6–11 are flash on the
classic ESP32, 34–39 are input-only with no pull resistors, 0/2/12/15 are
strapping pins, every ADC2 pin says it cannot be read while WiFi is on, and the
pins that belong to PSRAM on some module variants say which.

### Knowing whether it is actually hooked up

A flow bound to a board carries a status chip in the toolbar and a banner at
the top of the inspector, and between them they answer the question that was
otherwise left to guesswork. Every case says something:

| | |
| --- | --- |
| **not linked** | written for a board, but no device chosen yet |
| **never connected** | linked, but nothing has ever reported in — flash it, and check it can reach the console |
| **offline** | it has reported before; the readings shown are the last it sent, and the flow in its flash keeps running while it is away |
| **not deployed** | online, but running a different flow, or none |
| **live** | running this flow, with how long ago it was heard from |

The inspector shows **that board's pins**, in the same map the 40-pin header
gets: free, in this flow, or not available, with the reason in one word —
`flash`, `psram`, `camera`, `sd`, `no pin`. Once a device reports, each pin
also carries what it is doing: `out ▲`, `in ▼`, or a PWM duty. Buses and
peripherals are listed under it, with the camera's sensor and frame count when
one is running.

Devices report every two seconds while they are online: pin levels, the flow
they are running, their address, signal, free memory and agent version.

**A flow names a board, then a device.** In the studio, a flow's ⋯ menu has
*Runs on* (this host, or a profile) and *Linked device*. Choosing a board
changes three things at once, because all three read the same profile:

- the palette drops every node that board cannot run — no shell, no journal, no
  host metrics, no sub-flows;
- every pin dropdown becomes that board's pins, labelled with their
  capabilities, with the unavailable ones disabled and the reason on hover;
- this host stops arming the flow, because it is the device's to run.

A node the board cannot run is **kept and flagged**, never deleted: dashed red
on the canvas, with the reason in the inspector. Losing work to a dropdown is
worse than being told what needs changing.

**Groups** are named lists of devices, kept in the Groups widget on the Devices
tab. **Device command** and **Device event** take a group wherever they take a
device: a command goes to every member, and an event from any member fires the
trigger. Deleting a device takes it out of its groups.

### Bringing the network up

The console runs as an unprivileged service user, and NetworkManager's polkit
refuses that user the wifi actions outright (`enable-disable-wifi: no`). Rather
than hand the console broad NetworkManager power, one root helper with a fixed
verb set does the work:

```
sudo scripts/install.sh --iot
```

That installs `hostapd` and masks its own unit, puts the helper at
`/usr/local/sbin/iot-netctl`, and runs it as `zero2w-iotnet.service`, telling
it which group may use its socket (yours). Then set the access point's name,
passphrase and channel on the IOT screen.

**The console cannot use sudo, and should not be able to.** Its unit sets
`NoNewPrivileges=yes` — deliberately, since it also exposes a shell — and that
flag makes every `sudo` from it fail with *"the no new privileges flag is
set"*, whatever sudoers says. So the privileged half runs as its own root
service and listens on `/run/zero2w-iot/ctl.sock`, which only the console's
group can open. It accepts nine verbs and re-validates everything it is given.
`packaging/zero2w-iot.sudoers` is kept only for a machine that cannot run that
service; if you installed it, `sudo rm /etc/sudoers.d/zero2w-iot`.

`hostapd` is masked because the helper runs its own instance against a generated
config; the packaged unit would fight it.

The helper's verbs are `config`, `up`, `down`, `firewall`, `radio-on`,
`radio-off`, `status`, `diagnose` and `clients`, plus `serve`, which is how the
service runs it. It takes **no configuration on its command line**. `iot-netctl config`
reads JSON on stdin, validates every field, and stores it `0600` under
`/etc/zero2w-console/`. `up` and `down` take nothing at all. That is what keeps
the NOPASSWD entry from being a root shell — and the helper deliberately
imports nothing from this repository, because this repository is writable by the
user the console runs as.

See what it would do without touching anything:

```
python3 packaging/iot-netctl up --dry-run < network.json
```

### What a device on that network can reach

hostapd runs with `ap_isolate=1`, so devices cannot talk to each other through
the AP. The helper installs two iptables chains: an IoT device may reach DHCP,
DNS, ICMP and the console's own port on this host, and it may reach the
internet through the uplink — but **not the LAN**. Both chains are named, so
`down` removes exactly what `up` added.

Set `"lan_access": true` in the config to lift that, deliberately.

`--no-iot-net` refuses every network change while leaving the page readable.

## Flashing a board, and deploying a flow to it

```
python3 scripts/iot-flash.py --port /dev/ttyUSB0 --device dev_ab12cd34 --dry-run
```

The flasher asks the board what it is, asks the console for a one-time
enrolment token, fetches the right MicroPython build for that chip (cached
under `~/.cache`), writes it, and then copies three small files over the serial
REPL: `agent.py`, a `main.py` that starts it, and a `config.json` carrying the
network and that token. Everything else the board pulls over WiFi on first
boot. The serial port is driven straight from `termios` — there is no pyserial
here and no pip to get one.

The IOT screen does the same thing with a button, streaming the flasher's
output into a log on the page. The passphrase is asked for each time and never
stored: it goes down the flasher's stdin, so it is not in `ps` either.

**Debian's esptool cannot flash a classic ESP32, S2 or S3 at full speed.** It
ships as a `+dfsg` repack with those stub flashers stripped out, which surfaces
as `FileNotFoundError: stub_flasher_32.json`. Everything falls back to the ROM
loader, which works but is slow and cannot erase flash. To fix it properly:

```
sudo install -m 0644 ~/.cache/zero2w-console/esptool-stubs/*.json \
     /usr/lib/python3/dist-packages/esptool/targets/stub_flasher/
```

### What the device runs

The agent is MicroPython, in `zero2w_console/agent/`. On boot it joins the
network, enrols once, and asks for a manifest: the loader, the flow runner, and
**only the modules the deployed flow needs**, each with a sha it checks. Once
`scripts/build-mpy-cross.sh` has been run, every file but the agent goes to a
board as precompiled `.mpy` — a third of the size, and about 60KB less RAM for
the flow runner alone. A flow
that blinks an LED pulls about 62 KB — the agent and runner are 61 KB of that —
and no I2C or HTTP module at all.

The flow then runs **on the device**, from its flash, so it keeps working when
this host reboots or the link drops. Between runs of the flow it asks for
commands — deploy, reboot, fire a trigger, override a pin — and posts what it
sees back onto the same SSE bus the dashboard uses, so a remote flow's firings
appear in the studio's log exactly like a local one's.

Deploying is an act, not a side effect: linking a flow to a device in the
editor says where it is meant to run, and the **Deploy** button on the IOT
screen is what actually sends it. A deploy is refused if the flow is written
for a different board or contains a node the device cannot run.

`python3 scripts/sim-device.py` walks the whole path against a device that does
not exist, including the things that must fail.

## Cameras

**The flow decides.** A board having a lens on it is not a reason to run the
camera, so the camera is a node, and like every node it acts only when a
message reaches it:

| Node | What it does |
| --- | --- |
| **Camera feed** | a switch for the sensor: a truthy message starts it at the node's size and format, a falsy one stops it. An **Interval** wired in keeps it on; unwired, it never runs |
| **Camera to screen** | wired after a feed, puts the device on the Cameras screen and the dashboard, and sets how often a frame is asked for |
| **Camera capture** | takes one frame per message, for a picture on an edge or on a schedule. Not needed for the live view |

**Use jpeg.** Set the Camera feed node's *Picture* to `jpeg`: the camera
compresses on the board, and a 640×480 colour picture is 7–17 KB. The raw
formats are 38 KB (colour) and 19 KB (greyscale) at only 160×120, and this
radio is the slow part. Measured on an ESP32-CAM (OV3660) with *How often* at
100 ms: **6.9 new pictures a second at 640×480 JPEG**, against about one every
two seconds for 160×120 greyscale. JPEG needs camera firmware v0.6.0, which the
flasher installs; later releases cannot make JPEG on an OV3660 or OV5640.

**Pictures are encrypted.** A camera board streams them to the console over
TLS, on the link's port, and only while the Cameras screen or the dashboard is
open (each look holds a ten-second lease). The console makes its own
certificate on first start, with `openssl`, and hands it to each board signed
with that board's token, so a board trusts this console and nothing else. The
startup message says `pictures : encrypted stream (TLS)`. A console without
`openssl` has no certificate; its cameras then answer on port 8080,
unencrypted, as they used to.

They appear in the palette only for a board whose profile says it has a camera,
and they run on the device. A JPEG goes to the browser as it came, turned by
the sensor itself (and a quarter turn by the screen); a raw frame is turned and
made into a PNG here by `zero2w_console/pixels.py`, with nothing but `zlib`.

That also means the widget polls images rather than holding an MJPEG stream
open, which suits a device that may be asleep: nobody watching costs nothing.
The last good frame is kept and served with `X-Frame-Age` and `X-Frame-Stale`,
so a picture is never passed off as live when it is not.

Each camera has **two stacked frames and only the loaded one is shown**.
Swapping the `src` on a single image blanks it until the next frame arrives,
which at one frame a second reads as a flicker. Click a tile — or press Enter
on it — for an enlarged view that refreshes twice as fast; Escape or a click
outside closes it.

**A camera needs a camera-capable firmware**, and the flasher picks one
without being asked: a config with a camera gets a build that has a `camera`
module, fetched from the `micropython-camera-API` release for that board and
cached. It is pinned to v0.6.0: the two releases after it cannot start an
OV3660 or OV5640 sensor in JPEG. A stock build has no camera module at all, so flashing one onto a
camera device leaves it blind. `--stock` overrides it deliberately, and
`--firmware` takes any image you like.

`modules/camera.py` is still written so that asking for a camera on a build
without one logs a line rather than stopping the flow.

Byte order was settled by looking at a real frame: big-endian per pixel. The
other way round does not fail, it just produces a garish false-colour picture
that looks like broken hardware rather than a broken assumption.

## Moving a device onto a new board

For replacing a board that ran out of room, such as the motor controller going
onto an ESP32-WROVER or S3 with PSRAM:

1. Plug the new board in and **Scan USB** on the IoT screen's Devices tab.
2. On its row, **Move a config here** and choose the config. It keeps its name,
   flow and settings; the identity (MAC, chip, flash) is taken from the scan,
   and the old board's token stops working.
3. On the device page, set **PSRAM**: *quad* for a WROVER and most S3 boards,
   *octal* for an S3 N16R8 and similar. A scan only sees PSRAM built into the
   chip, and a WROVER's is not. The flasher then writes the build that uses
   it: `ESP32_GENERIC-SPIRAM` for a classic ESP32, `ESP32_GENERIC_S3-SPIRAM_OCT`
   for octal on an S3.
4. **Flash**. The board enrols as the same device and pulls its flow.

A WROVER is a classic ESP32, so the motor flow's pins carry over unchanged. An
S3 lacks some of them (GPIO 25, for one); moving a config onto one says which,
and the flow's board has to be changed and those pins remapped before it will
deploy.

## Testing a flow without a network

`--flow <id>` writes a flow to the board over the serial REPL along with the
modules it needs, so it runs standalone before it has ever seen the host:

```
python3 scripts/iot-flash.py --port /dev/ttyUSB0 --device dev_xxxxxxxx \
    --flow f_blink --config-only
```

`--config-only` skips the firmware and rewrites the files, which takes seconds
rather than minutes. The agent starts the flow in flash **before** it tries to
join anything, retries the network with a backoff, and never resets the board
for want of a host — a field device that will not run what is in its flash
until a server answers is not a field device. When it is offline its log lines
go to the serial console instead of the host.

## The IoT network is served on an interface, like any router

There is nothing special about "an access point". A router gives an interface
an address, hands out leases on it, routes and firewalls it — and on a wire
that is the entire job, because **the cable is the network**. Every Ethernet
NIC can do it; none of it touches the hardware.

A radio is different only in one respect: there is no medium until something
transmits beacons and runs the association and key handshake for each client.
That is "AP mode", and it lives in the radio's **firmware**, not in Linux — so
it is the only part of this that a chip can refuse. This board's chip does not
refuse it, but it will only do it when nothing else is holding the radio.

So the config names an interface, and the rest follows from what kind it is:

| Interface | What it needs | Status here |
| --- | --- | --- |
| the built-in `wlan0` | an address, `dnsmasq`, and `hostapd` for beacons | **works** — this is what the fleet runs on |
| a wire (`eth0`, a USB Ethernet adapter) | an address and `dnsmasq` | works — plug in a switch and devices appear |
| a USB wifi adapter | the same as `wlan0` | should work, untested here for want of one |

On a wire the screen stops asking for an SSID, a passphrase and a channel,
because a wire has none of those. `iot-netctl status` lists every interface
that could carry the network and what each would need.

The widget is two states and one button: **Set up** asks for a name, a
passphrase, a channel and an address range, and **Start** does the rest —
unblocking the radio, taking every other claim off it, writing the configs,
starting hostapd and dnsmasq, and applying the firewall. Nothing is asked of
you that the button can do itself.

### What this board's radio needs

`wlan0` is a UNISOC **uwe5622** combo over SDIO, driven by `sprdwl_ng`, and it
hosts the access point the fleet runs on. Its firmware has one bus channel, so
an AP cannot open while anything else claims the chip — NetworkManager, the
system `wpa_supplicant`, or the P2P device that supplicant leaves behind. The
firmware asserts in `bus_chn_init` and resets, taking Bluetooth with it.

So `up` takes the radio first: NM is told the interface is not its, the system
supplicant is stopped, P2P devices are deleted, the interface is set back to
station mode and unblocked. `down` hands all of it back. `docs/RADIO.md` has
the logs and the four things that were not the answer.

**An ESP32 is wifi-only**, so a wired IoT network still needs something to
broadcast on it — any access point or old router plugged into the switch will
do, and the console does not care which. What it needs is a network it can
reach, not one it made.

## Bluetooth

The IoT screen's **Bluetooth** tab is the host's adapter: scan, pair, connect,
disconnect and forget, and a browser for any connected BLE device's GATT —
services, characteristics, and read, write and subscribe on each. Setup's last
step walks through pairing a controller. No privilege is involved: the
console's user is in the `bluetooth` group.

- **Put a controller in pairing mode before pressing Scan** — hold its pair
  button until the light flashes quickly. A pad that is merely on does not
  advertise.
- **Pairing happens inside the scan.** BlueZ forgets an unpaired device when
  discovery ends, so Pair holds one `bluetoothctl` session open across scan,
  pair, trust and connect.
- **A scan shares a chip with the access point.** One scan beside the running
  AP has been observed to be harmless; it is still a button, bounded in time,
  and never part of a poll.

A paired gamepad drives the **Controller** nodes on this host through the
kernel's input layer (`zero2w_console/pad.py`), with the axis layout read
from the driver rather than assumed. `ex_motor_pad` is the example: a trigger
for throttle, a stick for steer, A to arm and B to disarm, and a Watchdog that
stops the wheels when the pad goes quiet.

Probes, all unprivileged:

```
python3 scripts/pad-probe.py                       # what a paired pad reports
python3 scripts/ble-gatt-probe.py --name <name>    # any BLE device's GATT
python3 scripts/ble-peripheral.py                  # a board on USB as a GATT target
python3 scripts/ble-pad-probe.py --name xbox       # can a board bond a BLE pad
```

Any other BLE device — a sensor, a bulb, another board — is reached from a
flow with four nodes on this host. **BLE device** holds the connection while
messages arrive; **BLE read** and **BLE write** take a characteristic by UUID
(as the Bluetooth tab lists it) and a format (`hex`, `utf8`, or a
little-endian number such as `uint16-le`); **BLE notify** fires on every value
the device sends, as it arrives.

The same four nodes run on a board with BLE, as one connection the flow's
tick drives. Changing the flow lets the connection go and turns the radio off.
While a board holds a BLE connection, its link to this host is slower: on the
CAM the median round trip went from 26ms to 62ms, because WiFi and BLE share
one radio. A board without PSRAM may not have the memory for BLE beside its
flow; the motor board does not (`docs/CONTEXT.md` §6, item 19). The
controller nodes still have no transport on a board: there they log once and
report nothing.

## Known limits

- I2C, SPI and hardware PWM need a device-tree overlay and a reboot (above).
  Software PWM and GPIO need neither.
- MQTT is QoS 0 only: no retained-message replay on reconnect, no persistent
  sessions.
- SPI is untested on this board — no `/dev/spidev*` exists until an overlay is
  enabled, so the transfer path has never run against real silicon here.
- The terminal runs **one command at a time**, not an interactive PTY. `ls`,
  `systemctl status`, `cat /sys/...` all work; `htop`, `vim` and tab completion
  do not, because there is no terminal emulator on the page.
- Commands time out after 15 s.
- Webfonts come from Google Fonts. With no internet the page falls back to
  `system-ui` and `ui-monospace` and stays fully legible.
- IPv6 addresses are not shown.

**On the device side**, the ones that will bite you rather than merely annoy you.
`docs/CONTEXT.md` §6 is the canonical list; these are the four that change how
you use the IOT screen:

- **A shared tag reaches a booting board at the fleet's current value** —
  right for a setpoint, wrong for an arm. Tick *Go back to its initial value
  when a board boots* on any tag that arms something: then any board booting
  resets it everywhere, and arming again is a deliberate act. The motor
  examples' `drive_armed` has it set.
- **A tag-driven flow has no deadman.** A host that disappears while a board is
  armed leaves the last speed applied for ever: the tick is local to the board.
  The controller nodes are the fix, once a board can read a pad.
- **A plain ESP32 cannot always complete a wifi join on USB power** — a host USB
  port does not carry an ESP32's association bursts. Bench-test from the board's
  own supply, and expect a cold power-on to be the hardest moment it sees.
- **A DualShock 4 cannot pair with a stock board at all.** It speaks Bluetooth
  *Classic* HID and MicroPython's ESP32 port ships BLE only. An Xbox Series X/S
  pad speaks BLE and needs no rebuild — `scripts/ble-pad-probe.py` measures
  whether bonding works before anything is built on it.

## License

MIT, see [LICENSE](LICENSE).
