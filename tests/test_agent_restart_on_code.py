"""New code for a module the board is running needs a reboot to take effect."""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for path in (ROOT, HERE):
    if path not in sys.path:
        sys.path.insert(0, path)

from test_agent_loop import FakeSocket, load_agent    # noqa: E402

GPIO = {"name": "modules/gpio.py", "sha": "1" * 64, "bytes": 685}


class RestartCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.was = os.getcwd()
        os.chdir(self.dir.name)
        FakeSocket.sent = []
        FakeSocket.commands = []
        FakeSocket.manifest = dict(FakeSocket.manifest, flow=None,
                                   modules=[dict(GPIO)])

    def tearDown(self):
        os.chdir(self.was)
        self.dir.cleanup()
        import gc as real_gc
        sys.modules["gc"] = real_gc
        FakeSocket.manifest = dict(FakeSocket.manifest, modules=[])
        sys.modules.pop("modules.gpio", None)
        for name in ("machine", "network", "usocket", "socket"):
            sys.modules.pop(name, None)

    def agent(self, lands=True):
        mod = load_agent()
        resets, logged = [], []
        real_request = mod.request

        def request(method, url, body=None, headers=None, timeout=30, to_file=None):
            if "/api/iot/module/" in url:
                if not lands:
                    raise OSError(116, "ETIMEDOUT")
                os.makedirs(os.path.dirname(to_file) or ".", exist_ok=True)
                with open(to_file, "w") as fh:
                    fh.write("# new\n")
                return 200, b""
            return real_request(method, url, body, headers, timeout, to_file)

        mod.request = request
        mod._sha_of = lambda path: GPIO["sha"]
        mod.machine.reset = lambda: resets.append(1)
        agent = mod.Agent()
        agent.log = lambda level, text: logged.append((level, text))
        return agent, resets, logged


class TestTheBoardRestartsForNewCode(RestartCase):
    def test_a_loaded_module_that_changed_restarts_the_board(self):
        sys.modules["modules.gpio"] = type(sys)("modules.gpio")
        agent, resets, logged = self.agent()
        agent.sync()
        self.assertEqual(resets, [1])
        self.assertTrue([t for l, t in logged if l == "warn" and "restarting" in t])

    def test_a_module_nothing_has_imported_yet_does_not(self):
        agent, resets, _logged = self.agent()
        agent.sync()
        self.assertEqual(resets, [])

    def test_a_fetch_that_failed_does_not(self):
        sys.modules["modules.gpio"] = type(sys)("modules.gpio")
        agent, resets, _logged = self.agent(lands=False)
        agent.sync()
        self.assertEqual(resets, [])


if __name__ == "__main__":
    unittest.main()
