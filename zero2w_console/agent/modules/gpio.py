"""GPIO helpers for the agent."""
import machine

_pins = {}


def pin(gpio, mode=None, pull=None):
    key = int(gpio)
    want = mode if mode is not None else machine.Pin.OUT
    if key not in _pins or _pins[key][1] != want:
        _pins[key] = (machine.Pin(key, want, pull), want)
    return _pins[key][0]


def read(gpio, pull=None):
    return pin(gpio, machine.Pin.IN, pull).value()


def write(gpio, value):
    p = pin(gpio, machine.Pin.OUT)
    p.value(1 if value else 0)
    return p.value()


def release(gpio):
    """Back to an input, so nothing is left driven when a flow is replaced."""
    key = int(gpio)
    _pins.pop(key, None)
    machine.Pin(key, machine.Pin.IN)
