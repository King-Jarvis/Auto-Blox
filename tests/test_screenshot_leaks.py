"""The leak check that stands between the demo world and a published picture."""
import os
import re
import struct
import sys
import tempfile
import unittest
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import screenshots  # noqa: E402


def deny(found, **kw):
    return screenshots.Denylist(found, **kw)


class TestTheMatcher(unittest.TestCase):
    def test_a_hostname_is_found_as_a_word_and_not_inside_one(self):
        d = deny({"hostname": {"workshop-pi"}})
        self.assertTrue(d.search("blox@workshop-pi:~$"))
        self.assertTrue(d.search("WORKSHOP-PI"))
        self.assertFalse(d.search("my-workshop-pieces"))

    def test_a_user_name_is_not_found_inside_ordinary_words(self):
        d = deny({"user": {"nick"}})
        self.assertFalse(d.search("nickel, snicker, knickknack"))
        self.assertTrue(d.search("owner nick"))

    def test_a_mac_is_found_however_it_is_written(self):
        d = deny({"mac": {"02:5e:11:a4:9c:3b"}})
        for text in ("02:5E:11:A4:9C:3B", "02-5e-11-a4-9c-3b", "enx025e11a49c3b"):
            self.assertTrue(d.search(text), text)
        self.assertFalse(d.search("02:5e:11:a4:9c:3c"))

    def test_an_ipv4_address_is_not_found_inside_a_longer_one(self):
        d = deny({"ip address": {"10.0.0.1"}})
        self.assertTrue(d.search("gateway 10.0.0.1/24"))
        self.assertFalse(d.search("10.0.0.12"))
        self.assertFalse(d.search("110.0.0.1"))

    def test_an_ipv6_address_is_found(self):
        d = deny({"ip address": {"fd7a:115c:a1e0::3a01:c2b1"}})
        self.assertTrue(d.search('"addr": "fd7a:115c:a1e0::3a01:c2b1"'))

    def test_clean_text_has_no_hits(self):
        d = deny({"hostname": {"workshop-pi"}, "mac": {"02:5e:11:a4:9c:3b"},
                  "ssid": {"HomeNet-5G"}})
        self.assertEqual(d.search("auto-blox · 10.42.0.51 · 08:3a:f2:4c:91:2e"), [])

    def test_the_context_never_repeats_the_value(self):
        d = deny({"ssid": {"HomeNet-5G"}})
        (kind, context), = d.search("joined HomeNet-5G at 12:00")
        self.assertEqual(kind, "ssid")
        self.assertNotIn("HomeNet", context)


class TestWhatIsExcused(unittest.TestCase):
    def test_a_value_the_demo_also_uses_is_not_checked(self):
        d = deny({"bluetooth": {"Xbox Wireless Controller"}},
                 allow_words=["xbox wireless controller"])
        self.assertEqual(d.search("Xbox Wireless Controller"), [])
        self.assertEqual(d.skipped, {"bluetooth": 1})

    def test_a_name_the_repository_already_ships_is_not_checked(self):
        d = deny({"console flow": {"Heartbeat watch"}},
                 allow_text="the example called heartbeat watch")
        self.assertEqual(d.search("Heartbeat watch"), [])

    def test_generic_interface_names_are_not_checked(self):
        d = deny({"interface": {"eth0", "wlan0", "lo", "enx025e11a49c3b"}})
        self.assertEqual(d.search("eth0 wlan0 lo"), [])
        self.assertTrue(d.search("enx025e11a49c3b"))

    def test_secrets_and_addresses_are_checked_whatever_else_says_them(self):
        found = {"console token": {"abcd1234secret"}, "mac": {"02:5e:11:a4:9c:3b"},
                 "home": {"/home/someone"}}
        d = deny(found, allow_words=["abcd1234secret", "/home/someone"],
                 allow_text="abcd1234secret 02:5e:11:a4:9c:3b /home/someone")
        self.assertEqual(len(d), 3)


def _chunk(kind, data):
    return (struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data)))


def _png(*extra):
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + b"".join(extra)
            + _chunk(b"IDAT", zlib.compress(b"\0\0")) + _chunk(b"IEND", b""))


def _jpeg(*segments):
    """SOI, the given segments, a quantisation table, then a scan."""
    body = b"".join(b"\xff" + bytes([m]) + struct.pack(">H", len(d) + 2) + d
                    for m, d in segments)
    dqt = b"\xff\xdb" + struct.pack(">H", 67) + bytes(65)
    return b"\xff\xd8" + body + dqt + b"\xff\xda\x00\x02picture\xff\xd9"


class TestFilesThatCarryMore(unittest.TestCase):
    def test_a_png_with_a_text_chunk_is_refused(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
            fh.write(_png(_chunk(b"tEXt", b"Author\0someone")))
        self.addCleanup(os.remove, fh.name)
        self.assertTrue(screenshots.png_problems(fh.name))

    def test_a_plain_png_passes(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
            fh.write(_png())
        self.addCleanup(os.remove, fh.name)
        self.assertEqual(screenshots.png_problems(fh.name), [])

    def test_a_jpeg_with_exif_or_a_comment_is_refused(self):
        exif = _jpeg((0xE0, b"JFIF\0\1\1"), (0xE1, b"Exif\0\0GPS"))
        comment = _jpeg((0xFE, b"taken at home"))
        profile = _jpeg((0xE2, b"ICC_PROFILE\0"))
        for data in (exif, comment, profile):
            self.assertTrue(screenshots.jpeg_problems(data))

    def test_a_plain_camera_frame_passes(self):
        self.assertEqual(screenshots.jpeg_problems(_jpeg((0xE0, b"JFIF\0\1\1"))), [])

    def test_stripping_leaves_only_the_picture(self):
        data = _jpeg((0xE0, b"JFIF\0\1\1"), (0xE1, b"Exif\0\0GPS"), (0xFE, b"note"))
        clean = screenshots.strip_jpeg(data)
        self.assertEqual(screenshots.jpeg_problems(clean), [])
        self.assertIn(b"JFIF", clean)
        self.assertTrue(clean.endswith(b"\xff\xda\x00\x02picture\xff\xd9"))
        self.assertNotIn(b"GPS", clean)


class TestTheScenes(unittest.TestCase):
    def test_every_scene_has_a_unique_name_and_a_path(self):
        names = [s[0] for s in screenshots.SCENES]
        self.assertEqual(len(names), len(set(names)))
        for name, path, _phone, wait, _script in screenshots.SCENES:
            self.assertTrue(re.fullmatch(r"[a-z0-9-]+", name), name)
            self.assertTrue(path.startswith("/"), path)
            self.assertGreater(wait, 0)

    def test_the_readme_shows_only_scenes_that_exist(self):
        with open(os.path.join(ROOT, "README.md")) as fh:
            text = fh.read()
        shown = set(re.findall(r"docs/screenshots/([a-z0-9-]+)\.png", text))
        self.assertTrue(shown, "the README shows no screenshots")
        self.assertEqual(shown - {s[0] for s in screenshots.SCENES}, set())


if __name__ == "__main__":
    unittest.main()
