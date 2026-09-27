"""The runner's own attributes, and the one that got assigned twice."""
import ast
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

AGENT = os.path.join(ROOT, "zero2w_console", "agent", "flow.py")


def runner_init():
    with open(AGENT) as fh:
        tree = ast.parse(fh.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Runner":
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                    return item
    raise AssertionError("Runner.__init__ not found")


def assigned_names(fn):
    """Every `self.x = ...` in order, so a repeat is visible."""
    out = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if (isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"):
                out.append(target.attr)
    return out


class TestNoAttributeIsAssignedTwice(unittest.TestCase):
    def test_every_name_in_init_is_set_once(self):
        names = assigned_names(runner_init())
        seen, twice = set(), []
        for name in names:
            if name in seen:
                twice.append(name)
            seen.add(name)
        self.assertEqual(twice, [],
                         "assigned twice in Runner.__init__: %s — the second "
                         "one wins and the first is silently the wrong type"
                         % twice)

    def test_the_interval_list_is_still_a_list(self):
        """start() appends to it; a dict there throws and stops the ticking."""
        with open(AGENT) as fh:
            src = fh.read()
        self.assertIn("self.timers = []", src)
        self.assertNotIn("self.timers = {}", src)

    def test_and_the_timer_node_keeps_its_own_name(self):
        with open(AGENT) as fh:
            src = fh.read()
        self.assertIn("self.timer_state = {}", src)


class TestStartArmsWhatTheFlowAsksFor(unittest.TestCase):
    """Run the real Runner against stub hardware, which is the check that
    would have caught this outright."""

    def setUp(self):
        self.stubs = {}
        for name in ("machine",):
            if name not in sys.modules:
                self.stubs[name] = True
                mod = type(sys)(name)

                class Pin:
                    IN = OUT = PULL_UP = 0
                    IRQ_RISING = 1
                    IRQ_FALLING = 2

                    def __init__(self, *a, **kw):
                        pass

                    def value(self, *a):
                        return 0

                    def irq(self, **kw):
                        pass

                mod.Pin = Pin
                mod.PWM = Pin
                sys.modules[name] = mod
        import importlib.util
        import time as _real
        spec = importlib.util.spec_from_file_location("agentflow", AGENT)
        self.flowmod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.flowmod)
        # MicroPython's millisecond clock, which CPython does not have.
        clock = type(sys)("time")
        clock.time = _real.time
        clock.sleep = _real.sleep
        clock.sleep_ms = lambda ms: _real.sleep(ms / 1000.0)
        clock.ticks_ms = lambda: int(_real.monotonic() * 1000)
        clock.ticks_diff = lambda a, b: a - b
        clock.ticks_add = lambda t, d: t + d
        self.flowmod.time = clock

    def tearDown(self):
        for name in self.stubs:
            sys.modules.pop(name, None)

    def runner(self, nodes, edges=None):
        class Agent:
            def log(self, *a, **kw):
                pass
        return self.flowmod.Runner(
            {"id": "f", "nodes": nodes, "edges": edges or []}, Agent())

    def test_an_interval_node_arms_a_timer(self):
        r = self.runner([{"id": "t", "type": "timer.interval",
                          "config": {"every": 1000}}])
        r.start()
        self.assertTrue(r.running)
        self.assertEqual(len(r.timers), 1, "the Interval was never armed")
        self.assertEqual(r.timers[0][0], "t")
        self.assertEqual(r.timers[0][1], 1000)

    def test_a_very_short_interval_is_floored(self):
        r = self.runner([{"id": "t", "type": "timer.interval",
                          "config": {"every": 1}}])
        r.start()
        self.assertEqual(r.timers[0][1], 50)

    def test_the_heartbeat_flow_arms(self):
        """The exact shape that was silently dead on the bench."""
        r = self.runner([
            {"id": "t", "type": "timer.interval", "config": {"every": 1000}},
            {"id": "c", "type": "logic.count", "config": {"target": 0}},
            {"id": "led", "type": "gpio.out", "config": {"gpio": 2, "action": "toggle"}},
            {"id": "say", "type": "host.notify", "config": {"kind": "heartbeat"}},
        ])
        r.start()
        self.assertEqual(len(r.timers), 1)

    def test_a_flow_with_no_trigger_arms_nothing_and_does_not_throw(self):
        r = self.runner([{"id": "l", "type": "log.write", "config": {}}])
        r.start()
        self.assertEqual(r.timers, [])
        self.assertTrue(r.running)

    def test_starting_twice_does_not_double_arm(self):
        """A reload builds a fresh runner; if it ever stopped doing that, a
        board would tick at twice the rate and nobody would know why."""
        r = self.runner([{"id": "t", "type": "timer.interval",
                          "config": {"every": 1000}}])
        r.start()
        first = len(r.timers)
        fresh = self.runner([{"id": "t", "type": "timer.interval",
                              "config": {"every": 1000}}])
        fresh.start()
        self.assertEqual(len(fresh.timers), first)


if __name__ == "__main__":
    unittest.main()
