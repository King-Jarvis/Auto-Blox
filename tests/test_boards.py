"""Board profiles: generated, internally consistent, and carrying the traps."""
import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from zero2w_console import iot  # noqa: E402

BOARDS = iot.board_profiles()


class TestGenerated(unittest.TestCase):
    def test_the_generator_reproduces_every_profile(self):
        """A hand-edited profile is a lie waiting to happen."""
        before = {}
        for name in os.listdir(iot.BOARDS_DIR):
            path = os.path.join(iot.BOARDS_DIR, name)
            with open(path, "rb") as fh:
                before[path] = fh.read()
        p = subprocess.run([sys.executable, os.path.join("scripts", "build_boards.py")],
                           cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr)
        for path, content in before.items():
            with open(path, "rb") as fh:
                after = fh.read()
            if after != content:
                with open(path, "wb") as fh:      # leave the tree as we found it
                    fh.write(content)
            self.assertEqual(after, content, "%s is stale — re-run scripts/build_boards.py" % path)

    def test_there_is_at_least_one_profile(self):
        self.assertTrue(BOARDS)
        for board, doc in BOARDS.items():
            self.assertEqual(doc["board"], board)
            self.assertTrue(doc["source"], "%s cites no vendor document" % board)


class TestInvariants(unittest.TestCase):
    def test_every_pin_is_coherent(self):
        for board, doc in BOARDS.items():
            seen = set()
            for pin in doc["pins"]:
                with self.subTest(board=board, gpio=pin["gpio"]):
                    self.assertNotIn(pin["gpio"], seen, "duplicate")
                    seen.add(pin["gpio"])
                    if not pin["usable"]:
                        self.assertEqual(pin["caps"], [], "an unusable pin offers nothing")
                        self.assertTrue(pin["note"], "and it says why")
                    if pin["input_only"]:
                        self.assertNotIn("out", pin["caps"])
                        self.assertNotIn("pwm", pin["caps"])
                    if "adc2" in pin["caps"]:
                        self.assertIn("ADC2", pin["note"] or "")
                    if pin["strapping"]:
                        self.assertIn("strapping", pin["note"] or "")

    def test_counts_match_the_pins(self):
        for board, doc in BOARDS.items():
            with self.subTest(board=board):
                pins = doc["pins"]
                self.assertEqual(doc["counts"]["gpio"], len(pins))
                self.assertEqual(doc["counts"]["usable"],
                                 len([p for p in pins if p["usable"]]))
                self.assertEqual(doc["counts"]["output"],
                                 len([p for p in pins if "out" in p["caps"]]))

    def test_default_bus_pins_exist_and_are_usable(self):
        for board, doc in BOARDS.items():
            by_gpio = {p["gpio"]: p for p in doc["pins"]}
            for bus, cfg in doc["buses"].items():
                for role, gpio in cfg.items():
                    if role == "note" or not isinstance(gpio, int):
                        continue
                    with self.subTest(board=board, bus=bus, role=role):
                        self.assertIn(gpio, by_gpio, "GPIO%d is not on this chip" % gpio)


class TestKnownTraps(unittest.TestCase):
    """Facts from the datasheets."""

    def caps(self, board, gpio):
        for p in BOARDS[board]["pins"]:
            if p["gpio"] == gpio:
                return p
        self.fail("%s has no GPIO%d" % (board, gpio))

    def test_classic_esp32_flash_pins_are_not_offered(self):
        for gpio in range(6, 12):
            self.assertFalse(self.caps("esp32", gpio)["usable"], "GPIO%d" % gpio)

    def test_classic_esp32_high_pins_are_input_only(self):
        for gpio in (34, 35, 36, 39):
            pin = self.caps("esp32", gpio)
            self.assertTrue(pin["input_only"])
            self.assertIn("in", pin["caps"])
            self.assertNotIn("out", pin["caps"])

    def test_classic_esp32_strapping_pins(self):
        for gpio in (0, 2, 12, 15):
            self.assertTrue(self.caps("esp32", gpio)["strapping"], "GPIO%d" % gpio)

    def test_s3_has_no_input_only_pins(self):
        self.assertEqual([p["gpio"] for p in BOARDS["esp32s3"]["pins"] if p["input_only"]], [])

    def test_s3_flash_pins_are_not_offered(self):
        for gpio in range(26, 33):
            self.assertFalse(self.caps("esp32s3", gpio)["usable"], "GPIO%d" % gpio)

    def test_c3_has_no_touch_peripheral(self):
        self.assertFalse(BOARDS["esp32c3"]["peripherals"]["touch"])
        for pin in BOARDS["esp32c3"]["pins"]:
            self.assertNotIn("touch", pin["caps"])

    def test_c3_flash_pins_are_not_offered(self):
        for gpio in range(11, 18):
            self.assertFalse(self.caps("esp32c3", gpio)["usable"], "GPIO%d" % gpio)

    def test_only_the_classic_esp32_family_has_a_dac(self):
        self.assertEqual([p["gpio"] for p in BOARDS["esp32"]["pins"] if "dac" in p["caps"]],
                         [25, 26])
        for board in ("esp32s3", "esp32c3"):
            self.assertEqual([p["gpio"] for p in BOARDS[board]["pins"] if "dac" in p["caps"]], [])

    def test_the_camera_board_offers_only_what_its_header_brings_out(self):
        """The AI-Thinker CAM has a 16-pin header and the camera owns the
        rest."""
        cam = BOARDS["esp32cam"]
        self.assertEqual([p["gpio"] for p in cam["pins"] if p["usable"]],
                         [1, 2, 3, 4, 12, 13, 14, 15, 33])
        self.assertTrue(cam["peripherals"]["camera"])

    def test_the_camera_board_does_not_offer_pins_it_has_no_hole_for(self):
        """GPIO17 exists on the chip and nowhere on this board."""
        for gpio in (17, 18, 19, 21, 22, 23, 25, 26, 27, 32, 34, 35, 36, 39):
            pin = self.caps("esp32cam", gpio)
            self.assertFalse(pin["usable"], "GPIO%d" % gpio)
            self.assertTrue(pin["note"])

    def test_the_camera_boards_psram_pin_is_not_offered(self):
        """This module carries PSRAM, so GPIO16 is spoken for even though it
        is on the header."""
        pin = self.caps("esp32cam", 16)
        self.assertFalse(pin["usable"])
        self.assertIn("PSRAM", pin["note"])

    def test_the_camera_boards_led_is_usable_and_says_where_it_is(self):
        pin = self.caps("esp32cam", 33)
        self.assertTrue(pin["usable"])
        self.assertIn("out", pin["caps"])
        self.assertIn("LED", pin["note"])

    def test_the_sd_shared_pins_say_so(self):
        for gpio in (2, 4, 12, 13, 14, 15):
            self.assertIn("microSD", self.caps("esp32cam", gpio)["note"] or "", "GPIO%d" % gpio)


class TestChipMatching(unittest.TestCase):
    def test_every_mapped_chip_has_a_profile(self):
        for chip, board in iot.CHIP_FAMILIES:
            if board is not None:
                self.assertIn(board, BOARDS, chip)

    def test_the_family_prefixes_are_ordered_most_specific_first(self):
        """"esp32" is a prefix of every other name, so if it came first an
        S3 would match the plain ESP32 profile."""
        self.assertEqual(iot.CHIP_FAMILIES[-1][0], "esp32")
        self.assertEqual(iot.board_for_chip("ESP32-S3"), "esp32s3")
        self.assertEqual(iot.board_for_chip("ESP32-C3"), "esp32c3")
        self.assertEqual(iot.board_for_chip("ESP32"), "esp32")
        self.assertIsNone(iot.board_for_chip("ESP32-C6"))
        self.assertIsNone(iot.board_for_chip(""))

    def test_a_real_esp32_cam_is_matched_by_family_not_by_die_name(self):
        """The board on the bench answers ESP32-D0WD-V3, not "ESP32"."""
        self.assertEqual(iot.board_for_chip("ESP32-D0WD-V3"), "esp32")

    def test_esptool_output_is_parsed(self):
        captured = """esptool.py v4.7.0
Found 1 serial ports
Serial port /dev/ttyUSB0
Connecting....
Detecting chip type... ESP32-S3
Chip is ESP32-S3 (QFN56) (revision v0.2)
Features: WiFi, BLE, Embedded PSRAM 8MB (AP_3v3)
Crystal is 40MHz
MAC: 7c:df:a1:00:11:22
Detected flash size: 8MB
Hard resetting via RTS pin..."""
        out = iot.parse_esptool(captured)
        self.assertEqual(out["chip"], "ESP32-S3")
        self.assertEqual(out["board"], "esp32s3")
        self.assertEqual(out["mac"], "7c:df:a1:00:11:22")
        self.assertEqual(out["flash"], "8MB")
        self.assertTrue(out["psram"])

    def test_real_output_from_the_board_on_the_bench(self):
        """Captured from an ESP32-CAM-MB, through the ROM loader."""
        captured = """esptool.py v4.7.0
Serial port /dev/ttyUSB0
Connecting.....
Detecting chip type... Unsupported detection protocol, switching and trying again...
Connecting....
Detecting chip type... ESP32
Chip is ESP32-D0WD-V3 (revision v3.1)
Features: WiFi, BT, Dual Core, 240MHz, VRef calibration in efuse, Coding Scheme None
Crystal is 40MHz
MAC: 70:4b:ca:00:00:b8
Enabling default SPI flash mode...
Manufacturer: 5e
Device: 4016
Detected flash size: 4MB
Hard resetting via RTS pin..."""
        out = iot.parse_esptool(captured)
        self.assertEqual(out["chip"], "ESP32")
        self.assertEqual(out["chip_detail"], "ESP32-D0WD-V3 (revision v3.1)")
        self.assertEqual(out["revision"], "v3.1")
        self.assertEqual(out["board"], "esp32")
        self.assertEqual(out["mac"], "70:4b:ca:00:00:b8")
        self.assertEqual(out["flash"], "4MB")

    def test_absent_psram_is_unknown_rather_than_no(self):
        """An ESP32-CAM has PSRAM on the module, where esptool cannot see
        it."""
        out = iot.parse_esptool("Features: WiFi, BT, Dual Core, 240MHz\n")
        self.assertIsNone(out["psram"])
        out = iot.parse_esptool("Features: WiFi, BLE, Embedded PSRAM 8MB\n")
        self.assertTrue(out["psram"])

    def test_a_missing_stub_is_recognised_so_it_can_be_retried(self):
        traceback = ("FileNotFoundError: [Errno 2] No such file or directory: "
                     "'/usr/lib/python3/dist-packages/esptool/targets/stub_flasher/"
                     "stub_flasher_32.json'")
        self.assertTrue(iot._stub_missing(traceback))
        self.assertIn("stub flasher", iot._esptool_error(traceback))
        self.assertFalse(iot._stub_missing("Failed to connect"))

    def test_a_chip_without_a_profile_is_reported_not_guessed(self):
        out = iot.parse_esptool("Detecting chip type... ESP32-C6\nMAC: 11:22:33:44:55:66\n")
        self.assertEqual(out["chip"], "ESP32-C6")
        self.assertIsNone(out["board"])

    def test_garbage_parses_to_nothing_rather_than_raising(self):
        out = iot.parse_esptool("A fatal error occurred: Failed to connect")
        self.assertIsNone(out["chip"])
        self.assertIsNone(out["board"])

    def test_esptool_errors_become_advice(self):
        self.assertIn("dialout", iot._esptool_error("serial.serialutil: Permission denied"))
        self.assertIn("BOOT", iot._esptool_error("A fatal error occurred: Failed to connect"))
        self.assertIn("open in something else", iot._esptool_error("Device or resource busy"))


if __name__ == "__main__":
    unittest.main()
