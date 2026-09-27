"""The same fact has to read the same way on every screen."""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "zero2w_console", "static")
PAGES = ("app.js", "iot.js", "flows.js", "cameras.js", "cameras-core.js", "nav.js")


def src(name):
    with open(os.path.join(STATIC, name), encoding="utf-8") as fh:
        return fh.read()


class TestOneImplementationOfEachFact(unittest.TestCase):
    def test_the_design_system_exports_them(self):
        bundle = src("bundle.js")
        for name in ("bytes", "ago", "duration"):
            with self.subTest(name=name):
                self.assertIn("function %s(" % name, bundle)
                self.assertRegex(bundle, r"%s:\s*%s" % (name, name),
                                 "%s is defined but not exported" % name)

    def test_no_page_defines_its_own(self):
        """A second copy is how they diverged the first time."""
        for page in PAGES:
            body = src(page)
            for name in ("bytes", "ago", "duration", "kbytes", "relTime"):
                with self.subTest(page=page, name=name):
                    self.assertNotIn("function %s(" % name, body,
                                     "%s defines its own %s()" % (page, name))

    def test_the_pages_use_the_shared_ones(self):
        for page in ("iot.js", "flows.js"):
            with self.subTest(page=page):
                self.assertRegex(src(page), r"Z\.(bytes|ago|duration)\(",
                                 "%s formats nothing through the design system"
                                 % page)


class TestNobodySecondGuessesTheServerAboutFreshness(unittest.TestCase):
    def test_no_page_hardcodes_a_freshness_window(self):
        """`device.online` is the answer."""
        for page in PAGES:
            body = src(page)
            hits = re.findall(r"last_seen\s*\)?\s*<\s*\d+|<\s*\d+\s*;\s*//.*fresh", body)
            with self.subTest(page=page):
                self.assertEqual(hits, [], "%s decides freshness itself: %r"
                                 % (page, hits))

    def test_the_iot_screen_asks_the_server(self):
        self.assertIn("d.online", src("iot.js"),
                      "the IOT screen no longer reads the server's answer")

    def test_the_server_still_has_exactly_one_number(self):
        from zero2w_console import fleet as fleetmod
        from zero2w_console import flows as flowmod
        self.assertEqual(fleetmod.FRESH, flowmod.DEVICE_FRESH)

    def test_the_device_variable_and_the_api_agree(self):
        """`{{device.online}}` in a flow and `online` in the API are the
        same question, so they must not be two answers."""
        body = src("flows.js") + src("iot.js")
        self.assertNotIn("DEVICE_FRESH", body,
                         "the browser is carrying a copy of the threshold")


class TestTheServerActuallySendsTheAnswer(unittest.TestCase):
    """Half a contract is worse than none."""

    def setUp(self):
        import tempfile
        from zero2w_console import iot as iotmod
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.iot = iotmod
        self.store = iotmod.DeviceStore(os.path.join(self.dir.name, "iot.json"))
        self.device = iotmod.create_device(self.store, {
            "name": "ESP32-Cam", "board": "esp32cam", "mac": "70:4b:ca:00:00:b8"})

    def seen(self, ago):
        from zero2w_console import iot as iotmod
        doc = self.store.load()
        doc["devices"][0]["last_seen"] = iotmod.now() - ago
        self.store.save(doc)
        return self.iot.public(self.store.load()["devices"][0])

    def test_every_public_device_carries_online(self):
        self.assertIn("online", self.iot.public(
            self.store.load()["devices"][0]))

    def test_the_device_list_carries_it_too(self):
        """This is the endpoint that was missing it."""
        for row in self.iot.devices(self.store):
            self.assertIn("online", row)

    def test_a_board_heard_from_just_now_is_online(self):
        self.assertIs(self.seen(1)["online"], True)

    def test_a_board_silent_for_hours_is_not(self):
        self.assertIs(self.seen(11201)["online"], False)

    def test_the_boundary_is_the_one_number(self):
        from zero2w_console import flows as flowmod
        self.assertIs(self.seen(flowmod.DEVICE_FRESH - 2)["online"], True)
        self.assertIs(self.seen(flowmod.DEVICE_FRESH + 2)["online"], False)

    def test_a_board_never_heard_from_is_not_online(self):
        """`last_seen` is absent before a board first reports, and `None`
        must not read as fresh."""
        row = self.iot.public(self.store.load()["devices"][0])
        self.assertIsNone(row.get("last_seen"))
        self.assertIs(row["online"], False)

    def test_it_still_hides_the_secrets(self):
        """Adding a field to this function must not lose what it is for."""
        doc = self.store.load()
        doc["devices"][0].update(token="t0ken", enroll_token="e0")
        self.store.save(doc)
        row = self.iot.public(self.store.load()["devices"][0])
        self.assertNotIn("token", row)
        self.assertNotIn("enroll_token", row)

    def test_the_answer_is_computed_once(self):
        """Every payload carrying a device should get it from `public()`,
        not work it out again."""
        with open(os.path.join(ROOT, "zero2w_console", "fleet.py"),
                  encoding="utf-8") as fh:
            body = fh.read()
        inline = body.count('iotmod.now() - d["last_seen"] < FRESH') + \
            body.count('iotmod.now() - device["last_seen"] < FRESH')
        self.assertLessEqual(inline, 2,
                             "a third payload works freshness out for itself")


class TestTheScreensUseTheSameWords(unittest.TestCase):
    def test_a_device_is_online_or_offline_everywhere(self):
        for page in ("iot.js", "flows.js"):
            with self.subTest(page=page):
                self.assertNotIn('"not seen"', src(page),
                                 "%s has its own word for offline" % page)

    def test_the_stream_pill_does_not_borrow_a_device_word(self):
        """"offline" beside a device that is also offline reads as one fact
        and is two: one is the board, one is this page's event stream."""
        for page in ("iot.js", "flows.js"):
            with self.subTest(page=page):
                self.assertNotIn('setState("critical", "offline"', src(page))
                self.assertNotIn('"live" : "offline"', src(page))


if __name__ == "__main__":
    unittest.main()
