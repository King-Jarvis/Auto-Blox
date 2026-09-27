"""Moving a device config onto replacement hardware, and flashing firmware that
uses the new board's PSRAM."""
import importlib.util
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from zero2w_console import fleet as fleetmod, iot                # noqa: E402


def flasher():
    spec = importlib.util.spec_from_file_location(
        "flasher_upgrade", os.path.join(ROOT, "scripts", "iot-flash.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = iot.DeviceStore(os.path.join(self.dir.name, "iot.json"))
        self.store.save({"enrollment": None, "devices": [
            {"id": "dev_motor", "name": "ESP32 Motor", "board": "esp32",
             "mac": "98:f4:ab:00:00:10", "chip": "ESP32-D0WD", "psram": None,
             "flow": "flow_drive", "link": False, "txpower": 8,
             "token": "secret", "enrolled": 1, "flashed": 1,
             "probe": {"agent": "0.9.1"}, "state": {"agent": "0.9.1"}},
            {"id": "dev_cam", "name": "ESP32-Cam", "board": "esp32cam",
             "mac": "70:4b:ca:00:00:b8"}]})

    def tearDown(self):
        self.dir.cleanup()

    def motor(self):
        return [d for d in self.store.load()["devices"] if d["id"] == "dev_motor"][0]


class TestPsram(StoreCase):
    def test_what_can_be_said(self):
        self.assertIsNone(iot.psram_kind(None))
        self.assertIsNone(iot.psram_kind(""))
        self.assertIsNone(iot.psram_kind("none"))
        self.assertEqual(iot.psram_kind(True), "quad", "a scan's yes is quad")
        self.assertEqual(iot.psram_kind("octal"), "octal")
        with self.assertRaises(iot.NetError):
            iot.psram_kind("lots")

    def test_it_is_a_setting_on_the_config(self):
        iot.update_device(self.store, "dev_motor", {"psram": "quad"})
        self.assertEqual(self.motor()["psram"], "quad")
        iot.update_device(self.store, "dev_motor", {"psram": None})
        self.assertIsNone(self.motor()["psram"])


class TestTheFirmwareFollowsIt(unittest.TestCase):
    def test_which_build(self):
        v = flasher().psram_variant
        self.assertIsNone(v("esp32", None))
        self.assertEqual(v("esp32", "quad"), "SPIRAM")
        self.assertIsNone(v("esp32s3", "quad"), "the plain S3 build handles quad")
        self.assertEqual(v("esp32s3", "octal"), "SPIRAM_OCT")
        self.assertIsNone(v("esp32c3", "quad"), "a C3 has no PSRAM build")

    def test_a_wrong_mac_points_at_the_way_to_move_a_config(self):
        with open(os.path.join(ROOT, "scripts", "iot-flash.py")) as fh:
            self.assertIn("Move a config here", fh.read())


class TestMovingAConfig(StoreCase):
    NEW = {"mac": "24:0a:c4:11:22:33", "chip": "ESP32-D0WD-V3",
           "flash": "4MB", "port": "/dev/ttyUSB0", "psram": None, "board": "esp32"}

    def test_the_identity_is_the_new_board_s_and_the_rest_stays(self):
        moved = iot.rehome_device(self.store, "dev_motor", dict(self.NEW))
        self.assertEqual(moved["mac"], "24:0a:c4:11:22:33")
        self.assertEqual(moved["previous_mac"], "98:f4:ab:00:00:10")
        self.assertEqual((moved["name"], moved["flow"], moved["txpower"]),
                         ("ESP32 Motor", "flow_drive", 8))

    def test_what_the_old_board_held_is_void(self):
        iot.rehome_device(self.store, "dev_motor", dict(self.NEW))
        m = self.motor()
        for key in ("token", "enrolled", "flashed", "probe", "state"):
            with self.subTest(key=key):
                self.assertIsNone(m[key])

    def test_a_hand_set_psram_survives_a_scan_that_cannot_see_it(self):
        iot.update_device(self.store, "dev_motor", {"psram": "quad"})
        iot.rehome_device(self.store, "dev_motor", dict(self.NEW))
        self.assertEqual(self.motor()["psram"], "quad")

    def test_a_scan_that_sees_psram_says_so(self):
        iot.rehome_device(self.store, "dev_motor", dict(self.NEW, psram=True))
        self.assertEqual(self.motor()["psram"], "quad")

    def test_a_board_another_config_owns_is_refused(self):
        with self.assertRaises(iot.NetError):
            iot.rehome_device(self.store, "dev_motor",
                              dict(self.NEW, mac="70:4b:ca:00:00:b8"))

    def test_the_same_board_is_not_a_move(self):
        with self.assertRaises(iot.NetError):
            iot.rehome_device(self.store, "dev_motor",
                              dict(self.NEW, mac="98:f4:ab:00:00:10"))

    def test_no_scan_no_move(self):
        with self.assertRaises(iot.NetError):
            iot.rehome_device(self.store, "dev_motor", {"board": "esp32"})


class TestWhatWillNotCarryOver(unittest.TestCase):
    def warnings(self, board, flow_board):
        f = fleetmod.Fleet(None, None, None)
        flow = {"id": "flow_drive", "name": "Motor drive", "board": flow_board,
                "nodes": [{"type": "pwm.out", "config": {"gpio": g}}
                          for g in (27, 25, 33, 32)]}
        f.flow_for = lambda device: flow
        return f.rehome_warnings({"board": board})

    def test_a_wrover_is_a_classic_esp32_and_nothing_changes(self):
        self.assertEqual(self.warnings("esp32", "esp32"), [])

    def test_an_s3_says_the_flow_and_the_pins_it_lacks(self):
        said = " ".join(self.warnings("esp32s3", "esp32"))
        self.assertIn("written for esp32", said)
        self.assertIn("GPIO 25", said)


class TestTheScreenOffersIt(unittest.TestCase):
    def test_the_route_and_the_controls(self):
        with open(os.path.join(ROOT, "zero2w_console", "server.py")) as fh:
            self.assertIn('if action == "rehome":', fh.read())
        with open(os.path.join(ROOT, "zero2w_console", "static", "iot.js")) as fh:
            js = fh.read()
        self.assertIn('"/rehome"', js)
        self.assertIn('"Move a config here"', js)
        self.assertIn('{ psram: ps.value || null }', js)


if __name__ == "__main__":
    unittest.main()
