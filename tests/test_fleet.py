"""The device link: enrolment, what a device is told to be, and what it may
reach."""
import hashlib
import json
import os
import sys
import tempfile
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from zero2w_console import fleet as fleetmod  # noqa: E402
from zero2w_console import flows as flowmod   # noqa: E402
from zero2w_console import iot as iotmod      # noqa: E402


class Bus:
    """Stands in for the SSE bus and remembers what was published."""

    def __init__(self):
        self.events = []

    def publish(self, topic, payload):
        self.events.append((topic, payload))


def blink_flow(device=None, board="esp32cam"):
    return {"id": "f_blink", "name": "Blink", "enabled": True,
            "board": board, "device": device,
            "nodes": [
                {"id": "t1", "type": "timer.interval", "config": {"every": 1000}},
                {"id": "a1", "type": "gpio.out", "config": {"gpio": 33, "action": "toggle"}}],
            "edges": [{"id": "e1", "from": "t1", "fromPort": "out",
                       "to": "a1", "toPort": "in"}]}


class FleetCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.devices = iotmod.DeviceStore(os.path.join(self.dir.name, "iot.json"))
        self.flows = flowmod.FlowStore(os.path.join(self.dir.name, "flows.json"))
        self.bus = Bus()
        self.fleet = fleetmod.Fleet(self.devices, self.flows, self.bus)
        self.device = iotmod.create_device(self.devices, {
            "name": "Bench CAM", "board": "esp32cam", "chip": "ESP32-D0WD-V3",
            "mac": "70:4b:ca:00:00:b8"})

    def tearDown(self):
        self.dir.cleanup()

    def enrolled(self, probe=None):
        prov = self.fleet.provision(self.device["id"])
        out = self.fleet.enroll(self.device["id"], prov["enroll_token"],
                                probe if probe is not None else {"chip": "ESP32"})
        return out, prov


class TestEnrolment(FleetCase):
    def test_a_device_gets_its_own_token(self):
        out, _ = self.enrolled()
        self.assertTrue(out["token"])
        record = iotmod.get_device(self.devices, self.device["id"])
        self.assertEqual(record["token"], out["token"])
        self.assertIsNone(record["enroll_token"], "the one-time token is spent")
        self.assertTrue(record["enrolled"])

    def test_the_enrol_token_works_exactly_once(self):
        _, prov = self.enrolled()
        with self.assertRaises(iotmod.NetError):
            self.fleet.enroll(self.device["id"], prov["enroll_token"], {})

    def test_a_wrong_token_is_refused(self):
        self.fleet.provision(self.device["id"])
        with self.assertRaises(iotmod.NetError):
            self.fleet.enroll(self.device["id"], "not-the-token", {})

    def test_nothing_enrols_through_a_closed_window(self):
        prov = self.fleet.provision(self.device["id"])
        self.fleet.close_window()
        with self.assertRaises(iotmod.NetError) as caught:
            self.fleet.enroll(self.device["id"], prov["enroll_token"], {})
        self.assertIn("closed", str(caught.exception))

    def test_the_window_expires_on_its_own(self):
        self.fleet.open_window(seconds=-1)
        self.assertIsNone(self.fleet.window_state())

    def test_reprovisioning_invalidates_the_old_token(self):
        out, _ = self.enrolled()
        self.fleet.provision(self.device["id"])
        self.assertIsNone(self.fleet.authenticate(out["token"]))

    def test_a_probe_that_disagrees_with_the_config_is_flagged(self):
        self.enrolled(probe={"chip": "ESP32-C3", "firmware": "micropython 1.24"})
        record = iotmod.get_device(self.devices, self.device["id"])
        self.assertIn("reports", record["mismatch"] or "")

    def test_a_probe_that_agrees_is_not(self):
        self.enrolled(probe={"chip": "ESP32"})
        self.assertIsNone(iotmod.get_device(self.devices, self.device["id"])["mismatch"])


class TestAuthentication(FleetCase):
    def test_a_device_token_identifies_one_device(self):
        out, _ = self.enrolled()
        self.assertEqual(self.fleet.authenticate(out["token"])["id"], self.device["id"])

    def test_rubbish_authenticates_as_nothing(self):
        self.enrolled()
        for bad in (None, "", "nope", 0):
            self.assertIsNone(self.fleet.authenticate(bad))


class TestManifest(FleetCase):
    def test_nothing_is_deployed_until_it_is_deployed(self):
        """Linking a flow to a device in the editor is authoring."""
        self.flows.save({"flows": [blink_flow(device=self.device["id"])]})
        out, _ = self.enrolled()
        man = self.fleet.manifest(self.fleet.authenticate(out["token"]))
        self.assertIsNone(man["flow"])

    def test_a_deployed_flow_comes_back_with_only_the_modules_it_needs(self):
        self.flows.save({"flows": [blink_flow()]})
        out, _ = self.enrolled()
        self.fleet.deploy(self.device["id"], "f_blink")
        man = self.fleet.manifest(self.fleet.authenticate(out["token"]))
        names = [m["name"] for m in man["modules"]]
        self.assertEqual(man["flow"]["id"], "f_blink")
        self.assertIn("agent.py", names)
        self.assertIn("flow.py", names)
        self.assertIn("modules/gpio.py", names)
        self.assertNotIn("modules/i2c.py", names, "a flow with no I2C pulls no I2C")
        self.assertNotIn("modules/http.py", names)

    def test_the_manifest_shas_match_the_files(self):
        self.flows.save({"flows": [blink_flow()]})
        out, _ = self.enrolled()
        self.fleet.deploy(self.device["id"], "f_blink")
        man = self.fleet.manifest(self.fleet.authenticate(out["token"]))
        for mod in man["modules"]:
            body = self.fleet.module_source(mod["name"])
            with self.subTest(module=mod["name"]):
                self.assertEqual(hashlib.sha256(body).hexdigest()[:16], mod["sha"])
                self.assertEqual(len(body), mod["bytes"])

    def test_it_does_not_carry_the_board_pin_profile(self):
        """It used to, and no agent has ever read it."""
        out, _ = self.enrolled()
        man = self.fleet.manifest(self.fleet.authenticate(out["token"]))
        self.assertNotIn("pins", man)

    def test_every_key_in_it_is_one_the_agent_reads(self):
        """The guard that keeps the manifest from growing back."""
        source = self.fleet.module_source("agent.py").decode()
        out, _ = self.enrolled()
        man = self.fleet.manifest(self.fleet.authenticate(out["token"]))
        # Tolerated unread, and each is tens of bytes: three identity fields
        # that make a manifest legible to a human with curl, and two the host
        # itself finds useful when reading one back. `pins` was none of these —
        # it was kilobytes — which is what the size guard below is for.
        spare = {"device", "name", "board", "camera", "flow_sha"}
        for key in man:
            if key in spare:
                continue
            with self.subTest(key=key):
                self.assertTrue(
                    ('man.get("%s"' % key) in source
                    or ('man["%s"]' % key) in source,
                    "the manifest carries %r and agent.py never reads it — "
                    "either read it or stop sending it" % key)
        for key in spare:
            if key not in man:
                continue
            with self.subTest(spare=key):
                self.assertLess(len(json.dumps(man[key])), 128,
                                "%r is tolerated because it is small; it is "
                                "not small any more" % key)


class TestModuleSource(FleetCase):
    def test_a_real_module_comes_back(self):
        self.assertIn(b"class Runner", self.fleet.module_source("flow.py"))

    def test_nothing_escapes_the_agent_directory(self):
        for bad in ("../server.py", "/etc/passwd", "modules/../../server.py",
                    "", None, "does-not-exist.py"):
            with self.subTest(name=bad):
                with self.assertRaises(iotmod.NetError):
                    self.fleet.module_source(bad)


class TestCommands(FleetCase):
    def test_a_queued_command_comes_back_at_once(self):
        out, _ = self.enrolled()
        device = self.fleet.authenticate(out["token"])
        self.fleet.push(self.device["id"], {"op": "reboot"})
        self.assertEqual(self.fleet.take_commands(device, wait=1),
                         [{"op": "reboot"}])

    def test_an_empty_poll_returns_empty_rather_than_never(self):
        out, _ = self.enrolled()
        device = self.fleet.authenticate(out["token"])
        self.assertEqual(self.fleet.take_commands(device, wait=1), [])

    def test_a_parked_poll_wakes_when_something_is_pushed(self):
        out, _ = self.enrolled()
        device = self.fleet.authenticate(out["token"])
        got = []

        def poll():
            got.extend(self.fleet.take_commands(device, wait=20))

        t = threading.Thread(target=poll)
        t.start()
        threading.Event().wait(0.3)          # let it park
        self.fleet.push(self.device["id"], {"op": "reload"})
        t.join(timeout=10)
        self.assertEqual(got, [{"op": "reload"}])

    def test_a_device_that_never_polls_does_not_grow_forever(self):
        for i in range(200):
            self.fleet.push(self.device["id"], {"op": "fire", "n": i})
        self.assertLessEqual(len(self.fleet.queues[self.device["id"]]), 64)


class TestDeploy(FleetCase):
    def test_deploying_records_it_and_tells_the_device(self):
        self.flows.save({"flows": [blink_flow()]})
        self.enrolled()
        self.fleet.deploy(self.device["id"], "f_blink")
        self.assertEqual(iotmod.get_device(self.devices, self.device["id"])["flow"], "f_blink")
        self.assertEqual(self.fleet.queues[self.device["id"]],
                         [{"op": "reload", "flow": "f_blink"}])

    def test_a_flow_with_a_host_only_node_is_refused(self):
        bad = blink_flow()
        bad["nodes"].append({"id": "s1", "type": "shell.run", "config": {"command": "id"}})
        self.flows.save({"flows": [bad]})
        with self.assertRaises(iotmod.NetError) as caught:
            self.fleet.deploy(self.device["id"], "f_blink")
        self.assertIn("shell.run", str(caught.exception))

    def test_a_flow_for_another_board_is_refused(self):
        self.flows.save({"flows": [blink_flow(board="esp32s3")]})
        with self.assertRaises(iotmod.NetError) as caught:
            self.fleet.deploy(self.device["id"], "f_blink")
        self.assertIn("esp32s3", str(caught.exception))

    def test_deploying_nothing_clears_it(self):
        self.flows.save({"flows": [blink_flow()]})
        self.fleet.deploy(self.device["id"], "f_blink")
        self.fleet.deploy(self.device["id"], None)
        self.assertIsNone(iotmod.get_device(self.devices, self.device["id"])["flow"])

    def test_an_unknown_flow_or_device_is_an_error(self):
        with self.assertRaises(iotmod.NetError):
            self.fleet.deploy(self.device["id"], "f_nope")
        with self.assertRaises(iotmod.NetError):
            self.fleet.deploy("dev_nope", None)


class TestEvents(FleetCase):
    def test_an_event_reaches_the_same_bus_the_local_engine_uses(self):
        out, _ = self.enrolled()
        device = self.fleet.authenticate(out["token"])
        self.bus.events.clear()
        self.fleet.event(device, {"kind": "flow", "level": "ok", "message": "blinked",
                                  "node": "a1", "ip": "10.42.0.64", "rssi": -58})
        topic, payload = self.bus.events[-1]
        self.assertEqual(topic, "flow")
        self.assertEqual(payload["message"], "blinked")
        self.assertEqual(payload["device"], self.device["id"])

    def test_a_device_can_update_its_own_address_but_not_its_identity(self):
        out, _ = self.enrolled()
        device = self.fleet.authenticate(out["token"])
        self.fleet.event(device, {"message": "hi", "ip": "10.42.0.64", "rssi": -58,
                                  "id": "dev_someone_else", "board": "esp32s3",
                                  "name": "Renamed by the device"})
        record = iotmod.get_device(self.devices, self.device["id"])
        self.assertEqual(record["ip"], "10.42.0.64")
        self.assertEqual(record["board"], "esp32cam")
        self.assertEqual(record["name"], "Bench CAM")

    def test_a_level_it_made_up_is_not_taken(self):
        out, _ = self.enrolled()
        device = self.fleet.authenticate(out["token"])
        self.bus.events.clear()
        self.fleet.event(device, {"level": "spectacular", "message": "x"})
        self.assertEqual(self.bus.events[-1][1]["level"], "idle")

    def test_a_very_long_message_is_cut(self):
        out, _ = self.enrolled()
        device = self.fleet.authenticate(out["token"])
        self.fleet.event(device, {"message": "x" * 5000})
        self.assertLessEqual(len(self.bus.events[-1][1]["message"]), 400)


class TestAgentSource(unittest.TestCase):
    """The agent is MicroPython, so it cannot be imported here — but it can
    be parsed, and it must not reach for anything a stock build lacks."""

    def files(self):
        out = []
        for root, _dirs, names in os.walk(fleetmod.AGENT_DIR):
            for name in names:
                if name.endswith(".py"):
                    out.append(os.path.join(root, name))
        return out

    def test_every_agent_file_parses(self):
        import ast
        for path in self.files():
            with self.subTest(file=os.path.basename(path)):
                with open(path, encoding="utf-8") as fh:
                    ast.parse(fh.read(), path)

    def test_the_agent_imports_only_what_a_stock_build_has(self):
        """One exception, and it is deliberate: `camera` exists only in a
        camera-enabled firmware, so modules/camera.py may ask for it and
        nothing else may."""
        import ast
        allowed = {
            # MicroPython builtins
            "machine", "network", "esp", "esp32", "micropython",
            "ujson", "usocket", "utime", "uos", "ubinascii",
            "ustruct", "uhashlib",
            # names a stock build also provides under their CPython spelling
            "json", "socket", "time", "os", "sys", "gc",
            # the link's framing and MAC. Both are in a stock ESP32 build, and
            # both are imported under their u-prefixed name first so the host
            # can import the same file.
            "struct", "hashlib",
            # blefmt's hex, the same way round.
            "binascii",
            # main.py's fallback when agent.mpy will not load.
            "agent_src",
            # NimBLE, in every stock ESP32 build; modules/ble.py.
            "bluetooth",
            # mbedTLS, in every stock ESP32 build; modules/media.py's stream.
            "ssl",
            # the socket errnos, asked of the runtime rather than assumed —
            # MicroPython on ESP32 is newlib, where EINPROGRESS is 112 and not
            # the 115 Linux uses, and hardcoding the Linux numbers is what kept
            # the link stuck in `connecting` on a board.
            "uerrno", "errno",
            # and one question asked of select, always with a zero timeout:
            # has the dial finished. The board answers a premature write with
            # ECONNABORTED, which no errno list can tell from a real abort.
            "uselect", "select",
            # the agent's own files
            "agent", "flow", "modules",
        }
        for path in self.files():
            extra = {"camera"} if os.path.basename(path) == "camera.py" else set()
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), path)
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and not node.level:
                    names = [(node.module or "").split(".")[0]]
                for name in names:
                    with self.subTest(file=os.path.basename(path), imports=name):
                        self.assertIn(name, allowed | extra)

    def test_the_camera_is_reached_for_behind_an_import_guard(self):
        """A stock build has no camera module."""
        with open(os.path.join(fleetmod.AGENT_DIR, "agent.py"), encoding="utf-8") as fh:
            source = fh.read()
        start = source.index("def start_camera")
        body = source[start:source.index("def stop_camera")]
        self.assertIn("except ImportError", body)
        self.assertIn("import modules.camera", body)

    def test_a_camera_device_is_sent_the_camera_module(self):
        flow = blink_flow()
        plain = fleetmod.Fleet(None, None, None).agent_files(flow, {"camera": False})
        with_cam = fleetmod.Fleet(None, None, None).agent_files(flow, {"camera": True})
        self.assertNotIn("modules/camera.py", plain)
        self.assertIn("modules/camera.py", with_cam)

    def test_the_runner_covers_every_node_type_a_device_may_be_given(self):
        """The editor will let a device flow contain any node marked
        runnable, so there must be a handler for each one — in the runner
        itself, or in a module the device pulls."""
        with open(os.path.join(fleetmod.AGENT_DIR, "flow.py"), encoding="utf-8") as fh:
            source = fh.read()
        for ntype, spec in flowmod.REGISTRY.items():
            if spec["runs"] == "host":
                continue
            name = ntype.replace(".", "_")
            with self.subTest(node=ntype):
                if "_do_" + name in source:
                    continue
                where = fleetmod.NODE_MODULES.get(ntype)
                self.assertTrue(
                    where,
                    "%s has no handler in flow.py and no module to look in"
                    % ntype)
                path = os.path.join(fleetmod.AGENT_DIR, "modules", where + ".py")
                with open(path, encoding="utf-8") as fh:
                    self.assertIn("def %s(" % name, fh.read(),
                                  "%s is mapped to modules/%s.py, which has no "
                                  "%s" % (ntype, where, name))

    def test_the_runner_and_the_uploader_agree_on_what_is_pulled(self):
        """flow.PULLED decides where the device looks; NODE_MODULES decides
        what it is sent."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "flowsrc", os.path.join(fleetmod.AGENT_DIR, "flow.py"))
        # Read the table without importing the module, which wants `machine`.
        import ast
        with open(spec.origin, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        pulled = None
        for node in tree.body:
            if isinstance(node, ast.Assign) and \
                    getattr(node.targets[0], "id", None) == "PULLED":
                pulled = ast.literal_eval(node.value)
        self.assertIsNotNone(pulled, "flow.PULLED is gone")
        for ntype, where in pulled.items():
            with self.subTest(node=ntype):
                self.assertEqual(fleetmod.NODE_MODULES.get(ntype), where,
                                 "%s is looked for in modules/%s.py but that "
                                 "is not what gets uploaded" % (ntype, where))


class TestCameraNodes(FleetCase):
    """A tile on the dashboard appears because a flow asks for it, not
    because a board happens to have a lens on it."""

    def camera_flow(self, publish=True, label=""):
        """Interval -> Camera feed -> Camera to screen."""
        nodes = [{"id": "t1", "type": "timer.interval", "config": {"every": 1000}},
                 {"id": "c1", "type": "camera.feed",
                  "config": {"frame_size": "QVGA"}}]
        edges = [{"id": "e1", "from": "t1", "fromPort": "out",
                  "to": "c1", "toPort": "in"}]
        if publish:
            nodes.append({"id": "p1", "type": "camera.publish",
                          "config": {"label": label}})
            edges.append({"id": "e2", "from": "c1", "fromPort": "out",
                          "to": "p1", "toPort": "in"})
        return {"id": "f_cam", "name": "Doorway", "enabled": True,
                "board": "esp32cam", "nodes": nodes, "edges": edges}

    def test_a_flow_that_sends_its_picture_puts_the_device_on_the_screen(self):
        self.flows.save({"flows": [self.camera_flow()]})
        self.fleet.deploy(self.device["id"], "f_cam")
        cams = self.fleet.screen_cameras()
        self.assertEqual([c["id"] for c in cams], [self.device["id"]])
        self.assertEqual(cams[0]["frame_size"], "QVGA")
        self.assertEqual(cams[0]["flow"], "Doorway")

    def test_a_camera_nobody_wired_to_the_screen_is_not_on_it(self):
        self.flows.save({"flows": [self.camera_flow(publish=False)]})
        self.fleet.deploy(self.device["id"], "f_cam")
        self.assertEqual(self.fleet.screen_cameras(), [])

    def test_the_tile_caption_overrides_the_device_name(self):
        self.flows.save({"flows": [self.camera_flow(label="Front door")]})
        self.fleet.deploy(self.device["id"], "f_cam")
        self.assertEqual(self.fleet.screen_cameras()[0]["name"], "Front door")

    def test_a_device_with_no_flow_is_not_on_the_screen(self):
        self.assertEqual(self.fleet.screen_cameras(), [])

    def test_a_camera_flow_is_sent_the_camera_module(self):
        names = self.fleet.agent_files(self.camera_flow(), {"camera": False})
        self.assertIn("modules/camera.py", names)

    def test_the_manifest_says_the_device_has_a_camera(self):
        self.flows.save({"flows": [self.camera_flow()]})
        out, _ = self.enrolled()
        self.fleet.deploy(self.device["id"], "f_cam")
        man = self.fleet.manifest(self.fleet.authenticate(out["token"]))
        self.assertTrue(man["camera"])

    def test_camera_nodes_are_offered_only_to_boards_with_a_camera(self):
        for board, expect in (("esp32cam", True), ("esp32s3", False), ("esp32", False)):
            palette = flowmod.registry_for(iotmod.board_profile(board))
            with self.subTest(board=board):
                self.assertEqual("camera.feed" in palette, expect)
                self.assertEqual("camera.capture" in palette, expect)

    def test_a_camera_node_only_runs_on_a_device(self):
        for ntype in ("camera.feed", "camera.capture"):
            self.assertEqual(flowmod.REGISTRY[ntype]["runs"], "device")
            self.assertTrue(flowmod.runs_on_device(ntype))


class TestReportedState(FleetCase):
    """What a device says about itself, and what the editor gets back."""

    def flow(self):
        return {"id": "f_pins", "name": "Pins", "enabled": True, "board": "esp32cam",
                "nodes": [
                    {"id": "t1", "type": "timer.interval", "config": {"every": 1000}},
                    {"id": "g1", "type": "gpio.out", "config": {"gpio": 33, "action": "toggle"}}],
                "edges": []}

    def test_state_and_screen_state_are_different_methods(self):
        """They collided once, and nothing noticed until a device reported."""
        self.assertTrue(callable(self.fleet.report_state))
        self.assertIn("enrollment", self.fleet.state())

    def test_a_report_is_kept_and_comes_back_on_the_pins(self):
        self.flows.save({"flows": [self.flow()]})
        out, _ = self.enrolled()
        device = self.fleet.authenticate(out["token"])
        self.fleet.deploy(self.device["id"], "f_pins")
        self.fleet.report_state(device, {
            "ip": "10.42.0.64", "rssi": -57, "agent": "0.1.0", "free_ram": 4090000,
            "pins": {"33": {"dir": "out", "value": 1}}})
        view = self.fleet.device_pins(iotmod.get_device(self.devices, self.device["id"]))
        by_gpio = {p["gpio"]: p for p in view["pins"]}
        self.assertEqual(by_gpio[33]["status"], "bound")
        self.assertEqual(by_gpio[33]["live"], {"dir": "out", "value": 1})
        self.assertEqual(by_gpio[33]["used_by"][0]["type"], "gpio.out")
        self.assertEqual(view["device"]["ip"], "10.42.0.64")
        self.assertEqual(view["device"]["agent"], "0.1.0")
        self.assertTrue(view["device"]["online"])

    def test_an_unused_pin_is_free_and_an_unusable_one_is_reserved(self):
        self.flows.save({"flows": [self.flow()]})
        self.fleet.deploy(self.device["id"], "f_pins")
        view = self.fleet.device_pins(iotmod.get_device(self.devices, self.device["id"]))
        by_gpio = {p["gpio"]: p for p in view["pins"]}
        self.assertEqual(by_gpio[13]["status"], "free")
        self.assertEqual(by_gpio[16]["status"], "reserved")   # this module's PSRAM

    def test_nothing_reported_reads_as_nothing_not_as_zero(self):
        view = self.fleet.device_pins(iotmod.get_device(self.devices, self.device["id"]))
        self.assertIsNone(view["reported_age"])
        for pin in view["pins"]:
            self.assertIsNone(pin["live"])

    def test_a_device_cannot_report_pins_that_are_not_numbers(self):
        out, _ = self.enrolled()
        device = self.fleet.authenticate(out["token"])
        self.fleet.report_state(device, {"pins": {"nonsense": {"dir": "out", "value": 1},
                                                  "7": {"dir": "out", "value": 0}}})
        kept = iotmod.get_device(self.devices, self.device["id"])["state"]["pins"]
        self.assertEqual(sorted(kept), ["7"])

    def test_a_report_cannot_rewrite_the_devices_identity(self):
        out, _ = self.enrolled()
        device = self.fleet.authenticate(out["token"])
        self.fleet.report_state(device, {"id": "dev_elsewhere", "name": "Renamed",
                                         "board": "esp32s3", "token": "stolen"})
        after = iotmod.get_device(self.devices, self.device["id"])
        self.assertEqual(after["name"], "Bench CAM")
        self.assertEqual(after["board"], "esp32cam")
        self.assertEqual(after["token"], out["token"])


class EnrollmentWindow(FleetCase):
    """The window opens itself, says why, and records who it turned away."""

    def test_the_window_remembers_why_it_opened(self):
        self.fleet.provision(self.device["id"])
        win = self.fleet.window_state()
        self.assertIn("Bench CAM", win["reason"])

    def test_opening_by_hand_says_so(self):
        self.fleet.open_window(300)
        self.assertEqual(self.fleet.window_state()["reason"], "opened by hand")

    def test_a_finished_flash_opens_a_longer_window_for_that_board(self):
        self.fleet.provision(self.device["id"])
        self.fleet.close_window()                    # as an expiry would
        self.fleet._flashed(self.device)
        win = self.fleet.window_state()
        self.assertIsNotNone(win)
        self.assertIn("just flashed", win["reason"])
        self.assertGreater(win["expires"] - win["opened"], fleetmod.ENROLL_WINDOW)
        self.assertEqual(self.fleet.awaiting, self.device["id"])

    def test_a_finished_flash_stamps_the_device(self):
        # `flashed` was initialised, read, and never written by anything.
        self.assertIsNone(iotmod.get_device(self.devices, self.device["id"])["flashed"])
        self.fleet._flashed(self.device)
        self.assertIsNotNone(
            iotmod.get_device(self.devices, self.device["id"])["flashed"])

    def test_the_window_closes_once_the_board_it_waited_for_joins(self):
        prov = self.fleet.provision(self.device["id"])
        self.assertIsNotNone(self.fleet.window_state())
        self.fleet.enroll(self.device["id"], prov["enroll_token"], {})
        self.assertIsNone(self.fleet.window_state())

    def test_a_window_opened_by_hand_survives_an_enrolment(self):
        prov = self.fleet.provision(self.device["id"])
        self.fleet.open_window(300)                  # by hand, for nobody
        self.fleet.enroll(self.device["id"], prov["enroll_token"], {})
        self.assertIsNotNone(self.fleet.window_state())

    def test_a_board_turned_away_is_recorded(self):
        prov = self.fleet.provision(self.device["id"])
        self.fleet.close_window()
        with self.assertRaises(iotmod.NetError):
            self.fleet.enroll(self.device["id"], prov["enroll_token"], {},
                              ip="10.42.0.55")
        refused = self.fleet.state()["refusals"]
        self.assertEqual(len(refused), 1)
        self.assertEqual(refused[0]["device"], self.device["id"])
        self.assertEqual(refused[0]["ip"], "10.42.0.55")
        self.assertIn("closed", refused[0]["reason"])

    def test_every_way_of_being_refused_is_recorded(self):
        prov = self.fleet.provision(self.device["id"])
        for device_id, token in ((self.device["id"], "wrong"),
                                 ("nope", prov["enroll_token"]),
                                 (None, None)):
            with self.assertRaises(iotmod.NetError):
                self.fleet.enroll(device_id, token, {})
        reasons = [r["reason"] for r in self.fleet.state()["refusals"]]
        self.assertEqual(len(reasons), 3)
        self.assertEqual(len(set(reasons)), 3, reasons)

    def test_the_refusal_log_cannot_grow_without_bound(self):
        # An unauthenticated route writes to this list.
        self.fleet.close_window()
        for _ in range(60):
            with self.assertRaises(iotmod.NetError):
                self.fleet.enroll("whatever", "nope", {})
        self.assertLessEqual(len(self.fleet.state()["refusals"]), 20)

    def test_a_successful_enrolment_records_no_refusal(self):
        self.enrolled()
        self.assertEqual(self.fleet.state()["refusals"], [])


class FlashHousekeeping(FleetCase):
    def test_flashing_one_board_keeps_the_other_cameras_frames(self):
        self.fleet.frames["other"] = {"png": b"", "at": 0, "width": 1, "height": 1}
        self.fleet.frames[self.device["id"]] = {"png": b"", "at": 0,
                                                "width": 1, "height": 1}
        try:
            self.fleet.flash(self.device["id"], "/dev/null", "ssid", "psk",
                             extra=["--help"])
        except iotmod.NetError:
            pass
        if self.fleet.flashing:
            self.fleet.flashing.join(timeout=20)
        self.assertIn("other", self.fleet.frames)
        self.assertNotIn(self.device["id"], self.fleet.frames)


if __name__ == "__main__":
    unittest.main()
