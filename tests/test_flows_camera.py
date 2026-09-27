"""What puts a device's picture on the wall, and what it costs to fetch."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from zero2w_console import flows  # noqa: E402


def open_agent(name):
    with open(os.path.join(ROOT, "zero2w_console", "agent", name)) as fh:
        return fh.read()


def flow(*nodes):
    return {"id": "f", "name": "f", "nodes": list(nodes), "edges": []}


def node(nid, ntype, **cfg):
    return {"id": nid, "type": ntype, "config": cfg}


class TestWhatReachesTheScreen(unittest.TestCase):
    def test_a_publish_node_says_so_outright(self):
        f = flow(node("c1", "camera.feed"), node("p1", "camera.publish"))
        self.assertEqual(flows.feeds_the_screen(f)["id"], "p1")

    def test_a_bare_feed_reaches_nothing(self):
        """Nothing is on the wall for existing in a graph — only for being
        wired to something that sends it there."""
        self.assertIsNone(flows.feeds_the_screen(flow(node("c1", "camera.feed"))))

    def test_an_old_display_setting_no_longer_counts(self):
        """It was how this worked; a flow relying on it needs rewiring."""
        f = flow(node("c1", "camera.feed", display="yes", label="Bench eye"))
        self.assertIsNone(flows.feeds_the_screen(f))

    def test_a_publish_node_counts_however_the_feed_is_configured(self):
        f = flow(node("c1", "camera.feed", display="no"),
                 node("p1", "camera.publish"))
        self.assertEqual(flows.feeds_the_screen(f)["id"], "p1")

    def test_a_capture_node_alone_does_not_put_anything_on_the_wall(self):
        self.assertIsNone(flows.feeds_the_screen(flow(node("c1", "camera.capture"))))

    def test_a_flow_with_no_camera_at_all_is_not_on_the_wall(self):
        self.assertIsNone(flows.feeds_the_screen(flow(node("t1", "timer.interval"))))

    def test_nothing_at_all_is_handled(self):
        self.assertIsNone(flows.feeds_the_screen(None))
        self.assertIsNone(flows.feeds_the_screen({}))


class TestHowOftenTheWallAsks(unittest.TestCase):
    def test_no_publish_node_means_the_screen_picks(self):
        self.assertIsNone(flows.screen_interval_ms(flow(node("c1", "camera.feed"))))

    def test_a_publish_node_without_a_setting_means_the_screen_picks(self):
        f = flow(node("p1", "camera.publish"))
        self.assertIsNone(flows.screen_interval_ms(f))

    def test_a_chosen_interval_is_used(self):
        f = flow(node("p1", "camera.publish", every_ms=5000))
        self.assertEqual(flows.screen_interval_ms(f), 5000)

    def test_asking_absurdly_fast_is_clamped_to_something_achievable(self):
        """A frame is captured on demand and sent uncompressed; 10ms is a lie."""
        f = flow(node("p1", "camera.publish", every_ms=10))
        self.assertEqual(flows.screen_interval_ms(f), flows.CAMERA_MIN_MS)

    def test_asking_absurdly_slowly_is_clamped_too(self):
        f = flow(node("p1", "camera.publish", every_ms=999999))
        self.assertEqual(flows.screen_interval_ms(f), 60000)

    def test_a_value_that_is_not_a_number_does_not_raise(self):
        f = flow(node("p1", "camera.publish", every_ms="soon"))
        self.assertIsNone(flows.screen_interval_ms(f))


class TestThePublishNodeItself(unittest.TestCase):
    def test_a_device_can_run_it(self):
        """It has to live in the device's flow: the host reads it from there."""
        self.assertTrue(flows.runs_on_device("camera.publish"))

    def test_its_floor_matches_the_one_the_browser_uses(self):
        path = os.path.join(ROOT, "zero2w_console", "static", "cameras-core.js")
        with open(path) as fh:
            core = fh.read()
        self.assertIn("beat: %d" % flows.CAMERA_MIN_MS, core)

    def test_the_feed_node_does_not_claim_it_needs_no_wiring(self):
        docs = " ".join(flows.REGISTRY["camera.feed"]["docs"])
        self.assertNotIn("does not need to be wired", docs)

    def test_the_feed_node_offers_no_display_field(self):
        """Camera to screen does that."""
        keys = {f["key"] for f in flows.REGISTRY["camera.feed"]["fields"]}
        self.assertNotIn("display", keys)

    def test_a_feed_does_not_start_the_sensor_by_existing(self):
        """The device runner only reports that a camera *could* be wanted;
        the sensor comes up when a message reaches the feed node."""
        src = open_agent("flow.py")
        self.assertIn("def has_camera_node", src)
        self.assertNotIn("def camera_setup", src)

    def test_the_agent_does_not_start_the_camera_at_boot(self):
        src = open_agent("agent.py")
        self.assertNotIn("camera_setup", src)


if __name__ == "__main__":
    unittest.main()
