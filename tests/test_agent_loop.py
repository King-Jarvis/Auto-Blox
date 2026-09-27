"""One full pass of the agent's main loop, against a canned host."""
import importlib.util
import json
import os
import sys
import time as _real
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

AGENT = os.path.join(ROOT, "zero2w_console", "agent", "agent.py")
DEVICE = "dev_ef56ab12"
TOKEN = "OFfVo0fv5S99A_pRrD5hDsWYTygYkS8a"


class Stop(BaseException):
    """Thrown from inside the loop to end it after one pass."""


class FakeSocket:
    """A host that answers every request the agent knows how to make."""

    sent = []
    manifest = {"device": DEVICE, "poll": 25, "modules": [], "flow": None,
                "tags": {}, "camera": False, "pins": [], "link": None}
    commands = []

    def __init__(self, *a, **kw):
        self.wrote = b""
        self.reply = None

    def settimeout(self, _t):
        pass

    def connect(self, _addr):
        pass

    def write(self, data):
        self.wrote += data

    def send(self, data):
        self.wrote += data
        return len(data)

    # name -> bytes, for /api/iot/module/<name>
    modules = {}

    def _body(self):
        head = self.wrote.split(b"\r\n", 1)[0].decode("latin-1")
        FakeSocket.sent.append(head)
        if "/api/iot/manifest" in head:
            return json.dumps(FakeSocket.manifest).encode()
        if "/api/iot/commands" in head:
            return json.dumps({"commands": FakeSocket.commands}).encode()
        if "/api/iot/module/" in head:
            name = head.split("/api/iot/module/", 1)[1].split(" ", 1)[0]
            return FakeSocket.modules.get(name, b"")
        return b"{}"

    def read(self, n=512):
        """Answers in chunks, the way a socket does."""
        if self.reply is None:
            body = self._body()
            self.reply = (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                          b"Content-Length: %d\r\n\r\n" % len(body)) + body
        if not self.reply:
            return b""
        out, self.reply = self.reply[:n], self.reply[n:]
        return out

    def recv(self, n=512):
        return self.read(n)

    def close(self):
        pass


def load_agent(cfg=None, flow=None, real_fs=False):
    """The real module, with only what a board provides replaced."""
    machine = type(sys)("machine")
    machine.freq = lambda: 240000000
    machine.unique_id = lambda: b"\x70\x4b\xca\x00\x00\xb8"
    machine.reset = lambda: None
    order = []

    network = type(sys)("network")
    network.STA_IF = 0
    network.STAT_CONNECTING = 1004

    class WLAN:
        PM_NONE = 0

        def __init__(self, _iface):
            order.append("radio")

        def active(self, _on):
            order.append("radio-active")

        def config(self, **kw):
            pass

        def isconnected(self):
            return True

        def ifconfig(self):
            return ("10.42.0.140", "255.255.255.0", "10.42.0.1", "10.42.0.1")

        def status(self, *a):
            return -57 if a else 1010

    network.WLAN = WLAN
    sock = type(sys)("socket")
    sock.socket = FakeSocket
    sock.getaddrinfo = lambda host, port: [(2, 1, 0, "", (host, port))]
    for name, mod in (("machine", machine), ("network", network),
                      ("usocket", sock), ("socket", sock)):
        sys.modules[name] = mod

    spec = importlib.util.spec_from_file_location("agent_loop_under_test", AGENT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    clock = type(sys)("time")
    clock.time, clock.sleep = _real.time, lambda s: None
    clock.sleep_ms = lambda ms: None
    clock.ticks_ms = lambda: int(_real.monotonic() * 1000)
    clock.ticks_diff = lambda a, b: a - b
    clock.ticks_add = lambda t, d: t + d
    mod.time = clock
    mod.order = order

    # `report()` asks gc how much memory is left, which only MicroPython
    # answers. Without this the real report raises, `run()` catches it the way
    # it catches everything, and the loop spins forever — which is how this
    # harness first behaved.
    import gc as real_gc
    shim = type(sys)("gc")
    shim.collect = real_gc.collect
    shim.mem_free = lambda: 102448
    sys.modules["gc"] = shim
    mod.gc = shim

    real_load = mod.load_json
    config = dict({"host": "http://10.42.0.1:8787", "token": TOKEN,
                   "device": DEVICE, "ssid": "example-wifi", "psk": "x" * 12,
                   "name": "ESP32 Motor", "board": "esp32"}, **(cfg or {}))

    def load(path, default=None):
        if path == mod.CONFIG:
            return config
        if path == mod.FLOW:
            return flow
        return real_load(path, default) if real_fs else default

    mod.load_json = load
    if not real_fs:
        mod.save_json = lambda path, doc: None
        mod.ensure_dir = lambda path: None
    # The agent prints its banner and its retries. A module-level name shadows
    # the builtin, so this keeps the suite's output about the suite.
    mod.print = lambda *a, **kw: None
    return mod


class LoopCase(unittest.TestCase):
    def setUp(self):
        FakeSocket.sent = []
        FakeSocket.commands = []
        FakeSocket.manifest = dict(FakeSocket.manifest, flow=None, modules=[])

    def tearDown(self):
        import gc as real_gc
        sys.modules["gc"] = real_gc
        for name in ("machine", "network", "usocket", "socket",
                     "modules", "modules.link", "modules.linkclient",
                     "modules.linkagent"):
            sys.modules.pop(name, None)

    def one_pass(self, mod, seconds=15):
        """Run the loop until one whole trip has finished, then stop."""
        agent = mod.Agent()
        polls = [0]
        deadline = _real.monotonic() + seconds
        real_call = agent.call

        def call(method, path, body=None, timeout=30):
            if _real.monotonic() > deadline:
                raise Stop()
            if path.startswith("/api/iot/commands"):
                polls[0] += 1
                if polls[0] >= 2:
                    raise Stop()
            return real_call(method, path, body, timeout)

        agent.call = call
        # The work window would otherwise spin for WORK_MS of real time.
        mod.WORK_MS = 1
        try:
            agent.run()
        except Stop:
            pass
        self.assertGreaterEqual(polls[0], 1,
                                "the loop never reached the command poll; it "
                                "was going round catching something")
        self.assertLess(_real.monotonic(), deadline,
                        "the loop did not complete a pass in %ds" % seconds)
        return agent


class TestItGetsAllTheWayRound(LoopCase):
    def test_one_pass_of_the_loop_does_not_raise(self):
        """The whole point."""
        mod = load_agent()
        agent = self.one_pass(mod)
        self.assertTrue(agent.synced, "it never finished a sync")
        self.assertTrue(any("/api/iot/manifest" in s for s in FakeSocket.sent))
        self.assertTrue(any("/api/iot/state" in s for s in FakeSocket.sent))
        self.assertTrue(any("/api/iot/commands" in s for s in FakeSocket.sent))

    def test_the_radio_comes_up_before_the_flow_is_loaded(self):
        """The ordering that took a board off the network once."""
        mod = load_agent(flow={"id": "f", "name": "F", "enabled": True,
                               "nodes": [], "edges": []})
        loaded = []
        mod.Agent.start_flow = lambda self: loaded.append(mod.order[:])
        self.one_pass(mod)
        self.assertTrue(loaded, "start_flow was never called")
        self.assertIn("radio-active", loaded[0],
                      "the flow was loaded before the radio existed")

    def test_it_joins_the_network_before_it_loads_the_flow(self):
        """The ordering that took the motor board off the air for an
        afternoon."""
        mod = load_agent(flow={"id": "f", "name": "F", "enabled": True,
                               "nodes": [], "edges": []})
        seq = []
        real_connect, real_start = mod.Agent.connect, mod.Agent.start_flow

        def connect(self, tries=20):
            seq.append("join")
            return real_connect(self, tries)

        def start_flow(self):
            seq.append("flow")
            return real_start(self)

        mod.Agent.connect, mod.Agent.start_flow = connect, start_flow
        self.one_pass(mod)
        self.assertTrue(seq, "neither happened")
        self.assertEqual(seq[0], "join",
                         "the flow was loaded before the board had joined: %r" % seq)
        self.assertIn("flow", seq, "it joined and never started the flow")

    def flow_import_watcher(self, order, fake):
        """Record when `import flow` is resolved, and hand back `fake`."""
        import importlib.abc
        import importlib.util

        class Loader(importlib.abc.Loader):
            def create_module(self, spec):
                return fake

            def exec_module(self, module):
                pass

        class Finder(importlib.abc.MetaPathFinder):
            def find_spec(self, name, path=None, target=None):
                if name != "flow":
                    return None
                order.append("import")
                return importlib.util.spec_from_loader(name, Loader())

        sys.modules.pop("flow", None)
        sys.meta_path.insert(0, Finder())
        self.addCleanup(sys.meta_path.pop, 0)
        self.addCleanup(sys.modules.pop, "flow", None)

    def fake_runner_module(self):
        import types
        fake = types.ModuleType("flow")

        class Runner:
            def __init__(self, flow, agent):
                self.flow = flow

            def start(self):
                pass

            def tick(self):
                pass

            def pin_states(self):
                return {}

            def take_fired(self):
                return []

            def node_summary(self):
                return {}

        fake.Runner = Runner
        return fake

    def test_the_runner_is_compiled_at_boot_while_the_heap_is_clean(self):
        """The allocation that has actually been killing this board."""
        mod = load_agent(flow={"id": "f", "name": "F", "enabled": True,
                               "nodes": [], "edges": []})
        mod._exists = lambda name: True          # flash holds a flow
        order = []
        self.flow_import_watcher(order, self.fake_runner_module())
        real_start = mod.Agent.start_flow
        mod.Agent.start_flow = lambda self: (order.append("start"),
                                             real_start(self))[1]
        self.one_pass(mod)
        self.assertIn("import", order,
                      "the runner is never imported before the flow starts, so "
                      "the compile lands on whatever heap the boot left behind")
        self.assertIn("start", order)
        self.assertLess(order.index("import"), order.index("start"),
                        "compiled after the network work rather than before: %r"
                        % order)

    def test_a_board_with_nothing_deployed_does_not_compile_the_runner(self):
        """35KB of bytecode is not free, and a board with no flow on its
        flash should not be holding it in case one arrives."""
        mod = load_agent(flow=None)
        asked = []
        mod._exists = lambda name: asked.append(name) or False
        order = []
        self.flow_import_watcher(order, self.fake_runner_module())
        self.one_pass(mod)
        self.assertIn(mod.FLOW, asked,
                      "it never checked whether a flow is even deployed")
        self.assertNotIn("import", order,
                         "it compiled a runner it has no flow for")

    def test_the_join_gets_more_than_one_try_before_the_flow_starts(self):
        """One attempt is not enough on the only power path this bench has."""
        mod = load_agent(flow={"id": "f", "name": "F", "enabled": True,
                               "nodes": [], "edges": []})
        seq = []
        real_start = mod.Agent.start_flow
        tries = [0]

        def connect(self, tries_=20):
            tries[0] += 1
            seq.append("join")
            if tries[0] < 2:            # the first attempt fails, as it does
                return False
            self.online = True
            return True

        def start_flow(self):
            seq.append("flow")
            return real_start(self)

        mod.Agent.connect, mod.Agent.start_flow = connect, start_flow
        self.one_pass(mod)
        self.assertEqual(seq[:3], ["join", "join", "flow"],
                         "the flow started before the join had a second go: %r"
                         % seq)

    def test_a_board_that_cannot_join_still_runs_its_flow(self):
        """A field device does not wait for a server."""
        mod = load_agent(flow={"id": "f", "name": "F", "enabled": True,
                               "nodes": [], "edges": []})
        started = []
        mod.Agent.connect = lambda self, tries=20: False
        mod.WORK_MS = 1

        def start_flow(self):
            started.append(1)
            raise Stop()

        mod.Agent.start_flow = start_flow
        try:
            mod.Agent().run()
        except Stop:
            pass
        self.assertTrue(started, "a board with no network ran nothing")

    def test_it_survives_a_flow_that_is_switched_off(self):
        mod = load_agent(flow={"id": "f", "name": "F", "enabled": False,
                               "nodes": [], "edges": []})
        agent = self.one_pass(mod)
        self.assertIsNone(agent.runner)

    def test_a_flow_that_will_not_start_does_not_take_the_agent_down(self):
        """The boot-time `start_flow()` runs before the radio joins, which
        is deliberate — and therefore before every route by which anyone
        could ask the board to stop running it."""
        mod = load_agent(flow={"id": "f", "name": "F", "enabled": True,
                               "nodes": [], "edges": []})

        def boom(self):
            raise MemoryError("memory allocation failed")

        # Every time, which is the real case: a flow whose handlers do not fit
        # does not start on the second attempt either.
        mod.Agent.start_flow = boom
        agent = self.one_pass(mod)
        self.assertTrue(agent.synced, "the board never got onto the network")
        self.assertIsNone(agent.runner)
        self.assertTrue(any("/api/iot/commands" in s for s in FakeSocket.sent),
                        "it never reached the poll, so it could not be told to "
                        "stop running the flow that broke it")

    def test_what_broke_at_boot_is_reported_once_there_is_a_way_to_say_it(self):
        """Otherwise the only record is a serial line, and a board on its
        own supply has nothing attached to read it."""
        mod = load_agent(flow={"id": "f", "name": "F", "enabled": True,
                               "nodes": [], "edges": []})
        real = mod.Agent.start_flow
        calls = [0]

        def once(self):
            calls[0] += 1
            if calls[0] == 1:                 # the boot call, before the radio
                raise MemoryError("memory allocation failed")
            return real(self)

        mod.Agent.start_flow = once
        said = []
        real_log = mod.Agent.log

        def log(self, level, message, **kw):
            said.append((level, message, self.online, bool(self.token)))
            return real_log(self, level, message, **kw)

        mod.Agent.log = log
        agent = self.one_pass(mod)
        boot = [row for row in said if "would not start" in row[1]]
        self.assertEqual(len(boot), 1,
                         "said %d times, not once: %r" % (len(boot), said))
        self.assertEqual(boot[0][0], "critical")
        self.assertIn("memory allocation failed", boot[0][1])
        self.assertTrue(boot[0][2] and boot[0][3],
                        "said before there was a network to say it on, which "
                        "is where it would be lost")
        self.assertIsNone(agent.boot_error, "it would repeat every pass")

    def test_a_report_never_reads_the_flow_document_back(self):
        """Two short strings must not cost a parse of flow.json per report."""
        doc = {"id": "flow_drive1", "name": "Motor drive", "enabled": False,
               "nodes": [], "edges": []}
        # The manifest hands back the same document, so a whole pass — boot,
        # sync, report — should leave the names right without a single read.
        FakeSocket.manifest = dict(FakeSocket.manifest, flow=doc)
        agent = self.one_pass(load_agent(flow=doc))
        self.assertEqual(agent.flow_id, "flow_drive1")
        self.assertEqual(agent.flow_name, "Motor drive")

        with open(os.path.join(ROOT, "zero2w_console", "agent", "agent.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
        report = src[src.index("    def report("):]
        report = report[:report.index("\n    def ", 1)]
        self.assertNotIn("load_json", report,
                         "a report is reading a file again")

    def test_a_sync_that_replaces_the_flow_updates_what_is_reported(self):
        """The case `start_flow` does not cover."""
        mod = load_agent(flow={"id": "f_old", "name": "Old", "enabled": False,
                               "nodes": [], "edges": []})
        agent = mod.Agent()
        agent.token = "t"
        agent.runner = object()                  # something is already running
        agent.flow_id, agent.flow_name = "f_old", "Old"

        FakeSocket.manifest = dict(FakeSocket.manifest, modules=[],
                                   flow={"id": "f_new", "name": "New",
                                         "enabled": True, "nodes": [],
                                         "edges": []})
        agent.sync()
        self.assertEqual(agent.flow_id, "f_new")
        self.assertEqual(agent.flow_name, "New")

        FakeSocket.manifest = dict(FakeSocket.manifest, flow=None)
        agent.sync()
        self.assertIsNone(agent.flow_id, "it reports a flow it no longer holds")
        self.assertIsNone(agent.flow_name)

    def test_it_survives_a_host_that_offers_no_link(self):
        mod = load_agent()
        agent = self.one_pass(mod)
        self.assertIsNone(agent.link)
        self.assertIsNone(agent.linkpol,
                          "it imported the link policy without being told to")

    def test_it_survives_every_command_the_host_can_send(self):
        """Each one goes through `handle()`, and a typo in any branch is a
        board that stops at the first command it is given."""
        for op in ("resync", "stop", "camera-off", "tags", "fire", "set",
                   "reload", "unknown-op"):
            with self.subTest(op=op):
                self.setUp()
                FakeSocket.commands = [{"op": op, "values": {"a": 1},
                                        "node": "n", "gpio": 27, "value": 0}]
                mod = load_agent()
                self.one_pass(mod)

    def test_a_reboot_is_the_only_one_that_resets_the_board(self):
        mod = load_agent()
        FakeSocket.commands = [{"op": "reboot"}]
        reset = []
        sys.modules["machine"].reset = lambda: reset.append(1)
        self.one_pass(mod)
        self.assertEqual(len(reset), 1)


class TestTheFirstSyncAfterAFlash(LoopCase):
    """The pass that a freshly flashed board actually makes, with real
    files."""

    def setUp(self):
        super().setUp()
        import shutil
        import tempfile
        from zero2w_console import fleet as fleetmod
        self.fleet_sha = fleetmod.sha
        self.dir = tempfile.mkdtemp()
        self.was = os.getcwd()
        # The agent writes to relative paths, which on a board is its flash.
        os.chdir(self.dir)
        self.addCleanup(os.chdir, self.was)
        self.addCleanup(shutil.rmtree, self.dir, True)

    def offer(self, files):
        """Publish these as the manifest's modules, with honest shas."""
        FakeSocket.modules = dict(files)
        FakeSocket.manifest = dict(
            FakeSocket.manifest,
            modules=[{"name": n, "sha": self.fleet_sha(b), "bytes": len(b)}
                     for n, b in files.items()])

    def real_agent_files(self):
        """The actual sizes, so this is not a test against toy inputs."""
        base = os.path.join(ROOT, "zero2w_console", "agent")
        out = {}
        for name in ("agent.py", "flow.py", "modules/drive.py",
                     "modules/expr.py", "modules/pwm.py", "modules/blocks.py"):
            with open(os.path.join(base, name), "rb") as fh:
                out[name] = fh.read()
        return out

    def test_it_pulls_every_module_and_keeps_going(self):
        files = self.real_agent_files()
        self.offer(files)
        mod = load_agent(real_fs=True)
        agent = self.one_pass(mod)
        self.assertTrue(agent.synced)
        for name, blob in files.items():
            with self.subTest(name=name):
                self.assertTrue(os.path.exists(os.path.join(self.dir, name)),
                                "%s never landed" % name)
                with open(os.path.join(self.dir, name), "rb") as fh:
                    self.assertEqual(fh.read(), blob, "%s arrived wrong" % name)

    def test_it_reports_after_that_sync(self):
        """The step the board never reached."""
        self.offer(self.real_agent_files())
        mod = load_agent(real_fs=True)
        self.one_pass(mod)
        self.assertTrue(any("/api/iot/state" in s for s in FakeSocket.sent),
                        "it synced and then never reported")

    def test_the_sha_it_computes_is_the_one_the_manifest_publishes(self):
        """Both sides, on the real files."""
        mod = load_agent(real_fs=True)
        for name, blob in self.real_agent_files().items():
            path = os.path.join(self.dir, "probe.bin")
            with open(path, "wb") as fh:
                fh.write(blob)
            with self.subTest(name=name):
                self.assertEqual(mod._sha_of(path), self.fleet_sha(blob),
                                 "%s: the agent and the host disagree" % name)

    def test_a_module_that_arrives_wrong_is_refused_and_the_rest_continue(self):
        files = self.real_agent_files()
        self.offer(files)
        # The host promises one sha and serves different bytes.
        FakeSocket.modules["modules/pwm.py"] = b"# truncated\n"
        mod = load_agent(real_fs=True)
        agent = self.one_pass(mod)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "modules/pwm.py")),
                         "a module that did not match its sha was installed")
        self.assertFalse(os.path.exists(os.path.join(self.dir,
                                                     "modules/pwm.py.part")),
                         "the part file was left behind")
        # And the sync still finished, so the board is not stuck on one bad file.
        self.assertTrue(agent.synced)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "flow.py")))

    def test_nothing_is_pulled_twice(self):
        """`modules.json` records what landed, so a second pass is quiet."""
        self.offer(self.real_agent_files())
        mod = load_agent(real_fs=True)
        self.one_pass(mod)
        fetched = len([s for s in FakeSocket.sent if "/api/iot/module/" in s])
        self.assertGreater(fetched, 0)
        FakeSocket.sent = []
        agent = mod.Agent()
        agent.online = True
        agent.sync()
        self.assertEqual([s for s in FakeSocket.sent if "/api/iot/module/" in s],
                         [], "it pulled the same modules again")


class TestNothingBlocksLongerThanAWatchdog(LoopCase):
    """The bug that made a motor drive, stop, and drive again in a loop."""

    def test_the_command_poll_asks_for_no_wait(self):
        mod = load_agent()
        self.one_pass(mod)
        polls = [s for s in FakeSocket.sent if "/api/iot/commands" in s]
        self.assertTrue(polls)
        for poll in polls:
            self.assertIn("wait=0", poll,
                          "the agent asked the host to park: %s" % poll)

    def test_the_flow_is_served_between_every_exchange(self):
        """`serve()` is what ticks the flow and answers the camera."""
        mod = load_agent()
        with open(AGENT, encoding="utf-8") as fh:
            body = fh.read()
        loop = body[body.index("    def run("):]
        self.assertGreaterEqual(loop.count("self.serve()"), 3,
                                "not enough serve() calls between exchanges")


if __name__ == "__main__":
    unittest.main()
