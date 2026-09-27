"""The device runner reads the keys the editor actually writes."""
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from zero2w_console import flows  # noqa: E402
from zero2w_console import fleet as fleetmod  # noqa: E402

AGENT_DIR = os.path.join(ROOT, "zero2w_console", "agent")
AGENT = os.path.join(AGENT_DIR, "flow.py")

# Keys a handler may read that are not config fields: a port's action is
# built from the port name, and these are message parts rather than settings.
NOT_FIELDS = re.compile(r"_action$")

# Fields a device is entitled to ignore, and why. Anything not in here that the
# editor offers but the runner never reads is a lie told to whoever fills in the
# form: they set it, and nothing happens.
DEVICE_IGNORES = {
    # Host PWM picks a sysfs chip and channel. MicroPython's LEDC assigns its
    # own, so there is nothing on a device for these to point at.
    ("pwm.out", "mode"),
    ("pwm.out", "channel"),
    # Placement and cadence only: the host draws the tile and decides how
    # often to ask. The device just answers when asked.
    ("camera.publish", "label"),
    ("camera.publish", "every_ms"),
    # This sensor is a bare board behind a protector and the frame arrives raw:
    # the host turns it the right way up in pixels.orient().
    ("camera.feed", "rotate"),
    ("camera.feed", "mirror"),
    # A device has one I2C peripheral and names its pins; a bus number is a
    # /dev/i2c-N on this host and means nothing on a board.
    ("i2c.read", "bus"),
    ("i2c.write", "bus"),
    ("i2c.scan", "bus"),
}

# Keys the device reads that the editor does not offer at all — the capability is
# there and unreachable, which is the opposite failure.
NOT_DECLARED = {
    # The runner will set a pull-up, but gpio.in has no field for it, so every
    # device input is floating unless the board pulls it in hardware.
    ("gpio.in", "pull"),
}

# Real divergences, recorded rather than blessed: the editor offers these and a
# device quietly does not honour them. Not "entitled to ignore", just not fixed.
KNOWN_GAPS = {
    # The host formats the bytes it read; a device hands back raw, so the
    # payload's shape depends on where the flow runs.
    ("i2c.read", "format"),
    # A still is taken at whatever size the feed is already running.
    ("camera.capture", "frame_size"),
    # Device requests go out with no extra headers, so anything needing an
    # API key works here and not there.
    ("http.request", "headers"),
}

# Fields read somewhere other than the node's own handler, with the method that
# reads them. Checked below: a renamed method leaves the excuse behind.
READ_ELSEWHERE = {
    # Interval triggers are armed once when the flow starts, not per message.
    ("timer.interval", "every"): "def start(self)",
    # An input pin is wired to its IRQ when the flow starts.
    ("gpio.in", "gpio"): "def _watch(self, node)",
    ("gpio.in", "edges"): "def _watch(self, node)",
    ("gpio.in", "pull"): "def _watch(self, node)",
    # Debounce is applied in tick(), because an IRQ handler must not allocate.
    ("gpio.in", "debounce"): "def tick(self)",
    # A watchdog fires because nothing arrived, so the value it sends is read
    # by the loop that notices, not by the handler that gets fed.
    ("safety.watchdog", "value"): "def tick(self)",
}


def method_body(src, header):
    """One method's source, from its `def` to the next one."""
    at = src.index(header)
    body = src[at:]
    cuts = [c for c in (body.find("\n    def "), body.find("\n\ndef ")) if c != -1]
    return body[:min(cuts)] if cuts else body


def keys_in(text):
    """Every config key a stretch of source reads."""
    keys = set()
    for call in re.findall(r"cfg\.get\(([^()]*(?:\([^()]*\)[^()]*)*)\)", text):
        keys.update(re.findall(r'"([^"]+)"', first_argument(call)))
    return keys


def first_argument(call):
    """Everything up to the top-level comma."""
    depth, quoted = 0, None
    for i, ch in enumerate(call):
        if quoted:
            if ch == quoted:
                quoted = None
        elif ch in "\"'":
            quoted = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            return call[:i]
    return call


def loose_keys_in(text):
    """Any `.get("key")` at all, for the shared methods."""
    return set(re.findall(r'\.get\(\s*"([^"]+)"', text))


def module_source(ntype):
    """The pulled module this node's handler delegates to, if it has one."""
    name = fleetmod.NODE_MODULES.get(ntype)
    if not name:
        return ""
    path = os.path.join(AGENT_DIR, "modules", name + ".py")
    if not os.path.exists(path):
        return ""
    with open(path) as fh:
        return fh.read()


def handlers():
    """(node type, keys its own handler reads, keys read anywhere for it)."""
    with open(AGENT) as fh:
        src = fh.read()
    bodies = {}
    for m in re.finditer(r"def (_do_\w+)\(.*?(?=\n    def |\Z)", src, re.S):
        name, body = m.group(1), m.group(0)
        parts = name[len("_do_"):].split("_", 1)
        if len(parts) == 2:
            bodies[parts[0] + "." + parts[1]] = body

    # A handler moved into a pulled module is still a handler, and must not
    # quietly drop out of this check by no longer being in flow.py.
    for ntype, where in sorted(fleetmod.NODE_MODULES.items()):
        if ntype in bodies:
            continue
        name = ntype.replace(".", "_")
        text = module_source(ntype)
        m = re.search(r"\ndef %s\(.*?(?=\ndef |\Z)" % name, text, re.S)
        if m:
            bodies[ntype] = m.group(0)

    for ntype in sorted(bodies):
        if ntype not in flows.REGISTRY:
            continue
        body = bodies[ntype]
        own = {k for k in keys_in(body) if not NOT_FIELDS.search(k)}
        anywhere = set(own) | keys_in(module_source(ntype))
        for (node, key), header in READ_ELSEWHERE.items():
            if node == ntype and key in loose_keys_in(method_body(src, header)):
                anywhere.add(key)
        yield ntype, own, anywhere


class TestDeviceHandlersReadRealFields(unittest.TestCase):
    def test_there_is_a_handler_to_check(self):
        """A rename that broke the scan would otherwise pass silently."""
        found = {ntype for ntype, _own, _all in handlers()}
        self.assertIn("gpio.out", found)
        self.assertGreater(len(found), 8)

    def test_every_key_a_handler_reads_is_a_field_on_that_node(self):
        for ntype, own, _all in handlers():
            declared = {f["key"] for f in flows.REGISTRY[ntype]["fields"]}
            for key in sorted(own):
                if (ntype, key) in NOT_DECLARED:
                    continue
                with self.subTest(node=ntype, key=key):
                    self.assertIn(key, declared,
                                  "%s reads cfg[%r], which the editor never "
                                  "writes" % (ntype, key))

    def test_every_field_the_editor_offers_is_read_by_the_handler(self):
        """The other direction, and the one that let `pwm.out` sit broken."""
        for ntype, _own, keys in handlers():
            spec = flows.REGISTRY[ntype]
            for field in spec["fields"]:
                key = field["key"]
                if (ntype, key) in DEVICE_IGNORES or (ntype, key) in KNOWN_GAPS \
                        or NOT_FIELDS.search(key):
                    continue
                with self.subTest(node=ntype, key=key):
                    self.assertIn(
                        key, keys,
                        "%s offers a %r field that the device never reads — "
                        "either read it, or add it to DEVICE_IGNORES with a "
                        "reason" % (ntype, key))

    def test_the_ignore_list_only_names_fields_that_exist(self):
        """Otherwise a renamed field leaves a stale excuse behind, and the
        new name goes unchecked."""
        every = sorted(DEVICE_IGNORES) + sorted(READ_ELSEWHERE) + sorted(KNOWN_GAPS)
        for ntype, key in every:
            if (ntype, key) in NOT_DECLARED:
                continue      # that list is precisely the undeclared ones
            with self.subTest(node=ntype, key=key):
                self.assertIn(ntype, flows.REGISTRY)
                declared = {f["key"] for f in flows.REGISTRY[ntype]["fields"]}
                self.assertIn(key, declared)

    def test_every_excuse_names_a_method_that_still_exists(self):
        """READ_ELSEWHERE points at the method doing the reading."""
        with open(AGENT) as fh:
            src = fh.read()
        for (ntype, key), header in sorted(READ_ELSEWHERE.items()):
            with self.subTest(node=ntype, key=key):
                self.assertIn(header, src,
                              "%s is gone, so nothing checks %s.%s any more"
                              % (header, ntype, key))
                self.assertIn(key, loose_keys_in(method_body(src, header)),
                              "%s no longer reads %r" % (header, key))

    def test_the_values_a_handler_branches_on_are_ones_a_dropdown_can_produce(self):
        """A branch on a string no field offers is dead code — which is what
        `action == "follow"` was, against a dropdown offering "from-
        payload"."""
        with open(AGENT) as fh:
            src = fh.read()
        # Port names and message parts are compared too, and are not
        # field values.
        allowed_everywhere = {"in", "in2", "reset", "out", "payload"}
        for ntype, spec in flows.REGISTRY.items():
            handler = "def _do_" + ntype.replace(".", "_") + "("
            if handler in src:
                body = src[src.index(handler):]
                # Stop at the next method *or* at the end of the class, so the
                # last handler does not swallow the module-level helpers below it.
                cuts = [c for c in (body.find("\n    def "),
                                    body.find("\n\ndef ")) if c != -1]
                body = body[:min(cuts)] if cuts else body
            else:
                # A pulled module is scanned whole, because its helpers do the
                # branching: `axis_value` reads `axis`, not the handler.
                body = module_source(ntype)
                if "def " + ntype.replace(".", "_") + "(" not in body:
                    continue
            offered = set(allowed_everywhere)
            # One pulled module implements several node types, and the scan
            # above is of the whole file, so the options of every type it
            # implements are legitimately reachable in it.
            kin = [spec]
            where = fleetmod.NODE_MODULES.get(ntype)
            if where and handler not in src:
                kin = [flows.REGISTRY[other]
                       for other, mod in fleetmod.NODE_MODULES.items()
                       if mod == where and other in flows.REGISTRY]
            for sibling in kin:
                for field in sibling["fields"]:
                    offered.update(field.get("options") or [])
                # And its own ports, which a handler compares r.arrived with.
                for port in (sibling.get("inputs") or []) + (sibling.get("outputs") or []):
                    offered.add(port if isinstance(port, str) else port["name"])
            branched = set(re.findall(r'==\s*"([a-z][a-z0-9_-]*)"', body))
            with self.subTest(node=ntype):
                self.assertFalse(
                    branched - offered,
                    "%s branches on %s, which nothing can set it to"
                    % (ntype, sorted(branched - offered)))


if __name__ == "__main__":
    unittest.main()
