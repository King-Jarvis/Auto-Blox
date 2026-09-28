#!/usr/bin/env python3
"""Flow engine — N8N-style graphs of triggers and actions, for GPIO and
network."""
import json
import os
import queue
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request

from . import buses
from . import mqtt as mqttmod
# One evaluator, in a file the device also pulls, so a formula cannot mean one
# thing here and another in the field. Same reasoning for the drive arithmetic.
from .agent.modules import expr as exprmod
from .agent.modules.drive import deadband, ramp_toward, split_drive
from .agent.modules import pad as padmod
from .agent.modules import tagwatch
from .agent.modules import pictures as picmod
from . import pixels
from . import pad as padmod_host
from . import bluetooth as btmod
from . import gatt as gattmod
from .agent.modules import blefmt

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".config", "zero2w-console")
# Where Save to SD keeps things on this board: its SD card, in plain sight.
CAPTURES = os.path.join(os.path.expanduser("~"), "captures")
FLOWS_FILE = os.path.join(CONFIG_DIR, "flows.json")

# The registry. `fields` drive the inspector UI; `inputs`/`outputs` drive ports.
def _f(key, label, kind, **kw):
    """A field definition."""
    d = {"key": key, "label": label, "kind": kind}
    d.update(kw)
    return d


# `runs` says where a node type can execute: "both" means a field device can run
# it from flash, "host" means it needs something only this board has. A flow
# bound to a board may only use nodes it can run, and the palette is filtered.
DEVICE_RUNS = ("both", "device")

# Everything this board may tell a field device to do. The agent implements
# exactly these, the HTTP route refuses anything else, and the Device command
# node offers them as a fixed list — one place, so the three cannot drift.
DEVICE_OPS = ("fire", "set", "camera-on", "camera-off",
              "reload", "resync", "stop", "reboot")


def runs_on_device(ntype):
    return REGISTRY.get(ntype, {}).get("runs", "host") in DEVICE_RUNS


# The fastest the wall will ask a device for a frame, and the fastest a streaming
# board is leased to send one. A 640x480 JPEG over the stream reaches the screen
# in tens of milliseconds, so this is about not flooding the radio, not a floor.
CAMERA_MIN_MS = 100


def toggle_action(cfg, to_port):
    """What an arriving message does to a Toggle, by the input it came in
    on."""
    port = (to_port or "in").strip() or "in"
    if port not in ("in", "in2"):
        port = "in"
    default = "toggle" if port == "in" else "off"
    action = (cfg or {}).get(port + "_action") or default
    return action if action in ("toggle", "on", "off") else default


def camera_nodes(flow):
    """Every camera node in a flow, so the host knows what a device is for."""
    return [n for n in (flow or {}).get("nodes", [])
            if (n.get("type") or "").startswith("camera.")]


def feeds_the_screen(flow):
    """Is this flow asking for its camera on the dashboard?"""
    for node in (flow or {}).get("nodes", []):
        if node.get("type") == "camera.publish":
            return node
    return None


def screen_interval_ms(flow):
    """How often the wall should ask this flow's device for a frame."""
    for node in (flow or {}).get("nodes", []):
        if node.get("type") != "camera.publish":
            continue
        try:
            want = int((node.get("config") or {}).get("every_ms") or 0)
        except (TypeError, ValueError):
            return None
        return max(CAMERA_MIN_MS, min(60000, want)) if want else None
    return None


def _feeders(flow, node_id):
    """Every node upstream of this one, however far back."""
    back = {}
    for edge in (flow or {}).get("edges", []):
        back.setdefault(edge.get("to"), []).append(edge.get("from"))
    seen, stack = set(), list(back.get(node_id, []))
    while stack:                                  # a graph, so it may loop
        here = stack.pop()
        if here in seen or here is None:
            continue
        seen.add(here)
        stack.extend(back.get(here, []))
    return seen


def advice(flow):
    """Things about a flow that are legal, saveable, and will not work."""
    out = []
    nodes = {n.get("id"): n for n in (flow or {}).get("nodes", [])}

    # A watchdog whose timeout is shorter than the cadence that feeds it starves
    # on every single cycle, by construction — and commands zero between each real
    # value, which reads at the bench as a motor that drives, pauses and drives
    # again. The shipped example pairs 100ms with 400ms.
    for node_id, node in nodes.items():
        if node.get("type") != "safety.watchdog":
            continue
        try:
            timeout = int(_num((node.get("config") or {}).get("timeout"), 500))
        except (TypeError, ValueError):
            continue
        periods = []
        for up in _feeders(flow, node_id):
            feeder = nodes.get(up) or {}
            if feeder.get("type") != "timer.interval":
                continue
            try:
                periods.append(int(_num((feeder.get("config") or {}).get("every"), 1000)))
            except (TypeError, ValueError):
                pass
        if not periods:
            continue                    # fed by a pin or a webhook; unknowable
        fastest = min(periods)
        if timeout <= fastest:
            out.append({"node": node_id, "level": "critical", "message":
                        "this watchdog gives up after %dms but the timer feeding "
                        "it only fires every %dms, so it will starve on every "
                        "cycle — everything downstream is driven to its safe "
                        "value once a second and back again"
                        % (timeout, fastest)})
        elif timeout < 2 * fastest:
            out.append({"node": node_id, "level": "warn", "message":
                        "this watchdog gives up after %dms and is fed every "
                        "%dms, which leaves nothing for jitter; the shipped "
                        "motor example allows four times the interval"
                        % (timeout, fastest)})
    return out


def runs_here(flow):
    """Does this host execute this flow?"""
    return not (flow or {}).get("board")


def registry_for(board=None):
    """The palette. With a board, only what that board can actually run."""
    if not board:
        return REGISTRY
    caps = set()
    for pin in board.get("pins", []):
        caps.update(pin.get("caps", []))
    per = board.get("peripherals", {})
    out = {}
    for ntype, spec in REGISTRY.items():
        if not runs_on_device(ntype):
            continue
        need = NEEDS.get(ntype)
        if need == "pwm" and "pwm" not in caps:
            continue
        if need == "i2c" and "i2c" not in board.get("buses", {}):
            continue
        if need == "wifi" and not per.get("wifi"):
            continue
        if need == "camera" and not per.get("camera"):
            continue
        if need == "ble" and not per.get("ble"):
            continue
        out[ntype] = spec
    return out


# What each node type puts into the message it passes on. Kept beside the
# registry the way NEEDS is and merged into REGISTRY below, so anything consuming
# a node definition still finds everything in one place. `kind` is documentation,
# not enforcement: the message is {payload, meta} and payload holds what a node
# put there.
def _o(name, kind, desc, example=None):
    return {"name": name, "kind": kind, "desc": desc, "example": example}


# A node that changes nothing: whatever arrived leaves again untouched.
PASSES_THROUGH = [
    _o("payload", "same", "Whatever arrived, unchanged."),
    _o("meta", "same", "Whatever arrived, unchanged."),
]

EMITS = {
    # -- triggers: these start a message rather than passing one on ---------
    "gpio.in": [
        _o("payload", "number", "The level after the edge.", 1),
        _o("meta.gpio", "number", "The pin that changed.", 263),
        _o("meta.edge", "text", "Which way it moved.", "rising"),
    ],
    "timer.interval": [
        _o("payload", "number", "Always 1. The tick is the message.", 1),
        _o("meta.tick", "bool", "Marks this as a timer firing.", True),
    ],
    "http.webhook": [
        _o("payload", "any", "The body that was POSTed, or its `payload` "
                             "field when it has one.", '{"open": true}'),
        _o("meta.webhook", "text", "The path it arrived on.", "ping"),
    ],
    "metric.threshold": [
        _o("payload", "number", "The reading that crossed the line.", 61.4),
        _o("meta.metric", "text", "Which reading.", "cpu-thermal"),
        _o("meta.threshold", "number", "The value it was compared against.", 60),
    ],
    "manual.fire": [
        _o("payload", "any", "1, or whatever the run request carried.", 1),
        _o("meta.manual", "bool", "Marks this as a hand-started run.", True),
    ],
    "mqtt.subscribe": [
        _o("payload", "text", "The message body.", "22.4"),
        _o("meta.topic", "text", "The exact topic it arrived on, which a "
                                 "wildcard subscription needs.", "home/+/temp"),
    ],
    "host.event": [
        _o("payload", "any", "The value the device sent.", 42),
        _o("meta.device", "text", "Which device reported.", "dev_ab12cd34"),
        _o("meta.kind", "text", "What it called the event.", "motion"),
        _o("meta.message", "text", "Its description of the event.", "pir tripped"),
    ],

    # -- logic --------------------------------------------------------------
    "logic.if": PASSES_THROUGH,
    "logic.delay": PASSES_THROUGH,
    "logic.throttle": PASSES_THROUGH,
    "logic.toggle": [
        _o("payload", "any", "The on value or the off value, depending on "
                             "which way it now sits.", "1"),
        _o("meta", "same", "Whatever arrived, unchanged."),
    ],
    "logic.set": [
        _o("payload", "any", "The value set here, with any {{variables}} "
                             "already filled in.", "closed"),
        _o("meta", "same", "Whatever arrived, unchanged."),
    ],

    "tag.set": [
        _o("payload", "same", "Whatever arrived, unchanged."),
        _o("meta.tag", "text", "The tag that was written.", "tank_level"),
        _o("meta.written", "any", "The value it now holds, as its type.", 72.5),
    ],
    "tag.read": [
        _o("payload", "any", "The tag's current value.", 72.5),
        _o("meta.tag", "text", "Which tag it came from.", "tank_level"),
    ],
    "tag.change": [
        _o("payload", "any", "The tag's new value.", 72.5),
        _o("meta.tag", "text", "Which tag changed.", "tank_level"),
        _o("meta.previous", "any", "What it held before.", 70.0),
    ],
    "pad.axis": [
        _o("payload", "number", "The axis, -1 to 1, or 0 to 1 for a trigger.", 0.42),
        _o("meta.pad_axis", "text", "Which axis it read.", "right trigger"),
        _o("meta.pad_age", "number", "Milliseconds since the pad last reported.", 18),
    ],
    "pad.button": [
        _o("payload", "number", "1 or 0, or nothing at all on an edge that did not happen.", 1),
        _o("meta.pad_button", "text", "Which button it read.", "a"),
        _o("meta.pad_edge", "text", "held, pressed or released.", "pressed"),
    ],
    "pad.link": [
        _o("payload", "same", "Whatever arrived, unchanged."),
        _o("meta.pad", "bool", "True while a controller is connected.", True),
        _o("meta.pad_name", "text", "What the connected controller calls itself.", "Xbox Wireless Controller"),
    ],
    "ble.link": [
        _o("payload", "same", "Whatever arrived, unchanged."),
        _o("meta.ble_connected", "bool", "Whether the device is connected now.", True),
        _o("meta.ble_device", "text", "Its Bluetooth address.", "AA:BB:CC:DD:EE:FF"),
    ],
    "ble.read": [
        _o("payload", "any", "The characteristic's value, decoded as Format says.", 87),
        _o("meta.uuid", "text", "The characteristic read.", "2a19"),
        _o("meta.hex", "text", "The raw bytes, as hex.", "57"),
    ],
    "ble.write": [
        _o("payload", "same", "Whatever arrived, unchanged."),
        _o("meta.uuid", "text", "The characteristic written.", "2a56"),
        _o("meta.written", "text", "The bytes sent, as hex.", "01"),
    ],
    "ble.notify": [
        _o("payload", "any", "The new value, decoded as Format says.", 72),
        _o("meta.uuid", "text", "The characteristic that notified.", "2a37"),
        _o("meta.hex", "text", "The raw bytes, as hex.", "0048"),
        _o("meta.ble_device", "text", "The device's address.", "AA:BB:CC:DD:EE:FF"),
    ],
    "logic.timer": [
        _o("payload", "same", "The message that survived the wait, unchanged.", 1),
        _o("meta.held_ms", "number", "How long it was held before coming "
                                     "through.", 2000),
    ],
    "logic.edge": [
        _o("payload", "same", "The value that arrived, unchanged.", 1),
        _o("meta.edge", "text", "Which way it moved to get here.", "rising"),
    ],
    "logic.count": [
        _o("payload", "number", "The count after this message.", 3),
        _o("meta.count", "number", "The same number, kept in meta so the "
                                   "payload can be replaced downstream.", 3),
        _o("meta.hit", "bool", "True on the message where the target was "
                               "reached.", True),
    ],
    "logic.hysteresis": [
        _o("payload", "any", "The on value or the off value.", "1"),
        _o("meta.state", "text", "Which way it now sits.", "on"),
        _o("meta.input", "number", "The reading that decided it.", 62.1),
    ],
    "logic.latch": [
        _o("payload", "any", "The on value while latched, the off value once "
                             "reset.", "1"),
        _o("meta.latched", "bool", "Whether it is currently held on.", 1),
        _o("meta.by", "text", "Which input last decided it.", "set"),
    ],
    "logic.step": [
        _o("payload", "any", "Whatever arrived on go, or on done when it left.", 1),
        _o("meta.step", "text", "The step's name.", "fill"),
        _o("meta.timed_out", "bool", "On left and next: whether Leave after "
                                     "moved it on rather than done.", 0),
    ],
    "safety.watchdog": [
        _o("payload", "any", "The value configured to send when the feed "
                             "stops.", "0"),
        _o("meta.starved", "bool", "1 when the feed stopped, 0 when it came "
                                   "back.", 1),
        _o("meta.silent_ms", "number", "How long it had been quiet.", 512),
    ],

    # -- maths --------------------------------------------------------------
    "math.expr": [
        _o("payload", "number", "The answer.", 21.7),
        _o("meta.formula", "text", "The sum as it was worked out, with the "
                                   "variables already filled in.",
           "(71 - 32) * 5 / 9"),
    ],
    "math.scale": [
        _o("payload", "number", "The reading in its new range.", 47.3),
        _o("meta.raw", "number", "What arrived, before scaling.", 1937),
        _o("meta.clamped", "bool", "True when the reading was outside the "
                                   "input range and was held at an end.", False),
    ],
    "math.smooth": [
        _o("payload", "number", "The smoothed reading.", 21.8),
        _o("meta.raw", "number", "The latest sample, unsmoothed.", 23.1),
        _o("meta.samples", "number", "How many readings it is working from.", 5),
    ],
    "math.deadband": [
        _o("payload", "any", "The value, or the resting value if it was too "
                             "small to count.", 0.42),
        _o("meta.raw", "number", "What arrived, before the dead zone.", 0.47),
        _o("meta.inside", "bool", "Whether it landed inside the dead zone.", 0),
    ],
    "math.split": [
        _o("payload", "number", "The part you asked for.", 64.0),
        _o("meta.raw", "number", "The signed value it came from.", -0.64),
        _o("meta.forward", "bool", "Whether that value meant forward, after "
                                   "any inversion.", 0),
    ],
    "math.ramp": [
        _o("payload", "number", "Where the ramp has got to.", 38.5),
        _o("meta.target", "number", "What it is heading for.", 100),
        _o("meta.arrived", "bool", "Whether it has caught up with the target.", 0),
    ],

    # -- actions ------------------------------------------------------------
    "gpio.out": [
        _o("payload", "number", "The level actually written.", 1),
        _o("meta", "same", "Whatever arrived, unchanged."),
    ],
    "pwm.out": [
        _o("payload", "number", "The duty cycle now running, as a percentage.", 30),
        _o("meta", "same", "Whatever arrived, unchanged."),
    ],
    "i2c.write": [
        _o("payload", "text", "The bytes written, as hex.", "04ff"),
        _o("meta", "same", "Whatever arrived, unchanged."),
    ],
    "i2c.read": [
        _o("payload", "any", "The bytes read, as a number or a list "
                             "depending on how this node is set to read them.", 512),
        _o("meta", "same", "Whatever arrived, unchanged."),
    ],
    "i2c.scan": [
        _o("payload", "text", "Every address that answered, as hex.", "0x3c, 0x68"),
        _o("meta", "same", "Whatever arrived, unchanged."),
    ],
    "spi.transfer": [
        _o("payload", "text", "What came back, as spaced hex.", "00 1f a2"),
        _o("meta", "same", "Whatever arrived, unchanged."),
    ],
    "mqtt.publish": [
        _o("payload", "same", "Whatever arrived, unchanged."),
        _o("meta.topic", "text", "The topic it was published to.", "home/door"),
    ],
    "flow.call": [
        _o("payload", "same", "Whatever arrived, unchanged."),
        _o("meta._depth", "number", "How many sub-flows deep this is. Capped "
                                    "at 8, so a loop cannot run away.", 1),
    ],
    "http.request": [
        _o("payload", "any", "The response body — parsed when it is JSON, "
                             "text otherwise.", '{"ok": true}'),
        _o("meta.status", "number", "The HTTP status it came back with.", 200),
    ],
    "shell.run": [
        _o("payload", "text", "Everything the command printed.", "up 4 days"),
        _o("meta.code", "number", "Its exit status. 0 is success.", 0),
    ],
    "camera.feed": PASSES_THROUGH,
    "camera.capture": [
        _o("payload", "number", "The size of the picture taken, in bytes.", 14336),
        _o("meta.frame", "number", "The same size, so a later node can read "
                                   "it without the payload being in the way.", 14336),
        _o("meta.format", "text", "jpeg, grayscale or rgb565.", "jpeg"),
    ],
    "picture.send": PASSES_THROUGH,
    "sd.save": [
        _o("payload", "same", "Whatever arrived, unchanged."),
        _o("meta.file", "text", "Where it was saved.", "/sd/captures/20260928-141503-001.jpg"),
    ],
    "camera.publish": PASSES_THROUGH,
    "log.write": PASSES_THROUGH,
    "host.notify": PASSES_THROUGH,
    "device.command": [
        _o("payload", "same", "Whatever arrived, unchanged."),
        _o("meta.device", "text", "The device or group chosen.", "group:wheels"),
        _o("meta.devices", "list", "Every device the command went to.",
           ["dev_ab12cd34"]),
    ],
}


# What a node type needs from the board beyond plain GPIO.
NEEDS = {
    "pwm.out": "pwm",
    "i2c.write": "i2c", "i2c.read": "i2c", "i2c.scan": "i2c",
    "http.request": "wifi",
    "camera.feed": "camera", "camera.capture": "camera",
    "pad.link": "ble", "pad.axis": "ble", "pad.button": "ble",
    "ble.link": "ble", "ble.read": "ble", "ble.write": "ble", "ble.notify": "ble",
}

REGISTRY = {
    # ---- triggers --------------------------------------------------------
    "gpio.in": {
        "label": "GPIO edge", "group": "Triggers", "kind": "trigger", "runs": "both",
        "series": "series-1", "glyph": "pin",
        "summary": "Fires when a header pin changes level.",
        "inputs": [], "outputs": ["out"],
        "docs": [
            "Watches one input pin and fires on each edge. Debounce ignores "
            "further edges for that many milliseconds, which a mechanical "
            "button needs or one press fires several times.",
            "The payload is 1 on a rising edge and 0 on a falling one, and "
            "{{meta.edge}} holds the word.",
        ],
        "fields": [
            _f("gpio", "Pin", "pin"),
            _f("edges", "Edge", "select", options=["rising", "falling", "both"], default="both"),
            _f("debounce", "Debounce (ms)", "number", default=50, min=0, max=5000),
        ],
    },
    "timer.interval": {
        "label": "Interval", "group": "Triggers", "kind": "trigger", "runs": "both",
        "series": "series-2", "glyph": "clock",
        "summary": "Fires on a fixed period.",
        "inputs": [], "outputs": ["out"],
        "fields": [_f("every", "Every (ms)", "number", default=1000, min=50, max=86400000)],
    },
    "http.webhook": {
        "label": "Webhook", "group": "Triggers", "kind": "trigger", "runs": "host",
        "series": "series-3", "glyph": "globe",
        "summary": "Fires when something POSTs to a URL on this board.",
        "inputs": [], "outputs": ["out"],
        "docs": [
            "Gives this flow a URL. Anything that can make an HTTP request "
            "can start the flow: another script, Home Assistant, a phone "
            "shortcut, a webhook from another service on your LAN.",
            "The request must carry the console token, the same as every "
            "other route. Send it as an X-Console-Token header, or append "
            "?t=<token> to the URL.",
            "Whatever you POST becomes the message. Send JSON with a "
            "\"payload\" key and that value arrives as {{payload}}; send any "
            "other JSON and the whole object arrives as the payload. The "
            "path you matched is available as {{meta.topic}}.",
            "Paths are matched exactly, and each path belongs to one node. "
            "A POST to a path with no webhook node returns 404.",
        ],
        "fields": [_f("path", "Path", "text", default="my-hook",
                      help="The last part of the URL. Letters, digits and "
                           "dashes; no leading slash.")],
    },
    "metric.threshold": {
        "label": "Metric threshold", "group": "Triggers", "kind": "trigger", "runs": "host",
        "series": "series-4", "glyph": "gauge",
        "summary": "Fires when a board reading crosses a value.",
        "inputs": [], "outputs": ["out"],
        "docs": [
            "Checks the reading every 2 seconds. It fires once on crossing, "
            "not repeatedly while it stays over: re-arm is how long before "
            "it may fire again, and it re-arms immediately once the reading "
            "comes back the other side.",
            "The payload is the reading that crossed; {{meta.threshold}} is "
            "the limit you set.",
        ],
        "fields": [
            _f("metric", "Metric", "select", default="cpu-thermal",
               options=["cpu-thermal", "gpu-thermal", "ve-thermal", "ddr-thermal",
                        "cpu-percent", "load1", "memory-percent", "root-percent"]),
            _f("op", "When", "select", options=["above", "below"], default="above"),
            _f("value", "Value", "number", default=60),
            _f("rearm", "Re-arm after (s)", "number", default=30, min=0, max=86400),
        ],
    },
    "manual.fire": {
        "label": "Manual", "group": "Triggers", "kind": "trigger", "runs": "both",
        "series": "series-1", "glyph": "play",
        "summary": "Fires only when you press Run in the editor.",
        "inputs": [], "outputs": ["out"],
        "fields": [],
    },

    "mqtt.subscribe": {
        "label": "MQTT subscribe", "group": "Triggers", "kind": "trigger", "runs": "host",
        "series": "series-3", "glyph": "globe",
        "summary": "Fires on every message published to a topic.",
        "inputs": [], "outputs": ["out"],
        "fields": [
            _f("host", "Broker host", "text", default="localhost"),
            _f("port", "Port", "number", default=1883, min=1, max=65535),
            _f("topic", "Topic", "text", default="zero2w/#",
               help="MQTT wildcards work: + for one level, # for the rest."),
            _f("username", "Username", "text", default=""),
            _f("password", "Password", "text", default=""),
            _f("tls", "TLS", "select", options=["no", "yes"], default="no"),
        ],
    },

    # ---- logic -----------------------------------------------------------
    "logic.if": {
        "label": "If", "group": "Logic", "kind": "logic", "runs": "both",
        "series": "series-2", "glyph": "branch",
        "summary": "Routes a message by comparing the payload.",
        "inputs": ["in"], "outputs": ["true", "false"],
        "fields": [
            _f("op", "Operator", "select", default="==",
               options=["==", "!=", ">", ">=", "<", "<=", "truthy"]),
            _f("value", "Compare with", "text", default="1"),
        ],
    },
    "logic.delay": {
        "label": "Delay", "group": "Logic", "kind": "logic", "runs": "both",
        "series": "series-2", "glyph": "clock",
        "summary": "Holds the message, then passes it on.",
        "inputs": ["in"], "outputs": ["out"],
        "fields": [_f("ms", "Delay (ms)", "number", default=500, min=0, max=600000)],
    },
    "logic.throttle": {
        "label": "Throttle", "group": "Logic", "kind": "logic", "runs": "both",
        "series": "series-2", "glyph": "filter",
        "summary": "Drops messages arriving faster than a rate.",
        "inputs": ["in"], "outputs": ["out"],
        "fields": [_f("ms", "Minimum gap (ms)", "number", default=1000, min=0, max=600000)],
    },
    "logic.toggle": {
        "label": "Toggle", "group": "Logic", "kind": "logic", "runs": "both",
        "series": "series-2", "glyph": "switch",
        "summary": "Holds on or off, and says which on every message.",
        # Two inputs, each anchored on its own side so a graph can show what it
        # means. The left one keeps the name every saved edge already uses.
        "inputs": [{"name": "in", "side": "left", "label_from": "in_action"},
                   {"name": "in2", "side": "top", "label_from": "in2_action"}],
        "outputs": ["out"],
        "docs": [
            "Each input decides what a message arriving there does: **flip** "
            "it, force it **on**, or force it **off**. The left one flips by "
            "default, and the first flip moves it off **Start**.",
            "Set left to *on* and top to *off* and it becomes a latch with a "
            "separate switch for each direction, which is what you want when "
            "one trigger starts something and a different one stops it.",
        ],
        "fields": [
            _f("in_action", "Left input does", "select",
               options=["toggle", "on", "off"], default="toggle"),
            _f("in2_action", "Top input does", "select",
               options=["toggle", "on", "off"], default="off"),
            _f("on_value", "On value", "text", default="1"),
            _f("off_value", "Off value", "text", default="0"),
            _f("start", "Start", "select", options=["off", "on"], default="off",
               help="Where it sits before anything arrives. Feed the output to "
                    "a PWM or GPIO node to latch that node on and off."),
        ],
    },
    "logic.set": {
        "label": "Set value", "group": "Logic", "kind": "logic", "runs": "both",
        "series": "series-2", "glyph": "edit",
        "summary": "Replaces the payload with a fixed value.",
        "inputs": ["in"], "outputs": ["out"],
        "fields": [_f("value", "Payload", "text", default="1")],
    },

    # ---- actions ---------------------------------------------------------
    "tag.set": {
        "label": "Write tag", "group": "Logic", "kind": "action", "runs": "both",
        "series": "series-1", "glyph": "tag",
        "summary": "Puts a value into the board's tag table.",
        "inputs": ["in"], "outputs": ["out"],
        "docs": [
            "A tag is shared memory: anything can write it, anything can read "
            "it as `{{tag.name}}`, and a **Tag change** trigger can start a "
            "flow when it moves. The thing that measures something does not "
            "need to know what cares about it.",
            "The message passes through unchanged, so this can sit in the "
            "middle of a chain and record what went past.",
        ],
        "fields": [
            _f("tag", "Tag", "combo", default="", options_from="tags",
               placeholder="— choose a tag —",
               empty="No tags defined yet — add some in the tag table."),
            _f("value", "Value", "text", default="{{payload}}",
               help="Anything, including a {{variable}}. It is converted to "
                    "the tag's type on the way in."),
        ],
    },
    "tag.read": {
        "label": "Read tag", "group": "Logic", "kind": "action", "runs": "both",
        "series": "series-1", "glyph": "tag",
        "summary": "Puts a tag's current value into the message.",
        "inputs": ["in"], "outputs": ["out"],
        "docs": [
            "Any field can already read a tag with `{{tag.name}}`. This is for "
            "when the tag should become the payload itself — feeding it into "
            "a comparison, a scale, or anything else that works on whatever "
            "arrives.",
        ],
        "fields": [
            _f("tag", "Tag", "combo", default="", options_from="tags",
               placeholder="— choose a tag —",
               empty="No tags defined yet."),
        ],
    },
    "tag.change": {
        "label": "Tag change", "group": "Triggers", "kind": "trigger", "runs": "both",
        "series": "series-1", "glyph": "tag",
        "summary": "Fires when a tag's value changes.",
        "inputs": [], "outputs": ["out"],
        "docs": [
            "The other half of shared memory. A flow that writes a tag does "
            "not need to know this exists, and this does not need to know "
            "which flow wrote it — which is how one reading can drive three "
            "unrelated things without any of them being wired together.",
            "It fires on a *change*. Writing the same value again is not one, "
            "and *becomes true* means false before and true now.",
            "On a device it watches the device's own copy of the tags: shared "
            "values the fleet pushes, and anything its own flow writes. The "
            "values a board receives when it syncs are where it starts, not "
            "a change.",
        ],
        "fields": [
            _f("tag", "Tag", "combo", default="", options_from="tags",
               placeholder="— any tag —",
               empty="No tags defined yet."),
            _f("when", "Only when it", "select", default="changes",
               options=["changes", "becomes true", "becomes false",
                        "rises", "falls"]),
        ],
    },
    "pad.axis": {
        "label": "Controller axis", "group": "Logic", "kind": "action", "runs": "both",
        "series": "series-3", "glyph": "gauge",
        "summary": "Puts a stick or trigger's current position into the message.",
        "inputs": ["in"], "outputs": ["out"],
        "docs": [
            "The payload becomes one number: **-1 to 1** for a stick or a "
            "d-pad, **0 to 1** for a trigger. That is what a Dead zone "
            "expects, so this wires straight into one.",
            "**It emits nothing when the pad is not there.** Not zero — "
            "nothing. A Watchdog downstream then starves and drives whatever "
            "it guards to its safe value, which is how a controller walking "
            "out of range stops a motor. A zero would look the same on the "
            "canvas and would stop nothing if this node were what failed.",
            "Put a **Controller** node upstream: it is what opens the pad, and "
            "this only reports what the pad last sent.",
            "Sticks rest near centre but not on it. Put a Dead zone after "
            "this, not a comparison.",
        ],
        "fields": [
            _f("axis", "Axis", "select", default="left stick x",
               options=["left stick x", "left stick y",
                        "right stick x", "right stick y",
                        "left trigger", "right trigger",
                        "dpad x", "dpad y"]),
            _f("invert", "Invert", "select", default="no", options=["no", "yes"],
               help="Forward on a stick already reads positive here. Turn this "
                    "on for an axis that still feels backwards."),
            _f("stale_ms", "Values go stale after (ms)", "number",
               default=250, min=50, max=10000,
               help="Past this since the pad last reported, its sticks are not "
                    "a reading any more and this node goes quiet — which is "
                    "what lets a Watchdog notice the pad has gone."),
            _f("decimals", "Decimal places", "number", default=3, min=0, max=6),
        ],
    },
    "pad.button": {
        "label": "Controller button", "group": "Logic", "kind": "action", "runs": "both",
        "series": "series-3", "glyph": "switch",
        "summary": "Puts a controller button's state into the message.",
        "inputs": ["in"], "outputs": ["out"],
        "docs": [
            "**Held** makes the payload 1 while the button is down and 0 while "
            "it is up — including 0 once the pad has gone, so an arm built on "
            "it releases by itself. **Pressed** and **Released** emit 1 on "
            "that edge and nothing otherwise, which is one message per press "
            "however long you hold it.",
            "A **Toggle** after *Pressed* and a **Set tag** after that is a "
            "latching switch you can press once. A latch is convenient and is "
            "**not** a deadman: a tag stays set until something clears it — "
            "mark the tag to reset when a board boots. For an arm that falls "
            "the moment you let go, use *Held* and no Toggle.",
            "Needs a **Controller** node upstream, the same as Controller axis.",
        ],
        "fields": [
            _f("button", "Button", "select", default="a",
               options=["a", "b", "x", "y", "lb", "rb",
                        "left stick click", "right stick click",
                        "view", "menu", "xbox",
                        "dpad up", "dpad down", "dpad left", "dpad right"]),
            _f("emit", "Report", "select", default="held",
               options=["held", "pressed", "released"]),
            _f("stale_ms", "Values go stale after (ms)", "number",
               default=250, min=50, max=10000,
               help="Past this, *Held* reports 0 and the edges report nothing."),
        ],
    },
    "logic.timer": {
        "label": "Timer", "group": "Logic", "kind": "logic", "runs": "both",
        "series": "series-2", "glyph": "clock",
        "summary": "Waits for a condition to hold, or stretches one out.",
        "inputs": ["in"], "outputs": ["out"],
        "docs": [
            "**Wait until it has been true for** is the one you want most: a "
            "message gets through only if nothing contradicted it for the "
            "whole time. A door sensor that flickers never reaches the alarm; "
            "one genuinely left open does. It is the PLC's on-delay.",
            "**Keep it true for** goes the other way — the message gets "
            "through at once, and the *off* is what waits. A light that stays "
            "on for a minute after the last movement. That is the off-delay.",
            "**One and then nothing for** passes the first message straight "
            "through and ignores everything else until the time is up, which "
            "is how you debounce anything that arrives in bursts.",
            "Anything not 0, empty, `false`, `off` or `no` counts as true.",
        ],
        "fields": [
            _f("mode", "Behaviour", "select", default="wait until true for",
               options=["wait until true for", "keep it true for",
                        "one and then nothing for"]),
            _f("ms", "For (ms)", "number", default=2000, min=1, max=3600000),
        ],
    },
    "logic.edge": {
        "label": "On change", "group": "Logic", "kind": "logic", "runs": "both",
        "series": "series-2", "glyph": "step",
        "summary": "Passes a message on only when the value changes.",
        "inputs": ["in"], "outputs": ["out"],
        "docs": [
            "A level becomes an event. A pin that reads 1 fifty times a second "
            "is fifty messages; this lets one through — the one where it "
            "stopped being 0 — and stops the rest.",
            "Anything not 0, empty, `false`, `off` or `no` counts as true.",
        ],
        "fields": [
            _f("edges", "Pass on", "select", default="rising",
               options=["rising", "falling", "any"],
               help="Rising is false to true. Falling is true to false."),
        ],
    },
    "logic.count": {
        "label": "Counter", "group": "Logic", "kind": "logic", "runs": "both",
        "series": "series-2", "glyph": "hash",
        "summary": "Counts messages, and can fire when it reaches a number.",
        # Reset gets its own anchor, so a graph shows what clears it.
        "inputs": [{"name": "in", "side": "left"},
                   {"name": "reset", "side": "top", "label": "reset"}],
        "outputs": ["out"],
        "docs": [
            "Counts every message that arrives on the left. A message on the "
            "top input sets it back to the starting value, whatever it was "
            "carrying.",
            "With a target of 0 it passes every message on, carrying the count. "
            "With a target set it stays quiet until it gets there — which is "
            "how you do *after the third time*.",
        ],
        "fields": [
            _f("step", "Count by", "number", default=1),
            _f("start", "Start at", "number", default=0),
            _f("target", "Fire at", "number", default=0,
               help="0 passes every message on. Any other number stays quiet "
                    "until the count reaches it."),
            _f("auto_reset", "Then", "select", default="reset",
               options=["reset", "keep counting"], showIf={"target": None}),
        ],
    },
    "logic.hysteresis": {
        "label": "Hysteresis", "group": "Logic", "kind": "logic", "runs": "both",
        "series": "series-2", "glyph": "wave",
        "summary": "Turns on at one level and off at another, so it cannot chatter.",
        "inputs": ["in"], "outputs": ["out"],
        "docs": [
            "One threshold makes a reading sitting on the line switch on and "
            "off over and over. Two thresholds with a gap between them cannot: "
            "once it is on, the value has to come all the way back down past "
            "the lower number before it goes off again.",
            "A thermostat is this. So is a tank pump, a fan, and anything else "
            "driven by a sensor that wobbles.",
        ],
        "fields": [
            _f("on_above", "Switch on above", "number", default=1),
            _f("off_below", "Switch off below", "number", default=0),
            _f("on_value", "On value", "text", default="1"),
            _f("off_value", "Off value", "text", default="0"),
            _f("emit", "Pass on", "select", default="on change",
               options=["on change", "every message"]),
        ],
    },
    "logic.latch": {
        "label": "Latch", "group": "Logic", "kind": "logic", "runs": "both",
        "series": "series-2", "glyph": "switch",
        "summary": "Remembers that something happened until something else clears it.",
        "inputs": [{"name": "in", "side": "left", "label": "set"},
                   {"name": "reset", "side": "top", "label": "reset"}],
        "outputs": ["out"],
        "docs": [
            "The ladder-logic seal-in, and the block every machine needs: a "
            "message on **set** latches it on and it stays on — through every "
            "message that follows — until a message on **reset** clears it.",
            "This is how you build an arm switch, a fault that has to be "
            "acknowledged, or a start button that does not have to be held. "
            "Pair it with a Watchdog on reset and a motor cannot restart by "
            "itself after a fault; it has to be armed again on purpose.",
            "Messages arrive one at a time, so the last one decides and there "
            "is no set-versus-reset priority to configure. If something must "
            "always win, wire it so it is the thing that speaks last — a "
            "Watchdog into reset does exactly that.",
        ],
        "fields": [
            _f("start", "Starts", "select", default="off", options=["off", "on"]),
            _f("on_value", "On value", "text", default="1"),
            _f("off_value", "Off value", "text", default="0"),
            _f("emit", "Pass on", "select", default="on change",
               options=["on change", "every message"]),
        ],
    },
    "logic.step": {
        "label": "Step", "group": "Logic", "kind": "logic", "runs": "both",
        "series": "series-2", "glyph": "list",
        "summary": "One stage of a sequence: active from go until done.",
        "inputs": [{"name": "in", "side": "top", "label": "go"},
                   {"name": "done", "side": "left", "label": "done"},
                   {"name": "reset", "side": "left", "label": "reset"}],
        "outputs": [{"name": "entered", "side": "right", "label": "entered"},
                    {"name": "left", "side": "right", "label": "left"},
                    {"name": "next", "side": "bottom", "label": "next"}],
        "docs": [
            "A sequence is Steps wired **next → go**, one under the other. A "
            "step is either **active** or not. A message on **go** makes it "
            "active; a message on **done** moves it on **only while it is "
            "active**, so a sensor that trips during some other step does "
            "nothing here.",
            "**entered** fires once on arrival: start the stage's action from "
            "it. **left** fires once on the way out: stop the action from it. "
            "**next** then carries the message to the following step's go. "
            "Wire the last step's next back to the first step's go to loop, "
            "or leave it unwired to stop.",
            "Conditions are their own nodes, wired into done: an **If**, a "
            "**Tag change**, a **Timer**. **Leave after** moves on by itself "
            "once the time is up, and {{meta.timed_out}} says which way it "
            "left.",
            "A second go while active is ignored, and **reset** makes it idle "
            "without saying anything. The canvas highlights a step while it "
            "is active. After a reboot every step is idle: a sequence starts "
            "when something sends go.",
        ],
        "fields": [
            _f("name", "Name", "text", default="",
               help="Shown as {{meta.step}}. Empty uses the node's id."),
            _f("leave_after", "Leave after (ms)", "number", default=0, min=0,
               max=86400000,
               help="Move on by itself this long after arriving. 0 waits for "
                    "done however long it takes."),
        ],
    },
    "safety.watchdog": {
        "label": "Watchdog", "group": "Logic", "kind": "logic", "runs": "both",
        "series": "series-2", "glyph": "clock",
        "summary": "Fires when the messages it was being fed stop arriving.",
        "inputs": ["in"], "outputs": ["out"],
        "docs": [
            "Everything else in a flow fires because something happened. This "
            "one fires because something *stopped* happening, which is the "
            "only way a graph can notice a link going quiet.",
            "Feed it from whatever is meant to keep arriving — a controller, a "
            "sensor, a heartbeat from the host — and wire its output to "
            "whatever should happen when that stops. On a moving machine that "
            "means stopping the drive: the value that comes out is 0, so it "
            "can go straight into a PWM output or a GPIO write.",
            "**It starts watching on the first message, not at boot.** A node "
            "does nothing by existing, so a flow that is never fed never "
            "trips — and a board that has just come up is not instantly in a "
            "fault it did nothing to earn.",
            "This runs on the device, which is the point. It keeps working "
            "when the host reboots, the wifi drops, or a controller walks out "
            "of range — the cases where nothing else can help.",
        ],
        "fields": [
            _f("timeout", "Quiet for (ms)", "number", default=500, min=20,
               max=600000,
               help="How long without a message counts as stopped. A little "
                    "longer than the gap you expect, or a slow round trip "
                    "trips it."),
            _f("value", "Send", "text", default="0",
               help="What to pass on when it trips. 0 goes straight into a "
                    "PWM duty or a pin."),
            _f("recovery", "Also say when it comes back", "select", default="no",
               options=["no", "yes"],
               help="Sends the first message after a trip on as well, so a "
                    "flow can re-arm itself."),
        ],
    },
    "math.expr": {
        "label": "Formula", "group": "Maths", "kind": "logic", "runs": "both",
        "series": "series-3", "glyph": "sigma",
        "summary": "Works out a sum and passes the answer on.",
        "inputs": ["in"], "outputs": ["out"],
        "docs": [
            "One node instead of an Add node, a Multiply node and a Divide "
            "node. Put values in with `{{payload}}`, `{{tag.name}}` or any "
            "other variable, and the rest is ordinary arithmetic: "
            "`({{payload}} - 32) * 5 / 9`.",
            "Understands `+ - * / // % **`, brackets, comparisons (which give "
            "1 or 0, ready for an **If**), and `abs`, `min`, `max`, `round`, "
            "`int`, `floor`, `ceil`, `sqrt`, `clamp`, `pi`, `e`.",
            "It is arithmetic, not a program: there are no names, no strings "
            "and no calls beyond that list. A formula that cannot be worked "
            "out says why and stops the branch rather than passing a wrong "
            "number on.",
        ],
        "fields": [
            _f("expr", "Formula", "textarea", default="{{payload}}",
               help="Variables are filled in first, then the sum is worked "
                    "out."),
            _f("decimals", "Decimal places", "number", default=None, min=0,
               max=9, help="Leave empty to keep the full answer."),
        ],
    },
    "math.scale": {
        "label": "Scale", "group": "Maths", "kind": "logic", "runs": "both",
        "series": "series-3", "glyph": "ruler",
        "summary": "Maps a number from one range onto another.",
        "inputs": ["in"], "outputs": ["out"],
        "docs": [
            "Raw readings are in the units the hardware felt like. A 12-bit ADC "
            "gives 0–4095; a sensor datasheet says that means 0–100%. This is "
            "that sum, so the rest of the flow works in real units.",
            "Ranges may run backwards — mapping 0–4095 onto 100–0 inverts it.",
        ],
        "fields": [
            _f("in_min", "From", "number", default=0),
            _f("in_max", "to", "number", default=4095),
            _f("out_min", "Onto", "number", default=0),
            _f("out_max", "to", "number", default=100),
            _f("clamp", "Keep inside the range", "select", default="yes",
               options=["yes", "no"],
               help="A reading outside the input range maps outside the "
                    "output range unless this holds it at the ends."),
            _f("decimals", "Decimal places", "number", default=1, min=0, max=6),
        ],
    },
    "math.smooth": {
        "label": "Smooth", "group": "Maths", "kind": "logic", "runs": "both",
        "series": "series-3", "glyph": "wave",
        "summary": "Evens out a noisy reading over the last few samples.",
        "inputs": [{"name": "in", "side": "left"},
                   {"name": "reset", "side": "top", "label": "reset"}],
        "outputs": ["out"],
        "docs": [
            "One noisy reading makes a threshold fire at random. An average "
            "over the last few makes it behave. **Running average** keeps the "
            "last N and means them; **weighted** leans on the newest reading "
            "and needs only one number in memory, which matters on a device.",
            "**Lowest** and **highest** report the extreme over the window, "
            "which is how you catch a spike that an average would hide.",
            "A message on the top input forgets what it has seen.",
        ],
        "fields": [
            _f("mode", "Using", "select", default="running average",
               options=["running average", "weighted", "lowest", "highest"]),
            _f("window", "Over the last", "number", default=5, min=1, max=64,
               showIf={"mode": ["running average", "lowest", "highest"]},
               help="How many samples to keep."),
            _f("weight", "Lean on the newest", "number", default=0.3,
               min=0.01, max=1, showIf={"mode": ["weighted"]},
               help="1 is no smoothing at all; 0.1 is very smooth and slow."),
            _f("decimals", "Decimal places", "number", default=2, min=0, max=6),
        ],
    },
    "math.deadband": {
        "label": "Dead zone", "group": "Maths", "kind": "logic", "runs": "both",
        "series": "series-3", "glyph": "filter",
        "summary": "Ignores small values, so a resting control reads as nothing.",
        "inputs": ["in"], "outputs": ["out"],
        "docs": [
            "A joystick does not return to exactly centre, a load cell drifts, "
            "and a potentiometer is never quite still. Anything smaller than "
            "the threshold, either side of zero, becomes the resting value.",
            "**Rescale** matters more than it sounds. Without it the output "
            "jumps from 0 straight to the threshold the moment you cross it. "
            "With it the remaining range is stretched back out, so the output "
            "still starts from nothing and still reaches full.",
        ],
        "fields": [
            _f("threshold", "Ignore below", "number", default=0.1, min=0,
               help="Compared against the size of the value, so it covers "
                    "both directions."),
            _f("full", "Full scale", "number", default=1,
               help="The largest value you expect, used to stretch the rest "
                    "of the range back out."),
            _f("rescale", "Rescale the rest", "select", default="yes",
               options=["yes", "no"]),
            _f("resting", "Resting value", "text", default="0"),
            _f("decimals", "Decimal places", "number", default=3, min=0, max=6),
        ],
    },
    "math.ramp": {
        "label": "Ramp", "group": "Maths", "kind": "logic", "runs": "both",
        "series": "series-3", "glyph": "up",
        "summary": "Limits how fast a value is allowed to change.",
        "inputs": [{"name": "in", "side": "left"},
                   {"name": "reset", "side": "top", "label": "reset"}],
        "outputs": ["out"],
        "docs": [
            "The value that comes out chases the value that goes in, but no "
            "faster than the rate you set. Slamming a stick to full gives a "
            "motor that winds up instead of one that lurches, and the same "
            "block keeps a heater, a valve or a lamp from stepping.",
            "Separate rates each way, because most things should be allowed "
            "to stop faster than they start. A rate of 0 means no limit in "
            "that direction.",
            "**It advances when messages arrive, not on a clock.** Feed it "
            "steadily and it is smooth. If the messages stop it holds where "
            "it was rather than coasting to zero — stopping on silence is a "
            "Watchdog's job. **Longest step** caps how much of a gap one message "
            "may count, so a stall is not followed by a jump; raise it for a "
            "flow that ticks slower than that on purpose.",
            "A message on the top input snaps it back to the starting value "
            "with no ramping at all.",
        ],
        "fields": [
            _f("rate_up", "Rise by", "number", default=1, min=0,
               help="Units per second, away from zero. 0 is no limit."),
            _f("rate_down", "Fall by", "number", default=2, min=0,
               help="Units per second, back towards zero. 0 is no limit."),
            _f("max_gap_ms", "Longest step (ms)", "number", default=250, min=0,
               max=60000,
               help="A gap between messages longer than this counts as this "
                    "long. Keep it above the tick feeding the ramp; 0 counts "
                    "the whole gap."),
            _f("start", "Start at", "number", default=0),
            _f("decimals", "Decimal places", "number", default=3, min=0, max=6),
        ],
    },
    "math.split": {
        "label": "Sign split", "group": "Maths", "kind": "logic", "runs": "both",
        "series": "series-3", "glyph": "branch",
        "summary": "Takes a signed value and gives you one part of it.",
        "inputs": ["in"], "outputs": ["out"],
        "docs": [
            "One signed number — a stick axis, a setpoint error, a desired "
            "speed — is really two things to the hardware: how hard, and "
            "which way. This hands you whichever one a particular pin needs, "
            "so one value can feed several pins by wiring this more than once.",
            "**Least that moves it** is the one people lose an evening to. A "
            "geared motor under load does not turn below some duty, so the "
            "bottom of the range buzzes and warms and the wheel stays still — "
            "a flow that fires every node, reports a duty and does nothing. "
            "Set it to the duty that actually breaks away and everything above "
            "zero starts from there; zero still stops.",
            "**Size** is the magnitude, scaled to 0–100 for a PWM duty. "
            "**Direction** is 1 forward and 0 back, for a direction pin. "
            "**Forward only** and **Back only** each give the size in one "
            "direction and 0 in the other, which is exactly what the two "
            "inputs of an H-bridge want.",
            "So: a driver with PWM and DIR takes Size and Direction. A "
            "BTS7960 or TB6612, with an input per side, takes Forward only "
            "and Back only. An L298N takes Size on its enable and Direction "
            "on one input with **Invert** on the other. A stepper takes "
            "Direction on DIR, and Size into a PWM output whose frequency is "
            "the step rate.",
        ],
        "fields": [
            _f("part", "Give me", "select", default="size",
               options=["size", "direction", "forward only", "back only"]),
            _f("full", "Full scale in", "number", default=1,
               help="The input value that means full. 1 for a stick axis, "
                    "100 if something upstream already works in percent."),
            _f("scale", "Full scale out", "number", default=100,
               showIf={"part": ["size", "forward only", "back only"]},
               help="100 suits a PWM duty."),
            _f("floor", "Least that moves it", "number", default=0, min=0,
               showIf={"part": ["size", "forward only", "back only"]},
               help="The duty below which the motor does not turn. A geared "
                    "motor under load often needs a quarter of full duty to "
                    "break away, and under that it buzzes and warms without "
                    "moving. Anything asked for at all comes out at least this "
                    "high; zero stays zero. Leave it at 0 for a servo, a lamp "
                    "or anything that does something useful at low duty."),
            _f("invert", "Invert", "select", default="no", options=["no", "yes"],
               help="Swaps which way is forward. On a direction pin this is "
                    "the second half of an H-bridge, and on a motor it is the "
                    "one wired backwards."),
            _f("decimals", "Decimal places", "number", default=2, min=0, max=6),
        ],
    },
    "gpio.out": {
        "label": "GPIO write", "group": "Actions", "kind": "action", "runs": "both",
        "series": "series-1", "glyph": "pin",
        "summary": "Drives a header pin high, low, toggled or pulsed.",
        "inputs": ["in"], "outputs": ["out"],
        "docs": [
            "On a board, a pin this node drove goes back **low** when the flow "
            "stops or is replaced, as a PWM output does, so the next flow never "
            "starts with a pin the last one left high. For a relay that "
            "switches on at low, that means on: wire it the other way.",
        ],
        "fields": [
            _f("gpio", "Pin", "pin"),
            _f("action", "Action", "select", default="high",
               options=["high", "low", "toggle", "pulse", "from-payload"]),
            _f("pulse_level", "Pulse to", "select", default="high",
               options=["high", "low"], showIf={"action": ["pulse"]},
               help="Pulse to high returns the pin low afterwards; pulse to low "
                    "returns it high. Use low for active-low hardware."),
            _f("pulse_ms", "Pulse (ms)", "number", default=200, min=1, max=60000,
               showIf={"action": ["pulse"]}),
        ],
    },
    "pwm.out": {
        "label": "PWM output", "group": "Actions", "kind": "action", "runs": "both",
        "series": "series-1", "glyph": "wave",
        "summary": "Drives a pin with a square wave at a duty cycle.",
        "inputs": ["in"], "outputs": ["out"],
        "fields": [
            _f("gpio", "Pin", "pin"),
            _f("action", "Action", "select", default="start",
               options=["start", "stop", "toggle"],
               help="The waveform latches: once started it keeps running until "
                    "a stop, so the node does not need re-firing to stay on."),
            _f("mode", "Mode", "select", options=["software", "hardware"], default="software",
               help="Software PWM works on any free pin. Hardware needs a pwm overlay. "
                    "This one is about this host only: a device assigns its own "
                    "channel and ignores both of these.",
               showIf={"action": ["start", "toggle"]}),
            _f("freq", "Frequency (Hz)", "number", default=1000, min=1, max=5000,
               showIf={"action": ["start", "toggle"]}),
            _f("units", "Duty is in", "select", options=["percent", "microseconds"],
               default="percent", showIf={"action": ["start", "toggle"]},
               help="Microseconds is how a servo is specified — 1500us of a 20ms "
                    "frame at 50Hz — and saves doing that arithmetic in the flow."),
            _f("duty", "Duty", "number", default=50, min=0, max=100,
               showIf={"action": ["start", "toggle"]},
               help="Accepts a variable, e.g. {{payload}}. Left empty, the "
                    "incoming payload is used."),
            _f("channel", "Hardware channel", "number", default=0, min=0, max=5,
               showIf={"mode": ["hardware"]}),
        ],
    },
    "i2c.write": {
        "label": "I2C write", "group": "Actions", "kind": "action", "runs": "both",
        "series": "series-2", "glyph": "chip",
        "summary": "Writes bytes to a device on an I2C bus.",
        "inputs": ["in"], "outputs": ["out"],
        "fields": [
            _f("bus", "Bus", "number", default=0, min=0, max=9),
            _f("address", "Address (hex)", "text", default="0x40"),
            _f("data", "Bytes (hex)", "text", default="0x00",
               help="Space or comma separated, e.g. 0x01 0xff. {{payload}} works."),
        ],
    },
    "i2c.read": {
        "label": "I2C read", "group": "Actions", "kind": "action", "runs": "both",
        "series": "series-2", "glyph": "chip",
        "summary": "Reads bytes, optionally from a register.",
        "inputs": ["in"], "outputs": ["out"],
        "fields": [
            _f("bus", "Bus", "number", default=0, min=0, max=9),
            _f("address", "Address (hex)", "text", default="0x40"),
            _f("register", "Register (hex, optional)", "text", default="",
               help="Set this for a repeated-start register read."),
            _f("length", "Bytes to read", "number", default=1, min=1, max=64),
            _f("format", "Payload as", "select",
               options=["hex", "int-be", "int-le", "bytes"], default="hex"),
        ],
    },
    "i2c.scan": {
        "label": "I2C scan", "group": "Actions", "kind": "action", "runs": "both",
        "series": "series-2", "glyph": "search",
        "summary": "Lists the addresses that answer on a bus.",
        "inputs": ["in"], "outputs": ["out"],
        "fields": [_f("bus", "Bus", "number", default=0, min=0, max=9)],
    },
    "spi.transfer": {
        "label": "SPI transfer", "group": "Actions", "kind": "action", "runs": "host",
        "series": "series-4", "glyph": "chip",
        "summary": "Full-duplex transfer; the payload is what came back.",
        "inputs": ["in"], "outputs": ["out"],
        "fields": [
            _f("device", "Device", "text", default="/dev/spidev1.0"),
            _f("data", "Bytes (hex)", "text", default="0x00"),
            _f("speed", "Speed (Hz)", "number", default=500000, min=1000, max=50000000),
            _f("spi_mode", "SPI mode", "select", options=["0", "1", "2", "3"], default="0"),
        ],
    },
    "mqtt.publish": {
        "label": "MQTT publish", "group": "Actions", "kind": "action", "runs": "host",
        "series": "series-3", "glyph": "globe",
        "summary": "Publishes the payload to a topic.",
        "inputs": ["in"], "outputs": ["out"],
        "fields": [
            _f("host", "Broker host", "text", default="localhost"),
            _f("port", "Port", "number", default=1883, min=1, max=65535),
            _f("topic", "Topic", "text", default="zero2w/out"),
            _f("payload", "Payload", "text", default="{{payload}}"),
            _f("retain", "Retain", "select", options=["no", "yes"], default="no"),
            _f("username", "Username", "text", default=""),
            _f("password", "Password", "text", default=""),
            _f("tls", "TLS", "select", options=["no", "yes"], default="no"),
        ],
    },
    "flow.call": {
        "label": "Call flow", "group": "Actions", "kind": "action", "runs": "host",
        "series": "series-2", "glyph": "branch",
        "summary": "Runs another flow's trigger, passing this message on.",
        "inputs": ["in"], "outputs": ["out"],
        "fields": [
            _f("flow", "Flow", "combo", default="", options_from="flows",
               placeholder="— choose a flow —",
               help="The flow to run, and the node in it to fire. Both lists "
                    "are what exists right now; either can be typed instead, "
                    "which is how a {{variable}} goes here."),
            _f("node", "Node", "combo", default="", options_from="nodes_in_flow",
               from_key="flow", placeholder="— choose a node —",
               empty="Choose a flow first."),
        ],
    },
    "http.request": {
        "label": "HTTP request", "group": "Actions", "kind": "action", "runs": "both",
        "series": "series-3", "glyph": "globe",
        "summary": "Calls an API and passes the response on.",
        "inputs": ["in"], "outputs": ["out"],
        "fields": [
            _f("method", "Method", "select", default="GET",
               options=["GET", "POST", "PUT", "PATCH", "DELETE"]),
            _f("url", "URL", "text", default="https://"),
            _f("headers", "Headers (JSON)", "textarea", default=""),
            _f("body", "Body", "textarea", default="",
               help="{{payload}} is replaced with the incoming payload."),
            _f("timeout", "Timeout (s)", "number", default=10, min=1, max=120),
        ],
    },
    "shell.run": {
        "label": "Shell command", "group": "Actions", "kind": "action", "runs": "host",
        "series": "series-4", "glyph": "terminal",
        "summary": "Runs one command and passes stdout on.",
        "inputs": ["in"], "outputs": ["out"],
        "fields": [
            _f("cmd", "Command", "textarea", default="echo {{payload}}"),
            _f("timeout", "Timeout (s)", "number", default=15, min=1, max=120),
        ],
    },
    "host.notify": {
        "label": "Tell the host", "group": "Actions", "kind": "action", "runs": "device",
        "series": "series-1", "glyph": "up",
        "summary": "Reports something to this board, which can start a flow here.",
        "inputs": ["in"], "outputs": ["out"],
        "docs": [
            "A device saying *this happened*. It travels on the report the "
            "agent already sends, so it needs no address, no URL and no "
            "console token — a field device never holds one.",
            "The host decides what it means: a **Device event** trigger there, "
            "set to this device and this kind, starts a flow when it arrives. "
            "Nothing happens if no flow is listening.",
        ],
        "fields": [
            _f("kind", "Kind", "text", default="event",
               help="A short name for what happened — motion, button, "
                    "low-battery. The host matches on it."),
            _f("payload", "Value", "text", default="{{payload}}"),
            _f("level", "Level", "select", default="ok",
               options=["ok", "warn", "serious", "critical", "idle"]),
        ],
    },
    "host.event": {
        "label": "Device event", "group": "Triggers", "kind": "trigger", "runs": "host",
        "series": "series-1", "glyph": "down",
        "summary": "Fires when a field device reports something.",
        "inputs": [], "outputs": ["out"],
        "docs": [
            "The other end of **Tell the host**. Choose a device and the kind "
            "it sends; this fires when that arrives, with the device's value "
            "as the payload.",
            "Leave the kind empty to catch everything that device reports, "
            "including the events it raises on its own.",
            "Choose a **group** instead of a device and it fires for any "
            "member; {{meta.device}} says which one it was.",
            "A picture from **Send to host** arrives as its kind — *picture* "
            "unless you changed it — carrying the picture, so a **Save to SD** "
            "after this keeps it on this board's card.",
        ],
        "fields": [
            _f("device", "Device", "combo", default="", options_from="devices",
               placeholder="— any device —",
               empty="No devices have enrolled yet."),
            _f("kind", "Kind", "text", default="",
               help="Match what the device's Tell the host node sends. "
                    "Empty matches anything."),
        ],
    },
    "device.command": {
        "label": "Device command", "group": "Actions", "kind": "action", "runs": "host",
        "series": "series-3", "glyph": "chip",
        "summary": "Tells one field device to do something.",
        "inputs": ["in"], "outputs": ["out"],
        "docs": [
            "The other direction from Device event: this board telling a "
            "device to do something, using the same commands the IOT screen's "
            "buttons send. The device picks it up on its next poll, within a "
            "couple of seconds.",
            "Choose a **group** and every device in it gets the command. "
            "Groups are named lists of devices, kept on the IoT screen's "
            "Devices tab.",
            "**Fire** runs a node in the flow the device is already running. "
            "**Set pin** drives one of its pins directly. The camera commands "
            "start and stop its sensor.",
        ],
        "fields": [
            _f("device", "Device", "combo", default="", options_from="devices",
               placeholder="— choose a device —",
               empty="No devices have enrolled yet."),
            _f("op", "Command", "select", default="fire",
               options=list(DEVICE_OPS)),
            _f("node", "Node to fire", "text", default="",
               showIf={"op": ["fire"]},
               help="A node id in the flow that device is running."),
            _f("gpio", "GPIO", "number", default=None, showIf={"op": ["set"]}),
            _f("value", "Value", "select", default="1", options=["1", "0"],
               showIf={"op": ["set"]}),
            _f("frame_size", "Frame size", "select", default="QQVGA",
               options=["QQVGA", "QVGA", "HVGA", "CIF", "VGA"],
               showIf={"op": ["camera-on"]}),
            _f("format", "Picture", "select", default="colour",
               options=["colour", "greyscale", "jpeg"], showIf={"op": ["camera-on"]}),
        ],
    },
    "pad.link": {
        "label": "Controller", "group": "Actions", "kind": "action", "runs": "both",
        "series": "series-4", "glyph": "switch",
        "summary": "Holds a Bluetooth controller open and reads what it sends.",
        "inputs": ["in"], "outputs": ["out"],
        "docs": [
            "A switch for the radio, and the only node that touches it. "
            "Anything truthy keeps the pad connected, anything falsy drops it. "
            "Unwired, no pad is ever looked for — an **Interval** straight in "
            "is the usual way to hold one open.",
            "Put it **upstream of the Controller axis and Controller button "
            "nodes, in the same chain**: they report what the pad last sent "
            "and open nothing themselves.",
            "**Name** picks which pad when more than one is connected; empty "
            "takes the first. On this board the pad is paired on the IoT "
            "screen's Bluetooth tab. A field device has no Bluetooth transport "
            "yet, so there it logs that once and the controller nodes report "
            "nothing.",
            "The message passes through unchanged. {{meta.pad}} is true while "
            "a controller is connected, so an If node after this can branch "
            "on whether there is one.",
        ],
        "fields": [
            _f("name", "Name contains", "text", default="",
               help="Empty takes the first controller that answers."),
        ],
    },
    "ble.link": {
        "label": "BLE device", "group": "Actions", "kind": "action", "runs": "both",
        "series": "series-4", "glyph": "switch",
        "summary": "Holds a connection to a BLE device while messages arrive.",
        "inputs": ["in"], "outputs": ["out"],
        "docs": [
            "A switch for one Bluetooth connection. Anything truthy connects, "
            "anything falsy disconnects. Unwired, nothing is connected: an "
            "**Interval** wired in holds it open and reconnects after a drop.",
            "Pair the device first on the IoT screen's Bluetooth tab. **BLE "
            "read**, **BLE write** and **BLE notify** need the connection this "
            "holds; they never open one themselves.",
            "The message passes through. {{meta.ble_connected}} says whether "
            "it is connected, so an If after this can branch on it.",
        ],
        "fields": [_f("device", "Device", "combo", default="", options_from="ble_devices",
               placeholder="an address or part of a name",
               empty="Nothing paired yet — pair it on the IoT screen's Bluetooth tab.",
               help="An address is safest: some devices take a different name "
                    "once connected.")],
    },
    "ble.read": {
        "label": "BLE read", "group": "Actions", "kind": "action", "runs": "both",
        "series": "series-4", "glyph": "down",
        "summary": "Reads a characteristic and passes its value on.",
        "inputs": ["in"], "outputs": ["out"],
        "docs": [
            "One read per message. The payload becomes the value, decoded as "
            "**Format** says; {{meta.hex}} keeps the raw bytes.",
            "It needs a **BLE device** node holding the connection. Not "
            "connected, or a characteristic the device does not have, stops "
            "the branch and says which.",
        ],
        "fields": [_f("device", "Device", "combo", default="", options_from="ble_devices",
               placeholder="an address or part of a name",
               empty="Nothing paired yet — pair it on the IoT screen's Bluetooth tab.",
               help="An address is safest: some devices take a different name "
                    "once connected."), _f("char", "Characteristic", "text", default="",
               placeholder="e.g. 2a19",
               help="Its UUID, as the Bluetooth tab lists it: 2a19, or a full "
                    "128-bit one. By UUID because the path BlueZ gives a "
                    "characteristic changes with every connection."), _f("format", "Format", "select", default="hex", options=list(blefmt.FORMATS),
               help="How the bytes read as a value. Multi-byte numbers are "
                    "little-endian, as BLE sends them.")],
    },
    "ble.write": {
        "label": "BLE write", "group": "Actions", "kind": "action", "runs": "both",
        "series": "series-4", "glyph": "up",
        "summary": "Writes a value to a characteristic.",
        "inputs": ["in"], "outputs": ["out"],
        "docs": [
            "**Value** is encoded as **Format** says and written. For *hex*, "
            "give pairs of digits, spaces allowed: `01 ff`. A value that does "
            "not fit the format stops the branch rather than writing something "
            "else.",
            "It needs a **BLE device** node holding the connection. The "
            "message passes on once the device has taken the write.",
        ],
        "fields": [_f("device", "Device", "combo", default="", options_from="ble_devices",
               placeholder="an address or part of a name",
               empty="Nothing paired yet — pair it on the IoT screen's Bluetooth tab.",
               help="An address is safest: some devices take a different name "
                    "once connected."), _f("char", "Characteristic", "text", default="",
               placeholder="e.g. 2a19",
               help="Its UUID, as the Bluetooth tab lists it: 2a19, or a full "
                    "128-bit one. By UUID because the path BlueZ gives a "
                    "characteristic changes with every connection."), _f("format", "Format", "select", default="hex", options=list(blefmt.FORMATS),
               help="How the bytes read as a value. Multi-byte numbers are "
                    "little-endian, as BLE sends them."),
            _f("value", "Value", "text", default="{{payload}}",
               help="Anything, including a {{variable}}."),
        ],
    },
    "ble.notify": {
        "label": "BLE notify", "group": "Triggers", "kind": "trigger", "runs": "both",
        "series": "series-1", "glyph": "wave",
        "summary": "Fires on every value a BLE device notifies.",
        "inputs": [], "outputs": ["out"],
        "docs": [
            "Subscribes while the device is connected and fires once per "
            "notification, with the value decoded as **Format** says. Values "
            "arrive as the device sends them, not on a poll.",
            "It does not connect anything: a **BLE device** node does. When the "
            "link drops this waits, and subscribes again when it is back.",
        ],
        "fields": [_f("device", "Device", "combo", default="", options_from="ble_devices",
               placeholder="an address or part of a name",
               empty="Nothing paired yet — pair it on the IoT screen's Bluetooth tab.",
               help="An address is safest: some devices take a different name "
                    "once connected."), _f("char", "Characteristic", "text", default="",
               placeholder="e.g. 2a19",
               help="Its UUID, as the Bluetooth tab lists it: 2a19, or a full "
                    "128-bit one. By UUID because the path BlueZ gives a "
                    "characteristic changes with every connection."), _f("format", "Format", "select", default="hex", options=list(blefmt.FORMATS),
               help="How the bytes read as a value. Multi-byte numbers are "
                    "little-endian, as BLE sends them.")],
    },
    "camera.feed": {
        "label": "Camera feed", "group": "Actions", "kind": "action", "runs": "device",
        "series": "series-4", "glyph": "eye",
        "summary": "Keeps the camera running and offers its picture to the console.",
        "inputs": ["in"], "outputs": ["out"],
        "docs": [
            "A switch for the sensor. It does nothing until a message reaches "
            "it: anything truthy starts the camera at the size and format set "
            "here, anything falsy stops it. Unwired, it never runs.",
            "A **Toggle** in front of it — one trigger set to *on*, another to "
            "*off* — is the usual way to run a camera only while something is "
            "happening. An **Interval** wired straight in keeps it on.",
            "Wire a **Camera to screen** node after it to put the picture on "
            "the Cameras screen and choose how often it is fetched. Without "
            "one the camera still runs and can still be reached; it is just "
            "not on the wall.",
            "Pictures reach the console **encrypted**: the board streams them "
            "over TLS to a certificate the console made for itself, and only "
            "while someone is looking. A console without `openssl` has no "
            "certificate, and then fetches them unencrypted instead.",
        ],
        "fields": [
            _f("frame_size", "Frame size", "select", default="QQVGA",
               options=["QQVGA", "QVGA", "HVGA", "CIF", "VGA"]),
            _f("format", "Picture", "select", default="colour",
               options=["colour", "greyscale", "jpeg"],
               help="jpeg is full colour, compressed on the camera, and many "
                    "times smaller than the other two, so it is the fast one. "
                    "It needs camera firmware that can make JPEG, which the "
                    "flasher installs."),
            _f("rotate", "Turn the picture", "select", default="0",
               options=["0", "90", "180", "270"],
               help="A camera module is soldered whichever way suits its "
                    "board, so a bare sensor with nothing but a protector on "
                    "it is very often upside down. 180 puts it right."),
            _f("mirror", "Mirror it", "select", default="no",
               options=["no", "yes"],
               help="Left and right swapped, the way a mirror does."),
        ],
    },
    "camera.publish": {
        "label": "Camera to screen", "group": "Actions", "kind": "action",
        "runs": "device", "series": "series-4", "glyph": "eye",
        "summary": "Puts this device's picture on the Cameras screen.",
        "inputs": ["in"], "outputs": ["out"],
        "docs": [
            "Wire this downstream of a **Camera feed** and the device appears "
            "as a tile on the Cameras screen and the dashboard. Without it, "
            "the camera still runs and can still be reached — it is just not "
            "on the wall.",
            "**How often** is how often the device sends a picture while the "
            "Cameras screen or the dashboard is open, and it sends none while "
            "nobody is looking. With **jpeg** on the Camera feed node, a "
            "640x480 picture is 7-17KB and several a second is realistic; "
            "*colour* and *greyscale* are uncompressed, and even 160x120 "
            "greyscale takes about a second each over this radio.",
            "Slower is cheaper: a camera nobody watches closely costs the "
            "radio and the board less at 5s.",
        ],
        "fields": [
            _f("label", "Tile caption", "text", default=""),
            _f("every_ms", "How often (ms)", "number", default=1000,
               min=CAMERA_MIN_MS, max=60000,
               help="Between %d and 60000. Below a second is only worth it "
                    "with jpeg on the Camera feed node." % CAMERA_MIN_MS),
        ],
    },
    "camera.capture": {
        "label": "Camera capture", "group": "Actions", "kind": "action", "runs": "device",
        "series": "series-4", "glyph": "eye",
        "summary": "Takes one picture when a message arrives.",
        "inputs": ["in"], "outputs": ["out"],
        "docs": [
            "One picture per message, which is how you take one on an edge, on "
            "a schedule, or when something else in the flow decides. **It is "
            "not needed for the live view** — Camera feed does that on its own.",
            "The picture travels on with the message, so a **Save to SD** or a "
            "**Send to host** after this has something to keep or send. The "
            "payload is its size in bytes (so is {{meta.frame}}), and "
            "{{meta.format}} says what it is, so a Log after this prints a "
            "number rather than the picture.",
            "With the camera off, it switches on at this size and format for "
            "the one picture and off again. With a **Camera feed** already "
            "running, the picture comes at the feed's size and format "
            "instead: one sensor, one setting at a time.",
        ],
        "fields": [
            _f("frame_size", "Frame size", "select", default="QQVGA",
               options=["QQVGA", "QVGA", "HVGA", "CIF", "VGA"]),
            _f("format", "Picture", "select", default="jpeg",
               options=["colour", "greyscale", "jpeg"],
               help="jpeg is full colour and a fraction of the size, which is "
                    "what makes a picture small enough to send or keep. It "
                    "needs the camera firmware the flasher installs."),
        ],
    },
    "picture.send": {
        "label": "Send to host", "group": "Actions", "kind": "action", "runs": "device",
        "series": "series-1", "glyph": "up",
        "summary": "Sends the picture in the message to this board.",
        "inputs": ["in"], "outputs": ["out"],
        "docs": [
            "Put this after a **Camera capture**. The picture goes to the "
            "console **encrypted**, over the same TLS stream as the live view "
            "— opened for the send if the camera has none, and closed again "
            "once nothing has gone for a while. The first can take a second or "
            "two while that stream is set up.",
            "Here it arrives as a **Device event** of this kind. A flow that "
            "starts from one can keep it with **Save to SD**.",
            "A picture must fit in one frame, 64 KB: a JPEG does at any size "
            "here, an uncompressed one only at the smallest. One waits to go at "
            "a time; a newer one replaces it.",
        ],
        "fields": [
            _f("kind", "Kind", "text", default="picture",
               help="What the Device event on the host matches on."),
        ],
    },
    "sd.save": {
        "label": "Save to SD", "group": "Actions", "kind": "action", "runs": "both",
        "series": "series-2", "glyph": "down",
        "summary": "Keeps the picture in the message on an SD card.",
        "inputs": ["in"], "outputs": ["out"],
        "docs": [
            "On a board it writes to the board's own **microSD card**; on this "
            "board, to its SD card under `~/captures`. Each picture is a file "
            "named for when it was taken, in the folder named here, and "
            "{{meta.file}} says where it went.",
            "A message with no picture is kept too: its payload becomes one "
            "line of `log.txt` in the same folder, so a reading can be logged "
            "the same way.",
            "**Keep** is how many pictures the folder holds before the oldest "
            "go, so a card does not fill. On this board a raw picture is saved "
            "as a PNG; on a board it stays raw, with its size in its name.",
            "A board's clock is set by the console when it first streams, so a "
            "board that has never reached the console names its files from "
            "2000-01-01.",
        ],
        "fields": [
            _f("folder", "Folder", "text", default="captures",
               help="Letters, digits, - and _."),
            _f("keep", "Keep", "number", default=200, min=0, max=100000,
               help="The newest this many pictures. 0 keeps them all."),
        ],
    },
    "log.write": {
        "label": "Log", "group": "Actions", "kind": "action", "runs": "both",
        "series": "series-2", "glyph": "list",
        "summary": "Writes a line to the flow log and the dashboard.",
        "inputs": ["in"], "outputs": ["out"],
        "fields": [
            _f("level", "Level", "select", default="ok",
               options=["ok", "warn", "serious", "critical", "idle"]),
            _f("message", "Message", "text", default="{{payload}}"),
        ],
    },
}

# Fold the output shapes in, so a node definition is still one thing to read
# and one thing to ship — the editor is handed REGISTRY and finds everything.
for _ntype, _spec in REGISTRY.items():
    _spec["emits"] = EMITS.get(_ntype, PASSES_THROUGH)
del _ntype, _spec

MAX_HOPS = 40  # a cycle in the graph must not spin forever

TOKEN_RE = re.compile(r"\{\{\s*([A-Za-z0-9_.]+)\s*\}\}")

# The variable library. The editor renders its dropdowns from this list and the
# resolver below answers them, so a variable cannot exist in one and not the
# other. `example` is what the editor shows as a sample value.
VARIABLES = [
    # -- the message ------------------------------------------------------
    {"name": "payload", "group": "Message", "example": "1",
     "desc": "Value passed in from the previous node."},
    {"name": "meta.edge", "group": "Message", "example": "rising",
     "desc": "Which edge fired a GPIO trigger."},
    {"name": "meta.gpio", "group": "Message", "example": "264",
     "desc": "GPIO number that fired the trigger."},
    {"name": "meta.topic", "group": "Message", "example": "zero2w/sensor",
     "desc": "MQTT topic the message arrived on."},
    {"name": "meta.status", "group": "Message", "example": "200",
     "desc": "HTTP status from the previous request node."},
    {"name": "meta.code", "group": "Message", "example": "0",
     "desc": "Exit code from the previous shell node."},
    {"name": "meta.metric", "group": "Message", "example": "cpu-thermal",
     "desc": "Metric that crossed its threshold."},
    {"name": "meta.threshold", "group": "Message", "example": "60",
     "desc": "Threshold value that was crossed."},

    # -- this flow --------------------------------------------------------
    {"name": "flow.name", "group": "Context", "example": "Overheat watch",
     "desc": "Name of the running flow."},
    {"name": "flow.id", "group": "Context", "example": "f_overheat",
     "desc": "Id of the running flow."},
    {"name": "node.id", "group": "Context", "example": "a1",
     "desc": "Id of the node being executed."},

    # -- the device this flow is deployed to ------------------------------
    # Named rather than numbered: a flow written for a board and deployed to one of
    # several should say "this device". Here they come from its last report.
    {"name": "device.id", "group": "Device", "example": "dev_ab12cd34",
     "desc": "The device this flow is deployed to."},
    {"name": "device.name", "group": "Device", "example": "ESP32-Cam",
     "desc": "What that device is called."},
    {"name": "device.board", "group": "Device", "example": "esp32cam",
     "desc": "Which board profile it is."},
    {"name": "device.ip", "group": "Device", "example": "10.42.0.152",
     "desc": "Its address on the IoT network."},
    {"name": "device.rssi", "group": "Device", "example": -58,
     "desc": "Signal strength where it is, in dBm. Closer to zero is better."},
    {"name": "device.uptime", "group": "Device", "example": 4821,
     "desc": "Seconds since that device last started."},
    {"name": "device.free", "group": "Device", "example": 41216,
     "desc": "Free memory on it, in bytes."},
    {"name": "device.online", "group": "Device", "example": 1,
     "desc": "1 while it has reported recently, 0 when it has gone quiet."},
    {"name": "device.seen", "group": "Device", "example": 2,
     "desc": "Seconds since it last reported."},

    # -- the controller ---------------------------------------------------
    # Axes only. `pad.x` would be the X button to whoever wrote it and the X
    # axis to whoever read it, and those are a switch and a stick. A button is a
    # discrete event that wants an edge on the canvas, so buttons stay a node.
    {"name": "pad.lx", "group": "Controller", "example": 0.0,
     "desc": "Left stick, left to right. -1 to 1."},
    {"name": "pad.ly", "group": "Controller", "example": 0.0,
     "desc": "Left stick, back to forward. -1 to 1."},
    {"name": "pad.rx", "group": "Controller", "example": 0.0,
     "desc": "Right stick, left to right. -1 to 1."},
    {"name": "pad.ry", "group": "Controller", "example": 0.0,
     "desc": "Right stick, back to forward. -1 to 1."},
    {"name": "pad.lt", "group": "Controller", "example": 0.0,
     "desc": "Left trigger, 0 to 1."},
    {"name": "pad.rt", "group": "Controller", "example": 0.42,
     "desc": "Right trigger, 0 to 1."},
    {"name": "pad.connected", "group": "Controller", "example": 1,
     "desc": "1 while a controller is connected, 0 when none is."},

    # -- board ------------------------------------------------------------
    {"name": "host", "group": "Board", "example": "auto-blox",
     "desc": "Hostname of this board."},
    {"name": "time", "group": "Board", "example": "14:02:31", "desc": "Local time, HH:MM:SS."},
    {"name": "date", "group": "Board", "example": "2026-09-19", "desc": "Local date, YYYY-MM-DD."},
    {"name": "iso", "group": "Board", "example": "2026-09-19T14:02:31",
     "desc": "Local date and time, ISO 8601."},
    {"name": "epoch", "group": "Board", "example": "1789786670",
     "desc": "Unix timestamp, whole seconds."},
    {"name": "uptime", "group": "Board", "example": "1d 04h", "desc": "How long the board has been up."},

    # -- live readings ----------------------------------------------------
    {"name": "cpu.temp", "group": "Readings", "example": "48.8", "desc": "cpu-thermal, degrees C."},
    {"name": "gpu.temp", "group": "Readings", "example": "48.7", "desc": "gpu-thermal, degrees C."},
    {"name": "ve.temp", "group": "Readings", "example": "47.9", "desc": "ve-thermal, degrees C."},
    {"name": "ddr.temp", "group": "Readings", "example": "48.7", "desc": "ddr-thermal, degrees C."},
    {"name": "cpu.percent", "group": "Readings", "example": "13.5", "desc": "CPU busy across all cores."},
    {"name": "cpu.mhz", "group": "Readings", "example": "1416", "desc": "Current CPU clock."},
    {"name": "load1", "group": "Readings", "example": "0.42", "desc": "1 minute load average."},
    {"name": "load5", "group": "Readings", "example": "0.31", "desc": "5 minute load average."},
    {"name": "mem.percent", "group": "Readings", "example": "23.7", "desc": "Memory used, percent."},
    {"name": "mem.used", "group": "Readings", "example": "928.5 MiB", "desc": "Memory used, human readable."},
    {"name": "mem.total", "group": "Readings", "example": "3.8 GiB", "desc": "Total memory."},
    {"name": "root.percent", "group": "Readings", "example": "2.2", "desc": "Root filesystem used, percent."},
    {"name": "root.free", "group": "Readings", "example": "227.1 GiB", "desc": "Root filesystem free space."},
    {"name": "net.ip", "group": "Readings", "example": "192.168.1.20",
     "desc": "Address of the default-route interface."},
    {"name": "net.iface", "group": "Readings", "example": "wlan0",
     "desc": "Name of the default-route interface."},
    {"name": "net.rx", "group": "Readings", "example": "6.0", "desc": "Receive rate, KiB/s."},
    {"name": "net.tx", "group": "Readings", "example": "248.2", "desc": "Transmit rate, KiB/s."},
]
# What each variable does once it is inside a node. Kept beside the list rather
# than inline so the table above stays readable.
_VAR_DETAIL = {
    "payload": ("any",
        "Whatever the previous node emitted. A GPIO write set to from-payload "
        "drives the pin high for any non-zero value; in a Log or MQTT node it is "
        "inserted as text; in a number field it becomes the number."),
    "meta.edge": ("text",
        "rising or falling. Only set when a GPIO edge trigger started the flow, "
        "so it is empty in a flow started by a timer or webhook."),
    "meta.gpio": ("number",
        "The sunxi GPIO number that fired, not the physical pin number. Useful "
        "for echoing which input caused an action."),
    "meta.topic": ("text",
        "The exact topic a wildcard subscription matched, so one node can route "
        "on which topic fired."),
    "meta.status": ("number",
        "HTTP status from the previous request node. Feed it to an If node to "
        "branch on success or failure."),
    "meta.code": ("number",
        "Exit code of the previous Shell node; 0 means success. Branch on it "
        "with an If node."),
    "meta.metric": ("text",
        "Which metric crossed its limit, when a Metric threshold trigger fired. "
        "Pairs with meta.threshold for a readable alert."),
    "meta.threshold": ("number",
        "The configured limit that was crossed, while payload holds the reading "
        "that crossed it."),
    "flow.name": ("text", "Name of the running flow, for logs that say where they came from."),
    "node.id": ("text", "Id of the node writing the message, for tracing a busy flow."),
    "host": ("text", "Hostname of this board, for messages sent off the device."),
    "uptime": ("text", "Human readable, e.g. 1d 04h. Text, so not usable in a number field."),
    "epoch": ("number", "Whole seconds since 1970, for stamping API payloads."),
}

for _v in VARIABLES:
    _d = _VAR_DETAIL.get(_v["name"])
    if _d:
        _v["type"], _v["desc"] = _d
    else:
        # Most Readings are numeric, but the pre-formatted ones are not.
        _text_readings = ("mem.used", "mem.total", "root.free", "net.ip", "net.iface")
        _numeric_device = ("device.rssi", "device.uptime", "device.free",
                           "device.online", "device.seen")
        _v.setdefault("type",
                      "text" if _v["name"] in _text_readings
                      else ("number" if _v["group"] == "Readings"
                            or _v["name"] in _numeric_device else "text"))

VARIABLE_NAMES = set(v["name"] for v in VARIABLES)


def variables_with_values(snapshot=None, tags=None, devices=None):
    """The library plus each variable's value right now, so the editor shows
    what a token will actually produce instead of only a sample."""
    ctx = {"snapshot": snapshot or {}, "tags": tags, "devices": devices or [],
           "flow": {"id": "(this flow)", "name": "(this flow)"},
           "node": {"id": "(this node)", "type": "(this node)"}}
    msg = {"payload": "(from previous node)", "meta": {}}
    out = []
    for v in VARIABLES:
        row = dict(v)
        try:
            val = resolve_variable(v["name"], msg, ctx)
        except Exception:
            val = None
        row["value"] = None if val is None else _fmt(val)
        out.append(row)
    for tag in (tags.list() if tags else []):
        out.append({
            "name": "tag." + tag["name"],
            "group": "Tags",
            "type": tag["type"],
            "desc": tag.get("desc") or "A tag you defined.",
            "example": _fmt(tag.get("initial")),
            "value": _fmt(tag.get("value")),
            "unit": tag.get("unit") or "",
        })
    for device in devices or []:
        key = device_key(device) or device.get("id")
        for field in DEV_FIELDS:
            name = "dev.%s.%s" % (key, field)
            out.append({
                "name": name, "group": "Other devices",
                "type": "text" if field in ("ip", "flow") else "number",
                "desc": "%s's %s, from its last report. A pin is "
                        "dev.%s.pin.<gpio>." % (device.get("name") or device.get("id"),
                                                field, key),
                "example": "", "value": _fmt(resolve_variable(name, msg, ctx)),
            })
    return out


def _as_png(pic, name):
    """A raw frame saved here becomes a PNG anyone can open; a JPEG stays one."""
    if pic.get("format") == "jpeg":
        return pic["data"], name
    png = pixels.frame_to_png(pic["data"], pic["width"], pic["height"], pic["format"])
    return png, name.rsplit("-", 2)[0] + ".png"


def _fmt(v):
    if v is None:
        return ""
    if isinstance(v, float):
        if v == int(v):
            return str(int(v))
        # two decimals, trailing zeros dropped: 0.34 stays 0.34, 52.30 -> 52.3
        return ("%.2f" % v).rstrip("0").rstrip(".")
    if isinstance(v, (dict, list)):
        return json.dumps(v)
    return str(v)


FALSEY = ("", "0", "0.0", "off", "false", "no", "none", "null")


def truthy(value):
    """One rule for what counts as on, shared by every node that asks."""
    if value is None or value is False:
        return False
    if value is True:
        return True
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() not in FALSEY


def _clip(v, limit=400):
    """A value small enough to carry on the bus and show in a panel."""
    if v is None or isinstance(v, (bool, int, float)):
        return v
    text = v if isinstance(v, str) else _fmt(v)
    if len(text) <= limit:
        return text
    return text[:limit] + "… (%d more)" % (len(text) - limit)


# How long a device may be quiet before it counts as gone. Here because flows.py
# is imported by fleet.py and not the other way round, so both sides read one
# number — {{device.online}} and the IOT screen must not disagree.
DEVICE_FRESH = 90


def _device_value(device, field):
    """One `device.*` variable out of what that device last reported."""
    if not device:
        return None
    if field in ("id", "name", "board", "ip"):
        return device.get(field)
    state = device.get("state") or {}
    if field == "rssi":
        return device.get("rssi", state.get("rssi"))
    if field == "uptime":
        return device.get("uptime", state.get("uptime"))
    if field == "free":
        return state.get("free") or state.get("mem")
    seen = device.get("last_seen")
    if field == "seen":
        return None if not seen else round(time.time() - seen, 1)
    if field == "online":
        return 1 if (seen and time.time() - seen < DEVICE_FRESH) else 0
    return None


def device_key(device):
    """How {{dev.<key>.x}} names a device: its name, lowercased, with anything
    that is not a letter or digit as `_`. `ESP32 Motor` is `esp32_motor`."""
    name = (device.get("name") or "").lower()
    return re.sub(r"[^a-z0-9]+", "_", name).strip("_")


# What {{dev.<key>.x}} offers, beyond the {{device.x}} fields.
DEV_FIELDS = ("online", "seen", "rssi", "free", "uptime", "ip", "flow")


def _other_device_value(devices, rest):
    """`{{dev.<id or key>.<field>}}` — another device, from its last report."""
    key, _, field = rest.partition(".")
    for device in devices or []:
        if key in (device.get("id"), device_key(device)):
            if field == "flow":
                return device.get("flow")
            if field.startswith("pin."):
                pin = ((device.get("state") or {}).get("pins") or {}).get(field[4:])
                return pin.get("value") if isinstance(pin, dict) else None
            return _device_value(device, field)
    return None


def _pad_value(state, key):
    """One `{{pad.x}}`, or None when no controller has ever reported.

    None rather than 0 on purpose: a resting stick and an absent pad are not
    the same claim, and `render` leaves a known name that resolves to None as
    an empty string rather than inventing a centre.
    """
    if not state:
        return None
    if key == "connected":
        return 1 if state.get("connected") else 0
    if key not in padmod.AXES.values():
        return None
    return state.get(key)


def resolve_variable(name, msg, ctx):
    """One variable name to its value, or None when it is not known."""
    msg = msg or {}
    # Tags are shared memory and readable from any field. Checked first
    # because the table is the authority on its own names.
    if name.startswith("tag."):
        table = (ctx or {}).get("tags")
        return table.get(name[4:]) if table else None
    if name.startswith("pad."):
        return _pad_value((ctx or {}).get("pad"), name[4:])
    if name.startswith("device."):
        return _device_value((ctx or {}).get("device"), name[7:])
    if name.startswith("dev."):
        devices = (ctx or {}).get("devices")
        return _other_device_value(devices() if callable(devices) else devices,
                                   name[4:])
    meta = msg.get("meta") or {}
    if name == "payload":
        return msg.get("payload")
    if name.startswith("meta."):
        return meta.get(name[5:])
    if name.startswith("flow."):
        return (ctx.get("flow") or {}).get(name[5:])
    if name.startswith("node."):
        return (ctx.get("node") or {}).get(name[5:])

    now = time.localtime()
    if name == "time":
        return time.strftime("%H:%M:%S", now)
    if name == "date":
        return time.strftime("%Y-%m-%d", now)
    if name == "iso":
        return time.strftime("%Y-%m-%dT%H:%M:%S", now)
    if name == "epoch":
        return int(time.time())

    snap = ctx.get("snapshot") or {}
    if not snap:
        return None
    if name == "host":
        return snap.get("host")
    if name == "uptime":
        return (snap.get("uptime") or {}).get("human")
    zones = {z.get("zone"): z.get("temp") for z in snap.get("thermal", [])}
    if name.endswith(".temp"):
        return zones.get(name.split(".")[0] + "-thermal")
    cpu = snap.get("cpu") or {}
    if name == "cpu.percent":
        return cpu.get("total")
    if name == "cpu.mhz":
        return cpu.get("mhz")
    load = snap.get("load") or {}
    if name == "load1":
        return load.get("one")
    if name == "load5":
        return load.get("five")
    mem = snap.get("memory") or {}
    if name == "mem.percent":
        return mem.get("pct")
    if name == "mem.used":
        return mem.get("used_h")
    if name == "mem.total":
        return mem.get("total_h")
    for fs in snap.get("filesystems", []):
        if fs.get("mount") == "/":
            if name == "root.percent":
                return fs.get("pct")
            if name == "root.free":
                return fs.get("free_h")
    for i in snap.get("network", []):
        if i.get("is_default"):
            if name == "net.ip":
                return (i.get("address") or "").split("/")[0]
            if name == "net.iface":
                return i.get("name")
            if name == "net.rx":
                return i.get("rx_kib")
            if name == "net.tx":
                return i.get("tx_kib")
    return None


def render(text, msg, ctx=None):
    """Substitute every {{variable}}."""
    if not isinstance(text, str) or "{{" not in text:
        return text
    ctx = ctx or {}

    def sub(m):
        name = m.group(1)
        val = resolve_variable(name, msg, ctx)
        if val is None and name not in VARIABLE_NAMES and not name.startswith("meta."):
            return m.group(0)
        return _fmt(val)
    return TOKEN_RE.sub(sub, text)


# A node that sends on more than one output returns (MANY, [(port, msg), ...]).
MANY = object()


def _outs(port, msg):
    return msg if port is MANY else [(port, msg)]


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


class RevConflict(Exception):
    """A save arrived against an older revision than the one on disk."""

    def __init__(self, current):
        super().__init__("stale revision")
        self.current = current


class FlowStore:
    """Flows on disk, as one JSON document."""

    def __init__(self, path=FLOWS_FILE):
        self.path = path
        self.lock = threading.Lock()
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)

    def _read(self):
        """The document on disk. Never raises; a corrupt file reads as empty."""
        if not os.path.exists(self.path):
            return {"flows": [], "rev": 0}
        try:
            with open(self.path) as fh:
                d = json.load(fh)
            if not isinstance(d, dict) or not isinstance(d.get("flows"), list):
                return {"flows": [], "rev": 0}
        except (ValueError, OSError):
            return {"flows": [], "rev": 0}
        try:
            d["rev"] = int(d.get("rev") or 0)
        except (TypeError, ValueError):
            d["rev"] = 0
        return d

    def load(self):
        with self.lock:
            return self._read()

    def save(self, doc, expect_rev=None):
        with self.lock:
            current = self._read()
            if expect_rev is not None and int(expect_rev) != current["rev"]:
                raise RevConflict(current)
            doc = dict(doc)
            doc["rev"] = current["rev"] + 1
            tmp = self.path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(doc, fh, indent=2)
            os.replace(tmp, self.path)   # atomic: never a half-written flow file
            os.chmod(self.path, 0o600)
        return doc


class FlowEngine:
    """Runs every enabled flow: owns trigger threads and executes graphs."""

    def __init__(self, store, driver, inventory, bus, metrics_source=None):
        self.store, self.driver, self.inv = store, driver, inventory
        self.bus, self.metrics_source = bus, metrics_source
        # Late-bound by the server once both exist, the way fleet.self_url is.
        # Only a node that commands a device needs it.
        self.fleet = None
        # The tag table, same arrangement. Without one, tag nodes say so and
        # {{tag.x}} stays verbatim like any other unknown name.
        self.tags = None
        self.tag_triggers = []      # (flow, node) armed on a tag change
        # Everything waiting to happen later, on one thread.
        self.clock = _Scheduler(self.fire)
        self.timers = {}            # (flow, node) -> what a timer is holding
        self.lock = threading.RLock()
        self.doc = {"flows": []}
        self.watchers = []          # EdgeWatcher / timer threads
        self.webhooks = {}          # path -> (flow_id, node_id)
        self.throttle_at = {}       # node key -> last pass time
        self.metric_armed = {}      # node key -> re-arm timestamp
        self.toggle_state = {}      # gpio -> last value we wrote
        self.toggle_nodes = {}      # (flow, node) -> flip-flop state
        self.edge_last = {}         # (flow, node) -> last truthiness seen
        self.counts = {}            # (flow, node) -> running count
        self.hyst_state = {}        # (flow, node) -> True while switched on
        self.windows = {}           # (flow, node) -> recent samples
        self.latches = {}           # (flow, node) -> True while held on
        self.steps = {}             # (flow, node) -> generation while active
        self.ble_names = {}         # a device field's name -> the address it matched
        self.step_gen = 0
        self.ramps = {}             # (flow, node) -> [value, last monotonic]
        self.dogs = {}              # (flow, node) -> True while it thinks fed
        self.last_output = {}       # (flow, node) -> what it last produced
        # The controller, shared by every flow on this host: one pad, one
        # reader. `pad.link` starts it on a message, never by existing.
        self.pad = padmod.blank()
        self.pad_reader = None
        self.runs = []              # recent executions, newest last
        self.jobs = queue.Queue(maxsize=200)
        self._stop = threading.Event()
        self.softpwm = buses.SoftPWM()
        self.hwpwm = {}            # (chip, channel) -> HardwarePWM
        self.mqtt = mqttmod.MQTTPool(on_state=self._mqtt_state)
        for _ in range(3):          # small pool: this is a 4-core board
            threading.Thread(target=self._worker, daemon=True, name="flow-worker").start()

    # -- lifecycle ---------------------------------------------------------
    def start(self):
        if not self.clock.is_alive():
            self.clock.start()
        self.reload()

    def reload(self):
        with self.lock:
            self._teardown()
            self.doc = self.store.load()
            for flow in self.doc.get("flows", []):
                if runs_here(flow) and flow.get("enabled", True):
                    self._arm(flow)
        return self.doc

    def _teardown(self):
        for w in self.watchers:
            try:
                w.stop() if hasattr(w, "stop") else None
            except Exception:
                pass
        self.watchers, self.webhooks = [], {}
        self.tag_triggers = []
        # A message waiting on a node that may no longer exist is not worth
        # delivering, and a timer's held state belongs to the old graph.
        self.clock.clear()
        self.timers = {}
        self.steps = {}
        # The pad reader is not in `watchers`: it is started from a message
        # rather than from _arm, so it would race this list being rebuilt.
        self.stop_pad()

    def _mqtt_state(self, key, state, detail):
        self._emit("mqtt", "%s:%s" % (key[0], key[1]),
                   "ok" if state == "connected" else "warn",
                   "broker %s (%s)" % (state, detail))

    def shutdown(self):
        self._stop.set()
        self.clock.stop()
        self._teardown()
        self.softpwm.stop_all()
        for h in self.hwpwm.values():
            h.stop()
        self.mqtt.stop_all()

    # -- arming triggers ---------------------------------------------------
    def _arm(self, flow):
        from . import gpio as gpio_mod
        for node in flow.get("nodes", []):
            t, cfg = node.get("type"), node.get("config") or {}
            key = (flow["id"], node["id"])
            if t == "gpio.in":
                pin = cfg.get("gpio")
                if pin is None:
                    continue
                w = gpio_mod.EdgeWatcher(
                    self.inv, int(pin), cfg.get("edges", "both"),
                    lambda g, edge, k=key: self.fire(k, {"payload": 1 if edge == "rising" else 0,
                                                         "meta": {"gpio": g, "edge": edge}}),
                    debounce_ms=int(_num(cfg.get("debounce"), 50)))
                w.start()
                self.watchers.append(w)
            elif t == "timer.interval":
                every = max(0.05, _num(cfg.get("every"), 1000) / 1000.0)
                w = _Interval(every, lambda k=key: self.fire(k, {"payload": 1, "meta": {"tick": True}}))
                w.start()
                self.watchers.append(w)
            elif t == "http.webhook":
                path = (cfg.get("path") or "").strip("/")
                if path:
                    self.webhooks[path] = key
            elif t == "mqtt.subscribe":
                host = (cfg.get("host") or "").strip()
                topic = (cfg.get("topic") or "").strip()
                if not host or not topic:
                    continue

                def deliver(tp, pl, k=key, want=topic):
                    if _topic_matches(want, tp):
                        self.fire(k, {"payload": pl, "meta": {"topic": tp}})

                client = self.mqtt.get(host, int(_num(cfg.get("port"), 1883)),
                                       username=cfg.get("username") or None,
                                       password=cfg.get("password") or None,
                                       tls=(cfg.get("tls") == "yes"),
                                       on_message=deliver)
                client.subscribe(topic)
            elif t == "metric.threshold":
                w = _Interval(2.0, lambda k=key, c=cfg: self._check_metric(k, c))
                w.start()
                self.watchers.append(w)
            elif t == "tag.change":
                # Noted rather than subscribed: one subscription on the table
                # fans out to these, so re-arming a flow cannot leak callbacks.
                self.tag_triggers.append((key, cfg))
            elif t == "ble.notify":
                mac = self._ble_mac(cfg)
                uuid = (cfg.get("char") or "").strip()
                if not mac or not uuid:
                    self._emit(key[0], key[1], "warn",
                               "needs a device and a characteristic")
                    continue
                w = gattmod.Subscription(
                    mac, uuid,
                    lambda data, k=key, c=cfg, m=mac: self._ble_notified(k, c, m, data),
                    on_state=lambda on, k=key: self._emit(
                        k[0], k[1], "ok" if on else "idle",
                        "subscribed" if on else "waiting for the device"))
                w.start()
                self.watchers.append(w)

    def _check_metric(self, key, cfg):
        if not self.metrics_source:
            return
        snap = self.metrics_source()
        if not snap:
            return
        val = self._metric_value(snap, cfg.get("metric"))
        if val is None:
            return
        target = _num(cfg.get("value"), 0)
        hit = val > target if cfg.get("op", "above") == "above" else val < target
        now = time.monotonic()
        if not hit:
            self.metric_armed.pop(key, None)
            return
        rearm = _num(cfg.get("rearm"), 30)
        last = self.metric_armed.get(key)
        if last is not None and now - last < rearm:
            return
        self.metric_armed[key] = now
        self.fire(key, {"payload": val, "meta": {"metric": cfg.get("metric"), "threshold": target}})

    @staticmethod
    def _metric_value(snap, metric):
        if metric in ("cpu-thermal", "gpu-thermal", "ve-thermal", "ddr-thermal"):
            for z in snap.get("thermal", []):
                if z["zone"] == metric:
                    return z.get("temp")
            return None
        if metric == "cpu-percent":
            return (snap.get("cpu") or {}).get("total")
        if metric == "load1":
            return (snap.get("load") or {}).get("one")
        if metric == "memory-percent":
            return (snap.get("memory") or {}).get("pct")
        if metric == "root-percent":
            for f in snap.get("filesystems", []):
                if f["mount"] == "/":
                    return f.get("pct")
        return None

    # -- firing ------------------------------------------------------------
    def fire(self, key, msg):
        try:
            self.jobs.put_nowait((key, msg))
        except queue.Full:
            pass

    def fire_webhook(self, path, payload):
        key = self.webhooks.get(path.strip("/"))
        if not key:
            return False
        self.fire(key, {"payload": payload, "meta": {"webhook": path}})
        return True

    def on_tag_changed(self, name, value, previous):
        """A tag moved: start any flow waiting on it."""
        fired = 0
        for key, cfg in list(self.tag_triggers):
            want = (cfg.get("tag") or "").strip()
            if want and want != name:
                continue
            if not tagwatch.tag_wants(cfg.get("when") or "changes", value,
                                    previous, truthy):
                continue
            self.fire(key, {"payload": value,
                            "meta": {"tag": name, "previous": previous}})
            fired += 1
        return fired

    def fire_device_event(self, device_id, kind, payload=None, message=None,
                          picture=None):
        """A device reported something; start any flow waiting on it. A
        picture from Send to host rides beside the payload, as it did there."""
        fired = 0
        with self.lock:
            flows_now = list(self.doc.get("flows", []))
        for flow in flows_now:
            if flow.get("enabled") is False or not runs_here(flow):
                continue
            for node in flow.get("nodes", []):
                if node.get("type") != "host.event":
                    continue
                cfg = node.get("config") or {}
                want_dev = (cfg.get("device") or "").strip()
                want_kind = (cfg.get("kind") or "").strip()
                if want_dev.startswith("group:") and self.fleet:
                    if device_id not in self.fleet.targets(want_dev):
                        continue
                elif want_dev and want_dev != device_id:
                    continue
                if want_kind and want_kind != kind:
                    continue
                msg = {"payload": payload,
                       "meta": {"device": device_id, "kind": kind,
                                "message": message}}
                if picture:
                    msg["picture"] = picture
                    msg["meta"].update(format=picture.get("format"),
                                       width=picture.get("width"),
                                       height=picture.get("height"))
                self.fire((flow["id"], node["id"]), msg)
                fired += 1
        return fired

    def _worker(self):
        while not self._stop.is_set():
            try:
                key, msg = self.jobs.get(timeout=1)
            except queue.Empty:
                continue
            try:
                self._run_from(key, msg)
            except Exception as exc:
                self._emit(key[0], key[1], "critical", "flow error: %s" % exc)

    def _flow(self, flow_id):
        for f in self.doc.get("flows", []):
            if f["id"] == flow_id:
                return f
        return None

    def _run_from(self, key, msg):
        flow_id, node_id = key
        flow = self._flow(flow_id)
        if not flow:
            return
        # A flow written for a board runs from that board's flash. Walking it here
        # would execute the device's graph against this host's pins, and the nodes
        # that only exist on a device would pass quietly through doing nothing.
        if not runs_here(flow):
            self._emit(flow_id, node_id, "warn",
                       "this flow runs on %s, not here" % flow.get("board"))
            return
        nodes = {n["id"]: n for n in flow.get("nodes", [])}
        edges = flow.get("edges", [])
        start = nodes.get(node_id)
        if not start:
            return
        if "_step_timeout" in msg:
            # A Step's Leave after, delivered by the scheduler to the step
            # itself: it leaves as if done had arrived.
            work = [(node_id, p, om, 0) for p, om in
                    _outs(*self._execute(flow, start, msg, "timeout"))
                    if om is not None]
        else:
            self._emit(flow_id, node_id, "ok", "%s fired" % REGISTRY.get(
                start["type"], {}).get("label", start["type"]))
            self._record_output(flow_id, node_id, msg)
            # Breadth-first walk with a hop budget, so a cycle cannot spin.
            work = [(node_id, "out", msg, 0)]
        while work:
            nid, port, m, hops = work.pop(0)
            if hops > MAX_HOPS:
                self._emit(flow_id, nid, "warn", "hop limit reached; branch stopped")
                continue
            for e in edges:
                if e.get("from") != nid or (e.get("fromPort") or "out") != port:
                    continue
                target = nodes.get(e.get("to"))
                if not target:
                    continue
                # Which input it arrived on. Only a node with more than one
                # cares; everything else is handed its single port's name.
                out_port, out_msg = self._execute(
                    flow, target, m, e.get("toPort"))
                for p, om in _outs(out_port, out_msg):
                    self._record_output(flow_id, target["id"], om, p)
                    if om is not None:
                        work.append((target["id"], p, om, hops + 1))

    # -- node execution ----------------------------------------------------
    def _ctx(self, flow, node):
        """Everything a {{variable}} can be resolved against, for this run."""
        snap = {}
        if self.metrics_source:
            try:
                snap = self.metrics_source() or {}
            except Exception:
                snap = {}
        return {"snapshot": snap,
                "tags": self.tags,
                "device": self._bound_device(flow),
                "devices": self._fleet_devices,
                "pad": self.pad,
                "flow": {"id": flow.get("id"), "name": flow.get("name")},
                "node": {"id": node.get("id"), "type": node.get("type")}}

    def _fleet_devices(self):
        """Every device's record, for {{dev.x}}; read only when one is used."""
        if not self.fleet:
            return []
        try:
            return self.fleet.devices.load()["devices"]
        except Exception:
            return []

    def _bound_device(self, flow):
        """What the device this flow is deployed to last said about itself."""
        want = (flow or {}).get("device")
        if not want or not self.fleet:
            return None
        try:
            for d in self.fleet.devices.load()["devices"]:
                if d.get("id") == want:
                    return d
        except Exception:
            return None
        return None

    def _tnum(self, value, msg, ctx, default):
        """A numeric field that may hold a template, e.g. {{payload}} ms."""
        return _num(render(value, msg, ctx) if isinstance(value, str) else value, default)

    def _execute(self, flow, node, msg, to_port=None):
        t = node.get("type")
        cfg = node.get("config") or {}
        fid, nid = flow["id"], node["id"]
        ctx = self._ctx(flow, node)
        try:
            if t == "logic.delay":
                # Handed to the scheduler rather than slept through: sleeping
                # here stops the engine, not just this branch.
                wait = min(600.0, self._tnum(cfg.get("ms"), msg, ctx, 500) / 1000.0)
                self.clock.at(wait, (fid, nid), msg, tag=(fid, nid))
                return "out", None
            if t == "logic.timer":
                return "out", self._timer(fid, nid, cfg, msg, ctx)
            if t == "logic.throttle":
                gap = _num(cfg.get("ms"), 1000) / 1000.0
                now = time.monotonic()
                last = self.throttle_at.get((fid, nid))
                if last is not None and now - last < gap:
                    return "out", None
                self.throttle_at[(fid, nid)] = now
                return "out", msg
            if t == "logic.toggle":
                key = (fid, nid)
                if key not in self.toggle_nodes:
                    # "start: on" means it sits on before anything arrives.
                    self.toggle_nodes[key] = cfg.get("start") == "on"
                action = toggle_action(cfg, to_port)
                if action == "on":
                    self.toggle_nodes[key] = True
                elif action == "off":
                    self.toggle_nodes[key] = False
                else:
                    self.toggle_nodes[key] = not self.toggle_nodes[key]
                state_on = self.toggle_nodes[key]
                val = cfg.get("on_value", "1") if state_on else cfg.get("off_value", "0")
                self._emit(fid, nid, "ok", "toggled %s" % ("on" if state_on else "off"))
                return "out", {"payload": render(val, msg, ctx), "meta": msg.get("meta", {})}
            if t == "logic.set":
                return "out", {"payload": render(cfg.get("value", ""), msg, ctx), "meta": msg.get("meta", {})}
            if t == "tag.set":
                return "out", self._tag_set(fid, nid, cfg, msg, ctx)
            if t == "tag.read":
                return "out", self._tag_read(fid, nid, cfg, msg)
            if t == "pad.link":
                return "out", self._pad_link(fid, nid, cfg, msg)
            if t == "pad.axis":
                return "out", self._pad_axis(cfg, msg)
            if t == "pad.button":
                return "out", self._pad_button(cfg, msg)
            if t == "logic.edge":
                return "out", self._edge(fid, nid, cfg, msg)
            if t == "logic.count":
                return "out", self._count(fid, nid, cfg, msg, to_port)
            if t == "logic.hysteresis":
                return "out", self._hysteresis(fid, nid, cfg, msg)
            if t == "logic.step":
                return self._step(fid, nid, cfg, msg, to_port)
            if t == "ble.link":
                return "out", self._ble_link(fid, nid, cfg, msg)
            if t == "ble.read":
                return "out", self._ble_read(fid, nid, cfg, msg)
            if t == "ble.write":
                return "out", self._ble_write(fid, nid, cfg, msg, ctx)
            if t == "logic.latch":
                return "out", self._latch(fid, nid, cfg, msg, ctx, to_port)
            if t == "safety.watchdog":
                return "out", self._watchdog(fid, nid, cfg, msg, ctx)
            if t == "math.expr":
                return "out", self._expr(fid, nid, cfg, msg, ctx)
            if t == "math.scale":
                return "out", self._scale(fid, nid, cfg, msg)
            if t == "math.smooth":
                return "out", self._smooth(fid, nid, cfg, msg, to_port)
            if t == "math.deadband":
                return "out", self._deadband(cfg, msg)
            if t == "math.split":
                return "out", self._split(cfg, msg)
            if t == "math.ramp":
                return "out", self._ramp(fid, nid, cfg, msg, to_port)
            if t == "logic.if":
                return self._if(cfg, msg), msg
            if t == "gpio.out":
                return "out", self._gpio_out(fid, nid, cfg, msg, ctx)
            if t == "http.request":
                return "out", self._http(fid, nid, cfg, msg, ctx)
            if t == "shell.run":
                return "out", self._shell(fid, nid, cfg, msg, ctx)
            if t == "pwm.out":
                return "out", self._pwm(fid, nid, cfg, msg, ctx)
            if t == "i2c.write":
                return "out", self._i2c_write(fid, nid, cfg, msg, ctx)
            if t == "i2c.read":
                return "out", self._i2c_read(fid, nid, cfg, msg, ctx)
            if t == "i2c.scan":
                return "out", self._i2c_scan(fid, nid, cfg, msg)
            if t == "spi.transfer":
                return "out", self._spi(fid, nid, cfg, msg, ctx)
            if t == "mqtt.publish":
                return "out", self._mqtt_publish(fid, nid, cfg, msg, ctx)
            if t == "flow.call":
                return "out", self._call_flow(fid, nid, cfg, msg)
            if t == "device.command":
                return "out", self._device_command(fid, nid, cfg, msg, ctx)
            if t == "log.write":
                self._emit(fid, nid, cfg.get("level", "ok"), str(render(cfg.get("message", ""), msg, ctx)))
                return "out", msg
            if t == "sd.save":
                path = picmod.save(CAPTURES, cfg, msg, convert=_as_png)
                out = dict(msg)
                out["meta"] = dict(msg.get("meta") or {}, file=path)
                return "out", out
        except Exception as exc:
            self._emit(fid, nid, "critical", "%s failed: %s" % (t, exc))
            return "out", None
        return "out", msg

    @staticmethod
    def _if(cfg, msg):
        op = cfg.get("op", "==")
        left = msg.get("payload")
        if op == "truthy":
            return "true" if left else "false"
        right = cfg.get("value", "")
        try:
            a, b = float(left), float(right)
        except (TypeError, ValueError):
            # A magnitude question about something that is not a number has no
            # true answer, and answering it alphabetically is worse than useless:
            # `"off" >= "0.5"` is True, which armed a motor flow gated on a text
            # tag. Ordering fails closed; == and != still compare as words.
            if op in (">", ">=", "<", "<="):
                return "false"
            a, b = str(left), str(right)
        res = {"==": a == b, "!=": a != b, ">": a > b, ">=": a >= b,
               "<": a < b, "<=": a <= b}.get(op, False)
        return "true" if res else "false"

    def _gpio_out(self, fid, nid, cfg, msg, ctx=None):
        pin = cfg.get("gpio")
        if pin is None:
            self._emit(fid, nid, "warn", "no pin selected")
            return None
        pin = int(pin)
        action = cfg.get("action", "high")
        if action == "high":
            value = 1
        elif action == "low":
            value = 0
        elif action == "toggle":
            value = 0 if self.toggle_state.get(pin, 0) else 1
        elif action == "from-payload":
            value = 1 if str(msg.get("payload")) not in ("0", "", "None", "False", "false") else 0
        else:  # pulse — to high by default, to low for active-low hardware
            value = 0 if cfg.get("pulse_level") == "low" else 1
        ok, reason = self.driver.write(pin, value)
        if not ok:
            self._emit(fid, nid, "critical", "pin %s: %s" % (pin, reason))
            return None
        self.toggle_state[pin] = value
        if action == "pulse":
            width = min(60.0, self._tnum(cfg.get("pulse_ms"), msg, ctx, 200) / 1000.0)
            rest = 1 - value
            time.sleep(width)
            self.driver.write(pin, rest)
            self.toggle_state[pin] = rest
            self._emit(fid, nid, "ok", "pin %s pulsed %s for %dms" %
                       (pin, "high" if value else "low", int(width * 1000)))
            return {"payload": value, "meta": msg.get("meta", {})}
        self._emit(fid, nid, "ok", "pin %s -> %s" % (pin, value))
        return {"payload": value, "meta": msg.get("meta", {})}

    def _pwm(self, fid, nid, cfg, msg, ctx=None):
        pin = cfg.get("gpio")
        if pin is None:
            self._emit(fid, nid, "warn", "no pin selected")
            return None
        pin = int(pin)
        freq = max(1.0, self._tnum(cfg.get("freq"), msg, ctx, 1000))
        # An empty field means "whatever arrived", which is what the device
        # has always done and what the field's help promises.
        raw = cfg.get("duty")
        if raw in (None, ""):
            raw = msg.get("payload")
        duty = self._tnum(raw, msg, ctx, 0)
        if cfg.get("units") == "microseconds":
            duty = 100.0 * duty / (1000000.0 / freq)
        duty = max(0.0, min(100.0, duty))

        # "enable" was the old on/off field; keep those flows working.
        action = cfg.get("action") or ("start" if cfg.get("enable", "on") == "on" else "stop")
        if action == "toggle":
            action = "stop" if self._pwm_running(pin) else "start"
        on = action == "start"

        if cfg.get("mode") == "hardware":
            chan = int(_num(cfg.get("channel"), 0))
            key = ("pwmchip0", chan)
            h = self.hwpwm.get(key) or buses.HardwarePWM("pwmchip0", chan)
            self.hwpwm[key] = h
            try:
                period_ns = int(1e9 / freq)
                h.apply(period_ns, int(period_ns * duty / 100.0), on)
            except OSError as exc:
                self._emit(fid, nid, "critical",
                           "hardware pwm unavailable (%s) — needs a pwm overlay "
                           "and write access to /sys/class/pwm" % exc)
                return None
            self._emit(fid, nid, "ok", "hw pwm ch%d %.0fHz %.0f%%" % (chan, freq, duty))
            return {"payload": duty, "meta": msg.get("meta", {})}

        ok, reason = self.inv.drivable(pin)
        if not ok:
            self._emit(fid, nid, "critical", "pin %s: %s" % (pin, reason))
            return None
        p = self.inv.by_gpio[pin]
        if not on:
            self.softpwm.stop(p["chip"], p["line"])
            self._emit(fid, nid, "ok", "pwm off on pin %s" % pin)
            return {"payload": 0, "meta": msg.get("meta", {})}
        ok, err = self.softpwm.start(p["chip"], p["line"], freq, duty)
        if not ok:
            self._emit(fid, nid, "critical", "pwm on pin %s: %s" % (pin, err))
            return None
        self._emit(fid, nid, "ok", "pwm pin %s %.0fHz %.0f%%" % (pin, freq, duty))
        return {"payload": duty, "meta": msg.get("meta", {})}

    def _pwm_running(self, gpio):
        pin = self.inv.by_gpio.get(gpio)
        if not pin:
            return False
        for a in self.softpwm.active():
            if a["chip"] == pin["chip"] and a["line"] == pin["line"]:
                return True
        for (chip, chan), h in self.hwpwm.items():
            try:
                with open(os.path.join(h.dir, "enable")) as fh:
                    if fh.read().strip() == "1":
                        return True
            except OSError:
                pass
        return False

    @staticmethod
    def _hexbytes(text):
        out = []
        for tok in str(text).replace(",", " ").split():
            out.append(int(tok, 16) if tok.lower().startswith("0x") else int(tok, 0))
        return bytes(b & 0xFF for b in out)

    def _i2c_write(self, fid, nid, cfg, msg, ctx=None):
        try:
            bus = buses.I2CBus(int(_num(cfg.get("bus"), 0)))
            addr = int(str(cfg.get("address", "0x00")), 16)
            data = self._hexbytes(render(cfg.get("data", ""), msg, ctx))
        except (ValueError, TypeError) as exc:
            self._emit(fid, nid, "warn", "bad i2c parameters: %s" % exc)
            return None
        try:
            bus.write(addr, data)
        except OSError as exc:
            self._emit(fid, nid, "critical", "i2c write 0x%02x: %s" % (addr, exc))
            return None
        self._emit(fid, nid, "ok", "i2c 0x%02x <- %s" % (addr, data.hex(" ")))
        return {"payload": data.hex(), "meta": msg.get("meta", {})}

    def _i2c_read(self, fid, nid, cfg, msg, ctx=None):
        try:
            bus = buses.I2CBus(int(_num(cfg.get("bus"), 0)))
            addr = int(str(cfg.get("address", "0x00")), 16)
            length = int(_num(cfg.get("length"), 1))
            reg = str(cfg.get("register") or "").strip()
        except (ValueError, TypeError) as exc:
            self._emit(fid, nid, "warn", "bad i2c parameters: %s" % exc)
            return None
        try:
            raw = bus.write_read(addr, self._hexbytes(reg), length) if reg \
                else bus.read(addr, length)
        except OSError as exc:
            self._emit(fid, nid, "critical", "i2c read 0x%02x: %s" % (addr, exc))
            return None
        fmt = cfg.get("format", "hex")
        if fmt == "int-be":
            payload = int.from_bytes(raw, "big")
        elif fmt == "int-le":
            payload = int.from_bytes(raw, "little")
        elif fmt == "bytes":
            payload = list(raw)
        else:
            payload = raw.hex(" ")
        self._emit(fid, nid, "ok", "i2c 0x%02x -> %s" % (addr, raw.hex(" ")))
        return {"payload": payload, "meta": msg.get("meta", {})}

    def _i2c_scan(self, fid, nid, cfg, msg):
        try:
            found = buses.I2CBus(int(_num(cfg.get("bus"), 0))).scan()
        except OSError as exc:
            self._emit(fid, nid, "critical", "i2c scan: %s" % exc)
            return None
        pretty = [hex(a) for a in found]
        self._emit(fid, nid, "ok" if found else "warn",
                   "i2c scan found %s" % (", ".join(pretty) if pretty else "nothing"))
        return {"payload": pretty, "meta": msg.get("meta", {})}

    def _spi(self, fid, nid, cfg, msg, ctx=None):
        dev = (cfg.get("device") or "").strip()
        if not os.path.exists(dev):
            self._emit(fid, nid, "critical",
                       "%s does not exist — add a spidev overlay to "
                       "/boot/armbianEnv.txt and reboot" % dev)
            return None
        try:
            data = self._hexbytes(render(cfg.get("data", ""), msg, ctx))
            rx = buses.SPIDev(dev).transfer(data, int(_num(cfg.get("speed"), 500000)),
                                            int(_num(cfg.get("spi_mode"), 0)))
        except (OSError, ValueError) as exc:
            self._emit(fid, nid, "critical", "spi transfer: %s" % exc)
            return None
        self._emit(fid, nid, "ok", "spi %s -> %s" % (data.hex(" "), rx.hex(" ")))
        return {"payload": rx.hex(" "), "meta": msg.get("meta", {})}

    def _mqtt_publish(self, fid, nid, cfg, msg, ctx=None):
        host = (cfg.get("host") or "").strip()
        topic = (cfg.get("topic") or "").strip()
        if not host or not topic:
            self._emit(fid, nid, "warn", "broker host and topic are required")
            return None
        client = self.mqtt.get(host, int(_num(cfg.get("port"), 1883)),
                               username=cfg.get("username") or None,
                               password=cfg.get("password") or None,
                               tls=(cfg.get("tls") == "yes"))
        payload = render(cfg.get("payload", "{{payload}}"), msg, ctx)
        if not client.connected.wait(timeout=5):
            self._emit(fid, nid, "critical",
                       "broker %s not connected%s" % (host, (": " + client.last_error) if client.last_error else ""))
            return None
        try:
            client.publish(topic, payload, retain=(cfg.get("retain") == "yes"))
        except Exception as exc:
            self._emit(fid, nid, "critical", "publish failed: %s" % exc)
            return None
        self._emit(fid, nid, "ok", "mqtt %s <- %s" % (topic, str(payload)[:60]))
        return {"payload": payload, "meta": dict(msg.get("meta", {}), topic=topic)}

    def _call_flow(self, fid, nid, cfg, msg):
        target_flow = (cfg.get("flow") or "").strip()
        target_node = (cfg.get("node") or "").strip()
        if not target_flow or not target_node:
            self._emit(fid, nid, "warn", "target flow and node are required")
            return None
        meta = dict(msg.get("meta", {}))
        depth = int(meta.get("_depth", 0)) + 1
        if depth > 8:
            self._emit(fid, nid, "warn", "sub-flow depth limit reached; not calling")
            return None
        meta["_depth"] = depth
        self.fire((target_flow, target_node), {"payload": msg.get("payload"), "meta": meta})
        self._emit(fid, nid, "ok", "called %s/%s" % (target_flow, target_node))
        return {"payload": msg.get("payload"), "meta": meta}

    # -- tags ---------------------------------------------------------------
    def _tag_set(self, fid, nid, cfg, msg, ctx=None):
        name = (cfg.get("tag") or "").strip()
        if not self.tags:
            self._emit(fid, nid, "warn", "there is no tag table here")
            return None
        if not name:
            self._emit(fid, nid, "warn", "no tag chosen")
            return None
        raw = cfg.get("value", "{{payload}}")
        # A field holding exactly one variable keeps that value's type rather than
        # becoming the string of it: {{payload}} into a number tag stays a number.
        whole = TOKEN_RE.fullmatch(str(raw).strip()) if isinstance(raw, str) else None
        value = resolve_variable(whole.group(1), msg, ctx) if whole \
            else render(raw, msg, ctx)
        try:
            written, _changed = self.tags.set(name, value)
        except Exception as exc:
            self._emit(fid, nid, "critical", "could not write %s: %s" % (name, exc))
            return None
        return {"payload": msg.get("payload"),
                "meta": dict(msg.get("meta", {}), tag=name, written=written)}

    def _tag_read(self, fid, nid, cfg, msg):
        name = (cfg.get("tag") or "").strip()
        if not self.tags or not name:
            self._emit(fid, nid, "warn", "no tag to read")
            return None
        if not self.tags.has(name):
            self._emit(fid, nid, "warn", "there is no tag called %r" % name)
            return None
        return {"payload": self.tags.get(name),
                "meta": dict(msg.get("meta", {}), tag=name)}

    def _timer(self, fid, nid, cfg, msg, ctx=None):
        """On-delay, off-delay and pulse, as one node with three behaviours."""
        key = (fid, nid)
        mode = cfg.get("mode") or "wait until true for"
        wait = min(3600.0, max(0.001, self._tnum(cfg.get("ms"), msg, ctx, 2000) / 1000.0))
        now = truthy(msg.get("payload"))
        held = dict(msg.get("meta", {}))
        held["held_ms"] = int(wait * 1000)
        later = {"payload": msg.get("payload"), "meta": held}

        if mode == "one and then nothing for":
            until = self.timers.get(key)
            if until is not None and time.monotonic() < until:
                return None                     # inside the quiet window
            self.timers[key] = time.monotonic() + wait
            out = dict(msg.get("meta", {}))
            out["held_ms"] = 0
            return {"payload": msg.get("payload"), "meta": out}

        if mode == "keep it true for":
            # True goes straight through; it is the falling edge that waits.
            if now:
                self.clock.cancel(key)
                if self.timers.get(key):
                    return None                 # already on, nothing new to say
                self.timers[key] = True
                out = dict(msg.get("meta", {}))
                out["held_ms"] = 0
                return {"payload": msg.get("payload"), "meta": out}
            if not self.timers.get(key):
                return None                     # already off
            self.clock.cancel(key)
            self.timers[key] = False
            self.clock.at(wait, key, later, tag=key)
            return None

        # wait until true for: the message only survives if nothing takes it
        # back before the time is up.
        if now:
            if self.clock.pending(key):
                return None                     # already counting down
            self.clock.at(wait, key, later, tag=key)
            return None
        dropped = self.clock.cancel(key)
        if dropped:
            self._emit(fid, nid, "idle", "went false before the wait was up")
        return None

    def _expr(self, fid, nid, cfg, msg, ctx=None):
        """Fill the variables in, then work the sum out."""
        filled = render(cfg.get("expr") or "", msg, ctx)
        try:
            value = exprmod.evaluate(filled)
        except exprmod.ExprError as exc:
            # A formula that cannot be worked out stops the branch: passing a
            # wrong number on is worse than passing nothing.
            self._emit(fid, nid, "warn", "%s \u2014 %s" % (exc, filled))
            return None
        places = cfg.get("decimals")
        value = exprmod.tidy(value, None if places in (None, "") else places)
        return {"payload": value,
                "meta": dict(msg.get("meta", {}), formula=filled)}

    # -- the standard blocks -----------------------------------------------
    def _edge(self, fid, nid, cfg, msg):
        """A level becomes an event: pass on only the message that changed it."""
        key = (fid, nid)
        now = truthy(msg.get("payload"))
        was = self.edge_last.get(key)
        self.edge_last[key] = now
        if was is None or was == now:
            return None                    # first sight, or no change
        want = cfg.get("edges") or "rising"
        edge = "rising" if now else "falling"
        if want != "any" and want != edge:
            return None
        return {"payload": msg.get("payload"),
                "meta": dict(msg.get("meta", {}), edge=edge)}

    def _count(self, fid, nid, cfg, msg, to_port=None):
        key = (fid, nid)
        start = _num(cfg.get("start"), 0)
        if (to_port or "in") == "reset":
            self.counts[key] = start
            self._emit(fid, nid, "idle", "counter reset to %s" % _fmt(start))
            return None                    # a reset is not a count
        count = self.counts.get(key, start) + _num(cfg.get("step"), 1)
        target = _num(cfg.get("target"), 0)
        hit = bool(target) and count >= target if target > 0 else \
            (bool(target) and count <= target)
        if hit and (cfg.get("auto_reset") or "reset") == "reset":
            self.counts[key] = start
        else:
            self.counts[key] = count
        if target and not hit:
            return None                    # still counting, nothing to say
        return {"payload": count,
                "meta": dict(msg.get("meta", {}), count=count, hit=bool(hit))}

    def _hysteresis(self, fid, nid, cfg, msg):
        key = (fid, nid)
        value = _num(msg.get("payload"), 0)
        on_above = _num(cfg.get("on_above"), 1)
        off_below = _num(cfg.get("off_below"), 0)
        was = self.hyst_state.get(key)
        state = bool(was)
        if value >= on_above:
            state = True
        elif value <= off_below:
            state = False
        # Between the two thresholds nothing changes, which is the point.
        changed = was is None or state != was
        self.hyst_state[key] = state
        if not changed and (cfg.get("emit") or "on change") == "on change":
            return None
        out = cfg.get("on_value", "1") if state else cfg.get("off_value", "0")
        return {"payload": out,
                "meta": dict(msg.get("meta", {}),
                             state="on" if state else "off", input=value)}

    def _scale(self, fid, nid, cfg, msg):
        raw = _num(msg.get("payload"), 0)
        in_min, in_max = _num(cfg.get("in_min"), 0), _num(cfg.get("in_max"), 1)
        out_min, out_max = _num(cfg.get("out_min"), 0), _num(cfg.get("out_max"), 1)
        if in_max == in_min:
            self._emit(fid, nid, "warn", "the input range is zero wide")
            return None
        frac = (raw - in_min) / (in_max - in_min)
        clamped = False
        if (cfg.get("clamp") or "yes") == "yes" and not 0.0 <= frac <= 1.0:
            frac = max(0.0, min(1.0, frac))
            clamped = True
        value = out_min + frac * (out_max - out_min)
        value = round(value, int(_num(cfg.get("decimals"), 1)))
        return {"payload": value,
                "meta": dict(msg.get("meta", {}), raw=raw, clamped=clamped)}

    def _smooth(self, fid, nid, cfg, msg, to_port=None):
        key = (fid, nid)
        if (to_port or "in") == "reset":
            self.windows.pop(key, None)
            self._emit(fid, nid, "idle", "smoothing forgot its samples")
            return None
        raw = _num(msg.get("payload"), 0)
        mode = cfg.get("mode") or "running average"
        places = int(_num(cfg.get("decimals"), 2))
        if mode == "weighted":
            # One number of memory, which is why a device can afford it.
            weight = min(1.0, max(0.01, _num(cfg.get("weight"), 0.3)))
            held = self.windows.get(key)
            value = raw if held is None else held[0] + weight * (raw - held[0])
            self.windows[key] = [value]
            samples = 1
        else:
            size = int(min(64, max(1, _num(cfg.get("window"), 5))))
            window = self.windows.get(key) or []
            window.append(raw)
            del window[:-size]
            self.windows[key] = window
            samples = len(window)
            if mode == "lowest":
                value = min(window)
            elif mode == "highest":
                value = max(window)
            else:
                value = sum(window) / float(samples)
        return {"payload": round(value, places),
                "meta": dict(msg.get("meta", {}), raw=raw, samples=samples)}

    # -- the controller ----------------------------------------------------
    def stop_pad(self):
        if self.pad_reader is not None:
            self.pad_reader.stop()
            self.pad_reader = None
        self.pad.update(padmod.blank())

    def _pad_link(self, fid, nid, cfg, msg):
        """Hold a pad open, or drop it, and say which in the message."""
        if truthy(msg.get("payload")):
            reader = self.pad_reader
            if reader is not None and not reader.is_alive():
                if reader.error:
                    self._emit(fid, nid, "warn", reader.error)
                self.pad_reader, reader = None, None
                self.pad.update(padmod.blank())
            if reader is None:
                self.pad.update(padmod.blank())
                self.pad_reader = padmod_host.Reader(self.pad, cfg.get("name"))
                self.pad_reader.start()
        else:
            self.stop_pad()
        return {"payload": msg.get("payload"),
                "meta": dict(msg.get("meta", {}),
                             pad=bool(self.pad.get("connected")),
                             pad_name=self.pad.get("name") or "")}

    def _pad_age_ms(self):
        at = self.pad.get("at") or 0
        return (time.monotonic() - at) * 1000.0 if at else 1e9

    def _pad_axis(self, cfg, msg):
        age = self._pad_age_ms()
        if padmod.stale(self.pad, cfg, age):
            return None             # the deadman: say nothing, starve the dog
        value = padmod.axis_value(self.pad, cfg)
        if value is None:
            return None
        return {"payload": value,
                "meta": dict(msg.get("meta", {}),
                             pad_axis=cfg.get("axis") or "left stick x",
                             pad_age=int(age))}

    def _pad_button(self, cfg, msg):
        gone = padmod.stale(self.pad, cfg, self._pad_age_ms())
        value = padmod.button_value(self.pad, cfg, gone)
        if value is None:
            return None
        return {"payload": value,
                "meta": dict(msg.get("meta", {}),
                             pad_button=cfg.get("button") or "a",
                             pad_edge=cfg.get("emit") or "held")}

    # -- the drive blocks --------------------------------------------------
    # Pure functions of (config, message) where they can be: the device runs the
    # same arithmetic, and a number that differs between the two sides is a bug
    # nobody would find by reading either one.

    def _deadband(self, cfg, msg):
        raw = _num(msg.get("payload"), 0)
        value, inside = deadband(raw, cfg)
        if inside:
            value = cfg.get("resting", "0")
        else:
            value = round(value, int(_num(cfg.get("decimals"), 3)))
        return {"payload": value,
                "meta": dict(msg.get("meta", {}), raw=raw, inside=inside)}

    def _split(self, cfg, msg):
        raw = _num(msg.get("payload"), 0)
        value, forward = split_drive(raw, cfg)
        if not isinstance(value, int):
            value = round(value, int(_num(cfg.get("decimals"), 2)))
        return {"payload": value,
                "meta": dict(msg.get("meta", {}), raw=raw, forward=forward)}

    def _ramp(self, fid, nid, cfg, msg, to_port=None):
        key = (fid, nid)
        start = _num(cfg.get("start"), 0)
        if (to_port or "in") == "reset":
            # Forget where it had got to, and say nothing. A reset restores state;
            # it must not issue a command. Emitting the start value here drove the
            # wheels on disarm — reset the ramp, the ramp emits 0, the mix adds the
            # steering. Anything wanting a message on reset can wire one onward.
            self.ramps.pop(key, None)
            self._emit(fid, nid, "idle", "ramp back to %g" % start)
            return None
        target = _num(msg.get("payload"), 0)
        now = time.monotonic()
        held = self.ramps.get(key)
        if held is None:
            held = [start, now]
        # Seconds since the last message is the step this one is allowed to
        # take. It advances on traffic, not on a clock — see the node's docs.
        value, arrived = ramp_toward(held[0], target, now - held[1], cfg)
        self.ramps[key] = [value, now]
        return {"payload": round(value, int(_num(cfg.get("decimals"), 3))),
                "meta": dict(msg.get("meta", {}), target=target, arrived=arrived)}

    def _step(self, fid, nid, cfg, msg, to_port=None):
        """A sequence stage: active from go until done, reset or Leave after."""
        key = (fid, nid)
        port = to_port or "in"
        name = cfg.get("name") or nid
        held = self.steps.get(key)
        meta = dict(msg.get("meta") or {}, step=name)
        if port == "reset":
            if held is not None:
                self.clock.cancel(("step", key))
                del self.steps[key]
                self._step_state(fid, nid, name, False)
            return "out", None
        if port in ("done", "timeout"):
            if held is None:
                return "out", None
            # A Leave after from an earlier visit must not end this one.
            if port == "timeout" and msg.get("_step_timeout") != held:
                return "out", None
            self.clock.cancel(("step", key))
            del self.steps[key]
            self._step_state(fid, nid, name, False)
            meta["timed_out"] = port == "timeout"
            out = {"payload": msg.get("payload"), "meta": meta}
            return MANY, [("left", out), ("next", out)]
        if held is not None:
            return "out", None              # already here: go again is not a re-entry
        self.step_gen += 1
        self.steps[key] = self.step_gen
        wait = _num(cfg.get("leave_after"), 0)
        if wait > 0:
            self.clock.at(wait / 1000.0, key,
                          {"payload": msg.get("payload"), "meta": msg.get("meta") or {},
                           "_step_timeout": self.step_gen}, tag=("step", key))
        self._step_state(fid, nid, name, True)
        return "entered", {"payload": msg.get("payload"), "meta": meta}

    # -- BLE ------------------------------------------------------------------
    def _ble_mac(self, cfg):
        """The device field as an address: one already, or part of a name.

        A name is remembered as the address it first matched, because BlueZ
        swaps the advertised name for the one GATT reports once connected.
        """
        want = (cfg.get("device") or "").strip()
        if not want:
            return None
        if btmod.is_address(want):
            return want.upper()
        low = want.lower()
        if low in self.ble_names:
            return self.ble_names[low]
        for device in gattmod.devices():
            if low in (device["name"] or "").lower():
                self.ble_names[low] = device["mac"]
                return device["mac"]
        return None

    def _ble_char(self, fid, nid, cfg):
        """(mac, object path) of the configured characteristic, or say why not."""
        mac = self._ble_mac(cfg)
        if not mac:
            self._emit(fid, nid, "warn", "no device matching %r"
                       % (cfg.get("device") or ""))
            return None, None
        uuid = (cfg.get("char") or "").strip()
        known = gattmod.tree()
        path = gattmod.find_char(mac, uuid, known=known) if uuid else None
        if path:
            return mac, path
        if not gattmod.is_connected(mac, known):
            self._emit(fid, nid, "warn", "%s is not connected — a BLE device "
                       "node holds the connection" % mac)
        else:
            self._emit(fid, nid, "warn", "%s has no characteristic %r"
                       % (mac, uuid))
        return mac, None

    def _ble_link(self, fid, nid, cfg, msg):
        mac = self._ble_mac(cfg)
        if not mac:
            self._emit(fid, nid, "warn", "no device matching %r" % (cfg.get("device") or ""))
            return None
        connected = gattmod.is_connected(mac)
        if truthy(msg.get("payload")):
            if not connected:
                got = gattmod.connect(mac, timeout=15)
                connected = got["ok"]
                self._emit(fid, nid, "ok" if connected else "warn", got["detail"])
        elif connected:
            gattmod.disconnect(mac)
            connected = False
            self._emit(fid, nid, "idle", "disconnected %s" % mac)
        return {"payload": msg.get("payload"),
                "meta": dict(msg.get("meta", {}), ble_connected=connected,
                             ble_device=mac)}

    def _ble_read(self, fid, nid, cfg, msg):
        _mac, path = self._ble_char(fid, nid, cfg)
        if not path:
            return None
        raw = gattmod.read(path)
        if raw is None:
            self._emit(fid, nid, "warn", "the read was refused")
            return None
        try:
            value = blefmt.decode(cfg.get("format") or "hex", raw)
        except blefmt.FormatError as exc:
            self._emit(fid, nid, "warn", str(exc))
            return None
        return {"payload": value,
                "meta": dict(msg.get("meta", {}), uuid=cfg.get("char"), hex=raw.hex())}

    def _ble_write(self, fid, nid, cfg, msg, ctx=None):
        try:
            data = blefmt.encode(cfg.get("format") or "hex",
                                 render(cfg.get("value", "{{payload}}"), msg, ctx))
        except blefmt.FormatError as exc:
            self._emit(fid, nid, "warn", str(exc))
            return None
        _mac, path = self._ble_char(fid, nid, cfg)
        if not path:
            return None
        if not gattmod.write(path, data):
            self._emit(fid, nid, "warn", "the device refused the write")
            return None
        return {"payload": msg.get("payload"),
                "meta": dict(msg.get("meta", {}), uuid=cfg.get("char"),
                             written=data.hex())}

    def _ble_notified(self, key, cfg, mac, data):
        try:
            value = blefmt.decode(cfg.get("format") or "hex", data)
        except blefmt.FormatError as exc:
            self._emit(key[0], key[1], "warn", str(exc))
            return
        self.fire(key, {"payload": value,
                        "meta": {"uuid": cfg.get("char"), "hex": bytes(data).hex(),
                                 "ble_device": mac}})

    def _step_state(self, fid, nid, name, active):
        """Tell the canvas, which highlights a step while it is active."""
        row = {"time": time.strftime("%H:%M:%S"), "flow": fid, "node": nid,
               "level": "ok" if active else "idle", "kind": "step",
               "message": ("step %s active" if active else "left step %s") % name,
               "payload": {"active": active}}
        with self.lock:
            self.runs.append(row)
            if len(self.runs) > 200:
                self.runs = self.runs[-200:]
        if self.bus:
            self.bus.publish("flow", row)

    def active_steps(self):
        """Every step active on this host, keyed "flow/node"."""
        with self.lock:
            return ["%s/%s" % k for k in self.steps]

    def _latch(self, fid, nid, cfg, msg, ctx=None, to_port=None):
        key = (fid, nid)
        if key not in self.latches:
            self.latches[key] = cfg.get("start") == "on"
        was = self.latches[key]
        # Anything that is not the reset anchor sets it. An edge saved without a
        # toPort arrives as "in", and that has to latch rather than land nowhere.
        port = "reset" if (to_port or "in") == "reset" else "in"
        state = port != "reset"
        self.latches[key] = state
        if state == was and (cfg.get("emit") or "on change") == "on change":
            return None
        val = cfg.get("on_value", "1") if state else cfg.get("off_value", "0")
        self._emit(fid, nid, "ok", "latched %s" % ("on" if state else "off"))
        return {"payload": render(val, msg, ctx),
                "meta": dict(msg.get("meta", {}), latched=state, by=port)}

    def _watchdog(self, fid, nid, cfg, msg, ctx=None):
        """Arm on every message; fire when one does not arrive in time."""
        key = (fid, nid)
        timeout = min(600.0, max(0.02, self._tnum(cfg.get("timeout"), msg, ctx, 500) / 1000.0))
        # Nothing left to cancel, on a node that has been fed before, means the
        # booking already went off: the feed stopped and this is it coming back.
        first = key not in self.dogs
        came_back = self.clock.cancel(key) == 0 and not first
        self.dogs[key] = True
        late = {"payload": render(cfg.get("value", "0"), msg, ctx),
                "meta": dict(msg.get("meta", {}), starved=True,
                             silent_ms=int(timeout * 1000))}
        self.clock.at(timeout, key, late, tag=key)
        if came_back:
            self._emit(fid, nid, "ok", "the feed came back")
            if (cfg.get("recovery") or "no") == "yes":
                return {"payload": msg.get("payload"),
                        "meta": dict(msg.get("meta", {}),
                                     starved=False, silent_ms=0)}
        return None

    def _device_command(self, fid, nid, cfg, msg, ctx=None):
        """Queue one command for a field device."""
        device = render(cfg.get("device") or "", msg, ctx).strip()
        op = (cfg.get("op") or "").strip()
        if not device:
            self._emit(fid, nid, "warn", "no device chosen")
            return None
        if op not in DEVICE_OPS:
            self._emit(fid, nid, "warn", "unknown command %r" % op)
            return None
        if not self.fleet:
            self._emit(fid, nid, "warn", "the fleet is not available here")
            return None
        command = {"op": op}
        if op == "fire":
            command["node"] = render(cfg.get("node") or "", msg, ctx).strip()
            if not command["node"]:
                self._emit(fid, nid, "warn", "fire needs a node to fire")
                return None
        elif op == "set":
            gpio = self._tnum(cfg.get("gpio"), msg, ctx, -1)
            if gpio < 0:
                self._emit(fid, nid, "warn", "set needs a pin")
                return None
            command["gpio"] = int(gpio)
            command["value"] = int(self._tnum(cfg.get("value"), msg, ctx, 0))
        elif op == "camera-on":
            command["frame_size"] = cfg.get("frame_size")
            command["format"] = cfg.get("format")
        targets = self.fleet.targets(device)
        if not targets:
            self._emit(fid, nid, "warn", "%s has no devices in it" % device)
            return None
        for target in targets:
            try:
                self.fleet.push(target, command)
            except Exception as exc:
                self._emit(fid, nid, "critical", "could not reach %s: %s" % (target, exc))
                return None
        self._emit(fid, nid, "ok", "%s → %s" % (op, device if len(targets) == 1
                                                  and targets[0] == device
                                                  else "%s (%d)" % (device, len(targets))))
        meta = dict(msg.get("meta", {}))
        meta["device"] = device
        meta["devices"] = targets
        return {"payload": msg.get("payload"), "meta": meta}

    def _http(self, fid, nid, cfg, msg, ctx=None):
        url = render((cfg.get("url") or "").strip(), msg, ctx)
        if not url.startswith(("http://", "https://")):
            self._emit(fid, nid, "warn", "url must start with http:// or https://")
            return None
        method = (cfg.get("method") or "GET").upper()
        body = render(cfg.get("body") or "", msg, ctx)
        data = body.encode("utf-8") if body and method != "GET" else None
        req = urllib.request.Request(url, data=data, method=method)
        raw_headers = (cfg.get("headers") or "").strip()
        if raw_headers:
            try:
                for k, v in json.loads(raw_headers).items():
                    req.add_header(k, str(v))
            except ValueError:
                self._emit(fid, nid, "warn", "headers are not valid JSON; ignored")
        if data is not None and not req.has_header("Content-type"):
            req.add_header("Content-Type", "application/json")
        timeout = min(120.0, _num(cfg.get("timeout"), 10))
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                text = r.read(65536).decode("utf-8", "replace")
                status = r.status
        except urllib.error.HTTPError as exc:
            text, status = exc.read(4096).decode("utf-8", "replace"), exc.code
        except Exception as exc:
            self._emit(fid, nid, "critical", "%s %s failed: %s" % (method, url, exc))
            return None
        self._emit(fid, nid, "ok" if status < 400 else "warn", "%s %s -> %s" % (method, url, status))
        try:
            payload = json.loads(text)
        except ValueError:
            payload = text
        return {"payload": payload, "meta": dict(msg.get("meta", {}), status=status)}

    def _shell(self, fid, nid, cfg, msg, ctx=None):
        cmd = render(cfg.get("cmd") or "", msg, ctx)
        if not cmd.strip():
            return None
        timeout = min(120.0, _num(cfg.get("timeout"), 15))
        try:
            p = subprocess.run(["bash", "-lc", cmd], capture_output=True,
                               text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            self._emit(fid, nid, "warn", "command timed out")
            return None
        out = (p.stdout or "").strip()
        self._emit(fid, nid, "ok" if p.returncode == 0 else "warn",
                   "exit %d%s" % (p.returncode, (": " + out[:80]) if out else ""))
        return {"payload": out, "meta": dict(msg.get("meta", {}), code=p.returncode)}

    # -- run log -----------------------------------------------------------
    def _emit(self, flow_id, node_id, level, message):
        row = {"time": time.strftime("%H:%M:%S"), "flow": flow_id,
               "node": node_id, "level": level, "message": message}
        with self.lock:
            self.runs.append(row)
            if len(self.runs) > 200:
                self.runs = self.runs[-200:]
        if self.bus:
            self.bus.publish("flow", row)

    def _record_output(self, flow_id, node_id, msg, port=None):
        """What a node just produced, so the inspector can show it."""
        row = {"time": time.strftime("%H:%M:%S"), "flow": flow_id,
               "node": node_id, "port": port, "kind": "output"}
        if msg is None:
            row["stopped"] = True
        else:
            row["payload"] = _clip(msg.get("payload"))
            meta = msg.get("meta")
            row["meta"] = {k: _clip(v, 120) for k, v in list(meta.items())[:8]} \
                if isinstance(meta, dict) else None
        with self.lock:
            self.last_output[(flow_id, node_id)] = row
        if self.bus:
            self.bus.publish("flow", row)

    def outputs(self):
        """The whole map, keyed "flow/node" so it survives JSON."""
        with self.lock:
            return {"%s/%s" % k: v for k, v in self.last_output.items()}

    def recent(self, n=60):
        with self.lock:
            return self.runs[-n:]

    def bindings(self):
        """{gpio: ["flow name · node label"]} — what the pin map shows."""
        out = {}
        for flow in self.doc.get("flows", []):
            for node in flow.get("nodes", []):
                pin = (node.get("config") or {}).get("gpio")
                if pin is None:
                    continue
                label = REGISTRY.get(node.get("type"), {}).get("label", node.get("type"))
                out.setdefault(int(pin), []).append("%s · %s" % (flow.get("name", "flow"), label))
        return out


def _topic_matches(filter_, topic):
    """MQTT wildcard match: + is one level, # is the rest."""
    f, t = filter_.split("/"), topic.split("/")
    for i, part in enumerate(f):
        if part == "#":
            return True
        if i >= len(t):
            return False
        if part != "+" and part != t[i]:
            return False
    return len(f) == len(t)


class _Scheduler(threading.Thread):
    """One thread holding everything waiting to happen later."""

    daemon = True

    def __init__(self, deliver):
        super().__init__(name="flow-scheduler")
        self.deliver = deliver          # fn(key, msg)
        self.cond = threading.Condition()
        self.items = []                 # [(due, seq, tag, key, msg)]
        self.seq = 0
        self._stop = threading.Event()

    def at(self, delay, key, msg, tag=None):
        """Deliver `msg` to `key` after `delay` seconds."""
        with self.cond:
            self.seq += 1
            self.items.append((time.monotonic() + max(0.0, delay),
                               self.seq, tag, key, msg))
            self.items.sort(key=lambda i: i[0])
            self.cond.notify()

    def cancel(self, tag):
        """Drop anything this node was waiting to send. Returns how many."""
        with self.cond:
            before = len(self.items)
            self.items = [i for i in self.items if i[2] != tag]
            self.cond.notify()
            return before - len(self.items)

    def pending(self, tag):
        with self.cond:
            return sum(1 for i in self.items if i[2] == tag)

    def clear(self):
        with self.cond:
            self.items = []
            self.cond.notify()

    def stop(self):
        self._stop.set()
        with self.cond:
            self.cond.notify()

    def run(self):
        while not self._stop.is_set():
            with self.cond:
                if not self.items:
                    self.cond.wait(1.0)
                    continue
                wait = self.items[0][0] - time.monotonic()
                if wait > 0:
                    self.cond.wait(min(wait, 1.0))
                    continue
                _due, _seq, _tag, key, msg = self.items.pop(0)
            try:
                self.deliver(key, msg)
            except Exception:
                pass


class _Interval(threading.Thread):
    daemon = True

    def __init__(self, seconds, fn):
        super().__init__(name="interval")
        self.seconds, self.fn = seconds, fn
        self._stop = threading.Event()

    def run(self):
        while not self._stop.wait(self.seconds):
            try:
                self.fn()
            except Exception:
                pass

    def stop(self):
        self._stop.set()
