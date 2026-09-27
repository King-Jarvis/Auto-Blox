"""What a board said, still readable after it has stopped saying anything."""
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from zero2w_console import fleet as fleetmod       # noqa: E402
from zero2w_console import flows as flowmod        # noqa: E402
from zero2w_console import iot as iotmod           # noqa: E402


class Bus:
    def __init__(self):
        self.events = []

    def publish(self, topic, data):
        self.events.append((topic, data))


class LogCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.devices = iotmod.DeviceStore(os.path.join(self.dir.name, "iot.json"))
        self.flows = flowmod.FlowStore(os.path.join(self.dir.name, "flows.json"))
        self.bus = Bus()
        self.fleet = fleetmod.Fleet(self.devices, self.flows, self.bus)
        self.device = iotmod.create_device(self.devices, {
            "name": "ESP32 Motor", "board": "esp32", "mac": "98:f4:ab:00:00:10"})

    def said(self, message, level="critical", kind="log"):
        self.fleet.event(self.record(), {"kind": kind, "level": level,
                                         "message": message})

    def record(self):
        return iotmod.get_device(self.devices, self.device["id"])

    def messages(self):
        return [e["message"] for e in self.fleet.device_log(self.device["id"])]


class TestItKeepsWhatADeviceSaid(LogCase):
    def test_a_message_can_be_read_back(self):
        self.said("the deployed flow would not start: memory allocation failed")
        self.assertEqual(
            self.messages(),
            ["the deployed flow would not start: memory allocation failed"])

    def test_the_order_is_oldest_first(self):
        for n in ("one", "two", "three"):
            self.said(n)
        self.assertEqual(self.messages(), ["one", "two", "three"])

    def test_it_carries_the_level_and_a_timestamp(self):
        self.said("boom")
        row = self.fleet.device_log(self.device["id"])[-1]
        self.assertEqual(row["level"], "critical")
        self.assertTrue(row["time"])
        self.assertIsInstance(row["at"], int)

    def test_it_still_reaches_the_stream(self):
        """The ring is in addition to the live stream, not instead of it."""
        self.said("boom")
        self.assertTrue(any(t == "flow" and p["message"] == "boom"
                            for t, p in self.bus.events))

    def test_a_console_with_no_bus_still_keeps_history(self):
        """The ring must not depend on anyone listening — that dependency is
        the whole bug."""
        fleet = fleetmod.Fleet(self.devices, self.flows, None)
        fleet.event(self.record(), {"level": "warn", "message": "alone"})
        self.assertEqual([e["message"] for e in fleet.device_log(self.device["id"])],
                         ["alone"])

    def test_link_open_and_close_are_in_it(self):
        """They bracket every disappearance, so they are the most useful two
        lines in the ring."""
        class Conn:
            device_id = self.device["id"]
            address = ("10.42.0.140", 63944)
        self.fleet.on_link_open(Conn())
        self.fleet.on_link_close(Conn())
        said = " ".join(self.messages())
        self.assertIn("on the link", said)
        self.assertIn("left the link", said)


class TestFiredNodesAreNotLogLines(LogCase):
    def test_a_report_full_of_fired_nodes_keeps_the_ring_clean(self):
        self.said("the deployed flow would not start: out of memory")
        for _ in range(6):
            self.fleet.report_state(self.record(), {
                "running": True, "fired": ["n%d" % i for i in range(64)]})
        self.assertEqual(
            self.messages(),
            ["the deployed flow would not start: out of memory"],
            "384 canvas flashes pushed out the one line that mattered")

    def test_they_still_reach_the_stream(self):
        self.fleet.report_state(self.record(), {"fired": ["n1"]})
        self.assertTrue(any(p.get("kind") == "fired" for _t, p in self.bus.events),
                        "the canvas stopped flashing")


class TestItIsBounded(LogCase):
    def test_it_holds_no_more_than_the_cap(self):
        for n in range(fleetmod.HISTORY + 50):
            self.said("line %d" % n)
        rows = self.fleet.device_log(self.device["id"])
        self.assertEqual(len(rows), fleetmod.HISTORY)
        self.assertEqual(rows[-1]["message"], "line %d" % (fleetmod.HISTORY + 49))
        self.assertNotIn("line 0", self.messages(), "the cap is not dropping")

    def test_a_limit_narrows_it(self):
        for n in range(20):
            self.said("line %d" % n)
        self.assertEqual(len(self.fleet.device_log(self.device["id"], 5)), 5)

    def test_a_nonsense_limit_does_not_raise(self):
        self.said("one")
        for bad in (0, -1, None, "7", fleetmod.HISTORY * 10):
            with self.subTest(limit=bad):
                self.assertTrue(self.fleet.device_log(self.device["id"], bad))

    def test_devices_do_not_share_a_ring(self):
        other = iotmod.create_device(self.devices, {
            "name": "ESP32-Cam", "board": "esp32cam", "mac": "70:4b:ca:00:00:b8"})
        self.said("mine")
        self.fleet.event(iotmod.get_device(self.devices, other["id"]),
                         {"level": "ok", "message": "theirs"})
        self.assertEqual(self.messages(), ["mine"])
        self.assertEqual([e["message"] for e in self.fleet.device_log(other["id"])],
                         ["theirs"])

    def test_a_device_that_never_spoke_reads_as_empty(self):
        self.assertEqual(self.fleet.device_log("dev_nothing"), [])


if __name__ == "__main__":
    unittest.main()
