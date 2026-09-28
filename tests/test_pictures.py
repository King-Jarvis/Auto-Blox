"""Camera capture → Save to SD → Send to host, and the host keeping it.

The same save code runs on a board (to its microSD) and here (to ~/captures),
so it is tested once as code and then from each end: the host engine, the
board's own runner, and a picture going board to host over real TLS.
"""
import os
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from zero2w_console import examples, fleet as fleetmod, flows  # noqa: E402
from zero2w_console.agent.modules import pictures  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0" + bytes(range(256)) * 20 + b"\xff\xd9"


def pic(fmt="jpeg", w=640, h=480, data=JPEG):
    return {"data": data, "format": fmt, "width": w, "height": h}


class SaveCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = self.dir.name

    def tearDown(self):
        self.dir.cleanup()


class TestSaving(SaveCase):
    def test_a_jpeg_is_one_file_named_for_when_it_was_taken(self):
        path = pictures.save(self.root, {"folder": "bench"}, {"picture": pic()})
        self.assertEqual(os.path.dirname(path), os.path.join(self.root, "bench"))
        self.assertRegex(os.path.basename(path), r"^\d{8}-\d{6}-001\.jpg$")
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), JPEG)

    def test_two_in_the_same_second_do_not_overwrite(self):
        a = pictures.save(self.root, {}, {"picture": pic()})
        b = pictures.save(self.root, {}, {"picture": pic()})
        self.assertNotEqual(a, b)
        self.assertEqual(len(os.listdir(os.path.dirname(a))), 2)

    def test_the_folder_cannot_climb_out(self):
        path = pictures.save(self.root, {"folder": "../../etc"}, {"picture": pic()})
        self.assertTrue(path.startswith(os.path.join(self.root, "etc") + "/"))
        self.assertEqual(pictures.folder_name("../"), "captures")

    def test_a_raw_picture_on_a_board_says_its_shape(self):
        raw = pic("grayscale", 160, 120, bytes(160 * 120))
        path = pictures.save(self.root, {}, {"picture": raw})
        self.assertTrue(path.endswith("-160x120-grayscale.raw"), path)

    def test_a_raw_picture_here_becomes_a_png(self):
        raw = pic("grayscale", 160, 120, bytes(160 * 120))
        path = pictures.save(self.root, {}, {"picture": raw}, convert=flows._as_png)
        self.assertTrue(path.endswith("-001.png"), path)
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(8), b"\x89PNG\r\n\x1a\n")

    def test_a_message_without_a_picture_is_a_line_of_the_log(self):
        pictures.save(self.root, {"folder": "readings"}, {"payload": 21.5})
        path = pictures.save(self.root, {"folder": "readings"}, {"payload": 22.0})
        self.assertEqual(os.path.basename(path), "log.txt")
        with open(path) as fh:
            lines = fh.read().splitlines()
        self.assertEqual([l.split(" ", 1)[1] for l in lines], ["21.5", "22.0"])

    def test_keep_drops_the_oldest_but_never_the_log(self):
        folder = os.path.join(self.root, "captures")
        os.makedirs(folder)
        for name in ("20260101-000000-001.jpg", "20260102-000000-001.jpg", "log.txt"):
            open(os.path.join(folder, name), "w").close()
        pictures.save(self.root, {"keep": 2}, {"picture": pic()})
        left = sorted(os.listdir(folder))
        self.assertEqual(len(left), 3)
        self.assertIn("log.txt", left)
        self.assertNotIn("20260101-000000-001.jpg", left)

    def test_keep_zero_keeps_everything(self):
        for _ in range(3):
            pictures.save(self.root, {"keep": 0}, {"picture": pic()})
        self.assertEqual(len(os.listdir(os.path.join(self.root, "captures"))), 3)


class TestOnTheHost(SaveCase):
    def setUp(self):
        super().setUp()
        self.was = flows.CAPTURES
        flows.CAPTURES = self.root

    def tearDown(self):
        flows.CAPTURES = self.was
        super().tearDown()

    def test_a_picture_from_a_device_event_is_saved_here(self):
        e = flows.FlowEngine(flows.FlowStore(os.devnull), None, None, None)
        flow = examples.by_id("ex_snapshot_keep")
        flow["enabled"] = True
        e.doc = {"flows": [flow]}
        self.assertEqual(e.fire_device_event("dev_1", "picture", len(JPEG),
                                             "jpeg 640x480", picture=pic()), 1)
        key, msg = e.jobs.get_nowait()
        self.assertEqual(msg["meta"]["format"], "jpeg")
        self.assertEqual((msg["meta"]["width"], msg["meta"]["height"]), (640, 480))
        node = [n for n in flow["nodes"] if n["type"] == "sd.save"][0]
        port, out = e._execute(flow, node, msg)
        self.assertTrue(out["meta"]["file"].startswith(os.path.join(self.root, "field")))
        with open(out["meta"]["file"], "rb") as fh:
            self.assertEqual(fh.read(), JPEG)

    def test_an_event_without_a_picture_carries_none(self):
        e = flows.FlowEngine(flows.FlowStore(os.devnull), None, None, None)
        flow = examples.by_id("ex_snapshot_keep")
        flow["enabled"] = True
        e.doc = {"flows": [flow]}
        e.fire_device_event("dev_1", "picture", 1)
        _key, msg = e.jobs.get_nowait()
        self.assertNotIn("picture", msg)

    def test_the_inspector_is_never_handed_the_picture(self):
        e = flows.FlowEngine(flows.FlowStore(os.devnull), None, None, None)
        e._record_output("f", "n", {"payload": 10, "picture": pic(), "meta": {}})
        self.assertNotIn(JPEG[:20].hex(), repr(e.outputs()))
        self.assertNotIn("picture", e.outputs()["f/n"])


class TestTheHostReceivesAStill(unittest.TestCase):
    def setUp(self):
        self.fired = []
        self.logged = []
        test = self

        class Engine:
            def fire_device_event(self, *a, **kw):
                test.fired.append((a, kw))

        self.fleet = fleetmod.Fleet(None, None, None)
        self.fleet.engine = Engine()
        self.fleet._emit = lambda device, level, message, **kw: self.logged.append(message)

    def body(self, kind=b"picture", code=1, data=JPEG):
        import struct
        return struct.pack(">BHHB", code, 640, 480, len(kind)) + kind + data

    def test_it_reaches_a_flow_as_a_device_event_with_the_picture(self):
        got = self.fleet.still({"id": "dev_1"}, self.body())
        self.assertEqual(got["data"], JPEG)
        (args, kw), = self.fired
        self.assertEqual(args[:3], ("dev_1", "picture", len(JPEG)))
        self.assertEqual(kw["picture"]["format"], "jpeg")
        self.assertIn("jpeg 640x480", self.logged[0])

    def test_nonsense_is_dropped(self):
        for body in (b"", b"\x01\x02", self.body(code=9), self.body(data=b"")):
            self.assertIsNone(self.fleet.still({"id": "dev_1"}, body))
        self.assertEqual(self.fired, [])


class TestOnTheBoard(SaveCase):
    """The real runner, with a camera and a card stood in."""

    def setUp(self):
        super().setUp()
        from tests.test_agent_pwm import load_runner
        self.flowmod = load_runner()
        test = self
        self.sent = []

        class Camera:
            agent = None

            @staticmethod
            def still(frame_size=None, fmt=None):
                test.asked = (frame_size, fmt)
                return pic()

        class Stream:
            state, outbox = 4, b""

            def poll(self):
                pass

            def send_still(self, *a):
                test.sent.append(a)
                return True

            def close(self, why):
                test.closed = why

        pkg = type(sys)("modules")
        pkg.__path__ = []
        linkclient = type(sys)("modules.linkclient")
        linkclient.READY = 4
        for name, mod in (("camera", Camera), ("pictures", pictures),
                          ("linkclient", linkclient)):
            sys.modules["modules." + name] = mod
            setattr(pkg, name, mod)
        sys.modules["modules"] = pkg
        self.saved = (pictures._mount, pictures.time)
        pictures._mount = lambda: self.root
        pictures._pending = None
        pictures._own = Stream()
        # MicroPython's clock, which has localtime as well as ticks.
        clock = self.flowmod.time
        clock.localtime = time.localtime
        pictures.time = clock
        pictures._quiet = clock.ticks_ms()

        class Agent:
            tags = {}

            def log(self, level, message, **kw):
                test.said.append((level, message))

        self.said = []
        self.flow = examples.by_id("ex_snapshot")
        self.runner = self.flowmod.Runner(self.flow, Agent())
        self.runner.start()

    def tearDown(self):
        pictures._pending = pictures._own = None
        pictures._mount, pictures.time = self.saved
        for name in ("machine", "modules", "modules.camera", "modules.pictures",
                     "modules.linkclient"):
            sys.modules.pop(name, None)
        super().tearDown()

    def run_all(self):
        self.runner.fire("c", 1)
        for _ in range(50):
            self.runner.tick()

    def test_capture_save_and_send_on_the_board(self):
        self.run_all()
        self.assertEqual(self.asked, ("VGA", "jpeg"))
        saved = os.listdir(os.path.join(self.root, "captures"))
        self.assertEqual(len(saved), 1, self.said)
        self.assertTrue(saved[0].endswith(".jpg"))
        self.assertEqual(self.sent, [("picture", "jpeg", 640, 480, JPEG)])
        self.assertFalse([m for lvl, m in self.said if lvl in ("warn", "critical")])

    def test_the_log_after_it_prints_a_size_and_a_path(self):
        self.run_all()
        payload, meta = self.runner.outs["s"][1], self.runner.outs["s"][2]
        self.assertEqual(payload, len(JPEG))
        self.assertTrue(meta["file"].endswith(".jpg"))

    def test_a_send_with_no_picture_says_so(self):
        self.runner.fire("h", 1)
        for _ in range(20):
            self.runner.tick()
        self.assertEqual(self.sent, [])
        self.assertTrue(any("no picture" in m for _lvl, m in self.said))

    def test_no_card_is_said_and_the_flow_carries_on(self):
        def no_card():
            raise OSError(19, "ENODEV")
        pictures._mount = no_card
        self.run_all()
        self.assertTrue(any("not saved to SD" in m for _lvl, m in self.said))
        self.assertEqual(len(self.sent), 1)


    def test_a_stream_opened_for_stills_closes_once_idle(self):
        self.closed = None
        pictures._quiet = self.flowmod.time.ticks_ms() - pictures.IDLE_MS - 1
        self.runner.tick()
        self.assertEqual(self.closed, "no pictures to send")
        self.assertIsNone(pictures._own)


class TestTheCameraTakesOne(unittest.TestCase):
    """modules/camera.py's still(), against a stand-in sensor."""

    def setUp(self):
        import importlib.util
        test = self
        self.events = []

        class Sensor:
            def __init__(self, **kw):
                test.events.append(("made", kw.get("pixel_format")))

            def init(self):
                pass

            def deinit(self):
                test.events.append("off")

            def capture(self):
                return JPEG

            def get_pixel_width(self):
                return 640

            def get_pixel_height(self):
                return 480

        fake = type(sys)("camera")
        fake.Camera = Sensor
        fake.PixelFormat = type("P", (), {"JPEG": "J", "GRAYSCALE": "G", "RGB565": "R"})
        fake.FrameSize = type("F", (), {"VGA": 8, "QQVGA": 0})
        fake.GrabMode = type("G", (), {"LATEST": 1})
        sys.modules["camera"] = fake
        spec = importlib.util.spec_from_file_location(
            "camera_under_test", os.path.join(ROOT, "zero2w_console", "agent",
                                              "modules", "camera.py"))
        self.cam = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.cam)

    def tearDown(self):
        sys.modules.pop("camera", None)

    def test_off_it_comes_on_for_one_picture_and_goes_off(self):
        got = self.cam.still("VGA", "jpeg")
        self.assertEqual(got, {"data": JPEG, "format": "jpeg", "width": 640, "height": 480})
        self.assertEqual(self.events, [("made", "J"), "off"])
        self.assertIsNone(self.cam._cam)

    def test_a_running_feed_is_used_as_it_is_and_left_running(self):
        self.cam._sensor("VGA", "greyscale")
        self.events.clear()
        got = self.cam.still("QQVGA", "jpeg")
        self.assertEqual(got["format"], "grayscale")
        self.assertEqual(self.events, [])
        self.assertIsNotNone(self.cam._cam)


class TestOverTheRealStream(unittest.TestCase):
    """A still from the board's own stream client, over TLS, into the fleet."""

    def test_a_still_goes_board_to_host_encrypted(self):
        from tests import test_camera_stream as cs
        if not cs.MADE:
            self.skipTest("no openssl here to make a certificate")
        case = cs.StreamCase("setUp")
        case.setUp()
        try:
            got = []
            case.fleet.engine = type("E", (), {"fire_device_event":
                                               lambda self, *a, **kw: got.append((a, kw))})()
            s = case.stream()
            self.assertTrue(case.spin(s, lambda: s.state == cs.linkclient.READY))
            self.assertTrue(s.send_still("picture", "jpeg", 640, 480, JPEG))
            self.assertTrue(case.spin(s, lambda: bool(got)))
            (args, kw), = got
            self.assertEqual(args[1], "picture")
            self.assertEqual(kw["picture"]["data"], JPEG)
        finally:
            case.tearDown()


class TestTheNodes(unittest.TestCase):
    def test_capture_offers_jpeg_and_defaults_to_it(self):
        field = [f for f in flows.REGISTRY["camera.capture"]["fields"]
                 if f["key"] == "format"][0]
        self.assertEqual(field["options"], ["colour", "greyscale", "jpeg"])
        self.assertEqual(field["default"], "jpeg")

    def test_a_camera_command_can_ask_for_jpeg_too(self):
        field = [f for f in flows.REGISTRY["device.command"]["fields"]
                 if f["key"] == "format"][0]
        self.assertIn("jpeg", field["options"])

    def test_save_runs_on_both_and_send_on_a_board(self):
        self.assertEqual(flows.REGISTRY["sd.save"]["runs"], "both")
        self.assertEqual(flows.REGISTRY["picture.send"]["runs"], "device")

    def test_a_board_that_sends_is_sent_the_stream_client(self):
        names = fleetmod.Fleet(None, None, None).agent_files(
            examples.by_id("ex_snapshot"), {"board": "esp32cam"})
        for want in ("modules/pictures.py", "modules/media.py",
                     "modules/linkclient.py", "modules/link.py", "modules/camera.py"):
            self.assertIn(want, names)


if __name__ == "__main__":
    unittest.main()
