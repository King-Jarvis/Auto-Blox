"""The example flows, checked against the registry they are made of."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from zero2w_console import examples, flows  # noqa: E402


def port_names(spec, which):
    return [p if isinstance(p, str) else p["name"] for p in (spec.get(which) or [])]


class TestTheCatalogue(unittest.TestCase):
    def setUp(self):
        self.all = examples.catalogue()

    def test_there_are_some(self):
        self.assertGreaterEqual(len(self.all), 8)

    def test_each_has_an_id_a_name_and_an_explanation(self):
        for ex in self.all:
            with self.subTest(example=ex["id"]):
                self.assertTrue(ex["id"].startswith("ex_"))
                self.assertTrue(ex["name"])
                self.assertGreater(len(ex["about"]), 40,
                                   "an example has to say what it is showing")

    def test_they_all_arrive_switched_off(self):
        """An example names pins that are a guess about someone else's
        board."""
        for ex in self.all:
            with self.subTest(example=ex["id"]):
                self.assertFalse(ex["enabled"])

    def test_the_ids_are_unique(self):
        ids = [e["id"] for e in self.all]
        self.assertEqual(len(ids), len(set(ids)))

    def test_one_can_be_fetched_by_id(self):
        self.assertIsNotNone(examples.by_id("ex_blink"))
        self.assertIsNone(examples.by_id("ex_nope"))


class TestEveryExampleIsValid(unittest.TestCase):
    def setUp(self):
        self.all = examples.catalogue()

    def test_every_node_type_exists(self):
        for ex in self.all:
            for node in ex["nodes"]:
                with self.subTest(example=ex["id"], node=node["id"]):
                    self.assertIn(node["type"], flows.REGISTRY)

    def test_every_config_key_is_a_field_on_that_node(self):
        for ex in self.all:
            for node in ex["nodes"]:
                spec = flows.REGISTRY[node["type"]]
                fields = {f["key"] for f in spec["fields"]}
                for key in node.get("config") or {}:
                    with self.subTest(example=ex["id"], node=node["type"], key=key):
                        self.assertIn(key, fields)

    def test_every_dropdown_value_is_one_the_field_offers(self):
        for ex in self.all:
            for node in ex["nodes"]:
                spec = flows.REGISTRY[node["type"]]
                for field in spec["fields"]:
                    options = field.get("options")
                    value = (node.get("config") or {}).get(field["key"])
                    if not options or value is None:
                        continue
                    with self.subTest(example=ex["id"], node=node["type"],
                                      field=field["key"]):
                        self.assertIn(value, options)

    def test_every_edge_joins_ports_that_exist(self):
        for ex in self.all:
            ids = {n["id"]: n for n in ex["nodes"]}
            for edge in ex["edges"]:
                with self.subTest(example=ex["id"], edge=edge["id"]):
                    self.assertIn(edge["from"], ids)
                    self.assertIn(edge["to"], ids)
                    src = flows.REGISTRY[ids[edge["from"]]["type"]]
                    dst = flows.REGISTRY[ids[edge["to"]]["type"]]
                    self.assertIn(edge["fromPort"], port_names(src, "outputs"))
                    self.assertIn(edge["toPort"], port_names(dst, "inputs"))

    def test_nothing_is_wired_into_a_trigger(self):
        for ex in self.all:
            ids = {n["id"]: n["type"] for n in ex["nodes"]}
            for edge in ex["edges"]:
                with self.subTest(example=ex["id"], edge=edge["id"]):
                    self.assertTrue(
                        flows.REGISTRY[ids[edge["to"]]]["inputs"],
                        "an edge lands on a trigger, which has no inputs")

    def test_every_node_is_connected_to_something(self):
        """No node acts by existing, so a loose one in an example is a bug."""
        for ex in self.all:
            joined = set()
            for edge in ex["edges"]:
                joined.add(edge["from"])
                joined.add(edge["to"])
            for node in ex["nodes"]:
                with self.subTest(example=ex["id"], node=node["id"]):
                    self.assertIn(node["id"], joined)

    def test_a_device_example_only_uses_nodes_a_device_can_run(self):
        for ex in self.all:
            if not ex.get("board"):
                continue
            with self.subTest(example=ex["id"]):
                self.assertEqual(ex["unsupported"], [],
                                 "a board flow cannot contain host-only nodes")

    def test_a_device_example_names_a_board_that_exists(self):
        from zero2w_console import iot as iotmod
        for ex in self.all:
            if not ex.get("board"):
                continue
            with self.subTest(example=ex["id"]):
                self.assertIsNotNone(iotmod.board_profile(ex["board"]))

    def test_the_tags_an_example_wants_are_well_formed(self):
        from zero2w_console import tags as tagmod
        for ex in self.all:
            for tag in ex.get("needs_tags") or []:
                with self.subTest(example=ex["id"], tag=tag.get("name")):
                    tagmod.clean(tag)          # raises if it is not usable

    def test_a_tag_an_example_writes_is_one_it_asks_for(self):
        """Otherwise opening the example leaves a node pointing at nothing."""
        for ex in self.all:
            wanted = {t["name"] for t in ex.get("needs_tags") or []}
            for node in ex["nodes"]:
                cfg = node.get("config") or {}
                name = cfg.get("tag")
                if node["type"] not in ("tag.set", "tag.read") or not name:
                    continue
                with self.subTest(example=ex["id"], tag=name):
                    self.assertIn(name, wanted)


class TestTheyActuallyRun(unittest.TestCase):
    """Walking a graph is cheap; proving the host engine accepts these
    shapes is what stops an example that only looks right."""

    def test_the_host_examples_load_into_an_engine_and_arm(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            store = flows.FlowStore(os.path.join(d, "flows.json"))
            host = [e for e in examples.catalogue() if not e.get("board")]
            store.save({"flows": [
                {k: v for k, v in e.items()
                 if k in ("id", "name", "enabled", "nodes", "edges")}
                for e in host]})
            engine = flows.FlowEngine(store, None, None, None)
            try:
                engine.doc = store.load()
                # Arming reaches every trigger type these examples use.
                for flow in engine.doc["flows"]:
                    engine._arm(flow)
                self.assertTrue(engine.webhooks)
            finally:
                engine._teardown()


if __name__ == "__main__":
    unittest.main()
