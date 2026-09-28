# Node library — the redesign brief

**Status: the library is built; the open design questions in §4 are not.**
53 node types, 44 of them runnable on a device, 50 variables, 28 examples —
counts checked against the registry by `tests/test_docs_agree.py`. Read
`docs/ARCHITECTURE.md` and then `CONTEXT.md`; this assumes their conventions
and constraints.

## Built so far

- **Every node declares what it emits.** `EMITS` beside the registry gives
  each type's output as named, typed, described fields, folded into
  `REGISTRY` at import. The inspector shows that structure with the live
  value from the last run beside each field.
- **Five standard blocks**, all running on host and device:
  `logic.edge` (a level becomes an event), `logic.count` (with a reset input
  of its own), `logic.hysteresis` (two thresholds, so a sensor cannot
  chatter), `math.scale` (raw readings into real units) and `math.smooth`
  (running average, weighted, lowest, highest). A test drives
  scale → smooth → hysteresis end to end, because the point of them is what
  they do in combination.
- **Ports can name a side and a label**, which is what lets a node have a
  reset input that the graph actually shows.
- `truthy()` is one shared rule for what counts as on, because a payload
  arrives as `1`, `"on"` or the string `"false"` depending on what produced
  it.

- **A tag table**, the PLC idea: named typed values any flow can write and any
  field can read as `{{tag.name}}`, with a `tag.change` trigger. This is what
  lets flows compose without being wired together, and it is the thing the
  examples lean on hardest.
- **28 examples** (`examples.py`), built in code and checked against the
  registry by a test, added switched off. They run as a course in seven steps,
  from Blink a pin to the motor set, and between them use every node type.
  Each explains itself in two plain sentences, and arrives with its pins and
  devices empty for you to choose. The motor set (bench, arm, two-wheel drive
  and drive-from-a-controller) composes through `drive_armed` rather than
  through wires.
- **The timer family**, as one `logic.timer` node with three behaviours:
  *wait until true for* (TON), *keep it true for* (TOF) and *one and then
  nothing for* (TP). Underneath is `_Scheduler`, one thread holding everything
  waiting to happen later, with cancellation tagged by node — which is the
  part `time.sleep` could never do. `logic.delay` uses it too, so a delay
  never stops the engine.

- **A Formula node** (`math.expr`), the ST escape hatch. A small recursive
  descent parser rather than `eval` — partly safety on a networked board,
  partly that `eval` is not dependable on a MicroPython build, and mostly
  that the answer has to be identical on both sides. It lives in
  `agent/modules/expr.py`, which the host imports and a device pulls, and
  imports nothing at all because MicroPython has no `ast`.
- **`{{device.x}}`** — the device a flow is deployed to, named rather than
  numbered. The host answers from the last report, the device from its own
  agent, so a flow reads the same either side.
- **Shared tags across the fleet.** A tag marked *shared* rides the manifest
  to a device, changes are pushed on the existing command queue, and a
  device's own writes come home on the report it already sends every two
  seconds. No new route. The echo is the part worth knowing about: a change
  is never sent back to the device that reported it, or the two hand the same
  number to each other forever.
- **The drive blocks** — Dead zone, Ramp, Sign split, Latch and Watchdog —
  with the arithmetic in `agent/modules/drive.py`, so host and device agree on
  a duty cycle to the decimal.
- **Controller nodes** — Controller, Controller axis and Controller button,
  plus `{{pad.*}}`. An axis emits nothing when the pad is stale, so a Watchdog
  downstream is a deadman. On this host they read a paired pad through the
  kernel; a field device has no Bluetooth transport for them yet.

- **Tag change runs on a device.** The runner compares its own copy of the
  tags every tick, so a pushed value, a synced one and the flow's own write
  all fire it the same way; values a board receives at sync are its
  baseline. The rule for *changes / becomes true / rises* is
  `tagwatch.tag_wants`, which the host imports too; a flow that uses it
  pulls only that 1.7KB module.

- **Sequences, SFC-style.** `logic.step`: Steps wired *next → go*, each
  active from *go* until *done* (or *Leave after*), with *entered* and *left*
  to start and stop that stage's action. A *done* while not active is
  ignored, which is the whole point. Conditions stay separate nodes wired
  into *done*. Host and device; the canvas highlights the active step. On the
  device it is `modules/steps.py`, pulled only by a flow that has one.

- **Generic BLE, on this host.** BLE device, BLE read, BLE write and BLE
  notify: characteristics by UUID, values through `blefmt` (hex, utf8 and
  little-endian numbers), notifications streamed as they arrive. The device
  side waits on a BLE transport for boards.

- **Groups.** Named lists of devices; Device command and Device event take
  `group:<name>` wherever they take a device.
- **`{{dev.<device>.<field>}}`** — any device from its last report, by name
  key or id: online, seen, rssi, free, uptime, ip, flow, and `pin.<gpio>`.
  Host only; a device leaves it verbatim.

## Still to do

1. **Bluetooth on a device** — a BLE transport for boards, gated on the
   bonding probe and on measuring the motor board's free internal SRAM
   (CONTEXT.md §6, items 18 and 19). The controller and BLE nodes then run
   there too.
2. **Deploying to a group** — a group is not yet a deploy target.

---

## 1. What this has to be

A universal node graph and library for automating a network of small devices,
where **the same nodes run on the host and on the field devices**. The host is
this board — an OrangePi Zero 2W broadcasting its own wireless network; the
devices are ESP32-class boards on that network running the MicroPython agent.

The design goal, stated directly: **general commands that become powerful in
succession, with emergent properties.** Not a catalogue of special-purpose
nodes — a small set of orthogonal primitives that compose. A node that does
one thing on a 240 MHz microcontroller and the same thing on the host.

Nodes must be intuitive: dropdowns rather than remembered ids, a description
that says what the node does, and declared input and output variables so the
next node's author knows what they are holding. The variable table needs the
same treatment — intuitive, and covering every category.

## 2. Where to steal from, and what specifically

| Source | What it gets right and worth taking |
|---|---|
| **Apple Shortcuts** | Plain-language node titles that read as a sentence. Output of the previous step offered as the obvious default input. Parameters inline in the sentence, not a separate form. Ruthless about hiding what you rarely change. |
| **n8n** | Every node declares its input and output schema, and the editor shows real sample data from the last run. Expressions with a picker over the actual available fields. Credentials separated from node config. Clear split of trigger / action / logic. |
| **MechMind** | Task-graph thinking for physical processes: a step is a unit of work with typed ports, and the graph is a procedure rather than a dataflow curiosity. |
| **The five IEC 61131-3 PLC languages** | The part that matters most here, because this is machine control. See below. |

### What the PLC languages contribute

- **Ladder (LD)** — the latch/seal-in idiom, and the discipline that outputs
  are evaluated from inputs every scan. `logic.toggle`'s two inputs are
  already a set/reset latch; that idea generalises.
- **Function Block Diagram (FBD)** — a block has named, typed input and output
  pins and *retains state between scans*. This is the closest model to what
  this engine should be, and the reason ports have names.
- **Structured Text (ST)** — the escape hatch. There must be one node where
  an expression can be written, or the library grows a node per arithmetic
  operation.
- **Sequential Function Chart (SFC)** — steps, transitions, and *which step is
  active now*. The thing most missing today: a flow has no notion of state,
  so "do A until X, then do B" cannot be drawn.
- **Instruction List** — deprecated in the standard; ignore.

**The standard library worth having**, in PLC terms: TON/TOF/TP timers
(on-delay, off-delay, pulse), CTU/CTD counters, R_TRIG/F_TRIG edge detectors,
RS/SR latches, scaling (map a range onto another), limit/clamp, hysteresis,
debounce, moving average, min/max/mean over a window, and a first-order
filter. These are the pieces that make sensor graphs work, and each now has
a node here: Timer, Counter (a negative step counts down), On change, Latch,
Scale, Hysteresis, Smooth, and Formula's `clamp`.

## 3. What is already true in this repo

Worth knowing before designing, because it constrains the answer:

- A node type is one entry in `flows.REGISTRY` plus a branch in
  `FlowEngine._execute`, plus `_do_<type>` in `agent/flow.py` — or a pulled
  module listed in both `fleet.NODE_MODULES` and `flow.PULLED` — if a device
  may run it. A test fails if the device handler is missing.
- `runs` is `host` / `device` / `both`, and a flow bound to a board may only
  use nodes that board can run. The palette filters itself.
- Ports are names; a port may be `{name, side}` to pin it to an edge of the
  node, and `label_from` names a config key to show as its label. The engine
  passes the edge's `toPort` into `_execute`.
- `fields` drive the inspector: kinds are `text`, `textarea`, `number`,
  `select`, `pin`, and `combo` (dropdown + free text, `options_from` resolved
  in the browser). `showIf` hides a field based on another's value.
- `VARIABLES` (50 variables, grouped Message / Context / Device / Controller /
  Board / Readings) is the single source of truth for `{{templating}}`,
  resolved live for the picker.
- The engine keeps each node's last output; the inspector shows it.
- Messages are `{"payload": any, "meta": {...}}`. That is the whole contract.

## 4. The open design questions

These are the decisions the redesign has to make. They are listed because
they are genuinely open, not because there is a preferred answer hiding here.

1. **Does the message stay untyped?** `{payload, meta}` with anything in
   payload is why every node composes today. Declared output *structure* (the
   n8n idea, and what the inspector should show) can be documentation without
   becoming enforcement — or it can become real types with coercion at the
   edges. Typed ports catch wiring mistakes at design time; untyped ones are
   why this works on a microcontroller.
2. **Is there scan-based evaluation, or only message passing?** PLC blocks
   evaluate every scan and hold state; this engine fires on events and walks
   edges. Timers and counters need *something* ticking. The device runner is
   already a polled loop, so a scan model is available — but it changes what
   a flow means.
3. **How does state get drawn?** Answered by Step: one node per stage, the
   active one highlighted on the canvas.
4. **One expression node, or many small ones?** Answered with both: Formula
   for arithmetic, a small node wherever there is state to hold.
5. **How are devices addressed as the fleet grows?** Answered with named
   groups. Roles and broadcast were considered and not built.
6. **What is the variable table when there are many devices?** `{{device.x}}`
   names the device a flow is deployed to; `{{dev.<device>.x}}` reads any
   other, and the picker lists every device with its live values.

## 5. Constraints that are not negotiable

- **It must run on the device.** Every node marked runnable needs a
  MicroPython implementation that fits alongside the others in flash, and must
  not block: the agent serves its camera socket and ticks its flow from one
  loop, and a handler that waits stops both.
- **No node acts by existing.** A node does something because a message
  reached it. This was decided by removing the last exception to it.
- **Nothing is standalone, and nothing is hidden.** If a node affects the
  world, an edge on the canvas says so.
- **Unknown `{{variables}}` stay verbatim**, so a typo is visible.
- **Honest capability reporting**: a node that cannot do something says what
  is missing and how to enable it, rather than failing quietly.
- The host is passively cooled with four A53s, and the devices have kilobytes.
  Cleverness costs more here than it does anywhere else.
