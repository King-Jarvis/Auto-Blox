"""I2C for the agent — pulled only when a flow uses it."""
import machine

_buses = {}


def bus(cfg):
    sda = int(cfg.get("sda", 21))
    scl = int(cfg.get("scl", 22))
    key = (sda, scl)
    if key not in _buses:
        _buses[key] = machine.SoftI2C(scl=machine.Pin(scl), sda=machine.Pin(sda),
                                      freq=int(cfg.get("freq", 100000)))
    return _buses[key]


def scan(cfg):
    return ["0x%02x" % a for a in bus(cfg).scan()]


def read(cfg):
    addr = int(str(cfg.get("address", "0")), 0)
    n = int(cfg.get("length", 1))
    reg = cfg.get("register")
    i2c = bus(cfg)
    if reg in (None, ""):
        raw = i2c.readfrom(addr, n)
    else:
        raw = i2c.readfrom_mem(addr, int(str(reg), 0), n)
    return list(raw)


def write(cfg, msg):
    addr = int(str(cfg.get("address", "0")), 0)
    reg = cfg.get("register")
    data = cfg.get("data")
    if data in (None, ""):
        data = msg.get("payload")
    if isinstance(data, str):
        data = bytes(int(x, 0) for x in data.replace(",", " ").split())
    elif isinstance(data, (list, tuple)):
        data = bytes(int(x) for x in data)
    else:
        data = bytes([int(data) & 0xFF])
    i2c = bus(cfg)
    if reg in (None, ""):
        return i2c.writeto(addr, data)
    return i2c.writeto_mem(addr, int(str(reg), 0), data)
