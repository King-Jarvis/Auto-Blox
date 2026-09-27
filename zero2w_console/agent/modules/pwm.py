"""PWM for the agent — one channel per pin, kept alive between firings."""
import machine

_channels = {}


def set(gpio, freq=1000, duty_percent=0):
    key = int(gpio)
    if key not in _channels:
        _channels[key] = machine.PWM(machine.Pin(key))
    ch = _channels[key]
    ch.freq(int(freq))
    ch.duty_u16(int(max(0, min(100, float(duty_percent))) * 65535 / 100))
    return ch


def stop(gpio):
    ch = _channels.pop(int(gpio), None)
    if ch:
        ch.deinit()
