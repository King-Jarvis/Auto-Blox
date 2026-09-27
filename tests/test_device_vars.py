"""`{{device.x}}` — the device this flow is deployed to, without naming it."""
import os
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from zero2w_console import flows  # noqa: E402


class FakeStore:
    def __init__(self, devices):
        self.devices = devices

    def load(self):
        return {"devices": self.devices}


class FakeFleet:
    def __init__(self, devices):
        self.devices = FakeStore(devices)


DEVICE = {
    "id": "dev_ab12cd34", "name": "ESP32-Cam", "board": "esp32cam",
    "ip": "10.42.0.152", "rssi": -58, "uptime": 4821,
    "last_seen": None,          # set per test
    "state": {"free": 41216},
}


class TestTheyAreInTheLibrary(unittest.TestCase):
    def test_there_is_a_device_group(self):
        groups = {v["group"] for v in flows.VARIABLES}
        self.assertIn("Device", groups)

    def test_it_covers_what_a_flow_would_want_to_know(self):
        names = {v["name"] for v in flows.VARIABLES if v["group"] == "Device"}
        self.assertEqual(names, {
            "device.id", "device.name", "device.board", "device.ip",
            "device.rssi", "device.uptime", "device.free", "device.online",
            "device.seen"})

    def test_the_numbers_are_typed_as_numbers(self):
        """Or a number field refuses them and offers a text box instead."""
        for v in flows.VARIABLES:
            if v["name"] in ("device.rssi", "device.uptime", "device.free",
                             "device.online", "device.seen"):
                with self.subTest(name=v["name"]):
                    self.assertEqual(v["type"], "number")

    def test_each_one_explains_itself(self):
        for v in flows.VARIABLES:
            if v["group"] == "Device":
                with self.subTest(name=v["name"]):
                    self.assertTrue(v["desc"].strip().endswith("."))


class TestResolvingThemOnTheHost(unittest.TestCase):
    def setUp(self):
        self.device = dict(DEVICE, last_seen=time.time() - 2)
        self.e = flows.FlowEngine(flows.FlowStore(os.devnull), None, None, None)
        self.e.fleet = FakeFleet([self.device])
        self.flow = {"id": "f", "name": "f", "board": "esp32cam",
                     "device": "dev_ab12cd34"}

    def tearDown(self):
        self.e.clock.stop()

    def value(self, name, flow=None):
        ctx = self.e._ctx(flow or self.flow, {"id": "n", "type": "log.write"})
        return flows.resolve_variable(name, {"payload": 1, "meta": {}}, ctx)

    def test_the_plain_facts_come_from_the_record(self):
        self.assertEqual(self.value("device.id"), "dev_ab12cd34")
        self.assertEqual(self.value("device.name"), "ESP32-Cam")
        self.assertEqual(self.value("device.board"), "esp32cam")
        self.assertEqual(self.value("device.ip"), "10.42.0.152")

    def test_the_readings_come_from_what_it_last_said(self):
        self.assertEqual(self.value("device.rssi"), -58)
        self.assertEqual(self.value("device.uptime"), 4821)
        self.assertEqual(self.value("device.free"), 41216)

    def test_a_device_reporting_recently_is_online(self):
        self.assertEqual(self.value("device.online"), 1)
        self.assertLess(self.value("device.seen"), 5)

    def test_one_that_has_gone_quiet_is_not(self):
        self.device["last_seen"] = time.time() - 300
        self.assertEqual(self.value("device.online"), 0)
        self.assertGreater(self.value("device.seen"), 200)

    def test_one_that_has_never_reported_is_not_online(self):
        self.device["last_seen"] = None
        self.assertEqual(self.value("device.online"), 0)
        self.assertIsNone(self.value("device.seen"))

    def test_a_flow_linked_to_nothing_resolves_none_of_them(self):
        """Left verbatim in output, like any unknown name, which says
        plainly that nothing is linked rather than quietly rendering
        empty."""
        loose = {"id": "f", "name": "f"}
        self.assertIsNone(self.value("device.ip", loose))

    def test_a_flow_naming_a_device_that_is_gone_resolves_none_of_them(self):
        self.assertIsNone(self.value("device.ip",
                                     dict(self.flow, device="dev_missing")))

    def test_an_unknown_device_field_is_not_invented(self):
        self.assertIsNone(self.value("device.colour"))

    def test_they_read_through_a_real_field(self):
        node = {"id": "n", "type": "logic.set",
                "config": {"value": "{{device.name}} at {{device.ip}}"}}
        _p, out = self.e._execute(self.flow, node, {"payload": 1, "meta": {}})
        self.assertEqual(out["payload"], "ESP32-Cam at 10.42.0.152")

    def test_and_a_formula_can_do_arithmetic_on_one(self):
        node = {"id": "n", "type": "math.expr",
                "config": {"expr": "{{device.rssi}} + 100"}}
        _p, out = self.e._execute(self.flow, node, {"payload": 1, "meta": {}})
        self.assertEqual(out["payload"], 42)


class TestTheDeviceAnswersForItself(unittest.TestCase):
    """The device-side render resolves the same names from the agent, so a
    flow reads the same whether it is being tested here or running out
    there."""

    def test_the_runner_handles_the_same_names(self):
        path = os.path.join(ROOT, "zero2w_console", "agent", "flow.py")
        with open(path) as fh:
            src = fh.read()
        self.assertIn('key.startswith("device.")', src)
        for field in ("id", "name", "board", "ip", "rssi", "uptime", "free",
                      "online", "seen"):
            with self.subTest(field=field):
                self.assertIn('== "%s"' % field, src)

    def test_it_asks_the_agent_rather_than_carrying_its_own_copy(self):
        path = os.path.join(ROOT, "zero2w_console", "agent", "flow.py")
        with open(path) as fh:
            src = fh.read()
        body = src[src.index("def _about_me"):]
        body = body[:body.index("\n\n\n")] if "\n\n\n" in body else body
        self.assertIn("agent", body)


if __name__ == "__main__":
    unittest.main()
