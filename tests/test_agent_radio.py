"""Transmit power, and why a field agent needs a dial for it."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tests.test_agent_loop import LoopCase, load_agent    # noqa: E402


class RadioCase(LoopCase):
    def radio_with(self, cfg):
        """Bring the interface up under `cfg` and report what was configured."""
        mod = load_agent(cfg=cfg)
        seen = {}
        wlan_cls = mod.network.WLAN
        real_config = wlan_cls.config

        def config(self, **kw):
            seen.update(kw)
            return real_config(self, **kw)

        wlan_cls.config = config
        try:
            agent = mod.Agent()
            agent.radio()
            self.assertIsNotNone(agent.wlan, "the interface never came up")
        finally:
            wlan_cls.config = real_config
        return seen


class TestTransmitPower(RadioCase):
    def test_it_is_left_alone_when_nothing_asks(self):
        """Absent means the firmware's default."""
        self.assertNotIn("txpower", self.radio_with({}))

    def test_a_number_reaches_the_radio(self):
        self.assertEqual(self.radio_with({"txpower": 11}).get("txpower"), 11.0)

    def test_a_string_from_a_json_config_still_works(self):
        """Config travels as JSON and a screen may well send "11"."""
        self.assertEqual(self.radio_with({"txpower": "11"}).get("txpower"), 11.0)

    def test_power_save_is_still_turned_off(self):
        """The existing setting has to survive the new one: a station that
        sleeps between beacons adds a wait to every round trip."""
        self.assertIn("pm", self.radio_with({"txpower": 11}))

    def test_rubbish_does_not_stop_the_radio_coming_up(self):
        """Being wrong about this must never be how a board goes silent — it
        is applied before there is any way to correct it remotely."""
        for bad in ("loud", None, "", [], {}, float("nan")):
            with self.subTest(txpower=bad):
                mod = load_agent(cfg={"txpower": bad})
                agent = mod.Agent()
                agent.radio()
                self.assertIsNotNone(agent.wlan,
                                     "txpower=%r took the interface down" % bad)


class TestItIsConfigurable(unittest.TestCase):
    def test_a_device_record_may_carry_it(self):
        from zero2w_console import iot as iotmod
        self.assertIn("txpower", iotmod.EDITABLE)

    def test_the_flasher_writes_it_into_the_config(self):
        """It is read before the first sync, so the manifest is too late."""
        with open(os.path.join(ROOT, "scripts", "iot-flash.py")) as fh:
            src = fh.read()
        block = src[src.index("    config = {"):]
        block = block[:block.index("if a.dry_run")]
        self.assertIn("txpower", block)


if __name__ == "__main__":
    unittest.main()
