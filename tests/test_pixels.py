"""Sensor frames into pictures, with nothing but the standard library."""
import os
import struct
import sys
import unittest
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from zero2w_console import pixels  # noqa: E402


def rgb565(value, swapped=False):
    return bytes([value & 0xFF, value >> 8]) if swapped else bytes([value >> 8, value & 0xFF])


class TestConversion(unittest.TestCase):
    def test_the_primaries_come_out_as_primaries(self):
        raw = rgb565(0xF800) + rgb565(0x07E0) + rgb565(0x001F) + rgb565(0xFFFF)
        out = pixels.rgb565_to_rgb(raw, 4, 1)
        self.assertEqual(out[0:3], b"\xff\x00\x00")
        self.assertEqual(out[3:6], b"\x00\xff\x00")
        self.assertEqual(out[6:9], b"\x00\x00\xff")
        self.assertEqual(out[9:12], b"\xff\xff\xff")

    def test_black_and_white_survive_the_round_trip(self):
        """Shifting instead of replicating the high bits makes white 0xf8."""
        out = pixels.rgb565_to_rgb(rgb565(0x0000) + rgb565(0xFFFF), 2, 1)
        self.assertEqual(out[0:3], b"\x00\x00\x00")
        self.assertEqual(out[3:6], b"\xff\xff\xff")

    def test_swapping_is_not_the_default(self):
        """Settled by looking at a real frame from the bench camera: big-
        endian per pixel is right."""
        red_be = rgb565(0xF800)
        self.assertEqual(pixels.rgb565_to_rgb(red_be, 1, 1), b"\xff\x00\x00")
        self.assertNotEqual(pixels.rgb565_to_rgb(red_be, 1, 1, swap=True), b"\xff\x00\x00")

    def test_a_short_frame_is_refused_rather_than_padded(self):
        with self.assertRaises(ValueError):
            pixels.rgb565_to_rgb(b"\x00\x00\x00", 4, 1)


class TestPNG(unittest.TestCase):
    def decode_header(self, blob):
        self.assertEqual(blob[:8], b"\x89PNG\r\n\x1a\n")
        length = struct.unpack(">I", blob[8:12])[0]
        self.assertEqual(blob[12:16], b"IHDR")
        w, h, depth, colour = struct.unpack(">IIBB", blob[16:16 + 10])[:4]
        return w, h, depth, colour, length

    def test_an_rgb_png_is_well_formed(self):
        blob = pixels.png(b"\xff\x00\x00" * 6, 3, 2)
        w, h, depth, colour, _ = self.decode_header(blob)
        self.assertEqual((w, h, depth, colour), (3, 2, 8, 2))
        self.assertTrue(blob.endswith(b"IEND\xae\x42\x60\x82"))

    def test_a_greyscale_png_says_so(self):
        blob = pixels.png(b"\x40" * 6, 3, 2, channels=1)
        self.assertEqual(self.decode_header(blob)[3], 0)

    def test_every_chunk_carries_a_correct_crc(self):
        blob = pixels.png(b"\x11\x22\x33" * 4, 2, 2)
        i = 8
        seen = []
        while i < len(blob):
            length = struct.unpack(">I", blob[i:i + 4])[0]
            kind = blob[i + 4:i + 8]
            body = blob[i + 8:i + 8 + length]
            crc = struct.unpack(">I", blob[i + 8 + length:i + 12 + length])[0]
            self.assertEqual(crc, zlib.crc32(kind + body) & 0xFFFFFFFF, kind)
            seen.append(kind)
            i += 12 + length
        self.assertEqual(seen, [b"IHDR", b"IDAT", b"IEND"])

    def test_the_pixels_survive_compression(self):
        want = bytes(range(0, 24))
        blob = pixels.png(want, 4, 2)
        idat = blob[blob.index(b"IDAT") + 4:]
        rows = zlib.decompress(idat[:len(idat) - 12])
        # every scanline is prefixed with its filter byte
        self.assertEqual(rows[0], 0)
        self.assertEqual(rows[1:13], want[:12])
        self.assertEqual(rows[13], 0)
        self.assertEqual(rows[14:26], want[12:])

    def test_a_frame_that_is_the_wrong_size_is_refused(self):
        with self.assertRaises(ValueError):
            pixels.png(b"\x00" * 5, 4, 2)


class TestFrameToPNG(unittest.TestCase):
    def test_rgb565(self):
        raw = rgb565(0xF800) * 4
        blob = pixels.frame_to_png(raw, 2, 2, "rgb565")
        self.assertEqual(blob[:8], b"\x89PNG\r\n\x1a\n")

    def test_greyscale(self):
        blob = pixels.frame_to_png(b"\x80" * 4, 2, 2, "grayscale")
        self.assertEqual(struct.unpack(">B", blob[25:26])[0], 0)

    def test_an_unknown_format_is_treated_as_rgb565(self):
        blob = pixels.frame_to_png(rgb565(0x07E0) * 4, 2, 2, "something-else")
        self.assertEqual(blob[:8], b"\x89PNG\r\n\x1a\n")

    def test_a_real_frame_from_the_bench_camera(self):
        """160x120 RGB565 is what the OV3660 on the bench actually produces."""
        raw = bytes((i * 7) % 256 for i in range(160 * 120 * 2))
        blob = pixels.frame_to_png(raw, 160, 120, "rgb565")
        self.assertEqual(struct.unpack(">II", blob[16:24]), (160, 120))
        self.assertLess(len(blob), 160 * 120 * 3, "a PNG that big is not compressing")


if __name__ == "__main__":
    unittest.main()
