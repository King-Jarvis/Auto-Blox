#!/usr/bin/env python3
"""Generate zero2w_console/data/boards/*.json — one profile per chip."""
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "zero2w_console", "data", "boards")

# Note text reused across chips, so the wording cannot drift between them.
NOTE = {
    "flash": "wired to the SPI flash — not available",
    "psram_octal": ("used by octal PSRAM on -R2 and -R8 modules, free on plain "
                    "ones; the device's own probe settles it"),
    "input_only": "input only, and has no internal pull-up or pull-down",
    "strap": "strapping pin — its level at reset changes how the chip boots",
    "adc2": "ADC2, which cannot be read while WiFi is on",
    "usb": "USB D+/D- — taking it breaks the native USB serial",
    "uart0": "UART0, the serial console used for flashing",
    "dac": "8-bit DAC output",
    "camera": "wired to the camera module",
    "sdcard": "wired to the microSD slot",
    "led": "on-board LED",
    "wrover_psram": ("used by PSRAM on WROVER modules — free on WROOM; the "
                     "device's own probe settles it"),
    "no_header": "not brought out on this board",
    "cam_psram": "wired to this module's PSRAM",
    "sd_shared": "shared with the microSD slot — free unless the slot is used",
    "status_led": "on-board red LED (active low), not on the header",
    "flash_led": "on-board flash LED — bright, and shares a line with the microSD slot",
}


def pins(spec):
    """One capability record per GPIO, derived from the chip's tables."""
    out = []
    for gpio in spec["gpios"]:
        caps, notes, usable = [], [], True

        # A board can bring out fewer pins than the chip has. Anything not on the
        # header is not a pin you have, whatever the chip's tables say.
        if "header" in spec and gpio not in spec["header"] and gpio not in spec.get("onboard", ()):
            usable = False
            notes.append(NOTE["no_header"])
        if gpio in spec.get("flash", ()):
            usable = False
            notes.append(NOTE["flash"])
        if gpio in spec.get("module_psram", ()):
            usable = False
            notes.append(NOTE["cam_psram"])
        if gpio in spec.get("psram_octal", ()):
            usable = False
            notes.append(NOTE["psram_octal"])
        if gpio in spec.get("camera", ()):
            usable = False
            notes.append(NOTE["camera"])

        # A pin something else owns offers nothing at all.
        input_only = gpio in spec.get("input_only", ())
        if usable:
            caps.append("in")
            if not input_only:
                caps += ["out", "pwm"]        # LEDC can drive any output pin
            else:
                notes.append(NOTE["input_only"])
            if gpio in spec.get("adc1", ()):
                caps.append("adc1")
            if gpio in spec.get("adc2", ()):
                caps.append("adc2")
                notes.append(NOTE["adc2"])
            if gpio in spec.get("touch", ()):
                caps.append("touch")
            if gpio in spec.get("dac", ()):
                caps.append("dac")
                notes.append(NOTE["dac"])
            if gpio in spec.get("rtc", ()):
                caps.append("rtc")

        strapping = gpio in spec.get("strapping", ())
        if strapping:
            notes.append(NOTE["strap"])
        if gpio in spec.get("usb", ()):
            notes.append(NOTE["usb"])
        if gpio in spec.get("uart0", ()):
            notes.append(NOTE["uart0"])
        if gpio in spec.get("led", ()):
            notes.append(NOTE["led"])
        # Still usable, but only if you know what you have.
        for key in spec.get("warn", {}).get(gpio, ()):
            notes.append(NOTE[key])

        out.append({
            "gpio": gpio,
            "name": "GPIO%d" % gpio,
            "caps": caps,
            "usable": usable,
            "input_only": input_only,
            "strapping": strapping,
            "note": "; ".join(notes) or None,
        })
    return out


def rng(*spans):
    """Inclusive ranges, because that is how the datasheets write them."""
    out = []
    for lo, hi in spans:
        out.extend(range(lo, hi + 1))
    return out


ESP32 = {
    "board": "esp32",
    "label": "ESP32",
    "chip": "ESP32",
    "family": "esp32",
    "cores": 2,
    "flash_default_mb": 4,
    "voltage": "3.3",
    "source": "ESP32 TRM ch.4; ESP32 Datasheet §2.2, §4.1.1; WROOM-32 Datasheet §3",
    # 20, 24, 28-31 are not bonded out; 37 and 38 exist on the die but not on
    # WROOM modules.
    "gpios": [0, 1, 2, 3, 4, 5] + rng((6, 11)) + rng((12, 19)) + [21, 22, 23, 25, 26, 27,
                                                                 32, 33, 34, 35, 36, 39],
    "flash": rng((6, 11)),
    "input_only": (34, 35, 36, 39),
    "adc1": (32, 33, 34, 35, 36, 39),
    "adc2": (0, 2, 4, 12, 13, 14, 15, 25, 26, 27),
    "touch": (0, 2, 4, 12, 13, 14, 15, 27, 32, 33),
    "dac": (25, 26),
    "rtc": (0, 2, 4) + tuple(rng((12, 15))) + (25, 26, 27, 32, 33, 34, 35, 36, 39),
    "strapping": (0, 2, 12, 15),
    "uart0": (1, 3),
    "warn": {16: ("wrover_psram",), 17: ("wrover_psram",)},
    "buses": {
        "i2c": {"sda": 21, "scl": 22, "note": "the usual devkit pair; any GPIO works"},
        "spi": {"mosi": 23, "miso": 19, "sck": 18, "cs": 5, "note": "VSPI"},
        "uart0": {"tx": 1, "rx": 3},
    },
    "peripherals": {"wifi": True, "ble": True, "usb": "bridge", "psram": "optional",
                    "camera": False, "touch": True, "dac": True},
}

ESP32_S3 = {
    "board": "esp32s3",
    "label": "ESP32-S3",
    "chip": "ESP32-S3",
    "family": "esp32",
    "cores": 2,
    "flash_default_mb": 8,
    "voltage": "3.3",
    "source": "ESP32-S3 TRM ch.6; ESP32-S3 Datasheet §2.1, §4.3; S3-WROOM-1 Datasheet §2.2",
    # 22-25 do not exist on this chip.
    "gpios": rng((0, 21)) + rng((26, 48)),
    "flash": rng((26, 32)),
    "psram_octal": rng((33, 37)),
    "input_only": (),                       # the S3 has none
    "adc1": rng((1, 10)),
    "adc2": rng((11, 20)),
    "touch": rng((1, 14)),
    "dac": (),                              # the S3 dropped the DAC
    "rtc": rng((0, 21)),
    "strapping": (0, 3, 45, 46),
    "usb": (19, 20),
    "uart0": (43, 44),
    "buses": {
        "i2c": {"sda": 8, "scl": 9, "note": "the usual devkit pair; any GPIO works"},
        "spi": {"mosi": 11, "miso": 13, "sck": 12, "cs": 10, "note": "SPI2 default"},
        "uart0": {"tx": 43, "rx": 44},
    },
    "peripherals": {"wifi": True, "ble": True, "usb": "native", "psram": "optional",
                    "camera": False, "touch": True, "dac": False},
}

ESP32_C3 = {
    "board": "esp32c3",
    "label": "ESP32-C3",
    "chip": "ESP32-C3",
    "family": "esp32",
    "cores": 1,
    "flash_default_mb": 4,
    "voltage": "3.3",
    "source": "ESP32-C3 TRM ch.5; ESP32-C3 Datasheet §2.2; C3-MINI-1 Datasheet §2.2",
    "gpios": rng((0, 21)),
    "flash": rng((11, 17)),
    "input_only": (),
    "adc1": rng((0, 4)),
    "adc2": (5,),
    "touch": (),                            # the C3 has no touch peripheral
    "dac": (),
    "rtc": rng((0, 5)),
    "strapping": (2, 8, 9),
    "usb": (18, 19),
    "uart0": (20, 21),
    "buses": {
        "i2c": {"sda": 8, "scl": 9, "note": "no fixed pins — the GPIO matrix routes it"},
        "spi": {"mosi": 7, "miso": 2, "sck": 6, "cs": 10, "note": "SPI2 default"},
        "uart0": {"tx": 21, "rx": 20},
    },
    "peripherals": {"wifi": True, "ble": True, "usb": "native", "psram": False,
                    "camera": False, "touch": False, "dac": False},
}

ESP32_CAM = dict(
    ESP32,
    board="esp32cam",
    label="ESP32-CAM (AI-Thinker)",
    flash_default_mb=4,
    source=("AI-Thinker ESP32-CAM schematic rev.1.0; esp32-camera camera_pins.h "
            "CAMERA_MODEL_AI_THINKER; ESP32 Datasheet §2.2"),
    # The 16-pin header, both rows, in silkscreen order:
    #   5V GND IO12 IO13 IO15 IO14 IO2 IO4 | 3V3 IO16 IO0 GND VCC U0R U0T GND
    # Everything else is inside the camera connector or not bonded out.
    header=(0, 1, 2, 3, 4, 12, 13, 14, 15, 16),
    # Wired on the board rather than to the header, and still worth driving.
    onboard=(33,),
    camera=(0, 5, 18, 19, 21, 22, 23, 25, 26, 27, 32, 34, 35, 36, 39),
    # This module carries 4 MB of PSRAM; GPIO16 is its chip select.
    module_psram=(16,),
    # The SD slot is optional, and these are the only general-purpose pins the
    # board has: marking them unusable would leave it with almost nothing.
    warn={2: ("sd_shared",), 12: ("sd_shared",), 13: ("sd_shared",),
          14: ("sd_shared",), 15: ("sd_shared",), 4: ("flash_led", "sd_shared"),
          33: ("status_led",), 1: (), 3: ()},
    peripherals=dict(ESP32["peripherals"], camera=True, psram=True),
    buses={"uart0": {"tx": 1, "rx": 3},
           "i2c": {"sda": 13, "scl": 15, "note": "no fixed pins — these are two free "
                                                 "header pins, shared with the SD slot"}},
)

BOARDS = [ESP32, ESP32_S3, ESP32_C3, ESP32_CAM]


def build(spec):
    table = pins(spec)
    doc = {
        "board": spec["board"],
        "label": spec["label"],
        "chip": spec["chip"],
        "family": spec["family"],
        "cores": spec["cores"],
        "voltage": spec["voltage"],
        "flash_default_mb": spec["flash_default_mb"],
        "source": spec["source"],
        "peripherals": spec["peripherals"],
        "buses": spec["buses"],
        "pins": table,
        "counts": {
            "gpio": len(table),
            "usable": len([p for p in table if p["usable"]]),
            "adc": len([p for p in table if "adc1" in p["caps"] or "adc2" in p["caps"]]),
            "output": len([p for p in table if "out" in p["caps"]]),
        },
    }

    # Invariants. A profile that breaks one of these would hand the editor a pin
    # that bricks a boot or silently reads nothing.
    seen = set()
    for p in table:
        assert p["gpio"] not in seen, "%s: duplicate GPIO%d" % (doc["board"], p["gpio"])
        seen.add(p["gpio"])
        if not p["usable"]:
            assert not p["caps"], "%s: GPIO%d is unusable but offers %s" % (
                doc["board"], p["gpio"], p["caps"])
        if p["input_only"]:
            assert "out" not in p["caps"] and "pwm" not in p["caps"], \
                "%s: GPIO%d is input-only but offers an output" % (doc["board"], p["gpio"])
        if "adc2" in p["caps"]:
            assert "ADC2" in (p["note"] or ""), "%s: GPIO%d must warn about ADC2" % (
                doc["board"], p["gpio"])
        if p["strapping"]:
            assert "strapping" in (p["note"] or ""), "%s: GPIO%d must warn" % (
                doc["board"], p["gpio"])
    assert doc["counts"]["usable"], "%s: no usable pin at all" % doc["board"]
    return doc


def main():
    os.makedirs(OUT, exist_ok=True)
    for spec in BOARDS:
        doc = build(spec)
        path = os.path.join(OUT, doc["board"] + ".json")
        with open(path, "w") as fh:
            json.dump(doc, fh, indent=2)
        c = doc["counts"]
        print("wrote %-34s %2d gpio · %2d usable · %2d output · %2d adc"
              % (os.path.relpath(path, ROOT), c["gpio"], c["usable"], c["output"], c["adc"]))


if __name__ == "__main__":
    main()
