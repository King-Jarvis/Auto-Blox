"""The host's gamepad reader, against synthetic kernel records.

Ranges and layout are read from the driver, never assumed.
"""
import os
import struct
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from zero2w_console import pad                             # noqa: E402
from zero2w_console.agent.modules import pad as padmod      # noqa: E402

DEVICES = """I: Bus=0019 Vendor=0000 Product=0000 Version=0000
N: Name="dw_hdmi"
P: Phys=
H: Handlers=kbd event4
B: EV=3

I: Bus=0005 Vendor=045e Product=0b13 Version=0517
N: Name="Xbox Wireless Controller"
P: Phys=c4:5f:ac:00:00:1f
H: Handlers=event5 js0
B: EV=100003

I: Bus=0003 Vendor=046d Product=c52b Version=0111
N: Name="Logitech Receiver"
P: Phys=usb-0000:00:14.0-1/input2
H: Handlers=sysrq kbd event6
B: EV=120013
"""


def reader(**ranges):
    r = pad.Reader(padmod.blank())
    r.ranges = ranges
    return r


def record(kind, code, value):
    return struct.pack(pad.EVENT, 0, 0, kind, code, value)


class TestFindingAPad(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
        self.tmp.write(DEVICES)
        self.tmp.close()
        self.addCleanup(os.unlink, self.tmp.name)
        self.old, pad.DEVICES = pad.DEVICES, self.tmp.name
        self.addCleanup(setattr, pad, "DEVICES", self.old)

    def test_only_the_device_with_a_js_handler_is_a_pad(self):
        """`joydev` binds to pads and nothing else, so that is the kernel's own
        answer — better than matching a name that varies by firmware."""
        self.assertEqual(pad.candidates(),
                         [("/dev/input/event5", "Xbox Wireless Controller")])

    def test_a_keyboard_is_not_a_pad_and_neither_is_hdmi(self):
        names = [name for _path, name in pad.candidates()]
        self.assertNotIn("dw_hdmi", names)
        self.assertNotIn("Logitech Receiver", names)

    def test_a_name_filter_that_matches_nothing_finds_nothing(self):
        self.assertEqual(pad.Reader(padmod.blank(), "dualshock").pick(),
                         ("", ""))

    def test_a_missing_devices_file_is_not_an_error(self):
        pad.DEVICES = "/nonexistent/input/devices"
        self.assertEqual(pad.candidates(), [])


class TestTheIoctlNumber(unittest.TestCase):
    def test_eviocgabs_matches_the_kernel_macro(self):
        """_IOR('E', 0x40 + abs, struct input_absinfo), 24 bytes. Getting this
        wrong does not fail loudly — it returns the wrong field."""
        self.assertEqual(pad.ABSINFO_SIZE, 24)
        self.assertEqual(pad._eviocgabs(0), 0x80184540)
        self.assertEqual(pad._eviocgabs(5), 0x80184545)

    def test_the_event_record_is_the_size_the_kernel_writes(self):
        self.assertEqual(pad.EVENT_SIZE, 24)


class TestScalingWhateverTheDriverReports(unittest.TestCase):
    def test_a_signed_stick(self):
        r = reader(lx=(-32768, 32767, 0))
        self.assertAlmostEqual(r.scale("lx", 32767), 1.0, places=3)
        self.assertAlmostEqual(r.scale("lx", -32768), -1.0, places=3)
        self.assertAlmostEqual(r.scale("lx", 0), 0.0, places=3)

    def test_an_unsigned_stick_centres_correctly(self):
        """The case that would pin a stick at an extreme if it were assumed
        signed."""
        r = reader(lx=(0, 65535, 0))
        self.assertAlmostEqual(r.scale("lx", 65535), 1.0, places=3)
        self.assertAlmostEqual(r.scale("lx", 0), -1.0, places=3)
        self.assertAlmostEqual(r.scale("lx", 32767), 0.0, places=2)

    def test_a_trigger_is_one_way_only(self):
        r = reader(rt=(0, 1023, 0))
        self.assertAlmostEqual(r.scale("rt", 0), 0.0, places=3)
        self.assertAlmostEqual(r.scale("rt", 1023), 1.0, places=3)
        self.assertGreaterEqual(r.scale("rt", 512), 0.0)

    def test_the_drivers_own_flat_zone_is_honoured(self):
        r = reader(lx=(-32768, 32767, 4000))
        self.assertEqual(r.scale("lx", 500), 0.0)
        self.assertNotEqual(r.scale("lx", 20000), 0.0)

    def test_a_dpad_needs_no_range_at_all(self):
        r = reader()
        self.assertEqual(r.scale("hx", 1), 1.0)
        self.assertEqual(r.scale("hx", -1), -1.0)
        self.assertEqual(r.scale("hx", 0), 0.0)

    def test_an_axis_with_no_range_reads_centred_rather_than_raising(self):
        self.assertEqual(reader().scale("lx", 12345), 0.0)


class TestWhichCodeMeansWhat(unittest.TestCase):
    """Sony swaps the right stick and the triggers. Read the standard way,
    `right trigger` is a stick resting at 0.53 — half throttle on arming."""

    def reader(self, vendor="", resting=None):
        r = pad.Reader(padmod.blank())
        r.path = "/dev/input/event9"
        r.vendor = lambda: vendor
        for key in ("lx", "ly", "rx", "ry", "lt", "rt"):
            r.ranges[key] = (0, 255, 15)
        r.choose_layout(resting or {})
        return r

    def test_the_default_is_the_common_layout(self):
        r = self.reader()
        self.assertEqual(r.layout, "standard")
        self.assertEqual(r.axes[0x02], "lt")
        self.assertEqual(r.axes[0x03], "rx")

    def test_a_sony_vendor_id_swaps_the_four(self):
        r = self.reader(vendor="054c")
        self.assertEqual(r.layout, "quirk:054c")
        self.assertEqual(r.axes[0x02], "rx")     # ABS_Z is the right stick
        self.assertEqual(r.axes[0x05], "ry")     # ABS_RZ too
        self.assertEqual(r.axes[0x03], "lt")     # and these are the triggers
        self.assertEqual(r.axes[0x04], "rt")

    def test_the_left_stick_and_dpad_are_never_in_doubt(self):
        for vendor in ("", "054c", "045e"):
            r = self.reader(vendor=vendor)
            with self.subTest(vendor=vendor):
                self.assertEqual(r.axes[0x00], "lx")
                self.assertEqual(r.axes[0x01], "ly")
                self.assertEqual(r.axes[0x10], "hx")
                self.assertEqual(r.axes[0x11], "hy")

    def test_an_unknown_pad_is_measured_instead_of_guessed(self):
        """A stick rests near the middle of its range, a trigger at the bottom.
        That needs no table."""
        r = self.reader(vendor="beef",
                        resting={0x02: 134, 0x05: 135, 0x03: 0, 0x04: 0})
        self.assertEqual(r.layout, "measured at rest")
        self.assertEqual(r.axes[0x02], "rx")
        self.assertEqual(r.axes[0x03], "lt")

    def test_the_other_way_round_is_measured_too(self):
        r = self.reader(vendor="beef",
                        resting={0x02: 0, 0x05: 0, 0x03: 128, 0x04: 128})
        self.assertEqual(r.layout, "measured at rest")
        self.assertEqual(r.axes[0x02], "lt")
        self.assertEqual(r.axes[0x03], "rx")

    def test_a_known_vendor_beats_the_measurement(self):
        """Its one weakness is somebody holding a trigger as the reader opens."""
        r = self.reader(vendor="054c",
                        resting={0x02: 0, 0x05: 0, 0x03: 128, 0x04: 128})
        self.assertEqual(r.layout, "quirk:054c")
        self.assertEqual(r.axes[0x03], "lt")

    def test_an_ambiguous_pad_keeps_the_common_layout(self):
        """Four centred axes says nothing, and inventing an answer from nothing
        is worse than using the usual one."""
        r = self.reader(vendor="beef",
                        resting={0x02: 128, 0x05: 128, 0x03: 128, 0x04: 128})
        self.assertEqual(r.layout, "standard")

    def test_a_trigger_reads_zero_at_rest_whichever_layout_it_is(self):
        """The assertion that matters: anything else is throttle nobody asked
        for."""
        for vendor, resting in (("054c", {}),
                                ("beef", {0x02: 134, 0x05: 135,
                                          0x03: 0, 0x04: 0})):
            r = self.reader(vendor=vendor, resting=resting)
            with self.subTest(vendor=vendor):
                self.assertEqual(r.scale("lt", 0), 0.0)
                self.assertEqual(r.scale("rt", 0), 0.0)


class TestFeedingRealRecords(unittest.TestCase):
    def setUp(self):
        self.r = reader(lx=(-32768, 32767, 0), ly=(-32768, 32767, 0),
                        rt=(0, 1023, 0))

    def feed(self, kind, code, value):
        self.r.feed(struct.unpack(pad.EVENT, record(kind, code, value)))

    def test_a_stick_lands_in_the_state(self):
        self.feed(pad.EV_ABS, 0x00, 16384)
        self.assertAlmostEqual(self.r.state["lx"], 0.5, places=2)

    def test_forward_on_a_stick_is_positive(self):
        """The kernel reports forward as negative, which is the opposite of what
        anyone means, so it is flipped once here rather than in every flow."""
        self.feed(pad.EV_ABS, 0x01, -32768)
        self.assertAlmostEqual(self.r.state["ly"], 1.0, places=3)

    def test_a_trigger_lands_in_the_state(self):
        self.feed(pad.EV_ABS, 0x05, 1023)
        self.assertAlmostEqual(self.r.state["rt"], 1.0, places=3)

    def test_a_button_press_and_release_are_both_recorded(self):
        self.feed(pad.EV_KEY, 0x130, 1)
        self.assertEqual(self.r.state["a"], 1)
        self.assertEqual(self.r.state["_a"], 1)
        self.feed(pad.EV_KEY, 0x130, 0)
        self.assertEqual(self.r.state["a"], 0)
        self.assertEqual(self.r.state["_a"], -1)

    def test_every_event_stamps_the_clock(self):
        """Staleness is the only fast detector there is: BLE link loss does not
        surface until the supervision timeout, seconds later."""
        self.r.state["at"] = 0
        self.feed(pad.EV_ABS, 0x00, 1)
        self.assertGreater(self.r.state["at"], 0)

    def test_an_unknown_code_is_ignored_without_stamping_the_clock(self):
        self.r.state["at"] = 0
        self.feed(pad.EV_ABS, 0x2f, 500)
        self.feed(pad.EV_KEY, 0x1ff, 1)
        self.assertEqual(self.r.state["at"], 0)

    def test_a_syn_event_changes_nothing(self):
        self.r.state["at"] = 0
        self.feed(0x00, 0x00, 0)
        self.assertEqual(self.r.state["at"], 0)


class TestATruncatedStream(unittest.TestCase):
    def test_a_partial_record_at_the_end_of_a_read_is_not_decoded(self):
        """`os.read` can split a record. Unpacking a short one raises, and this
        runs in a thread where raising means the pad silently stops working."""
        r = reader(lx=(-32768, 32767, 0))
        raw = record(pad.EV_ABS, 0x00, 16384) + b"\x01\x02\x03"
        seen = 0
        for at in range(0, len(raw) - pad.EVENT_SIZE + 1, pad.EVENT_SIZE):
            r.feed(struct.unpack(pad.EVENT, raw[at:at + pad.EVENT_SIZE]))
            seen += 1
        self.assertEqual(seen, 1, "the trailing 3 bytes must be left alone")


class TestTheReaderLifecycle(unittest.TestCase):
    def test_with_no_pad_it_says_so_and_stops(self):
        old, pad.DEVICES = pad.DEVICES, "/nonexistent"
        self.addCleanup(setattr, pad, "DEVICES", old)
        state = padmod.blank()
        r = pad.Reader(state)
        r.run()
        self.assertFalse(state["connected"])
        self.assertIn("input group", r.error)

    def test_stopping_is_honoured_without_touching_the_pad(self):
        """A blocking read would sit there until the next stick movement, so
        stopping a flow would hang on the operator."""
        import inspect
        source = inspect.getsource(pad.Reader.run)
        self.assertIn("select.select", source)

    def test_pads_reports_what_a_screen_should_show(self):
        self.assertIsInstance(pad.pads(), list)


if __name__ == "__main__":
    unittest.main()
