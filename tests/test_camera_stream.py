"""The encrypted picture stream, end to end over real TLS sockets: the board's
own stream client (modules/media.py) against the console's link server, fleet
and certificate."""
import os
import socket
import ssl
import struct
import sys
import tempfile
import time
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from zero2w_console import fleet as fleetmod  # noqa: E402
from zero2w_console import flows as flowmod  # noqa: E402
from zero2w_console import iot as iotmod  # noqa: E402
from zero2w_console import link as linkmod  # noqa: E402
from zero2w_console import pixels  # noqa: E402
from zero2w_console import tlscert  # noqa: E402
from zero2w_console.agent.modules import link as wire  # noqa: E402
from zero2w_console.agent.modules import linkclient  # noqa: E402
from zero2w_console.agent.modules import media  # noqa: E402
from tests.test_link_fleet import Bus  # noqa: E402

# MicroPython's millisecond clock, for the one module that uses it.
media.time = types.SimpleNamespace(
    ticks_ms=lambda: int(time.monotonic() * 1000),
    ticks_add=lambda a, b: a + b,
    ticks_diff=lambda a, b: a - b,
    time=time.time)

CERTS = tempfile.mkdtemp()
MADE = tlscert.ensure(CERTS)
JPEG = b"\xff\xd8\xff\xe0" + bytes(range(256)) * 40 + b"\xff\xd9"     # ~10KB

CAMERA_FLOW = {"id": "flow_cam", "name": "cam", "enabled": True, "board": "esp32cam",
               "nodes": [{"id": "feed", "type": "camera.feed",
                          "config": {"format": "jpeg"}},
                         {"id": "pub", "type": "camera.publish",
                          "config": {"every_ms": 250}}],
               "edges": [{"id": "e", "from": "feed", "to": "pub"}]}


class Agent:
    """What media.offer asks of the agent: its token and one HTTP call."""

    def __init__(self, token, answer):
        self.token, self.answer, self.said = token, answer, []

    def call(self, method, path):
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    def log(self, level, text):
        self.said.append((level, text))


@unittest.skipUnless(MADE, "no openssl here to make a certificate")
class StreamCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.devices = iotmod.DeviceStore(os.path.join(self.dir.name, "iot.json"))
        self.flows = flowmod.FlowStore(os.path.join(self.dir.name, "flows.json"))
        self.bus = Bus()
        self.fleet = fleetmod.Fleet(self.devices, self.flows, self.bus)
        self.device = iotmod.create_device(self.devices, {
            "name": "Cam", "board": "esp32cam", "chip": "ESP32-D0WD",
            "mac": "70:4b:ca:00:00:c9"})
        prov = self.fleet.provision(self.device["id"])
        self.token = self.fleet.enroll(self.device["id"], prov["enroll_token"],
                                       {"chip": "ESP32"})["token"]
        doc = self.flows.load()
        flow = dict(CAMERA_FLOW, device=self.device["id"])
        self.flows.save({"flows": [flow], "rev": doc.get("rev")})
        iotmod.update_device(self.devices, self.device["id"], {"flow": "flow_cam"})
        self.fleet.cert_der = tlscert.der(MADE[0])
        self.server = linkmod.LinkServer(
            lookup=self.fleet.device_token, on_frame=self.fleet.on_link_frame,
            on_open=self.fleet.on_link_open, on_close=self.fleet.on_link_close,
            port=0, host="127.0.0.1", tls=tlscert.server_context(*MADE),
            stream_lookup=self.fleet.stream_token).start()
        self.fleet.link = self.server
        self.streams = []

    def tearDown(self):
        for s in self.streams:
            s.close()
        self.server.stop()
        self.dir.cleanup()

    def record(self):
        return iotmod.get_device(self.devices, self.device["id"])

    def offer(self):
        return self.fleet.media_offer(self.record())

    def stream(self, cert=None, token=None):
        port, name, der = media.offer(Agent(token or self.token, self.offer()))
        s = media.Stream("127.0.0.1", port, self.device["id"], token or self.token,
                         cert or der, name)
        self.streams.append(s)
        return s

    def spin(self, s, until, seconds=10):
        end = time.time() + seconds
        while time.time() < end:
            s.poll()
            if until():
                return True
            time.sleep(0.002)
        return until()


class TestTheOffer(StreamCase):
    def test_a_board_takes_an_offer_signed_with_its_token(self):
        got = media.offer(Agent(self.token, self.offer()))
        self.assertEqual(got[0], self.server.port)
        self.assertEqual(got[2], self.fleet.cert_der)

    def test_an_offer_signed_with_another_token_is_refused(self):
        agent = Agent("not-this-boards-token", self.offer())
        self.assertIsNone(media.offer(agent))
        self.assertEqual(agent.said[0][0], "critical")

    def test_a_swapped_certificate_is_refused(self):
        doc = self.offer()
        other = tlscert.der(tlscert.ensure(tempfile.mkdtemp())[0])
        import base64
        doc["cert"] = base64.b64encode(other).decode()
        self.assertIsNone(media.offer(Agent(self.token, doc)))

    def test_no_stream_on_the_console_means_none_quietly(self):
        agent = Agent(self.token, RuntimeError("404"))
        self.assertIsNone(media.offer(agent))
        self.assertEqual(agent.said, [])

    def test_a_console_without_a_certificate_offers_nothing(self):
        self.fleet.cert_der = None
        self.assertIsNone(self.offer())
        self.assertNotIn("modules/media.py",
                         self.fleet.agent_files(self.fleet.flow_for(self.record()),
                                                self.record()))

    def test_a_camera_board_is_sent_the_stream_client(self):
        names = self.fleet.agent_files(self.fleet.flow_for(self.record()), self.record())
        for name in ("modules/camera.py", "modules/media.py",
                     "modules/linkclient.py", "modules/link.py"):
            self.assertIn(name, names)
        self.assertEqual(len(names), len(set(names)))


class TestTheStream(StreamCase):
    def test_it_connects_over_tls_as_a_stream_beside_no_link(self):
        """The board's link is off; a camera may stream anyway."""
        s = self.stream()
        self.assertTrue(self.spin(s, lambda: s.state == linkclient.READY), s.dropped)
        conn = self.server.stream(self.device["id"])
        self.assertIsNotNone(conn)
        self.assertTrue(conn.secure)
        self.assertEqual(self.server.online(), [])      # not a link
        self.assertIsInstance(conn.sock, ssl.SSLSocket)

    def test_nothing_is_sent_until_the_console_asks(self):
        s = self.stream()
        self.spin(s, lambda: s.state == linkclient.READY)
        self.assertFalse(s.due())

    def test_a_viewer_gets_the_boards_jpeg(self):
        s = self.stream()
        self.assertTrue(self.spin(s, lambda: s.state == linkclient.READY))
        pushed = []

        def board():
            s.poll()
            if s.due():
                pushed.append(s.push("jpeg", 640, 480, JPEG))
        import threading
        stop = threading.Event()

        def run():
            while not stop.is_set():
                board()
                time.sleep(0.005)
        t = threading.Thread(target=run)
        t.start()
        try:
            frame = self.fleet.frame(self.record())
        finally:
            stop.set()
            t.join()
        self.assertEqual(frame["type"], "image/jpeg")
        self.assertEqual(frame["image"], JPEG)
        self.assertEqual((frame["width"], frame["height"]), (640, 480))
        self.assertTrue(frame["streamed"])
        self.assertTrue(pushed and all(pushed))

    def test_the_lease_sets_the_pace_and_runs_out(self):
        s = self.stream()
        self.spin(s, lambda: s.state == linkclient.READY)
        conn = self.server.stream(self.device["id"])
        conn.send(wire.CONTROL, {"every": 250, "for": 300})
        self.assertTrue(self.spin(s, s.due, 3))
        self.assertEqual(s.every, 250)
        s.push("jpeg", 160, 120, JPEG)
        time.sleep(0.4)
        s.poll()
        self.assertFalse(s.due())               # the lease ran out

    def test_a_raw_frame_is_turned_into_a_png_here(self):
        grey = bytes(range(160)) * 120
        self.fleet._streamed(self.record(), struct.pack(">BHH", 2, 160, 120) + grey)
        got = self.fleet._picture(self.record(), self.fleet.frames[self.device["id"]])
        self.assertEqual(got["type"], "image/png")
        self.assertTrue(got["image"].startswith(b"\x89PNG"))

    def test_after_the_stream_closes_the_last_picture_is_shown_stale(self):
        """The camera switched off: no stream, nothing on 8080, and the last
        streamed picture is what a viewer gets, not an error."""
        self.fleet._streamed(self.record(), struct.pack(">BHH", 1, 640, 480) + JPEG)
        doc = self.devices.load()
        for d in doc["devices"]:
            if d["id"] == self.device["id"]:
                d["ip"], d["camera_port"] = "127.0.0.1", _free_port()
        self.devices.save(doc)
        got = self.fleet.frame(self.record(), timeout=1)
        self.assertEqual(got["type"], "image/jpeg")
        self.assertEqual(got["image"], JPEG)
        self.assertTrue(got["stale"])

    def test_the_clock_jump_does_not_become_uptime(self):
        agent = Agent(self.token, self.offer())
        agent.started = time.time() - 60
        machine = types.ModuleType("machine")
        jump = {"by": 0}

        class RTC:
            def datetime(self, t):
                jump["by"] = 800000000
        machine.RTC = RTC
        real = media.time
        media.time = types.SimpleNamespace(**dict(vars(real), time=lambda: time.time() + jump["by"]))
        sys.modules["machine"] = machine
        try:
            self.assertIsNotNone(media.offer(agent))
            self.assertAlmostEqual(media.time.time() - agent.started, 60, delta=2)
        finally:
            media.time = real
            sys.modules.pop("machine", None)

    def test_a_picture_too_big_for_a_frame_is_refused_and_said(self):
        s = self.stream()
        self.spin(s, lambda: s.state == linkclient.READY)
        self.assertFalse(s.push("rgb565", 320, 240, bytes(320 * 240 * 2)))
        self.assertIn("too big", s.said)

    def test_the_wrong_certificate_never_connects(self):
        other = tlscert.der(tlscert.ensure(tempfile.mkdtemp())[0])
        s = self.stream(cert=other)
        self.spin(s, lambda: s.state == linkclient.READY, 3)
        self.assertNotEqual(s.state, linkclient.READY)
        self.assertIsNone(self.server.stream(self.device["id"]))

    def test_a_stream_in_the_clear_is_refused(self):
        """Without TLS the hello says stream and the console hangs up."""
        c = linkclient.Client("127.0.0.1", self.server.port, self.device["id"],
                              self.token)
        c._dial()
        if c.state == linkclient.CONNECTING:
            hello = ('{"device": "%s", "nonce": "%s", "role": "stream"}'
                     % (self.device["id"], linkclient._hex(c.nonce))).encode()
            c.outbox = wire.seal(linkclient.GREETING_KEY, wire.HELLO, 1, hello)
        end = time.time() + 3
        while time.time() < end and c.state != linkclient.READY:
            c.poll()
            time.sleep(0.005)
        c.close()
        self.assertIsNone(self.server.stream(self.device["id"]))
        self.assertTrue(any("without TLS" in r[2] for r in self.server.refusals))

    def test_the_plain_link_still_works_on_the_same_port(self):
        iotmod.update_device(self.devices, self.device["id"], {"link": True})
        c = linkclient.Client("127.0.0.1", self.server.port, self.device["id"],
                              self.token)
        end = time.time() + 5
        while time.time() < end and c.state != linkclient.READY:
            c.poll()
            time.sleep(0.005)
        try:
            self.assertEqual(c.state, linkclient.READY, c.dropped)
            self.assertEqual(self.server.online(), [self.device["id"]])
            self.assertIsNone(self.server.stream(self.device["id"]))
        finally:
            c.close()


class TestTurningAJpeg(unittest.TestCase):
    """The sensor flips first, then the screen turns a quarter if need be;
    the result must be the picture the host would have made from raw."""

    W, H = 3, 2

    def picture(self):
        return bytes(range(self.W * self.H))

    def host(self, turn, mirror):
        out, w, h = pixels.orient(self.picture(), self.W, self.H, 1, turn, mirror)
        return bytes(out), w, h

    def test_every_turn_and_mirror_lands_on_the_hosts_picture(self):
        flips = _camera_flips()
        for turn in (0, 90, 180, 270):
            for mirror in (False, True):
                v, h = flips(turn, mirror)
                rows = [list(self.picture()[y * self.W:(y + 1) * self.W])
                        for y in range(self.H)]
                if v:
                    rows = rows[::-1]
                if h:
                    rows = [r[::-1] for r in rows]
                flat = bytes(sum(rows, []))
                w, hh = self.W, self.H
                if turn in (90, 270):          # the screen's quarter turn
                    flat, w, hh = pixels.orient(flat, w, hh, 1, 90, False)
                with self.subTest(turn=turn, mirror=mirror):
                    self.assertEqual((bytes(flat), w, hh), self.host(turn, mirror))


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _camera_flips():
    """camera.py's flips(), without the camera it imports."""
    import importlib.util
    sys.modules.setdefault("camera", types.ModuleType("camera"))
    spec = importlib.util.spec_from_file_location(
        "devcamera", os.path.join(ROOT, "zero2w_console", "agent", "modules", "camera.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.flips


if __name__ == "__main__":
    unittest.main()
