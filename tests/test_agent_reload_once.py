"""The same deploy, arriving twice, must start the flow once.

Enable queues two reloads (the save and the server's own), and a restart costs
heap that only a reboot gives back. The agent is where it is made idempotent,
since `reload` is retryable on purpose. docs/CONTEXT.md §5.
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

FLOW = {"id": "flow_motor1", "name": "Motor drive", "enabled": True,
        "nodes": [], "edges": []}


class ReloadCase(unittest.TestCase):
    def setUp(self):
        FakeSocket.sent = []
        FakeSocket.commands = []
        FakeSocket.manifest = dict(FakeSocket.manifest, modules=[],
                                   flow=dict(FLOW), flow_sha="a" * 64)

    def tearDown(self):
        import gc as real_gc
        sys.modules["gc"] = real_gc
        FakeSocket.manifest = dict(FakeSocket.manifest, flow=None)
        FakeSocket.manifest.pop("flow_sha", None)
        for name in ("machine", "network", "usocket", "socket"):
            sys.modules.pop(name, None)

    def agent(self):
        """An agent that records every flow start instead of making one."""
        mod = load_agent(flow=dict(FLOW))
        starts = []

        class Runner:
            def __init__(self, flow, _agent):
                self.flow = flow
                self.stopped = False
                starts.append(flow.get("id"))

            def start(self):
                pass

            def stop(self):
                self.stopped = True

            def tick(self):
                pass

        flowmod = type(sys)("flow")
        flowmod.Runner = Runner
        sys.modules["flow"] = flowmod
        self.addCleanup(sys.modules.pop, "flow", None)
        agent = mod.Agent()
        agent.log = lambda level, text: None
        return agent, starts, mod


class TestOneDocumentIsOneStart(ReloadCase):
    def test_a_single_reload_starts_it(self):
        agent, starts, _mod = self.agent()
        agent.handle({"op": "reload"})
        self.assertEqual(starts, ["flow_motor1"])

    def test_the_second_identical_reload_does_not(self):
        agent, starts, _mod = self.agent()
        agent.handle({"op": "reload"})
        agent.handle({"op": "reload"})
        self.assertEqual(len(starts), 1,
                         "one Enable click sends two reloads; acting on both "
                         "cost ~14KB of heap that only a reboot returns")

    def test_nor_do_four_of_them(self):
        agent, starts, _mod = self.agent()
        for _ in range(4):
            agent.handle({"op": "reload"})
        self.assertEqual(len(starts), 1)

    def test_a_changed_document_does_start_again(self):
        """Idempotence must not become deafness: an edited flow has to land."""
        agent, starts, _mod = self.agent()
        agent.handle({"op": "reload"})
        FakeSocket.manifest = dict(FakeSocket.manifest, flow_sha="b" * 64)
        agent.handle({"op": "reload"})
        self.assertEqual(len(starts), 2)

    def test_the_old_runner_is_stopped_before_the_new_one_starts(self):
        agent, _starts, _mod = self.agent()
        agent.handle({"op": "reload"})
        first = agent.runner
        FakeSocket.manifest = dict(FakeSocket.manifest, flow_sha="b" * 64)
        agent.handle({"op": "reload"})
        self.assertTrue(first.stopped)
        self.assertIsNot(agent.runner, first)

    def test_a_flow_switched_off_stops_and_stays_stopped(self):
        """`sync` writes flow.json before `try_flow` reads it, so the two agree
        about `enabled`; the harness has to write it too."""
        agent, starts, mod = self.agent()
        agent.handle({"op": "reload"})
        off = dict(FLOW, enabled=False)
        FakeSocket.manifest = dict(FakeSocket.manifest, flow=off,
                                   flow_sha="c" * 64)
        mod.load_json = lambda path, default=None: (
            off if path == mod.FLOW else (default if default is not None else {}))
        agent.handle({"op": "reload"})
        self.assertIsNone(agent.runner)
        self.assertEqual(len(starts), 1)

    def test_a_reload_that_cannot_start_does_not_escape(self):
        """A flow that will not start is reported, not thrown out of the poll."""
        agent, _starts, _mod = self.agent()
        agent.start_flow = lambda: (_ for _ in ()).throw(RuntimeError("no room"))
        agent.handle({"op": "reload"})
        self.assertIsNone(agent.runner)
        self.assertIn("no room", agent.boot_error)


if __name__ == "__main__":
    unittest.main()
