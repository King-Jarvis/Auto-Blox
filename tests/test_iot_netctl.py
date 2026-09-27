"""The privileged helper: what it accepts, what it refuses, what it
generates."""
import importlib.machinery
import importlib.util
import ipaddress
import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HELPER = os.path.join(ROOT, "packaging", "iot-netctl")
SUDOERS = os.path.join(ROOT, "packaging", "zero2w-iot.sudoers")


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def load_helper():
    # Loading it by path otherwise drops a __pycache__ beside a file that is
    # installed as root-owned.
    sys.dont_write_bytecode = True
    loader = importlib.machinery.SourceFileLoader("iot_netctl", HELPER)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


h = load_helper()

GOOD = {"ssid": "zero2w-iot", "psk": "correct-horse-battery", "channel": 6,
        "subnet": "10.42.0.0/24", "interface": "wlan0"}


class TestValidation(unittest.TestCase):
    def test_a_good_config_survives(self):
        cfg = h.validate(dict(GOOD))
        self.assertEqual(cfg["ssid"], "zero2w-iot")
        self.assertEqual(cfg["channel"], 6)
        self.assertTrue(cfg["isolate"], "stations must not reach each other by default")
        self.assertFalse(cfg["lan_access"], "the LAN must be off-limits by default")

    def _refused(self, **over):
        cfg = dict(GOOD)
        cfg.update(over)
        with self.assertRaises(h.Invalid):
            h.plan(h.validate(cfg))

    def test_refusals(self):
        self._refused(ssid="")
        self._refused(ssid="a" * 33)
        self._refused(psk="short")
        self._refused(psk="x" * 64)
        self._refused(channel=36)          # 5 GHz: no ESP32 can see it
        self._refused(channel=3)           # overlaps 1 and 6
        self._refused(subnet="8.8.8.0/24")     # public
        self._refused(subnet="10.42.0.5/24")   # host bits set
        self._refused(subnet="10.42.0.0/30")   # no room for a pool
        self._refused(interface="../../etc/passwd")
        self._refused(interface="definitely-not-an-interface")
        self._refused(country="United States")

    def test_a_newline_cannot_smuggle_a_config_directive(self):
        """hostapd.conf is line-based: an SSID with a newline would inject one."""
        with self.assertRaises(h.Invalid):
            h.validate(dict(GOOD, ssid="mynet\nignore_broadcast_ssid=1"))
        with self.assertRaises(h.Invalid):
            h.validate(dict(GOOD, psk="password\nwpa=0"))

    def test_the_uplink_cannot_be_the_ap_itself(self):
        self._refused(uplink="wlan0")

    def test_plan_derives_addresses_inside_the_subnet(self):
        p = h.plan(h.validate(dict(GOOD)))
        net = ipaddress.ip_network(GOOD["subnet"])
        for key in ("gateway", "dhcp_start", "dhcp_end"):
            self.assertIn(ipaddress.ip_address(p[key]), net, key)
        self.assertNotEqual(p["gateway"], p["dhcp_start"],
                            "the gateway must not be inside the pool")


class TestGeneratedConfig(unittest.TestCase):
    def setUp(self):
        self.cfg = h.validate(dict(GOOD))
        self.plan = h.plan(self.cfg)

    def test_hostapd_is_wpa2_ccmp_only(self):
        conf = h.hostapd_conf(self.cfg, self.plan)
        self.assertIn("wpa=2", conf)
        self.assertIn("rsn_pairwise=CCMP", conf)
        self.assertNotIn("TKIP", conf)
        self.assertIn("auth_algs=1", conf)          # no shared-key WEP
        self.assertIn("ap_isolate=1", conf)
        self.assertIn("hw_mode=g", conf)            # 2.4 GHz, the only band the fleet sees
        self.assertNotIn("hw_mode=a", conf)

    def test_dnsmasq_binds_only_the_ap_interface(self):
        conf = h.dnsmasq_conf(self.cfg, self.plan)
        self.assertIn("interface=wlan0", conf)
        self.assertIn("bind-interfaces", conf)
        self.assertIn("dhcp-range=%s,%s" % (self.plan["dhcp_start"], self.plan["dhcp_end"]), conf)

    def test_every_generated_line_is_a_single_line(self):
        for conf in (h.hostapd_conf(self.cfg, self.plan), h.dnsmasq_conf(self.cfg, self.plan)):
            for line in conf.splitlines():
                self.assertNotIn("\r", line)


class TestCommandSurface(unittest.TestCase):
    def test_the_verb_set_is_exactly_this(self):
        """Two lists on purpose: `serve` is how systemd starts the helper as
        root, and must never be something the console can ask for."""
        self.assertEqual(sorted(h.VERBS),
                         ["clients", "config", "diagnose", "down", "firewall",
                          "radio-off", "radio-on", "serve", "status", "up"])
        self.assertEqual(sorted(h.REMOTE),
                         ["clients", "config", "diagnose", "down", "firewall",
                          "radio-off", "radio-on", "status", "up"])
        self.assertNotIn("serve", h.REMOTE)

    def test_every_acting_verb_requires_root(self):
        """Reading is harmless; anything that changes the machine is not."""
        for verb in ("config", "up", "down", "firewall", "radio-on",
                     "radio-off", "serve"):
            self.assertIn(verb, h.NEEDS_ROOT)

    def test_the_sudoers_entry_and_the_helper_agree(self):
        """A verb the console can ask for but sudoers lacks fails at runtime
        as a password prompt, which is a confusing way to find out."""
        text = read(SUDOERS)
        for verb in h.REMOTE:
            self.assertIn("/usr/local/sbin/iot-netctl %s" % verb, text, verb)
        self.assertNotIn("iot-netctl serve", text)

    def test_the_console_whitelist_matches_the_helper(self):
        sys.path.insert(0, ROOT)
        from zero2w_console import iot
        self.assertEqual(sorted(iot.HELPER_VERBS), sorted(h.REMOTE))

    def test_an_unknown_verb_is_refused(self):
        p = subprocess.run([sys.executable, HELPER, "rm-rf"], capture_output=True, text=True)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("usage", (p.stderr + p.stdout).lower())

    def test_dry_run_needs_no_root_and_writes_nothing(self):
        before = os.path.exists(h.HOSTAPD_CONF)
        p = subprocess.run([sys.executable, HELPER, "up", "--dry-run"],
                           input=json.dumps(GOOD), capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("wpa=2", p.stdout)
        self.assertNotIn(GOOD["psk"], p.stdout, "the passphrase must never be printed")
        self.assertEqual(os.path.exists(h.HOSTAPD_CONF), before)

    def test_dry_run_refuses_a_bad_config(self):
        p = subprocess.run([sys.executable, HELPER, "up", "--dry-run"],
                           input=json.dumps(dict(GOOD, channel=36)),
                           capture_output=True, text=True)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("rejected", p.stderr + p.stdout)

    def test_the_helper_imports_nothing_from_this_repo(self):
        """Root must never execute code the console's user can rewrite."""
        import ast
        tree = ast.parse(read(HELPER), "iot-netctl")
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [(node.module or "").split(".")[0]]
            for name in names:
                self.assertNotEqual(name, "zero2w_console")
                self.assertIn(name, sys.stdlib_module_names, name)


class TestMissingTools(unittest.TestCase):
    """A board without iw or hostapd gets told so, not a traceback."""

    def setUp(self):
        self.h = load_helper()

    def test_a_tool_that_is_not_installed_answers_127(self):
        p = self.h.run(["zero2w-no-such-tool", "x"], check=False, quiet=True)
        self.assertEqual(p.returncode, 127)
        self.assertIn("not installed", p.stderr)

    def test_asked_to_succeed_it_stops_with_the_reason(self):
        with self.assertRaises(SystemExit) as caught:
            self.h.run(["zero2w-no-such-tool"], quiet=True)
        self.assertIn("not installed", str(caught.exception))


class TestSudoers(unittest.TestCase):
    def test_it_grants_exactly_the_five_verbs(self):
        text = read(SUDOERS)
        self.assertIn("NOPASSWD:", text)
        for verb in ("config", "up", "down", "radio-on", "radio-off", "status", "clients"):
            self.assertIn("/usr/local/sbin/iot-netctl %s" % verb, text)
        # A bare path would let any argument through, including none.
        self.assertNotIn("iot-netctl\n", text)
        self.assertNotIn("ALL\n", text)

    def test_it_names_no_shell(self):
        """Only the rules matter — a comment may say anything."""
        rules = "\n".join(line for line in read(SUDOERS).splitlines()
                          if line.strip() and not line.lstrip().startswith("#"))
        for danger in ("/bin/sh", "/bin/bash", "ALL=(ALL) NOPASSWD: ALL", "*", "iot-netctl serve"):
            self.assertNotIn(danger, rules)


if __name__ == "__main__":
    unittest.main()


class TestLinks(unittest.TestCase):
    """What can carry the IoT network, and what each kind needs."""

    def test_a_wire_needs_no_name_and_no_passphrase(self):
        cfg = h.validate({"subnet": "10.42.0.0/24", "interface": "lo"})
        self.assertFalse(cfg["wireless"])
        self.assertIsNone(cfg["ssid"])
        self.assertIsNone(cfg["psk"])
        self.assertIsNone(cfg["channel"])

    def test_a_radio_still_needs_all_three(self):
        real = h.is_wireless
        try:
            h.is_wireless = lambda iface: True
            with self.assertRaises(h.Invalid):
                h.validate({"subnet": "10.42.0.0/24", "interface": "lo"})
            with self.assertRaises(h.Invalid):
                h.validate({"subnet": "10.42.0.0/24", "interface": "lo",
                            "ssid": "x", "psk": "12345678"})       # no channel
            cfg = h.validate({"subnet": "10.42.0.0/24", "interface": "lo",
                              "ssid": "x", "psk": "12345678", "channel": 6})
            self.assertTrue(cfg["wireless"])
        finally:
            h.is_wireless = real

    def test_an_interface_that_does_not_exist_is_refused(self):
        with self.assertRaises(h.Invalid):
            h.validate({"subnet": "10.42.0.0/24", "interface": "definitely-not-here"})

    def test_candidates_say_what_each_one_would_need(self):
        for link in h.link_candidates():
            with self.subTest(interface=link["interface"]):
                self.assertIn(link["kind"], ("wired", "wireless"))
                if link["kind"] == "wired":
                    self.assertIn("address", link["needs"])
                else:
                    self.assertIn("hostapd", link["needs"])

    def test_loopback_and_container_interfaces_are_not_offered(self):
        names = [l["interface"] for l in h.link_candidates()]
        self.assertNotIn("lo", names)
        for name in names:
            self.assertFalse(name.startswith(("docker", "veth", "p2p-")))

    def test_dnsmasq_binds_the_interface_that_is_serving(self):
        cfg = h.validate({"subnet": "10.42.0.0/24", "interface": "lo"})
        conf = h.dnsmasq_conf(cfg, h.plan(cfg))
        self.assertIn("interface=lo", conf)
        self.assertIn("bind-interfaces", conf)


class TestVerbsResolve(unittest.TestCase):
    """Every verb the socket offers must actually call something."""

    def test_every_remote_verb_calls_a_function_that_exists(self):
        import inspect
        source = read(HELPER)
        for verb in h.REMOTE:
            fn = h.REMOTE[verb]
            with self.subTest(verb=verb):
                body = inspect.getsource(fn)
                for name in ("do_config", "do_up", "do_down", "do_firewall",
                             "do_radio", "do_status", "do_clients",
                             "do_diagnose"):
                    if name + "(" in body:
                        self.assertTrue(callable(getattr(h, name, None)),
                                        "%s calls %s, which is not defined" % (verb, name))
                        self.assertIn("def %s(" % name, source)
                        break
                else:
                    self.fail("%s does not call a do_* function" % verb)

    def test_every_cli_verb_calls_a_function_that_exists(self):
        for verb, fn in h.VERBS.items():
            with self.subTest(verb=verb):
                self.assertTrue(callable(fn))

    def test_the_acting_verbs_are_all_present(self):
        for name in ("do_up", "do_down", "do_firewall", "do_config",
                     "do_status", "do_clients", "do_diagnose"):
            self.assertTrue(callable(getattr(h, name, None)), name)


class TestWhatADeviceMayReach(unittest.TestCase):
    """The isolation rule, and the ports named above it."""

    def test_the_link_port_agrees_with_the_console(self):
        """The helper is root-owned and imports nothing from this repo, so
        the number is written twice."""
        from zero2w_console import link as linkmod
        self.assertEqual(h.LINK_PORT, linkmod.PORT)

    def test_the_console_port_agrees_too(self):
        self.assertEqual(h.CONSOLE_PORT, 8787)

    def test_both_ports_are_in_the_accept_list(self):
        source = read(HELPER)
        rules = source[source.index("def firewall_up("):]
        rules = rules[:rules.index("\ndef ")]
        self.assertIn("CONSOLE_PORT", rules)
        self.assertIn("LINK_PORT", rules)
        # And the catch-all is still under them, or naming ports means nothing.
        self.assertIn('"-j", "REJECT"', rules)
        self.assertLess(rules.index("LINK_PORT"),
                        rules.index('run([ipt, "-A", CHAIN_IN, "-i", wlan, "-j", "REJECT"])'),
                        "the accept rules have to come before the reject")

    def rules(self):
        """The iptables commands `firewall_up` would actually run."""
        called = []

        class Done:
            """What `run` hands back: `firewall_down` reads .stdout off it."""
            stdout = ""
            returncode = 0

        def fake_run(cmd, **kw):
            called.append(cmd)
            return Done()

        saved_run, saved_which = h.run, h.shutil.which
        h.run = fake_run
        h.shutil.which = lambda name: "/sbin/" + name
        try:
            cfg = h.validate(dict(GOOD))
            h.firewall_up(cfg, h.plan(cfg), iface="wlan0")
        finally:
            h.run, h.shutil.which = saved_run, saved_which
        return called

    def test_nothing_else_was_opened_up(self):
        """The list is short on purpose."""
        opened = set()
        for cmd in self.rules():
            if h.CHAIN_IN in cmd and "--dport" in cmd and "ACCEPT" in cmd:
                opened.add((cmd[cmd.index("-p") + 1],
                            int(cmd[cmd.index("--dport") + 1])))
        self.assertEqual(opened, {("udp", 67), ("udp", 53), ("tcp", 53),
                                  ("tcp", h.CONSOLE_PORT), ("tcp", h.LINK_PORT)})

    def test_the_rules_can_be_reapplied_without_bouncing_the_radio(self):
        """`up` was the only thing that rebuilt these, and on this wifi chip
        it is the operation with four documented ways to fail."""
        self.assertIn("firewall", h.REMOTE)
        import inspect
        body = inspect.getsource(h.do_firewall)
        self.assertIn("firewall_up(", body)
        for wrecking in ("cmd_up(", "cmd_down(", "hostapd", "dnsmasq",
                         "radio_ready("):
            self.assertNotIn(wrecking, body,
                             "do_firewall must not touch the radio")

    def test_the_reject_comes_after_every_accept(self):
        """Naming ports means nothing if the catch-all is above them."""
        inbound = [c for c in self.rules() if h.CHAIN_IN in c and "-A" in c]
        rejects = [i for i, c in enumerate(inbound) if "REJECT" in c]
        accepts = [i for i, c in enumerate(inbound) if "ACCEPT" in c]
        self.assertTrue(rejects and accepts)
        self.assertGreater(min(rejects), max(accepts))


class TestTheInstalledCopyIsNotAhead(unittest.TestCase):
    """The repo lost a fix to the running system for four days."""

    def test_the_hostapd_fallback_is_in_the_repo(self):
        source = read(HELPER)
        self.assertIn("reduced=False", source)
        self.assertIn("reduced=True", source)
        self.assertIn("country_code", source)

    def test_a_reduced_config_drops_what_the_driver_chokes_on(self):
        cfg = h.validate(dict(GOOD))
        p = h.plan(cfg) if hasattr(h, "plan") else None
        full = h.hostapd_conf(cfg, p)
        cut = h.hostapd_conf(cfg, p, reduced=True)
        self.assertIn("country_code=", full)
        self.assertIn("ieee80211n=1", full)
        self.assertNotIn("country_code=", cut)
        self.assertNotIn("ieee80211n=1", cut)
        # Everything that actually makes it an access point has to survive.
        for line in ("ssid=", "wpa=2", "wpa_passphrase=", "rsn_pairwise=CCMP"):
            self.assertIn(line, cut)
