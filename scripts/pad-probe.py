#!/usr/bin/env python3
"""Show what a gamepad on this host actually reports.

    python3 scripts/pad-probe.py
    python3 scripts/pad-probe.py --name xbox --seconds 30

Prints every pad the kernel shows, the axis ranges and layout the reader
chose, then the live scaled values: move one control at a time and watch which
name moves. The host twin of `scripts/ble-pad-probe.py`.

Pair the pad on the IoT screen's Bluetooth tab first. Read-only; needs the
`input` group.
"""
import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from zero2w_console import pad                              # noqa: E402
from zero2w_console.agent.modules import pad as padmod       # noqa: E402

BAR = 28


def say(*bits):
    print(*bits)
    sys.stdout.flush()


def meter(value, low=-1.0):
    """One axis as a bar."""
    span = 1.0 - low
    at = int(round((value - low) / span * (BAR - 1))) if span else 0
    at = max(0, min(BAR - 1, at))
    row = ["-"] * BAR
    row[at] = "#"
    return "".join(row)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--name", default="",
                    help="prefer a pad whose name contains this")
    ap.add_argument("--seconds", type=int, default=20,
                    help="how long to stream for")
    a = ap.parse_args()

    say("1. what the kernel is showing")
    seen = pad.pads()
    if not seen:
        say("   nothing with a joystick handler. Is the pad paired and on?")
        say("   " + pad.PAIR_HINT)
        return 1
    for row in seen:
        say("   %-34s %s%s" % (row["name"], row["path"],
                               "" if row["readable"] else "  NOT READABLE"))
    if not any(r["readable"] for r in seen):
        say("")
        say("   None of them is readable by this user. /dev/input is root:input,")
        say("   so: sudo usermod -aG input %s   then log out and back in."
            % os.environ.get("USER", "you"))
        return 1

    state = padmod.blank()
    reader = pad.Reader(state, a.name)
    path, name = reader.pick()
    if not path:
        say("   nothing matching %r" % a.name)
        return 1

    say("")
    say("2. what the driver says about its axes")
    fd = os.open(path, os.O_RDONLY)
    try:
        reader.read_ranges(fd)
    finally:
        os.close(fd)
    if not reader.ranges:
        say("   it reports no absolute axes at all — this is not a stick.")
        return 1
    say("   layout: %s" % reader.layout)
    if reader.layout == "standard":
        say("   (xpad's layout: ABS_Z/ABS_RZ are the triggers)")
    elif reader.layout.startswith("quirk"):
        say("   (a known vendor — ABS_Z/ABS_RZ are the right stick on these)")
    else:
        say("   (worked out from where each axis rests: a stick centres, a "
            "trigger bottoms out)")
    for key in sorted(reader.ranges):
        low, high, flat = reader.ranges[key]
        say("   %-4s %8d .. %-8d flat %-6d %s"
            % (key, low, high, flat,
               "one-way" if key in padmod.ONE_WAY else "centred"))
    say("   (anything not listed is a d-pad, which is -1..1 by definition)")

    say("")
    say("3. live, for %ds — move one control at a time" % a.seconds)
    say("   %s" % name)
    reader.start()
    deadline = time.time() + a.seconds
    try:
        while time.time() < deadline and reader.is_alive():
            time.sleep(0.1)
            rows = []
            for label, key in (("LX", "lx"), ("LY", "ly"),
                               ("RX", "rx"), ("RY", "ry")):
                rows.append("%s %s" % (label, meter(state.get(key) or 0.0)))
            for label, key in (("LT", "lt"), ("RT", "rt")):
                rows.append("%s %s" % (label, meter(state.get(key) or 0.0, 0.0)))
            down = [k for k in sorted(padmod.BUTTONS.values()) if state.get(k)]
            say("\x1b[2J\x1b[H" + "\n".join(rows)
                + "\n\ndown: %s" % (" ".join(down) or "-")
                + "\nreports: %d" % (state.get("seq") or 0))
    except KeyboardInterrupt:
        pass
    finally:
        reader.stop()
        reader.join(timeout=1)

    say("")
    if reader.error:
        say("ended: %s" % reader.error)
    if not state.get("seq"):
        say("It never sent anything. If it is connected, it only reports on")
        say("change — which a Controller node's `stale_ms` has to allow for.")
        return 1
    say("%d reports. The names above are the ones the Controller axis and"
        % state["seq"])
    say("Controller button nodes offer, so what moved is what to select.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
