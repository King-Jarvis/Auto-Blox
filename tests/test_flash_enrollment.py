"""The enrollment window has to still be open when the board finally asks."""
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from zero2w_console import fleet as fleetmod  # noqa: E402

SCRIPT = os.path.join(ROOT, "scripts", "iot-flash.py")


def source():
    with open(SCRIPT, encoding="utf-8") as fh:
        return fh.read()


class TestTheWindowOutlastsTheFlash(unittest.TestCase):
    def test_the_script_reopens_enrollment_after_writing(self):
        self.assertIn("/api/iot/enrollment/open", source(),
                      "nothing re-opens the window, so a slow flash enrolls "
                      "into a window that has already closed")

    def test_it_reopens_after_the_files_are_written_not_before(self):
        """Before the write is where provision already does it, and that is
        the thing that does not work."""
        src = source()
        wrote = src.index('say("    wrote %-12s')
        reopen = src.index("/api/iot/enrollment/open")
        self.assertGreater(reopen, wrote,
                           "the window is re-opened before the board is "
                           "written, which changes nothing")

    def test_a_dry_run_opens_nothing(self):
        """--dry-run promises to touch nothing, and a window is something."""
        src = source()
        reopen = src.index("/api/iot/enrollment/open")
        guard = src.rindex("if not a.dry_run:", 0, reopen)
        between = src[guard:reopen]
        self.assertNotIn("\ndef ", between,
                         "the dry-run guard is not the one wrapping this")

    def test_it_asks_for_the_same_window_the_ui_flasher_uses(self):
        src = source()
        self.assertIn("fleetmod.AFTER_FLASH", src,
                      "the command line and the IOT screen should not "
                      "disagree about how long a board has to come up")

    def test_the_fallback_matches_that_constant(self):
        """The import is wrapped, so the literal beside it must not drift."""
        src = source()
        window = src[src.index("fleetmod.AFTER_FLASH"):]
        literal = re.search(r"seconds = (\d+)", window)
        self.assertIsNotNone(literal, "no fallback beside the import")
        self.assertEqual(int(literal.group(1)), fleetmod.AFTER_FLASH,
                         "the fallback and AFTER_FLASH have drifted apart")

    def test_a_failure_to_reopen_does_not_lose_the_flash(self):
        """The board is already written by this point."""
        src = source()
        window = src[src.index("/api/iot/enrollment/open"):]
        window = window[:window.index("say(\"\\ndone.")]
        self.assertIn("except Exception", window)


class TestTheWindowIsLongEnoughToBeWorthIt(unittest.TestCase):
    def test_after_a_flash_is_longer_than_the_ordinary_window(self):
        """A board coming up for the first time is not quick, and the
        ordinary window is sized for somebody pressing a button."""
        self.assertGreater(fleetmod.AFTER_FLASH, fleetmod.ENROLL_WINDOW)

    def test_it_is_long_enough_for_a_write_and_a_boot(self):
        """1.79MB at 460800 is about half a minute, plus erase, plus the
        REPL copy, plus a cold ESP32 joining a network for the first
        time."""
        self.assertGreaterEqual(fleetmod.AFTER_FLASH, 300)


if __name__ == "__main__":
    unittest.main()
