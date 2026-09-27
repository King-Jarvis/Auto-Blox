"""The device client against the real server, over real sockets."""
import os
import socket
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from zero2w_console import link as linkmod  # noqa: E402
from zero2w_console.agent.modules import link as wire  # noqa: E402
from zero2w_console.agent.modules import linkclient  # noqa: E402

DEVICE = "dev_ef56ab12"
TOKEN = "OFfVo0fv5S99A_pRrD5hDsWYTygYkS8a"
FLEET = {DEVICE: TOKEN}


class Case(unittest.TestCase):
    def setUp(self):
        self.got = []            # (kind, body) the server received
        self.server = linkmod.LinkServer(
            lookup=FLEET.get,
            on_frame=lambda c, k, b: self.got.append((k, b)),
            port=0, host="127.0.0.1").start()
        self.arrived = []        # (kind, body) the client received
        self.clients = []

    def tearDown(self):
        for c in self.clients:
            c.close()
        self.server.stop()

    def client(self, device=DEVICE, token=TOKEN, port=None):
        c = linkclient.Client(
            "127.0.0.1", port or self.server.port, device, token,
            on_frame=lambda cl, k, b: self.arrived.append((k, b)))
        self.clients.append(c)
        return c

    def spin(self, client, until=None, seconds=5):
        """Poll it the way the agent's serve() slice would."""
        end = time.time() + seconds
        while time.time() < end:
            client.poll()
            if until and until():
                return True
            time.sleep(0.002)
        return bool(until and until())

    def connected(self, client):
        return self.spin(client, lambda: client.state == linkclient.READY)


class TestItConnects(Case):
    def test_the_two_halves_of_the_handshake_fit(self):
        c = self.client()
        self.assertTrue(self.connected(c),
                        "never reached ready: %s" % c.dropped)
        self.assertTrue(self.spin(c, lambda: self.server.online() == [DEVICE]))

    def test_it_starts_idle_and_does_nothing_until_polled(self):
        """No node acts by existing, and neither does this."""
        c = self.client()
        self.assertEqual(c.state, linkclient.IDLE)
        self.assertEqual(self.server.online(), [])

    def test_a_device_can_send_once_it_is_up(self):
        c = self.client()
        self.assertTrue(self.connected(c))
        self.assertTrue(c.send(wire.STATE, {"free": 95504}))
        self.assertTrue(self.spin(c, lambda: self.got))
        self.assertEqual(self.got[0][0], wire.STATE)
        self.assertIn(b"95504", self.got[0][1])

    def test_the_host_can_reach_it_without_being_asked(self):
        """The whole point of holding the socket open."""
        c = self.client()
        self.assertTrue(self.connected(c))
        self.assertTrue(self.spin(c, lambda: self.server.online()))
        self.server.send(DEVICE, wire.COMMAND, {"op": "stop"})
        self.assertTrue(self.spin(c, lambda: self.arrived))
        self.assertEqual(self.arrived[0][0], wire.COMMAND)
        self.assertIn(b"stop", self.arrived[0][1])

    def test_sending_before_it_is_up_is_refused_rather_than_queued(self):
        c = self.client()
        self.assertFalse(c.send(wire.STATE, {"free": 1}))

    def test_a_ping_from_the_host_is_answered(self):
        c = self.client()
        self.assertTrue(self.connected(c))
        self.assertTrue(self.spin(c, lambda: self.server.online()))
        conn = self.server.connection(DEVICE)
        before = conn.frames_in
        conn.send(wire.PING)
        self.assertTrue(self.spin(c, lambda: conn.frames_in > before))

    def test_on_ready_is_called_once(self):
        calls = []
        c = linkclient.Client("127.0.0.1", self.server.port, DEVICE, TOKEN,
                              on_ready=lambda cl: calls.append(1))
        self.clients.append(c)
        self.assertTrue(self.connected(c))
        self.spin(c, seconds=0.2)
        self.assertEqual(len(calls), 1)


class TestItNeverBlocks(Case):
    """The rule the agent lives by, checked rather than asserted in a comment."""

    def test_polling_with_nothing_listening_returns_at_once(self):
        c = linkclient.Client("127.0.0.1", 9, DEVICE, TOKEN)   # discard port
        self.clients.append(c)
        worst = 0
        for _ in range(20):
            started = time.time()
            c.poll()
            worst = max(worst, time.time() - started)
        self.assertLess(worst, 0.05,
                        "a poll took %.0fms with nothing on the other end"
                        % (worst * 1000))

    def test_polling_an_unroutable_address_returns_at_once(self):
        """A host that is gone rather than refusing — the case that hangs."""
        c = linkclient.Client("192.0.2.1", 8789, DEVICE, TOKEN)  # TEST-NET-1
        self.clients.append(c)
        started = time.time()
        for _ in range(5):
            c.poll()
        self.assertLess(time.time() - started, 0.5)

    def test_a_connected_but_silent_link_polls_cheaply(self):
        c = self.client()
        self.assertTrue(self.connected(c))
        started = time.time()
        for _ in range(200):
            c.poll()
        self.assertLess(time.time() - started, 0.5)

    def test_poll_does_not_raise_whatever_happens(self):
        """It is called from the loop that also runs the flow."""
        c = self.client()
        self.assertTrue(self.connected(c))
        c.sock.close()                  # yanked from under it
        for _ in range(5):
            c.poll()                    # must not raise
        self.assertEqual(c.state, linkclient.IDLE)


class TestWhenThingsGoWrong(Case):
    def test_a_wrong_token_does_not_come_up(self):
        c = self.client(token="OFfVo0fv5S99A_pRrD5hDsWYTygYkS8b")
        self.assertFalse(self.spin(c, lambda: c.state == linkclient.READY,
                                   seconds=2))
        self.assertIsNotNone(c.dropped)

    def test_an_unknown_device_does_not_come_up(self):
        c = self.client(device="dev_00000000")
        self.assertFalse(self.spin(c, lambda: c.state == linkclient.READY,
                                   seconds=2))

    def test_an_imposter_that_cannot_seal_a_ready_is_refused(self):
        """A rogue access point holding no token."""
        fake = linkmod.LinkServer(lookup=lambda d: "not-the-real-token",
                                  port=0, host="127.0.0.1").start()
        self.addCleanup(fake.stop)
        c = self.client(port=fake.port)
        self.assertFalse(self.spin(c, lambda: c.state == linkclient.READY,
                                   seconds=2))

    def test_the_host_going_away_drops_it_back_to_idle(self):
        c = self.client()
        self.assertTrue(self.connected(c))
        self.server.stop()
        self.assertTrue(self.spin(c, lambda: c.state == linkclient.IDLE,
                                  seconds=3))
        self.assertIsNotNone(c.dropped)

    def test_it_backs_off_rather_than_hammering(self):
        c = linkclient.Client("127.0.0.1", 9, DEVICE, TOKEN)
        self.clients.append(c)
        for _ in range(4):
            c.poll()
        self.assertGreater(c.failures, 0)
        self.assertGreater(c.next_try, time.time())

    def test_the_backoff_has_a_ceiling(self):
        c = linkclient.Client("127.0.0.1", 9, DEVICE, TOKEN)
        self.clients.append(c)
        c.failures = 40
        c._drop("testing")
        self.assertLessEqual(c.next_try - time.time(),
                             linkclient.BACKOFF_MAX + 1)

    def test_it_comes_back_after_the_host_returns(self):
        c = self.client()
        self.assertTrue(self.connected(c))
        port = self.server.port
        self.server.stop()
        self.assertTrue(self.spin(c, lambda: c.state == linkclient.IDLE,
                                  seconds=3))
        self.server = linkmod.LinkServer(
            lookup=FLEET.get,
            on_frame=lambda cn, k, b: self.got.append((k, b)),
            port=port, host="127.0.0.1").start()
        c.next_try = 0                  # skip the backoff the test just earned
        self.assertTrue(self.connected(c), "it did not reconnect: %s" % c.dropped)


class TestItRefusesWhatItShould(Case):
    def test_a_frame_bigger_than_it_can_hold_is_refused(self):
        """The bug that once stopped a board pulling a 31KB module was
        gathering bytes it could not afford."""
        c = self.client()
        self.assertTrue(self.connected(c))
        self.assertTrue(self.spin(c, lambda: self.server.online()))
        conn = self.server.connection(DEVICE)
        conn.send(wire.COMMAND, b"x" * (linkclient.MAX_IN + 1))
        self.assertTrue(self.spin(c, lambda: c.state == linkclient.IDLE,
                                  seconds=3))
        self.assertIn("more than this holds", c.dropped)

    def test_a_tampered_frame_ends_the_link(self):
        c = self.client()
        self.assertTrue(self.connected(c))
        self.assertTrue(self.spin(c, lambda: self.server.online()))
        conn = self.server.connection(DEVICE)
        frame = bytearray(conn.channel.send(wire.COMMAND, b'{"op":"stop"}'))
        frame[-4] ^= 0xFF
        conn.sock.sendall(bytes(frame))
        self.assertTrue(self.spin(c, lambda: c.state == linkclient.IDLE,
                                  seconds=3))
        self.assertEqual(self.arrived, [])

    def test_a_replayed_command_is_refused(self):
        c = self.client()
        self.assertTrue(self.connected(c))
        self.assertTrue(self.spin(c, lambda: self.server.online()))
        conn = self.server.connection(DEVICE)
        frame = conn.channel.send(wire.COMMAND, b'{"op":"stop"}')
        conn.sock.sendall(frame)
        self.assertTrue(self.spin(c, lambda: self.arrived))
        conn.sock.sendall(frame)                 # the same one again
        self.assertTrue(self.spin(c, lambda: c.state == linkclient.IDLE,
                                  seconds=3))
        self.assertEqual(len(self.arrived), 1)

    def test_what_it_will_not_queue_it_says_no_to(self):
        c = self.client()
        self.assertTrue(self.connected(c))
        c.outbox = b"x" * linkclient.MAX_OUT
        self.assertFalse(c.send(wire.STATE, {"free": 1}))


class TestWhatTheBoardTaughtIt(Case):
    """Three faults a CPython test could not have found, now pinned."""

    def test_a_flush_that_ends_the_link_stops_the_poll(self):
        """The masking bug."""
        c = self.client()
        self.assertTrue(self.connected(c))

        def die():
            c._drop("the real reason")
            return False

        c._flush = die
        c.poll()
        self.assertEqual(c.dropped, "the real reason")
        self.assertEqual(c.state, linkclient.IDLE)

    def test_the_errnos_come_from_the_runtime_not_from_memory(self):
        """MicroPython on ESP32 is newlib: EINPROGRESS is 112 and ENOTCONN
        is 128, not Linux's 115 and 107."""
        for value in (11, 35, 107, 112, 114, 115, 119, 120, 127, 128):
            self.assertIn(value, linkclient.AGAIN, "errno %d" % value)
        import errno as host_errno
        for name in ("EAGAIN", "EINPROGRESS", "ENOTCONN"):
            self.assertIn(getattr(host_errno, name), linkclient.AGAIN, name)

    def test_a_real_error_is_not_mistaken_for_not_yet(self):
        """The tuple must stay a list of "not yet", not a catch-all."""
        for name in ("ECONNRESET", "EPIPE", "ECONNREFUSED", "EBADF"):
            import errno as host_errno
            self.assertNotIn(getattr(host_errno, name), linkclient.AGAIN, name)

    def test_a_handshake_that_never_finishes_is_given_up_on(self):
        """A socket that connects and says nothing must not hold the client
        in `connecting` forever."""
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        self.addCleanup(listener.close)
        c = self.client(port=listener.getsockname()[1])
        c.poll()                                    # dials
        self.assertEqual(c.state, linkclient.CONNECTING)
        c.deadline = time.time() - 1                # as if it had been 20s
        for _ in range(20):
            c.poll()
            if c.state == linkclient.IDLE:
                break
        self.assertEqual(c.state, linkclient.IDLE)
        self.assertIn("handshake did not finish", c.dropped)
        self.assertGreater(c.next_try, time.time())

    def test_nothing_is_written_until_the_socket_is_writable(self):
        """The third fault, which the board named itself once the second was
        out of the way: a premature send answers ECONNABORTED, which by
        number cannot be told from a real abort."""
        c = self.client()
        c.poll()                                    # dials
        self.assertEqual(c.state, linkclient.CONNECTING)
        queued = c.outbox
        c.poller = type("NotYet", (), {"poll": lambda self, t: []})()
        for _ in range(5):
            c.poll()
        self.assertEqual(c.state, linkclient.CONNECTING)
        self.assertEqual(c.outbox, queued, "it wrote before it was writable")
        self.assertIsNone(c.dropped)

    def test_a_premature_write_is_survivable_with_no_poller_to_ask(self):
        """`select` should always be there, but if a build has trimmed it
        the fallback must be "try again", bounded by the handshake
        deadline — not "this socket is broken"."""
        class StillDialling:
            """A socket that answers a write the way the ESP32 build does."""

            def send(self, _data):
                raise OSError(113, "ECONNABORTED")

            def recv(self, _n):
                raise OSError(11, "EAGAIN")

            def close(self):
                pass

        c = self.client()
        c.poll()
        c.sock.close()
        c.sock = StillDialling()
        c.poller = None
        c.state = linkclient.CONNECTING
        c.outbox = b"unsent"
        c.poll()
        self.assertEqual(c.state, linkclient.CONNECTING)
        self.assertIsNone(c.dropped)
        c.deadline = time.time() - 1
        c.poll()
        self.assertEqual(c.state, linkclient.IDLE)
        self.assertIn("handshake did not finish", c.dropped)

    def test_the_poller_goes_with_the_socket(self):
        """A registration outliving its socket is a leak on a board that may
        dial hundreds of times before anyone looks at it."""
        c = self.client()
        self.assertTrue(self.connected(c))
        self.assertIsNotNone(c.poller)
        c.close("done")
        self.assertIsNone(c.poller)
        self.assertIsNone(c.sock)

    def test_a_dial_that_fails_says_which_errno(self):
        """MicroPython's OSError often stringifies to the bare number with
        no name, and the first round of this failing on a board was a
        guess about which number it was."""
        c = self.client(port=1)
        for _ in range(5):
            c.poll()
            if c.dropped:
                break
        self.assertIsNotNone(c.dropped)
        self.assertIn("errno", c.dropped)


class TestItFitsOnABoard(unittest.TestCase):
    def test_it_is_small_enough_to_pull(self):
        """The ceiling is about the board that has 102KB free, not the one
        with 4MB."""
        path = os.path.join(ROOT, "zero2w_console", "agent", "modules",
                            "linkclient.py")
        self.assertLess(os.path.getsize(path), 16 * 1024)

    def test_the_hex_helpers_agree_with_the_library(self):
        """Hand-rolled because a stock build's ubinascii is not a given."""
        for raw in (b"", b"\x00", b"\xff\x01\x7f", os.urandom(16)):
            self.assertEqual(linkclient._hex(raw), raw.hex())
            self.assertEqual(linkclient._unhex(raw.hex()), raw)

    def test_the_greeting_key_matches_the_host(self):
        self.assertEqual(linkclient.GREETING_KEY, linkmod.GREETING_KEY)


if __name__ == "__main__":
    unittest.main()
