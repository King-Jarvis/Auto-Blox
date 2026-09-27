#!/usr/bin/env python3
"""Run a known BLE peripheral on a board, so the host's GATT client has a target.

    python3 scripts/ble-peripheral.py --seconds 120
    python3 scripts/ble-peripheral.py --port /dev/ttyUSB0 --keep

Pushes `ble_peripheral_device.py` over USB, starts it, and streams what the
board prints. While it is up, run `scripts/ble-gatt-probe.py --name zero2w-gatt`
from another shell to check `zero2w_console/gatt.py` against a live link.

Needs no network. The board's agent stays stopped until the next reset.
"""
import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from zero2w_console import serialport                      # noqa: E402

DEVICE_FIXTURE = os.path.join(ROOT, "scripts", "ble_peripheral_device.py")


def say(*bits):
    print(*bits)
    sys.stdout.flush()


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", help="serial port; the newest one by default")
    ap.add_argument("--seconds", type=int, default=120,
                    help="how long the fixture stays up")
    ap.add_argument("--keep", action="store_true",
                    help="leave the fixture on the board's flash afterwards")
    a = ap.parse_args()

    port = a.port
    if not port:
        found = serialport.ports()
        if not found:
            raise SystemExit("ble-peripheral: no serial port — is the board "
                             "plugged in?")
        port = found[0]
        say("using %s" % port)

    with open(DEVICE_FIXTURE, "rb") as fh:
        code = fh.read()

    serial = serialport.Serial(port, timeout=0.1)
    try:
        say("resetting the board and entering the raw REPL…")
        repl = serialport.RawREPL(serial)
        if not repl.enter_through_boot():
            raise SystemExit("ble-peripheral: the board never reached the REPL — "
                             "unplug it, plug it back in, and try again")
        say("pushing the fixture (%d bytes)…" % len(code))
        sha = repl.put("ble_fixture.py", code)
        say("    landed, sha %s" % sha)

        say("")
        say("=" * 70)
        say("The peripheral is coming up as 'zero2w-gatt'. From another shell:")
        say("")
        say("    python3 scripts/ble-gatt-probe.py --name zero2w-gatt")
        say("")
        say("Staying up for %ds. Ctrl-C to stop early." % a.seconds)
        say("=" * 70)
        say("")

        start = "import ble_fixture\nble_fixture.run(%d)\n" % a.seconds
        serial.write(start.encode() + b"\x04")
        deadline = time.time() + a.seconds + 25
        line = b""
        try:
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
                if b"FIXTURE done" in line or b"RADIO off" in line:
                    break
        except KeyboardInterrupt:
            say("  (stopping)")
        if line.strip():
            say("  " + line.decode(errors="replace").rstrip())

        if not a.keep:
            try:
                repl.run("import os\nos.remove('ble_fixture.py')")
            except Exception:
                pass
        repl.exit()
    finally:
        serial.close()

    say("")
    say("Reset the board to bring its agent back.")


if __name__ == "__main__":
    main()
