#!/usr/bin/env python3
"""I2C, SPI and PWM for the flow engine — stdlib only, via ioctl and sysfs."""
import array
import fcntl
import glob
import json
import os
import struct
import subprocess
import threading
import time

# ---- ioctl numbers -------------------------------------------------------
I2C_SLAVE = 0x0703
I2C_RDWR = 0x0707
I2C_M_RD = 0x0001

SPI_IOC_WR_MODE = 0x40016B01
SPI_IOC_WR_BITS_PER_WORD = 0x40016B03
SPI_IOC_WR_MAX_SPEED_HZ = 0x40046B04

PWM_ROOT = "/sys/class/pwm"


def _ioc_message(n):
    # _IOW(SPI_IOC_MAGIC, 0, struct spi_ioc_transfer[n]) — size field is 14 bits.
    size = 32 * n
    return (1 << 30) | (size << 16) | (0x6B << 8) | 0


# ---------------------------------------------------------------------- I2C
class I2CBus:
    """Raw I2C over /dev/i2c-N using I2C_RDWR, so register reads get a
    repeated start rather than a stop-then-start."""

    def __init__(self, bus):
        self.path = "/dev/i2c-%d" % int(bus)
        self.bus = int(bus)

    def _open(self):
        return os.open(self.path, os.O_RDWR)

    def write(self, addr, data):
        fd = self._open()
        try:
            fcntl.ioctl(fd, I2C_SLAVE, addr)
            os.write(fd, bytes(data))
            return len(data)
        finally:
            os.close(fd)

    def read(self, addr, length):
        fd = self._open()
        try:
            fcntl.ioctl(fd, I2C_SLAVE, addr)
            return os.read(fd, length)
        finally:
            os.close(fd)

    def write_read(self, addr, out, length):
        """Write `out`, repeated START, then read `length` bytes."""
        fd = self._open()
        try:
            wbuf = array.array("B", bytes(out))
            rbuf = array.array("B", [0] * length)
            waddr, _ = wbuf.buffer_info()
            raddr, _ = rbuf.buffer_info()
            # struct i2c_msg { __u16 addr; __u16 flags; __u16 len; __u8 *buf; }
            msgs = struct.pack("HHHxxQ", addr, 0, len(out), waddr) + \
                   struct.pack("HHHxxQ", addr, I2C_M_RD, length, raddr)
            buf = array.array("B", msgs)
            baddr, _ = buf.buffer_info()
            # struct i2c_rdwr_ioctl_data { struct i2c_msg *msgs; __u32 nmsgs; }
            data = struct.pack("QI4x", baddr, 2)
            fcntl.ioctl(fd, I2C_RDWR, data)
            return bytes(rbuf)
        finally:
            os.close(fd)

    def scan(self, start=0x03, end=0x77):
        found = []
        for addr in range(start, end + 1):
            fd = None
            try:
                fd = self._open()
                fcntl.ioctl(fd, I2C_SLAVE, addr)
                try:
                    os.read(fd, 1)
                    found.append(addr)
                except OSError:
                    pass
            except OSError:
                pass
            finally:
                if fd is not None:
                    os.close(fd)
        return found


def i2c_buses():
    out = []
    for p in sorted(glob.glob("/dev/i2c-*")):
        n = p.rsplit("-", 1)[-1]
        if not n.isdigit():
            continue
        out.append({"bus": int(n), "path": p, "readable": os.access(p, os.R_OK | os.W_OK)})
    return out


# ---------------------------------------------------------------------- SPI
class SPIDev:
    def __init__(self, path):
        self.path = path

    def transfer(self, data, speed_hz=500000, mode=0, bits=8):
        fd = os.open(self.path, os.O_RDWR)
        try:
            fcntl.ioctl(fd, SPI_IOC_WR_MODE, struct.pack("B", mode & 0x03))
            fcntl.ioctl(fd, SPI_IOC_WR_BITS_PER_WORD, struct.pack("B", bits))
            fcntl.ioctl(fd, SPI_IOC_WR_MAX_SPEED_HZ, struct.pack("I", int(speed_hz)))
            tx = array.array("B", bytes(data))
            rx = array.array("B", [0] * len(data))
            txa, _ = tx.buffer_info()
            rxa, _ = rx.buffer_info()
            # struct spi_ioc_transfer is 32 bytes on 64-bit.
            xfer = struct.pack("QQIIHBBBBH", txa, rxa, len(data), int(speed_hz),
                               0, bits, 0, 0, 0, 0)
            fcntl.ioctl(fd, _ioc_message(1), xfer)
            return bytes(rx)
        finally:
            os.close(fd)


def spi_devices():
    return [{"path": p, "usable": os.access(p, os.R_OK | os.W_OK)}
            for p in sorted(glob.glob("/dev/spidev*"))]


# ---- PWM: hardware via sysfs, software via gpioset --toggle --------------
def pwm_chips():
    out = []
    for p in sorted(glob.glob(os.path.join(PWM_ROOT, "pwmchip*"))):
        try:
            with open(os.path.join(p, "npwm")) as fh:
                n = int(fh.read().strip())
        except (OSError, ValueError):
            n = 0
        out.append({"chip": os.path.basename(p), "channels": n,
                    "writable": os.access(os.path.join(p, "export"), os.W_OK)})
    return out


class HardwarePWM:
    def __init__(self, chip="pwmchip0", channel=0):
        self.base = os.path.join(PWM_ROOT, chip)
        self.channel = int(channel)
        self.dir = os.path.join(self.base, "pwm%d" % self.channel)

    def _w(self, name, value):
        with open(os.path.join(self.dir, name), "w") as fh:
            fh.write(str(value))

    def apply(self, period_ns, duty_ns, enable=True):
        if not os.path.isdir(self.dir):
            with open(os.path.join(self.base, "export"), "w") as fh:
                fh.write(str(self.channel))
            time.sleep(0.05)
        # Order matters: duty must never exceed the current period.
        self._w("duty_cycle", 0)
        self._w("period", int(period_ns))
        self._w("duty_cycle", int(duty_ns))
        self._w("enable", 1 if enable else 0)

    def stop(self):
        try:
            self._w("enable", 0)
        except OSError:
            pass


class SoftPWM:
    """Square wave on any GPIO via `gpioset --toggle`, held by a live
    process."""

    def __init__(self):
        self.procs = {}
        self.lock = threading.Lock()

    def start(self, chip, line, freq_hz, duty_pct):
        freq_hz = max(1.0, min(5000.0, float(freq_hz)))
        duty_pct = max(0.0, min(100.0, float(duty_pct)))
        period_us = 1_000_000.0 / freq_hz
        high_us = int(period_us * duty_pct / 100.0)
        low_us = int(period_us - high_us)
        key = (chip, line)
        with self.lock:
            self.stop(chip, line)
            if duty_pct <= 0 or duty_pct >= 100 or high_us < 1 or low_us < 1:
                # A flat line is not a waveform: hold the level instead.
                value = 1 if duty_pct >= 100 else 0
                proc = subprocess.Popen(
                    ["gpioset", "--banner", "-C", "console-pwm",
                     "-c", "gpiochip%d" % chip, "%d=%d" % (line, value)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            else:
                proc = subprocess.Popen(
                    ["gpioset", "--banner", "-C", "console-pwm",
                     "-c", "gpiochip%d" % chip,
                     "-t", "%dus,%dus" % (high_us, low_us),
                     "%d=1" % line],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            time.sleep(0.12)
            if proc.poll() is not None:
                err = (proc.stderr.read() or "").strip()
                return False, err or "gpioset exited immediately"
            self.procs[key] = proc
        return True, None

    def stop(self, chip, line, reset=True):
        proc = self.procs.pop((chip, line), None)
        if proc:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            if reset:
                # Same reason as PinDriver.reset_to_input: dropping the claim
                # leaves the line an output at its last level.
                try:
                    subprocess.run(["gpioget", "--numeric", "-C", "console-reset",
                                    "-c", "gpiochip%d" % chip, str(line)],
                                   capture_output=True, timeout=4)
                except Exception:
                    pass

    def stop_all(self):
        with self.lock:
            for key in list(self.procs):
                self.stop(*key)

    def reap(self):
        """Same as PinDriver.reap: a dead waveform holder must not be counted."""
        with self.lock:
            for key, p in list(self.procs.items()):
                if p.poll() is not None:
                    self.procs.pop(key, None)

    def active(self):
        self.reap()
        with self.lock:
            return [{"chip": c, "line": l} for (c, l), p in self.procs.items()
                    if p.poll() is None]


# ---------------------------------------------------------- capability probe
OVERLAY_HINTS = {
    "i2c": ("No header I2C bus is muxed. Add an overlay to /boot/armbianEnv.txt, "
            "e.g. `overlays=i2c3-ph` (pins 27/28 are PI10/PI9; i2c3-ph uses PH), "
            "then reboot."),
    "spi": ("No /dev/spidev* exists. Add `overlays=spidev1_0` to "
            "/boot/armbianEnv.txt and reboot."),
    "pwm": ("Hardware PWM needs both a pin-mux overlay (e.g. `overlays=pwm1-ph3` "
            "or `pwm3-pi13`) and write access to /sys/class/pwm. Software PWM "
            "works on any free GPIO with no overlay."),
}


OVERLAYS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data",
                        "overlays-zero2w.json")
ARMBIAN_ENV = "/boot/armbianEnv.txt"


def header_overlays(env_path=None):
    """Which overlays can put a bus on the header, and which are set now."""
    try:
        with open(OVERLAYS) as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return {"available": [], "current": [], "error": "no overlay table"}
    current = []
    try:
        with open(env_path or ARMBIAN_ENV) as fh:
            for line in fh:
                if line.startswith("overlays="):
                    current = line[len("overlays="):].split()
    except OSError:
        pass
    return {"available": doc.get("overlays") or [], "current": current,
            "file": env_path or ARMBIAN_ENV}


def capabilities():
    i2c = i2c_buses()
    spi = spi_devices()
    pwm = pwm_chips()
    usable_i2c = [b for b in i2c if b["readable"]]
    usable_pwm = [c for c in pwm if c["writable"] and c["channels"]]
    overlays = _enabled_overlays()
    return {
        "i2c": {
            "available": bool(usable_i2c),
            "buses": i2c,
            "hint": None if usable_i2c else
                    ("/dev/i2c-* exists but this user is not in the `i2c` group."
                     if i2c else OVERLAY_HINTS["i2c"]),
        },
        "spi": {
            "available": bool([d for d in spi if d["usable"]]),
            "devices": spi,
            "hint": None if spi else OVERLAY_HINTS["spi"],
        },
        "pwm": {
            "hardware": bool(usable_pwm),
            "chips": pwm,
            "software": True,      # always available on a free GPIO
            "hint": None if usable_pwm else OVERLAY_HINTS["pwm"],
        },
        "overlays": overlays,
    }


def _enabled_overlays():
    try:
        with open("/boot/armbianEnv.txt") as fh:
            for line in fh:
                if line.strip().startswith("overlays="):
                    return line.strip().split("=", 1)[1].split()
    except OSError:
        pass
    return []


if __name__ == "__main__":
    import json
    caps = capabilities()
    print(json.dumps(caps, indent=2))
