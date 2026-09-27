"""Precompiled device code: who gets it, how it is built, and how a board
switches over without the old source shadowing it."""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for path in (ROOT, HERE):
    if path not in sys.path:
        sys.path.insert(0, path)

from zero2w_console import fleet as fleetmod                   # noqa: E402

HAVE = fleetmod.compiler() is not None
READY = {"probe": {"firmware": "micropython 1.29.0.", "agent": "0.8.0"}}
READY_AGENT = {"probe": {"firmware": "micropython 1.29.0.", "agent": "0.9.0"}}
FLOW = {"nodes": [{"type": "math.ramp"}, {"type": "logic.step"}]}


class TestVersions(unittest.TestCase):
    def test_what_boards_and_agents_report(self):
        self.assertEqual(fleetmod.version_of("micropython 1.27.0."), (1, 27, 0))
        self.assertEqual(fleetmod.version_of("0.8.0"), (0, 8, 0))
        self.assertEqual(fleetmod.version_of("1.29.0-preview"), (1, 29, 0))
        self.assertEqual(fleetmod.version_of(None), ())


class TestWhoGetsIt(unittest.TestCase):
    def setUp(self):
        self.f = fleetmod.Fleet(None, None, None)
        self.saved = fleetmod.compiler
        fleetmod.compiler = lambda: "/bin/true"

    def tearDown(self):
        fleetmod.compiler = self.saved

    def test_a_ready_board(self):
        self.assertEqual(self.f.compiled_for(READY), 1)
        self.assertEqual(self.f.compiled_for(READY_AGENT), 2)

    def test_an_old_agent_gets_source_until_it_says_otherwise(self):
        old = {"probe": {"firmware": "micropython 1.29.0.", "agent": "0.7.1"}}
        self.assertFalse(self.f.compiled_for(old))
        self.assertEqual(self.f.compiled_for(old, agent_version="0.8.1"), 1,
                         "the version on the manifest request is the one that counts")
        self.assertEqual(self.f.compiled_for(old, agent_version="0.9.0"), 2)

    def test_old_firmware_gets_source(self):
        self.assertFalse(self.f.compiled_for(
            {"probe": {"firmware": "micropython 1.22.2.", "agent": "0.8.0"}}))

    def test_an_unknown_board_gets_source(self):
        self.assertFalse(self.f.compiled_for({}))
        self.assertFalse(self.f.compiled_for(None))

    def test_no_compiler_here_means_source_for_everyone(self):
        fleetmod.compiler = lambda: None
        self.assertFalse(self.f.compiled_for(READY))

    def test_below_0_9_the_agent_stays_source(self):
        names = self.f.agent_files(FLOW, READY, compiled=1)
        self.assertEqual(names[0], "agent.py")
        self.assertTrue(all(n.endswith(".mpy") for n in names[1:]), names)
        self.assertEqual(self.f.agent_files(FLOW, READY),
                         [n[:-4] + ".py" if n.endswith(".mpy") else n for n in names])

    def test_from_0_9_the_fallback_comes_before_the_compiled_agent(self):
        names = self.f.agent_files(FLOW, READY_AGENT, compiled=2)
        self.assertEqual(names[:3], ["main.py", "agent_src.py", "agent.mpy"])
        self.assertNotIn("agent.py", names)
        self.assertTrue(all(n.endswith(".mpy") for n in names[3:]), names)


@unittest.skipUnless(HAVE, "no mpy-cross on this machine")
class TestTheCompiledFiles(unittest.TestCase):
    def setUp(self):
        self.f = fleetmod.Fleet(None, None, None)
        self.dir = tempfile.TemporaryDirectory()
        self.saved = fleetmod.MPY_CACHE
        fleetmod.MPY_CACHE = self.dir.name

    def tearDown(self):
        fleetmod.MPY_CACHE = self.saved
        self.dir.cleanup()

    def test_every_device_file_compiles(self):
        agent = os.path.join(ROOT, "zero2w_console", "agent")
        for root, _dirs, files in os.walk(agent):
            for name in files:
                if not name.endswith(".py") or name == "agent.py":
                    continue
                rel = os.path.relpath(os.path.join(root, name), agent)
                with self.subTest(file=rel):
                    body = self.f.module_source(rel[:-3] + ".mpy")
                    self.assertEqual(body[:2], b"M\x06", "not an mpy v6 file")
                    self.assertLess(len(body), len(self.f.module_source(rel)))

    def test_it_is_cached_by_what_went_in(self):
        first = self.f.module_source("flow.mpy")
        self.assertEqual(len(os.listdir(self.dir.name)), 1)
        self.assertEqual(self.f.module_source("flow.mpy"), first)
        self.assertEqual(len(os.listdir(self.dir.name)), 1)

    def test_the_manifest_names_and_hashes_the_compiled_files(self):
        self.f.flow_for = lambda device: FLOW
        man = self.f.manifest(dict(READY, id="dev_x"))
        names = [m["name"] for m in man["modules"]]
        self.assertIn("flow.mpy", names)
        self.assertIn("agent.py", names)
        mpy = [m for m in man["modules"] if m["name"] == "flow.mpy"][0]
        self.assertEqual(mpy["sha"], fleetmod.sha(self.f.module_source("flow.mpy")))

    def test_agent_src_is_the_agent_s_own_source(self):
        self.assertEqual(self.f.module_source("agent_src.py"),
                         self.f.module_source("agent.py"))
        self.assertEqual(self.f.module_source("agent.mpy")[:2], b"M\x06")

    def test_the_flasher_writes_the_same_bootstrap_it_is_served(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "flasher_under_test", os.path.join(ROOT, "scripts", "iot-flash.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(mod.BOOTSTRAP.encode(), self.f.module_source("main.py"))

    def test_a_compile_that_fails_says_why(self):
        with self.assertRaises(Exception) as caught:
            fleetmod.compile_mpy(b"def (:\n", "broken.py")
        self.assertIn("broken.py", str(caught.exception))


class TestTheBoardSwitchesOver(unittest.TestCase):
    """Read from the agent's source: MicroPython imports x.py before x.mpy."""

    def setUp(self):
        with open(os.path.join(ROOT, "zero2w_console", "agent", "agent.py")) as fh:
            self.src = fh.read()

    def test_the_source_beside_a_new_mpy_is_removed(self):
        self.assertIn('_unlink(name[:-4] + ".py")', self.src)

    def test_it_says_which_agent_it_is_when_it_asks(self):
        self.assertIn('"/api/iot/manifest?v=" + VERSION', self.src)

    def test_the_agent_version_is_the_one_that_does_this(self):
        import re
        v = re.search(r'VERSION = "([^"]+)"', self.src).group(1)
        self.assertGreaterEqual(fleetmod.version_of(v), fleetmod.MPY_AGENT)

    def test_new_code_for_a_loaded_mpy_module_restarts_the_board(self):
        self.assertIn('name.rsplit(".", 1)[0].replace("/", ".") in sys.modules',
                      self.src)


class TestInternalRamIsReported(unittest.TestCase):
    """gc.mem_free() counts heap grown out of internal RAM as free, so a board
    that spent 59KB compiling looks no worse; the report carries both."""

    def test_the_report_and_the_card_carry_it(self):
        with open(os.path.join(ROOT, "zero2w_console", "agent", "agent.py")) as fh:
            agent = fh.read()
        self.assertIn('"idf_free": _idf_free()', agent)
        self.assertIn("h[0] < 1048576", agent, "PSRAM must not count as internal")
        with open(os.path.join(ROOT, "zero2w_console", "static", "iot.js")) as fh:
            self.assertIn("st.idf_free", fh.read())

    def test_the_console_keeps_it(self):
        """The report is whitelisted; a field left off it arrives as None."""
        f = fleetmod.Fleet(None, None, None)
        saved = {}
        f.devices = type("S", (), {"load": lambda s: {"devices": [{"id": "d"}]},
                                   "save": lambda s, doc: saved.update(doc)})()
        f.report_state({"id": "d"}, {"idf_free": 91234, "free_ram": 1})
        self.assertEqual(saved["devices"][0]["state"]["idf_free"], 91234)


class TestTheBootstrapFallsBack(unittest.TestCase):
    """main.py, run for real, against agents that do and do not load."""

    def boot(self, compiled_ok):
        import runpy
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "agent.py"), "w") as fh:
                fh.write("started = []\ndef main():\n    started.append('compiled')\n"
                         if compiled_ok else "raise ValueError('incompatible .mpy file')\n")
            with open(os.path.join(d, "agent_src.py"), "w") as fh:
                fh.write("BOOT_NOTE = None\nstarted = []\n"
                         "def main():\n    started.append('source')\n")
            sys.path.insert(0, d)
            try:
                runpy.run_path(os.path.join(ROOT, "zero2w_console", "agent", "main.py"))
                return sys.modules["agent"]
            finally:
                sys.path.remove(d)
                for name in ("agent", "agent_src"):
                    sys.modules.pop(name, None)

    def test_the_compiled_agent_runs_when_it_loads(self):
        self.assertEqual(self.boot(True).started, ["compiled"])

    def test_the_source_runs_when_it_does_not_and_says_why(self):
        agent = self.boot(False)
        self.assertEqual(agent.started, ["source"])
        self.assertIn("incompatible .mpy file", agent.BOOT_NOTE)
        self.assertIs(sys.modules.get("agent", agent), agent,
                      "`import agent` elsewhere must find the fallback")


class TestTheAgentKeepsItsFallback(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, "zero2w_console", "agent", "agent.py")) as fh:
            self.src = fh.read()

    def test_no_compiled_agent_without_its_source_on_flash(self):
        self.assertIn('if name == "agent.mpy" and not _exists("agent_src.py"):', self.src)

    def test_a_fallback_is_reported(self):
        self.assertIn("self.boot_error = BOOT_NOTE", self.src)

    def test_a_damaged_compiled_agent_is_fetched_again_and_an_intact_one_is_not(self):
        """Refetching an intact file the firmware refuses would reboot into
        the same failure for ever."""
        self.assertIn('if BOOT_NOTE and _sha_of("agent.mpy") != have.get("agent.mpy"):',
                      self.src)


class TestTheFlasherRestartsTheBoard(unittest.TestCase):
    def test_it_resets_by_hardware_after_writing(self):
        """Ctrl-D in the raw REPL runs the buffer; it left a board sitting at
        the prompt after --code-only until someone reset it."""
        with open(os.path.join(ROOT, "scripts", "iot-flash.py")) as fh:
            src = fh.read()
        self.assertNotIn('serial.write(b"\\r\\x04")', src)
        for part in ("def code_only(", 'say("5. handing it its identity")'):
            body = src[src.index(part):]
            body = body[:body.index("finally:")]
            with self.subTest(path=part):
                self.assertIn("serial.reset_into_run()", body)


class TestTheRealSyncSwitchesOver(unittest.TestCase):
    """The agent's own sync, against a manifest naming flow.mpy."""

    def setUp(self):
        from test_agent_loop import FakeSocket, load_agent
        self.FakeSocket, self.load_agent = FakeSocket, load_agent
        self.dir = tempfile.TemporaryDirectory()
        self.was = os.getcwd()
        os.chdir(self.dir.name)
        FakeSocket.sent, FakeSocket.commands = [], []
        FakeSocket.manifest = dict(FakeSocket.manifest, flow=None, modules=[
            {"name": "flow.mpy", "sha": "2" * 64, "bytes": 10000}])

    def tearDown(self):
        os.chdir(self.was)
        self.dir.cleanup()
        import gc as real_gc
        sys.modules["gc"] = real_gc
        self.FakeSocket.manifest = dict(self.FakeSocket.manifest, modules=[])
        for name in ("machine", "network", "usocket", "socket"):
            sys.modules.pop(name, None)

    def test_the_old_source_goes_when_the_bytecode_lands(self):
        with open("flow.py", "w") as fh:
            fh.write("# the old runner\n")
        mod = self.load_agent()
        real = mod.request

        def request(method, url, body=None, headers=None, timeout=30, to_file=None):
            if "/api/iot/module/" in url:
                with open(to_file, "wb") as fh:
                    fh.write(b"M\x06\x00\x1f")
                return 200, b""
            return real(method, url, body, headers, timeout, to_file)

        mod.request = request
        mod._sha_of = lambda path: "2" * 64
        agent = mod.Agent()
        agent.log = lambda *a, **kw: None
        agent.sync()
        self.assertTrue(os.path.exists("flow.mpy"))
        self.assertFalse(os.path.exists("flow.py"),
                         "flow.py would be imported instead of flow.mpy")
        asked = [s for s in self.FakeSocket.sent if "/api/iot/manifest" in s]
        self.assertTrue(asked and "manifest?v=" in asked[0], asked[:1])

    def fetching(self, names):
        """Run one sync with these files on offer; which did it fetch?"""
        self.FakeSocket.manifest = dict(self.FakeSocket.manifest, modules=[
            {"name": n, "sha": "2" * 64, "bytes": 100} for n in names])
        mod = self.load_agent()
        real, got = mod.request, []

        def request(method, url, body=None, headers=None, timeout=30, to_file=None):
            if "/api/iot/module/" in url:
                got.append(url.rsplit("/api/iot/module/", 1)[1])
                with open(to_file, "wb") as fh:
                    fh.write(b"x")
                return 200, b""
            return real(method, url, body, headers, timeout, to_file)

        mod.request = request
        mod._sha_of = lambda path: "2" * 64
        agent = mod.Agent()
        agent.log = lambda *a, **kw: None
        agent.sync()
        return got

    def test_the_compiled_agent_waits_for_its_fallback(self):
        with open("agent.py", "w") as fh:
            fh.write("# source\n")
        self.assertEqual(self.fetching(["agent.mpy"]), [])
        self.assertTrue(os.path.exists("agent.py"), "the only agent was removed")

    def test_running_from_source_refetches_a_damaged_agent_mpy(self):
        for name, body in (("agent_src.py", "# source\n"), ("agent.mpy", "damaged")):
            with open(name, "w") as fh:
                fh.write(body)
        with open("modules.json", "w") as fh:
            fh.write('{"agent.mpy": "%s"}' % ("2" * 64))
        mod = self.load_agent()
        mod.BOOT_NOTE = "compiled agent would not load (test)"
        real_sha = mod._sha_of
        self.FakeSocket.manifest = dict(self.FakeSocket.manifest, modules=[
            {"name": "agent.mpy", "sha": "2" * 64, "bytes": 100}])
        got, real = [], mod.request

        def request(method, url, body=None, headers=None, timeout=30, to_file=None):
            if "/api/iot/module/" in url:
                got.append(url)
                with open(to_file, "wb") as fh:
                    fh.write(b"fresh")
                return 200, b""
            return real(method, url, body, headers, timeout, to_file)

        mod.request = request
        mod._sha_of = lambda path: "2" * 64 if path.endswith(".part") else real_sha(path)
        agent = mod.Agent()
        agent.log = lambda *a, **kw: None
        agent.sync()
        self.assertEqual(len(got), 1, "the damaged agent.mpy was not fetched again")

    def test_in_manifest_order_both_land_in_one_sync(self):
        with open("agent.py", "w") as fh:
            fh.write("# source\n")
        self.assertEqual(self.fetching(["main.py", "agent_src.py", "agent.mpy"]),
                         ["main.py", "agent_src.py", "agent.mpy"])
        self.assertFalse(os.path.exists("agent.py"))
        self.assertTrue(os.path.exists("agent_src.py"))


if __name__ == "__main__":
    unittest.main()
