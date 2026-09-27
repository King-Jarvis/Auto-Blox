"""What each node says it produces."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from zero2w_console import flows  # noqa: E402

KINDS = {"number", "text", "bool", "list", "bytes", "any", "same"}


class TestEveryNodeDeclaresItsOutput(unittest.TestCase):
    def test_every_type_has_one(self):
        for ntype in flows.REGISTRY:
            with self.subTest(node=ntype):
                self.assertTrue(flows.REGISTRY[ntype].get("emits"))

    def test_nothing_is_declared_for_a_node_that_does_not_exist(self):
        self.assertEqual(set(flows.EMITS) - set(flows.REGISTRY), set())

    def test_the_table_covers_every_type_rather_than_leaning_on_the_default(self):
        """PASSES_THROUGH is a real answer, but it has to be chosen."""
        self.assertEqual(set(flows.EMITS), set(flows.REGISTRY))

    def test_each_field_is_named_typed_and_described(self):
        for ntype, spec in flows.REGISTRY.items():
            for field in spec["emits"]:
                with self.subTest(node=ntype, field=field.get("name")):
                    self.assertTrue(field.get("name"))
                    self.assertIn(field.get("kind"), KINDS)
                    self.assertTrue((field.get("desc") or "").strip())
                    self.assertTrue(field["desc"].strip().endswith("."),
                                    "a description reads as a sentence")

    def test_fields_name_somewhere_a_message_actually_has(self):
        """payload, meta, or a key inside meta — nothing else exists."""
        for ntype, spec in flows.REGISTRY.items():
            for field in spec["emits"]:
                name = field["name"]
                with self.subTest(node=ntype, field=name):
                    self.assertTrue(
                        name in ("payload", "meta") or name.startswith("meta."),
                        "%r is not part of a message" % name)

    def test_every_node_says_what_its_payload_is(self):
        for ntype, spec in flows.REGISTRY.items():
            with self.subTest(node=ntype):
                self.assertIn("payload", [f["name"] for f in spec["emits"]])

    def test_a_trigger_describes_the_message_it_starts(self):
        """A trigger has nothing incoming, so "unchanged" would be meaningless."""
        for ntype, spec in flows.REGISTRY.items():
            if spec["kind"] != "trigger":
                continue
            with self.subTest(node=ntype):
                kinds = [f["kind"] for f in spec["emits"]]
                self.assertNotIn("same", kinds)


class TestTheDeclarationsMatchTheEngine(unittest.TestCase):
    """Spot-checks against what the code actually builds, for the shapes
    worth getting wrong: a trigger's meta is what the flow after it
    reads."""

    def emitted(self, ntype):
        return {f["name"] for f in flows.REGISTRY[ntype]["emits"]}

    def test_a_pin_edge_carries_its_pin_and_direction(self):
        self.assertEqual(self.emitted("gpio.in"),
                         {"payload", "meta.gpio", "meta.edge"})

    def test_a_webhook_carries_its_path(self):
        self.assertIn("meta.webhook", self.emitted("http.webhook"))

    def test_a_threshold_carries_the_reading_and_the_line(self):
        self.assertEqual(self.emitted("metric.threshold"),
                         {"payload", "meta.metric", "meta.threshold"})

    def test_a_subscription_carries_the_exact_topic(self):
        """A wildcard subscription is useless without knowing what matched."""
        self.assertIn("meta.topic", self.emitted("mqtt.subscribe"))

    def test_a_device_event_carries_which_device_and_what_kind(self):
        self.assertIn("meta.device", self.emitted("host.event"))
        self.assertIn("meta.kind", self.emitted("host.event"))

    def test_an_http_request_carries_its_status(self):
        self.assertIn("meta.status", self.emitted("http.request"))

    def test_a_shell_command_carries_its_exit_code(self):
        self.assertIn("meta.code", self.emitted("shell.run"))

    def test_a_sub_flow_call_carries_the_depth_guard(self):
        self.assertIn("meta._depth", self.emitted("flow.call"))


if __name__ == "__main__":
    unittest.main()
