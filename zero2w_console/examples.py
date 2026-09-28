"""Example flows — the library, shown doing something.

They run top to bottom as a course: each step adds a kind of node the ones
above it did not need. Every node type in the registry appears in at least
one, which tests/test_examples.py holds to.
"""
from . import flows as flowmod

# Canvas geometry. NODE_W is 184 in the editor, so a column every 260px leaves
# a comfortable gutter for the links to curve through.
COL = 260
ROW = 150
X0, Y0 = 60, 60

# The headings the Examples menu groups them under, in order.
STEPS = {1: "First steps", 2: "Deciding", 3: "Sensors and buses",
         4: "The network", 5: "Boards and the host", 6: "Cameras",
         7: "Machines"}

# A dropdown that names a pin, a device or a flow on *your* console. An example
# cannot know any of them, so they arrive empty and the editor asks — rather
# than shipping a guess that drives a pin something else is wired to.
PICKED = ("devices", "ble_devices", "flows", "nodes_in_flow")


def blanks(ntype):
    """The fields of a node type that an example leaves for you to choose."""
    out = {}
    for f in (flowmod.REGISTRY.get(ntype) or {}).get("fields", []):
        if f["kind"] == "pin":
            out[f["key"]] = None
        elif f["kind"] == "combo" and f.get("options_from") in PICKED:
            out[f["key"]] = ""
    return out


def _n(nid, ntype, col=0, row=0, **config):
    config.update(blanks(ntype))
    return {"id": nid, "type": ntype,
            "x": X0 + col * COL, "y": Y0 + row * ROW,
            "config": config}


def _e(src, dst, from_port="out", to_port="in", from_side="right", to_side="left"):
    return {"id": "e_%s_%s" % (src, dst), "from": src, "fromPort": from_port,
            "fromSide": from_side, "to": dst, "toPort": to_port,
            "toSide": to_side}


def _flow(fid, step, name, about, nodes, edges, board=None, tags=None):
    # Added switched off, always: nothing in it points at your hardware until
    # you choose the pins and devices.
    out = {"id": fid, "step": step, "name": name, "about": about,
           "enabled": False, "nodes": nodes, "edges": edges}
    if board:
        out["board"] = board
    if tags:
        out["needs_tags"] = tags
    return out


def choose(flow, picks):
    """Fill in what an example left blank: {node_id: {field: value}}. For the
    demo world and the tests, which need a flow that actually drives pins."""
    for node in flow["nodes"]:
        node["config"].update(picks.get(node["id"], {}))
    return flow


TANK = {"name": "tank_level", "type": "number", "unit": "%",
        "desc": "How full the tank is, as a percentage"}
ARMED = {"name": "drive_armed", "type": "number", "share": True,
         "boot_reset": True, "initial": 0,
         "desc": "1 lets the wheels turn. Anything else stops them."}


# -- 1 First steps ---------------------------------------------------------
def blink():
    return _flow(
        "ex_blink", 1, "Blink a pin",
        "Every second the Interval flips the GPIO write, so whatever is on "
        "that pin blinks. Choose the pin, then enable the flow.",
        [_n("t", "timer.interval", 0, 0, every=1000),
         _n("p", "gpio.out", 1, 0, action="toggle")],
        [_e("t", "p")])


def press():
    return _flow(
        "ex_press", 1, "A button lights a light",
        "The light is on for as long as the button is held: the GPIO edge "
        "sends 1 or 0 on every change and the GPIO write copies it. Choose a "
        "pin for each.",
        [_n("b", "gpio.in", 0, 0, edges="both", debounce=50),
         _n("o", "gpio.out", 1, 0, action="from-payload")],
        [_e("b", "o")])


def hello():
    return _flow(
        "ex_hello", 1, "Say something by hand",
        "Press Run on the Manual node and the Log prints hello. Change what "
        "the Set value node holds and the Log says that instead.",
        [_n("go", "manual.fire", 0, 0),
         _n("v", "logic.set", 1, 0, value="hello"),
         _n("l", "log.write", 2, 0, level="ok", message="{{payload}}")],
        [_e("go", "v"), _e("v", "l")])


def later():
    return _flow(
        "ex_later", 1, "Act a moment later",
        "The light follows the button two seconds late: the Delay holds "
        "each message, then passes it on. Choose the button and light pins.",
        [_n("b", "gpio.in", 0, 0, edges="both", debounce=50),
         _n("d", "logic.delay", 1, 0, ms=2000),
         _n("o", "gpio.out", 2, 0, action="from-payload")],
        [_e("b", "d"), _e("d", "o")])


# -- 2 Deciding ------------------------------------------------------------
def webhook_to_pin():
    return _flow(
        "ex_hook", 2, "Drive a pin from the network",
        "POST 1 or 0 to /api/hook/relay, with the console token, and the pin "
        "follows. The If sends anything truthy to the high GPIO write and "
        "everything else to the low one.",
        [_n("h", "http.webhook", 0, 0, path="relay"),
         _n("i", "logic.if", 1, 0, op="truthy", value=""),
         _n("on", "gpio.out", 2, 0, action="high"),
         _n("off", "gpio.out", 2, 1, action="low")],
        [_e("h", "i"),
         _e("i", "on", from_port="true"),
         _e("i", "off", from_port="false", from_side="bottom", to_side="left")])


def button_latch():
    return _flow(
        "ex_latch", 2, "A button that latches",
        "One press turns the light on and the next turns it off. On change "
        "makes each press one message, and the Toggle remembers which way "
        "it is.",
        [_n("b", "gpio.in", 0, 0, edges="rising", debounce=50),
         _n("c", "logic.edge", 1, 0, edges="rising"),
         _n("k", "logic.toggle", 2, 0, in_action="toggle", in2_action="off",
            on_value="1", off_value="0"),
         _n("o", "gpio.out", 3, 0, action="from-payload")],
        [_e("b", "c"), _e("c", "k"), _e("k", "o")])


def count_and_act():
    return _flow(
        "ex_count", 2, "On the third press",
        "The Counter stays quiet until the third press, lets the Log speak, "
        "then starts again. A POST to the reset-count Webhook sets it back "
        "to zero at any time.",
        [_n("b", "gpio.in", 0, 0, edges="rising", debounce=50),
         _n("r", "http.webhook", 0, 1, path="reset-count"),
         _n("c", "logic.count", 1, 0, step=1, start=0, target=3,
            auto_reset="reset"),
         _n("l", "log.write", 2, 0, level="ok",
            message="that is {{meta.count}} presses")],
        [_e("b", "c"),
         _e("r", "c", to_port="reset", from_side="right", to_side="top"),
         _e("c", "l")])


def overheat():
    return _flow(
        "ex_overheat", 2, "Say something when the board is hot",
        "The Metric threshold fires when the CPU passes 70 C. The Throttle "
        "lets one of those through every five minutes, which sets the "
        "board_hot tag and writes a Log line.",
        [_n("t", "metric.threshold", 0, 0, metric="cpu-thermal", op="above",
            value=70, rearm=60),
         _n("k", "logic.throttle", 1, 0, ms=300000),
         _n("w", "tag.set", 2, 0, tag="board_hot", value="1"),
         _n("l", "log.write", 3, 0, level="serious",
            message="{{meta.metric}} at {{payload}} C, over {{meta.threshold}}")],
        [_e("t", "k"), _e("k", "w"), _e("w", "l")],
        tags=[{"name": "board_hot", "type": "bool",
               "desc": "Set when the CPU passed its warning trip"}])


def door_left_open():
    return _flow(
        "ex_timer", 2, "Only if it stays that way",
        "The door_open tag is set only once the door has stayed open thirty "
        "seconds, and the light stays on until a minute after the motion "
        "stops. Each Timer drops a message the input takes back in time.",
        [_n("d", "gpio.in", 0, 0, edges="both", debounce=50),
         _n("w", "logic.timer", 1, 0, mode="wait until true for", ms=30000),
         _n("t", "tag.set", 2, 0, tag="door_open", value="1"),
         # Both edges: the falling one is what starts the minute.
         _n("m", "gpio.in", 0, 1, edges="both", debounce=100),
         _n("k", "logic.timer", 1, 1, mode="keep it true for", ms=60000),
         _n("o", "gpio.out", 2, 1, action="from-payload")],
        [_e("d", "w"), _e("w", "t"), _e("m", "k"), _e("k", "o")],
        tags=[{"name": "door_open", "type": "bool",
               "desc": "Set once the door has been open a while"}])


# -- 3 Sensors and buses ---------------------------------------------------
def i2c_scan():
    return _flow(
        "ex_i2c_scan", 3, "What is on the I2C bus",
        "Press Run and the Log lists every address that answers on I2C bus "
        "1. Do this first with a new chip, to learn the address the other "
        "I2C nodes need.",
        [_n("go", "manual.fire", 0, 0),
         _n("s", "i2c.scan", 1, 0, bus=1),
         _n("l", "log.write", 2, 0, level="ok", message="found {{payload}}")],
        [_e("go", "s"), _e("s", "l")])


def pump_control():
    return _flow(
        "ex_pump", 3, "A sensor that drives a pump",
        "Every two seconds an I2C read takes the level, Scale makes it a "
        "percentage, Smooth steadies it and tank_level records it. "
        "Hysteresis runs the pump over 80% and stops it under 40%; choose "
        "its pin.",
        [_n("t", "timer.interval", 0, 0, every=2000),
         _n("r", "i2c.read", 1, 0, bus=1, address="0x48", register="0x00",
            length=2, format="int-be"),
         _n("s", "math.scale", 2, 0, in_min=0, in_max=4095, out_min=0,
            out_max=100, clamp="yes", decimals=1),
         _n("m", "math.smooth", 3, 0, mode="running average", window=5,
            decimals=1),
         _n("w", "tag.set", 4, 0, tag="tank_level", value="{{payload}}"),
         _n("h", "logic.hysteresis", 3, 1, on_above=80, off_below=40,
            on_value="1", off_value="0", emit="on change"),
         _n("p", "gpio.out", 4, 1, action="from-payload")],
        [_e("t", "r"), _e("r", "s"), _e("s", "m"), _e("m", "w"),
         _e("m", "h", from_side="bottom", to_side="top"), _e("h", "p")],
        tags=[TANK])


def tag_decoupling():
    return _flow(
        "ex_tag_react", 3, "React to a tag, from anywhere",
        "Runs whenever tank_level rises, with no wire to the flow that "
        "measures it. The If logs a warning at 95% or more and a quiet line "
        "otherwise.",
        [_n("t", "tag.change", 0, 0, tag="tank_level", when="rises"),
         _n("i", "logic.if", 1, 0, op=">=", value=95),
         _n("l", "log.write", 2, 0, level="warn",
            message="tank at {{payload}}% and still rising (was {{meta.previous}})"),
         _n("q", "log.write", 2, 1, level="idle",
            message="tank {{payload}}%")],
        [_e("t", "i"),
         _e("i", "l", from_port="true"),
         _e("i", "q", from_port="false", from_side="bottom", to_side="left")],
        tags=[TANK])


def bus_write():
    return _flow(
        "ex_bus_write", 3, "Send bytes to a chip",
        "The first Manual node sets every pin of an MCP23017 at 0x20 to an "
        "output and drives them high over I2C. The second asks an SPI flash "
        "chip for its ID, which the Log prints.",
        [_n("io", "manual.fire", 0, 0),
         # IODIRA (0x00) all outputs, then GPIOA (0x12) all high.
         _n("dir", "i2c.write", 1, 0, bus=1, address="0x20", data="0x00 0x00"),
         _n("hi", "i2c.write", 2, 0, bus=1, address="0x20", data="0x12 0xff"),
         _n("id", "manual.fire", 0, 1),
         # 0x9f is the JEDEC read-ID command; the three bytes after it clock
         # the maker and part number back.
         _n("spi", "spi.transfer", 1, 1, device="/dev/spidev1.0",
            data="0x9f 0x00 0x00 0x00", speed=500000, spi_mode="0"),
         _n("l", "log.write", 2, 1, level="ok", message="flash says {{payload}}")],
        [_e("io", "dir"), _e("dir", "hi"), _e("id", "spi"), _e("spi", "l")])


# -- 4 The network ---------------------------------------------------------
def fetch():
    return _flow(
        "ex_fetch", 4, "Ask a website",
        "Once an hour an HTTP request fetches a line of text from GitHub "
        "and the Log prints it. Swap the URL for any API; a JSON reply "
        "arrives already parsed.",
        [_n("t", "timer.interval", 0, 0, every=3600000),
         _n("r", "http.request", 1, 0, method="GET",
            url="https://api.github.com/zen", timeout=10),
         _n("l", "log.write", 2, 0, level="ok", message="{{payload}}")],
        [_e("t", "r"), _e("r", "l")])


def mqtt_bridge():
    return _flow(
        "ex_mqtt", 4, "Convert it over MQTT",
        "A temperature published to auto-blox/celsius comes back on "
        "auto-blox/fahrenheit: MQTT subscribe hears it, a Formula converts "
        "it and MQTT publish sends it. It needs a broker on this board.",
        [_n("s", "mqtt.subscribe", 0, 0, host="localhost", port=1883,
            topic="auto-blox/celsius", tls="no"),
         _n("f", "math.expr", 1, 0, expr="{{payload}} * 9 / 5 + 32",
            decimals=1),
         _n("p", "mqtt.publish", 2, 0, host="localhost", port=1883,
            topic="auto-blox/fahrenheit", payload="{{payload}}", retain="no",
            tls="no")],
        [_e("s", "f"), _e("f", "p")])


def housekeeping():
    return _flow(
        "ex_shell", 4, "Housekeeping",
        "Every hour a Shell command checks how full the SD card is and the "
        "Log shows the answer. Call flow then starts another flow; choose "
        "the flow and the node in it to fire.",
        [_n("t", "timer.interval", 0, 0, every=3600000),
         _n("sh", "shell.run", 1, 0, cmd="df --output=pcent / | tail -1",
            timeout=15),
         _n("l", "log.write", 2, 0, level="ok", message="SD card {{payload}} full"),
         _n("c", "flow.call", 2, 1)],
        [_e("t", "sh"), _e("sh", "l"),
         _e("sh", "c", from_side="bottom", to_side="left")])


# -- 5 Boards and the host -------------------------------------------------
def heartbeat():
    return _flow(
        "ex_heartbeat", 5, "Heartbeat",
        "The first flow for a new board: it blinks an LED every five seconds "
        "and tells the host each beat. Choose the LED pin, GPIO2 on most "
        "ESP32 boards; Heartbeat watch is the other half.",
        [_n("t", "timer.interval", 0, 0, every=5000),
         _n("c", "logic.count", 1, 0, step=1, start=0, target=0),
         _n("led", "gpio.out", 2, 0, action="toggle"),
         _n("say", "host.notify", 2, 1, kind="heartbeat",
            payload="{{meta.count}}", level="ok")],
        [_e("t", "c"), _e("c", "led"),
         _e("c", "say", from_side="bottom", to_side="left")],
        board="esp32")


def heartbeat_watch():
    return _flow(
        "ex_heartbeat_watch", 5, "Heartbeat watch",
        "The host half of Heartbeat. Each beat from any device is written to "
        "the last_beat and last_beat_from tags and logged, so one flow "
        "watches every board.",
        [_n("t", "host.event", 0, 0, kind="heartbeat"),
         _n("w", "tag.set", 1, 0, tag="last_beat", value="{{payload}}"),
         _n("who", "tag.set", 2, 0, tag="last_beat_from",
            value="{{meta.device}}"),
         _n("l", "log.write", 3, 0, level="ok",
            message="beat {{payload}} from {{meta.device}}")],
        [_e("t", "w"), _e("w", "who"), _e("who", "l")],
        tags=[{"name": "last_beat", "type": "number",
               "desc": "The beat number of the last heartbeat received"},
              {"name": "last_beat_from", "type": "text",
               "desc": "Which device sent the last heartbeat"}])


def device_round_trip():
    return _flow(
        "ex_fleet", 5, "A device reports, this board answers",
        "When a device reports motion, this records which one and a Device "
        "command turns a camera on. Choose the camera's device; the Throttle "
        "stops it asking more than every ten seconds.",
        [_n("t", "host.event", 0, 0, kind="motion"),
         _n("w", "tag.set", 1, 0, tag="last_motion", value="{{meta.device}}"),
         _n("k", "logic.throttle", 2, 0, ms=10000),
         _n("c", "device.command", 3, 0, op="camera-on",
            frame_size="QQVGA", format="greyscale"),
         _n("l", "log.write", 2, 1, level="ok",
            message="{{meta.device}} saw something")],
        [_e("t", "w"), _e("w", "k"), _e("k", "c"),
         _e("w", "l", from_side="bottom", to_side="left")],
        tags=[{"name": "last_motion", "type": "text",
               "desc": "Which device last reported motion"}])


def ble_sensor():
    return _flow(
        "ex_ble", 5, "A Bluetooth sensor",
        "Pair a device on the IoT page's Bluetooth tab and choose it in each "
        "BLE node. BLE device holds the connection, BLE read logs its "
        "battery, BLE notify logs what it sends and Run makes BLE write send "
        "01.",
        [_n("t", "timer.interval", 0, 0, every=5000),
         _n("link", "ble.link", 1, 0),
         _n("bat", "ble.read", 2, 0, char="2a19", format="uint8"),
         _n("lb", "log.write", 3, 0, level="ok", message="battery {{payload}}%"),
         _n("n", "ble.notify", 0, 1, char="", format="hex"),
         _n("ln", "log.write", 1, 1, level="idle", message="sent {{payload}}"),
         _n("go", "manual.fire", 0, 2),
         _n("w", "ble.write", 1, 2, char="", format="hex", value="01")],
        [_e("t", "link"), _e("link", "bat"), _e("bat", "lb"),
         _e("n", "ln"), _e("go", "w")])


# -- 6 Cameras -------------------------------------------------------------
def camera_on_motion():
    return _flow(
        "ex_cam", 6, "A camera that runs when something moves",
        "On an ESP32-CAM, motion at the GPIO edge turns the Camera feed on "
        "and Camera to screen shows it on the Cameras page. The Interval "
        "turns it off each minute, and Tell the host reports every motion.",
        [_n("m", "gpio.in", 0, 0, edges="rising", debounce=100),
         _n("q", "timer.interval", 0, 1, every=60000),
         _n("k", "logic.toggle", 1, 0, in_action="on", in2_action="off",
            on_value="1", off_value="0"),
         _n("c", "camera.feed", 2, 0, frame_size="QQVGA", format="greyscale"),
         _n("p", "camera.publish", 3, 0, label="Doorway", every_ms=2000),
         _n("n", "host.notify", 2, 1, kind="motion", payload="{{payload}}",
            level="ok")],
        [_e("m", "k"),
         _e("q", "k", to_port="in2", from_side="right", to_side="top"),
         _e("k", "c"), _e("c", "p"),
         _e("m", "n", from_side="bottom", to_side="left")],
        board="esp32cam")


def snapshots():
    return _flow(
        "ex_snapshot", 6, "A picture a minute, kept and sent",
        "Every minute an ESP32-CAM takes a JPEG with Camera capture, keeps it "
        "on its microSD card with Save to SD, and passes it to the console "
        "with Send to host.",
        [_n("t", "timer.interval", 0, 0, every=60000),
         _n("c", "camera.capture", 1, 0, frame_size="VGA", format="jpeg"),
         _n("s", "sd.save", 2, 0, folder="captures", keep=500),
         _n("h", "picture.send", 3, 0, kind="picture"),
         _n("l", "log.write", 2, 1, level="ok",
            message="{{payload}} bytes to {{meta.file}}")],
        [_e("t", "c"), _e("c", "s"), _e("s", "h"),
         _e("s", "l", from_side="bottom", to_side="left")],
        board="esp32cam")


def snapshots_kept():
    return _flow(
        "ex_snapshot_keep", 6, "Pictures from the field, kept here",
        "The console half of the last example. A picture a board sends "
        "arrives here as a Device event, and Save to SD keeps it under "
        "~/captures/field on this board.",
        [_n("t", "host.event", 0, 0, kind="picture"),
         _n("s", "sd.save", 1, 0, folder="field", keep=1000),
         _n("l", "log.write", 2, 0, level="ok",
            message="{{meta.device}}: {{meta.format}} {{meta.width}}x"
                    "{{meta.height}} to {{meta.file}}")],
        [_e("t", "s"), _e("s", "l")])


# -- 7 Machines ------------------------------------------------------------
def fill_heat_drain():
    return _flow(
        "ex_sequence", 7, "Fill, heat, drain",
        "Three Steps run in turn: fill until tank_level reaches 90%, heat "
        "for ten minutes, then drain until it reads 5%. Each Log stands in "
        "for a pump, heater or valve, on as its step is entered and off as "
        "it is left.",
        [_n("start", "manual.fire", 1, 0),
         _n("level", "tag.change", 0, 2, tag="tank_level", when="changes"),
         _n("full", "logic.if", 1, 1, op=">=", value=90),
         _n("empty", "logic.if", 1, 4, op="<=", value=5),
         _n("fill", "logic.step", 2, 0, name="fill", leave_after=0),
         _n("heat", "logic.step", 2, 2, name="heat", leave_after=600000),
         _n("drain", "logic.step", 2, 4, name="drain", leave_after=0),
         _n("a", "log.write", 3, 0, level="ok", message="pump on"),
         _n("b", "log.write", 3, 1, level="idle", message="pump off"),
         _n("c", "log.write", 3, 2, level="ok", message="heater on"),
         _n("d", "log.write", 3, 3, level="idle", message="heater off"),
         _n("g", "log.write", 3, 4, level="ok", message="drain open"),
         _n("h", "log.write", 3, 5, level="idle", message="drain shut")],
        [_e("start", "fill", to_side="top"),
         _e("level", "full"), _e("level", "empty"),
         _e("full", "fill", from_port="true", to_port="done"),
         _e("empty", "drain", from_port="true", to_port="done"),
         _e("fill", "heat", from_port="next", from_side="bottom", to_side="top"),
         _e("heat", "drain", from_port="next", from_side="bottom", to_side="top"),
         _e("fill", "a", from_port="entered"), _e("fill", "b", from_port="left"),
         _e("heat", "c", from_port="entered"), _e("heat", "d", from_port="left"),
         _e("drain", "g", from_port="entered"), _e("drain", "h", from_port="left")],
        tags=[TANK])


def motor_bench():
    """One H-bridge pair, built out of general blocks rather than a motor
    node."""
    return _flow(
        "ex_motor_bench", 7, "Motor bench",
        "Each Run on the Manual node nudges one motor forward at about 35% duty, "
        "and the Watchdog stops it 400 ms after the last one. Choose the "
        "H-bridge's two PWM pins; Motor drive does two wheels.",
        [_n("go", "manual.fire", 0, 1),
         _n("speed", "logic.set", 1, 1, value="0.35"),
         _n("dz", "math.deadband", 2, 1, threshold=0.08, full=1,
            rescale="yes", resting="0", decimals=3),
         _n("ramp", "math.ramp", 3, 1, rate_up=1.5, rate_down=4, start=0,
            decimals=3),
         _n("fwd", "math.split", 4, 0, part="forward only", full=1, scale=60,
            floor=25, invert="no", decimals=2),
         _n("rev", "math.split", 4, 1, part="back only", full=1, scale=60,
            floor=25, invert="no", decimals=2),
                  _n("lpwm", "pwm.out", 5, 0, action="start", mode="software",
            freq=1000, units="percent", duty="{{payload}}", channel=0),
         _n("rpwm", "pwm.out", 5, 1, action="start", mode="software",
            freq=1000, units="percent", duty="{{payload}}", channel=0),
         _n("dog", "safety.watchdog", 4, 2, timeout=400, value="0",
            recovery="no"),
         _n("stopl", "pwm.out", 5, 2, action="stop", mode="software",
            freq=1000, units="percent", duty="0", channel=0),
         _n("stopr", "pwm.out", 5, 3, action="stop", mode="software",
            freq=1000, units="percent", duty="0", channel=0)],
        [_e("go", "speed"), _e("speed", "dz"), _e("dz", "ramp"),
         _e("ramp", "fwd"), _e("ramp", "rev"),
         _e("fwd", "lpwm"), _e("rev", "rpwm"),
         _e("ramp", "dog", from_side="bottom", to_side="left"),
         _e("dog", "stopl"), _e("dog", "stopr")],
        board="esp32")


def motor_drive():
    """Both bridges, driven live from tags."""
    return _flow(
        "ex_motor_drive", 7, "Motor drive — two wheels, live",
        "Two wheels driven from the tag table: drive_speed from -1 to 1 is "
        "throttle, drive_steer is positive for right, and nothing moves until "
        "drive_armed is 1. Choose the four PWM pins; if the chain stops, the "
        "Watchdog takes every duty to zero within 400 ms.",
        [_n("tick", "timer.interval", 0, 1, every=100),
         _n("arm", "tag.read", 1, 1, tag="drive_armed"),
         _n("gate", "logic.if", 2, 1, op=">=", value="0.5"),
         _n("speed", "tag.read", 3, 1, tag="drive_speed"),
         _n("dz", "math.deadband", 4, 1, threshold=0.05, full=1,
            rescale="yes", resting="0", decimals=3),
         # Steer comes off the right wheel and onto the left, so a positive steer
         # turns right. Yaw for a differential drive goes as (right - left), which
         # here is -2 x steer whatever the throttle is doing, so the turn is the
         # same direction forwards and in reverse. The splits clamp.
         _n("mixr", "math.expr", 5, 0,
            expr="{{payload}} - {{tag.drive_steer}}", decimals=3),
         _n("mixl", "math.expr", 5, 2,
            expr="{{payload}} + {{tag.drive_steer}}", decimals=3),
         # A Ramp per wheel, and *after* the mix rather than before it. One Ramp
         # on the throttle would leave steering unlimited: a steer flicked from
         # -0.9 to 0.9 reversed a wheel inside one tick. Rate-limiting each wheel's
         # own command makes a crossing pass through zero instead of over it.
         _n("rampr", "math.ramp", 6, 0, rate_up=1.2, rate_down=4, start=0,
            decimals=3),
         _n("rampl", "math.ramp", 6, 2, rate_up=1.2, rate_down=4, start=0,
            decimals=3),
         # The right bridge's two inputs, then the left's.
         _n("rf", "math.split", 7, 0, part="forward only", full=1, scale=100,
            floor=25, invert="no", decimals=2),
         _n("rb", "math.split", 7, 1, part="back only", full=1, scale=100,
            floor=25, invert="no", decimals=2),
         _n("lf", "math.split", 7, 2, part="forward only", full=1, scale=100,
            floor=25, invert="no", decimals=2),
         _n("lb", "math.split", 7, 3, part="back only", full=1, scale=100,
            floor=25, invert="no", decimals=2),
         _n("p27", "pwm.out", 8, 0, action="start", mode="software",
            freq=1000, units="percent", duty="{{payload}}", channel=0),
         _n("p25", "pwm.out", 8, 1, action="start", mode="software",
            freq=1000, units="percent", duty="{{payload}}", channel=0),
         _n("p33", "pwm.out", 8, 2, action="start", mode="software",
            freq=1000, units="percent", duty="{{payload}}", channel=0),
         _n("p32", "pwm.out", 8, 3, action="start", mode="software",
            freq=1000, units="percent", duty="{{payload}}", channel=0),
         # Fed from the dead zone, so it starves the moment the chain stops for
         # any reason. Its zero goes straight into the four splits, which is why no
         # stop nodes are needed: zero through a Sign split is zero duty.
         _n("dog", "safety.watchdog", 5, 4, timeout=400, value="0",
            recovery="no")],
        [_e("tick", "arm"), _e("arm", "gate"),
         _e("gate", "speed", from_port="true"),
         _e("speed", "dz"),
         _e("dz", "mixr"), _e("dz", "mixl"),
         _e("mixr", "rampr"), _e("mixl", "rampl"),
         # Forward before back on each wheel, deliberately: the pin going to zero
         # is written before the pin coming up, which closes the overlap on a
         # falling crossing. A rising one is closed by the Ramp above.
         _e("rampr", "rf"), _e("rampr", "rb"),
         _e("rampl", "lf"), _e("rampl", "lb"),
         _e("rf", "p27"), _e("rb", "p25"),
         _e("lf", "p33"), _e("lb", "p32"),
         _e("dz", "dog", from_side="bottom", to_side="left"),
         _e("dog", "rf", from_side="right", to_side="bottom"),
         _e("dog", "rb", from_side="right", to_side="bottom"),
         _e("dog", "lf", from_side="right", to_side="bottom"),
         _e("dog", "lb", from_side="right", to_side="bottom"),
         # Disarmed resets both Ramps, so arming again starts from zero.
         _e("gate", "rampr", from_port="false", to_port="reset",
            from_side="bottom", to_side="top"),
         _e("gate", "rampl", from_port="false", to_port="reset",
            from_side="bottom", to_side="top")],
        board="esp32",
        tags=[ARMED,
              {"name": "drive_speed", "type": "number", "share": True,
               "initial": 0, "unit": "-1..1",
               "desc": "Throttle. Negative is reverse."},
              {"name": "drive_steer", "type": "number", "share": True,
               "initial": 0, "unit": "-1..1",
               "desc": "Turn: positive is right, negative is left. It comes "
                       "off the right wheel and onto the left, and the "
                       "direction is the same in reverse as going forward."}])


def motor_pad():
    """The same two bridges, driven from a Bluetooth controller instead."""
    return _flow(
        "ex_motor_pad", 7, "Motor drive — from a controller",
        "Motor drive from a Bluetooth controller: the right trigger is throttle, "
        "the left stick steers, A arms and B disarms, and the wheels stop if "
        "the pad goes quiet. A board cannot hold a controller yet, so today "
        "this waits for one.",
        [_n("tick", "timer.interval", 0, 1, every=100),
         _n("link", "pad.link", 1, 1, name=""),

         # The arm is its own branch, and it has to be: a button set to
         # `pressed` says nothing on the ticks you are not pressing it, so the
         # drive chain cannot hang off the back of it.
         _n("armbtn", "pad.button", 2, 0, button="a", emit="pressed",
            stale_ms=250),
         _n("offbtn", "pad.button", 2, 4, button="b", emit="pressed",
            stale_ms=250),
         _n("latch", "logic.toggle", 3, 0, in_action="on", in2_action="off",
            on_value="1", off_value="0", start="off"),
         _n("setarm", "tag.set", 4, 0, tag="drive_armed", value="{{payload}}"),

         _n("arm", "tag.read", 2, 1, tag="drive_armed"),
         _n("gate", "logic.if", 3, 1, op=">=", value="0.5"),
         # Throttle is a node because it is the payload the chain carries;
         # steering is `{{pad.lx}}` in the Formulas below.
         _n("speed", "pad.axis", 4, 1, axis="right trigger", invert="no",
            stale_ms=250, decimals=3),
         _n("dz", "math.deadband", 5, 1, threshold=0.05, full=1,
            rescale="yes", resting="0", decimals=3),
         _n("mixr", "math.expr", 6, 0, expr="{{payload}} - {{pad.lx}}",
            decimals=3),
         _n("mixl", "math.expr", 6, 2, expr="{{payload}} + {{pad.lx}}",
            decimals=3),
         _n("rampr", "math.ramp", 7, 0, rate_up=1.2, rate_down=4, start=0,
            decimals=3),
         _n("rampl", "math.ramp", 7, 2, rate_up=1.2, rate_down=4, start=0,
            decimals=3),
         _n("rf", "math.split", 8, 0, part="forward only", full=1, scale=100,
            floor=25, invert="no", decimals=2),
         _n("rb", "math.split", 8, 1, part="back only", full=1, scale=100,
            floor=25, invert="no", decimals=2),
         _n("lf", "math.split", 8, 2, part="forward only", full=1, scale=100,
            floor=25, invert="no", decimals=2),
         _n("lb", "math.split", 8, 3, part="back only", full=1, scale=100,
            floor=25, invert="no", decimals=2),
         _n("p27", "pwm.out", 9, 0, action="start", mode="software",
            freq=1000, units="percent", duty="{{payload}}", channel=0),
         _n("p25", "pwm.out", 9, 1, action="start", mode="software",
            freq=1000, units="percent", duty="{{payload}}", channel=0),
         _n("p33", "pwm.out", 9, 2, action="start", mode="software",
            freq=1000, units="percent", duty="{{payload}}", channel=0),
         _n("p32", "pwm.out", 9, 3, action="start", mode="software",
            freq=1000, units="percent", duty="{{payload}}", channel=0),
         _n("dog", "safety.watchdog", 6, 4, timeout=400, value="0",
            recovery="no")],
        # The arm branch first, so the tag the gate reads was written this tick
        # rather than last.
        [_e("tick", "link"),
         _e("link", "armbtn", from_side="top"),
         _e("armbtn", "latch"),
         _e("link", "offbtn", from_side="bottom"),
         _e("offbtn", "latch", to_port="in2", to_side="top"),
         _e("latch", "setarm"),
         _e("link", "arm"),
         _e("arm", "gate"),
         _e("gate", "speed", from_port="true"),
         _e("speed", "dz"),
         _e("dz", "mixr"),
         _e("dz", "mixl"),
         _e("mixr", "rampr"),
         _e("mixl", "rampl"),
         _e("rampr", "rf"),
         _e("rampr", "rb"),
         _e("rampl", "lf"),
         _e("rampl", "lb"),
         _e("rf", "p27"),
         _e("rb", "p25"),
         _e("lf", "p33"),
         _e("lb", "p32"),
         _e("dz", "dog", from_side="bottom"),
         _e("dog", "rf", to_side="bottom"),
         _e("dog", "rb", to_side="bottom"),
         _e("dog", "lf", to_side="bottom"),
         _e("dog", "lb", to_side="bottom"),
         # Both routes to a reset, deliberately. Disarming resets the ramps so
         # re-arming starts from a standstill; the Watchdog resets them too, so
         # a pad that comes back after a dropout cannot resume at the duty it
         # left — which does not depend on what happens to feed the gate.
         _e("gate", "rampr", from_port="false", to_port="reset",
            from_side="bottom", to_side="top"),
         _e("gate", "rampl", from_port="false", to_port="reset",
            from_side="bottom", to_side="top"),
         _e("dog", "rampr", to_port="reset", to_side="top"),
         _e("dog", "rampl", to_port="reset", to_side="top")],
        board="esp32",
        tags=[ARMED])


def motor_arm():
    """The arm switch for `motor_drive`, joined to it by a tag and nothing
    else."""
    return _flow(
        "ex_motor_arm", 7, "Arm before it drives",
        "Two Manual nodes, Arm and Disarm, set the drive_armed tag the motor "
        "flows read, through a Latch. The Watchdog disarms it if the Interval "
        "feeding it stops, so after a dropout it must be armed again by hand.",
        [_n("on", "manual.fire", 0, 0),
         _n("off", "manual.fire", 0, 1),
         _n("feed", "timer.interval", 0, 2, every=200),
         _n("arm", "logic.latch", 1, 0, start="off", on_value="1",
            off_value="0", emit="on change"),
         _n("dog", "safety.watchdog", 1, 2, timeout=600, value="0",
            recovery="no"),
         _n("gate", "tag.set", 2, 0, tag="drive_armed", value="{{payload}}"),
         _n("say", "log.write", 2, 1, level="ok",
            message="drive_armed is now {{payload}}")],
        [_e("on", "arm"),
         _e("off", "arm", to_port="reset", to_side="top"),
         _e("feed", "dog", to_side="left"),
         _e("dog", "arm", to_port="reset", from_side="right", to_side="top"),
         _e("arm", "gate"),
         _e("arm", "say", from_side="bottom", to_side="left")],
        tags=[ARMED])


CATALOGUE = [
    # 1 First steps
    blink, press, hello, later,
    # 2 Deciding
    webhook_to_pin, button_latch, count_and_act, overheat, door_left_open,
    # 3 Sensors and buses
    i2c_scan, pump_control, tag_decoupling, bus_write,
    # 4 The network
    fetch, mqtt_bridge, housekeeping,
    # 5 Boards and the host
    heartbeat, heartbeat_watch, device_round_trip, ble_sensor,
    # 6 Cameras
    camera_on_motion, snapshots, snapshots_kept,
    # 7 Machines
    fill_heat_drain, motor_bench, motor_arm, motor_drive, motor_pad,
]


def catalogue():
    """Every example, built fresh, in the order the menu shows them."""
    out = [fn() for fn in CATALOGUE]
    for ex in out:
        ex["step_name"] = STEPS[ex["step"]]
        ex["runs_on"] = ex.get("board") or "this board"
        ex["unsupported"] = sorted({
            n["type"] for n in ex["nodes"]
            if ex.get("board") and not flowmod.runs_on_device(n["type"])})
    return out


def by_id(example_id):
    for ex in catalogue():
        if ex["id"] == example_id:
            return ex
    return None


# The bench the tests and the demo world wire the examples to: an ESP32 with
# the right bridge on D27/D25, the left on D33/D32, and its LED on GPIO2.
BENCH = {
    "ex_motor_bench": {"lpwm": {"gpio": 27}, "rpwm": {"gpio": 25},
                       "stopl": {"gpio": 27}, "stopr": {"gpio": 25}},
    "ex_motor_drive": {"p27": {"gpio": 27}, "p25": {"gpio": 25},
                       "p33": {"gpio": 33}, "p32": {"gpio": 32}},
    "ex_motor_pad": {"p27": {"gpio": 27}, "p25": {"gpio": 25},
                     "p33": {"gpio": 33}, "p32": {"gpio": 32}},
    "ex_heartbeat": {"led": {"gpio": 2}},
    "ex_cam": {"m": {"gpio": 13}},
}


def wired(example_id):
    """An example with its pins chosen as on BENCH."""
    ex = by_id(example_id)
    return choose(ex, BENCH.get(example_id, {})) if ex else None
