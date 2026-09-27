"""Shared tags, across the fleet."""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from zero2w_console import fleet as fleetmod, tags as tagmod  # noqa: E402


class FakeStore:
    def __init__(self, devices):
        self._devices = devices

    def load(self):
        return {"devices": self._devices}

    def save(self, doc):
        self._devices = doc["devices"]
        return doc


class Fleet(fleetmod.Fleet):
    """A fleet with devices and no network."""

    def __init__(self, table, devices):
        fleetmod.Fleet.__init__(self, FakeStore(devices), None, None)
        self.tags = table
        self.sent = []

    def push(self, device_id, command):
        self.sent.append((device_id, command))
        return command

    def touch(self, *a, **kw):
        pass

    def _emit(self, *a, **kw):
        pass


class Case(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        store = tagmod.TagStore(os.path.join(self.dir.name, "tags.json"))
        self.table = tagmod.TagTable(store)
        self.table.define([
            {"name": "setpoint", "type": "number", "share": True},
            {"name": "mine", "type": "number"},          # not shared
        ])
        self.fleet = Fleet(self.table, [
            {"id": "dev_a", "token": "ta"},
            {"id": "dev_b", "token": "tb"},
            {"id": "dev_none"},                          # never enrolled
        ])
        # The wiring the server does: a shared tag moving goes to the fleet.
        self.table.watch(lambda n, v, p, source=None:
                         self.fleet.broadcast_tag(n, v, source)
                         if self.table.is_shared(n) else None)

    def tearDown(self):
        self.dir.cleanup()


class TestWhatGetsShared(Case):
    def test_only_the_tags_marked_shared(self):
        self.assertEqual(set(self.table.shared()), {"setpoint"})

    def test_a_shared_tag_carries_its_value(self):
        self.table.set("setpoint", 21)
        self.assertEqual(self.table.shared()["setpoint"], 21)

    def test_the_manifest_hands_a_device_the_current_numbers(self):
        """A board that has just come up starts where the fleet is, not
        where its flow's defaults were."""
        self.table.set("setpoint", 19)
        man = self.fleet.manifest({"id": "dev_a", "board": None})
        self.assertEqual(man["tags"], {"setpoint": 19})

    def test_a_private_tag_is_not_in_it(self):
        self.table.set("mine", 5)
        man = self.fleet.manifest({"id": "dev_a", "board": None})
        self.assertNotIn("mine", man["tags"])


class TestGoingOut(Case):
    def test_a_change_here_reaches_every_enrolled_device(self):
        self.table.set("setpoint", 23)
        went = [d for d, cmd in self.fleet.sent if cmd["op"] == "tags"]
        self.assertEqual(sorted(went), ["dev_a", "dev_b"])

    def test_a_device_that_never_enrolled_is_skipped(self):
        """It has no token, so it is a record rather than a board."""
        self.table.set("setpoint", 23)
        self.assertNotIn("dev_none", [d for d, _ in self.fleet.sent])

    def test_the_command_carries_the_name_and_the_value(self):
        self.table.set("setpoint", 23)
        _dev, cmd = self.fleet.sent[0]
        self.assertEqual(cmd, {"op": "tags", "values": {"setpoint": 23}})

    def test_a_private_tag_goes_nowhere(self):
        self.table.set("mine", 7)
        self.assertEqual(self.fleet.sent, [])

    def test_writing_the_same_value_again_sends_nothing(self):
        self.table.set("setpoint", 23)
        self.fleet.sent = []
        self.table.set("setpoint", 23)
        self.assertEqual(self.fleet.sent, [])


class TestComingBack(Case):
    def test_a_device_write_is_taken(self):
        self.fleet.apply_device_tags({"id": "dev_a"}, {"setpoint": 25})
        self.assertEqual(self.table.get("setpoint"), 25)

    def test_and_passed_on_to_the_others(self):
        self.fleet.apply_device_tags({"id": "dev_a"}, {"setpoint": 25})
        self.assertEqual([d for d, _ in self.fleet.sent], ["dev_b"])

    def test_but_never_back_to_the_one_that_sent_it(self):
        """The echo a device would have to ignore itself."""
        self.fleet.apply_device_tags({"id": "dev_a"}, {"setpoint": 25})
        self.assertNotIn("dev_a", [d for d, _ in self.fleet.sent])

    def test_a_device_cannot_write_a_tag_this_board_keeps_to_itself(self):
        self.fleet.apply_device_tags({"id": "dev_a"}, {"mine": 99})
        self.assertEqual(self.table.get("mine"), 0)

    def test_a_device_cannot_invent_a_tag(self):
        self.fleet.apply_device_tags({"id": "dev_a"}, {"nonsense": 1})
        self.assertFalse(self.table.has("nonsense"))

    def test_a_value_of_the_wrong_shape_is_refused_not_fatal(self):
        took = self.fleet.apply_device_tags({"id": "dev_a"},
                                            {"setpoint": "warm"})
        self.assertEqual(took, 0)
        self.assertEqual(self.table.get("setpoint"), 0)

    def test_rubbish_instead_of_a_table_is_ignored(self):
        self.assertEqual(self.fleet.apply_device_tags({"id": "dev_a"}, "no"), 0)

    def test_a_write_that_changes_nothing_is_not_passed_on(self):
        self.table.set("setpoint", 25)
        self.fleet.sent = []
        self.fleet.apply_device_tags({"id": "dev_a"}, {"setpoint": 25})
        self.assertEqual(self.fleet.sent, [])


class TestABootPutsTheArmBack(Case):
    """A board must not come up armed because the fleet still says armed."""

    def setUp(self):
        Case.setUp(self)
        self.table.define([
            {"name": "setpoint", "type": "number", "share": True},
            {"name": "mine", "type": "number", "boot_reset": True},
            {"name": "armed", "type": "number", "share": True,
             "boot_reset": True, "initial": 0},
        ])
        self.table.set("setpoint", 21)
        self.table.set("armed", 1)
        self.table.set("mine", 5)
        self.fleet.sent = []

    def test_the_flag_survives_validation(self):
        self.assertTrue(tagmod.clean({"name": "a", "boot_reset": True})["boot_reset"])
        self.assertFalse(tagmod.clean({"name": "a"})["boot_reset"])

    def test_booting_returns_it_to_its_initial_value(self):
        self.assertEqual(self.fleet.booted({"id": "dev_a"}), ["armed"])
        self.assertEqual(self.table.get("armed"), 0)
        man = self.fleet.manifest({"id": "dev_a", "board": None})
        self.assertEqual(man["tags"]["armed"], 0)

    def test_every_other_board_is_told(self):
        self.fleet.booted({"id": "dev_a"})
        told = [d for d, cmd in self.fleet.sent
                if cmd == {"op": "tags", "values": {"armed": 0}}]
        self.assertIn("dev_b", told)

    def test_tags_without_the_flag_keep_their_value(self):
        self.fleet.booted({"id": "dev_a"})
        self.assertEqual(self.table.get("setpoint"), 21)

    def test_a_private_tag_is_not_the_fleet_s_to_reset(self):
        self.fleet.booted({"id": "dev_a"})
        self.assertEqual(self.table.get("mine"), 5)

    def test_already_at_initial_is_quiet(self):
        self.fleet.booted({"id": "dev_a"})
        self.fleet.sent = []
        self.assertEqual(self.fleet.booted({"id": "dev_b"}), [])
        self.assertEqual(self.fleet.sent, [])

    def test_only_the_first_sync_after_power_on_says_boot(self):
        with open(os.path.join(ROOT, "zero2w_console", "agent", "agent.py")) as fh:
            agent = fh.read()
        self.assertIn('("" if self.synced else "&boot=1")', agent)
        with open(os.path.join(ROOT, "zero2w_console", "server.py")) as fh:
            server = fh.read()
        at = server.index('if path == "/api/iot/manifest":')
        self.assertIn('if qs.get("boot"):', server[at:at + 300])


class TestTheAgentSide(unittest.TestCase):
    """Read from the source: the device is MicroPython and cannot be
    imported here, but these are the three things that make the loop
    terminate."""

    def setUp(self):
        with open(os.path.join(ROOT, "zero2w_console", "agent", "agent.py")) as fh:
            self.agent = fh.read()
        with open(os.path.join(ROOT, "zero2w_console", "agent", "flow.py")) as fh:
            self.flow = fh.read()

    def test_it_takes_pushed_values(self):
        self.assertIn('op == "tags"', self.agent)

    def test_it_does_not_report_what_it_was_just_told(self):
        """Only what it wrote itself goes home, or the loop never ends."""
        body = self.agent[self.agent.index('elif op == "tags"'):]
        body = body[:body.index("elif op ==", 10)]
        self.assertIn("self.tags[name] = value", body)
        self.assertNotIn("tags_written", body)

    def test_a_write_on_the_device_is_queued_for_the_next_report(self):
        self.assertIn("self.agent.tags_written[name] = value", self.flow)

    def test_and_cleared_once_it_has_gone(self):
        self.assertIn("self.tags_written = {}", self.agent)

    def test_a_sync_does_not_undo_a_write_that_has_not_been_sent_yet(self):
        self.assertIn("if name not in self.tags_written", self.agent)


if __name__ == "__main__":
    unittest.main()
