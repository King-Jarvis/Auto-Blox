#!/usr/bin/env python3
"""Ask a board whether it can bond with a BLE gamepad, and print what it
sees."""
import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from zero2w_console import serialport                      # noqa: E402

DEVICE_PROBE = os.path.join(ROOT, "scripts", "ble_probe_device.py")


def say(*bits):
    print(*bits)
    sys.stdout.flush()


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", help="serial port; the newest one by default")
    ap.add_argument("--name", default="",
                    help="prefer an advertiser whose name contains this")
    ap.add_argument("--seconds", type=int, default=10, help="how long to scan")
    ap.add_argument("--keep", action="store_true",
                    help="leave the probe on the board's flash afterwards")
    a = ap.parse_args()

    port = a.port
    if not port:
        found = serialport.ports()
        if not found:
            raise SystemExit("ble-pad-probe: no serial port — is the board plugged in?")
        port = found[0]
        say("using %s" % port)

    with open(DEVICE_PROBE, "rb") as fh:
        code = fh.read()

    serial = serialport.Serial(port, timeout=0.1)
    try:
        repl = serialport.RawREPL(serial)
        say("resetting the board and entering the raw REPL…")
        if not repl.enter_through_boot():
            raise SystemExit("ble-pad-probe: the board never reached the REPL — "
                             "unplug it, plug it back in, and try again")
        say("pushing the probe (%d bytes)…" % len(code))
        repl.put("ble_probe.py", code)

        say("")
        say("=" * 68)
        say("Put the pad in pairing mode now: hold its pair button until the")
        say("light flashes quickly. Scanning for %ds." % a.seconds)
        say("=" * 68)
        say("")

        # Streamed rather than run(), because the interesting part is the order
        # things happen in and it takes the best part of a minute.
        start = "import ble_probe\nble_probe.run(%r, %d)\n" % (a.name, a.seconds)
        serial.write(start.encode() + b"\x04")
        deadline = time.time() + a.seconds + 70
        line = b""
        while time.time() < deadline:
            chunk = serial.read(256)
            if not chunk:
                continue
            for byte in chunk:
                ch = bytes([byte])
                if ch in (b"\r", b"\n"):
                    if line.strip():
                        say("  " + line.decode(errors="replace").rstrip())
                    line = b""
                elif ch == b"\x04":
                    continue
                else:
                    line += ch
            if b"PROBE end" in line or line.endswith(b">"):
                break
        if line.strip():
            say("  " + line.decode(errors="replace").rstrip())

        if not a.keep:
            try:
                repl.run("import os\nos.remove('ble_probe.py')")
            except Exception:
                pass
        repl.exit()
    finally:
        serial.close()

    say("")
    say("What the lines mean:")
    say("  FOUND hid=True      an advertiser offering HID over GATT — a pad")
    say("  ENCRYPTION bonded=True   the thing that had to work; everything else")
    say("                      is buildable once this line appears")
    say("  REPORT …            raw HID bytes. Move one stick at a time and the")
    say("                      offsets fall out of the hex on their own")
    say("")
    say("Reset the board to bring the agent back.")


if __name__ == "__main__":
    main()
