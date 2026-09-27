"""The console cannot use sudo, so it asks the helper over a socket."""
import importlib.machinery
import importlib.util
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

HELPER = os.path.join(ROOT, "packaging", "iot-netctl")

from zero2w_console import iot  # noqa: E402


def load_helper():
    sys.dont_write_bytecode = True
    loader = importlib.machinery.SourceFileLoader("iot_netctl_sock", HELPER)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class TestSocketProtocol(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        cls.helper = load_helper()
        cls.sock_path = os.path.join(cls.dir.name, "ctl.sock")
        cls.helper.SOCKET = cls.sock_path
        cls.helper.NET_CONFIG = os.path.join(cls.dir.name, "iot-net.json")
        cls.helper.SOCKET_GROUP = None          # skip the chown when not root

        # Serve without the root-only setup steps, which are systemd's job.
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(cls.sock_path)
        srv.listen(4)
        cls.srv = srv
        cls.stop = threading.Event()

        def serve():
            while not cls.stop.is_set():
                try:
                    conn, _ = srv.accept()
                except OSError:
                    return
                with conn:
                    raw = b""
                    while b"\n" not in raw:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        raw += chunk
                    try:
                        req = json.loads(raw.decode() or "{}")
                    except ValueError:
                        conn.sendall(b'{"error": "not JSON"}\n')
                        continue
                    verb = req.get("verb")
                    if verb not in cls.helper.REMOTE:
                        conn.sendall(b'{"error": "unknown verb"}\n')
                        continue
                    try:
                        out = cls.helper.REMOTE[verb](req.get("payload"))
                    except cls.helper.Invalid as exc:
                        out = {"error": "rejected: %s" % exc}
                    except Exception as exc:
                        out = {"error": "%s: %s" % (type(exc).__name__, exc)}
                    conn.sendall((json.dumps(out) + "\n").encode())

        cls.thread = threading.Thread(target=serve, daemon=True)
        cls.thread.start()
        time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        cls.stop.set()
        try:
            cls.srv.close()
        except OSError:
            pass
        cls.dir.cleanup()

    def ask(self, verb, payload=None):
        old = iot.HELPER_SOCKET
        iot.HELPER_SOCKET = self.sock_path
        try:
            return iot._helper_over_socket(verb, payload, 10)
        finally:
            iot.HELPER_SOCKET = old

    def test_status_answers(self):
        out = self.ask("status")
        self.assertIn("up", out)
        self.assertIn("configured", out)

    def test_clients_answers(self):
        self.assertIn("clients", self.ask("clients"))

    def test_config_is_stored_and_the_passphrase_is_not_echoed(self):
        out = self.ask("config", {"ssid": "zero2w-iot", "psk": "correct-horse",
                                  "channel": 6, "subnet": "10.42.0.0/24",
                                  "interface": "wlan0"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["config"]["psk"], "********")
        with open(self.helper.NET_CONFIG) as fh:
            self.assertEqual(json.load(fh)["ssid"], "zero2w-iot")

    def test_a_bad_config_is_refused_by_the_privileged_side(self):
        """The console validates for a readable error; this is the check
        that counts, because it runs as root."""
        for bad in ({"ssid": "x", "psk": "short", "channel": 6, "subnet": "10.42.0.0/24"},
                    {"ssid": "x", "psk": "12345678", "channel": 36, "subnet": "10.42.0.0/24"},
                    {"ssid": "a\nb", "psk": "12345678", "channel": 6, "subnet": "10.42.0.0/24"}):
            with self.subTest(config=bad):
                with self.assertRaises(iot.NetError):
                    self.ask("config", bad)

    def test_status_carries_the_config_but_never_the_passphrase(self):
        """The stored file is 0600 root, so the console cannot read back
        what it saved — this is how it learns, and the one field that
        must stay on the privileged side does."""
        self.ask("config", {"ssid": "zero2w-iot", "psk": "correct-horse-battery",
                            "channel": 11, "subnet": "10.44.0.0/24",
                            "interface": "wlan0"})
        out = self.ask("status")
        self.assertTrue(out["configured"])
        self.assertEqual(out["config"]["ssid"], "zero2w-iot")
        self.assertEqual(out["config"]["channel"], 11)
        self.assertEqual(out["config"]["gateway"], "10.44.0.1")
        self.assertNotIn("psk", out["config"])
        self.assertNotIn("correct-horse", json.dumps(out))

    def test_status_before_anything_is_configured(self):
        import tempfile as tf
        old = self.helper.NET_CONFIG
        self.helper.NET_CONFIG = os.path.join(tf.mkdtemp(), "nothing.json")
        try:
            out = self.ask("status")
            self.assertFalse(out["configured"])
            self.assertIsNone(out["config"])
        finally:
            self.helper.NET_CONFIG = old

    def test_an_unknown_verb_is_refused(self):
        with self.assertRaises(iot.NetError) as caught:
            self.ask("rm-rf")
        self.assertIn("unknown verb", str(caught.exception))

    def test_the_verbs_on_the_socket_are_the_verbs_the_console_knows(self):
        self.assertEqual(sorted(self.helper.REMOTE), sorted(iot.HELPER_VERBS))

    def test_a_dead_socket_is_a_readable_error(self):
        old = iot.HELPER_SOCKET
        iot.HELPER_SOCKET = os.path.join(self.dir.name, "not-there.sock")
        try:
            with self.assertRaises(iot.NetError) as caught:
                iot._helper_over_socket("status", None, 2)
            self.assertIn("not answering", str(caught.exception))
        finally:
            iot.HELPER_SOCKET = old


class TestPeerCheck(unittest.TestCase):
    """Who may talk to the socket, beyond the file mode the kernel enforces."""

    def setUp(self):
        self.helper = load_helper()

    def test_root_is_always_allowed(self):
        self.assertIn(0, self.helper._uids_in_group("root"))

    def test_membership_is_by_group_not_by_name(self):
        import grp
        import pwd
        me = pwd.getpwuid(os.getuid())
        mine = grp.getgrgid(me.pw_gid).gr_name
        self.assertIn(me.pw_uid, self.helper._uids_in_group(mine),
                      "a user in a group must be allowed through it")

    def test_an_unknown_group_allows_only_root(self):
        self.assertEqual(self.helper._uids_in_group("no-such-group-here"), {0})


class TestServiceUnit(unittest.TestCase):
    def unit(self):
        with open(os.path.join(ROOT, "packaging", "zero2w-iotnet.service"),
                  encoding="utf-8") as fh:
            return fh.read()

    def test_it_runs_the_helper_in_serve_mode(self):
        self.assertIn("ExecStart=/usr/local/sbin/iot-netctl serve", self.unit())

    def test_the_socket_directory_is_not_world_readable(self):
        self.assertIn("RuntimeDirectoryMode=0750", self.unit())

    def test_restarting_the_helper_does_not_take_the_access_point_down(self):
        """hostapd and dnsmasq are started by this service and inherit its
        cgroup, and the default KillMode takes the whole cgroup with it."""
        self.assertIn("KillMode=process", self.unit())

    def test_the_console_unit_still_forbids_privilege_escalation(self):
        """If this ever flips to no, the socket was not the reason — check why."""
        with open(os.path.join(ROOT, "packaging", "zero2w-console.service"),
                  encoding="utf-8") as fh:
            self.assertIn("NoNewPrivileges=yes", fh.read())


if __name__ == "__main__":
    unittest.main()
