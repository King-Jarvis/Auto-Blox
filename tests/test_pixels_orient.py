"""Turning a frame the right way up."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from zero2w_console import pixels  # noqa: E402

# A 3x2 greyscale picture, every pixel distinct:
#     1 2 3
#     4 5 6
GREY = bytes([1, 2, 3, 4, 5, 6])
W, H = 3, 2


def grid(data, width, height, channels=1):
    """Back into rows, so a test reads like the picture it is checking."""
    stride = width * channels
    return [list(data[y * stride:(y + 1) * stride]) for y in range(height)]


class TestNothingToDo(unittest.TestCase):
    def test_no_turn_and_no_mirror_returns_it_untouched(self):
        out, w, h = pixels.orient(GREY, W, H, 1, 0, False)
        self.assertEqual(out, GREY)
        self.assertEqual((w, h), (W, H))

    def test_a_full_turn_is_no_turn(self):
        out, w, h = pixels.orient(GREY, W, H, 1, 360, False)
        self.assertEqual(out, GREY)

    def test_a_turn_that_is_not_a_quarter_is_refused(self):
        with self.assertRaises(ValueError):
            pixels.orient(GREY, W, H, 1, 45, False)


class TestGreyscale(unittest.TestCase):
    def test_mirror_swaps_left_and_right(self):
        out, w, h = pixels.orient(GREY, W, H, 1, 0, True)
        self.assertEqual(grid(out, w, h), [[3, 2, 1], [6, 5, 4]])
        self.assertEqual((w, h), (3, 2))

    def test_half_a_turn_puts_the_bottom_at_the_top_and_reverses_it(self):
        out, w, h = pixels.orient(GREY, W, H, 1, 180, False)
        self.assertEqual(grid(out, w, h), [[6, 5, 4], [3, 2, 1]])

    def test_a_quarter_turn_clockwise(self):
        """The left-hand column becomes the top row, bottom-up."""
        out, w, h = pixels.orient(GREY, W, H, 1, 90, False)
        self.assertEqual((w, h), (2, 3))
        self.assertEqual(grid(out, w, h), [[4, 1], [5, 2], [6, 3]])

    def test_a_quarter_turn_the_other_way(self):
        out, w, h = pixels.orient(GREY, W, H, 1, 270, False)
        self.assertEqual((w, h), (2, 3))
        self.assertEqual(grid(out, w, h), [[3, 6], [2, 5], [1, 4]])

    def test_a_quarter_turn_swaps_the_sides(self):
        for turn in (90, 270):
            _out, w, h = pixels.orient(GREY, W, H, 1, turn, False)
            with self.subTest(turn=turn):
                self.assertEqual((w, h), (H, W))

    def test_two_quarter_turns_are_a_half_turn(self):
        once, w, h = pixels.orient(GREY, W, H, 1, 90, False)
        twice, w2, h2 = pixels.orient(once, w, h, 1, 90, False)
        half, w3, h3 = pixels.orient(GREY, W, H, 1, 180, False)
        self.assertEqual(twice, half)
        self.assertEqual((w2, h2), (w3, h3))

    def test_four_quarter_turns_come_back_to_the_start(self):
        data, w, h = GREY, W, H
        for _ in range(4):
            data, w, h = pixels.orient(data, w, h, 1, 90, False)
        self.assertEqual(data, GREY)
        self.assertEqual((w, h), (W, H))

    def test_the_mirror_happens_after_the_turn(self):
        """Upright and mirrored means turn it, then flip it — the other
        order gives a different picture, and this is the one people mean."""
        out, w, h = pixels.orient(GREY, W, H, 1, 180, True)
        self.assertEqual(grid(out, w, h), [[4, 5, 6], [1, 2, 3]])

    def test_upside_down_and_mirrored_is_the_same_as_flipping_it_vertically(self):
        """Which is what a bare sensor usually needs, and worth knowing."""
        out, _w, _h = pixels.orient(GREY, W, H, 1, 180, True)
        rows = grid(GREY, W, H)
        self.assertEqual(grid(out, W, H), list(reversed(rows)))


class TestColour(unittest.TestCase):
    """Three bytes a pixel: a reversal must keep each pixel's channels in
    order, or the picture comes out in false colour rather than
    backwards."""

    # Two pixels: red then green.
    RGB = bytes([255, 0, 0,  0, 255, 0])

    def test_mirroring_moves_whole_pixels_not_bytes(self):
        out, w, h = pixels.orient(self.RGB, 2, 1, 3, 0, True)
        self.assertEqual(list(out), [0, 255, 0, 255, 0, 0])

    def test_a_half_turn_keeps_the_colours_intact(self):
        out, w, h = pixels.orient(self.RGB, 2, 1, 3, 180, False)
        self.assertEqual(list(out), [0, 255, 0, 255, 0, 0])

    def test_a_quarter_turn_keeps_the_colours_intact(self):
        out, w, h = pixels.orient(self.RGB, 2, 1, 3, 90, False)
        self.assertEqual((w, h), (1, 2))
        self.assertEqual(list(out), [255, 0, 0, 0, 255, 0])


class TestThroughTheWholeConversion(unittest.TestCase):
    def test_a_turned_frame_still_makes_a_valid_png(self):
        out = pixels.frame_to_png(GREY, W, H, "greyscale", rotate=90)
        self.assertTrue(out.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_the_png_carries_the_turned_size(self):
        """A quarter turn swaps the sides, and the header has to agree or a
        browser renders a smear."""
        out = pixels.frame_to_png(GREY, W, H, "greyscale", rotate=90)
        width, height = pixels_size(out)
        self.assertEqual((width, height), (H, W))

    def test_an_untouched_frame_is_the_size_it_started(self):
        out = pixels.frame_to_png(GREY, W, H, "greyscale")
        self.assertEqual(pixels_size(out), (W, H))

    def test_colour_frames_turn_too(self):
        raw = bytes([0x00, 0x1F] * (W * H))          # blue, in rgb565
        out = pixels.frame_to_png(raw, W, H, "rgb565", rotate=270, mirror=True)
        self.assertEqual(pixels_size(out), (H, W))


def pixels_size(data):
    """Width and height out of a PNG's IHDR."""
    import struct
    return struct.unpack(">II", data[16:24])


if __name__ == "__main__":
    unittest.main()
