"""BlueZ's object tree, parsed, against synthetic trees in gdbus's shape.

A mis-parse does not raise — it hands a flow the wrong characteristic — so the
shape is what is tested, including a Classic HID device with no GATT and BlueZ
listing an LE bearer on a device that has none. `scripts/ble-gatt-probe.py`
covers a live link.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from zero2w_console import gatt                              # noqa: E402

DEV = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"
HRM = "0000180d-0000-1000-8000-00805f9b34fb"
MEAS = "00002a37-0000-1000-8000-00805f9b34fb"
VENDOR = "f000aa01-0451-4000-b000-000000000000"


def v(t, data):
    """A property value; the D-Bus type is documentation only."""
    return data


def wrap(tree):
    """A tree in the one-element tuple gdbus returns it in."""
    return (tree,)


# A heart-rate strap: one assigned service with a notifying characteristic and a
# descriptor, plus a vendor service. This is the shape a real BLE sensor has.
STRAP = {
    "/org/bluez/hci0": {
        "org.bluez.Adapter1": {
            "Address": v("s", "C4:5F:AC:00:00:1F"),
            "Powered": v("b", True),
            "Discovering": v("b", False),
        },
        "org.bluez.LEAdvertisingManager1": {},
        "org.bluez.GattManager1": {},
    },
    DEV: {
        "org.bluez.Device1": {
            "Address": v("s", "AA:BB:CC:DD:EE:FF"),
            "Name": v("s", "Polar H10"),
            "Icon": v("s", "unknown"),
            "AddressType": v("s", "random"),
            "Paired": v("b", True),
            "Bonded": v("b", True),
            "Trusted": v("b", False),
            "Connected": v("b", True),
            "ServicesResolved": v("b", True),
            "RSSI": v("n", -63),
            "UUIDs": v("as", [HRM, "0000180f-0000-1000-8000-00805f9b34fb"]),
        },
        "org.bluez.Bearer.LE1": {},
    },
    DEV + "/service000a": {
        "org.bluez.GattService1": {
            "UUID": v("s", HRM),
            "Primary": v("b", True),
            "Device": v("o", DEV),
        },
    },
    DEV + "/service000a/char000b": {
        "org.bluez.GattCharacteristic1": {
            "UUID": v("s", MEAS),
            "Service": v("o", DEV + "/service000a"),
            "Flags": v("as", ["notify"]),
            "Notifying": v("b", True),
            "Value": v("ay", [16, 72]),
        },
    },
    DEV + "/service000a/char000b/desc000d": {
        "org.bluez.GattDescriptor1": {
            "UUID": v("s", "00002902-0000-1000-8000-00805f9b34fb"),
            "Characteristic": v("o", DEV + "/service000a/char000b"),
        },
    },
    DEV + "/service0010": {
        "org.bluez.GattService1": {
            "UUID": v("s", VENDOR),
            "Primary": v("b", True),
            "Device": v("o", DEV),
        },
    },
    DEV + "/service0010/char0011": {
        "org.bluez.GattCharacteristic1": {
            "UUID": v("s", VENDOR),
            "Service": v("o", DEV + "/service0010"),
            "Flags": v("as", ["read", "write"]),
            "Value": v("ay", []),
        },
    },
}

# And the thing actually on this bench: Classic HID, no GATT, and BlueZ still
# advertising an LE bearer interface for it.
DUALSHOCK = {
    "/org/bluez/hci0/dev_A4_AE_11_00_00_5B": {
        "org.bluez.Device1": {
            "Address": v("s", "A4:AE:11:00:00:5B"),
            "Alias": v("s", "A4-AE-11-00-00-5B"),
            "Icon": v("s", "input-gaming"),
            "AddressType": v("s", "public"),
            "Class": v("u", 9480),
            "Connected": v("b", True),
            "Paired": v("b", True),
            "ServicesResolved": v("b", True),
            "UUIDs": v("as", ["00001124-0000-1000-8000-00805f9b34fb",
                              "00001200-0000-1000-8000-00805f9b34fb"]),
        },
        "org.bluez.Bearer.LE1": {},
        "org.bluez.Bearer.BREDR1": {},
        "org.bluez.Input1": {},
    },
}


class GattCase(unittest.TestCase):
    def stub(self, replies):
        calls = []

        def fake(path, method, *args, **kw):
            calls.append([path, method] + list(args))
            for key, out in replies.items():
                if key in method:
                    return out
            return None

        self.addCleanup(setattr, gatt, "_gdbus", gatt._gdbus)
        gatt._gdbus = fake
        return calls


class TestTheTree(GattCase):
    def test_one_call_returns_the_whole_tree(self):
        """Browsing a device's GATT is one subprocess, not one per attribute."""
        calls = self.stub({"GetManagedObjects": wrap(STRAP)})
        got = gatt.tree()
        self.assertEqual(len(calls), 1)
        self.assertIn(DEV, got)
        self.assertEqual(got[DEV]["org.bluez.Device1"]["Name"], "Polar H10")

    def test_a_failed_call_is_an_empty_tree_not_an_exception(self):
        self.stub({})
        self.assertEqual(gatt.tree(), {})

    def test_a_missing_tool_is_swallowed_by_the_wrapper_itself(self):
        import subprocess
        old = subprocess.run
        subprocess.run = lambda *a, **kw: (_ for _ in ()).throw(OSError("nope"))
        try:
            self.assertIsNone(gatt._gdbus("/", "x.y"))
        finally:
            subprocess.run = old

    def test_output_that_will_not_parse_is_none_rather_than_an_exception(self):
        """gdbus prints GVariant, and a tool that changed its mind about the
        format must not take the console down with it."""
        import subprocess

        class Done:
            returncode = 0
            stdout = "not gvariant at all ((("
        old = subprocess.run
        subprocess.run = lambda *a, **kw: Done()
        try:
            self.assertIsNone(gatt._gdbus("/", "x.y"))
        finally:
            subprocess.run = old


class TestReadingDevices(unittest.TestCase):
    def test_an_le_sensor_is_described_correctly(self):
        d = gatt.devices(STRAP)[0]
        self.assertEqual(d["mac"], "AA:BB:CC:DD:EE:FF")
        self.assertEqual(d["name"], "Polar H10")
        self.assertTrue(d["le"])
        self.assertFalse(d["bredr"])
        self.assertFalse(d["classic_hid"])
        self.assertEqual(d["rssi"], -63)
        self.assertIn("180d", d["uuids"])

    def test_a_classic_pad_is_not_called_le(self):
        """BlueZ lists Bearer.LE1 on it anyway, so the bearer interface is not
        the signal — `Class` is, and only BR/EDR devices have one."""
        d = gatt.devices(DUALSHOCK)[0]
        self.assertTrue(d["bredr"])
        self.assertFalse(d["le"])
        self.assertTrue(d["classic_hid"])

    def test_bluez_own_icon_identifies_a_gamepad(self):
        d = gatt.devices(DUALSHOCK)[0]
        self.assertTrue(gatt.is_gamepad(d),
                        "input-gaming is the honest signal")

    def test_a_name_that_merely_contains_controller_is_not_one(self):
        """The first version of this flagged "ACI-UniversalController", which is
        an infrared blaster."""
        self.assertFalse(gatt.is_gamepad({"icon": "", "name": "ACI-UniversalController"}))
        self.assertFalse(gatt.is_gamepad({"icon": "", "name": "Govee_H6076_0A41"}))

    def test_connected_devices_sort_first(self):
        tree = dict(STRAP)
        tree["/org/bluez/hci0/dev_11_11_11_11_11_11"] = {
            "org.bluez.Device1": {"Address": "11:11:11:11:11:11",
                                  "Name": "aaa", "Connected": False}}
        self.assertEqual(gatt.devices(tree)[0]["name"], "Polar H10")


class TestTheGattTree(unittest.TestCase):
    def setUp(self):
        self.tree = STRAP

    def test_services_are_found_with_their_characteristics(self):
        services = gatt.gatt("AA:BB:CC:DD:EE:FF", self.tree)
        self.assertEqual(len(services), 2)
        names = [s["name"] for s in services]
        self.assertIn("Heart Rate", names)

    def test_a_characteristic_hangs_off_the_service_that_owns_it(self):
        """Matched on the Service property, not on the path looking right: a
        path is BlueZ's own numbering and is not a contract."""
        hr = [s for s in gatt.gatt("AA:BB:CC:DD:EE:FF", self.tree)
              if s["name"] == "Heart Rate"][0]
        self.assertEqual(len(hr["chars"]), 1)
        self.assertEqual(hr["chars"][0]["name"], "Heart Rate Measurement")
        self.assertEqual(hr["chars"][0]["flags"], ["notify"])
        self.assertTrue(hr["chars"][0]["notifying"])

    def test_a_descriptor_hangs_off_its_characteristic(self):
        hr = [s for s in gatt.gatt("AA:BB:CC:DD:EE:FF", self.tree)
              if s["name"] == "Heart Rate"][0]
        self.assertEqual(len(hr["chars"][0]["descriptors"]), 1)

    def test_a_vendor_uuid_is_left_whole(self):
        """Shortening one would point a flow at a different characteristic."""
        other = [s for s in gatt.gatt("AA:BB:CC:DD:EE:FF", self.tree)
                 if s["name"] != "Heart Rate"][0]
        self.assertEqual(other["name"], VENDOR)

    def test_a_classic_device_has_no_gatt_and_that_is_not_an_error(self):
        """Measured on the DualShock: zero GATT objects however connected it is.
        A screen has to tell that apart from 'not connected yet'."""
        self.assertEqual(gatt.gatt("A4:AE:11:00:00:5B",
                                   DUALSHOCK), [])

    def test_another_devices_attributes_are_not_picked_up(self):
        tree = dict(self.tree)
        tree["/org/bluez/hci0/dev_99_99_99_99_99_99/service0001"] = {
            "org.bluez.GattService1": {"UUID": HRM, "Primary": True,
                                       "Device": "/org/bluez/hci0/dev_99_99_99_99_99_99"}}
        self.assertEqual(len(gatt.gatt("AA:BB:CC:DD:EE:FF", tree)), 2)


class TestFindingACharacteristic(unittest.TestCase):
    def setUp(self):
        self.tree = STRAP

    def test_by_short_uuid(self):
        self.assertEqual(gatt.find_char("AA:BB:CC:DD:EE:FF", "2a37", None, self.tree),
                         DEV + "/service000a/char000b")

    def test_by_full_uuid(self):
        self.assertEqual(gatt.find_char("AA:BB:CC:DD:EE:FF", MEAS, None, self.tree),
                         DEV + "/service000a/char000b")

    def test_narrowed_by_service(self):
        self.assertIsNone(gatt.find_char("AA:BB:CC:DD:EE:FF", "2a37", VENDOR,
                                         self.tree))
        self.assertIsNotNone(gatt.find_char("AA:BB:CC:DD:EE:FF", "2a37", "180d",
                                            self.tree))

    def test_something_that_is_not_there(self):
        self.assertIsNone(gatt.find_char("AA:BB:CC:DD:EE:FF", "2a19", None,
                                         self.tree))

    def test_a_flow_names_uuids_because_paths_are_not_stable(self):
        """BlueZ renumbers attributes between connections, so resolving the path
        every time is what makes a flow survive a reconnect."""
        def renumber(text):
            return (text.replace("service000a", "service0042")
                        .replace("char000b", "char0043"))

        # The Service and Characteristic properties have to move with the paths
        # they point at: BlueZ keeps those consistent and a tree where they
        # disagree is not one this could ever be handed.
        moved = {}
        for path, ifaces in STRAP.items():
            fresh = {}
            for iface, props in ifaces.items():
                fresh[iface] = {k: (renumber(val) if isinstance(val, str) else val)
                                for k, val in props.items()}
            moved[renumber(path)] = fresh
        found = gatt.find_char("AA:BB:CC:DD:EE:FF", "2a37", None, moved)
        self.assertTrue(found and found.endswith("char0043"), found)


class TestReadWriteNotify(GattCase):
    def test_a_read_comes_back_as_bytes(self):
        self.stub({"ReadValue": ([16, 72, 255],)})
        self.assertEqual(gatt.read("/x"), b"\x10H\xff")

    def test_a_read_printed_as_a_bytes_literal_also_works(self):
        """gdbus prints a byte array either as `[byte 0x10, ...]` or as a
        Python-style `b'...'`, depending on its contents. Both are the same
        value and both have to come back the same way."""
        self.stub({"ReadValue": (b"\x10H\xff",)})
        self.assertEqual(gatt.read("/x"), b"\x10H\xff")

    def test_a_read_that_fails_is_none_rather_than_empty_bytes(self):
        """Empty and failed are different, and a flow must be able to tell."""
        self.stub({})
        self.assertIsNone(gatt.read("/x"))

    def test_a_write_is_typed_explicitly(self):
        """`@ay` rather than a bare `[...]`: an empty array has no element type
        to infer, and gdbus refuses an untyped one."""
        calls = self.stub({"WriteValue": ()})
        self.assertTrue(gatt.write("/x", b"\x01\x02\x03"))
        args = calls[0]
        self.assertEqual(args[1], "org.bluez.GattCharacteristic1.WriteValue")
        self.assertEqual(args[2], "@ay [0x01, 0x02, 0x03]")
        self.assertEqual(args[3], "@a{sv} {}")

    def test_a_string_payload_is_encoded(self):
        calls = self.stub({"WriteValue": ()})
        gatt.write("/x", "hi")
        self.assertEqual(calls[0][2], "@ay [0x68, 0x69]")

    def test_an_empty_write_is_still_well_formed(self):
        calls = self.stub({"WriteValue": ()})
        gatt.write("/x", b"")
        self.assertEqual(calls[0][2], "@ay []")

    def test_notify_on_and_off_call_different_methods(self):
        calls = self.stub({"Notify": ()})
        gatt.notify("/x", True)
        gatt.notify("/x", False)
        self.assertTrue(calls[0][1].endswith("StartNotify"), calls[0])
        self.assertTrue(calls[1][1].endswith("StopNotify"), calls[1])


class TestConnecting(GattCase):
    def test_a_link_comes_up(self):
        self.stub({"Connect": ()})
        self.assertTrue(gatt.connect("AA:BB:CC:DD:EE:FF")["ok"])

    def test_already_connected_is_success_not_failure(self):
        """Pressing Connect twice must not read as an error."""
        def fake(path, method, *args, **kw):
            if method.endswith("Connect"):
                return None
            return wrap(STRAP)
        self.addCleanup(setattr, gatt, "_gdbus", gatt._gdbus)
        gatt._gdbus = fake
        out = gatt.connect("AA:BB:CC:DD:EE:FF")
        self.assertTrue(out["ok"])
        self.assertIn("already", out["detail"])

    def test_a_refusal_says_so(self):
        self.stub({})
        out = gatt.connect("AA:BB:CC:DD:EE:FF")
        self.assertFalse(out["ok"])
        self.assertIn("AA:BB:CC:DD:EE:FF", out["detail"])

    def test_the_path_is_built_from_the_address(self):
        calls = self.stub({"Connect": ()})
        gatt.connect("aa:bb:cc:dd:ee:ff")
        self.assertEqual(calls[0][0], DEV)


class TestTheHostCanAlsoBeAPeripheral(unittest.TestCase):
    def test_the_adapter_reports_whether_it_could_serve_gatt(self):
        """Read rather than assumed: it decides whether the other direction —
        something connecting *to* this host — is possible here at all."""
        a = gatt.adapter(STRAP)
        self.assertTrue(a["can_advertise"])
        self.assertTrue(a["can_serve_gatt"])

    def test_an_adapter_without_them_says_so(self):
        bare = {"/org/bluez/hci0": {"org.bluez.Adapter1": {"Address": "x"}}}
        a = gatt.adapter(bare)
        self.assertFalse(a["can_advertise"])
        self.assertFalse(a["can_serve_gatt"])


if __name__ == "__main__":
    unittest.main()
