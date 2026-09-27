"""How the agent pulls a module, and how big a module may get."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

AGENT_DIR = os.path.join(ROOT, "zero2w_console", "agent")


def source(name):
    with open(os.path.join(AGENT_DIR, name)) as fh:
        return fh.read()


class TestModulesStreamToFlash(unittest.TestCase):
    def setUp(self):
        self.agent = source("agent.py")

    def test_request_can_write_straight_to_a_file(self):
        self.assertIn("def request(", self.agent)
        self.assertIn("to_file", self.agent)

    def test_sync_uses_it_rather_than_holding_the_module_in_memory(self):
        body = self.agent[self.agent.index("def sync("):]
        body = body[:body.index("\n    def ")]
        self.assertIn("to_file=", body,
                      "sync must stream a module, not buffer it")

    def test_a_module_lands_through_a_part_file(self):
        """A transfer that dies half way must leave the old module in place."""
        body = self.agent[self.agent.index("def sync("):]
        body = body[:body.index("\n    def ")]
        self.assertIn(".part", body)
        self.assertIn("os.rename(", body)

    def test_the_body_goes_to_the_file_without_being_gathered(self):
        """The whole point. The body is the part that is 31KB."""
        branch = self.agent[self.agent.index("if to_file:"):]
        branch = branch[:branch.index("return status, b\"\"")]
        writing = branch[branch.index("with open(to_file"):]
        self.assertIn("fh.write(chunk)", writing)
        self.assertNotIn("+= chunk", writing)

    def test_the_headers_are_gathered_but_bounded(self):
        """They do accumulate, which is fine — they are a few hundred bytes,
        and the loop gives up at 4KB rather than trusting the other end."""
        branch = self.agent[self.agent.index("if to_file:"):]
        branch = branch[:branch.index("with open(to_file")]
        self.assertIn("head_raw += chunk", branch)
        self.assertIn("> 4096", branch)

    def test_a_failed_fetch_is_caught_rather_than_ending_the_sync(self):
        """One module that will not download must not stop the board reporting."""
        body = self.agent[self.agent.index("def sync("):]
        body = body[:body.index("\n    def ")]
        self.assertIn("except Exception", body)


class TestTheFilesStayFetchable(unittest.TestCase):
    """A budget, not a measured limit."""

    BUDGET = 40 * 1024

    def test_no_agent_file_has_quietly_doubled(self):
        for name in ("agent.py", "flow.py"):
            with self.subTest(name=name):
                size = os.path.getsize(os.path.join(AGENT_DIR, name))
                self.assertLess(size, self.BUDGET,
                                "%s is %d bytes; split it rather than raising "
                                "this" % (name, size))

    def test_a_pulled_module_stays_small(self):
        """These are optional extras; none should rival the runner."""
        mods = os.path.join(AGENT_DIR, "modules")
        for name in sorted(os.listdir(mods)):
            if not name.endswith(".py"):
                continue
            with self.subTest(name=name):
                self.assertLess(os.path.getsize(os.path.join(mods, name)),
                                16 * 1024)

    def test_a_device_pulls_only_what_its_flow_needs(self):
        """The reason a budget is workable at all."""
        from zero2w_console import fleet as fleetmod
        blink = {"nodes": [{"id": "t", "type": "timer.interval", "config": {}},
                           {"id": "g", "type": "gpio.out", "config": {}}]}
        names = fleetmod.Fleet(None, None, None).agent_files(blink, {})
        self.assertNotIn("modules/expr.py", names)
        self.assertNotIn("modules/camera.py", names)


if __name__ == "__main__":
    unittest.main()
