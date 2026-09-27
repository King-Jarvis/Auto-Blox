"""The parts of the BLE pad probe that can be tested without a radio."""
import os
import sys
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

PROBE = os.path.join(ROOT, "scripts", "ble_probe_device.py")


def load_probe():
    """The device probe, with the board-only modules stood up."""
    ble = types.ModuleType("bluetooth")
    ble.BLE = object
    sys.modules["bluetooth"] = ble
    binascii = types.ModuleType("ubinascii")
    import binascii as real
    binascii.hexlify = real.hexlify
    sys.modules["ubinascii"] = binascii
    mod = types.ModuleType("ble_probe_under_test")
    with open(PROBE, encoding="utf-8") as fh:
        code = fh.read()
    exec(compile(code, PROBE, "exec"), mod.__dict__)      # noqa: S102
    return mod


def ad(*fields):
    """Build an advertisement payload from (type, bytes) pairs."""
    out = bytearray()
    for kind, body in fields:
        out.append(len(body) + 1)
        out.append(kind)
        out.extend(body)
    return bytes(out)


class ProbeCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.probe = load_probe()

    @classmethod
    def tearDownClass(cls):
        for name in ("bluetooth", "ubinascii"):
            sys.modules.pop(name, None)


class TestReadingAnAdvertisement(ProbeCase):
    def test_a_complete_local_name_is_found(self):
        payload = ad((0x09, b"Xbox Wireless Controller"))
        self.assertEqual(self.probe.ad_name(payload), "Xbox Wireless Controller")

    def test_a_shortened_name_counts_too(self):
        self.assertEqual(self.probe.ad_name(ad((0x08, b"Xbox"))), "Xbox")

    def test_no_name_is_empty_rather_than_an_error(self):
        self.assertEqual(self.probe.ad_name(ad((0x01, b"\x06"))), "")

    def test_the_hid_service_is_recognised(self):
        """0x1812 little-endian in a 16-bit service class list."""
        payload = ad((0x01, b"\x06"), (0x03, b"\x12\x18"),
                     (0x09, b"Xbox Wireless Controller"))
        self.assertTrue(self.probe.ad_has_hid(payload))

    def test_an_incomplete_service_list_counts(self):
        self.assertTrue(self.probe.ad_has_hid(ad((0x02, b"\x12\x18"))))

    def test_hid_is_found_among_several_services(self):
        self.assertTrue(self.probe.ad_has_hid(
            ad((0x03, b"\x0f\x18" b"\x12\x18" b"\x0a\x18"))))

    def test_something_that_is_not_a_pad_is_not_claimed(self):
        for payload in (ad((0x03, b"\x0f\x18")),          # battery service
                        ad((0x09, b"Some Sensor")),
                        ad((0x03, b"\x18\x12")),           # byte-swapped, not HID
                        b""):
            with self.subTest(payload=payload):
                self.assertFalse(self.probe.ad_has_hid(payload))


class TestItSurvivesRubbishOffTheAir(ProbeCase):
    """Advertisement bytes come off a radio, so they can be anything at all."""

    def test_truncated_and_malformed_payloads(self):
        for payload in (b"", b"\x00", b"\x05", b"\x05\x09Xb",
                        b"\xff\x09short", b"\x03\x03\x12", b"\x02\x03\x12\x18",
                        bytes(range(40))):
            with self.subTest(payload=payload):
                self.probe.ad_name(payload)
                self.probe.ad_has_hid(payload)
                self.probe.ad_fields(payload)

    def test_a_zero_length_field_stops_the_walk(self):
        """A zero length is the "end of data" padding an advertiser may
        send; reading past it walks into whatever follows."""
        payload = ad((0x09, b"Xbox")) + b"\x00\x00\x00\x00"
        self.assertEqual(self.probe.ad_name(payload), "Xbox")

    def test_a_name_that_is_not_utf8_does_not_raise(self):
        self.assertTrue(self.probe.ad_name(ad((0x09, b"\xff\xfe pad"))))


class TestTheHostSideIsWiredUp(unittest.TestCase):
    def test_the_driver_pushes_the_device_probe(self):
        with open(os.path.join(ROOT, "scripts", "ble-pad-probe.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("ble_probe_device.py", src)
        self.assertIn("serialport", src,
                      "it should use the shared serial/REPL implementation")

    def test_the_repl_helper_is_the_shared_one(self):
        """`RawREPL` moved out of the flasher when this became its second
        caller, which is what serialport.py exists for."""
        from zero2w_console import serialport
        self.assertTrue(hasattr(serialport, "RawREPL"))
        self.assertTrue(hasattr(serialport, "VERIFY"))
        with open(os.path.join(ROOT, "scripts", "iot-flash.py"),
                  encoding="utf-8") as fh:
            flasher = fh.read()
        self.assertNotIn("class RawREPL", flasher,
                         "the flasher kept its own copy, so the two will drift")

    def test_both_probes_enter_through_the_boot(self):
        """A running agent catches Ctrl-C; only a reset reliably gets in."""
        for name in ("ble-pad-probe.py", "ble-peripheral.py"):
            with open(os.path.join(ROOT, "scripts", name), encoding="utf-8") as fh:
                with self.subTest(script=name):
                    self.assertIn("enter_through_boot()", fh.read())

    def test_entering_through_the_boot_resets_then_interrupts(self):
        from zero2w_console import serialport

        class FakeSerial:
            def __init__(self):
                self.log = []

            def reset_into_run(self):
                self.log.append("reset")

            def write(self, data):
                self.log.append(data)

            def read(self, n=4096):
                return b""

            def read_until(self, marker, timeout):
                return b"raw REPL; CTRL-B to exit"

        s = FakeSerial()
        self.assertTrue(serialport.RawREPL(s).enter_through_boot(seconds=0.2))
        self.assertEqual(s.log[0], "reset")
        self.assertIn(b"\x03", s.log[1:])
        self.assertEqual(s.log[-1], b"\r\x01", "it ends by asking for the raw REPL")


if __name__ == "__main__":
    unittest.main()
