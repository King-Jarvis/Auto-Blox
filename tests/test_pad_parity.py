"""The same controller reading, through both runtimes, compared.

`agent/modules/pad.py` holds the naming and the scaling and both sides call it,
so this is not testing arithmetic twice — it is testing that the two *handlers*
around that arithmetic agree. That is where the last divergence of this kind
lived: `pwm.out` read a config key the host spelled differently and sat broken on
devices for months, which is why `tests/test_drive_blocks.py` grew the same
sweep for the drive blocks.
"""
import importlib.util
import os
import sys
import time as _real
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from zero2w_console import flows                            # noqa: E402
from zero2w_console.agent.modules import pad as padmod       # noqa: E402

AGENT = os.path.join(ROOT, "zero2w_console", "agent", "flow.py")

# Raw axis values a stick and a trigger actually produce, plus the edges.
SWEEP = (-1.0, -0.61, -0.1, 0.0, 0.1, 0.61, 1.0, 2.5, -2.5)


class TestBothSidesAgree(unittest.TestCase):
    def setUp(self):
        machine = type(sys)("machine")

        class Pin:
            IN = OUT = PULL_UP = 0
            IRQ_RISING, IRQ_FALLING = 1, 2

            def __init__(self, *a, **kw):
                pass

            def value(self, *a):
                return 0

            def irq(self, **kw):
                pass

        machine.Pin = machine.PWM = Pin
        sys.modules["machine"] = machine
        # On a board these sit under modules/ and the runner reaches them with
        # __import__("modules.x"). Stand that package up so the path under test
        # is the real one — and leave `modules.blepad` absent, which is what a
        # board looks like before the BLE module has ever been deployed.
        pkg = type(sys)("modules")
        pkg.__path__ = []
        pkg.pad = padmod
        sys.modules["modules"] = pkg
        sys.modules["modules.pad"] = padmod
        spec = importlib.util.spec_from_file_location("agentflow_pad", AGENT)
        self.dev = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.dev)
        clock = type(sys)("time")
        clock.time, clock.sleep = _real.time, _real.sleep
        clock.sleep_ms = lambda ms: None
        clock.ticks_ms = lambda: int(_real.monotonic() * 1000)
        clock.ticks_diff = lambda a, b: a - b
        clock.ticks_add = lambda t, d: t + d
        self.dev.time = clock
        self.host = flows.FlowEngine.__new__(flows.FlowEngine)
        self.host.pad = padmod.blank()
        self.host.pad_reader = None

    def tearDown(self):
        for name in ("machine", "modules", "modules.pad"):
            sys.modules.pop(name, None)

    # -- the two paths -----------------------------------------------------
    def runner(self, ntype, cfg):
        class Agent:
            tags = {}

            def log(self, *a, **kw):
                pass

        flow = {"id": "f", "nodes": [{"id": "n", "type": ntype, "config": cfg}],
                "edges": []}
        return self.dev.Runner(flow, Agent())

    def on_device(self, ntype, cfg, values, age_ms=0):
        r = self.runner(ntype, cfg)
        r.pad.update(padmod.blank())
        r.pad.update({"connected": True, "name": "Xbox",
                      "at": self.dev.time.ticks_ms() - age_ms})
        r.pad.update(values)
        sent = []
        r._emit = lambda nid, msg, hops, port="out": sent.append(msg)
        r._run("n", {"payload": 1, "meta": {}}, 0)
        return sent[0]["payload"] if sent else None

    def on_host(self, ntype, cfg, values, age_ms=0):
        self.host.pad.update(padmod.blank())
        self.host.pad.update({"connected": True, "name": "Xbox",
                              "at": _real.monotonic() - age_ms / 1000.0})
        self.host.pad.update(values)
        msg = {"payload": 1, "meta": {}}
        if ntype == "pad.axis":
            out = self.host._pad_axis(cfg, msg)
        else:
            out = self.host._pad_button(cfg, msg)
        return out["payload"] if out else None

    def both(self, ntype, cfg, values, age_ms=0):
        return (self.on_host(ntype, cfg, values, age_ms),
                self.on_device(ntype, cfg, values, age_ms))

    # -- axes --------------------------------------------------------------
    def test_every_axis_over_a_sweep(self):
        for name, key in sorted(padmod.AXES.items()):
            for raw in SWEEP:
                cfg = {"axis": name, "invert": "no", "stale_ms": 250,
                       "decimals": 3}
                host, dev = self.both("pad.axis", cfg, {key: raw})
                with self.subTest(axis=name, raw=raw):
                    self.assertIsNotNone(host)
                    self.assertAlmostEqual(host, dev, places=4)

    def test_inverted_too(self):
        for raw in SWEEP:
            cfg = {"axis": "left stick y", "invert": "yes", "stale_ms": 250,
                   "decimals": 3}
            host, dev = self.both("pad.axis", cfg, {"ly": raw})
            with self.subTest(raw=raw):
                self.assertAlmostEqual(host, dev, places=4)

    def test_decimals_agree(self):
        for places in (0, 1, 3, 6):
            cfg = {"axis": "left stick x", "invert": "no", "stale_ms": 250,
                   "decimals": places}
            host, dev = self.both("pad.axis", cfg, {"lx": 0.123456})
            with self.subTest(decimals=places):
                self.assertEqual(host, dev)

    # -- the deadman, on both sides ---------------------------------------
    def test_a_stale_axis_says_nothing_on_both(self):
        cfg = {"axis": "right trigger", "invert": "no", "stale_ms": 250,
               "decimals": 3}
        host, dev = self.both("pad.axis", cfg, {"rt": 0.9}, age_ms=2000)
        self.assertIsNone(host)
        self.assertIsNone(dev)

    def test_a_fresh_axis_speaks_on_both(self):
        cfg = {"axis": "right trigger", "invert": "no", "stale_ms": 250,
               "decimals": 3}
        host, dev = self.both("pad.axis", cfg, {"rt": 0.9}, age_ms=10)
        self.assertAlmostEqual(host, 0.9, places=3)
        self.assertAlmostEqual(dev, 0.9, places=3)

    def test_a_stale_held_button_reads_zero_on_both(self):
        cfg = {"button": "a", "emit": "held", "stale_ms": 250}
        host, dev = self.both("pad.button", cfg, {"a": 1}, age_ms=2000)
        self.assertEqual(host, 0)
        self.assertEqual(dev, 0)

    # -- buttons -----------------------------------------------------------
    def test_every_button_held(self):
        for name, key in sorted(padmod.BUTTONS.items()):
            for down in (0, 1):
                cfg = {"button": name, "emit": "held", "stale_ms": 250}
                host, dev = self.both("pad.button", cfg, {key: down})
                with self.subTest(button=name, down=down):
                    self.assertEqual(host, down)
                    self.assertEqual(dev, down)

    def test_edges_agree(self):
        for emit, seen, want in (("pressed", 1, 1), ("pressed", -1, None),
                                 ("released", -1, 1), ("released", 1, None),
                                 ("pressed", 0, None)):
            cfg = {"button": "a", "emit": emit, "stale_ms": 250}
            host, dev = self.both("pad.button", cfg, {"_a": seen})
            with self.subTest(emit=emit, seen=seen):
                self.assertEqual(host, want)
                self.assertEqual(dev, want)


class TestABoardWithNoRadioFailsSafe(unittest.TestCase):
    """A flow with pad nodes reaching a board whose BLE module never arrived.

    It has to load and it has to stop the wheels, not act as a plain wire. That
    is the shape `tests/test_drive_blocks.py` pins for a missing handler, and
    this is the same question asked of a missing *transport*.
    """

    def setUp(self):
        machine = type(sys)("machine")

        class Pin:
            IN = OUT = PULL_UP = 0
            IRQ_RISING, IRQ_FALLING = 1, 2

            def __init__(self, *a, **kw):
                pass

            def value(self, *a):
                return 0

            def irq(self, **kw):
                pass

        machine.Pin = machine.PWM = Pin
        sys.modules["machine"] = machine
        pkg = type(sys)("modules")
        pkg.__path__ = []
        pkg.pad = padmod
        sys.modules["modules"] = pkg
        sys.modules["modules.pad"] = padmod
        spec = importlib.util.spec_from_file_location("agentflow_noble", AGENT)
        self.dev = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.dev)
        clock = type(sys)("time")
        clock.time, clock.sleep = _real.time, _real.sleep
        clock.sleep_ms = lambda ms: None
        clock.ticks_ms = lambda: int(_real.monotonic() * 1000)
        clock.ticks_diff = lambda a, b: a - b
        clock.ticks_add = lambda t, d: t + d
        self.dev.time = clock

    def tearDown(self):
        for name in ("machine", "modules", "modules.pad"):
            sys.modules.pop(name, None)

    def build(self):
        said = []

        class Agent:
            tags = {}

            def log(self, level, text, **kw):
                said.append((level, text))

        flow = {"id": "f", "edges": [], "nodes": [
            {"id": "link", "type": "pad.link", "config": {"name": ""}},
            {"id": "ax", "type": "pad.axis",
             "config": {"axis": "right trigger", "invert": "no",
                        "stale_ms": 250, "decimals": 3}}]}
        return self.dev.Runner(flow, Agent()), said

    def test_the_link_node_says_what_is_missing_once(self):
        r, said = self.build()
        for _ in range(4):
            r._run("link", {"payload": 1, "meta": {}}, 0)
        warns = [t for lvl, t in said if lvl == "warn"]
        self.assertEqual(len(warns), 1, "it must say so, and say it once")
        self.assertIn("Bluetooth", warns[0])

    def test_the_link_node_still_passes_the_message_on(self):
        r, _said = self.build()
        sent = []
        r._emit = lambda nid, msg, hops, port="out": sent.append(msg)
        r._run("link", {"payload": 1, "meta": {}}, 0)
        self.assertEqual(len(sent), 1)
        self.assertIs(sent[0]["meta"]["pad"], False)

    def test_the_axis_node_stops_the_branch_rather_than_passing_a_zero(self):
        r, _said = self.build()
        sent = []
        r._emit = lambda nid, msg, hops, port="out": sent.append(msg)
        r._run("link", {"payload": 1, "meta": {}}, 0)
        r._run("ax", {"payload": 1, "meta": {}}, 0)
        self.assertEqual([m["payload"] for m in sent[1:]], [],
                         "a board with no radio must starve the watchdog")


if __name__ == "__main__":
    unittest.main()
