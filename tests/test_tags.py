"""The tag table — the board's shared memory."""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from zero2w_console import flows, tags as tagmod  # noqa: E402


class TestCoercion(unittest.TestCase):
    """A payload arrives as whatever produced it; the tag decides its type."""

    def test_anything_number_shaped_becomes_a_number(self):
        self.assertEqual(tagmod.coerce("12", "number"), 12)
        self.assertEqual(tagmod.coerce(12.5, "number"), 12.5)
        self.assertEqual(tagmod.coerce(True, "number"), 1)

    def test_a_whole_float_does_not_keep_its_point(self):
        """72.0 in a table reads as a rounding artefact; 72 reads as a value."""
        self.assertEqual(repr(tagmod.coerce(72.0, "number")), "72")

    def test_something_that_is_not_a_number_says_so(self):
        with self.assertRaises(tagmod.TagError):
            tagmod.coerce("warm", "number")

    def test_bools_take_the_same_words_a_person_would(self):
        for v in (1, "1", "on", "true", "yes", True):
            self.assertTrue(tagmod.coerce(v, "bool"), v)
        for v in (0, "0", "off", "false", "no", "", None, False):
            self.assertFalse(tagmod.coerce(v, "bool"), v)

    def test_text_takes_anything(self):
        self.assertEqual(tagmod.coerce(12, "text"), "12")
        self.assertEqual(tagmod.coerce(None, "text"), "")

    def test_a_tag_cannot_become_a_log_file(self):
        with self.assertRaises(tagmod.TagError):
            tagmod.coerce("x" * 5000, "text")


class TestDefinitions(unittest.TestCase):
    def test_a_name_has_to_be_typeable_into_a_field(self):
        for bad in ("", "2fast", "has space", "has-dash", "a" * 41, "{{x}}"):
            with self.subTest(name=bad):
                with self.assertRaises(tagmod.TagError):
                    tagmod.clean({"name": bad})

    def test_a_reasonable_name_is_accepted(self):
        self.assertEqual(tagmod.clean({"name": "tank_level_2"})["name"],
                         "tank_level_2")

    def test_an_unknown_type_is_refused(self):
        with self.assertRaises(tagmod.TagError):
            tagmod.clean({"name": "x", "type": "struct"})

    def test_a_tag_starts_where_its_type_starts(self):
        self.assertEqual(tagmod.clean({"name": "a", "type": "number"})["initial"], 0)
        self.assertEqual(tagmod.clean({"name": "b", "type": "text"})["initial"], "")
        self.assertIs(tagmod.clean({"name": "c", "type": "bool"})["initial"], False)


class TableCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = tagmod.TagStore(os.path.join(self.dir.name, "tags.json"))
        self.table = tagmod.TagTable(self.store)

    def tearDown(self):
        self.dir.cleanup()

    def define(self, *tags):
        return self.table.define(list(tags))


class TestTheTable(TableCase):
    def test_a_tag_can_be_defined_and_read(self):
        self.define({"name": "level", "type": "number", "initial": 5})
        self.assertEqual(self.table.get("level"), 5)

    def test_writing_it_changes_what_it_holds(self):
        self.define({"name": "level", "type": "number"})
        value, changed = self.table.set("level", "42")
        self.assertEqual(value, 42)
        self.assertTrue(changed)
        self.assertEqual(self.table.get("level"), 42)

    def test_writing_the_same_value_is_not_a_change(self):
        """A trigger fires on movement; rewriting 1 over 1 is not movement."""
        self.define({"name": "level", "type": "number", "initial": 1})
        _v, changed = self.table.set("level", 1)
        self.assertFalse(changed)

    def test_writing_a_tag_that_does_not_exist_says_so(self):
        with self.assertRaises(tagmod.TagError):
            self.table.set("nope", 1)

    def test_two_tags_with_the_same_name_are_refused(self):
        with self.assertRaises(tagmod.TagError):
            self.define({"name": "a", "type": "number"},
                        {"name": "a", "type": "text"})

    def test_redefining_a_tag_keeps_the_value_it_is_holding(self):
        """Editing a description should not blank what the machine knows."""
        self.define({"name": "level", "type": "number"})
        self.table.set("level", 99)
        self.define({"name": "level", "type": "number", "desc": "the tank"})
        self.assertEqual(self.table.get("level"), 99)

    def test_a_tag_dropped_from_the_table_goes_away(self):
        self.define({"name": "a", "type": "number"}, {"name": "b", "type": "number"})
        self.define({"name": "a", "type": "number"})
        self.assertFalse(self.table.has("b"))

    def test_the_listing_carries_the_value_and_the_definition(self):
        self.define({"name": "level", "type": "number", "unit": "%",
                     "desc": "how full"})
        row = self.table.list()[0]
        self.assertEqual(row["name"], "level")
        self.assertEqual(row["unit"], "%")
        self.assertEqual(row["desc"], "how full")
        self.assertIn("value", row)

    def test_a_bad_definition_leaves_the_table_as_it_was(self):
        self.define({"name": "good", "type": "number"})
        with self.assertRaises(tagmod.TagError):
            self.define({"name": "good", "type": "number"},
                        {"name": "2bad", "type": "number"})
        self.assertEqual([t["name"] for t in self.table.list()], ["good"])


class TestRetention(TableCase):
    def test_a_kept_tag_survives_a_restart(self):
        self.define({"name": "hours", "type": "number", "retain": True})
        self.table.set("hours", 1234)
        again = tagmod.TagTable(self.store)          # as if the console restarted
        self.assertEqual(again.get("hours"), 1234)

    def test_an_ordinary_tag_starts_again_at_its_initial_value(self):
        """Most tags describe right now, and right now is not what it was."""
        self.define({"name": "reading", "type": "number", "initial": 0})
        self.table.set("reading", 999)
        again = tagmod.TagTable(self.store)
        self.assertEqual(again.get("reading"), 0)


class TestWatching(TableCase):
    def test_a_change_reaches_a_watcher(self):
        seen = []
        self.table.watch(lambda n, v, p: seen.append((n, v, p)))
        self.define({"name": "level", "type": "number"})
        self.table.set("level", 7)
        self.assertEqual(seen, [("level", 7, 0)])

    def test_a_write_that_changes_nothing_reaches_nobody(self):
        seen = []
        self.define({"name": "level", "type": "number", "initial": 3})
        self.table.watch(lambda n, v, p: seen.append(n))
        self.table.set("level", 3)
        self.assertEqual(seen, [])

    def test_one_watcher_throwing_does_not_stop_the_next(self):
        seen = []

        def bad(n, v, p):
            raise RuntimeError("boom")

        self.table.watch(bad)
        self.table.watch(lambda n, v, p: seen.append(n))
        self.define({"name": "level", "type": "number"})
        self.table.set("level", 1)
        self.assertEqual(seen, ["level"])

    def test_a_watcher_that_raises_typeerror_is_called_once(self):
        """The arity of a watcher is asked once, when it registers."""
        calls = []

        def throws_typeerror(n, v, p, source=None):
            calls.append(n)
            raise TypeError("nothing to do with my signature")

        self.table.watch(throws_typeerror)
        self.define({"name": "level", "type": "number"})
        self.table.set("level", 1)
        self.assertEqual(calls, ["level"], "the watcher ran twice")

    def test_a_three_argument_watcher_still_works(self):
        """Arity detection has to keep the older shape working; both forms
        are in this repo."""
        seen = []
        self.table.watch(lambda n, v, p: seen.append((n, v, p)))
        self.define({"name": "level", "type": "number"})
        self.table.set("level", 4)
        self.assertEqual(seen, [("level", 4, 0)])

    def test_a_four_argument_watcher_is_given_the_source(self):
        seen = []
        self.table.watch(lambda n, v, p, source=None: seen.append(source))
        self.define({"name": "level", "type": "number", "share": True})
        self.table.set("level", 5, source="dev_x")
        self.assertEqual(seen, ["dev_x"])

    def test_a_watcher_that_fails_is_reported_rather_than_swallowed(self):
        """A tag that does not reach the fleet with nothing anywhere saying
        so is the shape of failure that cost this project a day."""
        published = []

        class Bus:
            def publish(self, topic, data):
                published.append((topic, data))

        table = tagmod.TagTable(self.store, bus=Bus())
        table.define([{"name": "level", "type": "number"}])
        table.watch(lambda n, v, p: (_ for _ in ()).throw(RuntimeError("no")))
        table.set("level", 2)
        said = [d for t, d in published if d.get("level") == "critical"]
        self.assertTrue(said, "a failing watcher said nothing: %r" % published)
        self.assertIn("level", said[0]["message"])


class TestTagsInFlows(TableCase):
    """The engine side: reading tags in fields, writing them, and the trigger."""

    def setUp(self):
        super().setUp()
        self.e = flows.FlowEngine(flows.FlowStore(os.devnull), None, None, None)
        self.e.tags = self.table

    def run_node(self, ntype, cfg, payload=1):
        node = {"id": "n", "type": ntype, "config": cfg}
        _p, out = self.e._execute({"id": "f", "name": "f"}, node,
                                  {"payload": payload, "meta": {}})
        return out

    def test_a_field_can_read_a_tag(self):
        self.define({"name": "where", "type": "text", "initial": "shed"})
        out = self.run_node("logic.set", {"value": "it is in the {{tag.where}}"})
        self.assertEqual(out["payload"], "it is in the shed")

    def test_an_unknown_tag_stays_verbatim_like_any_other_typo(self):
        out = self.run_node("logic.set", {"value": "{{tag.nope}}"})
        self.assertEqual(out["payload"], "{{tag.nope}}")

    def test_writing_a_tag_from_a_flow(self):
        self.define({"name": "level", "type": "number"})
        self.run_node("tag.set", {"tag": "level", "value": "{{payload}}"}, 63)
        self.assertEqual(self.table.get("level"), 63)

    def test_a_number_stays_a_number_rather_than_becoming_its_own_string(self):
        """A field holding exactly one variable keeps that value's type."""
        self.define({"name": "level", "type": "text"})
        self.run_node("tag.set", {"tag": "level", "value": "{{payload}}"}, 63)
        self.assertEqual(self.table.get("level"), "63")

    def test_writing_passes_the_message_through_untouched(self):
        self.define({"name": "level", "type": "number"})
        out = self.run_node("tag.set", {"tag": "level", "value": "1"}, "keep me")
        self.assertEqual(out["payload"], "keep me")
        self.assertEqual(out["meta"]["tag"], "level")

    def test_reading_a_tag_into_the_payload(self):
        self.define({"name": "level", "type": "number", "initial": 12})
        out = self.run_node("tag.read", {"tag": "level"}, "replaced")
        self.assertEqual(out["payload"], 12)

    def test_a_missing_tag_table_is_said_out_loud(self):
        self.e.tags = None
        self.assertIsNone(self.run_node("tag.set", {"tag": "x", "value": "1"}))
        self.assertIn("no tag table", self.e.recent()[-1]["message"])


class TestTheTagTrigger(TableCase):
    def setUp(self):
        super().setUp()
        self.e = flows.FlowEngine(flows.FlowStore(os.devnull), None, None, None)
        self.e.tags = self.table

    def arm(self, cfg):
        self.e.tag_triggers = [(("f", "n"), cfg)]

    def fired(self):
        return [self.e.jobs.get_nowait() for _ in range(self.e.jobs.qsize())]

    def test_a_change_starts_a_flow_watching_that_tag(self):
        self.arm({"tag": "level", "when": "changes"})
        self.assertEqual(self.e.on_tag_changed("level", 5, 0), 1)
        key, msg = self.fired()[0]
        self.assertEqual(key, ("f", "n"))
        self.assertEqual(msg["payload"], 5)
        self.assertEqual(msg["meta"]["previous"], 0)

    def test_another_tag_does_not_start_it(self):
        self.arm({"tag": "level", "when": "changes"})
        self.assertEqual(self.e.on_tag_changed("other", 5, 0), 0)

    def test_no_tag_named_watches_all_of_them(self):
        self.arm({"tag": "", "when": "changes"})
        self.assertEqual(self.e.on_tag_changed("anything", 1, 0), 1)

    def test_becoming_true_ignores_the_other_direction(self):
        self.arm({"tag": "run", "when": "becomes true"})
        self.assertEqual(self.e.on_tag_changed("run", True, False), 1)
        self.assertEqual(self.e.on_tag_changed("run", False, True), 0)

    def test_rising_and_falling_compare_numbers(self):
        self.arm({"tag": "level", "when": "rises"})
        self.assertEqual(self.e.on_tag_changed("level", 10, 5), 1)
        self.assertEqual(self.e.on_tag_changed("level", 2, 5), 0)

    def test_rising_on_something_that_is_not_a_number_fires_nothing(self):
        self.arm({"tag": "note", "when": "rises"})
        self.assertEqual(self.e.on_tag_changed("note", "b", "a"), 0)


class TestTagsAppearInThePicker(TableCase):
    def test_a_defined_tag_is_offered_beside_the_built_in_variables(self):
        self.define({"name": "level", "type": "number", "desc": "how full",
                     "unit": "%"})
        self.table.set("level", 42)
        rows = flows.variables_with_values({}, self.table)
        mine = [r for r in rows if r["name"] == "tag.level"]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["group"], "Tags")
        self.assertEqual(mine[0]["value"], "42")
        self.assertEqual(mine[0]["desc"], "how full")

    def test_without_a_table_the_built_in_list_is_unchanged(self):
        self.assertEqual(len(flows.variables_with_values({})), len(flows.VARIABLES))


if __name__ == "__main__":
    unittest.main()
