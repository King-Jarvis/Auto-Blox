#!/usr/bin/env python3
"""Generate zero2w_console/data/overlays-zero2w.json from the overlays and the
device tree actually installed on this board.

An overlay names pin groups (`i2c3_ph_pins`) or a controller (`spi1`); the base
tree says which pins those are. Both are read with `dtc`, so the result is what
this kernel will do, not what a wiki says.
"""
import glob
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "zero2w_console", "data", "overlays-zero2w.json")
PINOUT = os.path.join(ROOT, "zero2w_console", "data", "pinout-zero2w.json")
DTB_DIR = os.environ.get("ZERO2W_DTB_DIR", "/boot/dtb/allwinner")
BASE = "sun50i-h618-orangepi-zero2w.dtb"
PREFIX = "sun50i-h616-"
# Overlays for one other vendor's board, or for a peripheral rather than a bus.
SKIP = re.compile(r"bananapi|walnutpi|tft35|mcp2515|ws2812|keys|light|gpu|^ir$|fixup")
KINDS = (("i2c", r"^i2c\d"), ("pwm", r"^pwm\d"), ("spi", r"^spi"), ("uart", r"^uart\d"))


def dts(path):
    return subprocess.run(["dtc", "-q", "-I", "dtb", "-O", "dts", path],
                          capture_output=True, text=True, check=True).stdout


def parse(text):
    """{path: {prop: value}} from dtc's output. Values: a list of strings, a
    list of ints, or True for a bare property."""
    nodes, stack = {}, []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("/dts-v1/", "//")):
            continue
        m = re.match(r"^(?:[\w,-]+:\s*)?([\w@,.+/-]+)\s*\{$", line)
        if m:
            name = m.group(1)
            path = "/" if not stack else stack[-1].rstrip("/") + "/" + name
            stack.append(path)
            nodes.setdefault(path, {})
            continue
        if line.startswith("};"):
            stack.pop()
            continue
        m = re.match(r'^([\w,#.+?-]+)\s*=\s*(.+);$', line)
        if m and stack:
            key, value = m.groups()
            if value.startswith('"'):
                nodes[stack[-1]][key] = re.findall(r'"([^"]*)"', value)
            elif value.startswith("<"):
                nodes[stack[-1]][key] = [int(x, 0) for x in re.findall(r"0x[0-9a-f]+|\d+", value)]
            else:
                nodes[stack[-1]][key] = [value]
        elif re.match(r"^[\w,#-]+;$", line) and stack:
            nodes[stack[-1]][line[:-1]] = True
    return nodes


def main():
    base_path = os.path.join(DTB_DIR, BASE)
    if not os.path.isfile(base_path):
        raise SystemExit("build_overlays: no %s — run this on the board" % base_path)
    base = parse(dts(base_path))
    symbols = {k: v[0] for k, v in base.get("/__symbols__", {}).items()}
    by_phandle = {props["phandle"][0]: path for path, props in base.items()
                  if isinstance(props.get("phandle"), list)}
    with open(PINOUT) as fh:
        header = {p["label"]: p["phys"] for p in json.load(fh)["pins"]
                  if p.get("kind") == "gpio"}

    def pins_of(path):
        return (base.get(path) or {}).get("pins") or []

    out = []
    for file in sorted(glob.glob(os.path.join(DTB_DIR, "overlay", PREFIX + "*.dtbo"))):
        name = os.path.basename(file)[len(PREFIX):-len(".dtbo")]
        if SKIP.search(name):
            continue
        kind = next((k for k, rx in KINDS if re.match(rx, name)), None)
        if not kind:
            continue
        ov = parse(dts(file))
        fixups = ov.get("/__fixups__", {})
        pins = []
        # Pin groups the overlay names itself.
        for label, refs in fixups.items():
            if any(":pinctrl-" in r for r in refs) and label in symbols:
                pins += pins_of(symbols[label])
        # A controller it switches on, with the pins the base tree gives it.
        if not pins:
            for label, refs in fixups.items():
                if any(r.endswith(":target:0") for r in refs) and label in symbols:
                    for ph in (base.get(symbols[label]) or {}).get("pinctrl-0") or []:
                        pins += pins_of(by_phandle.get(ph, ""))
        seen = []
        for p in pins:
            if p not in seen:
                seen.append(p)
        entry = {
            "name": name, "kind": kind, "file": os.path.basename(file),
            "pins": [{"pin": p, "phys": header.get(p)} for p in seen],
            "on_header": bool(seen) and all(p in header for p in seen),
        }
        if not seen:
            # spidev1_*: SPI1 switched on, and neither the overlay nor the base
            # tree gives it a pin group, so nothing reaches the header.
            entry["note"] = "switches the controller on without assigning any pins"
        out.append(entry)
    doc = {"board": "orangepi-zero2w", "prefix": PREFIX.rstrip("-"),
           "source": "dtc over %s and %s/overlay/%s*.dtbo" % (BASE, DTB_DIR, PREFIX),
           "overlays": out}
    text = json.dumps(doc, indent=1) + "\n"
    if "--check" in sys.argv:
        with open(OUT) as fh:
            sys.exit(0 if fh.read() == text else 1)
    with open(OUT, "w") as fh:
        fh.write(text)
    print("%d overlays, %d on the header" % (len(out), sum(o["on_header"] for o in out)))


if __name__ == "__main__":
    main()
