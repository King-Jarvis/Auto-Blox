"""Device groups in the store, and {{dev.<device>.x}} for any device."""
import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from zero2w_console import flows, iot                         # noqa: E402


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = iot.DeviceStore(os.path.join(self.dir.name, "iot.json"))
        self.store.save({"devices": [{"id": "dev_1", "name": "ESP32 Motor"},
                                     {"id": "dev_2", "name": "ESP32-Cam"}],
                         "enrollment": None})

    def tearDown(self):
        self.dir.cleanup()


class TestGroupsInTheStore(StoreCase):
    def test_saved_and_read_back(self):
        iot.save_groups(self.store, [{"name": "wheels", "devices": ["dev_1", "dev_1"]}])
        self.assertEqual(iot.groups(self.store),
                         [{"name": "wheels", "devices": ["dev_1"]}])

    def test_names_are_checked(self):
        for bad in ("", "2fast", "has space", "x" * 33):
            with self.subTest(name=bad):
                with self.assertRaises(iot.NetError):
                    iot.save_groups(self.store, [{"name": bad, "devices": []}])

    def test_two_groups_cannot_share_a_name(self):
        with self.assertRaises(iot.NetError):
            iot.save_groups(self.store, [{"name": "a", "devices": []},
                                         {"name": "A", "devices": []}])

    def test_a_member_must_be_a_device(self):
        with self.assertRaises(iot.NetError):
            iot.save_groups(self.store, [{"name": "a", "devices": ["dev_9"]}])

    def test_deleting_a_device_takes_it_out_of_its_groups(self):
        iot.save_groups(self.store, [{"name": "all", "devices": ["dev_1", "dev_2"]}])
        iot.delete_device(self.store, "dev_1")
        self.assertEqual(iot.groups(self.store)[0]["devices"], ["dev_2"])

    def test_targets(self):
        doc = {"groups": [{"name": "wheels", "devices": ["dev_1", "dev_2"]}]}
        self.assertEqual(iot.targets(doc, "dev_1"), ["dev_1"])
        self.assertEqual(iot.targets(doc, "group:wheels"), ["dev_1", "dev_2"])
        self.assertEqual(iot.targets(doc, "group:nope"), [])
        self.assertEqual(iot.targets(doc, ""), [])

    def test_the_server_routes_are_there(self):
        with open(os.path.join(ROOT, "zero2w_console", "server.py")) as fh:
            src = fh.read()
        self.assertIn('if route == "/api/iot/groups":', src)
        self.assertIn('"groups": iotmod.groups(self.server.devices)', src)


DEVICES = [{"id": "dev_1", "name": "ESP32 Motor", "flow": "flow_x",
            "last_seen": time.time(),
            "state": {"rssi": -61, "free": 88000, "uptime": 120,
                      "pins": {"27": {"dir": "pwm", "value": 42}}}},
           {"id": "dev_2", "name": "ESP32-Cam", "last_seen": time.time() - 999}]


class TestAnyDeviceAsAVariable(unittest.TestCase):
    def say(self, text):
        return flows.render(text, {}, {"devices": lambda: DEVICES})

    def test_by_name_key_or_by_id(self):
        self.assertEqual(self.say("{{dev.esp32_motor.rssi}}"), "-61")
        self.assertEqual(self.say("{{dev.dev_1.rssi}}"), "-61")

    def test_the_name_key(self):
        self.assertEqual(flows.device_key({"name": "ESP32 Motor"}), "esp32_motor")
        self.assertEqual(flows.device_key({"name": "ESP32-Cam"}), "esp32_cam")

    def test_the_fields(self):
        self.assertEqual(self.say("{{dev.esp32_motor.online}}"), "1")
        self.assertEqual(self.say("{{dev.esp32_cam.online}}"), "0")
        self.assertEqual(self.say("{{dev.esp32_motor.flow}}"), "flow_x")
        self.assertEqual(self.say("{{dev.esp32_motor.pin.27}}"), "42")
        self.assertEqual(self.say("{{dev.esp32_motor.free}}"), "88000")

    def test_a_device_that_does_not_exist_stays_visible(self):
        self.assertEqual(self.say("{{dev.nobody.rssi}}"), "{{dev.nobody.rssi}}")

    def test_a_formula_can_use_it(self):
        from zero2w_console.agent.modules import expr
        text = self.say("{{dev.esp32_motor.rssi}} + 100")
        self.assertEqual(expr.evaluate(text), 39)

    def test_the_picker_lists_every_device_with_its_value(self):
        rows = flows.variables_with_values(devices=DEVICES)
        mine = {r["name"]: r["value"] for r in rows if r["group"] == "Other devices"}
        self.assertEqual(mine["dev.esp32_motor.rssi"], "-61")
        self.assertIn("dev.esp32_cam.online", mine)

    def test_the_engine_reads_the_fleet_only_when_asked(self):
        e = flows.FlowEngine(flows.FlowStore(os.devnull), None, None, None)
        self.assertEqual(e._fleet_devices(), [])
        ctx = e._ctx({"id": "f"}, {"id": "n", "type": "log.write"})
        self.assertTrue(callable(ctx["devices"]))


if __name__ == "__main__":
    unittest.main()
