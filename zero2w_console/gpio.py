#!/usr/bin/env python3
"""GPIO access for the Zero 2W, through the libgpiod v2 CLI tools."""
import json
import os
import re
import subprocess
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PINOUT = os.path.join(HERE, "data", "pinout-zero2w.json")

# "line   5:  \"PA5\"  \"regulator\"  output active-high [used]"
LINE_RE = re.compile(
    r'^\s*line\s+(?P<offset>\d+):\s*'
    r'(?P<name>"[^"]*"|unnamed)\s*'
    r'(?P<rest>.*)$'
)
CONSUMER_RE = re.compile(r'"([^"]*)"')


def _run(args, timeout=6):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", "%s not installed" % args[0]
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"
    except Exception as exc:
        return 1, "", str(exc)


class PinInventory:
    """The board's header, merged with what the kernel says each line is doing."""

    def __init__(self):
        with open(PINOUT) as fh:
            self.board = json.load(fh)
        self.pins = self.board["pins"]
        # Which gpiochip carries the 40-pin header is NOT fixed: on this board
        # gpiochip0 is r_pio (PL bank only) and gpiochip1 is the main pinctrl.
        # Resolve it from the hardware rather than hard-code an index that can
        # differ between kernels.
        self.main_chip = self._find_main_chip()
        for pin in self.pins:
            if pin.get("kind") == "gpio":
                pin["chip"] = self.main_chip
        self.by_gpio = {p["gpio"]: p for p in self.pins if p["kind"] == "gpio"}
        self._cache = {"at": 0.0, "lines": {}, "error": None}
        self.lock = threading.Lock()

    def _find_main_chip(self):
        """The chip with enough lines to hold the highest header offset."""
        need = 0
        for p in self.board["pins"]:
            if p.get("kind") == "gpio":
                need = max(need, p["line"])
        code, out, _ = _run(["gpiodetect"])
        best, best_lines = None, -1
        if code == 0:
            for line in out.splitlines():
                m = re.match(r"(\S*gpiochip(\d+))\s+\[([^\]]*)\]\s+\((\d+)\s+lines\)",
                             line.strip())
                if not m:
                    continue
                idx, label, n = int(m.group(2)), m.group(3), int(m.group(4))
                if n > need and n > best_lines:
                    best, best_lines = idx, n
        if best is None:
            # No access yet (permissions) — fall back to the file's own value.
            for p in self.board["pins"]:
                if p.get("kind") == "gpio":
                    return p.get("chip", 0)
            return 0
        return best

    # -- access ------------------------------------------------------------
    def access(self):
        """Can this process open the gpiochips at all?"""
        code, out, err = _run(["gpiodetect"])
        if code == 0:
            chips = []
            for line in out.splitlines():
                m = re.match(r"(\S+)\s+\[([^\]]*)\]\s+\((\d+)\s+lines\)", line.strip())
                if m:
                    chips.append({"chip": m.group(1), "label": m.group(2),
                                  "lines": int(m.group(3))})
            return {"ok": True, "chips": chips, "error": None}
        reason = (err or "").strip() or "gpiodetect failed"
        return {"ok": False, "chips": [], "error": reason,
                "hint": self._denied_hint() if "Permission denied" in reason else None}

    @staticmethod
    def _denied_hint():
        """Tell apart 'not set up yet' from 'set up, but this process is
        stale'."""
        import grp
        import pwd
        chip = "/dev/gpiochip0"
        try:
            st = os.stat(chip)
            dev_group = grp.getgrgid(st.st_gid).gr_name
            group_ok = dev_group == "gpio" and bool(st.st_mode & 0o060)
        except (OSError, KeyError):
            return ("%s is not reachable. Run: sudo scripts/setup-gpio.sh" % chip)
        if not group_ok:
            return ("%s is root-only. Run: sudo scripts/setup-gpio.sh, then log out "
                    "and back in." % chip)
        user = pwd.getpwuid(os.getuid()).pw_name
        try:
            members = set(grp.getgrnam("gpio").gr_mem)
        except KeyError:
            members = set()
        in_group = user in members
        have_now = os.getgid() in os.getgroups() and any(
            g == grp.getgrnam("gpio").gr_gid for g in os.getgroups()) if in_group else False
        if in_group and not have_now:
            return ("Permissions are correct and %s is in the gpio group, but "
                    "THIS process started before that and still has the old "
                    "credentials. Restart the console: "
                    "`systemctl restart zero2w-console` (or stop it and start "
                    "it again from a shell you have logged into since)." % user)
        if not in_group:
            return ("%s is not in the gpio group. Run: sudo scripts/setup-gpio.sh" % user)
        return "gpiodetect was denied despite correct permissions."

    # -- live line state ---------------------------------------------------
    def lines(self, max_age=3.0):
        """Parse `gpioinfo` into {(chip_index, offset): {...}}, cached briefly."""
        with self.lock:
            if time.monotonic() - self._cache["at"] < max_age:
                return self._cache["lines"], self._cache["error"]
            code, out, err = _run(["gpioinfo"])
            lines, error = {}, None
            if code != 0:
                error = (err or "gpioinfo failed").strip()
            else:
                chip_idx = None
                for raw in out.splitlines():
                    head = re.match(r"^(\S*gpiochip(\d+))\s+-\s+(\d+)\s+lines:", raw.strip())
                    if head:
                        chip_idx = int(head.group(2))
                        continue
                    m = LINE_RE.match(raw)
                    if not m or chip_idx is None:
                        continue
                    offset = int(m.group("offset"))
                    name = m.group("name")
                    name = None if name == "unnamed" else name.strip('"')
                    rest = m.group("rest")
                    consumer = None
                    cm = CONSUMER_RE.search(rest)
                    if cm:
                        consumer = cm.group(1) or None
                    # The field before "output" may be a tab, so match on a
                    # word boundary rather than a leading space.
                    direction = "output" if re.search(r"\boutput\b", rest) else "input"
                    used = "[used]" in rest or bool(consumer)
                    lines[(chip_idx, offset)] = {
                        "name": name, "consumer": consumer,
                        "direction": direction, "used": used,
                    }
            self._cache = {"at": time.monotonic(), "lines": lines, "error": error}
            return lines, error

    # -- the merged view the UI renders ------------------------------------
    def snapshot(self, bindings=None, held=None):
        """bindings: {gpio: [flow labels]} · held: {gpio: value} driven by us."""
        bindings = bindings or {}
        held = held or {}
        live, err = self.lines()
        acc = self.access()
        pins = []
        for p in self.pins:
            e = dict(p)
            if p["kind"] == "gpio":
                info = live.get((p["chip"], p["line"]))
                consumer = info["consumer"] if info else None
                # Our own holder processes are not a foreign claim.
                ours = consumer in (None, "console-out", "console-in")
                e["consumer"] = consumer
                e["direction"] = info["direction"] if info else None
                e["kernel_used"] = bool(info and info["used"] and not ours)
                e["bound"] = bindings.get(p["gpio"], [])
                e["held"] = held.get(p["gpio"])
                if e["kernel_used"]:
                    e["status"] = "reserved"
                elif e["bound"]:
                    e["status"] = "bound"
                elif not acc["ok"]:
                    e["status"] = "unknown"
                else:
                    e["status"] = "free"
            else:
                e["status"] = p["kind"]
            pins.append(e)
        return {
            "board": self.board["board"],
            "soc": self.board["soc"],
            "source": self.board["source"],
            "access": acc,
            "error": err,
            "pins": pins,
        }

    def drivable(self, gpio):
        """(ok, reason) — the single gate every write goes through."""
        p = self.by_gpio.get(gpio)
        if not p:
            return False, "GPIO %s is not on this board's 40-pin header" % gpio
        live, _ = self.lines()
        info = live.get((p["chip"], p["line"]))
        if info and info["used"] and info["consumer"] not in (None, "console-out", "console-in"):
            return False, "line is claimed by '%s'" % info["consumer"]
        return True, None


class PinDriver:
    """Reads lines, and holds outputs with one long-lived gpioset per pin."""

    def __init__(self, inventory):
        self.inv = inventory
        self.held = {}          # gpio -> {"proc": Popen, "value": int}
        self.lock = threading.Lock()

    @staticmethod
    def _chip(pin):
        return "gpiochip%d" % pin["chip"]

    def read(self, gpio):
        pin = self.inv.by_gpio.get(gpio)
        if not pin:
            return None, "GPIO %s is not on the header" % gpio
        with self.lock:
            if gpio in self.held:
                return self.held[gpio]["value"], None
        code, out, err = _run(["gpioget", "--numeric", "-C", "console-in",
                               "-c", self._chip(pin), str(pin["line"])])
        if code != 0:
            return None, (err or "gpioget failed").strip()
        try:
            return int(out.strip().split()[-1]), None
        except (ValueError, IndexError):
            return None, "unparsable gpioget output: %r" % out[:60]

    def write(self, gpio, value):
        ok, reason = self.inv.drivable(gpio)
        if not ok:
            return False, reason
        pin = self.inv.by_gpio[gpio]
        value = 1 if value else 0
        with self.lock:
            cur = self.held.get(gpio)
            if cur and cur["value"] == value and cur["proc"].poll() is None:
                return True, None
            self._release_locked(gpio)
            try:
                # --banner makes the process print once it actually owns the
                # line, so a failure to claim surfaces immediately.
                proc = subprocess.Popen(
                    ["gpioset", "--banner", "-C", "console-out",
                     "-c", self._chip(pin), "%d=%d" % (pin["line"], value)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            except Exception as exc:
                return False, str(exc)
            time.sleep(0.12)
            if proc.poll() is not None:
                err = (proc.stderr.read() or "").strip()
                return False, err or "gpioset exited immediately"
            self.held[gpio] = {"proc": proc, "value": value}
        return True, None

    def release(self, gpio):
        with self.lock:
            self._release_locked(gpio)
        return True, None

    def _release_locked(self, gpio):
        cur = self.held.pop(gpio, None)
        if cur:
            try:
                cur["proc"].terminate()
                cur["proc"].wait(timeout=2)
            except Exception:
                try:
                    cur["proc"].kill()
                except Exception:
                    pass
            self.reset_to_input(gpio)

    def reset_to_input(self, gpio):
        """Put a line back to high-impedance input."""
        pin = self.inv.by_gpio.get(gpio)
        if not pin:
            return
        _run(["gpioget", "--numeric", "-C", "console-reset",
              "-c", self._chip(pin), str(pin["line"])], timeout=4)

    def release_all(self):
        with self.lock:
            for gpio in list(self.held):
                self._release_locked(gpio)

    def reap(self):
        """Drop holders that died on us."""
        lost = []
        with self.lock:
            for gpio, v in list(self.held.items()):
                if v["proc"].poll() is not None:
                    self.held.pop(gpio, None)
                    lost.append(gpio)
        for gpio in lost:
            self.reset_to_input(gpio)
        return lost

    def held_values(self):
        self.reap()
        with self.lock:
            return {g: v["value"] for g, v in self.held.items()
                    if v["proc"].poll() is None}


class EdgeWatcher(threading.Thread):
    """One gpiomon process per watched pin; calls back on each edge."""

    daemon = True

    def __init__(self, inventory, gpio, edges, callback, debounce_ms=0):
        super().__init__(name="gpiomon-%s" % gpio)
        self.inv, self.gpio, self.edges = inventory, gpio, edges
        self.callback, self.debounce = callback, debounce_ms / 1000.0
        self.proc = None
        self._stop = threading.Event()
        self._last = 0.0

    def run(self):
        pin = self.inv.by_gpio.get(self.gpio)
        if not pin:
            return
        args = ["gpiomon", "--banner", "-C", "console-in",
                "-e", self.edges, "-c", "gpiochip%d" % pin["chip"],
                "--format", "%e %o", str(pin["line"])]
        try:
            self.proc = subprocess.Popen(args, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, text=True, bufsize=1)
        except Exception:
            return
        for raw in self.proc.stdout:
            if self._stop.is_set():
                break
            parts = raw.split()
            if not parts or not parts[0].isdigit():
                continue
            now = time.monotonic()
            if self.debounce and now - self._last < self.debounce:
                continue
            self._last = now
            edge = "rising" if parts[0] == "1" else "falling"
            try:
                self.callback(self.gpio, edge)
            except Exception:
                pass

    def stop(self):
        self._stop.set()
        if self.proc:
            try:
                self.proc.terminate()
            except Exception:
                pass


if __name__ == "__main__":
    inv = PinInventory()
    acc = inv.access()
    print("board :", inv.board["board"], "-", inv.board["soc"])
    print("access:", "OK" if acc["ok"] else "DENIED — " + (acc.get("hint") or acc["error"]))
    for c in acc["chips"]:
        print("  %s [%s] %d lines" % (c["chip"], c["label"], c["lines"]))
    snap = inv.snapshot()
    free = [p for p in snap["pins"] if p.get("status") == "free"]
    res = [p for p in snap["pins"] if p.get("status") == "reserved"]
    print("header: %d gpio · %d free · %d reserved by kernel drivers"
          % (len(inv.by_gpio), len(free), len(res)))
    for p in res:
        print("   pin %-2d %-5s claimed by %s" % (p["phys"], p["label"], p["consumer"]))
