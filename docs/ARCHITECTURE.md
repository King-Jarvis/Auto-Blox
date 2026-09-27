# Auto-Blox — what it is, how it works, and how to build on it

The one document to read first. `README.md` is operational (how to run it),
`CONTEXT.md` §5 is the trap list (what will bite you), `IOT-PLAN.md` and
`NODE-LIBRARY.md` are the plans behind two subsystems, `RADIO.md` is what the
combo wifi chip needs before it will host an access point. This explains the *shape*: what the parts
are, which part owns which fact, and where to plug in.

Counts and versions here are checked against the code by `tests/test_docs_agree.py`.

---

## 1. What it is

A single-board automation console. One OrangePi Zero 2W runs a web application
that does four separable things:

1. **Watches the board it runs on** — thermals, CPU, memory, filesystems,
   network, journal, processes, and its own 40-pin header.
2. **Runs a visual automation language.** A *flow* is a graph of nodes; a node
   reads a pin, does arithmetic, waits, publishes MQTT, calls HTTP, drives PWM.
   Flows run on the host **or** on a device, from the same registry.
3. **Hosts a private network for small devices and runs flows on them.** `wlan0`
   is an access point; ESP32-class boards join it, enrol, pull a MicroPython
   agent and only the modules their flow needs, and execute that flow from their
   own flash.
4. **Talks Bluetooth.** It pairs controllers, which then drive flows through
   the Controller nodes, and reads, writes and subscribes to any BLE device's
   GATT (`bluetooth.py`, `gatt.py`, `pad.py`).

The constraints are the whole design. **Python 3.14 standard library only** — no
Node, no package manager, no build step; the browser is handed the source files
as written. A four-core A53 also driving a browser, so nothing may redraw per
message. And `sudo` needs a password, so anything privileged is handed to the
user as one command rather than attempted.

## 2. The shape

```
        browser  ──HTTP+SSE──┐
                             │
                    ┌────────▼──────────────────────────────────┐
                    │  server.py — one process, ThreadingHTTP   │
                    │                                           │
                    │  FlowEngine   the host's flow runtime     │
                    │  TagTable     shared named values         │
                    │  Fleet        devices, manifests, commands│
                    │  LinkServer   optional held-open socket   │
                    │  Bus          one SSE fan-out             │
                    └───┬───────────────────────────┬───────────┘
              gpio/i2c/ │                           │ wlan0 = 10.42.0.1/24
              journal   │                           │ hostapd + dnsmasq
                        ▼                           ▼
                  the board itself          ESP32 devices, each running
                                            agent.py + flow.py from flash
```

Four screens, one design system (`static/bundle.js`): `/` the dashboard, `/flows`
the Flow Studio, `/iot` the fleet, `/cameras` the wall.

State lives in `~/.config/zero2w-console/` — `flows.json`, `iot.json`,
`tags.json`, `token` — written atomically. Nothing application-level lives in
`/etc`; only the privileged network config does.

## 3. How a flow runs

A flow is JSON: `{id, name, enabled, board?, device?, nodes[], edges[]}`. A node
is `{id, type, config, x, y}`. `flows.REGISTRY` is the single source of truth for
what a node type is — its fields, what it emits, whether it can run on a device.
**51 node types, 42 of them runnable on a device, 50 variables, 17 examples.**

A message is `{"payload": ..., "meta": {...}}`. Nodes pass messages along edges;
`{{payload}}`, `{{meta.x}}` and `{{tag.name}}` are rendered in any field.

**Two runtimes, deliberately.** `flows.py` executes on the host (threads, a
scheduler, real sockets). `agent/flow.py` executes on a device (one cooperative
tick, no threads, MicroPython). They are not shared code and are not meant to be:
one has a 3.8 GiB heap, the other about 100 KB. What *is* shared is the
arithmetic — `agent/modules/{expr,drive,link,pad,tagwatch,blefmt}.py` are imported by the host
and pulled by the device, so a mixer cannot behave differently in the two places.

`runs` in the registry decides where a type may run. A flow with a `board` set
runs on that device and nothing else; the host will not execute it, because
arming it here would watch the wrong pins.

## 4. A device, from blank to running

```
flash ──► enrol ──► sync ──► deploy ──► run ──► report
```

- **Flash.** `scripts/iot-flash.py` fetches the right MicroPython build, writes
  it, then copies three files over the serial REPL: `config.json` (the network,
  one URL, a one-time enrol token), `agent.py`, and a `main.py` that imports it.
  With `--flow` it also writes the flow and its modules, so the board boots
  complete and its first sync fetches nothing.
- **Enrol.** The board presents its one-time token inside a time window the
  console opens. It gets a long-lived device token back. The console's own token
  is never put on a device.
- **Sync.** The board GETs `/api/iot/manifest` and receives: the flow document,
  the list of modules it needs *with shas*, the shared tags, and whether to dial
  the link. It fetches only modules whose sha differs, **streamed straight to
  flash** through a part file, and refuses one whose sha does not match.
- **Deploy.** Binding a flow to a device sets `device.flow`; the console pushes a
  `reload`. Switching a flow off pushes one too — `enabled` rides the manifest
  and the device reads it.
- **Run.** The board executes the flow from flash, host present or not.
- **Report.** Every two seconds: pin levels and directions, which nodes fired,
  address, signal, free memory, uptime, what flow is running.

### The boot order, which is load-bearing

`Agent.run()` does, in this order, and each step was bought by a board that
stopped:

1. `radio()` — activate the interface. This allocation fails first when memory is
   short, and a board that cannot reach the host cannot be told anything.
2. `connect()`, twice — **associating is a second allocation**, and after the
   flow has loaded it can fail with zero attempts made, which from the host is
   indistinguishable from a dead board.
3. `gc.collect()` — joining leaves transients and MicroPython does not compact.
4. `import flow`, if a flow is on flash. **This is the expensive step**, not the
   Runner: ~30 KB of source compiled on the device wants a large *contiguous*
   block. Measured failing at 89,456 bytes free and working at 107,744 — on a
   two-node flow, so it is fragmentation, not size. The import is cached, so
   doing it here spends memory a running flow spends anyway, at the one moment
   the heap can find it.
5. **Not the flow.** That starts in the loop: after the first sync, or after
   `FLOW_GRACE` seconds if there is no host. A field device still runs from flash
   with no server — seconds later, not immediately — and that is the difference
   between a board that enrols and one that joins and is never heard from.

A flow that will not start is caught (`try_flow`), because all of this is
upstream of the command poll: raising leaves no route by which anyone could ask
the board to stop running what stopped it.

## 5. Tags — the shared memory

A tag is a named typed value with an initial, a description and two flags:
`retain` (survives a restart) and `share` (reaches devices). Any flow reads one
as `{{tag.name}}`, any flow writes one, and a change can trigger a flow.

This is what lets flows compose without being wired together, and it is how the
motor set works: an Arm flow writes `drive_armed`, a Drive flow reads it, and
nothing connects them.

**An arm must not survive a boot.** The manifest hands a booting board the
fleet's current values — right for a setpoint, wrong for an arm. A shared tag
marked `boot_reset` goes back to its initial value everywhere when any board
boots, so arming again is always deliberate. The motor examples set it on
`drive_armed`.

## 6. Two transports, and why one is off

**The poll.** The device GETs `/api/iot/commands?wait=0` every pass and POSTs
state and events. Simple, survives anything, and costs a full TCP connection per
exchange — so a command waits up to one work window (1.5 s).

**The link.** An optional held-open TCP socket on 8789. 16-byte header plus an
8-byte truncated HMAC-SHA256, a per-direction monotonic counter, a session key
derived as `HMAC(token, nonce_device ‖ nonce_host)`, and mutual authentication —
the device proves it holds the token, and so does the host. Frame types are
`HELLO/CHALLENGE/AUTH/READY`, `STATE/EVENT/TAGS`, `COMMAND/TAGSET`,
`MEDIA/CONTROL`, `PING/PONG`. **Authentication, not encryption**: the AP is WPA2
and the payloads are telemetry.

It works, and **it is switched off on both boards.** Its three modules measured
**~39 KB of a ~100 KB heap** on a plain ESP32 — a third of the board, for a
transport — when they were ~31 KB of source; they are ~21 KB now, unmeasured. The code is kept and tested; nothing pulls it while
`device.link` is false. One field per device turns it back on.

## 7. Memory is the binding constraint

Every device-side design decision comes from this arithmetic, and getting it
wrong takes a board off the network:

| | bytes |
|---|---|
| A plain ESP32, radio up, nothing loaded | ~120,000 free |
| `agent.py` + `flow.py` | ~60,000 of source |
| The drive flow's four handler modules | ~25,000 of source |
| The link, if enabled | ~39,000 measured |

**A MicroPython import costs 1.00–2.2 bytes of RAM per character**, and you need
both figures: 1.00 is steady state (measured: 25,072 bytes for 25,126
characters), 2.2 is a cold board with the wifi stack allocating around it. And
**a comment costs RAM, because the board compiles the source** — taking the
narrative out of the device files gave about 11,000 bytes of heap back.
`tests/test_agent_budget.py` holds the line; prose belongs in `CONTEXT.md`.

**BLE is invisible to all of that.** NimBLE takes 33,164 bytes of the ESP-IDF
heap and `gc.mem_free()` does not move, so a board without PSRAM has to be
measured directly before it is given a Bluetooth transport.

Two further facts that shape everything: **the collector does not compact**, so a
board with 93,232 bytes free can refuse a 6,657-byte request; and **stopping a
flow does not give the memory back** — only a reboot does.

**Every device file but `agent.py` goes to a capable board as `.mpy`.** Importing
`flow.py` on the motor board took 77,892 bytes, most of it heap grown to compile
and never returned; `flow.mpy` took 17,040. The host compiles with `mpy-cross` on
demand; `CONTEXT.md` §6, item 15 has the rules.

## 8. Who owns which fact

The rule that keeps four screens and five documents honest:

| Fact | Owner | Everyone else |
|---|---|---|
| Is a device online? | `flows.DEVICE_FRESH`, sent as `device.online` | reads it; the browser has no threshold |
| What a node type is | `flows.REGISTRY` | the palette, the inspector, the docs |
| How a byte count reads | `Z.bytes` in the design system | calls it |
| How a time reads | `Z.ago` (a moment) / `Z.duration` (a span) | calls them |
| Which modules a flow needs | `fleet.NODE_MODULES` + `flow.PULLED` | must agree, or a board hunts a handler it never received |
| What a board holds | `device.modules`, by sha | `deploy_cost` treats it as a hint and errs toward "must fetch" |

`tests/test_screen_congruency.py` and `tests/test_docs_agree.py` enforce the left
column. They exist because each row was once two answers.

## 9. Integrating with it

### The HTTP API

Two credentials, deliberately unequal. **The console token**
(`~/.config/zero2w-console/token`) as `X-Console-Token:` or `?t=`, for everything
a browser does. **A device token** as `X-Device-Token:`, which reaches only
`/api/iot/{manifest,commands,module/*,enroll,state,event}` — a compromised board
cannot read the fleet or write a flow.

```
GET  /api/snapshot           everything the dashboard shows, once
GET  /api/stream             SSE: metrics, log, flow, tag events
GET  /api/hardware           what the buses, GPIO, PWM and MQTT can do right now
GET  /api/gpio/pins          the pin table and what claims each pin
GET  /api/flows              the whole document, with its rev
POST /api/flows              save; send the rev you loaded or get a 409
POST /api/flows/run          fire one node by hand (travels to the device)
GET  /api/tags               definitions and live values
POST /api/tags/set           write one
GET  /api/iot/status         AP, clients, refusals
GET  /api/iot/devices        the fleet, tokens redacted, and its groups
POST /api/iot/groups         replace the groups: [{name, devices: [ids]}]
POST /api/iot/devices/<id>/{deploy,flash,command,provision}
GET  /api/iot/devices/<id>/{pins,cost,log}
POST /api/hook/<name>        trigger a flow from outside
GET  /api/bt                 the Bluetooth adapter and every device BlueZ knows
GET  /api/bt/gatt?mac=<mac>  one device's services and characteristics
POST /api/bt/<verb>          scan, pair, connect, disconnect, forget, read,
                             write, notify
```

Two things worth knowing. Flow saves are **revision-checked**: send the `rev` you
loaded, get a 409 with the current document if disk has moved on. A save with no
rev is trusted, which is how a restore from `backups/` works. And the save
response carries `advice` — things that saved fine and will not work, such as a
watchdog whose timeout is shorter than the timer feeding it.

### Adding a node type

One entry in `flows.REGISTRY` (label, fields, what it emits, `runs`), a handler
in `flows.py` for the host, and — if it should run on a device — a handler in
`agent/flow.py` or a pulled module plus its name in **both** `flow.PULLED` and
`fleet.NODE_MODULES`. Parity tests check every declared field is read and every
read field is declared, so a typo fails rather than silently doing nothing.

### Adding a device

Board profiles are generated (`scripts/build_boards.py`) into a pin table the
palette, the inspector and the flasher all read. A new ESP32 variant is a profile
plus a firmware name, not code.

### Talking to a device yourself

Implement the poll, not the link: GET the manifest, fetch modules by sha, POST to
`/api/iot/state`. `scripts/sim-device.py` is a complete worked example that runs
against a live console. If you want the link, `agent/modules/link.py` is the wire
format and is shared by both ends verbatim.

### What not to do

- Do not reorder the boot sequence in §4.
- Do not put a freshness threshold, a byte formatter or a time formatter in a
  page.
- Do not add prose to an agent file; it is heap.
- Do not restart the console while a board is syncing — the fetch fails with
  `ENOTCONN` and the board keeps the old module.
- Do not trust `state` as live: it is the last *report*. `probe` says what a board
  actually booted.

## 10. Where the sharp edges are

`CONTEXT.md` §5 is the list, and it is long because each entry cost real time.
The five that catch people:

1. **Never hardcode a socket errno.** MicroPython on ESP32 is newlib: EINPROGRESS
   is 112, ENOTCONN 128 — not Linux's 115 and 107.
2. **A ping is answered by lwIP, not the VM.** It cannot tell a wedged agent from
   a healthy one.
3. **`state` is the last report**, and a freshly flashed board shows stale values.
4. **A sync is minutes of silence** — 70 KB measured at 370 s — and silence looks
   like death. `deploy_cost` says what it will cost first.
5. **On a BTS7960 pair, both inputs high is a brake, not a short.** This document
   said otherwise twice.

---

*Written 2026-09-26. The numeric claims are checked by
`tests/test_docs_agree.py`; the fact-ownership rules by
`tests/test_screen_congruency.py`.*
