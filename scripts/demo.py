#!/usr/bin/env python3
"""A made-up Auto-Blox install: for screenshots, and for looking around.

The console, its pages and the board agent are all the real code. Only what
they read about the machine is invented — the host's name, its network, the
radio, Bluetooth, processes, the journal and the terminal — so nothing on
screen belongs to the machine this runs on. The boards are the real agent.py,
each in its own process with MicroPython's hardware modules stood in for, and
they enrol, sync, link and stream pictures exactly as a board does.

    python3 scripts/demo.py --serve                 # prints the address, runs until ^C
    python3 scripts/demo.py --serve --photo my.jpg  # what the camera shows

scripts/screenshots.py starts it the same way. Everything it writes lives in a
temporary directory: its own HOME, its own tokens, its own flows.

Never reached from here: the privileged helper and its socket, the real
leases, serial ports, /dev/rfkill, BlueZ, the journal and a shell. The PATH is
narrowed to the few tools that only describe the board model (gpio, openssl),
so anything missed fails to run rather than reading the real thing.
"""
import argparse
import json
import os
import random
import shutil
import signal
import sys
import tempfile
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# Everything the demo claims about the world. Addresses are private ranges; the
# MACs start with vendor prefixes a real board of that kind would have and end
# in digits that were made up.
DEMO = {
    "host": "auto-blox",
    "user": "blox",
    "home": "/home/blox",
    "ssid": "auto-blox",
    "channel": 6,
    "subnet": "10.42.0.0/24",
    "gateway": "10.42.0.1",
    "wlan_mac": "2c:c6:82:5a:10:02",
    "uplink": {"name": "eth0", "address": "192.168.1.64/24", "via": "192.168.1.1"},
    "bt": {"address": "2C:C6:82:5A:10:03", "name": "auto-blox"},
    "boards": [
        {"name": "Rover Drive", "board": "esp32", "chip": "ESP32-D0WD-V3",
         "mac": "08:3a:f2:4c:91:2e", "ip": "10.42.0.51", "rssi": -58,
         "flow": "ex_motor_drive", "note": "Two wheels, TB6612 bridge"},
        {"name": "Workbench Cam", "board": "esp32cam", "chip": "ESP32-D0WD-V3",
         "mac": "a4:cf:12:7d:3b:60", "ip": "10.42.0.52", "rssi": -63,
         "flow": "demo_cam", "camera": True, "note": "OV2640, on the shelf"},
        {"name": "Garden Sensor", "board": "esp32", "chip": "ESP32-D0WD-V3",
         "mac": "ec:62:60:1e:88:c4", "ip": "10.42.0.53", "rssi": -71,
         "flow": "ex_heartbeat", "note": "Solar, in the raised bed"},
    ],
    # Asks to join with a code that is not its own, so enrolment has a refusal.
    "stranger": {"device": "dev_9c1e04", "ip": "10.42.0.61"},
    "pads": [
        {"address": "C8:3F:26:5B:90:17", "name": "Xbox Wireless Controller",
         "icon": "input-gaming", "paired": True, "connected": True, "rssi": -52},
        {"address": "A4:C1:38:2E:7B:D9", "name": "LYWSD03MMC",
         "icon": "", "paired": False, "connected": False, "rssi": -74},
    ],
}

# The tools a page may still run for real: they describe the board model, which
# every Zero 2W shares, and the certificate the picture stream needs.
ALLOWED_TOOLS = ("openssl", "gpiodetect", "gpioinfo", "gpioget", "gpioset", "gpiomon")
TOOLS = {}


# =========================================================================
# The console side
# =========================================================================
def prepare(base):
    """HOME, PATH and the switches that keep the console off the real machine."""
    home = os.path.join(base, "home")
    os.makedirs(home, exist_ok=True)
    # Found before PATH narrows: only `iw phy` is ever run with it (see run()).
    TOOLS["iw"] = shutil.which("iw") or "iw"
    tools = os.path.join(base, "bin")
    os.makedirs(tools, exist_ok=True)
    for name in ALLOWED_TOOLS:
        real = shutil.which(name)
        link = os.path.join(tools, name)
        if real and not os.path.exists(link):
            os.symlink(real, link)
    # Found on PATH, so the console believes hostapd is installed. Never run:
    # the helper that would start it is replaced below.
    stand_in = os.path.join(tools, "hostapd")
    with open(stand_in, "w") as fh:
        fh.write("#!/bin/sh\nexit 1\n")
    os.chmod(stand_in, 0o755)
    # Present, so the console believes a helper is installed; never executed,
    # because `iot.helper` is replaced below.
    for name in ("iot-netctl", "ctl.sock"):
        open(os.path.join(base, name), "a").close()
    os.environ.update({
        "HOME": home, "USER": DEMO["user"], "LOGNAME": DEMO["user"], "TZ": "UTC",
        "PATH": tools,
        "ZERO2W_IOT_HELPER": os.path.join(base, "iot-netctl"),
        "ZERO2W_IOT_SOCKET": os.path.join(base, "ctl.sock"),
        "ZERO2W_IOT_NET_CONFIG": os.path.join(base, "no-net-config.json"),
        "ZERO2W_IOT_SERIAL_GLOBS": os.path.join(base, "no-serial-*"),
        # No compiler: a board here is CPython, which loads source, not .mpy.
        "ZERO2W_MPY_CROSS": os.path.join(base, "no-mpy-cross"),
    })
    time.tzset()
    return home


def _jitter(base, spread):
    return round(base + random.uniform(-spread, spread), 1)


def patch_console():
    """Replace every reader of the real machine. Imported only after prepare()."""
    sys.path.insert(0, ROOT)
    from zero2w_console import server, iot, bluetooth, gatt, link, fleet

    # -- the host ----------------------------------------------------------
    real_init = server.Collector.__init__

    def init(self):
        real_init(self)
        self.hostname, self.user = DEMO["host"], DEMO["user"]
        self.demo_net = {"eth0": [8_400_000_000, 1_900_000_000],
                         "wlan0": [640_000_000, 2_300_000_000]}

    def network(self):
        up = DEMO["uplink"]
        rows = []
        for name, address, default in ((up["name"], up["address"], True),
                                       ("wlan0", DEMO["gateway"] + "/24", False)):
            rx_rate, tx_rate = (_jitter(38, 20), _jitter(9, 5)) if default \
                else (_jitter(96, 40), _jitter(14, 6))
            held = self.demo_net[name]
            held[0] += int(rx_rate * 1024 * 2)
            held[1] += int(tx_rate * 1024 * 2)
            rows.append({"name": name, "operstate": "up", "state": "ok",
                         "address": address, "is_default": default,
                         "rx_kib": max(0.1, rx_rate), "tx_kib": max(0.1, tx_rate),
                         "rx_total_h": server.fmt_bytes(held[0]),
                         "tx_total_h": server.fmt_bytes(held[1])})
        return rows

    procs = [
        (612, DEMO["user"], "python3 -m zero2w_console", 3.2, 4.6),
        (598, "root", "python3 /usr/local/sbin/iot-netctl serve", 0.1, 1.2),
        (731, "root", "/usr/sbin/hostapd -B -P /run/zero2w-iot/hostapd.pid "
                      "/etc/zero2w-console/hostapd-iot.conf", 0.3, 0.4),
        (733, "nobody", "/usr/sbin/dnsmasq --conf-file=/run/zero2w-iot/dnsmasq.conf",
         0.0, 0.2),
        (402, "root", "/usr/libexec/bluetooth/bluetoothd", 0.1, 0.5),
        (1, "root", "/sbin/init", 0.0, 0.6),
        (288, "root", "/lib/systemd/systemd-journald", 0.2, 0.9),
        (517, "root", "/usr/sbin/NetworkManager --no-daemon", 0.1, 1.9),
        (540, "root", "sshd: /usr/sbin/sshd -D", 0.0, 0.4),
        (389, "systemd-timesync", "/lib/systemd/systemd-timesyncd", 0.0, 0.3),
        (455, "avahi", "avahi-daemon: running [auto-blox.local]", 0.0, 0.2),
        (471, "root", "/usr/sbin/cron -f", 0.0, 0.1),
    ]

    def processes(self, limit=8):
        rows = [{"pid": pid, "user": user, "cpu": max(0.0, _jitter(cpu, cpu / 2)),
                 "mem": mem, "cmd": cmd, "state": None}
                for pid, user, cmd, cpu, mem in procs]
        rows.sort(key=lambda r: r["cpu"], reverse=True)
        return rows[:limit]

    real_fs = server.Collector.filesystems

    def filesystems():
        # The card's own sizes are fine to show; anything mounted by hand is not.
        return [f for f in real_fs() if f["mount"] in ("/", "/boot", "/tmp", "/var/log")]

    server.Collector.__init__ = init
    server.Collector.network = network
    server.Collector.processes = processes
    server.Collector.filesystems = staticmethod(filesystems)

    # -- the journal and the terminal ---------------------------------------
    server.LogFollower = DemoJournal
    server.recent_logs = lambda n=40: DemoJournal.history(n)
    server.run_command = demo_shell

    # -- the radio and the access point ---------------------------------------
    real_run = iot._run

    def run(cmd, timeout=6):
        if cmd[:2] == ["iw", "phy"]:
            # The chip's capabilities are the board model's, but drop the one
            # line that carries this unit's address.
            return "\n".join(line for line in real_run([TOOLS["iw"]] + cmd[1:],
                                                        timeout).splitlines()
                             if "addr" not in line.lower())
        if cmd == ["iw", "dev"]:
            return ("phy#0\n\tInterface wlan0\n\t\tifindex 3\n\t\twdev 0x1\n"
                    "\t\taddr %s\n\t\tssid %s\n\t\ttype AP\n"
                    "\t\tchannel %d (2437 MHz), width: 20 MHz, center1: 2437 MHz\n"
                    "\t\ttxpower 20.00 dBm\n" % (DEMO["wlan_mac"], DEMO["ssid"],
                                                  DEMO["channel"]))
        if cmd[:4] == ["ip", "-o", "-4", "addr"]:
            iface = cmd[-1]
            addr = {"wlan0": DEMO["gateway"] + "/24",
                    DEMO["uplink"]["name"]: DEMO["uplink"]["address"]}.get(iface)
            return ("3: %s    inet %s scope global %s\n" % (iface, addr, iface)) if addr else ""
        if cmd[:3] == ["iw", "dev", "wlan0"] and "station" in cmd:
            return "".join("Station %s (on wlan0)\n\tinactive time:\t%d ms\n"
                           "\tsignal:  \t%d [%d] dBm\n"
                           % (b["mac"], random.randint(20, 900),
                              b["rssi"], b["rssi"]) for b in DEMO["boards"])
        if cmd[:3] == ["ip", "-o", "route"]:
            return "default via %s dev %s proto dhcp metric 100\n" % (
                DEMO["uplink"]["via"], DEMO["uplink"]["name"])
        return ""

    def links():
        return [{"interface": DEMO["uplink"]["name"], "kind": "wired",
                 "operstate": "up", "carrier": True, "needs": "nothing but an address"},
                {"interface": "wlan0", "kind": "wireless", "operstate": "up",
                 "carrier": True, "needs": "an access point (hostapd)"}]

    def leases():
        soon = int(time.time()) + 3600
        return [{"expires": soon, "mac": b["mac"], "ip": b["ip"],
                 "name": "esp32-" + b["mac"].replace(":", "")[-6:]}
                for b in DEMO["boards"]]

    def status():
        return {"hostapd": True, "dnsmasq": True, "configured": True,
                "leases": leases(), "interface": "wlan0", "wireless": True,
                "links": links(), "up": True,
                "config": {"interface": "wlan0", "ssid": DEMO["ssid"],
                           "channel": DEMO["channel"], "subnet": DEMO["subnet"],
                           "wireless": True, "isolate": True, "lan_access": False,
                           "uplink": DEMO["uplink"]["name"],
                           "gateway": DEMO["gateway"], "prefix": 24,
                           "netmask": "255.255.255.0",
                           "dhcp_start": "10.42.0.64", "dhcp_end": "10.42.0.253"}}

    def helper(verb, payload=None, timeout=30):
        if verb not in iot.HELPER_VERBS:
            raise iot.NetError("unknown verb")
        if verb == "status":
            return status()
        if verb == "clients":
            return {"clients": iot.stations("wlan0")}
        if verb == "diagnose":
            return {"hostapd": {"ok": True, "output": "wlan0: AP-ENABLED"},
                    "status": status()}
        return {"ok": True}

    iot._run = run
    iot.link_candidates = links
    iot.helper = helper
    iot.helper_socket_ready = lambda: True
    iot.REPO = DEMO["home"] + "/auto-blox"
    iot.set_radio = lambda on: {"ok": True, "via": "/dev/rfkill", "blocked": not on}
    iot._rfkill_write = lambda soft: None
    iot.radio_writable = lambda: True
    iot.scan = demo_usb_scan

    # -- Bluetooth -----------------------------------------------------------
    gatt._gdbus = demo_gdbus
    bluetooth._run = demo_bluetoothctl

    def no_session(args):
        raise OSError("not in the demo")

    bluetooth._popen = no_session
    gatt.connect = lambda mac, timeout=25: {"ok": True}
    gatt.disconnect = lambda mac, timeout=10: {"ok": True}
    gatt.hold_notify = lambda path: None
    gatt.release_notify = lambda proc: None

    # -- the link: a board's address as the AP would have handed it out -------
    ips = {}
    real_conn = link.Connection.__init__

    def conn_init(self, sock, address, device_id, *a, **kw):
        if device_id in ips:
            address = (ips[device_id], address[1])
        real_conn(self, sock, address, device_id, *a, **kw)

    link.Connection.__init__ = conn_init

    # -- flashing: the flasher's own words, without a board on the end --------
    def flash(self, device_id, port, ssid=None, psk=None, flow=None, extra=None):
        device = iot.get_device(self.devices, device_id)
        if not device:
            raise iot.NetError("no such device")
        replay_flash(self, device, port)
        return {"ok": True, "device": device_id, "port": port}

    fleet.Fleet.flash = flash

    # The HTTP server, caught as it is made, so the demo can drive it.
    made = {}
    real_server = server.ThreadingHTTPServer

    class Caught(real_server):
        def __init__(self, *a, **kw):
            real_server.__init__(self, *a, **kw)
            made["httpd"] = self

    server.ThreadingHTTPServer = Caught
    return {"server": server, "made": made, "ips": ips, "fleet": fleet, "iot": iot}


class DemoJournal(threading.Thread):
    """The journal, as a board running Auto-Blox would write it."""
    daemon = True
    LINES = [
        ("hostapd", "idle", "wlan0: STA {mac} IEEE 802.11: associated"),
        ("hostapd", "ok", "wlan0: AP-STA-CONNECTED {mac}"),
        ("dnsmasq-dhcp", "idle", "DHCPREQUEST(wlan0) {ip} {mac}"),
        ("dnsmasq-dhcp", "idle", "DHCPACK(wlan0) {ip} {mac} esp32-{tail}"),
        ("zero2w-console", "idle", "link: {name} ready (tls)"),
        ("zero2w-console", "ok", "{name}: report, rssi {rssi} dBm"),
        ("systemd", "idle", "Starting apt-daily.service - Daily apt download activities..."),
        ("systemd", "ok", "Finished apt-daily.service - Daily apt download activities."),
        ("systemd-timesyncd", "ok", "Contacted time server 185.125.190.57:123 (ntp.ubuntu.com)."),
        ("sshd", "idle", "Server listening on :: port 22."),
        ("kernel", "warn", "sprdwl: sprdwl_cfg80211_dump_station, dev wlan0 not connected"),
        ("zero2w-iotnet", "ok", "status: hostapd up, dnsmasq up, 3 leases"),
    ]

    def __init__(self, bus):
        threading.Thread.__init__(self, name="demo-journal")
        self.bus = bus

    @classmethod
    def line(cls):
        unit, level, text = random.choice(cls.LINES)
        b = random.choice(DEMO["boards"])
        return {"time": time.strftime("%H:%M:%S"), "level": level, "unit": unit,
                "message": text.format(mac=b["mac"], ip=b["ip"], name=b["name"],
                                       rssi=b["rssi"] + random.randint(-3, 3),
                                       tail=b["mac"].replace(":", "")[-6:])}

    @classmethod
    def history(cls, n=40):
        return [cls.line() for _ in range(min(n, 30))]

    def run(self):
        while True:
            time.sleep(random.uniform(0.6, 2.2))
            self.bus.publish("log", self.line())


SHELL = {
    "uptime": " %s up 3 days,  4:12,  1 user,  load average: 0.21, 0.18, 0.16",
    "hostname": DEMO["host"],
    "whoami": DEMO["user"],
    "pwd": DEMO["home"],
    "ls": "auto-blox",
    "ls auto-blox": "LICENSE  README.md  backups  design  docs  get.sh  modules  "
                    "packaging  scripts  tests  zero2w_console",
    "systemctl is-active zero2w-console": "active",
    "iw dev wlan0 info": ("Interface wlan0\n\tifindex 3\n\twdev 0x1\n\taddr %s\n"
                          "\tssid %s\n\ttype AP\n\twiphy 0\n\tchannel 6 (2437 MHz), "
                          "width: 20 MHz\n\ttxpower 20.00 dBm" % (DEMO["wlan_mac"],
                                                                   DEMO["ssid"])),
}


def demo_shell(cmd, cwd, timeout=15):
    """The terminal, answering from a script; nothing reaches a shell."""
    cmd = cmd.strip()
    out = SHELL.get(cmd)
    if out is None:
        lines = [{"kind": "dim", "text": "(demo) this terminal answers: %s"
                  % ", ".join(sorted(SHELL))}]
        code = 0
    else:
        if "%s" in out:
            out = out % time.strftime("%H:%M:%S")
        lines, code = [{"kind": "out", "text": out}], 0
    return {"lines": lines, "cwd": DEMO["home"], "code": code,
            "ms": random.randint(4, 30)}


ESPTOOL = """esptool.py v4.7.0
Serial port /dev/ttyUSB0
Connecting....
Detecting chip type... ESP32
Chip is ESP32-D0WD-V3 (revision v3.1)
Features: WiFi, BT, Dual Core, 240MHz, VRef calibration in efuse, Coding Scheme None
Crystal is 40MHz
MAC: 24:6f:28:9e:51:a8
Uploading stub...
Running stub...
Stub running...
Manufacturer: 5e
Device: 4016
Detected flash size: 4MB
Hard resetting via RTS pin...
"""


def demo_usb_scan(probe=True):
    """One board on the USB port, in esptool's words, read by the real parser."""
    from zero2w_console import iot
    row = {"port": "/dev/ttyUSB0", "driver": "cp210x", "vendor": "Silicon Labs",
           "product": "CP2102 USB to UART Bridge Controller", "writable": True,
           "ok": True, "raw": ESPTOOL, "stubless": False}
    row.update(iot.parse_esptool(ESPTOOL))
    return {"ports": [row], "esptool": True, "dialout": True, "stubs": None,
            "hint": None}


# -- BlueZ, as a tree of objects ------------------------------------------------
def _dev_path(mac):
    return "/org/bluez/hci0/dev_" + mac.upper().replace(":", "_")


def _uuid(short):
    return "0000%s-0000-1000-8000-00805f9b34fb" % short


def bluez_tree():
    tree = {"/org/bluez/hci0": {
        "org.bluez.Adapter1": {
            "Address": DEMO["bt"]["address"], "AddressType": "public",
            "Name": DEMO["bt"]["name"], "Alias": DEMO["bt"]["name"],
            "Powered": True, "Discovering": False, "Discoverable": False,
            "Pairable": True},
        "org.bluez.LEAdvertisingManager1": {},
        "org.bluez.GattManager1": {}}}
    for pad in DEMO["pads"]:
        path = _dev_path(pad["address"])
        uuids = ["1800", "1801", "180a", "180f", "1812"] if pad["icon"] else ["181a"]
        tree[path] = {"org.bluez.Device1": {
            "Address": pad["address"], "AddressType": "public" if pad["icon"] else "random",
            "Name": pad["name"], "Alias": pad["name"], "Icon": pad["icon"],
            "Paired": pad["paired"], "Bonded": pad["paired"], "Trusted": pad["paired"],
            "Connected": pad["connected"], "ServicesResolved": pad["connected"],
            "RSSI": pad["rssi"] + random.randint(-2, 2),
            "UUIDs": [_uuid(u) for u in uuids]}}
        if not pad["connected"]:
            continue
        services = [
            ("service0008", "180f", [("char0009", "2a19", ["read", "notify"], [87])]),
            ("service000c", "1812", [
                ("char000d", "2a4b", ["read"], [0x05, 0x01, 0x09, 0x05, 0xa1, 0x01]),
                ("char0011", "2a4d", ["read", "notify"],
                 [0x00, 0x80, 0x00, 0x80, 0x00, 0x80, 0x00, 0x80, 0, 0, 0, 0, 0, 0]),
                ("char0015", "2a4d", ["read", "write-without-response", "write"], [0] * 8)]),
            ("service0020", "180a", [("char0021", "2a29", ["read"], list(b"Microsoft"))]),
        ]
        for spath, suuid, chars in services:
            sfull = path + "/" + spath
            tree[sfull] = {"org.bluez.GattService1": {"UUID": _uuid(suuid), "Primary": True}}
            for cpath, cuuid, flags, value in chars:
                cfull = sfull + "/" + cpath
                tree[cfull] = {"org.bluez.GattCharacteristic1": {
                    "UUID": _uuid(cuuid), "Service": sfull, "Flags": flags,
                    "Notifying": "notify" in flags, "Value": value}}
                if "notify" in flags:
                    tree[cfull + "/desc0013"] = {"org.bluez.GattDescriptor1": {
                        "UUID": _uuid("2902"), "Characteristic": cfull}}
    return tree


def demo_gdbus(path, method, *args, **kw):
    if method.endswith("GetManagedObjects"):
        return [bluez_tree()]
    if method.endswith("ReadValue"):
        for p, ifaces in bluez_tree().items():
            if p == path and "org.bluez.GattCharacteristic1" in ifaces:
                return [bytes(ifaces["org.bluez.GattCharacteristic1"]["Value"])]
        return None
    if method.endswith(("WriteValue", "StartNotify", "StopNotify")):
        return []
    return None


def demo_bluetoothctl(args, timeout=8):
    if args[:1] == ["--version"]:
        return "bluetoothctl: 5.79\n"
    if args[:1] == ["devices"]:
        which = args[1] if len(args) > 1 else None
        rows = [p for p in DEMO["pads"]
                if which is None or (which == "Paired" and p["paired"])
                or (which == "Connected" and p["connected"])]
        return "".join("Device %s %s\n" % (p["address"], p["name"]) for p in rows)
    if "scan" in args:
        time.sleep(min(float(args[1]) if args[0] == "--timeout" else 5, 12))
        return ""
    return ""


# -- a flash, replayed -----------------------------------------------------
def replay_flash(fleet, device, port):
    lines = [
        "$ python3 %s/auto-blox/scripts/iot-flash.py --port %s --device %s --ssid %s"
        % (DEMO["home"], port, device["id"], DEMO["ssid"]),
        "==> scanning %s" % port,
        "    esptool.py v4.7.0",
        "    Detecting chip type... ESP32",
        "    Chip is ESP32-D0WD-V3 (revision v3.1)",
        "    Features: WiFi, BT, Dual Core, 240MHz, VRef calibration in efuse",
        "    Crystal is 40MHz",
        "    MAC: %s" % (device.get("mac") or "24:6f:28:9e:51:a8"),
        "==> firmware: ESP32_GENERIC-20241129-v1.24.1.bin (cached)",
        "    erase_flash",
        "    Chip erase completed successfully in 9.8s",
        "    write_flash 0x1000",
        "    Wrote 1720208 bytes (1127946 compressed) at 0x00001000 in 27.4 seconds "
        "(effective 502.2 kbit/s)...",
        "    Hash of data verified.",
        "==> waiting for MicroPython to boot",
        "    MicroPython v1.24.1 on 2024-11-29; Generic ESP32 module with ESP32",
        "==> copying main.py, agent.py and config.json over the REPL",
        "    main.py        312 bytes",
        "    agent.py     31967 bytes",
        "    config.json    221 bytes",
        "==> provisioned %s; it enrols on its first boot" % device["name"],
    ]
    with fleet.lock:
        fleet.flash_log = []

    def run():
        for text in lines:
            fleet._flash_line(text, "idle")
            time.sleep(0.05)
        fleet._flash_line("flasher exited 0", "ok")

    threading.Thread(target=run, daemon=True).start()


# -- driving it ---------------------------------------------------------------
def api(base, method, path, body=None):
    req = urllib.request.Request(base + path, method=method)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, data, timeout=30) as fh:
        return json.loads(fh.read() or b"{}")


def demo_flows():
    """The shipped examples, plus the camera flow for the demo's camera board."""
    from zero2w_console import examples
    flows = examples.catalogue()
    for f in flows:
        f.pop("runs_on", None)
        f.pop("unsupported", None)
        f.pop("step_name", None)
        # The examples arrive with pins and devices empty; the demo's bench
        # has them all chosen, as a console someone has set up would.
        examples.choose(f, dict(examples.BENCH.get(f["id"], {}),
                                **DEMO_CHOICES.get(f["id"], {})))
    flows.append({
        "id": "demo_cam", "name": "Workbench camera",
        "about": "Pictures while the Interval keeps the feed on, sent to the "
                 "Cameras page over the encrypted stream.",
        "enabled": True, "board": "esp32cam",
        "nodes": [
            {"id": "t", "type": "timer.interval", "x": 60, "y": 60,
             "config": {"every": 15000}},
            {"id": "c", "type": "camera.feed", "x": 320, "y": 60,
             "config": {"frame_size": "VGA", "format": "jpeg"}},
            {"id": "p", "type": "camera.publish", "x": 580, "y": 60,
             "config": {"label": "Workbench", "every_ms": 150}}],
        "edges": [
            {"id": "e1", "from": "t", "fromPort": "out", "fromSide": "right",
             "to": "c", "toPort": "in", "toSide": "left"},
            {"id": "e2", "from": "c", "fromPort": "out", "fromSide": "right",
             "to": "p", "toPort": "in", "toSide": "left"}]})
    # Running: the host-side watcher and the board flows the demo deploys.
    for f in flows:
        if f["id"] in ("ex_heartbeat_watch", "ex_overheat", "ex_timer"):
            f["enabled"] = True
    return flows


# What the demo chose for the examples BENCH does not cover: host header pins
# for the host flows, the demo camera and the demo sensor.
_SENSOR = DEMO["pads"][1]["address"]
DEMO_CHOICES = {
    "ex_blink": {"p": {"gpio": 264}},
    "ex_press": {"b": {"gpio": 263}, "o": {"gpio": 264}},
    "ex_later": {"b": {"gpio": 263}, "o": {"gpio": 264}},
    "ex_hook": {"on": {"gpio": 264}, "off": {"gpio": 264}},
    "ex_latch": {"b": {"gpio": 263}, "o": {"gpio": 264}},
    "ex_count": {"b": {"gpio": 263}},
    "ex_timer": {"d": {"gpio": 263}, "m": {"gpio": 266}, "o": {"gpio": 264}},
    "ex_pump": {"p": {"gpio": 265}},
    "ex_shell": {"c": {"flow": "ex_hello", "node": "go"}},
    "ex_fleet": {"c": {"device": "Workbench Cam"}},
    "ex_ble": {k: {"device": _SENSOR} for k in ("link", "bat", "n", "w")},
}


def all_tags(flows):
    seen, out = set(), []
    for f in flows:
        for tag in f.pop("needs_tags", None) or []:
            if tag["name"] not in seen:
                seen.add(tag["name"])
                out.append(tag)
    return out


def start_boards(base, url, link_port, photo, ctx, log):
    httpd = ctx["made"]["httpd"]
    fleet = httpd.fleet
    doc = {"flows": demo_flows()}
    tags = all_tags(doc["flows"])
    api(url, "POST", "/api/tags", {"tags": tags})
    # The theme kit's worked example, imported but not in use, so the
    # screenshots can show a theme of someone's own (?theme=).
    with open(os.path.join(ROOT, "zero2w_console", "theme_kit", "example.json")) as fh:
        api(url, "POST", "/api/themes", json.load(fh))
    procs = []
    for spec in DEMO["boards"]:
        if spec.get("camera") and not photo:
            log("no photo, so no camera board (pass --photo)")
            continue
        dev = api(url, "POST", "/api/iot/devices", {
            "name": spec["name"], "board": spec["board"], "chip": spec["chip"],
            "mac": spec["mac"], "flash": "4MB", "note": spec["note"],
            "camera": bool(spec.get("camera")), "link": True,
            "psram": "quad" if spec.get("camera") else None})
        ctx["ips"][dev["id"]] = spec["ip"]
        for f in doc["flows"]:
            if f["id"] == spec["flow"]:
                f["device"], f["enabled"] = dev["id"], True
        api(url, "POST", "/api/flows", doc)
        prov = api(url, "POST", "/api/iot/devices/%s/provision" % dev["id"], {})
        where = os.path.join(base, "boards", dev["id"])
        os.makedirs(where, exist_ok=True)
        shutil.copy(os.path.join(ROOT, "zero2w_console", "agent", "agent.py"), where)
        with open(os.path.join(where, "config.json"), "w") as fh:
            json.dump({"host": url, "ssid": DEMO["ssid"], "psk": "", "device": dev["id"],
                       "enroll": prov["enroll_token"], "token": None,
                       "camera": bool(spec.get("camera"))}, fh)
        with open(os.path.join(where, "demo.json"), "w") as fh:
            json.dump(dict(spec, photo=photo), fh)
        out = open(os.path.join(where, "board.log"), "ab")
        procs.append(__import__("subprocess").Popen(
            [sys.executable, os.path.abspath(__file__), "--board", where],
            stdout=out, stderr=out, start_new_session=True))
        # One at a time: provisioning opens the window for that board alone.
        for _ in range(200):
            d = [x for x in api(url, "GET", "/api/iot/devices")["devices"]
                 if x["id"] == dev["id"]][0]
            if d.get("enrolled"):
                break
            time.sleep(0.1)
        else:
            log("%s never enrolled; see %s" % (spec["name"], where))
            continue
        api(url, "POST", "/api/iot/devices/%s/deploy" % dev["id"], {"flow": spec["flow"]})
        log("%s enrolled and running %s" % (spec["name"], spec["flow"]))
    # A board that is not one of ours, knocking.
    fleet.open_window(300, reason="waiting for a new board")
    try:
        fleet.enroll(DEMO["stranger"]["device"], "0" * 32, {"chip": "ESP32"},
                     ip=DEMO["stranger"]["ip"])
    except Exception:
        pass
    # And the last flash, as the Flashing tab remembers it.
    first = [d for d in api(url, "GET", "/api/iot/devices")["devices"]]
    if first:
        replay_flash(fleet, first[-1], "/dev/ttyUSB0")
    return procs


def run_console(a):
    base = a.dir or tempfile.mkdtemp(prefix="auto-blox-demo-")
    home = prepare(base)
    ctx = patch_console()
    server = ctx["server"]
    sys.path.insert(0, HERE)
    import jsc
    port = a.port or jsc._free_port()
    link_port = jsc._free_port()
    sys.argv = ["auto-blox", "--host", "127.0.0.1", "--port", str(port),
                "--link-port", str(link_port), "--no-auth", "--no-gpio-write"]
    quiet = open(os.path.join(base, "console.log"), "w")
    real_stdout = sys.stdout
    sys.stdout = quiet
    threading.Thread(target=server.main, daemon=True, name="console").start()
    url = "http://127.0.0.1:%d" % port

    def log(text):
        real_stdout.write("demo: %s\n" % text)
        real_stdout.flush()

    for _ in range(200):
        if "httpd" in ctx["made"]:
            try:
                api(url, "GET", "/api/config")
                break
            except Exception:
                pass
        time.sleep(0.1)
    else:
        raise SystemExit("the console never came up; see %s" % base)
    log("console at %s (home %s)" % (url, home))
    # Stopped by a signal as well as ^C, so the boards never outlive it.
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    procs = start_boards(base, url, link_port, a.photo, ctx, log)
    time.sleep(a.settle)
    # The motor board with something to do. After the settle: a board resets
    # drive_armed when it boots, and each one reboots once for its new code.
    for name, value in (("drive_armed", 1), ("drive_speed", 0.45), ("drive_steer", 0.1)):
        api(url, "POST", "/api/tags/set", {"name": name, "value": value})
    time.sleep(3)
    log("ready %s" % url)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for p in procs:
            try:
                os.killpg(p.pid, 15)
            except OSError:
                pass
        if not a.dir and not a.keep:
            shutil.rmtree(base, ignore_errors=True)


# =========================================================================
# A board: the real agent, on CPython, with MicroPython's modules stood in
# =========================================================================
class _Reset(BaseException):
    """machine.reset(): past the agent's `except Exception`, like a reboot."""


def _jpeg_size(data):
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        length = (data[i + 2] << 8) | data[i + 3]
        if marker in (0xC0, 0xC1, 0xC2):
            return (data[i + 7] << 8) | data[i + 8], (data[i + 5] << 8) | data[i + 6]
        i += 2 + length
    return 640, 480


def install_micropython(spec):
    """The modules a board has and CPython does not, just enough of each."""
    import gc
    import socket as _socket
    import types

    mac = bytes(int(x, 16) for x in spec["mac"].split(":"))
    start = time.monotonic()

    time.ticks_ms = lambda: int((time.monotonic() - start) * 1000)
    time.ticks_us = lambda: int((time.monotonic() - start) * 1_000_000)
    time.ticks_diff = lambda a, b: a - b
    time.ticks_add = lambda t, d: t + d
    time.sleep_ms = lambda ms: time.sleep(ms / 1000.0)
    time.sleep_us = lambda us: time.sleep(us / 1_000_000.0)
    gc.mem_free = lambda: 96_000 + random.randint(-4000, 4000)
    gc.mem_alloc = lambda: 40_000
    sys.print_exception = lambda exc, *a: __import__("traceback").print_exception(exc)

    # usocket: MicroPython's stream methods on CPython's socket.
    class Socket(_socket.socket):
        def write(self, data):
            try:
                if self.gettimeout() == 0.0:
                    return self.send(data)
                self.sendall(data)
                return len(data)
            except BlockingIOError:
                return None

        def read(self, n=4096):
            try:
                return self.recv(n)
            except BlockingIOError:
                return None

        def readinto(self, buf):
            try:
                return self.recv_into(buf)
            except BlockingIOError:
                return None

    usocket = types.ModuleType("usocket")
    usocket.__dict__.update({k: getattr(_socket, k) for k in dir(_socket)
                             if not k.startswith("__")})
    usocket.socket = Socket
    sys.modules["usocket"] = usocket

    machine = types.ModuleType("machine")

    class Pin:
        IN, OUT, OPEN_DRAIN = 1, 3, 7
        PULL_UP, PULL_DOWN = 1, 2
        IRQ_RISING, IRQ_FALLING = 1, 2

        def __init__(self, gpio, mode=-1, pull=-1, value=None, **kw):
            self.gpio, self._v = gpio, value or 0

        def init(self, *a, **kw):
            pass

        def value(self, v=None):
            if v is None:
                return self._v
            self._v = 1 if v else 0

        def on(self):
            self._v = 1

        def off(self):
            self._v = 0

        def irq(self, handler=None, trigger=0, **kw):
            return None

        __call__ = value

    class PWM:
        def __init__(self, pin, freq=1000, duty=0, duty_u16=None, **kw):
            self._f, self._d = freq, duty_u16 if duty_u16 is not None else duty * 64

        def freq(self, f=None):
            if f is None:
                return self._f
            self._f = f

        def duty(self, d=None):
            if d is None:
                return self._d // 64
            self._d = d * 64

        def duty_u16(self, d=None):
            if d is None:
                return self._d
            self._d = d

        def deinit(self):
            pass

    class RTC:
        def datetime(self, t=None):
            return None if t is not None else time.localtime()[:7] + (0,)

    class SoftI2C:
        def __init__(self, *a, **kw):
            pass

        def scan(self):
            return []

        def __getattr__(self, name):
            def fail(*a, **kw):
                raise OSError(19, "ENODEV")
            return fail

    def reset():
        raise _Reset()

    machine.Pin, machine.PWM, machine.RTC, machine.SoftI2C = Pin, PWM, RTC, SoftI2C
    machine.I2C = SoftI2C
    machine.reset = reset
    machine.freq = lambda f=None: 240_000_000
    machine.unique_id = lambda: mac
    sys.modules["machine"] = machine

    network = types.ModuleType("network")
    network.STA_IF, network.AP_IF = 0, 1
    network.STAT_IDLE, network.STAT_CONNECTING, network.STAT_GOT_IP = 1000, 1001, 1010

    class WLAN:
        PM_NONE = 0

        def __init__(self, iface=0):
            pass

        def active(self, on=None):
            return True

        def config(self, *a, **kw):
            if a and a[0] == "mac":
                return mac
            return None

        def isconnected(self):
            return True

        def connect(self, *a, **kw):
            pass

        def disconnect(self):
            pass

        def ifconfig(self):
            return (spec["ip"], "255.255.255.0", DEMO["gateway"], DEMO["gateway"])

        def status(self, *a):
            return spec["rssi"] + random.randint(-3, 3) if a else network.STAT_GOT_IP

    network.WLAN = WLAN
    sys.modules["network"] = network

    esp = types.ModuleType("esp")
    esp.flash_size = lambda: 4 * 1024 * 1024
    sys.modules["esp"] = esp
    esp32 = types.ModuleType("esp32")
    esp32.HEAP_DATA = 4
    esp32.idf_heap_info = lambda kind: [(262144, 88000 + random.randint(-3000, 3000),
                                         32768, 70000)]
    sys.modules["esp32"] = esp32

    camera = types.ModuleType("camera")
    photo = b""
    if spec.get("photo"):
        with open(spec["photo"], "rb") as fh:
            photo = fh.read()

    class _Names:
        def __init__(self, *names):
            for i, n in enumerate(names):
                setattr(self, n, i)

    camera.PixelFormat = _Names("RGB565", "GRAYSCALE", "JPEG")
    camera.FrameSize = _Names("QQVGA", "QCIF", "HQVGA", "QVGA", "CIF", "HVGA", "VGA",
                              "SVGA", "XGA", "HD", "SXGA", "UXGA")
    camera.GrabMode = _Names("WHEN_EMPTY", "LATEST")

    class Camera:
        def __init__(self, pixel_format=0, **kw):
            self.jpeg = pixel_format == camera.PixelFormat.JPEG
            self.w, self.h = _jpeg_size(photo) if photo else (160, 120)
            self.vflip = self.hmirror = False

        def init(self):
            pass

        def deinit(self):
            pass

        def capture(self):
            if self.jpeg:
                return photo
            return bytes(self.w * self.h)

        def get_pixel_width(self):
            return self.w

        def get_pixel_height(self):
            return self.h

        def get_sensor_name(self):
            return "OV2640"

    camera.Camera = Camera
    sys.modules["camera"] = camera

    # Last, once the standard library has read them: what the agent calls itself.
    impl = dict(vars(sys.implementation))
    impl.update(name="micropython", version=(1, 24, 1), _machine="ESP32")
    sys.implementation = types.SimpleNamespace(**impl)


def _orphaned(parent):
    """A board with no console behind it has nothing to do: stop with it."""
    while True:
        time.sleep(1)
        if os.getppid() != parent:
            os._exit(0)


def run_board(where):
    with open(os.path.join(where, "demo.json")) as fh:
        spec = json.load(fh)
    parent = int(os.environ.get("AUTOBLOX_DEMO_PARENT") or os.getppid())
    os.environ["AUTOBLOX_DEMO_PARENT"] = str(parent)
    threading.Thread(target=_orphaned, args=(parent,), daemon=True).start()
    sys.stdout.reconfigure(line_buffering=True)
    os.chdir(where)
    sys.path.insert(0, where)
    install_micropython(spec)
    try:
        import agent
        agent.main()
    except _Reset:
        # A reboot: the same process image, from the top, as a board does.
        os.execv(sys.executable, [sys.executable, os.path.abspath(__file__),
                                  "--board", where])


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--serve", action="store_true", help="run until interrupted")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--photo", help="a JPEG for the camera board to stream")
    ap.add_argument("--settle", type=float, default=20.0,
                    help="seconds to let the boards run before saying ready")
    ap.add_argument("--dir", help="work here instead of a temporary directory")
    ap.add_argument("--keep", action="store_true", help="leave the directory behind")
    ap.add_argument("--board", help=argparse.SUPPRESS)
    a = ap.parse_args()
    if a.board:
        return run_board(a.board)
    if a.photo:
        a.photo = os.path.abspath(a.photo)
    else:
        default = os.path.join(ROOT, "docs", "screenshots", "source", "bench.jpg")
        a.photo = default if os.path.isfile(default) else None
    run_console(a)


if __name__ == "__main__":
    main()
