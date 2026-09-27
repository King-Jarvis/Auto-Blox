# Auto-Blox — context for building on top of this

**Read `docs/ARCHITECTURE.md` first** — it explains the shape in one pass.
This file is the detail underneath it: the hardware, the seams, the decisions,
and above all §5, which is every trap that cost real debugging time.

Written as the starting point for planning an extension. It assumes no memory of
how any of this was built. Everything here was verified against a running
board, not inferred.

---

## 1. The machine

What it was built and measured on. Nothing below is required beyond the board
itself; `scripts/install.sh` sets up the rest for whoever installs it.

| | |
|---|---|
| Board | OrangePi Zero 2W, `xunlong,orangepi-zero2w` / `allwinner,sun50i-h618` |
| CPU / RAM | 4× Cortex-A53 @ 1416 MHz, 4 GiB, aarch64 |
| OS | Armbian, `BOARD=orangepizero2w`, kernel 6.18 sunxi64, Python 3.14 |
| User | the one who ran `scripts/install.sh`, added to `systemd-journal`, `gpio`, `i2c`, `input`, `bluetooth`, `dialout` |
| Network | wired (onboard or USB ethernet) as the uplink; `wlan0` free for the access point |
| Cooling | none. Passive. Thermal is the first thing to degrade under load |

**Constraints that shaped every decision:**

- **No Node, no JS engine, no package manager story.** Python 3.14 only. Every
  line of backend is Python standard library. There is no build step; the
  browser is handed the source files as written.
- **A 4-core A53 driving a browser.** Anything that redraws per frame or per
  message must be cheap. This is why there is no framework.
- **`sudo` needs a password**, so an agent cannot install services, edit
  `/boot`, or reboot. Anything privileged must be handed to the user as a
  single command (usually `sudo scripts/install.sh`).

---

## 2. What exists

The repository checkout — no dependencies, no build step. It runs from
wherever it is cloned; `scripts/install.sh` points the service at it.

```
zero2w_console/          the application package, stdlib only
  __main__.py            python3 -m zero2w_console  (the entry point)
  server.py              HTTP, SSE, auth, routing
  flows.py               node registry + flow runtime, host and device
  gpio.py                pin inventory, libgpiod v2 access
  buses.py               I2C / SPI / PWM + capability probe
  mqtt.py                hand-rolled MQTT 3.1.1 client
  iot.py                 the radio, the link, board profiles, device configs
  fleet.py               the device link: enrol, manifest, commands, frames
  tags.py                the tag table: shared memory, host and fleet
  examples.py            the example flows, built in code and checked by tests
  pixels.py              sensor frames into PNG, rotation, with zlib
  link.py                the held-open socket per device, host side. Started
                         by server.py on 8789 unless --no-link; fleet.push()
                         takes it when a device holds one
  serialport.py          a serial port and the raw REPL, from termios
  bluetooth.py           the host adapter: status, scan, pair (bluetoothctl)
  gatt.py                any BLE device's GATT through BlueZ D-Bus (gdbus)
  gvariant.py            the parser for what gdbus prints
  pad.py                 a gamepad on this host, read off /dev/input
  agent/                 MicroPython: the field agent, its flow runner and
                         the modules a device pulls on demand.
                         modules/expr.py, modules/drive.py and
                         modules/link.py are imported by the host as well, so
                         a formula, a duty cycle and a frame mean the same
                         thing in both places.
                         modules/blocks.py holds the standard blocks, pulled
                         only by flows that use one — see the memory trap in
                         §5 before moving anything back into flow.py.
                         modules/linkclient.py is the device end of link.py
                         (transport) and modules/linkagent.py is every decision
                         around it (when to dial, which route a report takes,
                         how long to believe a silent host). Both pulled only by
                         a board whose record has link: true. See §6.
                         modules/pad.py is the controller nodes' naming and
                         scaling, shared with the host's pad.py;
                         modules/tagwatch.py is Tag change on a device, and
                         its firing rule, which the host imports too;
                         modules/blefmt.py turns characteristic bytes into
                         values and back, for the BLE nodes;
                         modules/ble.py is the BLE nodes on a device
  data/boards/           GENERATED board profiles (esp32, s3, c3, cam)
  data/                  GENERATED vendor-derived pinout
  static/                index.html (dashboard), flows.html (studio),
                         iot.html (the fleet), cameras.html (the wall),
                         login.html and their js/css. nav.js is the one nav
                         bar every page mounts; cameras-core.js is the frame
                         swapper the dashboard and the wall share; pins.css is
                         the pin map the studio and the IOT device view share
design/tokens.json       design tokens, source of truth
scripts/                 generators (tokens, pinout, boards), the flasher,
                         iot-bringup.py, sim-device.py, check-js.py,
                         check-pages.py, and the Bluetooth probes:
                         pad-probe.py (host pad), ble-gatt-probe.py (any
                         GATT device), ble-peripheral.py (a board as a GATT
                         target), ble-pad-probe.py (can a board bond a pad)
packaging/               unit and launcher templates (install.sh fills
                         them in), and iot-netctl — the root helper that
                         owns the network
docs/                    CONTEXT.md (this), IOT-PLAN.md, RADIO.md,
                         NODE-LIBRARY.md (the node redesign: what is built,
                         what is not, and the questions still open)
tests/                   stdlib unittest checks — no hardware, no ports
backups/                 where to keep flow snapshots, see backups/README.md
```

Everything the app needs at runtime lives inside `zero2w_console/`, resolved
from `__file__`, so the package can be moved wholesale without editing paths.
The unit in `packaging/` runs `python3 -m zero2w_console` from the checkout;
`scripts/install.sh` fills in the user, group and path and installs it.

`python3 -m unittest discover -s tests` proves the layout still holds — it also
fails if either generated file has drifted from its generator.

Four pages, all token-protected, all carrying the same nav bar at the top
(built once in `static/nav.js`; a page may append its own items after a
hairline, which is what the dashboard's in-page jumps are):

- **`/`** — the dashboard. Health strip, thermal, network, live journal,
  terminal, processes (full width, 16 rows), cores, storage, cameras.
- **`/flows`** — Flow Studio. A card index of flows; the node editor behind it.
- **`/iot`** — the fleet, in six tabs following the setup order: **Setup**
  (radio → network → enrolment → a board → a flow → a controller, as numbered
  steps that collapse once done), **Devices** (scan, configs, and a detail view
  per device), **Bluetooth** (the adapter, scan and pair, and any device's
  GATT), **Enrollment**, **Flashing** (the live log) and **Boards**. The tab
  is in the hash, so `/iot#devices` opens there, and in `localStorage`, so a
  reload keeps your place. Only the open tab is built on each poll.
- **`/cameras`** — every device whose flow feeds the screen, as a wall of live
  tiles; click one to enlarge.

Installed as `zero2w-console.service`, **active and enabled**, listening on
`0.0.0.0:8787`. It declares `SupplementaryGroups=systemd-journal gpio i2c` —
that is what gives it hardware and journal access regardless of login session.

### The HTTP surface

| Route | Purpose |
|---|---|
| `GET /`, `/flows`, `/iot`, `/cameras` | the four pages |
| `GET /api/snapshot` | one metrics sample |
| `GET /api/stream` | **SSE**: `metrics` every 2s, `log` per journal line, `flow` per node execution |
| `GET /api/logs` | one-shot journal tail |
| `GET /api/hardware` | live capability probe (gpio/i2c/spi/pwm/mqtt) |
| `GET /api/gpio/pins` | the 40-pin map merged with live line state |
| `POST /api/gpio/write`, `/release` | drive or release one pin |
| `GET/POST /api/flows` | the whole flow document; POST honours `rev` and answers 409 on a stale one |
| `GET /api/flows/registry` | node definitions **and** variables with live values |
| `GET /api/flows/outputs` | what each node last produced, for the inspector |
| `GET /api/flows/examples` | the example catalogue, built from `examples.py` |
| `GET/POST /api/tags`, `POST /api/tags/set` | the tag table, and one write |
| `POST /api/flows/run` | fire a manual trigger |
| `POST /api/hook/<path>` | webhook trigger |
| `POST /api/exec` | run one shell command |
| `GET /api/bt`, `/api/bt/gatt?mac=` | the host's Bluetooth adapter and devices; one device's services and characteristics |
| `POST /api/bt/{scan,pair,connect,disconnect,forget,read,write,notify}` | Bluetooth actions; `read`/`write`/`notify` take a characteristic's object path |
| `GET /api/iot/status`, `/scan`, `/boards`, `/devices`, `/cameras` | the fleet, read-only. `/cameras` carries the board's own `now` so a frame's age is not measured against the browser's clock |
| `POST /api/iot/network`, `/network/{up,down}`, `/radio/{on,off}` | the network, through the root helper |
| `POST /api/iot/devices/…/{provision,flash,deploy,command,delete}` | one device |
| `POST /api/iot/groups` | replace every group — named lists of devices, read back with `/api/iot/devices` |
| `GET /api/iot/devices/…/{pins,camera.png}` | what it is doing, and what it sees |
| `POST /api/iot/{enroll,state,event}`, `GET /api/iot/{manifest,commands}` | **device token only** — see "How a device and this board actually talk" below |
| `GET /healthz` | the only unauthenticated route |

### How a device and this board actually talk

Three transports now, two of them in opposite directions. This section
describes the two that every board uses; the third, the held-open socket, is in
§6 and is off unless a device's config says `link: true`.

**The polled routes — the device always starts them.** Every transaction is the device
making an HTTP request to this console on 8787 with its own token in
`X-Device-Token`. This board never opens a connection to a device. There are
exactly six routes a device token reaches (`server.py`, the whitelist in
`do_GET` and the `device_route` line in `do_POST`); everything else is 401.

| | Route | Carries |
|---|---|---|
| GET | `/api/iot/manifest` | what it should be: module names and shas, the deployed flow, the poll interval, the shared tags, whether it has a camera |
| GET | `/api/iot/module/<name>` | one source file, **streamed to flash** |
| GET | `/api/iot/commands?wait=0` | whatever is queued; not parked (§5) |
| POST | `/api/iot/enroll` | once: a one-time enrolment token becomes a device token |
| POST | `/api/iot/state` | every 2s: pins, which nodes fired, ip/rssi/uptime/free, camera state, tag writes |
| POST | `/api/iot/event` | as things happen: kind, level, message, payload, node |

**The camera streams, encrypted.** A camera board opens a second connection
on the link's port, over TLS, with `"role": "stream"` in its hello
(`agent/modules/media.py`). The link server tells TLS from the plain link by
the first byte (`0x16`), refuses a stream in the clear, and keeps streams in
their own table so a board can hold both. The certificate is made once with
`openssl` (`tlscert.py`) and fetched by the board from `GET /api/iot/media`,
signed with its token and carrying the time (TLS checks dates; the board's
clock starts at 2000-01-01). `Fleet.frame()` sends a lease (CONTROL:
`every`, `for` 10s) and returns the newest MEDIA frame, waiting up to 4s for
the first. Only a console without a certificate falls back to the old HTTP
GET to the device on port 8080, which has no authentication.

**There is no channel *to* a device unless it holds a link.** `fleet.push()`
tries the link first and otherwise appends to an in-memory queue the device
collects on its next poll: `reload`, `reboot`, `stop`, `fire`, `set`, `resync`,
`camera-on`, `camera-off`, `tags`. The consequence matters, and is unchanged
for a board that is not on the link — **a device that cannot finish its sync
never reaches the command poll, so nothing you send can reach it.** That is not
a hang to wait out; it needs a reflash over USB. A device on the link is the
one case where a board that is stuck syncing can still be told something,
which is a large part of why the link exists.

Cadence: state every 2s, the command poll asks with `wait=0` because a parked
poll starves the flow, a 1500ms work window between host calls, and a device counts as online if
it has been heard from inside `flows.DEVICE_FRESH` (90s — one constant, read
by both the screens and `{{device.online}}`).

**Auth:** a token at `~/.config/zero2w-console/token` (0600), passed as
`?t=` once or an `X-Console-Token` header; the server then sets a year-long
cookie. An unauthenticated browser gets a self-contained sign-in page; curl and
`/api/*` get a plain-text 401.

**`POST /api/exec` is a remote shell** running as the console's user, usually in `sudo`. The
token is the only thing in front of it. Do not port-forward 8787. `--no-exec`
disables it; `--no-gpio-write` makes the hardware surface read-only.

---

## 3. The seams — where an extension plugs in

These are deliberate extension points, already used more than once.

**Add a node type** → one entry in `flows.REGISTRY` (label, group, inputs,
outputs, `fields`, optional `docs`, optional `showIf`) plus a branch in
`FlowEngine._execute`. The editor builds its palette, ports, inspector fields
and conditional visibility from that same dict, so there is nothing to update
in the UI. 43 types exist. A type marked `runs: "device"` or `"both"` also
needs a device implementation, and a test fails if it is missing: either
`_do_<type>` in `agent/flow.py`, or a function of the same name in the module
`fleet.NODE_MODULES` maps it to, with `flow.PULLED` naming the same module.
Prefer the module — read the memory trap in §5 first, because that choice is
what keeps a board on the network.

**A port** is usually just a name. It can instead be `{"name", "side"}` to pin
it to one edge of the node — `logic.toggle` is the only one that does, so its
two inputs can mean different things — and `label_from` names a config key to
show as the anchor's label, so the canvas says what that input currently does.
`_run_from` passes the edge's `toPort` into `_execute`; every node with one
input can ignore it.

**Add a field kind** → `fieldEl()` in `static/flows.js`. `combo` is a dropdown
plus free text: the field says `options_from`, and `COMBO_SOURCES` in the same
file resolves it from state the editor already holds (`flows`,
`nodes_in_flow`, `devices`). Use it for anything that takes an id — nobody can
retype one from memory.

**Add a variable** → one entry in `flows.VARIABLES` plus a case in
`resolve_variable`. The `{x}` picker and the inspector's library are generated
from it, and `/api/flows/registry` resolves each one's *live* value. 43
exist, and every tag defined on this board is offered alongside them.

**Add a metric** → extend `Collector.snapshot()` in `zero2w_console/server.py`. It flows
automatically to the SSE stream, the `metric.threshold` trigger and the
variable resolver.

**Tags** (`zero2w_console/tags.py`) are the board's shared memory — a name, a
type, a description, a unit. Any flow writes one, any field reads it as
`{{tag.name}}`, and `tag.change` starts a flow when one moves, so a producer
and a consumer never have to be wired together. `retain` writes a tag to disk
so it survives a restart; everything else starts at its initial value.
`TagTable` is built in `server.py` and the engine subscribes once — a flow
arming adds a row to `engine.tag_triggers` rather than another callback.

A tag marked **`share`** belongs to the whole fleet. It rides the manifest to
a device, changes are pushed on the command queue the device already polls,
and a device's own writes come home in the state report it already sends —
no new route for any of it. **A change is never sent back to the device that
reported it**, or the two would hand the same number to each other forever;
that is what the `source` argument threaded through `TagTable.set` and the
watchers is for. A device may only write tags that are shared and already
exist: it cannot invent one or reach one this board keeps to itself.

**Add an example** → a function in `zero2w_console/examples.py` and an entry
in `CATALOGUE`. They are built in code rather than stored as JSON so that
positions are computed and `tests/test_examples.py` can check every node type,
config key, dropdown value and edge against the registry — an example that no
longer loads is worse than no example. They are added **switched off**,
because the pins in one are a guess about someone else's board.

**Add a screen** → a page in `static/`, a route beside `/iot` in `server.py`,
an entry in `nav.js`'s `screens()`, and the page in the tuple in
`tests/test_layout.py`. Two tests hold the rest together: every screen must
carry the same nav, and every href the nav offers must be routed.

**Add a UI component** → `zero2w_console/static/bundle.js`, which is a copy
of the *Auto-Blox* design system's component bundle (namespace
`Zero2W`). If you keep the design system elsewhere, **fix bugs in both or
they drift**.

**Add a bus/peripheral** → `zero2w_console/buses.py` follows one pattern: a class that talks
to the device, plus a `capabilities()` entry that reports honestly what is
missing and how to enable it.

---

## 4. Decisions and why (do not casually reverse these)

- **No framework.** Vanilla DOM factories returning detached nodes. A 4×A53
  should not spend cycles on a render loop.
- **The registry is the single source of truth.** Node definitions and the
  variable library drive both the runtime and the editor, so the two cannot
  disagree.
- **Thresholds come from the kernel.** `cpu-thermal` trips at 60/70/100 °C;
  the other three zones expose a single 100 °C trip and therefore get a
  critical point and *no* warn band. Nothing is invented.
- **The pinout is vendor-sourced.** This board's device tree has **no
  `gpio-line-names`**, so the header mapping cannot come from the kernel. It is
  generated by `scripts/build_pinout.py` from `physToGpio_ZERO_2_W` /
  `pinToGpio_ZERO_2_W` in orangepi-xunlong/wiringOP. 28 GPIO on banks PC/PH/PI.
  **Never hand-write these numbers.**
- **The colour palette was computed, not chosen.** Hand-picked values failed
  the colourblind-separation and contrast checks and were re-derived. All 80
  text/ground pairs hold WCAG in both themes. Re-run the checks if you change a
  value.
- **Unknown `{{variables}}` are left verbatim** in output rather than becoming
  empty, so a typo is visible.
- **Honest capability reporting.** Everything unavailable says precisely what
  is missing and how to enable it, rather than failing silently.

---

## 5. Traps — each of these cost real debugging time

**The radio** — the whole story is in `docs/RADIO.md`, but the one line is:
hostapd works on `wlan0` **only when nothing else holds the radio**. Stop
`wpa_supplicant`, take the interface off NetworkManager, delete the leftover
P2P devices, set it back to station mode. Otherwise the firmware asserts in
`bus_chn_init` and resets the chip, taking Bluetooth with it.

**Devices**

- A device runs its flow from its own flash and keeps running when the host is
  away. The agent starts the flow *before* it tries to join anything.
- **The agent must never block, and a Watchdog is the thing that notices.** A
  long poll that parks stops the flow ticking, and a `safety.watchdog` with a
  shorter timeout than the pause starves on every pass. On a motor that is drive
  for a second and a half, stop for a second, drive again, in a loop — a device
  fighting its own agent. The host used to clamp an idle command poll up to a
  full second against a 400ms watchdog. `COMMAND_WAIT` is 0 now and
  `take_commands` honours zero, so the gap is one round trip; a test holds the
  example's timeout against that constant. **Anything new that blocks in the
  agent has to be measured against the shortest watchdog a flow might carry.**
- **`uptime` was not uptime.** Before agent 0.7.0 it reported `time.time()`,
  which on an ESP32 counts from power-on and keeps counting straight through
  `machine.reset()` — the RTC is not in the reset domain. It was watched going
  80,160 to 80,588 across a reboot that had demonstrably taken effect, and read
  as proof the board had never restarted. From 0.7.0 it is seconds since the
  agent started, on both sides, so a reboot is visible again.
- **A ping proves lwIP is alive, not the VM.** ICMP is answered by the network
  task, so a board with a wedged or busy Python side still replies. It cannot
  tell a crashed agent from a backing-off one — and after the AP dropped, one
  board took **eight minutes** of compounding backoff to return. `uptime` in the
  next report is what settles it.
- **A module arriving wrong used to be installed anyway.** `sync()` renamed a
  download into place without checking the sha the manifest published, then
  recorded the wanted sha — so a truncated transfer became the live module *and*
  was never fetched again. A file edited while the host serves it is enough. It
  is checked now, in chunks, and a bad one is refused without stopping the sync.
- Turn the station's **power save off** (`wlan.config(pm=PM_NONE)`) or every
  round trip waits for a beacon — it was the difference between four seconds a
  frame and one.
- Agent updates go over the air: change the file and send `resync`. The device
  fetches it by sha and, because MicroPython caches imports, restarts itself
  when a module it has already imported changed.
- **A sync is minutes of silence, and silence looks like death.** Measured
  2026-09-24: `agent.py` and `flow.py` together, about 70KB, took **370 seconds**
  before the board reported again — each file is fetched between work windows
  rather than back to back. `Fleet.deploy_cost` says what a deploy would cost
  before you start one, and past 32KB the screens recommend
  `iot-flash.py --flow`, which writes the flow and its modules over USB in
  seconds and leaves the first sync nothing to do.
- **A MicroPython import costs between 1.00 and 2.2 bytes of RAM per character
  of code, and you need both numbers.** 2.2 came from the flow.py incident —
  5,612 code characters cost 12,576 bytes — on a *cold* board with the wifi stack
  allocating around it. 1.00 is the steady state, measured on the motor board on
  2026-09-25: its four handler modules cost **25,072 bytes for 25,126
  characters**. Budget with 1.00 because it is the honest floor, and expect 2.2
  at a cold boot because that is when the failure happens. It is also why the
  link's policy lives in `modules/linkagent.py` rather than in `agent.py`: a
  board not using the link would otherwise pay for code it never runs.
- **A comment costs RAM, because the board compiles the source.** This is not a
  style opinion. `agent.py` grew 5,313 bytes in one session of which **only 723
  were code** — the rest was prose, written while diagnosing the memory problem
  it was making worse, on a board with roughly 19KB of working headroom. The
  prose belongs in this file; `tests/test_agent_budget.py` holds the line.
  Every file but `agent.py` now goes to a capable board as `.mpy` (§6, item
  15), which carries no comments at all.
- **esptool times its own write against the crystal it thinks is fitted.** The
  motor board read `Detected crystal freq 15.44MHz ... normalized 26MHz` on the
  run that wrote it and a clean `Crystal is 40MHz` on the two either side; a
  write at 460800 under a wrong clock assumption can land corrupt, and the board
  then boots erratically or enrols once and vanishes. `iot-flash.py` refuses to
  erase or write on that warning — unplug, replug, check it reads 40MHz.
  `--any-crystal` overrides it for a board genuinely fitted with another.
- A stock MicroPython has **no camera module**. A config with a camera gets a
  camera-capable build automatically; flashing stock over one leaves it blind.

**Flows, and the three that drive motors**

- **A `math.split` with no `floor` will not turn a geared motor.** Under about a
  quarter duty the winding buzzes and warms and the shaft stays put, so the whole
  bottom of the range is a flow that fires every node, reports a duty and does
  nothing. `floor` rescales the live range into floor..scale and leaves nought at
  nought. It was missing, and it is why the motor flow looked broken on the
  chassis: capped at 40% with the dead zone rescaling underneath it, a 0.3
  command arrived as 10.5%.
- **A Ramp belongs after the mix, not before it.** One Ramp on the throttle
  leaves steering unlimited: a steer flicked from -0.9 to 0.9 reversed a wheel
  from 38% duty to 34% the other way inside a single tick.
- **`ramp_toward` stops at zero for one step on a reversal.** That is the
  cheapest dead time there is, and it is what makes "the two halves of a bridge
  are never both driving" structural rather than a property of edge order —
  measured at zero overlaps in all four crossings *and* with the two edges out of
  a ramp deliberately swapped, where before the swap produced one.
- **A reset must not emit.** `math.ramp` used to send its start value downstream
  when reset, and the arm gate's false branch resets it — so *disarming* issued a
  command, the mix added the steering to it, and the wheels drove with nothing
  armed. Reproduced at 20% duty.
- **`>=` against text answered alphabetically, and said yes.** `"off" >= "0.5"`
  is true because "o" sorts after "0", and `str(None)` is `"None"`. So an If
  asking "is this at least a half" opened for any text at all, and a flow gated
  on `drive_armed >= 0.5` armed itself. The ordering operators fail closed now;
  `==` and `!=` still compare as words.
- **Both bridge inputs high is not shoot-through on a BTS7960 pair.** It puts
  both motor terminals at +V, so the motor sees 0V and brakes. Still wrong for a
  flow to do, but not a supply short — this was written down wrongly twice.
- The three motor examples compose through the `drive_armed` tag and nothing
  else: **Motor bench** (device, 11 nodes, one bridge, hand-fired, capped lowest
  because nothing in it can be armed), **Motor drive** (device, 18 nodes, both
  bridges, live throttle and steer, floor 25 and scale 100), **Arm before it
  drives** (host, 7 nodes, writes the tag Drive reads). Seven tests hold them in
  agreement, because a renamed tag would leave Arm writing into nothing and Drive
  permanently disarmed with every node firing happily.
- `drive_steer` is **positive for right**, and turns the same way in reverse:
  yaw goes as (right − left), so the steer term does not depend on the throttle's
  sign. It was the other way round when the flow was written.

**Hardware**

- `gpiochip0` is *r_pio* (32 lines, PL bank). **`gpiochip1` is the main pinctrl**
  (288 lines) carrying the header. The index is resolved at runtime from
  `gpiodetect`; never hard-code it.
- `gpioset` holds a line only while its process lives. A driven output is a
  long-lived process tracked in `PinDriver.held`.
- **Releasing a line does not reset it** — the kernel leaves it an output at its
  last level. `reset_to_input()` runs `gpioget` afterwards.
- Holder processes that die elsewhere leave zombies and a stale ownership
  record; `reap()` clears them.
- Supplementary groups are fixed at process start, so a console started before
  `scripts/setup-gpio.sh` ran keeps the old credentials. `gpio.py` detects this
  and says to restart.

**Browser / layout**

- **`.node` width in CSS must equal `NODE_W` in flows.js.** All anchor, link and
  auto-fit geometry is computed from the constant, not measured. A CSS-only
  override desynchronises everything. There is a check that fails loudly.
- **Never pair `height: 100dvh` with `min-height: 100vh`.** On a phone `100vh`
  assumes the URL bar is hidden and is *taller* than the viewport; as a
  min-height it wins and pushes half the app off-screen under `overflow:hidden`.
- **On mobile `.studio` needs `grid-template-rows: 1fr`.** The drawers are
  `position: fixed` there and leave the grid flow; the remaining item's content
  is absolutely positioned, so an auto track collapses.
- **`.canvas-nodes` must keep `pointer-events: none`** (with
  `.node { pointer-events: auto }`). It covers the canvas and paints above the
  links, so otherwise it swallows every click meant for a link.
- **Never call `renderCanvas()` inside a pointerdown handler.** It removes the
  element that received the event, releasing implicit pointer capture and firing
  `pointercancel` — the drag dies instantly. Toggle classes instead.
- **Never rebuild in a pointermove handler.** Node drags move one element and
  redraw links; link drags update only the ghost path's `d`.
- **Touch devices never fire `dragstart`.** Anything drag-only is unusable on a
  phone; the palette needed a tap-to-add path.
- Full-height drawers need their own close button — they cover the toggle that
  opened them.
- Form controls need **16px** on `pointer: coarse` or mobile browsers zoom the
  page on focus.
- A live log must **append**, never re-render, or it throws away the reader's
  scroll position on every line. `Z.LogStream` + `Z.LogAppend` already do this
  properly: LogAppend measures whether the reader is at the tail *before* it
  adds anything and only follows if they were. The IOT flash log is built once
  and updated in place — `render()` returns early rather than rebuilding that
  tab — because **moving a scrolled element to a new parent resets its
  `scrollTop`**, so even a correct append is undone by a re-render around it.
- `Z.LogRow` reads `r.message`; the flash log's rows carry `r.text`. And a
  `LogStream` built with no entries appends an "empty" paragraph that is never
  removed again.
- **`.z-dock` is a fixed height** in the design system, so anything that makes
  its contents wrap paints outside its own box. `app.css` gives it
  `height: auto; min-height` under 700px, where the item list has to wrap or
  three of the four screens sit behind a sideways scroll nobody would guess at.
- **`.z-widget-sub` is `word-break: break-all`** — right for
  `enx0a1b2c3d4e5f`, wrong for a sentence, which it split mid-word.
  `overflow-wrap: anywhere` serves both and is overridden in `app.css`.
- Drawers must be `visibility: hidden` when closed, not merely translated
  off-screen — a translated pane is still painted and appears when zoomed out.
- Auto-fit floors at 0.85 on narrow screens; 10px node text at 0.55 is ~5px.

**Privilege**

- The console's unit sets `NoNewPrivileges=yes`, so **`sudo` can never work
  from the service** — the message is "the no new privileges flag is set", and
  no sudoers entry changes it. Anything privileged has to be a separate root
  service the console talks to over a socket, which is what
  `packaging/zero2w-iotnet.service` is.
- NetworkManager's polkit refuses this service user the wifi actions outright
  (`enable-disable-wifi: no`), so nmcli is not a way round it either.
- **`systemctl restart zero2w-iotnet` takes the access point down with it.**
  hostapd and dnsmasq are started by that service and inherit its cgroup, and
  the default `KillMode` stops the whole cgroup. It happened on 2026-09-24 while
  installing a helper change, and both boards dropped off.
  `KillMode=process` is in the unit now; **reinstall it and `daemon-reload`
  before restarting that service again.** Bringing the AP back needs no root:
  the helper socket's `up` verb, or the IOT screen's Start.
- **A board takes up to ~8 minutes to come back after the AP does, and that is
  the backoff, not a fault.** The ESP32-Cam returned in seconds; the motor board
  stayed silent for eight minutes and was misread as wedged — its lwIP stack
  answered a TCP probe in 3ms while the Python side said nothing, which looks
  exactly like a wedge from outside. The agent's join retry backs off to 60s and
  the exception path sleeps up to 30s, and those compound. **A ping proves only
  that lwIP is alive — it is answered by the network task, not by the VM — so it
  cannot tell a wedged agent from a backing-off one.**
- **`uptime` did not tell them apart either, and was read as though it did.**
  Before agent 0.7.0 the agent reported `time.time()`, which on an ESP32 counts
  from **power-on and keeps counting straight through `machine.reset()`**,
  because the RTC is not in the reset domain. It was watched going 80,160 →
  80,588 across a reboot that had demonstrably taken effect. So "uptime is 21
  hours, therefore it never restarted" was wrong: it only ever meant the board
  had not lost power. From 0.7.0 `uptime` is seconds since the agent started,
  which is what the variable table always claimed, and a reboot is visible in
  it again.
- **A resync that pulls two large modules can cost six minutes of silence, not
  the ~137s recorded here.** Measured 2026-09-24 pulling `agent.py` and
  `flow.py` together: 370s before the board reported again. It is not a hang.

**The fleet's screens**

- A camera tile's age must be measured **where the frame lands**
  (`swapper.age()`), not from the camera list — that list is re-fetched every
  15 seconds, so an age computed from it climbed to 15 and snapped back.
- `online` in the camera list is a 15-second-old fact about the *device*. A
  tile can say "live" over a picture from minutes ago unless the age is
  checked too.
- `iw dev <iface> station dump` **needs privilege and comes back empty without
  it**. An empty dump is not evidence that nothing is associated, and must not
  be rendered as "no" — the device view says it cannot tell.
- The enrolment window is opened by `provision()`, which the flasher calls near
  the *start* of a flash that takes minutes. It is opened again, for longer,
  when the flasher exits 0 — that is when the board is about to boot and ask.
  Refusals are recorded (capped) so a board that arrives late is visible
  instead of silent. The window is in memory and does not survive a restart.
- `/api/iot/devices` returns the whole stored record minus a blacklist
  (`iot.public`), so everything a device reports is already on the wire. The
  pin map needs one extra call: `/api/iot/devices/<id>/pins`.

**Memory on a device — the one that takes a board off the network**

Read this before adding anything to `agent/flow.py`. It is the most expensive
thing in this file.

- **`flow.py`'s size is a wifi budget, not a flash budget.** It went from
  31,583 to 38,812 bytes and that cost the board 12.5KB of RAM: 108,016 free
  became 95,440. On a **cold boot** the wifi stack then could not allocate —
  `wifi nvs cfg alloc out of memory`, then `Failed to deinit Wi-Fi driver` —
  after which the board never joined, never synced, and **could not be told to
  stop running the thing that had filled the memory.** It came back over a
  cable. There is nothing subtle about the failure once you see the serial
  console, and nothing visible about it from the host.
- **The boot order is radio, network, compile, then the flow — and every step
  of it was bought by a board that stopped.** `run()` does, in order:
  `radio()` (activate the interface, which is the allocation that fails first
  when memory is short); `connect()` twice (associating allocates again, and the
  flow competes for a rail this fleet's motor board has little of); `gc.collect()`
  (joining leaves transients and nothing compacts); `import flow` if a flow is on
  flash; and then **nothing** — the flow itself starts in the loop, after the
  first sync, or after `FLOW_GRACE` seconds if there is no host. Do not reorder
  these, and do not move the flow start back into `run()`.

  The two that are easy to get wrong:

  - **Joining is not the same as having a radio.** Activating the interface and
    *associating* are two allocations, and the second used to happen a whole work
    window after the flow had loaded. The board came up with a radio it could not
    use, joined nothing, and so could not be told to stop running the flow that
    was the reason. **Zero association attempts reached the access point** — from
    the host that is indistinguishable from a dead board.
  - **`import flow` is the expensive step, not the Runner.** `flow.py` is ~34KB
    of source and MicroPython compiles it on the device, so the import wants a
    large *contiguous* block. The device log caught it both ways in five minutes
    on a **two-node LED flow**: it failed with **89,456 bytes free** and worked
    with **107,744**. That is fragmentation, not exhaustion, and with two nodes
    deployed it cannot be the flow's own size. The import is cached, so it is only
    ever expensive once — doing it at boot spends memory a running flow would
    spend anyway, at the one moment the heap can still find the room.

  A flow that will not start is caught rather than fatal (`Agent.try_flow`),
  because all of this is upstream of the command poll: raising here leaves no
  route by which anyone could ask the board to stop running what stopped it.
- **A handler in a pulled module is resolved on the first message, not at
  import.** `Runner._pulled()` plus `flow.PULLED` is what keeps a cold boot
  down to compiling `flow.py` alone; `modules/blocks.py` arrives later, by
  which time the radio is long up. So *when* something is compiled matters as
  much as how big it is.
- **Stopping a flow does not give the memory back.** MicroPython caches
  imports, so a board with its flow switched off sits at 102,448 free against
  the 122,832 it had before anything was ever loaded. Only a reboot reclaims
  it. Anything planning to "stop the flow to free RAM" — pairing Bluetooth,
  say — does not work.
- **`flow.PULLED` and `fleet.NODE_MODULES` must agree.** One says where the
  device looks, the other says what it is sent. A type in one and not the other
  is a handler the board goes looking for and never received.
- **A missing handler stops the branch; it does not pass the message on.** It
  used to, and that turned a Watchdog into a plain wire — the bench flow fired
  its motor-stop nodes on every message. A node whose job is holding something
  back until a condition cannot fail open.

**The agent, and how it fails silently**

These three all present the same way: a board that flashes, enrols, reports
itself healthy every two seconds, lists the right nodes — and does nothing.
Anything that asks "is it alive" says yes. Do not trust that.

- **A module is fetched straight to flash, and must stay that way.** It used
  to be gathered with `raw += chunk`, which builds a whole new bytes object
  every 512 bytes: pulling the 31KB `flow.py` peaked at **253KB held**, eight
  times the file, on a board with about 124KB free once the radio is up. It
  raised, never set `synced`, and went round again — fetching the manifest
  often enough to keep its last-seen fresh and look fine. `flow.py` was 15KB
  when that last worked. There is a size budget in `tests/test_agent_fetch.py`;
  if a file needs to pass it, split it the way `modules/` already works rather
  than raising the number.
- **Editing a device file makes every board re-fetch it, and one of them
  cannot.** On 2026-09-26 the narrative comments came out of the device files.
  Every sha changed, so the manifest told both boards to pull everything. The
  camera, with 4MB of PSRAM, took the two 30KB files in a second each. The motor
  board pulled the 0.7-8KB handler modules and timed out on `agent.py` and
  `flow.py` every single time: a supply that cannot hold a 30KB transfer up
  while four PWM channels drive two H-bridges (§6, item 17). **The two big files only
  arrive reliably over the cable** — `iot-flash.py --config-only --flow <id>` —
  so a device-file change and a bench visit are the same event. The fetch now
  backs off for `FETCH_RETRY` and says so, rather than spending the board's
  whole life in a download it cannot finish; `tests/test_agent_fetch_backoff.py`.
- **One Enable click used to restart the flow twice.** `save()` redeploys a flow
  the device is already running and the console pushes its own reload because
  `enabled` changed — both honest, both arriving. Stopping a flow gives nothing
  back on MicroPython, so each click cost about 14KB, and the log read `starting
  flow_motor1, 44400 free` then `starting flow_motor1, 29616 free` a second
  later. Four clicks took a working board to a MemoryError. `try_flow()` now
  compares the manifest's `flow_sha` and a repeated reload is a no-op;
  `tests/test_agent_reload_once.py`. Idempotence belongs on the device, because
  a reload legitimately comes from two places and `reload` is retryable.
- **`Runner.__init__` must not assign a name twice.** `self.timers` was set to
  a list for the interval triggers and then to a dict for the Timer node's
  state. The dict won, `start()` called `.append` on it and threw — *after*
  setting `running = True` on its first line. Every device flow with an
  Interval trigger was dead, while the board still answered a manual fire.
  `tests/test_agent_runner_state.py` walks `__init__` and fails on a repeat.
- **Only a reboot applies new code to a module that is already imported.**
  MicroPython caches imports, so a re-fetched `flow.py` changes the file on
  flash and nothing else. `sync()` therefore resets the board itself after
  pulling such a file; a board that could not fetch it keeps running the old
  code, and says so.

**Flows and the fleet**

- **`feeds_the_screen()` accepts two shapes on purpose.** A `camera.publish`
  node is the wired way to ask for a tile; a bare `camera.feed` with its old
  `display` setting is what every flow written before that node existed looks
  like — including the one running on the bench camera. Deleting the second
  branch takes a working camera off the wall.
- **`logic.toggle`'s first input is named `in` for a reason.** Every edge ever
  saved carries `"toPort": "in"`, and that input's default action is the flip
  the node has always done. Renaming it orphans every existing edge.
- **A device chooses its own event `kind`, and it rides the same SSE channel
  the engine's own rows use.** The editor tells them apart by `device` being
  absent, not by the kind alone — a device could send `kind: "output"`.
- **The engine and the fleet are bound to each other after construction**
  (`server.py`, beside `fleet.self_url`), because each needs the other and
  neither can be built second. Both default to `None` so either can be built
  alone in a test.
- **A flow bound to a board must not be walked here.** `_run_from` refuses one
  and `POST /api/flows/run` sends a `fire` command to the device instead.
  Running it on the host executes the device's graph against this board's
  pins, and the nodes that only exist on a device — a camera feed — pass
  silently through doing nothing, which looks exactly like success.
- **A Toggle's first flip moves it off `start`** (since 2026-09-27; before
  that it reported the start value, so one press armed nothing). A flow saved
  under the old rule and relying on a silent first press now acts on it.
- **`enabled` only reaches a board because something pushes it.** The field has
  always ridden the manifest inside the flow document and nothing on a device
  read it, so a flow switched off in the studio went on running from flash with
  the board still reporting it as its flow — which reads as the switch doing
  nothing rather than as a bug. `Agent.start_flow()` honours it now, and
  `Fleet.reconcile()` compares the document before and after a save and pushes
  a reload to any device running a flow whose switch moved. Only the switch:
  every edit restarting every board is a different feature.
- **The report carries `running`,** because deployed-and-off and
  deployed-and-broken otherwise both look like a flow with no nodes and no
  pins. `report_state()` has a field whitelist, so a new report field has to be
  added there or it is dropped on arrival.
- **A `reload` takes the board off the air for over a minute.** Measured at
  ~137s between the last report before a reload and the first after it — sync,
  stop, restart. It recovers on its own. Do not diagnose a dead board inside
  that window; that mistake was made twice in one session.
- **An enrolment window can close during the flash that opened it.**
  `provision()` opens 300s and runs at step 2 of `iot-flash.py`, which is also
  where the passphrase prompt waits — then comes the erase, a write of a minute
  or two, and a cold boot. The board then joins, asks to enroll, and is refused,
  which from outside is indistinguishable from a failed flash, so the flash gets
  run again and fails the same way. The script re-opens the window with
  `AFTER_FLASH` once the files are written; the IOT screen's flasher always did.
- **The AP passphrase is root-only 0600 on purpose** and `iot-flash.py` reads it
  from stdin with `--psk -` so it never reaches `argv`. Do not work around the
  file permissions; run the script where there is a TTY.

**Bluetooth**

- **`busctl --json` cannot read a Bluetooth tree, and looked like it could.**
  `ManufacturerData` is `a{qv}` — a dictionary keyed by uint16 — and a JSON
  object's keys must be strings, so systemd's dumper answers `Failed to dump
  DBus message to JSON object: Invalid argument`. It therefore works perfectly
  on an adapter with no BLE devices around it and fails on every tree worth
  having. Measured with 32 devices present: 0 bytes and exit 1, against
  `gdbus`'s 24,940 bytes of correct GVariant. Hence
  `zero2w_console/gvariant.py`, a small parser rather than a D-Bus
  implementation.
- **On MicroPython 1.29 for ESP32, `gc.mem_free()` is not the heap's free
  space.** The heap starts at 56,000 bytes and grows on demand
  (`MICROPY_GC_SPLIT_HEAP_AUTO`), and `mem_free()` reports its free space
  *plus* the internal RAM it could still grow into (`micropython.mem_info()`
  calls that "max new split"). Measured on the motor board with the agent,
  flow and modules loaded: heap total 56,000, used 45,008, free 10,992 — and
  `mem_free()` said 92,912. Growth is not proportional: when an allocation
  fails after a collection, `gc_try_add_heap` (py/gc.c) adds an area as large
  as the whole heap so far, capped by the largest free block of internal RAM,
  and never gives it back. On that board the first growth takes ~59KB. So
  every "free" figure before 2026-09-27 overstated the room, and room for
  anything allocated outside MicroPython (WiFi, BLE) has to be judged by the
  report's `idf_free`.
- **BLE costs about 33KB of internal SRAM, and only a board without PSRAM
  shows it in `gc.mem_free()`.** On the CAM, across `BLE.active(True)`,
  `idf_heap_info(HEAP_DATA)` free went 4,262,163 → 4,228,999 while
  `gc.mem_free()` did not move, because its MicroPython heap is in PSRAM and
  every other memory figure here is a `gc.mem_free()` figure. On the motor board it
  does, and it was measured there: 33,528 bytes, visible to `gc.mem_free()`
  too, because that board's MicroPython heap shares internal SRAM. Judge room
  for it by the report's `idf_free`, never by `gc.mem_free()`, which counts
  heap grown out of internal RAM as free (§6, item 19).
- **A BLE subscription lasts only as long as the process that asked for it.**
  BlueZ ties StartNotify to the caller's D-Bus connection, and a `gdbus call`
  exits at once — measured, a monitor on the characteristic saw `Notifying`
  go true and straight back to false, and no value ever arrived. (An earlier
  note here read that as "BlueZ leaves Notifying false while values arrive";
  that was wrong.) `gatt.hold_notify` keeps a `bluetoothctl` session open for
  the subscription's life; the values themselves come from `gdbus monitor`,
  which must run under `stdbuf -oL` or it prints nothing until its pipe fills.
  Proven against `scripts/ble-peripheral.py`: one notification per second, as
  sent.
- **A device's name can change once it is connected.** BlueZ swaps the
  advertised name for the one the device reports over GATT: the peripheral
  fixture advertises `zero2w-gatt` and becomes `MPY ESP32`, MicroPython's
  default, as soon as it connects. The BLE nodes remember the address a name
  first matched; an address in the Device field avoids the question.
- **BlueZ forgets a device it discovered but never paired.** As soon as the
  discovery session ends, `bluetoothctl pair <mac>` answers `Device ... not
  available` — measured here after a scan that had just listed 27 devices. So
  pairing cannot be three separate `bluetoothctl` calls: it is one session that
  holds `scan on` open across the `pair`, with `agent on` registered because a
  scripted pairing has nobody to answer a prompt. `zero2w_console/bluetooth.py`,
  `tests/test_bluetooth.py`.
- **A controller only advertises while it is in pairing mode.** A pad that is
  merely switched on, or already connected to a console or a phone, does not
  appear in a scan at all — and a scan of a normal room returns twenty-odd
  bulbs, beacons and televisions, so "found nothing useful" and "found nothing"
  are different messages and the screen says which.
- **A DualShock 4's axes are not where xpad's are.** The kernel gives Sony's
  layout: ABS_Z/ABS_RZ are the right stick and ABS_RX/ABS_RY the triggers.
  Read the standard way, `right trigger` is the right stick's Y, which rests at
  **0.53** — half throttle the moment a flow arms. `pad.py` picks the layout by
  vendor id (`054c`, which the kernel writes unpadded as `54c`) and otherwise
  by where each axis rests.
- **Neither a name nor `Bearer.LE1` says what a device is.** Matching names
  called an IR remote ("ACI-UniversalController") a gamepad; BlueZ's `Icon:
  input-gaming` is the answer. BlueZ 5.85 lists `Bearer.LE1` on every device,
  the Classic DualShock included; only a Classic-capable device has a `Class`.

**Serial, on this board**

- **A long read over the CP2102 does not survive.** Reading 4MB of an ESP32's
  flash failed three times: once at 460800 with serial noise, then twice at
  115200 with "Corrupt data, expected 0x1000 bytes but received 0x10 bytes".
  Nothing else held the port and dmesg was clean — the adapter shares a hub
  with the ethernet. What worked was reading in 64KB chunks, retrying each,
  and keeping them in a work directory so a re-run resumes rather than
  starting over. That is a one-off tool rather than part of this project, and
  lives outside the repo.
- **Back a board up before flashing it.** A compiled Arduino sketch cannot be
  turned back into source, and the image is the only copy there will ever be.
- **Reading the flash over the REPL leaves the board at the REPL.** `RawREPL`
  stops `main.py` to get there, and `repl.exit()` drops to the friendly prompt
  rather than re-running it. A board that has just been inspected looks exactly
  like one that has crashed: on the network, answering pings, reporting
  nothing. Send `\r\x03` then `\x04` to soft-reset it back into the agent, and
  expect to be fooled by this at least once.
- **Decode the line before concluding anything.** 305KB of high-bit garbage in
  38 seconds reads as a boot loop and is not one — it was a saturated line at a
  mismatched rate. Linux has no `B74880`, so arbitrary rates need `termios2`
  with `BOTHER` (see the scratch scripts in the session log); 115200 is what
  this board actually talks at, and at that rate the OOM above was one legible
  line.
- **The motor board is not USB-powered.** It runs from its own 12V→5V buck and
  USB is only for flashing, so a `USB disconnect` in `dmesg` does not mean the
  board died — and a board that *has* died is not explained by one. Two
  disconnects in this session were misread as crashes before the timestamps
  were checked against the board's own uptime.

**Process**

- `pkill -f <pattern>` matches the *shell running it* if the pattern appears in
  its command line. This killed the session five times. Match on
  `pgrep -x python3` plus an exact cmdline check.
- **A second console started for testing shares `~/.config/zero2w-console`**,
  so it writes the *live* flow document — and `scripts/sim-device.py` posts a
  flow document with no `rev`, which is a blind overwrite. That is how four
  real flows were lost in one command. Give a test instance its own config:
  `HOME=/tmp/somewhere python3 -m zero2w_console --port 8788`. Every path is
  resolved from `expanduser("~")`, so that is all it takes.
- **Python changes need a restart; static files do not.** A page reload picks
  up JS and CSS from disk, but a new route or a registry change needs
  `sudo systemctl restart zero2w-console.service`. A half-restarted console
  looks like a UI bug: the editor asks for `/api/flows/outputs`, gets a 404,
  and quietly shows an empty panel.
- Static files are served from disk, so **UI changes need only a page reload**;
  only Python changes need `systemctl restart`. Say which is which.
- `scripts/check-js.py` proves every static script compiles. **A page whose
  script throws on line one compiles perfectly and renders nothing**, so
  `scripts/check-pages.py` drives the real pages in Chromium — desktop and
  phone — and fails on any exception, console error, missing nav, sideways
  overflow or tab that does not follow its hash. It needs a listening server,
  so it is a script rather than part of `unittest discover`:

  ```
  python3 -m zero2w_console --port 8788 --no-auth --no-exec --no-iot-net &
  python3 scripts/check-pages.py http://127.0.0.1:8788 --shots=/tmp/shots
  ```

  It found three real bugs the compile check could not see.
- Chromium headless at a narrow window is **not** a phone. Use a mobile UA and
  `--force-device-scale-factor=2`; real bugs only appeared under that.
- Flow saves post the **entire document**, so every save is a whole-document
  write. A `rev` in the document makes that safe: the editor sends the revision
  it loaded, and the server answers **409** with the current document rather
  than letting a stale tab overwrite it. A body with **no** `rev` is trusted —
  that is how a restore from `backups/` works, and it is the one way to
  overwrite blind.

---

## 6. Current state

**Working and verified on hardware:** GPIO read and write, software PWM, I2C
access, hardware PWM channels writable, the full flow engine, webhooks,
variables resolving live readings, the dashboard, the studio.

**The IoT side, end to end, on real hardware:**

- `wlan0` hosts an access point (`hostapd` + `dnsmasq`), addressed `10.42.0.1/24`,
  firewalled so a device reaches the console and the internet but not the LAN.
- An **ESP32-CAM-MB** runs MicroPython 1.27 with a camera module, flashed from
  this board over USB, and holds a DHCP lease on that network.
- It enrolled with its own token, pulled the agent and the modules its flow
  needs by sha, and runs the deployed flow **from its own flash**.
- It reports every two seconds: pin levels and directions, which nodes fired,
  its address, signal, free memory and camera state.
- The dashboard shows its camera as a live tile at about one frame a second;
  the studio flashes its nodes as they run and shows its pins in the inspector.
- Agent updates go **over the air** — six versions were pushed to it without a
  cable.

**The node library, rebuilt.** 51 node types (42 runnable on a device), 50
variables, 17 examples. `docs/NODE-LIBRARY.md` holds the brief, what is built and
what is not. Those four counts are checked against the registry by
`tests/test_docs_agree.py`, because a document that states a number nothing
verifies is a document that is wrong within the week — which is why the test
count is *not* quoted here: run the suite.

- **Editor.** The flow name keeps the caret; both ids have a copy button; the
  theme switch is in the nav bar, once, for every screen. Ids are picked from
  dropdowns rather than typed. Every node declares the shape it emits and the
  inspector shows it with the last real value beside each field.
- **Blocks**, the pieces a sensor graph needs: On change, Counter,
  Hysteresis, Scale, Smooth, Timer (on-delay, off-delay, pulse) and Formula.
  Timer and Delay run on a scheduler rather than sleeping — a Delay used to
  stop the whole engine for its duration.
- **Tags** — shared memory with a table on the flows screen; `share` puts one
  on every device, and a device's writes come home on the report it already
  sends.
- **Both directions to a device.** Device event / Tell the host up, Device
  command down, Deploy and redeploy-on-save from the toolbar, `{{device.x}}`
  naming the device a flow is on rather than numbering it.

**Verified on the real fleet, 2026-09-22:** a second board (ESP32-D0WD,
`dev_ef56ab12`) flashed from this console, enrolled, pulled its modules,
and ran the Heartbeat example from its own flash — blinking GPIO2 and
reporting a climbing count that a separate host flow recorded in a tag.
That exercised the deploy, the command queue, the state report, `host.notify`
into `host.event`, and the tag table, end to end.

**The drive blocks, 2026-09-23.** Dead zone, Ramp, Sign split, Latch and
Watchdog, all `runs: both`, with the arithmetic in `modules/drive.py` so the
host and a device cannot disagree about a duty cycle. Sign split is what lets
one library drive an H-bridge, a servo or a stepper without a motor node: size
and direction for a PWM+DIR board, forward-only and back-only for the two
inputs of a bridge, and a test asserts across the whole input range that those
two can never both be driving. `pwm.out` on a device was fixed at the same
time — it could not stop a motor and could not take a duty from upstream.

The board those were written against, `dev_ef56ab12` ("ESP32 Motor"), is an
ESP32-D0WD in a printed case with a 20V→12V buck-boost and a 12V→5V converter,
driving **two BTS7960 half-bridge pairs**: D27 and D25 are the right motor's
L_PWM and R_PWM, D33 and D32 the left. **The bridge enables are tied high in
hardware**, so there is no line to drop — stopping goes through the PWM pins,
which is why `action: "stop"` releases the channel *and* drives the pin low.
D14 and D26 are spare outputs behind opto-isolators, wired to nothing yet. Its
previous firmware was an Arduino sketch, "EVA-AGV v2", backed up whole to
`~/Documents` before reflashing; the image holds its web UI and UDP control
protocol but **not its pin numbers**, which were compiled to immediates.

**The link, carrying both boards, 2026-09-25.** `link.py`,
`agent/modules/link.py`, `agent/modules/linkclient.py` (transport) and
`agent/modules/linkagent.py` (policy): a device dials in, the socket stays open,
and every frame carries a MAC over its own header and body with a counter that
must advance. Mutually authenticated — `READY` is sealed with the session key, so
a rogue access point cannot impersonate this console to a board and drive it.
Both `dev_ab12cd34` and `dev_cd34ef56` held one, `state: ready`, hundreds of
frames each way, zero failed dials, before it was switched off (below). **0.230ms host→device is still a loopback
number** — nothing timed has crossed the real link yet, and taking that
measurement is the first thing step five makes possible.

Four of the five cutover steps are done. `server.py` starts the listener on
8789; `agent.py` dials out after a sync and polls the client from `serve()`;
`fleet.push()` takes the link when a device holds one and falls back to the
queue; and the device's own reports and events take it too, with the polled
route as the fallback and the **command poll skipped entirely while the link is
ready** — which is what actually removes the two to three seconds. Step five's
camera half is done: pictures stream over TLS and port 8080 stays shut
wherever the console has a certificate. Deleting the old polled routes is what
is left.

Two things that had to come with step four. The host has nothing to answer once
reports arrive unasked, so it never pings, and a dead host would leave a board
writing into a socket TCP holds open for minutes — the agent now asks after 20s
of silence and gives up 5s later. And because the policy around the client is
some 2,300 characters of code, it lives in `modules/linkagent.py` and is pulled
only by a board whose record says `link: true`; a board that never links does
not pay ~5KB of RAM for it.

**It is off per device until switched on** — `link: true` in the device config,
the button on the IOT device card. The three modules are about 21KB of source and
an import on MicroPython does not give the memory back before a reboot, which on
a 100KB board is a decision rather than a default. Flipping the switch pushes a
`reload`, because a device only reads the manifest when it syncs.

Which files a device pulls follows **the device record, not whether a listener is
running here**. Those are different questions — the record says what kind of
board this is, the manifest's port says whether to dial today — and keying the
file list on both meant the flasher, which has no listener of its own, wrote a
different set than the manifest asked for, so a board flashed "with everything"
still had 29KB to fetch.

**The console remembers what it has served each board**, on every module fetch
and on a flash (`Fleet.note_modules`, `device["modules"]`). That is what lets
`deploy_cost` charge only for what is missing or changed; counting everything
every time answers "use the wire" for a two-node blink, which is a guard nobody
keeps. The record is a hint: a file it does not mention
is counted as one the board must fetch, so it errs towards recommending the
wire.

**A command in doubt goes back on the queue.** A frame the link accepted and a
socket that then died used to add up to a lost command. A send is now held for
`INFLIGHT_GRACE` seconds and requeued if the connection ends inside it —
**only the idempotent ones**, listed in `fleet.RETRYABLE`. `fire` injects a
message into a running flow, so a duplicate is a second message and losing it
is the lesser fault; either way it is said out loud on the bus. Device to host
is still at-most-once, which is what the polled route always was.

#### Four things the board found that CPython could not, 2026-09-24

The client ran on the ESP32-Cam and never left `connecting`. Read this before
debugging anything socket-shaped on a device — none of the four was visible
from the host, from the tests, or from reading the file.

1. **The errnos were the Linux ones.** MicroPython on ESP32 is built against
   newlib, where `EINPROGRESS` is **112** and `ENOTCONN` is **128**, not the 115
   and 107 Linux uses. `_again()` now asks the runtime's own errno module.
2. **The error message was the second error.** `poll()` read after flushing
   without checking whether the flush had ended the connection, so
   `AttributeError: 'NoneType' object has no attribute 'recv'` overwrote the
   real reason in `dropped`. The only clue was the failure counter going up in
   twos. **A polled state machine that swallows everything must keep the first
   thing it swallowed.**
3. **A premature write answers `ECONNABORTED`, not "not yet".** So no errno list
   can tell a socket still dialling from one that was really aborted, and
   `_dialled()` asks `select.poll(0)` instead of finding out by failing.
4. **The firewall was rejecting it, and that is what ECONNABORTED really was.**
   The AP's isolation rule names the ports a device may reach unprompted and
   REJECTs the rest on `wlan0`; a REJECT arrives at a board as ECONNABORTED. The
   link port is named in `packaging/iot-netctl` now. **Anything new a device is
   meant to reach on this host has to be added there, or it will look like a
   MicroPython bug.** `iot-netctl firewall` reapplies the rules without bouncing
   the radio.

**And the helper had drifted.** `hostapd_conf(reduced=True)` existed only in the
installed `/usr/local/sbin/iot-netctl`, hand-edited on 2026-09-20 to get the AP
up and never committed, so for four days the repo copy would have taken the
access point down if installed. The repo is now a superset with tests for the
fallback. **`/usr/local/sbin/iot-netctl` is a copy, not the original — check
`diff` before installing over it.**

**Verified on the bench, 2026-09-23:** switching `f_heartbeat` off in the studio
stopped it on `dev_ef56ab12` with nothing rebooted by hand — `reconcile` pushed
the reload both ways, the board reported `running` flipping, and the beat
counter froze. Free RAM with that flow running went from 95,440 before the
runner was split to 102,448 after.

#### Three bugs from one bench report, 2026-09-25

The motor flow drove without the arm, and then drove-paused-drove in a loop. All
three were reproduced before anything was changed, and the third was found by
sweeping what the first implied rather than by being reported:

1. **`math.ramp` emitted when reset** — so disarming issued a command. See the
   flow traps in §5.
2. **The watchdog was faster than the agent's own blocking gap.** See the device
   traps in §5.
3. **`drive_armed` set to `None` or the text `off` armed it**, because `>=` fell
   back to comparing as words. See §5.

The lesson worth carrying: a bug reported at the bench is a *sample*. Sweeping
every tag value someone might type found a worse bug than the one reported.

#### What the fleet looks like now, 2026-09-27

- `dev_ab12cd34` "ESP32-Cam", esp32cam, MicroPython 1.27, agent 0.9.1 with
  everything compiled, **link on** for latency measurements, ~4.1MB free (PSRAM, which
  hides the internal-heap cost of anything — do not measure memory on this
  board). It upgraded itself over the air: pulled agent.py, restarted, asked
  with `v=0.8.1`, pulled seven `.mpy` files in five seconds, restarted again,
  and served frames as before; then the same for 0.9.0's compiled agent.
- `dev_cd34ef56` "ESP32 Motor", esp32, MicroPython 1.29, agent 0.9.1 with
  everything compiled, **link off**. Installed with `iot-flash.py
  --code-only`, then upgraded over the air on USB power to 0.8.1, 0.9.0 and
  0.9.1, restarting itself each time.
  `gc.mem_free()` running the drive flow read 48,592 on `.py` and 48,160 on
  `.mpy` — the same, because it counts heap grown out of internal RAM as free.
  *Internal ram free* (`idf_free`, 0.8.1) read 26,492 with the flow running:
  the figure that decides whether BLE fits (item 19). **Note the id: the earlier
  `dev_ef56ab12` was replaced by a fresh config**, so anything keyed to the old
  one has nothing to show.
- **The link is off on the motor board**, by choice. It costs ~39KB of a
  100KB heap — a third of it — for a transport, and the failures it was blamed
  for turned out to be elsewhere. `link.py`, `linkclient.py` and `linkagent.py`
  are kept and tested; nothing pulls them while `device.link` is false. Turning
  it back on is one field per device.
- The motor flow **runs**: 18 nodes, stable for the best part of an hour at a
  time, all four bridge pins driven, `drive_armed` gating it. Getting there is
  most of §5's device half.
- **`drive_armed`, `drive_speed` and `drive_steer` are shared tags**, handed to
  a booting board at the fleet's current values. `drive_armed` is the one that
  must not be: see open item 16.

**Flashed and enrolled are different facts.** Provisioning clears `enrolled` on
purpose, because the old token is void the moment a new image is written, so a
board flashed a minute ago is flashed and not yet enrolled. One `life()` in
`iot.js` decides a device's state, and the Setup step reports both. Related: `/api/iot/devices` redacts `token` and
`enroll_token`, so reading them as `None` there means nothing — check the store.

**SPI1 cannot reach the header with the stock overlays.** `spidev1_0/1/2`
switch SPI1 on, but neither they nor this board's device tree assign it a pin
group (read with `dtc`; the Armbian fixup script adds none either), so the
controller would come up with no pins. The studio's header-bus picker says so
rather than offering it.

**Not available until someone edits `/boot/armbianEnv.txt` and reboots:**
`overlays=` is empty, so no header I2C bus, no `/dev/spidev*`, and no PWM pin
mux. **SPI has never run against real silicon here.**

**Open items:**

1. Nothing off-LAN can reach the board unless you add a VPN (Tailscale,
   WireGuard). Never port-forward 8787: `/api/exec` is a shell.
2. The terminal runs one command at a time, not a PTY — no `htop`, no `vim`.
3. MQTT is QoS 0 only; no retained replay, no persistent sessions.
4. **JPEG works on camera firmware v0.6.0, and not after it.** v0.6.1 and
   v0.6.2 of micropython-camera-API fail in the constructor ("Failed to
   capture initial frame") for an OV3660 or OV5640 in JPEG, whatever the
   settings (upstream issue 55); 0.6.0 captures in 36 ms. The flasher pins
   0.6.0 and caches it by tag, because every release names its asset alike.
   Measured on the CAM, 2026-09-27: a 640x480 JPEG is 7-17KB; over the TLS
   stream with *How often* at 100ms the board delivers 6.9 new pictures a
   second (median 145ms apart) and the wall shows them 11ms after asking;
   the link's round trip goes from a 26ms median to 43ms while it streams,
   and internal RAM free from ~135KB to ~88KB. Before: 160x120 greyscale,
   19KB raw over plain HTTP, 0.47 a second. What caps it at ~7 a second is
   not measured; making the old raw sends non-blocking measured no faster.
   **Unexplained, once:** after the first deploy of this, the CAM went silent
   for three minutes without fetching anything, until reset over USB. It
   logs to the console rather than serial, so nothing showed why.
5. **The runner and the agent's whole loop are executed in tests now, against
   stub hardware.** `tests/test_agent_loop.py` drives the real `run()` against a
   host stubbed at the socket — including a first sync that pulls the actual
   `agent.py`, `flow.py` and four modules through the streaming path and the sha
   check. Nothing covered `run()` before, which meant a mistake in the most
   load-bearing path on the board did not fail a test; it produced a board that
   answered nothing and came back over a cable. Three things that harness had to
   learn are in its docstring, and the shortest is: **the sentinel that ends the
   loop cannot be an `Exception`, because `run()` catches those and carries on.**
   The older note still stands: a
   recording `machine.Pin`/`machine.PWM` in `tests/test_agent_pwm.py`, reused
   by `tests/test_drive_blocks.py`, which runs the shipped Motor bench example
   end to end and checks a duty reaches the pins and the watchdog takes it
   away. That is what caught the fail-open dispatch. What the stubs cannot
   model is the thing that has actually bitten hardest: **memory**. Every
   RAM failure in this project was found by a board on a bench, not by a test,
   and `scripts/check-pages.py` drives the real pages without clicking
   anything.
6. **A board flow's Output panel shows what its nodes last sent.** The runner
   keeps the last message per node, clipped (payload to 40 characters, six
   meta keys), and the report carries the ones that changed. The console
   marks those rows `from_report`, because a device names its own event
   kinds and `kind: "output"` alone could be one of them.
7. **New code for a running module restarts the board.** `sync()` resets the
   board after pulling a file whose module is already imported, since an
   import is cached. A module not yet imported just lands.
8. **Tag change on a device polls, once per tick.** A tag written and written
   back between two ticks is never seen to change. Good enough for anything a
   person or a sensor does; not an event bus.
9. **The link itself is signed, not encrypted.** Its frames carry a MAC under
   a key from the board's token, so nothing can be forged, but commands,
   tags and state cross the AP readable by anyone holding the AP passphrase
   (WPA2-PSK). Pictures are encrypted (item 4). Moving the link onto
   the same TLS is the follow-up; the certificate is already there. The
   agent's own HTTP to the console is plain too, and carries its token.
10. **The link's round trip, measured: median 26ms.** Twenty pings to the CAM
    on 2026-09-27 — min 17.4, median 25.9, p90 38.7, max 55.4ms — against a
    polled command's wait of up to 1.5s. `POST /api/iot/devices/<id>/ping`
    (the device card's *Ping over the link*) times a PING to the board's PONG,
    answered from the agent's loop, so it is the radio and the VM together.
11. **`agent.py` has a budget of its own** — 32,000 bytes, in
    `tests/test_agent_budget.py`. On a board that compiles its own source a
    comment is heap, so the prose lives in this file and the code lives there;
    taking it out of the device files gave back about 11KB.
12. **Nothing acknowledges a report, and nothing acknowledges a command.**
    At-most-once in both directions, which is what the queue always gave. An ack
    frame with the host requeuing on a drop would make it at-least-once; whether
    the duplicate that implies beats the loss it replaces is a real question and
    not one to answer by accident.
13. **A tag is not a stream, so the tag-driven motor flow has no deadman.** The
    tick is local to the board, so a host that disappears while armed leaves the
    last speed applied for ever. The controller nodes are the fix — an axis goes
    silent when the pad does and the watchdog starves — but on a field device
    they need a pad transport on the board that does not exist yet (item 19).
14. **A watchdog can only fire while the runner is ticking.** PWM is hardware and
    keeps outputting through a wedged VM, so no flow-level guard protects against
    that. It is an argument for the arm being a physical switch eventually.
15. **Everything but `agent.py` goes to a board as bytecode (`.mpy`).**
    Measured on the motor board: importing `flow.py` took 77,892 bytes and
    544ms — 18.5KB of result and 59KB of heap grown to compile it, never given
    back — and `flow.mpy` took 17,040 bytes and 69ms. The host compiles with
    `mpy-cross` (built by `scripts/build-mpy-cross.sh` into
    `~/.cache/zero2w-console/micropython`) on demand, cached by source. A board
    gets `.mpy` only when its firmware is ≥1.23 (mpy v6.3) and its agent is
    ≥0.8.0, which says so on its manifest request and deletes the `.py` beside
    each `.mpy` it lands, since MicroPython imports `x.py` first. With no
    compiler here, everything stays `.py`. **From agent 0.9.0 the agent is
    compiled too, with a fallback.** A board then holds `main.py` (the
    bootstrap), `agent_src.py` (the agent's source) and `agent.mpy`, and no
    `agent.py`. `main.py` imports `agent`, which is the `.mpy`; if that will
    not load it imports `agent_src` as `agent` — so `import agent` elsewhere
    still finds it — and the agent reports why once it is online. The
    manifest lists `main.py` and `agent_src.py` before `agent.mpy`, and the
    agent will not land `agent.mpy` without `agent_src.py` on flash, so a
    board cannot hold a compiled agent with nothing to fall back on. The
    flasher writes the same `main.py` the console serves. Proven on the motor
    board by breaking `agent.mpy`'s version byte: it booted from source,
    logged "compiled agent would not load (incompatible .mpy file); running
    from source", fetched a good `agent.mpy` (0.9.1 fetches it again only if
    the file no longer matches its sha — an intact file the firmware refuses
    would just fail again), restarted, and was back on the compiled agent
    ten seconds after the failure.
    `iot-flash.py --code-only` writes the agent, the flow and its modules over
    USB, compiled where the board can take them, without touching config.json.
16. **A board could boot armed — closed for tags marked `boot_reset`.** The
    manifest hands a booting board the fleet's current values. A shared tag
    with *Go back to its initial value when a board boots* is reset on the host,
    and so everywhere, when any board's first sync after power-on arrives
    (`?boot=1`), before its manifest is built. The motor examples set it on
    `drive_armed`; a tag created by hand has to be ticked. A board that boots
    with no host has no tags at all, and the arm gate fails closed.
17. **The motor board cannot reliably complete a wifi join on USB power.** A Pi
    USB port does not always carry an ESP32's association bursts (it did join,
    twice, on 2026-09-27 from this Pi's port, and fetched flow.py), so every
    bench test runs from a cold 12V power-on through a buck converter with two
    H-bridge modules on the same rail. `txpower` exists per device as the one lever software has on that;
    the actual fix is electrical — bulk capacitance at the module, or a converter
    with more headroom — and software can only stop making it worse.
18. **The DualShock 4 cannot pair with a stock board at all.** It speaks
    Bluetooth *Classic* HID; MicroPython's ESP32 port ships BLE only, so there is
    nothing to configure. An Xbox Series X/S pad speaks BLE and needs no rebuild,
    but sends no reports until the link is **bonded** — `scripts/ble-pad-probe.py`
    measures whether MicroPython's central stack can manage that, and nothing
    should be built on top until it says yes. It has not been run.
19. **A field device has a BLE central but no pad transport.** The BLE nodes
    run on a board (item 20). The controller nodes do not: `agent/modules/pad.py`
    imports `modules.blepad` if it exists, and it does not, so on a board they
    log once and report nothing. Gated on item 18. Memory decides which boards
    can run BLE at all: a board with PSRAM can; the motor board, without PSRAM,
    is measured and **does not fit yet**. At the REPL on
    the motor board (MicroPython 1.29, no PSRAM), internal heap free was
    141,832 at rest, 92,672 with WiFi on and 59,144 with WiFi and BLE on —
    BLE takes 33,528, steady through a 3s scan of 296 adverts. But with the
    agent and the drive flow running, `idf_free` reads **~26,500** — on
    agent 0.9.0 with everything compiled, exactly as before, because the cost
    is not compiling. Traced step by step at the REPL: loading every `.mpy`
    costs internal RAM nothing; joining WiFi costs ~6.6KB; then the agent and
    flow outgrow MicroPython's 56,000-byte initial heap, and the first
    allocation past it (the `flow.json` parse, in every order tried) makes the
    heap double, taking ~59KB (see the `gc.mem_free()` trap in §5). The
    budget: 137,612 free at a bare REPL, less WiFi (~51.5KB), less the
    doubling (~59.4KB), less BLE (33.5KB), is about 7KB short. The ways out:
    keep the running agent and flow inside the initial heap (trim what a
    board parses — the flow document carries editor fields, and a sync
    parses it again inside the manifest); a firmware built with a larger
    fixed heap and no auto-growth; a board with PSRAM; or keep BLE on the
    host and drive the board over the link (median 26ms, item 10). **Chosen:
    a board with PSRAM**, where MicroPython's heap moves out of internal RAM
    (the CAM runs its flow and link with ~135KB internal free). The repo is
    ready for the swap: a config's `psram` picks the SPIRAM firmware, and
    *Move a config here* (`POST /api/iot/devices/<id>/rehome`) puts an
    existing config onto the new board's MAC — README, "Moving a device onto
    a new board".
20. **Generic BLE nodes run on this host and on a board.** `ble.link`,
    `ble.read`, `ble.write` and `ble.notify` name characteristics by UUID and
    decode through `agent/modules/blefmt.py` on both sides. On the host,
    notifications stream from a `gdbus monitor` per subscription, which must
    run under `stdbuf -oL` or it prints nothing until its pipe fills. On a
    board they are `agent/modules/ble.py`: one central connection driven from
    the runner's tick. A flow change restarts the runner without a reboot, so
    the module's `stop()` switches the radio off; before that hook existed, a
    link opened by the old flow stayed up under the new one. Proven on the CAM
    on 2026-09-27 against the motor board running
    `scripts/ble-peripheral.py`. It connected 5s after the flow started,
    then read, wrote and received every notification for 2.5 minutes. While
    connected, the CAM's link round trip went from a 26ms median to **62ms**
    (min 19, max 159): the ESP32 has one radio shared between WiFi and BLE.
    Anything latency-bound over the link pays that while BLE is up.
21. **The host can only be a GATT client.** The adapter reports
    `can_advertise` and `can_serve_gatt`, so serving is possible; exporting
    GATT objects over D-Bus is the work, and none of it is started.

**Bluetooth on this host, 2026-09-27.** The IoT screen's Bluetooth tab scans,
pairs (one `bluetoothctl` session, §5) and browses any device's GATT; a
DualShock 4 drives the controller nodes on the host through the kernel's HID
stack, and the BLE nodes' read, write and notify were proven against a board
running `scripts/ble-peripheral.py`. The same nodes on the CAM were then
proven against that fixture (item 20).

**The IOT extension is built.** `docs/IOT-PLAN.md` holds the plan and what
became of each phase; `docs/RADIO.md` records what the wifi chip actually does
and the four things that turned out not to be the problem.

---

## 7. Conventions

- Values in the UI come from design tokens; no hard-coded colours anywhere in
  CSS (there is a check for this).
- Identifiers the kernel uses are rendered monospaced and verbatim
  (`enx0a1b2c3d4e5f`, not "USB Ethernet").
- Status is never colour alone — always a colour plus a word.
- Every widget names its real data source in its subtitle.
- Anything privileged is handed to the user as one command, never attempted.
- Flows live in `~/.config/zero2w-console/flows.json`, written atomically.
  **Back it up before testing anything that writes flows.**
