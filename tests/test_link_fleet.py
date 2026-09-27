"""The link, bolted to the fleet: what a device on a socket actually
reaches."""
import json
import os
import socket
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from zero2w_console import fleet as fleetmod   # noqa: E402
from zero2w_console import flows as flowmod    # noqa: E402
from zero2w_console import iot as iotmod       # noqa: E402
from zero2w_console import link as linkmod     # noqa: E402
from zero2w_console import tags as tagmod      # noqa: E402
from zero2w_console.agent.modules import link as wire  # noqa: E402


class Bus:
    def __init__(self):
        self.events = []

    def publish(self, topic, payload):
        self.events.append((topic, payload))


class Conn:
    """Enough of a `link.Connection` for the dispatch to be tested alone."""

    def __init__(self, device_id, address="10.42.0.51"):
        self.device_id = device_id
        self.address = (address, 51234)
        self.sent = []

    def send(self, kind, body=b""):
        self.sent.append((kind, body))
        return True


class FleetLinkCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.devices = iotmod.DeviceStore(os.path.join(self.dir.name, "iot.json"))
        self.flows = flowmod.FlowStore(os.path.join(self.dir.name, "flows.json"))
        self.bus = Bus()
        self.fleet = fleetmod.Fleet(self.devices, self.flows, self.bus)
        self.device = iotmod.create_device(self.devices, {
            "name": "ESP32 Motor", "board": "esp32", "chip": "ESP32-D0WD",
            "mac": "70:4b:ca:00:00:b8"})

    def tearDown(self):
        self.dir.cleanup()

    def enrol(self, link=True):
        prov = self.fleet.provision(self.device["id"])
        token = self.fleet.enroll(self.device["id"], prov["enroll_token"],
                                  {"chip": "ESP32"})["token"]
        if link:
            iotmod.update_device(self.devices, self.device["id"], {"link": True})
        return token

    def record(self):
        return iotmod.get_device(self.devices, self.device["id"])


class TestLookup(FleetLinkCase):
    def test_an_enrolled_device_has_a_token_to_look_up(self):
        token = self.enrol()
        self.assertEqual(self.fleet.device_token(self.device["id"]), token)

    def test_a_device_that_has_not_enrolled_has_none(self):
        self.assertIsNone(self.fleet.device_token(self.device["id"]))

    def test_an_id_that_does_not_exist_has_none(self):
        self.assertIsNone(self.fleet.device_token("dev_nothing"))

    def test_a_device_whose_link_is_switched_off_cannot_dial(self):
        """Enforced here, not only on the board."""
        self.enrol(link=False)
        self.assertIsNone(self.fleet.device_token(self.device["id"]))

    def test_a_reflashed_device_loses_its_old_token(self):
        """Provisioning clears it, so a board flashed again cannot dial on
        the credential the one before it held."""
        old = self.enrol()
        self.fleet.provision(self.device["id"])
        self.assertIsNone(self.fleet.device_token(self.device["id"]))
        self.assertNotEqual(old, None)


class TestDispatch(FleetLinkCase):
    def setUp(self):
        super().setUp()
        self.enrol()
        del self.bus.events[:]          # enrolling says so; that is not this
        self.conn = Conn(self.device["id"])

    def frame(self, kind, doc):
        self.fleet.on_link_frame(self.conn, kind,
                                 json.dumps(doc).encode() if doc is not None else b"")

    def test_state_lands_where_the_polled_route_puts_it(self):
        self.frame(wire.STATE, {"ip": "10.42.0.51", "free_ram": 102448,
                                "running": True, "pins": {"27": {"dir": "out",
                                                                 "value": 1}}})
        state = self.record()["state"]
        self.assertEqual(state["free_ram"], 102448)
        self.assertIs(state["running"], True)
        self.assertEqual(state["pins"]["27"]["value"], 1)

    def test_an_event_reaches_the_bus(self):
        self.frame(wire.EVENT, {"kind": "log", "level": "warn",
                                "message": "watchdog cut drive"})
        said = [p for t, p in self.bus.events if t == "flow"]
        self.assertTrue(any(p["message"] == "watchdog cut drive" for p in said))

    def table(self):
        store = tagmod.TagStore(os.path.join(self.dir.name, "tags.json"))
        table = tagmod.TagTable(store, bus=self.bus)
        table.define([
            {"name": "speed", "type": "number", "initial": 0, "share": True},
            {"name": "secret", "type": "number", "initial": 1, "share": False}])
        self.fleet.tags = table
        return table

    def test_a_shared_tag_written_on_the_device_comes_home(self):
        table = self.table()
        self.frame(wire.TAGS, {"values": {"speed": 42}})
        self.assertEqual(table.get("speed"), 42)

    def test_a_tag_this_host_keeps_to_itself_is_not_writable(self):
        table = self.table()
        self.frame(wire.TAGS, {"values": {"secret": 99}})
        self.assertEqual(table.get("secret"), 1)

    def test_a_device_may_not_issue_a_command(self):
        """The direction is authenticated, which is exactly why this must be
        dropped rather than trusted: only this end issues commands."""
        self.frame(wire.COMMAND, {"op": "reboot"})
        self.frame(wire.CONTROL, {"op": "reboot"})
        self.assertIsNone(self.record().get("state"))
        self.assertEqual(self.bus.events, [])

    def test_a_frame_from_an_id_with_no_record_does_nothing(self):
        self.fleet.on_link_frame(Conn("dev_nothing"), wire.STATE, b"{}")
        self.assertEqual(self.bus.events, [])

    def test_a_body_that_is_not_json_is_ignored(self):
        self.fleet.on_link_frame(self.conn, wire.STATE, b"\xff\xfe not json")
        self.assertIsNone(self.record().get("state"))

    def test_a_body_that_is_json_but_not_an_object_is_ignored(self):
        self.fleet.on_link_frame(self.conn, wire.STATE, b"[1, 2, 3]")
        self.assertIsNone(self.record().get("state"))

    def test_a_handler_that_raises_does_not_escape(self):
        """A frame that verified means the link is sound."""
        def boom(device, body):
            raise ValueError("no")
        self.fleet.report_state = boom
        self.frame(wire.STATE, {"ip": "10.42.0.51"})
        said = [p["message"] for t, p in self.bus.events if t == "flow"]
        self.assertTrue(any("link frame" in m for m in said), said)

    def test_opening_and_closing_say_so(self):
        self.fleet.on_link_open(self.conn)
        self.fleet.on_link_close(self.conn)
        said = [p["message"] for t, p in self.bus.events if t == "flow"]
        self.assertIn("on the link from 10.42.0.51", said)
        self.assertIn("left the link", said)
        self.assertEqual(self.record()["ip"], "10.42.0.51")

    def test_a_connection_replaced_by_a_new_one_did_not_leave(self):
        """A board that restarts dials again; its old socket closes after."""
        self.fleet.on_link_open(self.conn)
        self.conn.replaced = True
        self.fleet.on_link_close(self.conn)
        said = [p["message"] for t, p in self.bus.events if t == "flow"]
        self.assertNotIn("left the link", said)


class TestWhatTheDeviceIsTold(FleetLinkCase):
    def test_no_listener_means_the_manifest_says_so(self):
        """Not a default port."""
        self.enrol()
        self.assertIsNone(self.fleet.manifest(self.record())["link"])
        self.assertIsNone(self.fleet.state()["link"])

    def listening(self):
        server = linkmod.LinkServer(lookup=self.fleet.device_token,
                                    port=0, host="127.0.0.1").start()
        self.addCleanup(server.stop)
        self.fleet.link = server
        return server

    def test_a_listener_alone_is_not_enough(self):
        """Per device, and off until turned on."""
        self.enrol(link=False)
        server = self.listening()
        self.assertIsNone(self.fleet.manifest(self.record())["link"])
        self.assertEqual(self.fleet.state()["link"]["port"], server.port)

    def test_the_manifest_carries_the_port_once_the_device_is_told_to(self):
        self.enrol()
        server = self.listening()
        self.assertEqual(self.fleet.manifest(self.record())["link"],
                         {"port": server.port})

    def test_the_modules_follow_the_same_switch(self):
        """Not pulled onto a board that was never told to hold a link open —
        the same rule the camera module already follows."""
        self.enrol(link=False)
        self.listening()
        names = self.fleet.agent_files(None, self.record())
        self.assertNotIn("modules/linkclient.py", names)
        iotmod.update_device(self.devices, self.device["id"], {"link": True})
        names = self.fleet.agent_files(None, self.record())
        self.assertIn("modules/link.py", names)
        self.assertIn("modules/linkclient.py", names)
        for entry in self.fleet.manifest(self.record())["modules"]:
            self.assertTrue(entry["sha"] and entry["bytes"], entry)

    def test_the_modules_follow_the_device_and_not_the_listener(self):
        """Two different questions."""
        self.enrol()
        names = self.fleet.agent_files(None, self.record())
        self.assertIn("modules/linkclient.py", names)
        self.assertIn("modules/linkagent.py", names)
        # And it is still told not to dial, which is the part that matters.
        self.assertIsNone(self.fleet.manifest(self.record())["link"])


class TestEndToEnd(FleetLinkCase):
    """A real socket, a real handshake, a real device record at the end of
    it."""

    def setUp(self):
        super().setUp()
        self.token = self.enrol()
        self.server = linkmod.LinkServer(
            lookup=self.fleet.device_token, on_frame=self.fleet.on_link_frame,
            on_open=self.fleet.on_link_open, on_close=self.fleet.on_link_close,
            port=0, host="127.0.0.1").start()
        self.fleet.link = self.server

    def tearDown(self):
        # Before the temp directory goes, not after. A handler thread writes the
        # device store, and `addCleanup` runs after `tearDown` — so a frame
        # still in flight would land in a directory being deleted underneath
        # it. That showed up as an intermittent "Directory not empty".
        self.server.stop()
        super().tearDown()

    def dial(self, device=None, token=None):
        sock = socket.create_connection(("127.0.0.1", self.server.port), timeout=5)
        self.addCleanup(sock.close)
        sock.settimeout(5)
        nonce = os.urandom(wire.NONCE_BYTES)
        sock.sendall(wire.seal(linkmod.GREETING_KEY, wire.HELLO, 1, json.dumps(
            {"device": device or self.device["id"], "nonce": nonce.hex()}).encode()))
        head, body = linkmod._read_frame(sock)
        kind, _seq, body = wire.open_frame(linkmod.GREETING_KEY, head, body)
        self.assertEqual(kind, wire.CHALLENGE)
        nonce_host = bytes.fromhex(json.loads(body.decode())["nonce"])
        channel = wire.Channel(wire.session_key(token or self.token, nonce,
                                                nonce_host))
        sock.sendall(channel.send(wire.AUTH, b"{}"))
        return sock, channel

    def wait(self, predicate, seconds=3):
        end = time.time() + seconds
        while time.time() < end:
            if predicate():
                return True
            time.sleep(0.01)
        return False

    def test_a_device_reports_over_the_link(self):
        sock, channel = self.dial()
        head, body = linkmod._read_frame(sock)
        self.assertEqual(channel.receive(head, body)[0], wire.READY)
        sock.sendall(channel.send(wire.STATE, json.dumps(
            {"free_ram": 102448, "running": True}).encode()))
        self.assertTrue(self.wait(
            lambda: (self.record().get("state") or {}).get("free_ram") == 102448))
        self.assertEqual(self.server.online(), [self.device["id"]])

    def test_dialling_again_is_not_leaving(self):
        """A restarted board's new link opens before its old socket closes."""
        first, ch1 = self.dial()
        linkmod._read_frame(first)
        second, ch2 = self.dial()
        head, body = linkmod._read_frame(second)
        self.assertEqual(ch2.receive(head, body)[0], wire.READY)
        said = lambda: [p["message"] for t, p in self.bus.events if t == "flow"]
        self.assertTrue(self.wait(
            lambda: said().count("on the link from 127.0.0.1") == 2))
        self.assertFalse(self.wait(lambda: "left the link" in said(), 1))
        second.close()
        self.assertTrue(self.wait(lambda: "left the link" in said()))

    def test_the_real_token_is_what_gets_in(self):
        sock, _ = self.dial(token="not-the-token")
        sock.settimeout(3)
        try:
            self.assertEqual(sock.recv(64), b"")
        except ConnectionResetError:
            pass
        self.assertEqual(self.server.online(), [])

    def test_a_command_goes_down_the_link_and_not_the_queue(self):
        sock, channel = self.dial()
        head, body = linkmod._read_frame(sock)
        self.assertEqual(channel.receive(head, body)[0], wire.READY)
        self.assertTrue(self.wait(lambda: self.server.online() == [self.device["id"]]))

        self.fleet.push(self.device["id"], {"op": "fire", "node": "n1"})
        kind, body = channel.receive(*linkmod._read_frame(sock))
        self.assertEqual(kind, wire.COMMAND)
        self.assertEqual(json.loads(body.decode()), {"op": "fire", "node": "n1"})
        self.assertEqual(self.fleet.queues.get(self.device["id"], []), [])

    def test_a_tag_push_gets_the_frame_type_that_is_for_it(self):
        sock, channel = self.dial()
        channel.receive(*linkmod._read_frame(sock))          # READY
        self.assertTrue(self.wait(lambda: self.server.online() == [self.device["id"]]))

        self.fleet.push(self.device["id"], {"op": "tags", "values": {"speed": 7}})
        kind, body = channel.receive(*linkmod._read_frame(sock))
        self.assertEqual(kind, wire.TAGSET)
        self.assertEqual(json.loads(body.decode()), {"values": {"speed": 7}})

    def test_a_device_with_no_link_still_gets_the_queue(self):
        """Nothing changes for a board that never dials, which for most of
        the cutover is every board."""
        self.fleet.push(self.device["id"], {"op": "reload"})
        self.assertEqual(self.fleet.queues[self.device["id"]],
                         [{"op": "reload"}])

    def test_a_link_that_has_dropped_falls_back_rather_than_vanishing(self):
        sock, channel = self.dial()
        channel.receive(*linkmod._read_frame(sock))          # READY
        self.assertTrue(self.wait(lambda: self.server.online() == [self.device["id"]]))
        sock.close()
        self.assertTrue(self.wait(lambda: self.server.online() == []))
        self.fleet.push(self.device["id"], {"op": "reload"})
        self.assertEqual(self.fleet.queues[self.device["id"]],
                         [{"op": "reload"}])

    def test_a_command_in_doubt_goes_back_on_the_queue(self):
        """The link accepted it and then died."""
        sock, channel = self.dial()
        channel.receive(*linkmod._read_frame(sock))          # READY
        self.assertTrue(self.wait(lambda: self.server.online() == [self.device["id"]]))

        self.fleet.push(self.device["id"], {"op": "reload", "flow": "f_x"})
        self.assertEqual(self.fleet.queues.get(self.device["id"], []), [])
        sock.close()
        self.assertTrue(self.wait(
            lambda: self.fleet.queues.get(self.device["id"])))
        self.assertEqual(self.fleet.queues[self.device["id"]],
                         [{"op": "reload", "flow": "f_x"}])

    def test_a_command_that_cannot_be_repeated_is_not_requeued_silently(self):
        """A duplicate `fire` is a second message into a running flow — one
        more step from a motor."""
        sock, channel = self.dial()
        channel.receive(*linkmod._read_frame(sock))
        self.assertTrue(self.wait(lambda: self.server.online() == [self.device["id"]]))

        self.fleet.push(self.device["id"], {"op": "fire", "node": "n1"})
        sock.close()
        self.assertTrue(self.wait(
            lambda: any("cannot be repeated" in p.get("message", "")
                        for _t, p in self.bus.events)))
        self.assertEqual(self.fleet.queues.get(self.device["id"], []), [])

    def test_a_reboot_is_never_requeued(self):
        """Because succeeding at it looks exactly like failing at it."""
        sock, channel = self.dial()
        channel.receive(*linkmod._read_frame(sock))
        self.assertTrue(self.wait(lambda: self.server.online() == [self.device["id"]]))

        self.fleet.push(self.device["id"], {"op": "reboot"})
        sock.close()
        self.assertTrue(self.wait(
            lambda: any("cannot be repeated" in p.get("message", "")
                        for _t, p in self.bus.events)))
        self.assertEqual(self.fleet.queues.get(self.device["id"], []), [],
                         "a reboot came back round and rebooted the board again")

    def test_a_command_delivered_long_ago_is_not_replayed(self):
        """Otherwise a reconnect replays the morning's work."""
        sock, channel = self.dial()
        channel.receive(*linkmod._read_frame(sock))
        self.assertTrue(self.wait(lambda: self.server.online() == [self.device["id"]]))

        self.fleet.push(self.device["id"], {"op": "reload"})
        held = self.fleet.inflight[self.device["id"]]
        # As if it had been sent before the grace period began.
        self.fleet.inflight[self.device["id"]] = [
            (held[0][0] - fleetmod.INFLIGHT_GRACE - 1, held[0][1])]
        sock.close()
        self.assertTrue(self.wait(lambda: self.server.online() == []))
        self.assertEqual(self.fleet.queues.get(self.device["id"], []), [])

    def test_the_held_list_does_not_grow_without_bound(self):
        """It is trimmed on every push, not only when a link closes — a
        device that never drops would otherwise accumulate for as long as
        it is up."""
        sock, channel = self.dial()
        channel.receive(*linkmod._read_frame(sock))
        self.assertTrue(self.wait(lambda: self.server.online() == [self.device["id"]]))
        for _ in range(30):
            self.fleet.push(self.device["id"], {"op": "tags", "values": {"a": 1}})
        held = self.fleet.inflight[self.device["id"]]
        self.assertLessEqual(len(held), 30)
        self.fleet.inflight[self.device["id"]] = [
            (at - fleetmod.INFLIGHT_GRACE - 1, c) for at, c in held]
        self.fleet.push(self.device["id"], {"op": "tags", "values": {"a": 2}})
        self.assertEqual(len(self.fleet.inflight[self.device["id"]]), 1)

    def test_nothing_is_held_when_the_queue_carried_it(self):
        """Only a link can lose a command this way."""
        self.fleet.push(self.device["id"], {"op": "reload"})
        self.assertEqual(self.fleet.inflight.get(self.device["id"], []), [])
        self.assertEqual(len(self.fleet.queues[self.device["id"]]), 1)

    def test_a_device_that_never_enrolled_cannot_dial(self):
        other = iotmod.create_device(self.devices, {
            "name": "Not yet", "board": "esp32", "mac": "70:4b:ca:00:00:c9"})
        sock, _ = self.dial(device=other["id"], token="anything")
        sock.settimeout(3)
        try:
            self.assertEqual(sock.recv(64), b"")
        except ConnectionResetError:
            pass
        self.assertEqual(self.server.online(), [])


if __name__ == "__main__":
    unittest.main()
