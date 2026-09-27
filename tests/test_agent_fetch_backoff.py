"""A module that will not download must not eat the loop.

A failed fetch is left alone for FETCH_RETRY, so a board that cannot move 30KB
keeps reporting instead of retrying forever. docs/CONTEXT.md §5.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for path in (ROOT, HERE):
    if path not in sys.path:
        sys.path.insert(0, path)

from test_agent_loop import FakeSocket, load_agent    # noqa: E402

BIG = {"name": "agent.py", "sha": "0" * 64, "bytes": 30149}


class BackoffCase(unittest.TestCase):
    def setUp(self):
        FakeSocket.sent = []
        FakeSocket.commands = []
        FakeSocket.manifest = dict(FakeSocket.manifest, flow=None,
                                   modules=[dict(BIG)])

    def tearDown(self):
        import gc as real_gc
        sys.modules["gc"] = real_gc
        FakeSocket.manifest = dict(FakeSocket.manifest, modules=[])
        for name in ("machine", "network", "usocket", "socket"):
            sys.modules.pop(name, None)

    def agent(self):
        """An agent whose module fetches always time out, and nothing else."""
        mod = load_agent()
        tried, logged = [], []
        real_request = mod.request

        def request(method, url, *args, **kw):
            if "/api/iot/module/" in url:
                tried.append(url)
                raise OSError(116, "ETIMEDOUT")
            return real_request(method, url, *args, **kw)

        mod.request = request
        mod._unlink = lambda path: None
        agent = mod.Agent()
        agent.log = lambda level, text: logged.append((level, text))
        return agent, tried, logged


class TestItGivesUpForAWhile(BackoffCase):
    def test_the_first_failure_is_tried_and_reported(self):
        agent, tried, logged = self.agent()
        agent.sync()
        self.assertEqual(len(tried), 1)
        self.assertEqual([l for l, _ in logged], ["warn"])

    def test_the_line_says_how_big_it_was_and_what_to_do(self):
        """The size is the reason, and the cable is the answer.

        "could not fetch agent.py: [Errno 116] ETIMEDOUT" was true and left the
        reader with nowhere to go.
        """
        agent, _tried, logged = self.agent()
        agent.sync()
        text = logged[0][1]
        self.assertIn("agent.py", text)
        self.assertIn("30149", text)
        self.assertIn("USB", text)

    def test_the_next_sync_does_not_try_it_again(self):
        agent, tried, _logged = self.agent()
        agent.sync()
        agent.sync()
        agent.sync()
        self.assertEqual(len(tried), 1,
                         "a doomed 30KB fetch was retried on every sync, which "
                         "is what stopped the board reporting")

    def test_and_says_nothing_more_about_it_either(self):
        """One line per backoff, not one per sync: the log is how a board that
        is still alive is told apart from one that is not."""
        agent, _tried, logged = self.agent()
        for _ in range(4):
            agent.sync()
        self.assertEqual(len(logged), 1)

    def test_it_tries_again_once_the_wait_is_up(self):
        agent, tried, _logged = self.agent()
        agent.sync()
        agent.fetch_wait["agent.py"] = 0
        agent.sync()
        self.assertEqual(len(tried), 2)

    def test_the_backoff_is_long_enough_to_report_through(self):
        """A board reports every two seconds, and the point of waiting is that a
        run of them gets out before it tries again."""
        mod = load_agent()
        self.assertGreaterEqual(mod.FETCH_RETRY, 60)

    def test_a_sync_still_pulls_what_it_can(self):
        """The small modules came down fine on the board that could not manage
        the big ones, so one failure must not abandon the rest."""
        agent, tried, _logged = self.agent()
        FakeSocket.manifest = dict(
            FakeSocket.manifest,
            modules=[dict(BIG), {"name": "modules/gpio.py",
                                 "sha": "1" * 64, "bytes": 685}])
        agent.sync()
        self.assertEqual(len(tried), 2, "the second module was never attempted")


class TestItDoesNotForgetASuccess(BackoffCase):
    def test_a_module_that_lands_clears_its_wait(self):
        agent, _tried, _logged = self.agent()
        agent.fetch_wait["modules/gpio.py"] = 0
        with open(os.path.join(ROOT, "zero2w_console", "agent", "agent.py"),
                  encoding="utf-8") as fh:
            self.assertIn("fetch_wait.pop", fh.read(),
                          "a module that arrives must stop being backed off")


if __name__ == "__main__":
    unittest.main()
