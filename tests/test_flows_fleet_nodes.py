"""The two directions between this board and a field device."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from zero2w_console import flows  # noqa: E402


class FakeFleet:
    def __init__(self, groups=None):
        self.pushed = []
        self.groups = groups or []

    def push(self, device_id, command):
        self.pushed.append((device_id, command))
        return command

    def targets(self, value):
        from zero2w_console import iot
        return iot.targets({"groups": self.groups}, value)


def engine(doc=None, fleet=None):
    e = flows.FlowEngine(flows.FlowStore(os.devnull), None, None, None)
    e.doc = doc or {"flows": []}
    e.fleet = fleet
    return e


def flow_with(node_cfg, ntype="host.event", enabled=True, board=None):
    f = {"id": "f1", "name": "f1", "enabled": enabled,
         "nodes": [{"id": "n1", "type": ntype, "config": node_cfg}], "edges": []}
    if board:
        f["board"] = board
    return {"flows": [f]}


class TestDeviceEventReachesAFlow(unittest.TestCase):
    def fired(self, e):
        return [e.jobs.get_nowait() for _ in range(e.jobs.qsize())]

    def test_a_matching_device_and_kind_fires(self):
        e = engine(flow_with({"device": "dev_1", "kind": "motion"}))
        self.assertEqual(e.fire_device_event("dev_1", "motion", 1), 1)
        key, msg = self.fired(e)[0]
        self.assertEqual(key, ("f1", "n1"))
        self.assertEqual(msg["payload"], 1)
        self.assertEqual(msg["meta"]["device"], "dev_1")
        self.assertEqual(msg["meta"]["kind"], "motion")

    def test_another_device_does_not_fire_it(self):
        e = engine(flow_with({"device": "dev_1", "kind": "motion"}))
        self.assertEqual(e.fire_device_event("dev_2", "motion", 1), 0)

    def test_another_kind_does_not_fire_it(self):
        e = engine(flow_with({"device": "dev_1", "kind": "motion"}))
        self.assertEqual(e.fire_device_event("dev_1", "button", 1), 0)

    def test_an_empty_kind_catches_everything_that_device_sends(self):
        e = engine(flow_with({"device": "dev_1", "kind": ""}))
        self.assertEqual(e.fire_device_event("dev_1", "anything", 1), 1)

    def test_an_empty_device_watches_the_whole_fleet(self):
        e = engine(flow_with({"device": "", "kind": "motion"}))
        self.assertEqual(e.fire_device_event("dev_9", "motion", 1), 1)

    def test_a_disabled_flow_stays_quiet(self):
        e = engine(flow_with({"device": "dev_1", "kind": "motion"}, enabled=False))
        self.assertEqual(e.fire_device_event("dev_1", "motion", 1), 0)

    def test_a_flow_that_runs_on_a_board_is_not_armed_here(self):
        """It runs from the device's own flash; this host does not execute it."""
        e = engine(flow_with({"device": "dev_1", "kind": "motion"}, board="esp32"))
        self.assertEqual(e.fire_device_event("dev_1", "motion", 1), 0)

    def test_nothing_listening_is_not_an_error(self):
        e = engine({"flows": []})
        self.assertEqual(e.fire_device_event("dev_1", "motion", 1), 0)


class TestCommandingADevice(unittest.TestCase):
    def run_node(self, cfg, fleet=None, msg=None):
        fleet = fleet or FakeFleet()
        e = engine(fleet=fleet)
        flow = {"id": "f1", "name": "f1"}
        node = {"id": "n1", "type": "device.command", "config": cfg}
        port, out = e._execute(flow, node, msg or {"payload": 1, "meta": {}})
        return fleet, out, e

    def test_a_command_is_queued_for_the_named_device(self):
        fleet, out, _ = self.run_node({"device": "dev_1", "op": "camera-on",
                                       "frame_size": "QVGA", "format": "colour"})
        self.assertEqual(fleet.pushed[0][0], "dev_1")
        self.assertEqual(fleet.pushed[0][1]["op"], "camera-on")
        self.assertEqual(fleet.pushed[0][1]["frame_size"], "QVGA")
        self.assertIsNotNone(out)

    def test_the_device_is_named_in_the_meta_it_passes_on(self):
        _, out, _ = self.run_node({"device": "dev_1", "op": "reload"})
        self.assertEqual(out["meta"]["device"], "dev_1")

    def test_setting_a_pin_carries_the_pin_and_the_value(self):
        fleet, _, _ = self.run_node({"device": "dev_1", "op": "set",
                                     "gpio": 4, "value": "1"})
        self.assertEqual(fleet.pushed[0][1], {"op": "set", "gpio": 4, "value": 1})

    def test_a_pin_can_come_from_the_message(self):
        fleet, _, _ = self.run_node({"device": "dev_1", "op": "set",
                                     "gpio": "{{payload}}", "value": "0"},
                                    msg={"payload": 7, "meta": {}})
        self.assertEqual(fleet.pushed[0][1]["gpio"], 7)

    def test_fire_without_a_node_does_not_queue_anything(self):
        fleet, out, _ = self.run_node({"device": "dev_1", "op": "fire", "node": ""})
        self.assertEqual(fleet.pushed, [])
        self.assertIsNone(out)

    def test_no_device_does_not_queue_anything(self):
        fleet, out, _ = self.run_node({"device": "", "op": "reload"})
        self.assertEqual(fleet.pushed, [])
        self.assertIsNone(out)

    def test_an_op_outside_the_whitelist_is_refused(self):
        """The field offers a fixed list; this is the belt to that's braces."""
        fleet, out, _ = self.run_node({"device": "dev_1", "op": "rm -rf"})
        self.assertEqual(fleet.pushed, [])
        self.assertIsNone(out)

    def test_it_says_so_rather_than_throwing_when_there_is_no_fleet(self):
        e = engine(fleet=None)
        port, out = e._execute({"id": "f1"}, {"id": "n1", "type": "device.command",
                                              "config": {"device": "d", "op": "reload"}},
                               {"payload": 1})
        self.assertIsNone(out)
        self.assertIn("fleet", e.recent()[-1]["message"])


class TestTheWhitelistHasOneHome(unittest.TestCase):
    def test_the_node_offers_exactly_the_ops_that_exist(self):
        spec = flows.REGISTRY["device.command"]
        op = [f for f in spec["fields"] if f["key"] == "op"][0]
        self.assertEqual(tuple(op["options"]), flows.DEVICE_OPS)

    def test_the_agent_implements_every_one_of_them(self):
        """If this fails, the console can send something no device understands."""
        path = os.path.join(ROOT, "zero2w_console", "agent", "agent.py")
        with open(path) as fh:
            agent = fh.read()
        for op in flows.DEVICE_OPS:
            with self.subTest(op=op):
                self.assertIn('== "%s"' % op, agent)


class TestGroups(unittest.TestCase):
    GROUPS = [{"name": "wheels", "devices": ["dev_1", "dev_2"]},
              {"name": "empty", "devices": []}]

    def test_a_command_to_a_group_reaches_every_member(self):
        fleet = FakeFleet(self.GROUPS)
        e = engine(fleet=fleet)
        node = {"id": "n", "type": "device.command",
                "config": {"device": "group:wheels", "op": "reboot"}}
        _p, out = e._execute({"id": "f", "name": "f"}, node, {"payload": 1, "meta": {}})
        self.assertEqual([d for d, _c in fleet.pushed], ["dev_1", "dev_2"])
        self.assertEqual(out["meta"]["devices"], ["dev_1", "dev_2"])
        self.assertEqual(out["meta"]["device"], "group:wheels")

    def test_group_names_ignore_case(self):
        fleet = FakeFleet(self.GROUPS)
        e = engine(fleet=fleet)
        node = {"id": "n", "type": "device.command",
                "config": {"device": "group:Wheels", "op": "reboot"}}
        e._execute({"id": "f", "name": "f"}, node, {"payload": 1, "meta": {}})
        self.assertEqual(len(fleet.pushed), 2)

    def test_an_empty_or_unknown_group_sends_nothing_and_stops(self):
        for name in ("group:empty", "group:nobody"):
            with self.subTest(group=name):
                fleet = FakeFleet(self.GROUPS)
                e = engine(fleet=fleet)
                node = {"id": "n", "type": "device.command",
                        "config": {"device": name, "op": "reboot"}}
                _p, out = e._execute({"id": "f", "name": "f"}, node,
                                     {"payload": 1, "meta": {}})
                self.assertIsNone(out)
                self.assertEqual(fleet.pushed, [])

    def test_an_event_from_any_member_fires_a_group_trigger(self):
        e = engine(flow_with({"device": "group:wheels", "kind": ""}),
                   fleet=FakeFleet(self.GROUPS))
        self.assertEqual(e.fire_device_event("dev_2", "bump", 1), 1)
        self.assertEqual(e.fire_device_event("dev_9", "bump", 1), 0)


if __name__ == "__main__":
    unittest.main()
