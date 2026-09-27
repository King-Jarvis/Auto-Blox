"""The link's wire format: framing, the handshake, and forgery."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from zero2w_console.agent.modules import link  # noqa: E402

TOKEN = "OFfVo0fv5S99A_pRrD5hDsWYTygYkS8a"     # the shape a real one has


def keyed():
    return link.session_key(TOKEN, b"\x01" * 16, b"\x02" * 16)


class TestHMAC(unittest.TestCase):
    """Hand-rolled, so it is checked against the standard library."""

    def test_it_matches_the_reference_implementation(self):
        import hashlib
        import hmac as reference
        for key, msg in ((b"key", b"The quick brown fox"),
                         (b"", b""),
                         (b"k" * 200, b"a longer key than the block size"),
                         (b"\x00\xff" * 8, bytes(range(256)))):
            with self.subTest(key=key[:12], msg=msg[:20]):
                self.assertEqual(link.hmac_sha256(key, msg),
                                 reference.new(key, msg, hashlib.sha256).digest())

    def test_a_key_longer_than_the_block_is_hashed_first(self):
        import hashlib
        import hmac as reference
        key = b"x" * 65
        self.assertEqual(link.hmac_sha256(key, b"hello"),
                         reference.new(key, b"hello", hashlib.sha256).digest())


class TestConstantTimeCompare(unittest.TestCase):
    def test_it_agrees_with_equality(self):
        self.assertTrue(link.same(b"abcd", b"abcd"))
        self.assertFalse(link.same(b"abcd", b"abce"))
        self.assertFalse(link.same(b"abcd", b"abc"))
        self.assertFalse(link.same(b"", b"a"))
        self.assertTrue(link.same(b"", b""))

    def test_it_does_not_stop_at_the_first_difference(self):
        """Returning early tells anyone timing it how much of a guess was
        right, which turns forging a MAC into a byte-at-a-time search."""
        import inspect
        body = inspect.getsource(link.same)
        after = body.split("diff = 0", 1)[1]
        self.assertNotIn("return", after.split("return diff == 0")[0])


class TestTheSessionKey(unittest.TestCase):
    def test_both_nonces_change_it(self):
        base = link.session_key(TOKEN, b"\x01" * 16, b"\x02" * 16)
        self.assertNotEqual(base, link.session_key(TOKEN, b"\x09" * 16, b"\x02" * 16))
        self.assertNotEqual(base, link.session_key(TOKEN, b"\x01" * 16, b"\x09" * 16))

    def test_a_different_token_is_a_different_key(self):
        self.assertNotEqual(keyed(),
                            link.session_key("another-token", b"\x01" * 16, b"\x02" * 16))

    def test_it_is_the_length_sha256_gives(self):
        self.assertEqual(len(keyed()), 32)

    def test_the_token_does_not_appear_in_it(self):
        """Obvious, and worth asserting: the key goes nowhere near the wire
        in a recoverable form."""
        self.assertNotIn(TOKEN.encode(), keyed())

    def test_a_recording_of_one_session_is_useless_against_the_next(self):
        """The reason both ends contribute a nonce."""
        old = link.session_key(TOKEN, b"\x01" * 16, b"\x02" * 16)
        new = link.session_key(TOKEN, b"\x01" * 16, b"\x03" * 16)
        frame = link.seal(old, link.CONTROL, 1, b'{"throttle":1.0}')
        with self.assertRaises(link.LinkError):
            link.open_frame(new, frame[:link.HEADER], frame[link.HEADER:])


class TestFrames(unittest.TestCase):
    def setUp(self):
        self.key = keyed()

    def split(self, frame):
        return frame[:link.HEADER], frame[link.HEADER:]

    def test_a_frame_survives_the_round_trip(self):
        frame = link.seal(self.key, link.STATE, 7, b'{"free":108016}')
        kind, seq, body = link.open_frame(self.key, *self.split(frame))
        self.assertEqual((kind, seq, body),
                         (link.STATE, 7, b'{"free":108016}'))

    def test_the_header_is_the_size_it_claims(self):
        frame = link.seal(self.key, link.PING, 1, b"")
        self.assertEqual(len(frame), link.HEADER)

    def test_an_empty_body_is_fine(self):
        frame = link.seal(self.key, link.PING, 1, b"")
        kind, _seq, body = link.open_frame(self.key, *self.split(frame))
        self.assertEqual((kind, body), (link.PING, b""))

    def test_a_full_size_body_is_fine(self):
        big = b"\x5a" * link.MAX_BODY
        frame = link.seal(self.key, link.MEDIA, 1, big)
        _k, _s, body = link.open_frame(self.key, *self.split(frame))
        self.assertEqual(body, big)

    def test_a_body_past_what_the_header_can_say_is_refused(self):
        with self.assertRaises(link.LinkError):
            link.seal(self.key, link.MEDIA, 1, b"x" * (link.MAX_BODY + 1))

    def test_an_unknown_type_cannot_be_sent_or_received(self):
        with self.assertRaises(link.LinkError):
            link.seal(self.key, 200, 1, b"")
        good = link.seal(self.key, link.PING, 1, b"")
        bad = bytes([good[0], 200]) + good[2:]
        with self.assertRaises(link.LinkError):
            link.open_frame(self.key, bad[:link.HEADER], bad[link.HEADER:])

    def test_a_frame_from_a_future_version_is_refused_rather_than_guessed_at(self):
        good = link.seal(self.key, link.PING, 1, b"")
        bad = bytes([99]) + good[1:]
        with self.assertRaises(link.LinkError):
            link.open_frame(self.key, bad[:link.HEADER], bad[link.HEADER:])


class TestForgery(unittest.TestCase):
    """Each of these is something reachable by anyone already on the wifi."""

    def setUp(self):
        self.key = keyed()
        self.frame = link.seal(self.key, link.CONTROL, 5, b'{"throttle":0.2}')

    def tamper(self, frame):
        with self.assertRaises(link.LinkError):
            link.open_frame(self.key, frame[:link.HEADER], frame[link.HEADER:])

    def test_a_changed_body_is_caught(self):
        """The one that matters: turning a gentle throttle into a full one."""
        body = self.frame[link.HEADER:].replace(b"0.2", b"1.0")
        self.tamper(self.frame[:link.HEADER] + body)

    def test_a_changed_type_is_caught(self):
        """Relabelling a ping as a motor command, or the reverse."""
        head = bytearray(self.frame[:link.HEADER])
        head[1] = link.PING
        self.tamper(bytes(head) + self.frame[link.HEADER:])

    def test_a_changed_length_is_caught(self):
        head = bytearray(self.frame[:link.HEADER])
        head[2], head[3] = 0, 4
        self.tamper(bytes(head) + self.frame[link.HEADER:])

    def test_a_changed_sequence_is_caught(self):
        head = bytearray(self.frame[:link.HEADER])
        head[7] = 99
        self.tamper(bytes(head) + self.frame[link.HEADER:])

    def test_a_changed_mac_is_caught(self):
        head = bytearray(self.frame[:link.HEADER])
        head[8] ^= 0x01
        self.tamper(bytes(head) + self.frame[link.HEADER:])

    def test_the_wrong_key_is_caught(self):
        other = link.session_key("someone-elses-token", b"\x01" * 16, b"\x02" * 16)
        with self.assertRaises(link.LinkError):
            link.open_frame(other, self.frame[:link.HEADER],
                            self.frame[link.HEADER:])

    def test_a_truncated_body_is_caught_before_the_mac_is_even_checked(self):
        self.tamper(self.frame[:-3])

    def test_a_frame_made_up_from_nothing_is_caught(self):
        self.tamper(b"\x01\x31\x00\x04\x00\x00\x00\x01" + b"\x00" * 8 + b"fire")

    def test_every_single_bit_of_the_frame_is_covered(self):
        """Exhaustive rather than representative: flip each bit in turn and
        none of them may produce a frame that opens."""
        for i in range(len(self.frame)):
            for bit in range(8):
                broken = bytearray(self.frame)
                broken[i] ^= 1 << bit
                if bytes(broken) == self.frame:
                    continue
                with self.subTest(byte=i, bit=bit):
                    with self.assertRaises(link.LinkError):
                        link.open_frame(self.key, bytes(broken[:link.HEADER]),
                                        bytes(broken[link.HEADER:]))


class TestReplay(unittest.TestCase):
    def setUp(self):
        self.key = keyed()

    def test_the_same_frame_twice_is_refused(self):
        """A recorded 'full throttle' must not work a second time."""
        frame = link.seal(self.key, link.CONTROL, 4, b'{"throttle":1.0}')
        head, body = frame[:link.HEADER], frame[link.HEADER:]
        link.open_frame(self.key, head, body, expect_after=3)
        with self.assertRaises(link.LinkError):
            link.open_frame(self.key, head, body, expect_after=4)

    def test_an_older_frame_is_refused(self):
        frame = link.seal(self.key, link.CONTROL, 2, b"")
        with self.assertRaises(link.LinkError):
            link.open_frame(self.key, frame[:link.HEADER],
                            frame[link.HEADER:], expect_after=9)

    def test_a_gap_is_allowed_through(self):
        """TCP does not lose frames, but a reconnection resets nothing on
        the sender."""
        frame = link.seal(self.key, link.CONTROL, 100, b"")
        kind, seq, _b = link.open_frame(self.key, frame[:link.HEADER],
                                        frame[link.HEADER:], expect_after=4)
        self.assertEqual(seq, 100)


class TestChannel(unittest.TestCase):
    def setUp(self):
        key = keyed()
        self.device, self.host = link.Channel(key), link.Channel(key)

    def deliver(self, frame, to):
        return to.receive(frame[:link.HEADER], frame[link.HEADER:])

    def test_a_conversation_runs_both_ways(self):
        kind, body = self.deliver(
            self.device.send(link.STATE, b'{"free":108016}'), self.host)
        self.assertEqual((kind, body), (link.STATE, b'{"free":108016}'))
        kind, body = self.deliver(
            self.host.send(link.COMMAND, b'{"op":"reload"}'), self.device)
        self.assertEqual((kind, body), (link.COMMAND, b'{"op":"reload"}'))

    def test_counters_advance_on_their_own_side(self):
        for _ in range(3):
            self.deliver(self.device.send(link.PING), self.host)
        self.assertEqual(self.device.out_seq, 3)
        self.assertEqual(self.host.in_seq, 3)
        self.assertEqual(self.host.out_seq, 0)

    def test_the_two_directions_do_not_share_a_counter(self):
        """Otherwise one side talking fast would make the other's frames
        look like replays."""
        for _ in range(5):
            self.deliver(self.device.send(link.STATE, b"{}"), self.host)
        kind, _body = self.deliver(self.host.send(link.COMMAND, b"{}"),
                                   self.device)
        self.assertEqual(kind, link.COMMAND)

    def test_a_captured_frame_replayed_into_the_channel_is_refused(self):
        frame = self.device.send(link.CONTROL, b'{"throttle":1.0}')
        self.deliver(frame, self.host)
        with self.assertRaises(link.LinkError):
            self.deliver(frame, self.host)

    def test_a_stream_of_control_frames_stays_cheap(self):
        """Not a benchmark — a guard."""
        frame = self.device.send(link.CONTROL, b'{"t":0.5,"s":-0.2}')
        self.assertEqual(len(frame), link.HEADER + 18)


if __name__ == "__main__":
    unittest.main()
