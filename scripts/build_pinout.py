#!/usr/bin/env python3
"""Generate zero2w_console/data/pinout-zero2w.json from the vendor's own pin
table."""
import json
import os

# physToGpio_ZERO_2_W: index = physical header pin, value = sunxi GPIO number.
PHYS_TO_GPIO = {
    3: 264, 5: 263, 7: 269, 8: 224, 10: 225, 11: 226, 12: 257, 13: 227,
    15: 261, 16: 270, 18: 228, 19: 231, 21: 232, 22: 262, 23: 230, 24: 229,
    26: 233, 27: 266, 28: 265, 29: 256, 31: 271, 32: 267, 33: 268,
    35: 258, 36: 76, 37: 272, 38: 260, 40: 259,
}
# Non-GPIO pins on the 40-pin header.
POWER = {1: "3.3V", 2: "5V", 4: "5V", 17: "3.3V"}
GROUND = {6, 9, 14, 20, 25, 30, 34, 39}

# pinToGpio_ZERO_2_W: index = wPi number, value = sunxi GPIO number.
WPI_TO_GPIO = [
    264, 263, 269, 224, 225, 226, 257, 227, 261, 270, 228, 231, 232, 262,
    230, 229, 233, 266, 265, 256, 271, 267, 268, 258, 76, 272, 260, 259,
]
GPIO_TO_WPI = {g: i for i, g in enumerate(WPI_TO_GPIO)}

BANKS = "ABCDEFGHI"


def bank_label(n):
    return "P%s%d" % (BANKS[n // 32], n % 32)


def main():
    pins = []
    for phys in range(1, 41):
        entry = {"phys": phys, "row": "left" if phys % 2 else "right"}
        if phys in POWER:
            entry.update(kind="power", label=POWER[phys])
        elif phys in GROUND:
            entry.update(kind="ground", label="GND")
        elif phys in PHYS_TO_GPIO:
            n = PHYS_TO_GPIO[phys]
            entry.update(
                kind="gpio",
                gpio=n,
                label=bank_label(n),
                wpi=GPIO_TO_WPI.get(n),
                # NOTE: `chip` is a fallback only. Which gpiochip carries the
                # header differs between kernels, so gpio.PinInventory resolves it
                # from gpiodetect at runtime.
                chip=1,
                line=n,
            )
        else:
            entry.update(kind="nc", label="NC")
        pins.append(entry)

    doc = {
        "board": "OrangePi Zero 2W",
        "soc": "allwinner,sun50i-h618",
        "armbian_board": "orangepizero2w",
        "header": "40-pin",
        "source": ("orangepi-xunlong/wiringOP wiringPi/wiringPi.c @next — "
                   "physToGpio_ZERO_2_W and pinToGpio_ZERO_2_W"),
        "numbering": "sunxi global: bank*32+offset (A=0..I=8); gpiochip0 base 0",
        "pins": pins,
    }
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = os.path.join(root, "zero2w_console", "data", "pinout-zero2w.json")
    with open(out, "w") as fh:
        json.dump(doc, fh, indent=2)
    gp = [p for p in pins if p["kind"] == "gpio"]
    print("wrote %s" % out)
    print("  %d header pins: %d gpio, %d power, %d ground, %d nc"
          % (len(pins), len(gp), len(POWER), len(GROUND),
             len(pins) - len(gp) - len(POWER) - len(GROUND)))
    print("  banks used: %s" % ", ".join(sorted({p["label"][:2] for p in gp})))
    assert len(gp) == 28, "expected 28 usable GPIO on this header"
    assert all(p["wpi"] is not None for p in gp), "every GPIO pin maps to a wPi number"


if __name__ == "__main__":
    main()
