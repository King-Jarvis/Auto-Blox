"""The link server, over real sockets."""
import json
import os
import threading
import socket
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from zero2w_console import link as linkmod  # noqa: E402
from zero2w_console.agent.modules import link as wire  # noqa: E402

DEVICE = "dev_ef56ab12"
TOKEN = "OFfVo0fv5S99A_pRrD5hDsWYTygYkS8a"
FLEET = {DEVICE: TOKEN, "dev_ab12cd34": "N8bah95ghsfh_q2p4pGRf5-bPLUf5Vzc"}


class Client:
    """A device, as far as the server can tell. Blocking; this is a test."""

    def __init__(self, port, device=DEVICE, token=TOKEN):
        self.device, self.token = device, token
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.sock.settimeout(5)
        self.channel = None

    def greet(self, nonce=None):
        nonce = nonce or os.urandom(wire.NONCE_BYTES)
        self.nonce = nonce
        body = json.dumps({"device": self.device, "nonce": nonce.hex()}).encode()
        self.sock.sendall(wire.seal(linkmod.GREETING_KEY, wire.HELLO, 1, body))
        head, body = linkmod._read_frame(self.sock)
        kind, _seq, body = wire.open_frame(linkmod.GREETING_KEY, head, body)
        assert kind == wire.CHALLENGE, kind
        self.nonce_host = bytes.fromhex(json.loads(body.decode())["nonce"])
        return self.nonce_host

    def authenticate(self, token=None):
        key = wire.session_key(token or self.token, self.nonce, self.nonce_host)
        self.channel = wire.Channel(key)
        self.sock.sendall(self.channel.send(wire.AUTH, b"{}"))

    def ready(self):
        """Read READY — and opening it is what proves the host is genuine."""
        head, body = linkmod._read_frame(self.sock)
        kind, _body = self.channel.receive(head, body)
        assert kind == wire.READY, kind
        return True

    def connect(self):
        self.greet()
        self.authenticate()
        self.ready()
        return self

    def send(self, kind, body=b""):
        self.sock.sendall(self.channel.send(kind, body))

    def recv(self):
        head, body = linkmod._read_frame(self.sock)
        return self.channel.receive(head, body)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class ServerCase(unittest.TestCase):
    def setUp(self):
        self.seen = []
        self.opened = []
        self.closed = []
        self.server = linkmod.LinkServer(
            lookup=FLEET.get,
            on_frame=lambda c, k, b: self.seen.append((c.device_id, k, b)),
            on_open=self.opened.append,
            on_close=self.closed.append,
            port=0, host="127.0.0.1").start()
        self.clients = []

    def tearDown(self):
        for c in self.clients:
            c.close()
        self.server.stop()

    def client(self, **kw):
        c = Client(self.server.port, **kw)
        self.clients.append(c)
        return c

    def wait(self, predicate, seconds=3):
        end = time.time() + seconds
        while time.time() < end:
            if predicate():
                return True
            time.sleep(0.01)
        return False

    def dropped(self, sock):
        """True if the server hung up rather than answering."""
        sock.settimeout(3)
        try:
            return sock.recv(64) == b""
        except ConnectionResetError:
            return True
        except (socket.timeout, OSError):
            return False


class TestAGenuineDeviceGetsIn(ServerCase):
    def test_the_handshake_completes(self):
        self.client().connect()
        self.assertTrue(self.wait(lambda: self.server.online() == [DEVICE]))

    def test_frames_arrive_at_the_callback(self):
        c = self.client().connect()
        c.send(wire.STATE, b'{"free":95504}')
        self.assertTrue(self.wait(lambda: self.seen))
        self.assertEqual(self.seen[0], (DEVICE, wire.STATE, b'{"free":95504}'))

    def test_the_host_can_speak_first_once_the_socket_is_open(self):
        """The whole point: no poll, no queue, no two-second wait."""
        c = self.client().connect()
        self.assertTrue(self.wait(lambda: self.server.online()))
        self.assertTrue(self.server.send(DEVICE, wire.COMMAND,
                                         {"op": "fire", "node": "go"}))
        kind, body = c.recv()
        self.assertEqual(kind, wire.COMMAND)
        self.assertEqual(json.loads(body.decode())["op"], "fire")

    def test_a_command_for_a_device_that_is_not_connected_says_so(self):
        """False is the caller's cue to fall back to the queue, not to
        pretend it was delivered."""
        self.assertFalse(self.server.send("dev_ab12cd34", wire.COMMAND, b"{}"))

    def test_two_devices_do_not_share_a_channel(self):
        a = self.client().connect()
        b = self.client(device="dev_ab12cd34",
                        token=FLEET["dev_ab12cd34"]).connect()
        self.assertTrue(self.wait(lambda: len(self.server.online()) == 2))
        self.server.send(DEVICE, wire.COMMAND, b'{"for":"a"}')
        kind, body = a.recv()
        self.assertEqual(body, b'{"for":"a"}')
        self.server.send("dev_ab12cd34", wire.COMMAND, b'{"for":"b"}')
        _kind, body = b.recv()
        self.assertEqual(body, b'{"for":"b"}')

    def test_ping_is_answered(self):
        c = self.client().connect()
        c.send(wire.PING)
        self.assertEqual(c.recv()[0], wire.PONG)

    def test_open_and_close_are_both_reported(self):
        c = self.client().connect()
        self.assertTrue(self.wait(lambda: self.opened))
        c.close()
        self.assertTrue(self.wait(lambda: self.closed))
        self.assertTrue(self.wait(lambda: self.server.online() == []))


class TestTheHostProvesItselfToo(ServerCase):
    """Mutual, and not a formality."""

    def test_ready_opens_under_the_session_key(self):
        c = self.client()
        c.greet()
        c.authenticate()
        self.assertTrue(c.ready())

    def test_an_imposter_holding_no_token_cannot_produce_a_ready(self):
        """Stand up a server that does not know the token — as a rogue AP
        would not — and the device's own check rejects what it sends
        back."""
        fake = linkmod.LinkServer(lookup=lambda d: "not-the-real-token",
                                  port=0, host="127.0.0.1").start()
        self.addCleanup(fake.stop)
        c = Client(fake.port)
        self.clients.append(c)
        c.greet()
        c.authenticate()          # sealed with the real token
        # The imposter cannot open that, so it hangs up rather than answering.
        self.assertTrue(self.dropped(c.sock),
                        "an imposter answered a handshake it could not verify")

    def test_a_device_rejects_a_ready_sealed_with_the_wrong_key(self):
        c = self.client()
        c.greet()
        c.authenticate()
        c.ready()
        forged = wire.seal(wire.session_key("wrong", c.nonce, c.nonce_host),
                           wire.COMMAND, 2, b'{"op":"fire"}')
        with self.assertRaises(wire.LinkError):
            c.channel.receive(forged[:wire.HEADER], forged[wire.HEADER:])


class TestWhoGetsHungUpOn(ServerCase):
    def test_the_wrong_token_does_not_get_in(self):
        c = self.client()
        c.greet()
        c.authenticate(token="OFfVo0fv5S99A_pRrD5hDsWYTygYkS8b")   # one byte
        self.assertTrue(self.dropped(c.sock))
        self.assertEqual(self.server.online(), [])

    def test_a_device_nobody_has_heard_of_does_not_get_in(self):
        c = self.client(device="dev_00000000", token="whatever")
        c.greet()
        c.authenticate()
        self.assertTrue(self.dropped(c.sock))

    def test_an_unknown_device_is_answered_as_far_as_a_known_one(self):
        """Otherwise the exchange itself tells an attacker which device ids
        are real, before any token is involved."""
        known = self.client()
        unknown = self.client(device="dev_00000000", token="whatever")
        self.assertEqual(len(known.greet()), wire.NONCE_BYTES)
        self.assertEqual(len(unknown.greet()), wire.NONCE_BYTES)

    def test_saying_something_other_than_hello_first_does_not_get_in(self):
        sock = socket.create_connection(("127.0.0.1", self.server.port), timeout=5)
        self.addCleanup(sock.close)
        sock.sendall(wire.seal(linkmod.GREETING_KEY, wire.STATE, 1, b"{}"))
        self.assertTrue(self.dropped(sock))

    def test_rubbish_does_not_get_in(self):
        sock = socket.create_connection(("127.0.0.1", self.server.port), timeout=5)
        self.addCleanup(sock.close)
        sock.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        self.assertTrue(self.dropped(sock))

    def test_a_hello_with_a_short_nonce_does_not_get_in(self):
        c = self.client()
        body = json.dumps({"device": DEVICE, "nonce": "aabb"}).encode()
        c.sock.sendall(wire.seal(linkmod.GREETING_KEY, wire.HELLO, 1, body))
        self.assertTrue(self.dropped(c.sock))

    def test_a_hello_that_is_not_json_does_not_get_in(self):
        c = self.client()
        c.sock.sendall(wire.seal(linkmod.GREETING_KEY, wire.HELLO, 1, b"not json"))
        self.assertTrue(self.dropped(c.sock))

    def test_every_refusal_is_written_down(self):
        c = self.client()
        c.greet()
        c.authenticate(token="wrong-entirely")
        self.assertTrue(self.wait(lambda: self.server.refusals))
        self.assertIn("authenticate", self.server.refusals[-1][2])

    def test_a_refusal_is_not_told_which_part_was_wrong(self):
        """The server writes down why; it does not send it."""
        c = self.client()
        c.greet()
        c.authenticate(token="wrong-entirely")
        c.sock.settimeout(3)
        self.assertEqual(c.sock.recv(256), b"",
                         "the server explained itself to a failed caller")


class TestATamperedStream(ServerCase):
    def test_a_replayed_frame_ends_the_connection(self):
        """A recorded 'full throttle' must not work a second time — and the
        socket does not survive the attempt."""
        c = self.client().connect()
        frame = c.channel.send(wire.CONTROL, b'{"throttle":1.0}')
        c.sock.sendall(frame)
        self.assertTrue(self.wait(lambda: self.seen))
        c.sock.sendall(frame)
        self.assertTrue(self.wait(lambda: self.server.online() == []),
                        "the server kept reading after a replay")
        self.assertEqual(len(self.seen), 1)

    def test_an_altered_body_ends_the_connection(self):
        c = self.client().connect()
        frame = bytearray(c.channel.send(wire.CONTROL, b'{"throttle":0.2}'))
        frame[-3:] = b"1.0"
        c.sock.sendall(bytes(frame))
        self.assertTrue(self.wait(lambda: self.server.online() == []))
        self.assertEqual(self.seen, [])

    def test_a_stream_is_not_resynchronised_after_a_bad_frame(self):
        """Skipping to the next frame would mean a tampered stream keeps
        working, which is the opposite of noticing."""
        c = self.client().connect()
        bad = bytearray(c.channel.send(wire.CONTROL, b"{}"))
        bad[9] ^= 0xFF
        c.sock.sendall(bytes(bad))
        c.sock.sendall(c.channel.send(wire.STATE, b'{"free":1}'))
        self.assertTrue(self.wait(lambda: self.server.online() == []))
        self.assertEqual(self.seen, [])


class TestReconnection(ServerCase):
    def test_a_board_that_rebooted_replaces_its_old_socket(self):
        """The old one looks perfectly healthy from here; TCP will hold a
        dead peer open far longer than a fleet should believe in it."""
        first = self.client().connect()
        self.assertTrue(self.wait(lambda: self.server.online() == [DEVICE]))
        second = self.client().connect()
        self.assertTrue(self.wait(lambda: len(self.opened) == 2))
        self.assertEqual(self.server.online(), [DEVICE])
        self.assertTrue(self.server.send(DEVICE, wire.COMMAND, b'{"op":"stop"}'))
        kind, _body = second.recv()
        self.assertEqual(kind, wire.COMMAND)
        first.close()

    def test_each_connection_starts_its_counters_again(self):
        """A fresh nonce means a fresh key, so a counter from the previous
        session means nothing here."""
        self.client().connect()
        c = self.client().connect()
        c.send(wire.STATE, b'{"n":1}')
        self.assertTrue(self.wait(lambda: self.seen))
        self.assertEqual(self.seen[-1][2], b'{"n":1}')


class TestStatus(ServerCase):
    def test_it_reports_what_is_connected(self):
        self.client().connect()
        self.assertTrue(self.wait(lambda: self.server.online()))
        status = self.server.status()
        self.assertEqual(status["devices"][0]["device"], DEVICE)
        self.assertEqual(status["devices"][0]["address"], "127.0.0.1")

    def test_refusals_are_visible_without_being_unbounded(self):
        for _ in range(70):
            c = self.client()
            c.greet()
            c.authenticate(token="no")
            c.close()
        self.assertTrue(self.wait(lambda: len(self.server.refusals) >= 64))
        self.assertLessEqual(len(self.server.refusals), 64,
                             "the refusal list grows without limit")


class TestItIsQuickEnoughToDriveSomething(ServerCase):
    def test_a_round_trip_is_milliseconds_not_seconds(self):
        """The thing being replaced lands a command in two to three seconds."""
        c = self.client().connect()
        self.assertTrue(self.wait(lambda: self.server.online()))
        started = time.time()
        rounds = 20
        for i in range(rounds):
            self.server.send(DEVICE, wire.CONTROL, b'{"t":0.5}')
            c.recv()
        each = (time.time() - started) / rounds
        self.assertLess(each, 0.05,
                        "a control round trip took %.1fms" % (each * 1000))


class TestPingingOverTheLink(ServerCase):
    def answer_pings(self, c):
        def loop():
            try:
                while True:
                    kind, _b = c.recv()
                    if kind == wire.PING:
                        c.send(wire.PONG)
            except Exception:
                pass
        threading.Thread(target=loop, daemon=True).start()

    def test_a_ping_is_timed_to_its_pong(self):
        c = self.client().connect()
        self.assertTrue(self.wait(lambda: self.server.online()))
        self.answer_pings(c)
        rtt, why = self.server.ping(DEVICE)
        self.assertIsNotNone(rtt, why)
        self.assertLess(rtt, 500)
        self.assertEqual(self.server.connection(DEVICE).rtt_ms, rtt)

    def test_silence_is_no_answer_rather_than_a_number(self):
        self.client().connect()
        self.assertTrue(self.wait(lambda: self.server.online()))
        rtt, why = self.server.ping(DEVICE, timeout=0.3)
        self.assertIsNone(rtt)
        self.assertIn("no answer", why)

    def test_no_link_says_so(self):
        rtt, why = self.server.ping("dev_nobody")
        self.assertIsNone(rtt)
        self.assertIn("no link", why)

    def test_the_route_is_wired(self):
        """Reachable, not merely present: the device POST block is gated on a
        tuple of actions, and a handler outside it answers "unknown action"."""
        with open(os.path.join(ROOT, "zero2w_console", "server.py")) as fh:
            src = fh.read()
        gate = src[src.index('if route.startswith("/api/iot/devices/") and \\'):]
        gate = gate[:gate.index(":\n")]
        self.assertIn('"/ping"', gate)
        self.assertIn('if route.endswith("/ping"):', src)


if __name__ == "__main__":
    unittest.main()
