#!/usr/bin/env python3
"""Browse and exercise any BLE device's GATT from this host.

    python3 scripts/ble-gatt-probe.py --name zero2w-gatt
    python3 scripts/ble-gatt-probe.py --mac AA:BB:CC:DD:EE:FF --write 2a56=01

Finds the device, connects, prints its attribute table, reads every readable
characteristic and watches every notifying one; `--write` writes one too. This
is `zero2w_console/gatt.py` against a real link — `scripts/ble-peripheral.py`
puts a board up as a target with known answers.

Read-only unless you pass --write. Unprivileged.
"""
import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from zero2w_console import bluetooth as btmod              # noqa: E402
from zero2w_console import gatt                            # noqa: E402


def say(*bits):
    print(*bits)
    sys.stdout.flush()


def find(name, mac, seconds):
    """The device, scanning for it if it is not already known."""
    for device in gatt.devices():
        if mac and device["mac"].upper() == mac.upper():
            return device
        if name and name.lower() in (device["name"] or "").lower():
            return device
    say("not known to BlueZ yet — scanning %ds" % seconds)
    btmod.scan(seconds)
    # Searched during the scan: BlueZ forgets an unpaired device when it ends.
    end = time.time() + seconds + 8
    while time.time() < end:
        for device in gatt.devices():
            if mac and device["mac"].upper() == mac.upper():
                return device
            if name and name.lower() in (device["name"] or "").lower():
                return device
        if not btmod.scan_state()["running"]:
            break
        time.sleep(1)
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--name", default="", help="match a device by name")
    ap.add_argument("--mac", default="", help="or by address")
    ap.add_argument("--seconds", type=int, default=12, help="how long to scan")
    ap.add_argument("--watch", type=int, default=8,
                    help="how long to watch subscribed values move")
    ap.add_argument("--write", default="",
                    help="uuid=hex, e.g. f0de0003-...=01ff. Writes, then reads back.")
    a = ap.parse_args()
    if not a.name and not a.mac:
        raise SystemExit("ble-gatt-probe: pass --name or --mac")

    say("1. the adapter")
    status = btmod.status(force=True)
    adapter = status["adapter"]
    if not adapter["present"]:
        say("   " + status.get("missing", "no adapter"))
        return 1
    say("   %s  powered=%s  can_serve_gatt=%s"
        % (adapter["address"], adapter["powered"], adapter["can_serve_gatt"]))

    say("")
    say("2. finding it")
    device = find(a.name, a.mac, a.seconds)
    if device is None:
        say("   nothing matching %r. A BLE device only shows up while it is"
            % (a.mac or a.name))
        say("   advertising — is the peripheral actually running?")
        return 1
    say("   %s  %r  le=%s  connected=%s"
        % (device["mac"], device["name"], device["le"], device["connected"]))
    if device["classic_hid"]:
        say("")
        say("   This is a Bluetooth Classic HID device. It has no GATT at all —")
        say("   the kernel drives it through the HID stack, which is what")
        say("   scripts/pad-probe.py reads. There is nothing here to browse.")
        return 1

    if not device["connected"]:
        say("")
        say("3. connecting")
        out = gatt.connect(device["mac"])
        say("   " + out["detail"])
        if not out["ok"]:
            return 1
    else:
        say("")
        say("3. already connected")

    # The attribute tree fills in after the link comes up.
    say("")
    say("4. waiting for BlueZ to resolve its services")
    services = []
    end = time.time() + 15
    while time.time() < end:
        known = gatt.tree()
        services = gatt.gatt(device["mac"], known)
        if services:
            break
        time.sleep(0.5)
    if not services:
        say("   it published none. Some devices only do once bonded.")
        return 1
    say("   %d service(s)" % len(services))

    say("")
    say("5. the attribute table")
    readable, notifying = [], []
    for svc in services:
        say("   %s  %s" % (svc["name"], "(primary)" if svc["primary"] else ""))
        say("     %s" % svc["uuid"])
        for ch in svc["chars"]:
            flags = ",".join(ch["flags"])
            say("     - %-28s %s" % (ch["name"], flags))
            if ch["name"] != ch["uuid"]:
                say("       %s" % ch["uuid"])
            if "read" in ch["flags"]:
                readable.append(ch)
            if "notify" in ch["flags"] or "indicate" in ch["flags"]:
                notifying.append(ch)

    say("")
    say("6. reading every readable characteristic")
    for ch in readable:
        raw = gatt.read(ch["path"])
        if raw is None:
            say("   %-28s could not be read" % ch["name"])
        else:
            text = "".join(chr(b) if 32 <= b < 127 else "." for b in raw)
            say("   %-28s %-24s %r" % (ch["name"], raw.hex() or "(empty)", text))

    if a.write:
        say("")
        say("7. writing")
        want, _, hexed = a.write.partition("=")
        path = gatt.find_char(device["mac"], want.strip())
        if path is None:
            say("   no characteristic %r on this device" % want.strip())
        else:
            try:
                payload = bytes.fromhex(hexed.strip())
            except ValueError:
                say("   %r is not hex" % hexed)
                payload = None
            if payload is not None:
                ok = gatt.write(path, payload)
                say("   wrote %s: %s" % (payload.hex(), "accepted" if ok
                                         else "BlueZ refused it"))
                back = gatt.read(path)
                if back is not None:
                    say("   reads back as %s %s" % (back.hex(),
                        "— matches" if back == payload else "— DIFFERENT"))

    if notifying:
        say("")
        say("8. subscribing, and watching for %ds" % a.watch)
        # Held by a live session: BlueZ ends a subscription when the process
        # that asked for it exits, which a one-off gdbus call does at once.
        holders = [gatt.hold_notify(ch["path"]) for ch in notifying]
        for ch in notifying:
            say("   %s: subscribed" % ch["name"])
        want = set(ch["path"] for ch in notifying)
        seen = {}
        end = time.time() + a.watch
        while time.time() < end:
            time.sleep(1)
            known = gatt.tree()
            for svc in gatt.gatt(device["mac"], known):
                for ch in svc["chars"]:
                    if ch["path"] not in want:
                        continue
                    value = bytes(ch["value"]).hex()
                    if seen.get(ch["path"]) != value:
                        seen[ch["path"]] = value
                        say("   %-28s %s" % (ch["name"], value or "(empty)"))
        for holder in holders:
            gatt.release_notify(holder)
        if not seen:
            say("   nothing arrived. A characteristic can be subscribable and")
            say("   silent — the device decides when it has something to say.")

    say("")
    say("Done. The device is left connected; disconnect it on the Bluetooth tab")
    say("or with: bluetoothctl disconnect %s" % device["mac"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
