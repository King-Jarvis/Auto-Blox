"""A deploy to a board that is already running something has to take effect."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tests.test_agent_loop import FakeSocket, LoopCase, load_agent   # noqa: E402

OLD = {"id": "f_old", "name": "Old", "enabled": True, "nodes": [], "edges": []}
NEW = {"id": "f_new", "name": "New", "enabled": True, "nodes": [], "edges": []}


class Redeploy(LoopCase):
    """An agent with a runner already up, and a `flow.json` that has moved on."""

    def agent_with(self, running, on_flash):
        self.started, self.stopped = [], []
        test = self
        mod = load_agent(flow=on_flash)

        class Runner:
            def __init__(self, flow, agent):
                self.flow = flow
                test.started.append(flow.get("id"))

            def start(self):
                pass

            def stop(self):
                test.stopped.append(self.flow.get("id"))

        import types
        fake = types.ModuleType("flow")
        fake.Runner = Runner
        sys.modules["flow"] = fake
        self.addCleanup(sys.modules.pop, "flow", None)

        agent = mod.Agent()
        if running is not None:
            agent.runner = Runner(running, agent)
            self.started = []          # that one was the setup, not the subject
        # What a sync would have just written, which is what `try_flow` compares.
        agent.flow_id = (on_flash or {}).get("id")
        agent.flow_name = (on_flash or {}).get("name")
        agent.flow_on = bool(on_flash) and on_flash.get("enabled") is not False
        return agent


class TestADeployReplacesWhatIsRunning(Redeploy):
    def test_the_old_flow_is_stopped_and_the_new_one_started(self):
        agent = self.agent_with(running=OLD, on_flash=NEW)
        agent.try_flow()
        self.assertEqual(self.stopped, ["f_old"], "the old runner kept going")
        self.assertEqual(self.started, ["f_new"])
        self.assertEqual(agent.runner.flow.get("id"), "f_new")

    def test_the_same_flow_is_not_restarted_for_nothing(self):
        """Restarting on every sync would drop a motor's ramp state and re-
        claim every pin on each reconnect."""
        agent = self.agent_with(running=OLD, on_flash=OLD)
        agent.try_flow()
        self.assertEqual(self.stopped, [])
        self.assertEqual(self.started, [])

    def test_a_flow_switched_off_stops_the_one_that_is_running(self):
        agent = self.agent_with(running=OLD, on_flash=dict(OLD, enabled=False))
        agent.try_flow()
        self.assertEqual(self.stopped, ["f_old"])
        self.assertIsNone(agent.runner, "a disabled flow went on running")

    def test_a_flow_removed_altogether_stops_it(self):
        agent = self.agent_with(running=OLD, on_flash=None)
        agent.try_flow()
        self.assertEqual(self.stopped, ["f_old"])
        self.assertIsNone(agent.runner)

    def test_nothing_running_just_starts_it(self):
        agent = self.agent_with(running=None, on_flash=NEW)
        agent.try_flow()
        self.assertEqual(self.stopped, [])
        self.assertEqual(self.started, ["f_new"])

    def test_a_flow_that_will_not_start_is_survived_and_named(self):
        """Both callers are upstream of the command poll."""
        agent = self.agent_with(running=None, on_flash=NEW)
        agent.start_flow = lambda: (_ for _ in ()).throw(MemoryError("no room"))
        agent.try_flow()
        self.assertIsNone(agent.runner)
        self.assertIn("no room", agent.boot_error)

    def test_what_is_reported_is_what_is_running(self):
        """The field that made a failed deploy look successful."""
        agent = self.agent_with(running=OLD, on_flash=NEW)
        agent.try_flow()
        self.assertEqual(agent.runner.flow.get("id"), agent.flow_id)


