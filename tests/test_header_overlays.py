"""Header buses: the overlay table read from this board's device tree, and the
command the studio hands over to switch them on."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from zero2w_console import buses                                   # noqa: E402

DATA = os.path.join(ROOT, "zero2w_console", "data", "overlays-zero2w.json")
BASE_DTB = "/boot/dtb/allwinner/sun50i-h618-orangepi-zero2w.dtb"


def table():
    with open(DATA) as fh:
        return {o["name"]: o for o in json.load(fh)["overlays"]}


class TestTheTable(unittest.TestCase):
    @unittest.skipUnless(os.path.exists(BASE_DTB), "not on the board")
    def test_it_is_what_the_generator_makes_now(self):
        done = subprocess.run([sys.executable, os.path.join(ROOT, "scripts",
                                                            "build_overlays.py"), "--check"])
        self.assertEqual(done.returncode, 0,
                         "overlays-zero2w.json is stale — run scripts/build_overlays.py")

    def test_the_pins_are_the_device_tree_s(self):
        t = table()
        self.assertEqual([(p["pin"], p["phys"]) for p in t["i2c3-ph"]["pins"]],
                         [("PH4", 18), ("PH5", 24)])
        self.assertEqual([(p["pin"], p["phys"]) for p in t["pwm1-ph3"]["pins"]],
                         [("PH3", 13)])

    def test_a_bank_the_header_does_not_carry_is_marked(self):
        self.assertFalse(table()["i2c3-pg"]["on_header"])

    def test_spi1_is_switched_on_with_no_pins_and_says_so(self):
        """The base tree gives SPI1 no pin group and the overlay adds none."""
        o = table()["spidev1_0"]
        self.assertEqual(o["pins"], [])
        self.assertFalse(o["on_header"])
        self.assertIn("without assigning", o["note"])


class TestWhatIsSetNow(unittest.TestCase):
    def env(self, text):
        fd, path = tempfile.mkstemp()
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        self.addCleanup(os.unlink, path)
        return path

    def test_the_overlays_line_is_read(self):
        h = buses.header_overlays(self.env("verbosity=1\noverlays=i2c3-ph pwm1-ph3\n"))
        self.assertEqual(h["current"], ["i2c3-ph", "pwm1-ph3"])
        self.assertTrue(h["available"])

    def test_no_line_is_nothing(self):
        self.assertEqual(buses.header_overlays(self.env("verbosity=1\n"))["current"], [])


class TestTheCommand(unittest.TestCase):
    """The studio builds `sudo sed ... && sudo reboot`; the sed part is run
    here on a copy, without sudo and without the reboot."""

    def run_sed(self, before, names):
        edit = "sed -i -e '/^overlays=/d'" + \
            (" -e '$a overlays=" + " ".join(names) + "'" if names else "")
        fd, path = tempfile.mkstemp()
        with os.fdopen(fd, "w") as fh:
            fh.write(before)
        self.addCleanup(os.unlink, path)
        subprocess.run(edit + " " + path, shell=True, check=True)
        with open(path) as fh:
            return fh.read()

    def test_it_adds_a_line(self):
        out = self.run_sed("verbosity=1\noverlay_prefix=sun50i-h616\n", ["i2c3-ph"])
        self.assertEqual(out.splitlines()[-1], "overlays=i2c3-ph")
        self.assertIn("overlay_prefix=sun50i-h616", out)

    def test_it_replaces_the_one_there(self):
        out = self.run_sed("overlays=uart5\nverbosity=1\n", ["i2c3-ph", "pwm1-ph3"])
        self.assertEqual([l for l in out.splitlines() if l.startswith("overlays=")],
                         ["overlays=i2c3-ph pwm1-ph3"])

    def test_choosing_nothing_removes_it(self):
        self.assertNotIn("overlays=", self.run_sed("overlays=uart5\nverbosity=1\n", []))

    def test_the_studio_builds_that_command(self):
        with open(os.path.join(ROOT, "zero2w_console", "static", "flows.js")) as fh:
            js = fh.read()
        self.assertIn('"sudo sed -i -e \'/^overlays=/d\'"', js)
        self.assertIn('" -e \'$a overlays=" + names.join(" ") + "\'"', js)
        self.assertIn('" && sudo reboot"', js)


if __name__ == "__main__":
    unittest.main()
