"""Gamepad naming and scaling, shared by the host and a device.

The host fills the state dict from `zero2w_console/pad.py`. A device needs a BLE
transport, `modules/blepad.py`, which is not written yet; without it the nodes
load and report nothing.
"""

# Dropdown values to state keys; the option strings are the contract.
AXES = {
    "left stick x": "lx", "left stick y": "ly",
    "right stick x": "rx", "right stick y": "ry",
    "left trigger": "lt", "right trigger": "rt",
    "dpad x": "hx", "dpad y": "hy",
}

BUTTONS = {
    "a": "a", "b": "b", "x": "x", "y": "y", "lb": "lb", "rb": "rb",
    "left stick click": "ls", "right stick click": "rs",
    "view": "view", "menu": "menu", "xbox": "xbox",
    "dpad up": "up", "dpad down": "down",
    "dpad left": "left", "dpad right": "right",
}

# Triggers are 0..1, everything else -1..1, scaled before it reaches the dict.
ONE_WAY = ("lt", "rt")


def _num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def blank():
    """A state dict with every key present."""
    state = {"connected": False, "name": "", "at": 0, "seq": 0}
    for key in AXES.values():
        state[key] = 0.0
    for key in BUTTONS.values():
        state[key] = 0
        state["_" + key] = 0        # seen down since the last drain
    return state


def stale(state, cfg, age_ms):
    """Whether these values are too old to be a reading. Each runtime passes
    its own age, since they count time differently."""
    if not state or not state.get("connected"):
        return True
    limit = _num(cfg.get("stale_ms"), 250)
    return limit > 0 and age_ms > limit


def axis_value(state, cfg):
    """One axis, in the units the Dead zone expects: -1..1, or 0..1 for a
    trigger."""
    key = AXES.get(cfg.get("axis") or "left stick x")
    if key is None:
        return None
    value = _num(state.get(key), 0.0)
    if (cfg.get("invert") or "no") == "yes":
        value = -value
    low = 0.0 if key in ONE_WAY else -1.0
    if value < low:
        value = low
    elif value > 1.0:
        value = 1.0
    return round(value, int(_num(cfg.get("decimals"), 3)))


def button_value(state, cfg, gone=False):
    """One button per the node's Report setting; None means say nothing.

    A gone pad reads as released under `held`, so an arm built on it falls.
    """
    key = BUTTONS.get(cfg.get("button") or "a")
    if key is None:
        return None
    emit = cfg.get("emit") or "held"
    if emit == "held":
        return 0 if gone else (1 if state.get(key) else 0)
    if gone:
        return None
    seen = state.get("_" + key) or 0
    state["_" + key] = 0
    if emit == "pressed":
        return 1 if seen > 0 else None
    return 1 if seen < 0 else None          # released


def press(state, key, down):
    """Record a button edge, so a tap between two reads is not lost."""
    was = 1 if state.get(key) else 0
    now = 1 if down else 0
    state[key] = now
    if now != was:
        state["_" + key] = 1 if now else -1


# -- the device's node handlers (reached through flow.PULLED) ----------------
# Here rather than in blepad.py, so a pad flow still loads and fails safe on a
# board without a BLE module.

def _transport(r):
    """The thing that actually talks to a pad, or None on a board without one."""
    held = getattr(r, "pad_radio", None)
    if held is not None:
        return held or None
    try:
        held = __import__("modules.blepad", None, None, ("x",))
    except ImportError:
        held = False
    r.pad_radio = held
    return held or None


def _tell(r, state):
    """Report a connection change once, as an event: a field in the report
    would cost agent.py bytes it does not have."""
    now = 1 if state.get("connected") else 0
    if state.get("told") == now:
        return
    state["told"] = now
    try:
        r.agent.log("ok" if now else "idle",
                    ("controller connected: %s" % (state.get("name") or "?"))
                    if now else "controller gone",
                    kind="pad",
                    payload={"connected": bool(now),
                             "name": state.get("name") or "",
                             "address": state.get("address") or ""})
    except Exception:
        pass


def pad_link(r, node_id, cfg, msg, hops):
    state = r.pad
    if "connected" not in state:
        state.update(blank())
    radio = _transport(r)
    if r.truthy(msg.get("payload")):
        if radio is None:
            if not state.get("said"):
                state["said"] = 1
                r.agent.log("warn", "no Bluetooth module on this device — the "
                            "controller nodes will report nothing", node=node_id)
        else:
            radio.hold(r, state, (cfg.get("name") or "").strip())
            radio.drain(state)
    elif radio is not None:
        radio.drop(state)
    _tell(r, state)
    out = dict(msg)
    meta = dict(out.get("meta") or {})
    meta["pad"] = bool(state.get("connected"))
    meta["pad_name"] = state.get("name") or ""
    out["meta"] = meta
    r._emit(node_id, out, hops)


def pad_axis(r, node_id, cfg, msg, hops):
    state = r.pad
    age = r.since(state.get("at") or 0)
    if stale(state, cfg, age):
        return                          # the deadman: say nothing, starve the dog
    value = axis_value(state, cfg)
    if value is None:
        return
    out = dict(msg)
    out["payload"] = value
    meta = dict(out.get("meta") or {})
    meta["pad_axis"] = cfg.get("axis") or "left stick x"
    meta["pad_age"] = age
    out["meta"] = meta
    r._emit(node_id, out, hops)


def pad_button(r, node_id, cfg, msg, hops):
    state = r.pad
    gone = stale(state, cfg, r.since(state.get("at") or 0))
    value = button_value(state, cfg, gone)
    if value is None:
        return
    out = dict(msg)
    out["payload"] = value
    meta = dict(out.get("meta") or {})
    meta["pad_button"] = cfg.get("button") or "a"
    meta["pad_edge"] = cfg.get("emit") or "held"
    out["meta"] = meta
    r._emit(node_id, out, hops)
