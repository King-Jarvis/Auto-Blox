"""The GVariant text parser, against shapes taken from a real BlueZ capture."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from zero2w_console import gvariant                        # noqa: E402

parse = gvariant.parse


class TestScalars(unittest.TestCase):
    def test_a_string(self):
        self.assertEqual(parse("'Oura Ring 5'"), "Oura Ring 5")

    def test_unicode_escapes_in_a_name(self):
        self.assertEqual(parse(r"'caf\u00e9 \U0001f600'"), "caf\u00e9 \U0001f600")

    def test_an_octal_escape_is_at_most_three_digits(self):
        self.assertEqual(parse(r"b'\0012'"), b"\x012")

    def test_a_variant_is_transparent(self):
        self.assertEqual(parse("<'x'>"), "x")

    def test_booleans(self):
        self.assertIs(parse("<true>"), True)
        self.assertIs(parse("<false>"), False)

    def test_numbers_including_negative_and_hex(self):
        self.assertEqual(parse("<-82>"), -82)
        self.assertEqual(parse("<0x1f>"), 31)
        self.assertEqual(parse("42"), 42)

    def test_a_double(self):
        self.assertEqual(parse("1.5"), 1.5)

    def test_a_type_name_before_a_value_is_not_part_of_it(self):
        self.assertEqual(parse("uint16 315"), 315)
        self.assertEqual(parse("byte 0x06"), 6)
        self.assertEqual(parse("objectpath '/org/bluez/hci0'"), "/org/bluez/hci0")

    def test_an_escape_inside_a_string(self):
        self.assertEqual(parse(r"'it\'s'"), "it's")
        self.assertEqual(parse(r"'a\nb'"), "a\nb")


class TestContainers(unittest.TestCase):
    def test_an_array(self):
        self.assertEqual(parse("['a', 'b']"), ["a", "b"])

    def test_a_type_name_appears_on_the_first_element_only(self):
        """`[byte 0x06]` and `[byte 0x01, 0x02]` — the rest are bare."""
        self.assertEqual(parse("[byte 0x01, 0x02, 0xff]"), [1, 2, 255])

    def test_a_dict(self):
        self.assertEqual(parse("{'Name': <'x'>, 'Paired': <true>}"),
                         {"Name": "x", "Paired": True})

    def test_a_dict_keyed_by_a_number_which_is_why_json_failed(self):
        self.assertEqual(parse("<{uint16 315: <[byte 0x02, 0x00]>}>"),
                         {315: [2, 0]})

    def test_a_one_element_tuple_has_a_trailing_comma(self):
        self.assertEqual(parse("({'a': <1>},)"), ({"a": 1},))

    def test_a_longer_tuple(self):
        self.assertEqual(parse("(1, 'two', true)"), (1, "two", True))

    def test_an_explicit_type_on_an_empty_container(self):
        self.assertEqual(parse("@a{sv} {}"), {})
        self.assertEqual(parse("@as []"), [])

    def test_nesting_all_the_way_down(self):
        text = ("({'/org/bluez/hci0/dev_AA': {'org.bluez.Device1': "
                "{'UUIDs': <['0000180d-0000-1000-8000-00805f9b34fb']>, "
                "'RSSI': <int16 -63>}}},)")
        got = parse(text)
        dev = got[0]["/org/bluez/hci0/dev_AA"]["org.bluez.Device1"]
        self.assertEqual(dev["RSSI"], -63)
        self.assertEqual(len(dev["UUIDs"]), 1)


class TestByteStrings(unittest.TestCase):
    """gdbus prints a byte array as a bytes literal when it can."""

    def test_an_empty_one(self):
        self.assertEqual(parse("<b''>"), b"")

    def test_printable_bytes(self):
        self.assertEqual(parse("b'ab'"), b"ab")

    def test_a_hex_escape(self):
        self.assertEqual(parse(r"b'\x00\x10'"), b"\x00\x10")

    def test_the_usual_escapes(self):
        self.assertEqual(parse(r"b'a\nb'"), b"a\nb")

    def test_an_octal_escape(self):
        self.assertEqual(parse(r"b'\101'"), b"A")

    def test_service_data_as_it_really_arrives(self):
        text = ("<{'0000a201-0000-1000-8000-00805f9b34fb': <b''>, "
                "'0000180a-0000-1000-8000-00805f9b34fb': <[byte 0xe2, 0x00]>}>")
        got = parse(text)
        self.assertEqual(got["0000a201-0000-1000-8000-00805f9b34fb"], b"")
        self.assertEqual(got["0000180a-0000-1000-8000-00805f9b34fb"], [226, 0])


class TestItRefusesRatherThanGuesses(unittest.TestCase):
    def test_trailing_rubbish_is_an_error(self):
        with self.assertRaises(gvariant.GVariantError):
            parse("'a' and then some")

    def test_an_unterminated_string(self):
        with self.assertRaises(gvariant.GVariantError):
            parse("'never closed")

    def test_an_unterminated_container(self):
        with self.assertRaises(gvariant.GVariantError):
            parse("[1, 2")

    def test_nothing_at_all(self):
        with self.assertRaises(gvariant.GVariantError):
            parse("")

    def test_a_word_that_is_not_a_type_or_a_literal(self):
        with self.assertRaises(gvariant.GVariantError):
            parse("wombat")


class TestARealCapture(unittest.TestCase):
    """One device exactly as gdbus printed it."""

    TEXT = ("({'/org/bluez/hci0': {'org.bluez.Adapter1': "
            "{'Address': <'C4:5F:AC:00:00:1F'>, 'Powered': <true>, "
            "'Discovering': <true>}, 'org.bluez.GattManager1': @a{sv} {}}, "
            "'/org/bluez/hci0/dev_3C_2E_F5_00_00_12': {'org.bluez.Device1': "
            "{'Address': <'3C:2E:F5:00:00:12'>, 'AddressType': <'random'>, "
            "'Alias': <'3C-2E-F5-00-00-12'>, 'Paired': <false>, "
            "'Connected': <false>, 'RSSI': <int16 -86>, "
            "'ManufacturerData': <{uint16 315: <[byte 0x02, 0x00, 0x11]>}>, "
            "'ServiceData': <{'0000a201-0000-1000-8000-00805f9b34fb': <b''>}>, "
            "'AdvertisingFlags': <[byte 0x06]>}}},)")

    def test_it_parses_whole(self):
        tree = parse(self.TEXT)[0]
        self.assertEqual(len(tree), 2)

    def test_the_adapter_survives(self):
        tree = parse(self.TEXT)[0]
        self.assertEqual(tree["/org/bluez/hci0"]["org.bluez.Adapter1"]["Address"],
                         "C4:5F:AC:00:00:1F")
        self.assertEqual(tree["/org/bluez/hci0"]["org.bluez.GattManager1"], {})

    def test_the_property_that_broke_json(self):
        tree = parse(self.TEXT)[0]
        dev = tree["/org/bluez/hci0/dev_3C_2E_F5_00_00_12"]["org.bluez.Device1"]
        self.assertEqual(dev["ManufacturerData"], {315: [2, 0, 17]})
        self.assertEqual(dev["RSSI"], -86)
        self.assertEqual(dev["AdvertisingFlags"], [6])

    def test_and_the_one_that_arrives_as_a_bytes_literal(self):
        tree = parse(self.TEXT)[0]
        dev = tree["/org/bluez/hci0/dev_3C_2E_F5_00_00_12"]["org.bluez.Device1"]
        self.assertEqual(dev["ServiceData"],
                         {"0000a201-0000-1000-8000-00805f9b34fb": b""})

    def test_gatt_reads_devices_straight_out_of_it(self):
        from zero2w_console import gatt
        devices = gatt.devices(parse(self.TEXT)[0])
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["rssi"], -86)
        self.assertTrue(devices[0]["le"], "AddressType random is an LE device")


if __name__ == "__main__":
    unittest.main()
