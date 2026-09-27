"""`{{variables}}`: the same name has to mean the same thing on both
runners."""
import importlib.util
import os
import sys
import time as _real
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from zero2w_console import flows  # noqa: E402

DEVICE_FLOW = os.path.join(ROOT, "zero2w_console", "agent", "flow.py")

# Prefixes a device is expected to answer, because a flow deployed to one can
# use them and nothing on the host is involved once it is running.
# `pad.` is on both because the controller is read where the flow runs: a board
# holds its own BLE link and answers from that, and a host reads the kernel's
# input layer. Neither is asking the other, which is the whole reason the pad
# belongs on the device at all.
ON_BOTH = ("payload", "meta.", "device.", "tag.", "pad.")

# And the ones only a host flow gets, each for a stated reason. A device that
# answered these would be making numbers up.
HOST_ONLY = {
    "flow.": "the device is told its flow, not the flow document",
    "node.": "same",
    "time": "no RTC unless something sets it",
    "date": "same",
    "iso": "same",
    "epoch": "same",
    "host": "the hostname of the console, not of the board",
    "uptime": "the console's, and the board has {{device.uptime}}",
    "cpu.": "this board's readings",
    "gpu.": "same",
    "ve.": "same",
    "ddr.": "same",
    "mem.": "same",
    "root.": "same",
    "net.": "same",
    "load1": "same",
    "load5": "same",
}


def load_device_runner():
    """The real `agent/flow.py`, with `machine` stubbed."""
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
    spec = importlib.util.spec_from_file_location("agentflow_vars", DEVICE_FLOW)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    clock = type(sys)("time")
    clock.time, clock.sleep = _real.time, _real.sleep
    clock.sleep_ms = lambda ms: None
    clock.ticks_ms = lambda: int(_real.monotonic() * 1000)
    clock.ticks_diff = lambda a, b: a - b
    clock.ticks_add = lambda t, d: t + d
    mod.time = clock
    return mod


class VariableCase(unittest.TestCase):
    def setUp(self):
        self.dev = load_device_runner()

        class Agent:
            device = "dev_ef56ab12"
            cfg = {"name": "ESP32 Motor", "board": "esp32"}
            tags = {"drive_speed": 0.42, "drive_armed": 1, "empty": 0}
            online = True

            def log(self, *a, **kw):
                pass

            def ip(self):
                return "10.42.0.140"

            def rssi(self):
                return -57

        self.agent = Agent()
        flow = {"id": "f", "nodes": [], "edges": []}
        self.runner = self.dev.Runner(flow, self.agent)
        self.dev._RUNNER = self.runner

    def tearDown(self):
        self.dev._RUNNER = None
        sys.modules.pop("machine", None)

    def on_device(self, text, msg=None):
        return self.dev.render(text, msg or {"payload": 1, "meta": {}})

    def on_host(self, text, tags=None):
        table = tags if tags is not None else self.agent.tags
        return flows.resolve_variable(text, {"payload": 1, "meta": {}},
                                      {"tags": table})


class TestTagsResolveOnADevice(VariableCase):
    """The bug itself, stated three ways."""

    def test_a_tag_resolves(self):
        self.assertEqual(self.on_device("{{tag.drive_speed}}"), "0.42")

    def test_a_tag_in_a_formula_is_a_number_and_not_the_text(self):
        """This is what made it silent: the unresolved text went through
        `num()`, which is where a speed became a nought."""
        rendered = self.on_device("{{payload}} - {{tag.drive_speed}}")
        self.assertNotIn("{{", rendered)
        self.assertEqual(self.dev.num(self.on_device("{{tag.drive_speed}}"), -1),
                         0.42)

    def test_the_two_sides_give_the_same_tag_the_same_value(self):
        """The actual claim, rather than each side being checked alone."""
        for name in ("drive_speed", "drive_armed", "empty"):
            with self.subTest(tag=name):
                self.assertEqual(self.on_device("{{tag.%s}}" % name),
                                 str(self.on_host("tag." + name)))

    def test_a_tag_that_does_not_exist_is_left_visible(self):
        """The same rule as every other unknown name: a typo must be
        readable, not an empty string that quietly becomes zero."""
        self.assertEqual(self.on_device("{{tag.nope}}"), "{{tag.nope}}")

    def test_a_tag_holding_zero_is_not_mistaken_for_missing(self):
        """`if not tags` and `tags.get(name) or None` would both get this
        wrong, and zero is the value a stopped motor has."""
        self.assertEqual(self.on_device("{{tag.empty}}"), "0")

    def test_it_survives_a_runner_with_no_agent(self):
        self.dev._RUNNER = None
        self.assertEqual(self.on_device("{{tag.drive_speed}}"),
                         "{{tag.drive_speed}}")

    def test_it_survives_an_agent_with_no_tags(self):
        self.agent.tags = None
        self.assertEqual(self.on_device("{{tag.drive_speed}}"),
                         "{{tag.drive_speed}}")


class TestTheTwoSidesAgree(VariableCase):
    def test_every_declared_variable_is_placed_on_one_side_or_the_other(self):
        """Nothing in the variable table may be unaccounted for."""
        for row in flows.VARIABLES:
            name = row["name"]
            both = any(name == p or name.startswith(p) for p in ON_BOTH)
            host = any(name == p or name.startswith(p) for p in HOST_ONLY)
            with self.subTest(variable=name):
                self.assertTrue(both or host,
                                "%s is in the variable table and neither "
                                "runner has been said to own it" % name)
                self.assertFalse(both and host, "%s is claimed by both" % name)

    def test_the_device_answers_everything_it_is_supposed_to(self):
        msg = {"payload": 7, "meta": {"edge": "rising"}}
        for text in ("{{payload}}", "{{meta.edge}}", "{{device.id}}",
                     "{{device.name}}", "{{device.ip}}", "{{device.online}}",
                     "{{tag.drive_speed}}"):
            with self.subTest(text=text):
                self.assertNotIn("{{", self.on_device(text, msg),
                                 "%s went unresolved on the device" % text)

    def test_the_device_answers_a_stick_from_its_own_pad(self):
        """The regression this guards: a mixer reading {{pad.lx}} on a board
        where the name went unresolved would subtract the literal text, which
        reads as straight ahead for ever."""
        from zero2w_console.agent.modules import pad as padmod
        self.runner.pad.update(padmod.blank())
        self.runner.pad.update({"connected": True, "lx": 0.25})
        self.assertEqual(self.on_device("{{pad.lx}}"), "0.25")
        self.assertEqual(self.on_device("{{pad.connected}}"), "1")

    def test_a_device_with_no_pad_leaves_the_name_alone(self):
        """Verbatim, not zero: an unresolved stick must be visible."""
        self.assertEqual(self.on_device("{{pad.lx}}"), "{{pad.lx}}")

    def test_device_uptime_is_since_this_run_and_not_since_power_on(self):
        """`time.time()` on an ESP32 counts from power-on and keeps counting
        through `machine.reset()`, because the RTC is not in the reset
        domain."""
        self.agent.started = _real.time() - 42
        self.assertAlmostEqual(int(self.on_device("{{device.uptime}}")), 42,
                               delta=2)

    def test_the_device_leaves_host_only_names_alone(self):
        """Visibly alone."""
        for text in ("{{cpu.temp}}", "{{load1}}", "{{host}}", "{{flow.name}}"):
            with self.subTest(text=text):
                self.assertEqual(self.on_device(text), text)

    def test_the_prefixes_the_device_handles_are_exactly_these(self):
        """Read off the source, so adding a branch without deciding which
        list it belongs in fails here."""
        with open(DEVICE_FLOW, encoding="utf-8") as fh:
            src = fh.read()
        body = src[src.index("def render("):]
        body = body[:body.index("\n\n\n")]
        found = set()
        for line in body.splitlines():
            if "key.startswith(" in line:
                found.add(line.split('startswith("')[1].split('"')[0])
            elif "key ==" in line:
                found.add(line.split('key == "')[1].split('"')[0])
        self.assertEqual(found, {"payload", "meta.", "device.", "tag.", "pad."})


if __name__ == "__main__":
    unittest.main()
