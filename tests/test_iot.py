"""The IOT module reports honestly and never needs privilege to do it."""
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from zero2w_console import iot  # noqa: E402


class TestProbes(unittest.TestCase):
    def test_radio_probe_never_raises(self):
        r = iot.radio()
        self.assertIn("interfaces", r)
        self.assertIn("phys", r)
        self.assertIn("rfkill", r)

    def test_rfkill_entries_are_booleans(self):
        for entry in iot.rfkill():
            self.assertIsInstance(entry["soft_blocked"], bool)
            self.assertIsInstance(entry["hard_blocked"], bool)

    def test_ap_state_lists_what_is_missing(self):
        st = iot.ap_state()
        self.assertIsInstance(st["up"], bool)
        self.assertIsInstance(st["missing"], list)
        # Nothing may claim to be up while something is still missing.
        if st["missing"]:
            self.assertFalse(st["up"])

    def test_only_non_overlapping_24ghz_channels_are_offered(self):
        """An ESP32 is 2.4 GHz only, so a 5 GHz channel would be invisible."""
        self.assertEqual(iot.AP_CHANNELS, [1, 6, 11])

    def test_capabilities_shape(self):
        caps = iot.capabilities(None)
        for key in ("radio", "ap", "devices"):
            self.assertIn(key, caps)
            self.assertIn("available", caps[key])
        # Serialisable, because it goes out as JSON.
        json.dumps(caps)

    def test_every_unavailable_capability_says_what_to_do(self):
        """Including the case where nothing is missing and it simply has not
        been started — a widget reading "down" with no explanation is the
        thing this convention exists to prevent."""
        caps = iot.capabilities(None)
        for key, cap in caps.items():
            if not cap["available"]:
                with self.subTest(capability=key):
                    self.assertTrue(cap.get("hint"), "%s is unavailable and silent" % key)

    def test_a_ready_but_stopped_ap_says_to_start_it(self):
        state = {"up": False, "missing": [], "configured": True}
        self.assertIn("Start", iot._ap_hint(state, helper_ok=True, hostapd_ok=True) or "")

    def test_a_missing_piece_outranks_the_start_hint(self):
        state = {"up": False, "missing": ["hostapd is not installed"], "configured": True}
        self.assertIn("apt install", iot._ap_hint(state, helper_ok=True, hostapd_ok=False) or "")


class TestNetworkValidation(unittest.TestCase):
    """The console validates before calling the helper, purely so the person
    gets a readable error."""

    GOOD = {"ssid": "zero2w-iot", "psk": "correct-horse", "channel": 6,
            "subnet": "10.42.0.0/24"}

    def test_good(self):
        out = iot.validate_net_config(dict(self.GOOD))
        self.assertEqual(out["channel"], 6)
        self.assertTrue(out["isolate"])
        self.assertFalse(out["lan_access"])

    def _refused(self, **over):
        cfg = dict(self.GOOD)
        cfg.update(over)
        with self.assertRaises(iot.NetError):
            iot.validate_net_config(cfg)

    def test_refusals_match_the_helper(self):
        self._refused(ssid="")
        self._refused(ssid="a" * 33)
        self._refused(ssid="net\nignore_broadcast_ssid=1")
        self._refused(psk="short")
        self._refused(channel=36)
        self._refused(channel=None)
        self._refused(subnet="8.8.8.0/24")
        self._refused(subnet="10.42.0.0/30")

    @unittest.skipIf(os.path.exists(iot.HELPER), "the helper is installed here")
    def test_helper_absent_is_a_readable_error(self):
        with self.assertRaises(iot.NetError) as caught:
            iot.helper("status")
        self.assertIn("helper", str(caught.exception))

    def test_unknown_verbs_never_reach_sudo(self):
        with self.assertRaises(iot.NetError):
            iot.helper("rm-rf")


class TestRadioSwitch(unittest.TestCase):
    """Turning the radio on and off, without disturbing it."""

    def test_the_event_matches_struct_rfkill_event(self):
        import struct
        # __u32 idx, __u8 type, __u8 op, __u8 soft, __u8 hard — 8 bytes, packed.
        packed = struct.pack("<IBBBB", 0, iot.RFKILL_TYPE_WLAN,
                             iot.RFKILL_OP_CHANGE_ALL, 1, 0)
        self.assertEqual(len(packed), 8)
        self.assertEqual(packed, b"\x00\x00\x00\x00\x01\x03\x01\x00")

    def test_wlan_and_change_all_are_the_kernel_constants(self):
        self.assertEqual(iot.RFKILL_TYPE_WLAN, 1)
        self.assertEqual(iot.RFKILL_OP_CHANGE_ALL, 3)

    def test_radio_writable_answers_without_writing(self):
        before = iot.rfkill()
        self.assertIsInstance(iot.radio_writable(), bool)
        self.assertEqual(iot.rfkill(), before, "probing must not change the block")

    def test_both_radio_verbs_are_in_the_helper_whitelist(self):
        """Checked against the constant, not by calling: invoking radio-off
        for real on a machine where the helper is installed would take
        the radio down mid-test."""
        for verb in ("radio-on", "radio-off"):
            self.assertIn(verb, iot.HELPER_VERBS)


class TestDeviceStore(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = iot.DeviceStore(os.path.join(self.dir.name, "iot.json"))

    def tearDown(self):
        self.dir.cleanup()

    def test_empty_store(self):
        self.assertEqual(self.store.load(), {"devices": [], "enrollment": None})

    def test_round_trip_and_permissions(self):
        self.store.save({"devices": [{"id": "d1", "board": "esp32s3"}], "enrollment": None})
        self.assertEqual(len(self.store.load()["devices"]), 1)
        self.assertEqual(os.stat(self.store.path).st_mode & 0o777, 0o600)

    def test_corrupt_file_reads_as_empty(self):
        with open(self.store.path, "w") as fh:
            fh.write("]not json[")
        self.assertEqual(self.store.load()["devices"], [])

    def test_create_update_delete(self):
        d = iot.create_device(self.store, {"name": "Bench", "board": "esp32s3",
                                           "chip": "ESP32-S3", "mac": "aa:bb:cc:dd:ee:01"})
        self.assertTrue(d["id"].startswith("dev_"))
        self.assertIsNone(d["flow"], "a new config is deployed nothing")
        self.assertIsNone(d["enrolled"])
        self.assertEqual(iot.update_device(self.store, d["id"], {"name": "Bench 2"})["name"],
                         "Bench 2")
        iot.delete_device(self.store, d["id"])
        self.assertEqual(iot.devices(self.store), [])

    def test_a_config_needs_a_name_and_a_real_profile(self):
        with self.assertRaises(iot.NetError):
            iot.create_device(self.store, {"name": "  "})
        with self.assertRaises(iot.NetError):
            iot.create_device(self.store, {"name": "x", "board": "esp32s9"})

    def test_one_config_per_board(self):
        iot.create_device(self.store, {"name": "A", "mac": "aa:bb:cc:dd:ee:02"})
        with self.assertRaises(iot.NetError):
            iot.create_device(self.store, {"name": "B", "mac": "aa:bb:cc:dd:ee:02"})
        with self.assertRaises(iot.NetError):
            iot.create_device(self.store, {"name": "A"})

    def test_only_editable_fields_can_be_written(self):
        """A device's identity comes from the scan and its state from itself
        — neither is something a form may overwrite."""
        d = iot.create_device(self.store, {"name": "Bench", "chip": "ESP32-S3"})
        iot.update_device(self.store, d["id"], {
            "name": "Bench", "chip": "NOT A CHIP", "token": "stolen",
            "enrolled": 1, "last_seen": 999, "id": "dev_other"})
        after = iot.get_device(self.store, d["id"])
        self.assertEqual(after["chip"], "ESP32-S3")
        self.assertIsNone(after["token"])
        self.assertIsNone(after["enrolled"])
        self.assertEqual(after["id"], d["id"])

    def test_updating_an_unknown_device_is_an_error(self):
        with self.assertRaises(iot.NetError):
            iot.update_device(self.store, "dev_nope", {"name": "x"})
        with self.assertRaises(iot.NetError):
            iot.delete_device(self.store, "dev_nope")

    def test_secrets_never_leave_the_store(self):
        secret = {"id": "d1", "board": "esp32s3", "token": "s3cret", "psk": "hunter2",
                  "enroll_token": "one-time", "ip": "10.42.0.9"}
        self.store.save({"devices": [secret], "enrollment": None})
        out = iot.devices(self.store)
        self.assertEqual(out[0]["ip"], "10.42.0.9")
        for key in iot.SECRET_KEYS:
            self.assertNotIn(key, out[0])
        # and nothing secret survives a round trip through JSON either
        self.assertNotIn("s3cret", json.dumps(out))
        self.assertNotIn("one-time", json.dumps(out))


if __name__ == "__main__":
    unittest.main()


class TestTheCrystalWarning(unittest.TestCase):
    """esptool times its own stub against the crystal it thinks is fitted."""

    BAD = ("Chip is ESP32-D0WD (revision v1.0)\n"
           "Features: WiFi, BT, Dual Core, 240MHz, VRef calibration in efuse\n"
           "WARNING: Detected crystal freq 15.44MHz is quite different to "
           "normalized freq 26MHz. Unsupported crystal in use?\n"
           "Crystal is 26MHz\n"
           "MAC: 98:f4:ab:00:00:10\n"
           "Detected flash size: 4MB\n")

    GOOD = ("Chip is ESP32-D0WD (revision v1.0)\n"
            "Features: WiFi, BT, Dual Core, 240MHz, VRef calibration in efuse\n"
            "Crystal is 40MHz\n"
            "MAC: 98:f4:ab:00:00:10\n"
            "Detected flash size: 4MB\n")

    def test_the_warning_is_carried_out_of_the_output(self):
        info = iot.parse_esptool(self.BAD)
        self.assertIn("15.44MHz", info["crystal_warning"])
        self.assertEqual(info["crystal"], "26MHz")

    def test_a_clean_read_carries_none(self):
        info = iot.parse_esptool(self.GOOD)
        self.assertIsNone(info["crystal_warning"])
        self.assertEqual(info["crystal"], "40MHz")

    def test_everything_else_is_still_parsed_from_the_bad_run(self):
        """The warning must not swallow the identity — the board is still
        recognisable, it is only the write that is unsafe."""
        info = iot.parse_esptool(self.BAD)
        self.assertEqual(info["mac"], "98:f4:ab:00:00:10")
        self.assertEqual(info["flash"], "4MB")
        self.assertEqual(info["chip_detail"], "ESP32-D0WD (revision v1.0)")

    def test_the_flasher_refuses_to_write_on_it(self):
        """A guard that can be overridden, because a board genuinely fitted
        with another crystal exists — but not one that is silent by
        default."""
        path = os.path.join(ROOT, "scripts", "iot-flash.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn('info.get("crystal_warning")', src)
        self.assertIn("--any-crystal", src)
        # And it has to come before anything is erased or written.
        guard = src.index('info.get("crystal_warning")')
        for later in ("erase_flash", "write_flash"):
            self.assertLess(guard, src.index('"%s"' % later),
                            "the guard is after the %s" % later)
