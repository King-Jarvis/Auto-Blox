"""The flow runner — MicroPython."""
import machine
import time

MAX_HOPS = 40          # the same cycle guard the host uses
MAX_QUEUE = 64

# Node types whose handler lives in a pulled module rather than in this file.
# Must agree with fleet.NODE_MODULES, which decides what a device is actually
# sent; a test checks the two against each other.
PULLED = {
    "logic.edge": "blocks", "logic.count": "blocks",
    "logic.hysteresis": "blocks", "logic.timer": "blocks",
    "logic.latch": "blocks", "safety.watchdog": "blocks",
    "math.scale": "blocks", "math.smooth": "blocks",
    "pad.link": "pad", "pad.axis": "pad", "pad.button": "pad",
    "tag.change": "tagwatch", "logic.step": "steps",
    "ble.link": "ble", "ble.read": "ble", "ble.write": "ble", "ble.notify": "ble",
}

# Types whose module also wants a call every tick, as `poll(runner)`.
POLLED = ("tag.change", "logic.step", "ble.link", "ble.read", "ble.write",
          "ble.notify")


def ticks():
    return time.ticks_ms()


def since(then):
    return time.ticks_diff(ticks(), then)


# The same rule the host uses for what counts as on: a pin gives 1, a webhook
# gives the string "false", and they all mean the same thing to a person.
FALSEY = ("", "0", "0.0", "off", "false", "no", "none", "null")


def truthy(value):
    if value is None or value is False:
        return False
    if value is True:
        return True
    if isinstance(value, int) or isinstance(value, float):
        return value != 0
    return str(value).strip().lower() not in FALSEY


def num(value, default=0.0):
    """A number out of whatever arrived, or the default."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


class Runner:
    def __init__(self, flow, agent):
        self.flow = flow or {}
        self.agent = agent
        self.nodes = {}
        self.out = {}          # node id -> [(port, target id)]
        self.pins = {}         # gpio -> machine.Pin (inputs the flow watches)
        self.outputs = {}      # gpio -> the last level this flow wrote
        self.duties = {}       # gpio -> the last duty this flow set
        self.pwms = {}         # gpio -> machine.PWM
        self.queue = []        # pending (node id, msg, due_ms)
        self.timers = []       # (node id, period_ms, last_ms)
        self.toggles = {}      # node id -> bool
        self.throttled = {}    # node id -> last pass ms
        self.edges = {}        # node id -> last truthiness seen
        self.counts = {}       # node id -> running count
        self.hyst = {}         # node id -> True while switched on
        self.windows = {}      # node id -> recent samples
        self.ramps = {}        # node id -> [value, ticks when it got there]
        self.latches = {}      # node id -> True while held on
        # node id -> [last fed, timeout ms, already tripped, hops]. Empty
        # until something is fed: a watchdog nobody uses must never fire.
        self.dogs = {}
        self.bounce = {}       # node id -> when its pin edge was last taken
        self.pulled = {}       # node type -> its handler from a pulled module
        self.pad = {}          # the controller, as modules/pad.py keeps it
        self.polls = []        # poll(runner) from pulled modules, every tick
        self.stops = []        # and their stop(runner), for what outlives a flow
        # Not `timers`, which is the interval triggers armed in start(): sharing
        # the name turned that list into a dict and stopped every Interval.
        self.timer_state = {}  # node id -> what a Timer node is holding
        self.waiting = []      # (node id, msg, hops, due) a timer may cancel
        self.arrived = "in"    # the input the message being run came in on
        self.pending = []      # ids fired from an IRQ, drained by tick()
        self.fired = []        # ids that ran since the last report, for the editor
        self.outs = {}         # node id -> [port, payload, meta] it last sent
        self.running = False
        # So {{device.x}} can ask the agent about itself. One runner at a time.
        global _RUNNER
        _RUNNER = self
        self._index()

    def _index(self):
        for n in self.flow.get("nodes", []):
            self.nodes[n["id"]] = n
        for e in self.flow.get("edges", []):
            # The input it lands on matters to a node with more than one.
            self.out.setdefault(e["from"], []).append(
                (e.get("fromPort", "out"), e["to"], e.get("toPort") or "in"))

    # -- lifecycle ---------------------------------------------------------
    def has_camera_node(self):
        """Whether anything in this flow could ever want the sensor."""
        for n in self.flow.get("nodes", []):
            if (n.get("type") or "").startswith("camera."):
                return True
        return False

    def start(self):
        self.running = True
        for n in self.flow.get("nodes", []):
            kind = n.get("type")
            if kind == "timer.interval":
                every = int(n.get("config", {}).get("every") or 1000)
                self.timers.append([n["id"], max(50, every), ticks()])
            elif kind == "gpio.in":
                self._watch(n)
            if kind in POLLED:
                mod = _load("modules." + PULLED[kind])
                if mod and mod.poll not in self.polls:
                    self.polls.append(mod.poll)
                    if hasattr(mod, "stop"):
                        self.stops.append(mod.stop)
        for poll in self.polls:
            poll(self)

    def stop(self):
        self.running = False
        for stop in self.stops:
            try:
                stop(self)
            except Exception:
                pass
        for gpio, pwm in self.pwms.items():
            try:
                pwm.deinit()
                # deinit leaves the pin where the waveform stopped, which on a
                # motor driver is a wheel still turning after a stop.
                machine.Pin(gpio, machine.Pin.OUT).value(0)
            except Exception:
                pass
        self.pwms = {}
        # Plain outputs too. Left as they were, a flash LED stayed lit under
        # the next flow, whose Toggle had started at off.
        for gpio in self.outputs:
            try:
                machine.Pin(gpio, machine.Pin.OUT).value(0)
            except Exception:
                pass
        self.outputs = {}
        for gpio, pin in self.pins.items():
            try:
                pin.irq(handler=None)
            except Exception:
                pass

    # -- triggers ----------------------------------------------------------
    def _watch(self, node):
        cfg = node.get("config", {})
        gpio = cfg.get("gpio")
        if gpio is None:
            return
        pull = machine.Pin.PULL_UP if cfg.get("pull") == "up" else None
        pin = machine.Pin(int(gpio), machine.Pin.IN, pull)
        edges = cfg.get("edges") or "both"
        trigger = 0
        if edges in ("rising", "both"):
            trigger |= machine.Pin.IRQ_RISING
        if edges in ("falling", "both"):
            trigger |= machine.Pin.IRQ_FALLING
        node_id = node["id"]

        def handler(p, node_id=node_id):
            # IRQ context: allocation is unsafe here, so only note it happened.
            if len(self.pending) < MAX_QUEUE:
                self.pending.append(node_id)

        pin.irq(handler=handler, trigger=trigger)
        self.pins[int(gpio)] = pin

    def fire(self, node_id, payload=1, meta=None):
        node = self.nodes.get(node_id)
        if not node:
            return
        msg = {"payload": payload, "meta": meta or {}}
        self.queue.append((node_id, msg, ticks(), 0, "in"))

    def set_pin(self, gpio, value):
        """A host override, outside the flow."""
        if gpio is None:
            return
        pin = machine.Pin(int(gpio), machine.Pin.OUT)
        pin.value(1 if value else 0)

    # -- the loop ----------------------------------------------------------
    def tick(self):
        if not self.running:
            return
        now = ticks()
        while self.pending:
            node_id = self.pending.pop(0)
            node = self.nodes.get(node_id)
            cfg = (node or {}).get("config", {}) or {}
            # Debounce here, not in the IRQ, which must not allocate.
            settle = int(num(cfg.get("debounce"), 50))
            if settle > 0:
                last = self.bounce.get(node_id)
                if last is not None and since(last) < settle:
                    continue
                self.bounce[node_id] = now
            level = None
            gpio = cfg.get("gpio")
            if gpio is not None and int(gpio) in self.pins:
                level = self.pins[int(gpio)].value()
            self.queue.append((node_id, {"payload": level if level is not None else 1,
                                         "meta": {"edge": "rising" if level else "falling",
                                                  "gpio": gpio}}, now, 0, "in"))
        for poll in self.polls:
            poll(self)
        for t in self.timers:
            if since(t[2]) >= t[1]:
                t[2] = now
                self.queue.append((t[0], {"payload": 1, "meta": {"timer": t[1]}}, now, 0, "in"))

        # A watchdog fires because something stopped.
        for node_id in self.dogs:
            dog = self.dogs[node_id]
            if dog[2] or since(dog[0]) < dog[1]:
                continue
            dog[2] = True
            cfg = (self.nodes.get(node_id) or {}).get("config", {}) or {}
            silent = since(dog[0])
            self.agent.log("warn", "fed nothing for %dms" % silent, node=node_id)
            self._emit(node_id,
                       {"payload": render(cfg.get("value", "0"), {"payload": 0}),
                        "meta": {"starved": True, "silent_ms": silent}},
                       dog[3])

        # A timer's message comes out when its wait is up, unless something
        # cancelled it in the meantime.
        if self.waiting:
            due = [w for w in self.waiting if time.ticks_diff(w[3], now) <= 0]
            self.waiting = [w for w in self.waiting if time.ticks_diff(w[3], now) > 0]
            for node_id, msg, hops, _at in due:
                self._emit(node_id, msg, hops)

        ready = [item for item in self.queue if item[2] <= now]
        self.queue = [item for item in self.queue if item[2] > now]
        for node_id, msg, _due, hops, port in ready:
            self._run(node_id, msg, hops, port)

    def _emit(self, node_id, msg, hops, port="out"):
        self.outs[node_id] = [port, msg.get("payload"), msg.get("meta")]
        for out_port, target, to_port in self.out.get(node_id, []):
            if out_port != port:
                continue
            if hops + 1 > MAX_HOPS:
                self.agent.log("warn", "cycle guard tripped", node=node_id)
                return
            self.queue.append((target, msg, ticks(), hops + 1, to_port))

    def _pulled(self, kind, name):
        """A handler that lives in a module this device pulled."""
        if kind in self.pulled:
            return self.pulled[kind]
        found = None
        where = PULLED.get(kind)
        if where:
            mod = _load("modules." + where)
            if mod is None:
                self.agent.log("warn", "no %s module on this device" % where)
            else:
                fn = getattr(mod, name, None)
                if fn is None:
                    self.agent.log("warn", "%s has no %s" % (where, name))
                else:
                    def found(node_id, cfg, msg, hops, _fn=fn):
                        return _fn(self, node_id, cfg, msg, hops)
        self.pulled[kind] = found
        return found

    def _run(self, node_id, msg, hops, port="in"):
        node = self.nodes.get(node_id)
        if not node:
            return
        kind = node.get("type")
        cfg = node.get("config", {}) or {}
        # Which input it arrived on. Read here rather than passed as an
        # argument; safe because this runner is polled and single-threaded.
        self.arrived = port
        # Remembered so the editor can flash what is actually running. Capped:
        # a fast flow must not fill memory with its own history.
        if len(self.fired) < 64:
            self.fired.append(node_id)
        try:
            name = kind.replace(".", "_")
            handler = getattr(self, "_do_" + name, None)
            if handler is None:
                handler = self._pulled(kind, name)
            if handler is None:
                # Stop the branch. Passing the message on made a Watchdog with
                # no handler a plain wire that fired on every message.
                self.agent.log("critical", "no handler for %s here" % kind,
                               node=node_id)
                return
            handler(node_id, cfg, msg, hops)
        except Exception as exc:
            self.agent.log("critical", "%s failed: %s" % (kind, exc), node=node_id)

    def pin_states(self):
        """What this flow is doing to the board, right now."""
        out = {}
        for gpio, pin in self.pins.items():
            try:
                out[str(gpio)] = {"dir": "in", "value": pin.value()}
            except Exception:
                pass
        for gpio, value in self.outputs.items():
            out[str(gpio)] = {"dir": "out", "value": value}
        for gpio, duty in self.duties.items():
            out[str(gpio)] = {"dir": "pwm", "value": duty}
        return out

    def take_outputs(self):
        """What each node last sent since this was last asked, clipped small
        enough to ride the report."""
        out, self.outs = self.outs, {}
        for k in out:
            port, payload, meta = out[k]
            meta = meta or {}
            out[k] = [port, _small(payload),
                      dict((m, _small(meta[m])) for m in list(meta)[:6])]
        return out

    def take_fired(self):
        """Which nodes ran since this was last asked, and forget them."""
        out, self.fired = self.fired, []
        return out

    def node_summary(self):
        """Which node types are in play, so the console can say what it runs."""
        counts = {}
        for n in self.flow.get("nodes", []):
            kind = n.get("type") or "?"
            counts[kind] = counts.get(kind, 0) + 1
        return counts

    # -- node types --------------------------------------------------------
    def _do_timer_interval(self, node_id, cfg, msg, hops):
        self._emit(node_id, msg, hops)

    def _do_gpio_in(self, node_id, cfg, msg, hops):
        self._emit(node_id, msg, hops)

    def _do_manual_fire(self, node_id, cfg, msg, hops):
        self._emit(node_id, msg, hops)

    def _do_gpio_out(self, node_id, cfg, msg, hops):
        gpio = cfg.get("gpio")
        if gpio is None:
            return
        pin = machine.Pin(int(gpio), machine.Pin.OUT)
        action = cfg.get("action") or "high"
        if action == "high":
            pin.value(1)
        elif action == "low":
            pin.value(0)
        elif action == "toggle":
            pin.value(0 if pin.value() else 1)
        elif action == "from-payload":
            # The registry calls it "from-payload"; "follow" never matched.
            pin.value(1 if truthy(msg.get("payload")) else 0)
        elif action == "pulse":
            level = 0 if cfg.get("pulse_level") == "low" else 1
            pin.value(level)
            time.sleep_ms(int(num(cfg.get("pulse_ms"), 100)))
            pin.value(0 if level else 1)
        self.outputs[int(gpio)] = pin.value()
        self._emit(node_id, msg, hops)

    def _do_pwm_out(self, node_id, cfg, msg, hops):
        # `mode` and `channel` are host-only: LEDC picks its own channel.
        gpio = cfg.get("gpio")
        if gpio is None:
            return
        gpio = int(gpio)
        action = cfg.get("action") or "start"
        if action == "toggle":
            action = "stop" if gpio in self.pwms else "start"

        if action == "stop":
            pwm = self.pwms.pop(gpio, None)
            if pwm is not None:
                try:
                    pwm.deinit()
                except Exception:
                    pass
            # Deinit leaves the pin where the waveform stopped, and a floating
            # H-bridge input is a motor still running.
            machine.Pin(gpio, machine.Pin.OUT).value(0)
            self.duties[gpio] = 0
            out = dict(msg)
            out["payload"] = 0
            return self._emit(node_id, out, hops)

        freq = int(num(render(cfg.get("freq"), msg), 1000))
        freq = 1 if freq < 1 else freq
        pwm = self.pwms.get(gpio)
        if pwm is None:
            pwm = machine.PWM(machine.Pin(gpio))
            self.pwms[gpio] = pwm
        pwm.freq(freq)

        duty = cfg.get("duty")
        if duty in (None, ""):
            duty = msg.get("payload")
        else:
            duty = render(duty, msg)
        duty = num(duty, 0)
        if (cfg.get("units") or "percent") == "microseconds":
            percent = 100.0 * duty / (1000000.0 / freq)   # servo pulse width
        else:
            percent = duty
        percent = 0.0 if percent < 0 else (100.0 if percent > 100 else percent)
        pwm.duty_u16(int(percent * 65535 / 100))
        self.duties[gpio] = round(percent, 2)
        out = dict(msg)
        out["payload"] = round(percent, 2)
        meta = dict(out.get("meta") or {})
        meta["gpio"] = gpio
        meta["freq"] = freq
        out["meta"] = meta
        self._emit(node_id, out, hops)

    def _do_logic_if(self, node_id, cfg, msg, hops):
        """The same comparison the host makes, on the same operators."""
        op = cfg.get("op", "==")
        left = msg.get("payload")
        if op == "truthy":
            return self._emit(node_id, msg, hops, "true" if truthy(left) else "false")
        right = cfg.get("value", "")
        try:
            a, b = float(left), float(right)
        except (TypeError, ValueError):
            # Ordering something that is not a number fails closed. Comparing as
            # words made `"off" >= "0.5"` true, which armed a motor flow; == and !=
            # still fall back, because there that is the intent.
            if op in (">", ">=", "<", "<="):
                return self._emit(node_id, msg, hops, "false")
            a, b = str(left), str(right)
        if op == "==":
            ok = a == b
        elif op == "!=":
            ok = a != b
        elif op == ">":
            ok = a > b
        elif op == ">=":
            ok = a >= b
        elif op == "<":
            ok = a < b
        elif op == "<=":
            ok = a <= b
        else:
            ok = False
        self._emit(node_id, msg, hops, "true" if ok else "false")

    def _do_logic_delay(self, node_id, cfg, msg, hops):
        ms = int(cfg.get("ms") or 1000)
        for _out_port, target, to_port in self.out.get(node_id, []):
            self.queue.append((target, msg, time.ticks_add(ticks(), ms),
                               hops + 1, to_port))

    def _wait_then_emit(self, node_id, msg, hops, ms):
        self.waiting.append((node_id, msg, hops, time.ticks_add(ticks(), ms)))

    def _waiting(self, node_id):
        for item in self.waiting:
            if item[0] == node_id:
                return True
        return False

    def _drop_waiting(self, node_id):
        self.waiting = [i for i in self.waiting if i[0] != node_id]

    def _do_logic_throttle(self, node_id, cfg, msg, hops):
        # "ms" is what the registry calls it; "every" always used the fallback.
        every = int(num(cfg.get("ms"), 1000))
        last = self.throttled.get(node_id)
        if last is not None and since(last) < every:
            return
        self.throttled[node_id] = ticks()
        self._emit(node_id, msg, hops)

    def _do_logic_toggle(self, node_id, cfg, msg, hops):
        """Each input decides: flip, force on, or force off."""
        port = self.arrived if self.arrived in ("in", "in2") else "in"
        action = cfg.get(port + "_action") or ("toggle" if port == "in" else "off")
        state = self.toggles.get(node_id, cfg.get("start") == "on")
        if action == "on":
            state = True
        elif action == "off":
            state = False
        else:
            state = not state
        self.toggles[node_id] = state
        out = dict(msg)
        out["payload"] = render(cfg.get("on_value" if state else "off_value",
                                        "1" if state else "0"), msg)
        self._emit(node_id, out, hops)

    def _do_camera_publish(self, node_id, cfg, msg, hops):
        """Nothing to do here: the host reads the flow to decide what goes
        on the wall, and asks this device for frames over its own
        connection."""
        self._emit(node_id, msg, hops)

    # -- the standard blocks ----------------------------------------------
    def _do_tag_set(self, node_id, cfg, msg, hops):
        """Write a shared tag, here and then everywhere."""
        name = (cfg.get("tag") or "").strip()
        if not name:
            return
        raw = cfg.get("value", "{{payload}}")
        whole = isinstance(raw, str) and raw.strip().startswith("{{") \
            and raw.strip().endswith("}}") and raw.strip().count("{{") == 1
        if whole:
            key = raw.strip()[2:-2].strip()
            value = msg.get("payload") if key == "payload" else render(raw, msg)
        else:
            value = render(raw, msg)
        self.agent.tags[name] = value
        self.agent.tags_written[name] = value
        out = dict(msg)
        meta = dict(out.get("meta") or {})
        meta["tag"] = name
        meta["written"] = value
        out["meta"] = meta
        self._emit(node_id, out, hops)

    def _do_tag_read(self, node_id, cfg, msg, hops):
        name = (cfg.get("tag") or "").strip()
        if not name or name not in self.agent.tags:
            self.agent.log("warn", "no tag called %s here" % name, node=node_id)
            return
        out = dict(msg)
        out["payload"] = self.agent.tags[name]
        meta = dict(out.get("meta") or {})
        meta["tag"] = name
        out["meta"] = meta
        self._emit(node_id, out, hops)

    def _do_math_expr(self, node_id, cfg, msg, hops):
        """The same evaluator the host runs, pulled as a module."""
        mod = _load("modules.expr")
        if not mod:
            self.agent.log("warn", "no formula module on this device",
                           node=node_id)
            return
        filled = render(cfg.get("expr") or "", msg)
        try:
            value = mod.evaluate(filled)
        except Exception as exc:
            self.agent.log("warn", "%s" % exc, node=node_id)
            return
        places = cfg.get("decimals")
        out = dict(msg)
        out["payload"] = mod.tidy(value, None if places in (None, "") else places)
        meta = dict(out.get("meta") or {})
        meta["formula"] = filled
        out["meta"] = meta
        self._emit(node_id, out, hops)

    def _do_math_deadband(self, node_id, cfg, msg, hops):
        mod = _load("modules.drive")
        if not mod:
            self.agent.log("warn", "no drive module on this device", node=node_id)
            return
        raw = num(msg.get("payload"), 0)
        value, inside = mod.deadband(raw, cfg)
        out = dict(msg)
        out["payload"] = (cfg.get("resting", "0") if inside
                          else round(value, int(num(cfg.get("decimals"), 3))))
        meta = dict(out.get("meta") or {})
        meta["raw"] = raw
        meta["inside"] = inside
        out["meta"] = meta
        self._emit(node_id, out, hops)

    def _do_math_split(self, node_id, cfg, msg, hops):
        mod = _load("modules.drive")
        if not mod:
            self.agent.log("warn", "no drive module on this device", node=node_id)
            return
        raw = num(msg.get("payload"), 0)
        value, forward = mod.split_drive(raw, cfg)
        out = dict(msg)
        out["payload"] = (value if isinstance(value, int)
                          else round(value, int(num(cfg.get("decimals"), 2))))
        meta = dict(out.get("meta") or {})
        meta["raw"] = raw
        meta["forward"] = forward
        out["meta"] = meta
        self._emit(node_id, out, hops)

    def _do_math_ramp(self, node_id, cfg, msg, hops):
        mod = _load("modules.drive")
        if not mod:
            self.agent.log("warn", "no drive module on this device", node=node_id)
            return
        start = num(cfg.get("start"), 0)
        out = dict(msg)
        meta = dict(out.get("meta") or {})
        if self.arrived == "reset":
            # Forget where it had got to, and emit nothing. A reset restores
            # state; it must not issue a command. Sending the start value on meant
            # disarming drove the wheels.
            self.ramps.pop(node_id, None)
            return
        target = num(msg.get("payload"), 0)
        now = ticks()
        held = self.ramps.get(node_id)
        if held is None:
            held = [start, now]
        value, arrived = mod.ramp_toward(held[0], target,
                                         time.ticks_diff(now, held[1]) / 1000.0,
                                         cfg)
        self.ramps[node_id] = [value, now]
        out["payload"] = round(value, int(num(cfg.get("decimals"), 3)))
        meta["target"] = target
        meta["arrived"] = arrived
        out["meta"] = meta
        self._emit(node_id, out, hops)

    def _do_logic_set(self, node_id, cfg, msg, hops):
        out = dict(msg)
        out["payload"] = render(cfg.get("value"), msg)
        self._emit(node_id, out, hops)

    def _do_host_notify(self, node_id, cfg, msg, hops):
        """Tell the host something happened."""
        kind = str(render(cfg.get("kind"), msg) or "event")[:32]
        self.agent.log(cfg.get("level") or "ok",
                       str(render(cfg.get("payload"), msg)), node=node_id,
                       payload=render(cfg.get("payload"), msg), kind=kind)
        self._emit(node_id, msg, hops)

    def _do_log_write(self, node_id, cfg, msg, hops):
        self.agent.log(cfg.get("level") or "idle",
                       str(render(cfg.get("message"), msg)), node=node_id,
                       payload=msg.get("payload"), kind="flow")
        self._emit(node_id, msg, hops)

    def _do_i2c_read(self, node_id, cfg, msg, hops):
        mod = _load("modules.i2c")
        if not mod:
            return
        out = dict(msg)
        out["payload"] = mod.read(cfg)
        self._emit(node_id, out, hops)

    def _do_i2c_write(self, node_id, cfg, msg, hops):
        mod = _load("modules.i2c")
        if mod:
            mod.write(cfg, msg)
        self._emit(node_id, msg, hops)

    def _do_i2c_scan(self, node_id, cfg, msg, hops):
        mod = _load("modules.i2c")
        out = dict(msg)
        out["payload"] = mod.scan(cfg) if mod else []
        self._emit(node_id, out, hops)

    def _do_camera_feed(self, node_id, cfg, msg, hops):
        """Standing arrangement by default; a switch when something is wired
        in."""
        want = msg.get("payload")
        if want is not None and str(want).strip().lower() not in (
                "", "0", "off", "false", "no", "none"):
            self.agent.start_camera(cfg.get("frame_size"), cfg.get("format"))
        elif want is not None:
            self.agent.stop_camera()
        self._emit(node_id, msg, hops)

    def _do_camera_capture(self, node_id, cfg, msg, hops):
        cam = getattr(self.agent, "camera", None)
        out = dict(msg)
        if not cam:
            self.agent.log("warn", "no camera on this device", node=node_id)
            return self._emit(node_id, out, hops)
        frame = cam.capture()
        size = len(frame) if frame else 0
        out["payload"] = size
        meta = dict(out.get("meta") or {})
        meta["frame"] = size
        out["meta"] = meta
        self._emit(node_id, out, hops)

    def _do_http_request(self, node_id, cfg, msg, hops):
        mod = _load("modules.http")
        out = dict(msg)
        if mod:
            status, body = mod.call(cfg, msg)
            out["payload"] = body
            out.setdefault("meta", {})["status"] = status
        self._emit(node_id, out, hops)


def _small(v):
    if v is None or isinstance(v, (bool, int, float)):
        return v
    return str(v)[:40]


def _load(name):
    try:
        return __import__(name, None, None, ("x",))
    except ImportError:
        return None


def render(text, msg):
    """{{payload}}, {{meta.x}}, {{device.x}} and {{tag.x}}."""
    if not isinstance(text, str) or "{{" not in text:
        return text
    out, rest = "", text
    while "{{" in rest:
        before, _, after = rest.partition("{{")
        name, close, rest = after.partition("}}")
        if not close:
            return out + before + "{{" + after
        key = name.strip()
        if key == "payload":
            value = msg.get("payload")
        elif key.startswith("meta."):
            value = (msg.get("meta") or {}).get(key[5:])
        elif key.startswith("device."):
            value = _about_me(key[7:])
        elif key.startswith("tag."):
            value = _my_tag(key[4:])
        elif key.startswith("pad."):
            value = _my_pad(key[4:])
        else:
            value = None
        out += before + (("{{" + name + "}}") if value is None else str(value))
    return out + rest


# The one Runner on this board, so {{device.x}} can answer for itself. Safe here
# in a way it would not be on the host: one flow, one polled thread.
_RUNNER = None


def _my_pad(key):
    """`{{pad.x}}` off whatever the Controller node last read."""
    if _RUNNER is None:
        return None
    state = _RUNNER.pad
    if not state:
        return None
    if key == "connected":
        return 1 if state.get("connected") else 0
    return state.get(key)


def _my_tag(name):
    """A tag as this board last heard it — the manifest, then any push since."""
    agent = getattr(_RUNNER, "agent", None)
    tags = getattr(agent, "tags", None) if agent is not None else None
    return tags.get(name) if tags else None


def _about_me(field):
    """`{{device.x}}` on the device it is about."""
    agent = getattr(_RUNNER, "agent", None)
    if agent is None:
        return None
    if field == "id":
        return getattr(agent, "device", None)
    if field == "name":
        return (getattr(agent, "cfg", None) or {}).get("name")
    if field == "board":
        return (getattr(agent, "cfg", None) or {}).get("board")
    if field == "ip":
        try:
            return agent.ip()
        except Exception:
            return None
    if field == "rssi":
        try:
            return agent.rssi()
        except Exception:
            return None
    if field == "uptime":
        # Since this run of the agent began, matching what it reports. Not
        # time.time(), which counts from power-on and survives a reset.
        return int(time.time() - getattr(agent, "started", 0))
    if field == "free":
        try:
            import gc as _gc
            _gc.collect()
            return _gc.mem_free()
        except Exception:
            return None
    if field == "online":
        return 1 if getattr(agent, "online", False) else 0
    if field == "seen":
        return 0            # it is this device: it is reporting right now
    return None


# Handlers in pulled modules reach these through the runner: they cannot import
# this file, which needs `machine`. Bound here because `render` is defined below.
Runner.num = staticmethod(num)
Runner.truthy = staticmethod(truthy)
Runner.render = staticmethod(render)
Runner.ticks = staticmethod(ticks)
Runner.since = staticmethod(since)
