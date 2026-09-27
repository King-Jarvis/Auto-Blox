"""The link: how a device and the host frame and authenticate what they say."""
try:
    import ustruct as struct
except ImportError:
    import struct

try:
    import uhashlib as hashlib
except ImportError:
    import hashlib

VERSION = 1
HEADER = 16            # version, type, length, seq, mac
MAC_BYTES = 8          # 64 bits, and every guess costs a round trip
MAX_BODY = 65535       # what the two length bytes can say
NONCE_BYTES = 16

# What a frame is. Grouped so a reader can tell a handshake from traffic at a
# glance, and so there is room to add to each group without renumbering.
HELLO, CHALLENGE, AUTH, READY = 1, 2, 3, 4
STATE, EVENT, TAGS = 16, 17, 18
COMMAND, TAGSET = 32, 33
MEDIA, CONTROL = 48, 49
PING, PONG = 64, 65

TYPES = (HELLO, CHALLENGE, AUTH, READY, STATE, EVENT, TAGS,
         COMMAND, TAGSET, MEDIA, CONTROL, PING, PONG)


class LinkError(Exception):
    """Anything that means this connection cannot be trusted any more."""


# ----------------------------------------------------------------- the maths
def _sha256(data):
    return hashlib.sha256(data).digest()


def hmac_sha256(key, message):
    """HMAC, hand-rolled because MicroPython has no `hmac`."""
    if len(key) > 64:
        key = _sha256(key)
    key = key + b"\x00" * (64 - len(key))
    inner = bytearray(64)
    outer = bytearray(64)
    for i in range(64):
        inner[i] = key[i] ^ 0x36
        outer[i] = key[i] ^ 0x5C
    return _sha256(bytes(outer) + _sha256(bytes(inner) + message))


def same(a, b):
    """Compare without leaking where the difference is."""
    if len(a) != len(b):
        return False
    diff = 0
    for i in range(len(a)):
        diff |= a[i] ^ b[i]
    return diff == 0


def session_key(token, nonce_device, nonce_host):
    """Both nonces, so one session's key is useless against another."""
    if not isinstance(token, bytes):
        token = str(token).encode()
    return hmac_sha256(token, bytes(nonce_device) + bytes(nonce_host))


# ---------------------------------------------------------------- the frames
def seal(key, kind, seq, body):
    """One frame, ready to put on the wire."""
    if kind not in TYPES:
        raise LinkError("unknown frame type %r" % (kind,))
    if not isinstance(body, bytes):
        body = bytes(body)
    if len(body) > MAX_BODY:
        raise LinkError("body of %d is past the %d a frame can say"
                        % (len(body), MAX_BODY))
    if seq < 0 or seq > 0xFFFFFFFF:
        raise LinkError("sequence %d is outside the counter" % seq)
    head = struct.pack(">BBHI", VERSION, kind, len(body), seq)
    return head + hmac_sha256(key, head + body)[:MAC_BYTES] + body


def read_header(head):
    """(kind, length, seq) from the first 16 bytes. No trust implied yet."""
    if len(head) != HEADER:
        raise LinkError("a header is %d bytes, got %d" % (HEADER, len(head)))
    version, kind, length, seq = struct.unpack(">BBHI", head[:8])
    if version != VERSION:
        raise LinkError("frame version %d, expected %d" % (version, VERSION))
    if kind not in TYPES:
        raise LinkError("unknown frame type %d" % kind)
    return kind, length, seq


def open_frame(key, head, body, expect_after=None):
    """Check a frame and hand back (kind, seq, body)."""
    kind, length, seq = read_header(head)
    if len(body) != length:
        raise LinkError("frame says %d bytes of body, got %d"
                        % (length, len(body)))
    want = hmac_sha256(key, head[:8] + body)[:MAC_BYTES]
    if not same(head[8:], want):
        raise LinkError("frame did not verify")
    if expect_after is not None and seq <= expect_after:
        raise LinkError("sequence %d is not past %d — replayed or reordered"
                        % (seq, expect_after))
    return kind, seq, body


class Channel:
    """One end of a verified conversation: counters in, counters out."""

    def __init__(self, key):
        self.key = key
        self.out_seq = 0
        self.in_seq = None      # nothing accepted yet, so anything may be first

    def send(self, kind, body=b""):
        self.out_seq += 1
        return seal(self.key, kind, self.out_seq, body)

    def receive(self, head, body):
        kind, seq, body = open_frame(self.key, head, body,
                                     expect_after=self.in_seq)
        self.in_seq = seq
        return kind, body
