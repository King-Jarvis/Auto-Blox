"""The agent dialling out: the real `agent.py`, against a real LinkServer."""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time as _real
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from zero2w_console import link as linkmod                    # noqa: E402
from zero2w_console.agent.modules import link as wire         # noqa: E402
from zero2w_console.agent.modules import linkagent            # noqa: E402
from zero2w_console.agent.modules import linkclient           # noqa: E402

AGENT = os.path.join(ROOT, "zero2w_console", "agent", "agent.py")
DEVICE = "dev_ef56ab12"
TOKEN = "OFfVo0fv5S99A_pRrD5hDsWYTygYkS8a"


def load_agent():
    """The real module, with only what a board provides stubbed out."""
    machine = type(sys)("machine")
    machine.freq = lambda: 240000000
    machine.unique_id = lambda: b"\x70\x4b\xca\x00\x00\xb8"
    machine.reset = lambda: None
    network = type(sys)("network")
    network.STA_IF = 0
    network.STAT_CONNECTING = 1004

    class WLAN:
        PM_NONE = 0

        def __init__(self, _iface):
            pass

        def active(self, _on):
            pass

        def config(self, **kw):
            pass

        def isconnected(self):
            return True

        def ifconfig(self):
            return ("10.42.0.51", "255.255.255.0", "10.42.0.1", "10.42.0.1")

        def status(self, *a):
            return -55 if a else 1010

    network.WLAN = WLAN
    sys.modules["machine"] = machine
    sys.modules["network"] = network
    # On a board these sit under modules/ and are reached as modules.x. Stand
    # the package up so the import the agent actually runs is the real one.
    pkg = type(sys)("modules")
    pkg.__path__ = []
    pkg.link, pkg.linkclient, pkg.linkagent = wire, linkclient, linkagent
    sys.modules["modules"] = pkg
    sys.modules["modules.link"] = wire
    sys.modules["modules.linkclient"] = linkclient
    sys.modules["modules.linkagent"] = linkagent

    spec = importlib.util.spec_from_file_location("agent_under_test", AGENT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    clock = type(sys)("time")
    clock.time, clock.sleep = _real.time, _real.sleep
    clock.sleep_ms = lambda ms: None
    clock.ticks_ms = lambda: int(_real.monotonic() * 1000)
    clock.ticks_diff = lambda a, b: a - b
    clock.ticks_add = lambda t, d: t + d
    # Both modules, because `linkagent` imports `time` for itself rather than
    # reaching back through the agent for a tick count. On a board that is
    # MicroPython's time; here it has to be stubbed in both places or the
    # difference only shows up as a missing `ticks_diff`.
    mod.time = clock
    linkagent.time = clock
    return mod


class AgentCase(unittest.TestCase):
    def setUp(self):
        self.mod = load_agent()
        self.seen = []
        self.server = linkmod.LinkServer(
            lookup={DEVICE: TOKEN}.get,
            on_frame=lambda c, k, b: self.seen.append((c.device_id, k, b)),
            port=0, host="127.0.0.1").start()
        self.addCleanup(self.server.stop)
        # The real `__init__`, with the config it would have read off flash.
        # Hand-building the object here meant every attribute added to the
        # agent broke thirteen tests with an AttributeError instead of telling
        # anyone what was actually wrong.
        self.mod.load_json = lambda path, default=None: {
            "host": "http://127.0.0.1:8787", "token": TOKEN, "device": DEVICE,
            "name": "ESP32 Motor", "board": "esp32"}
        self.agent = self.mod.Agent()
        self.agent.online = True
        self.said = []
        self.agent.log = lambda level, msg, **kw: self.said.append((level, msg))
        self.handled = []
        self.agent.handle = self.handled.append
        # `start_link` remembers the port in config.json. On a board that is
        # flash; here it would be a file written into the repository, so
        # nothing below gets to touch the disk unless it says so.
        self.written = {}
        self.mod.save_json = lambda path, doc: self.written.update({path: doc})

    def tearDown(self):
        for name in ("machine", "network", "modules", "modules.link",
                     "modules.linkclient", "modules.linkagent"):
            sys.modules.pop(name, None)
        if self.agent.link:
            self.agent.link.close("test over")

    def spin(self, until=None, seconds=5):
        end = _real.time() + seconds
        while _real.time() < end:
            self.agent.next_link = 0          # no rate limit inside a test
            self.agent.poll_link()
            if until and until():
                return True
            _real.sleep(0.002)
        return bool(until and until())

    def up(self):
        self.agent.start_link({"port": self.server.port})
        return self.spin(lambda: self.agent.link.state == linkclient.READY)


class TestWhenItDoesNotDial(AgentCase):
    def test_no_link_in_the_manifest_means_no_client_at_all(self):
        """A console with no listener, or a board not told to keep one."""
        self.assertIsNone(self.agent.start_link(None))
        self.assertIsNone(self.agent.link)

    def test_a_device_with_no_token_does_not_dial(self):
        """Before enrolment there is nothing to authenticate with, and a
        handshake it cannot finish is a socket opened for nothing."""
        self.agent.token = None
        self.assertIsNone(self.agent.start_link({"port": self.server.port}))
        self.assertIsNone(self.agent.link)

    def test_a_missing_module_says_so_rather_than_raising(self):
        """The flag can be turned on before the sync that fetches the files."""
        sys.modules.pop("modules.linkagent")
        del sys.modules["modules"].linkagent
        self.assertIsNone(self.agent.start_link({"port": self.server.port}))
        self.assertTrue(any("policy is not here" in m for _l, m in self.said),
                        self.said)

    def test_polling_a_link_that_is_not_there_does_nothing(self):
        self.agent.poll_link()
        self.assertIsNone(self.agent.link)


class TestItComesUp(AgentCase):
    def test_the_agent_reaches_the_real_server(self):
        self.assertTrue(self.up(), self.agent.link.dropped)
        self.assertTrue(self.spin(lambda: self.server.online() == [DEVICE]))
        self.assertTrue(any("link up to 127.0.0.1" in m for _l, m in self.said),
                        self.said)

    def test_the_address_comes_from_the_console_url(self):
        """One setting, so a second one cannot be made to disagree with it."""
        for url, want in (("http://10.42.0.1:8787", "10.42.0.1"),
                          ("http://10.42.0.1", "10.42.0.1"),
                          ("http://console.local:8787/", "console.local"),
                          ("10.42.0.1:8787", "10.42.0.1")):
            self.agent.host = url
            self.assertEqual(self.agent.host_name(), want)

    def test_starting_twice_keeps_the_one_socket(self):
        self.assertTrue(self.up())
        first = self.agent.link
        self.assertIs(self.agent.start_link({"port": self.server.port}), first)

    def test_a_host_that_is_not_listening_leaves_the_agent_working(self):
        """The failure that matters: it must look like nothing, not like a
        board that stopped."""
        self.agent.start_link({"port": 1})
        began = _real.time()
        for _ in range(50):
            self.agent.next_link = 0
            self.agent.poll_link()
        self.assertLess(_real.time() - began, 1.0)
        self.assertNotEqual(self.agent.link.state, linkclient.READY)

    def test_it_is_not_polled_faster_than_the_rate_limit(self):
        """Every poll of an idle socket is a failed recv, and on MicroPython
        that is an exception object made and collected."""
        self.assertTrue(self.up())
        self.agent.next_link = self.mod.time.ticks_add(
            self.mod.time.ticks_ms(), self.mod.LINK_POLL_MS)
        before = self.agent.link.frames_in
        polled = []
        self.agent.link.poll = lambda: polled.append(1)
        self.agent.poll_link()
        self.assertEqual(polled, [])
        self.assertEqual(self.agent.link.frames_in, before)


class TestTheWayBackIn(AgentCase):
    """The port is remembered so the next boot can dial *before* it syncs."""

    def test_a_working_port_is_written_to_the_config(self):
        self.assertTrue(self.up())
        self.assertEqual(self.written[self.mod.CONFIG]["link_port"],
                         self.server.port)

    def test_a_boot_arms_one_attempt_only_when_there_is_a_port(self):
        self.mod.load_json = lambda path, default=None: {
            "host": "http://127.0.0.1:8787", "link_port": self.server.port}
        self.assertTrue(self.mod.Agent().link_remembered)
        self.mod.load_json = lambda path, default=None: {
            "host": "http://127.0.0.1:8787"}
        self.assertFalse(self.mod.Agent().link_remembered)

    def test_switching_it_off_forgets_the_port(self):
        """Or the next boot dials a host that has already said no."""
        self.assertTrue(self.up())
        self.written.clear()
        self.agent.stop_link("switched off")
        self.assertIsNone(self.written[self.mod.CONFIG]["link_port"])
        self.assertIsNone(self.agent.link)

    def test_the_run_loop_spends_the_attempt_before_it_syncs(self):
        with open(AGENT) as fh:
            src = fh.read()
        body = src[src.index("    def run("):]
        before = body[:body.index("if self.online and self.token and not self.synced")]
        self.assertIn("link_remembered", before,
                      "the remembered dial must come before the sync, or it "
                      "cannot help a board that cannot finish one")
        self.assertIn("self.link_remembered = False", before,
                      "one attempt per boot, or a missing module is logged "
                      "on every pass of the loop")


class TestWhatTheHostCanSay(AgentCase):
    def send(self, kind, doc):
        self.assertTrue(self.up())
        self.server.send(DEVICE, kind, doc)
        return self.spin(lambda: bool(self.handled) or bool(self.agent.tags))

    def test_a_command_goes_through_the_same_handle(self):
        """No second implementation to drift from the first."""
        self.assertTrue(self.send(wire.COMMAND, {"op": "fire", "node": "n1"}))
        self.assertEqual(self.handled, [{"op": "fire", "node": "n1"}])

    def test_a_tag_push_lands_in_the_same_dict_the_queue_writes(self):
        self.assertTrue(self.send(wire.TAGSET, {"values": {"speed": 42}}))
        self.assertEqual(self.agent.tags, {"speed": 42})

    def test_a_pushed_tag_is_not_echoed_back(self):
        """It came from there."""
        self.assertTrue(self.send(wire.TAGSET, {"values": {"speed": 42}}))
        self.assertEqual(self.agent.tags_written, {})

    def test_a_frame_that_is_not_json_is_ignored(self):
        self.assertTrue(self.up())
        linkagent.on_frame(self.agent.link, wire.COMMAND, b"\xff not json")
        self.assertEqual(self.handled, [])

    def test_a_frame_that_is_json_but_not_an_object_is_ignored(self):
        self.assertTrue(self.up())
        linkagent.on_frame(self.agent.link, wire.COMMAND, b"[1,2]")
        self.assertEqual(self.handled, [])

    def test_a_kind_it_has_no_business_receiving_does_nothing(self):
        self.assertTrue(self.up())
        linkagent.on_frame(self.agent.link, wire.STATE,
                                 b'{"op": "reboot"}')
        self.assertEqual(self.handled, [])

    def test_a_command_that_raises_is_said_out_loud_and_named(self):
        """The most expensive silence in this fleet so far."""
        self.assertTrue(self.up())

        def boom(cmd):
            raise MemoryError("memory allocation failed")

        self.agent.handle = boom
        linkagent.on_frame(self.agent.link, wire.COMMAND, b'{"op": "reload"}')
        levels = [lvl for lvl, _m in self.said]
        said = " ".join(m for _l, m in self.said)
        self.assertIn("critical", levels, self.said)
        self.assertIn("reload", said)
        self.assertIn("memory allocation failed", said)

    def test_a_command_that_raises_does_not_escape_into_the_client(self):
        """Whatever it says, it may not reach `poll()` — that end drops the
        link on anything it catches, and a board that cannot run one
        command is not a board that should lose its only fast route to
        the host."""
        self.assertTrue(self.up())

        def boom(cmd):
            raise ValueError("no")

        self.agent.handle = boom
        linkagent.on_frame(self.agent.link, wire.COMMAND, b'{"op": "stop"}')
        self.assertEqual(self.agent.link.state, self.agent.linkmod.READY)
        self.assertIsNone(self.agent.link.dropped)

    def test_a_log_that_also_fails_falls_back_to_serial(self):
        """The likeliest reason to be reporting a failure here is
        MemoryError, and building the message can raise the same thing
        again."""
        self.assertTrue(self.up())

        def boom(cmd):
            raise MemoryError("first")

        def no_log(level, msg, **kw):
            raise MemoryError("second")

        self.agent.handle = boom
        self.agent.log = no_log
        linkagent.on_frame(self.agent.link, wire.COMMAND, b'{"op": "reload"}')
        self.assertEqual(self.agent.link.state, self.agent.linkmod.READY)


class TestWhatItTellsTheHost(AgentCase):
    def setUp(self):
        super().setUp()
        # `report()` asks gc how much is left, which only MicroPython answers.
        import gc as real_gc
        shim = type(sys)("gc")
        shim.collect = real_gc.collect
        shim.mem_free = lambda: 102448
        sys.modules["gc"] = shim
        self.addCleanup(sys.modules.__setitem__, "gc", real_gc)
        self.posted = []
        self.agent.call = lambda m, p, b=None, **kw: self.posted.append((p, b))
        self.agent.ip = lambda: "10.42.0.51"
        self.agent.rssi = lambda: -55

    def over_link(self, kind):
        return [b for dev, k, b in self.seen if k == kind]

    def test_a_report_takes_the_link_when_it_is_up(self):
        """The point of step four. Nothing on the polled route."""
        self.assertTrue(self.up())
        self.agent.report()
        self.assertTrue(self.spin(lambda: self.over_link(wire.STATE), seconds=3),
                        "no STATE frame reached the host")
        body = json.loads(self.over_link(wire.STATE)[0].decode())
        self.assertEqual(body["free_ram"], 102448)
        self.assertEqual(body["link"]["state"], "ready")
        self.assertEqual(self.posted, [], "it posted as well as sending")

    def test_a_report_falls_back_to_the_route_when_the_link_is_down(self):
        """And still carries the link's state while doing it."""
        self.assertTrue(self.up())
        self.agent.link.close("gone")
        self.agent.report()
        self.assertEqual([p for p, _b in self.posted], ["/api/iot/state"])
        body = self.posted[0][1]
        self.assertEqual(body["link"]["state"], "idle")
        self.assertEqual(body["link"]["dropped"], "gone")

    def test_a_board_with_no_link_at_all_posts_as_it_always_did(self):
        self.agent.report()
        self.assertEqual([p for p, _b in self.posted], ["/api/iot/state"])
        self.assertIsNone(self.posted[0][1]["link"])

    def test_an_event_takes_the_link_too(self):
        self.assertTrue(self.up())
        # The real `log`, not the recorder the rest of the file installs.
        del self.agent.log
        self.agent.log("warn", "watchdog cut drive", node="dog")
        self.assertTrue(self.spin(lambda: self.over_link(wire.EVENT), seconds=3))
        body = json.loads(self.over_link(wire.EVENT)[0].decode())
        self.assertEqual(body["message"], "watchdog cut drive")
        self.assertEqual(body["node"], "dog")

    def test_a_tag_write_that_nobody_took_stays_unsent(self):
        """The one thing in a report the host cannot work out for itself."""
        self.agent.online = False            # no link, no route
        self.agent.tags_written = {"speed": 42}
        self.agent.report()
        self.assertEqual(self.agent.tags_written, {"speed": 42})

    def test_a_tag_write_that_went_is_not_sent_twice(self):
        self.assertTrue(self.up())
        self.agent.tags_written = {"speed": 42}
        self.agent.report()
        self.assertTrue(self.spin(lambda: self.over_link(wire.STATE), seconds=3))
        self.assertEqual(self.agent.tags_written, {})
        body = json.loads(self.over_link(wire.STATE)[0].decode())
        self.assertEqual(body["tags"], {"speed": 42})

    def test_a_newer_write_beats_the_one_being_put_back(self):
        """Restoring an unsent write must not undo something changed since."""
        self.agent.online = False
        self.agent.tags_written = {"speed": 42}
        original = self.agent.report

        def report_then_change():
            self.agent.tags_written["speed"] = 99
            return None

        self.agent.tell = lambda kind, path, body: report_then_change()
        self.agent.report()
        self.assertEqual(self.agent.tags_written, {"speed": 99})


class TestTheHostGoingQuiet(AgentCase):
    """A ready link with a dead host on the end of it."""

    def quiet_for(self, ms):
        """Wind the clock back on the link's idea of when it last heard."""
        self.agent.link_quiet = self.mod.time.ticks_ms() - ms

    def test_a_silent_host_is_asked_before_it_is_given_up_on(self):
        self.assertTrue(self.up())
        before = self.agent.link.frames_out
        self.quiet_for(linkagent.QUIET_MS + 1)
        self.agent.next_link = 0
        self.agent.poll_link()
        self.assertTrue(self.agent.link_asked)
        self.assertGreater(self.agent.link.frames_out, before,
                           "it gave up without asking")
        self.assertEqual(self.agent.link.state, linkclient.READY)

    def test_it_is_asked_once_and_not_on_every_pass(self):
        self.assertTrue(self.up())
        self.quiet_for(linkagent.QUIET_MS + 1)
        self.agent.next_link = 0
        self.agent.poll_link()
        after_one = self.agent.link.frames_out
        for _ in range(5):
            self.agent.next_link = 0
            self.agent.poll_link()
        self.assertEqual(self.agent.link.frames_out, after_one)

    def test_no_answer_ends_the_link(self):
        self.assertTrue(self.up())
        self.agent.link.send = lambda *a, **kw: True     # swallow the ping
        self.quiet_for(linkagent.QUIET_MS + 1)
        self.agent.next_link = 0
        self.agent.poll_link()
        self.quiet_for(linkagent.QUIET_MS + linkagent.ANSWER_MS + 1)
        self.agent.next_link = 0
        self.agent.poll_link()
        self.assertEqual(self.agent.link.state, linkclient.IDLE)
        self.assertIn("no answer", self.agent.link.dropped)

    def test_a_real_answer_clears_it(self):
        """The server answers a PING with a PONG, so a live host resets this
        without anything here having to know that."""
        self.assertTrue(self.up())
        self.quiet_for(linkagent.QUIET_MS + 1)
        self.agent.next_link = 0
        self.agent.poll_link()
        self.assertTrue(self.spin(lambda: not self.agent.link_asked, seconds=3))
        self.assertEqual(self.agent.link.state, linkclient.READY)

    def test_the_command_poll_comes_back_when_the_link_goes(self):
        """Because the poll is skipped while linked, losing the link has to
        put it back or the board goes deaf."""
        self.assertTrue(self.up())
        self.assertTrue(self.agent.linked())
        self.agent.link.close("gone")
        self.assertFalse(self.agent.linked())


class TestAModuleThatArrivesWrong(unittest.TestCase):
    """`sync()` renamed a download into place without checking its sha."""

    def setUp(self):
        self.mod = load_agent()
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def tearDown(self):
        for name in ("machine", "network", "modules", "modules.link",
                     "modules.linkclient", "modules.linkagent"):
            sys.modules.pop(name, None)

    def write(self, name, data):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def test_the_sha_matches_the_one_the_manifest_publishes(self):
        """Same function on both sides or the check is theatre."""
        from zero2w_console import fleet as fleetmod
        for blob in (b"", b"x", b"a" * 5000, os.urandom(1024)):
            path = self.write("m.py", blob)
            self.assertEqual(self.mod._sha_of(path), fleetmod.sha(blob))

    def test_it_reads_in_chunks_rather_than_holding_the_file(self):
        """Streaming a module to flash was the whole point; hashing it
        afterwards must not undo that."""
        blob = os.urandom(40000)
        path = self.write("big.py", blob)
        from zero2w_console import fleet as fleetmod
        self.assertEqual(self.mod._sha_of(path, chunk=64), fleetmod.sha(blob))

    def test_a_truncated_file_does_not_match(self):
        from zero2w_console import fleet as fleetmod
        whole = os.urandom(3000)
        path = self.write("m.py", whole[:1500])
        self.assertNotEqual(self.mod._sha_of(path), fleetmod.sha(whole))

    def test_a_file_that_cannot_be_read_gives_none_rather_than_raising(self):
        """None means "cannot tell", and sync treats that as the old
        behaviour."""
        self.assertIsNone(self.mod._sha_of(os.path.join(self.dir, "nothing")))

    def test_sync_refuses_a_module_whose_sha_is_wrong(self):
        with open(AGENT, encoding="utf-8") as fh:
            source = fh.read()
        body = source[source.index("    def sync("):]
        body = body[:body.index("\n    def ")]
        self.assertIn("_sha_of(part)", body)
        # And it must give up on that file rather than install it.
        after = body[body.index("_sha_of(part)"):]
        self.assertIn("_unlink(part)", after)
        self.assertIn("continue", after)
        self.assertLess(after.index("continue"), after.index("os.rename("),
                        "a wrong sha has to stop before the rename")


class TestSource(unittest.TestCase):
    """Two properties of the file itself, both load-bearing on a board."""

    def setUp(self):
        with open(AGENT) as fh:
            self.src = fh.read()

    def test_the_link_is_imported_lazily(self):
        """An import costs memory that nothing gives back before a reboot,
        so a board that will never link must not pay for the code that
        would."""
        top = self.src[:self.src.index("class Agent:")]
        self.assertNotIn("linkclient", top)
        self.assertNotIn("linkagent", top)
        body = self.src[self.src.index("def link_policy("):]
        self.assertIn("import modules.linkagent", body[:body.index("\n    def ")])

    def test_the_policy_is_not_in_this_file(self):
        """Measured: a MicroPython import costs about 2.2 bytes of RAM per
        character of code, and the link's policy is some 2,300 of them."""
        for owned_by_the_module in ("def on_frame(", "def check(",
                                    "lc.Client(", "QUIET_MS"):
            self.assertNotIn(owned_by_the_module, self.src,
                             "%s belongs in modules/linkagent.py" % owned_by_the_module)

    def test_the_policy_module_can_stand_alone(self):
        """It cannot import `agent.py` — that needs `machine`, which exists
        only on a board — so anything it needs it must import for itself."""
        path = os.path.join(ROOT, "zero2w_console", "agent", "modules",
                            "linkagent.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("import agent", src)
        self.assertIn("import time", src)
        self.assertLess(len(src), 16 * 1024)

    def test_serve_gives_the_link_a_turn(self):
        body = self.src[self.src.index("    def serve("):]
        body = body[:body.index("\n    # -- the loop")]
        self.assertIn("poll_link()", body)

    def test_nothing_here_wrote_a_device_file_into_the_repository(self):
        """`start_link` remembers the port in config.json, which on a board
        is flash and here is the working directory."""
        self.assertFalse(os.path.exists(os.path.join(ROOT, "config.json")),
                         "a test wrote a device config into the repository")

    def test_agent_py_still_fits_in_a_micropython_module(self):
        """The file is compiled on the board."""
        self.assertLess(len(self.src), 40960,
                        "agent.py is %d bytes" % len(self.src))


if __name__ == "__main__":
    unittest.main()
