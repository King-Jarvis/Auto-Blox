"""Example flows — the library, shown doing something."""
from . import flows as flowmod

# Canvas geometry. NODE_W is 184 in the editor, so a column every 260px leaves
# a comfortable gutter for the links to curve through.
COL = 260
ROW = 150
X0, Y0 = 60, 60


def _n(nid, ntype, col=0, row=0, **config):
    return {"id": nid, "type": ntype,
            "x": X0 + col * COL, "y": Y0 + row * ROW,
            "config": config}


def _e(src, dst, from_port="out", to_port="in", from_side="right", to_side="left"):
    return {"id": "e_%s_%s" % (src, dst), "from": src, "fromPort": from_port,
            "fromSide": from_side, "to": dst, "toPort": to_port,
            "toSide": to_side}


def _flow(fid, name, about, nodes, edges, board=None, tags=None):
    # Added switched off, always. An example names pins and addresses that are a
    # guess about someone else's board: adding one to read it should not start
    # driving a pin something else is using. Point it at your hardware, then enable.
    out = {"id": fid, "name": name, "about": about, "enabled": False,
           "nodes": nodes, "edges": edges}
    if board:
        out["board"] = board
    if tags:
        out["needs_tags"] = tags
    return out


# --------------------------------------------------------------------------
def blink():
    return _flow(
        "ex_blink", "Blink a pin",
        "The smallest useful flow: a timer and an output. Everything else is "
        "this with more in the middle.",
        [_n("t", "timer.interval", 0, 0, every=1000),
         _n("p", "gpio.out", 1, 0, gpio=264, action="toggle")],
        [_e("t", "p")])


def button_latch():
    return _flow(
        "ex_latch", "A button that latches",
        "A momentary button that stays on until it is pressed again. The "
        "Toggle's left input is set to flip, so each press changes it — and "
        "On change means one press is one message, not one per bounce.",
        [_n("b", "gpio.in", 0, 0, gpio=263, edges="rising", debounce=50),
         _n("c", "logic.edge", 1, 0, edges="rising"),
         _n("k", "logic.toggle", 2, 0, in_action="toggle", in2_action="off",
            on_value="1", off_value="0"),
         _n("o", "gpio.out", 3, 0, gpio=264, action="from-payload")],
        [_e("b", "c"), _e("c", "k"), _e("k", "o")])


def pump_control():
    return _flow(
        "ex_pump", "A sensor that drives a pump",
        "The shape of every control loop: read, scale into real units, smooth "
        "the noise out, then decide with two thresholds so it cannot hunt. The "
        "level is written to a tag, which is how anything else on the board "
        "can see it without being wired here.",
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
         _n("p", "gpio.out", 4, 1, gpio=265, action="from-payload")],
        [_e("t", "r"), _e("r", "s"), _e("s", "m"), _e("m", "w"),
         _e("m", "h", from_side="bottom", to_side="top"), _e("h", "p")],
        tags=[{"name": "tank_level", "type": "number", "unit": "%",
               "desc": "How full the tank is, as a percentage"}])


def tag_decoupling():
    return _flow(
        "ex_tag_react", "React to a tag, from anywhere",
        "Nothing connects this to the flow that writes tank_level — the tag "
        "does. Add as many of these as you like and the measuring flow never "
        "changes, which is the whole reason a tag table exists.",
        [_n("t", "tag.change", 0, 0, tag="tank_level", when="rises"),
         _n("i", "logic.if", 1, 0, op=">=", value=95),
         _n("l", "log.write", 2, 0, level="warn",
            message="tank at {{payload}}% and still rising (was {{meta.previous}})"),
         _n("q", "log.write", 2, 1, level="idle",
            message="tank {{payload}}%")],
        [_e("t", "i"),
         _e("i", "l", from_port="true"),
         _e("i", "q", from_port="false", from_side="bottom", to_side="left")],
        tags=[{"name": "tank_level", "type": "number", "unit": "%",
               "desc": "How full the tank is, as a percentage"}])


def fill_heat_drain():
    return _flow(
        "ex_sequence", "Fill, heat, drain",
        "A sequence drawn as **Steps** wired *next → go*: fill until the tank "
        "is full, heat for ten minutes, drain until it is empty. The canvas "
        "highlights whichever step is active.\n\n"
        "One tank_level sensor feeds both conditions, and that is the point of "
        "a step: *full* reaches **done** on Fill, but Fill only moves on while "
        "it is the active step, so the same reading means nothing during Heat "
        "or Drain. Heat has no condition at all; **Leave after** moves it on.\n\n"
        "The Logs stand where the pump, heater and valve would go: start each "
        "one from **entered** and stop it from **left**. Wire Drain's next "
        "back to Fill's go and it runs for ever.",
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
        tags=[{"name": "tank_level", "type": "number", "unit": "%",
               "desc": "How full the tank is, as a percentage"}])


def door_left_open():
    return _flow(
        "ex_timer", "Only if it stays that way",
        "A door that flickers is not a door left open. The timer holds the "
        "message and throws it away if anything contradicts it before the "
        "time is up, so only a door genuinely open for thirty seconds gets "
        "through — and the light it turns on stays on for a minute after the "
        "last movement rather than snapping off.",
        [_n("d", "gpio.in", 0, 0, gpio=263, edges="both", debounce=50),
         _n("w", "logic.timer", 1, 0, mode="wait until true for", ms=30000),
         _n("t", "tag.set", 2, 0, tag="door_open", value="1"),
         _n("m", "gpio.in", 0, 1, gpio=266, edges="rising", debounce=100),
         _n("k", "logic.timer", 1, 1, mode="keep it true for", ms=60000),
         _n("o", "gpio.out", 2, 1, gpio=264, action="from-payload")],
        [_e("d", "w"), _e("w", "t"), _e("m", "k"), _e("k", "o")],
        tags=[{"name": "door_open", "type": "bool",
               "desc": "Set once the door has been open a while"}])


def overheat():
    return _flow(
        "ex_overheat", "Say something when the board is hot",
        "A threshold on a reading this board already takes. Throttle is what "
        "keeps it from saying the same thing every two seconds — it passes "
        "one message then ignores the rest for a while.",
        [_n("t", "metric.threshold", 0, 0, metric="cpu-thermal", op="above",
            value=70, rearm=60),
         _n("k", "logic.throttle", 1, 0, ms=300000),
         _n("w", "tag.set", 2, 0, tag="board_hot", value="1"),
         _n("l", "log.write", 3, 0, level="serious",
            message="{{meta.metric}} at {{payload}} C, over {{meta.threshold}}")],
        [_e("t", "k"), _e("k", "w"), _e("w", "l")],
        tags=[{"name": "board_hot", "type": "bool",
               "desc": "Set when the CPU passed its warning trip"}])


def count_and_act():
    return _flow(
        "ex_count", "Do something on the third time",
        "A counter with a target stays quiet until it gets there. The reset "
        "input has its own anchor on the top of the node, so the graph shows "
        "what clears it — here, a webhook you can call to start again.",
        [_n("b", "gpio.in", 0, 0, gpio=263, edges="rising", debounce=50),
         _n("r", "http.webhook", 0, 1, path="reset-count"),
         _n("c", "logic.count", 1, 0, step=1, start=0, target=3,
            auto_reset="reset"),
         _n("l", "log.write", 2, 0, level="ok",
            message="that is {{meta.count}} presses")],
        [_e("b", "c"),
         _e("r", "c", to_port="reset", from_side="right", to_side="top"),
         _e("c", "l")])


def webhook_to_pin():
    return _flow(
        "ex_hook", "Drive a pin from anywhere on the network",
        "POST to the path and the pin follows what you send. The If is doing "
        "the real work: it takes whatever arrived and routes it, so the same "
        "endpoint can turn something on and off.",
        [_n("h", "http.webhook", 0, 0, path="relay"),
         _n("i", "logic.if", 1, 0, op="truthy", value=""),
         _n("on", "gpio.out", 2, 0, gpio=264, action="high"),
         _n("off", "gpio.out", 2, 1, gpio=264, action="low")],
        [_e("h", "i"),
         _e("i", "on", from_port="true"),
         _e("i", "off", from_port="false", from_side="bottom", to_side="left")])


def camera_on_motion():
    return _flow(
        "ex_cam", "A camera that runs only when something happens",
        "For a field device. The camera is a switch: motion turns the sensor "
        "on, a timer turns it off again a while later, and Camera to screen "
        "is what puts the picture on the Cameras page. Nothing here runs "
        "because it exists — every node is doing so because something reached "
        "it.",
        [_n("m", "gpio.in", 0, 0, gpio=13, edges="rising", debounce=100),
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


def heartbeat():
    return _flow(
        "ex_heartbeat", "Heartbeat",
        "The first flow to put on a new board. It blinks the onboard LED so "
        "you can see it is alive across the room, counts the beats so a gap "
        "is obvious, and tells the host each time — which proves the radio, "
        "the enrolment and the token all work, not just that the board is "
        "powered.",
        [_n("t", "timer.interval", 0, 0, every=5000),
         _n("c", "logic.count", 1, 0, step=1, start=0, target=0),
         _n("led", "gpio.out", 2, 0, gpio=2, action="toggle"),
         _n("say", "host.notify", 2, 1, kind="heartbeat",
            payload="{{meta.count}}", level="ok")],
        [_e("t", "c"), _e("c", "led"),
         _e("c", "say", from_side="bottom", to_side="left")],
        board="esp32")


def heartbeat_watch():
    return _flow(
        "ex_heartbeat_watch", "Heartbeat watch",
        "The other half, on this board. It records each beat in a tag so the "
        "tag table shows the count climbing, and says which device sent it — "
        "so this one flow watches every board you put a heartbeat on.",
        [_n("t", "host.event", 0, 0, device="", kind="heartbeat"),
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
        "ex_fleet", "A device reports, this board answers",
        "Both directions in one picture. The device sends an event with Tell "
        "the host; this flow starts on it, records it in a tag, and sends a "
        "command straight back. Neither side holds the other's address.",
        [_n("t", "host.event", 0, 0, device="", kind="motion"),
         _n("w", "tag.set", 1, 0, tag="last_motion", value="{{meta.device}}"),
         _n("k", "logic.throttle", 2, 0, ms=10000),
         _n("c", "device.command", 3, 0, device="{{meta.device}}", op="camera-on",
            frame_size="QQVGA", format="greyscale"),
         _n("l", "log.write", 2, 1, level="ok",
            message="{{meta.device}} saw something")],
        [_e("t", "w"), _e("w", "k"), _e("k", "c"),
         _e("w", "l", from_side="bottom", to_side="left")],
        tags=[{"name": "last_motion", "type": "text",
               "desc": "Which device last reported motion"}])


def motor_bench():
    """One H-bridge pair, built out of general blocks rather than a motor
    node."""
    return _flow(
        "ex_motor_bench", "Motor bench",
        "The smallest flow that turns a wheel: one bridge, fired by hand. A "
        "speed from -1 to 1 becomes two PWM duties, and it stops on its own if "
        "the messages stop. Dead zone throws away the wobble around centre, "
        "Ramp stops it lurching, and the two Sign splits feed the two halves "
        "of the bridge so they can never fight. The Watchdog is the important "
        "one: it runs on the board, so the motor stops when the host reboots, "
        "the wifi drops or whatever was feeding it walks away. Fire it by "
        "hand to nudge the wheel; stop firing and it halts. **Capped at 60% "
        "duty** by the `scale` on the two Sign splits — a hand-fired flow with "
        "no arm switch has no business reaching full power. What actually "
        "arrives is 35%, and the gap is worth following once: 0.35 through the "
        "dead zone's rescaling is 0.293, and the splits map that onto their "
        "25–60% band as 25 + 0.293 x 35. Use "
        "**Motor drive** for both wheels, a live speed and an arm.",
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
         # D27 is the right motor's L_PWM, D25 its R_PWM.
         _n("lpwm", "pwm.out", 5, 0, gpio=27, action="start", mode="software",
            freq=1000, units="percent", duty="{{payload}}", channel=0),
         _n("rpwm", "pwm.out", 5, 1, gpio=25, action="start", mode="software",
            freq=1000, units="percent", duty="{{payload}}", channel=0),
         _n("dog", "safety.watchdog", 4, 2, timeout=400, value="0",
            recovery="no"),
         _n("stopl", "pwm.out", 5, 2, gpio=27, action="stop", mode="software",
            freq=1000, units="percent", duty="0", channel=0),
         _n("stopr", "pwm.out", 5, 3, gpio=25, action="stop", mode="software",
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
        "ex_motor_drive", "Motor drive — two wheels, live",
        "Set `drive_speed` between -1 and 1 in the tag table and the wheels "
        "follow it: its **sign is the direction**, and each wheel gets its own "
        "sign after steering is mixed in, so a Sign split decides forward or "
        "back per wheel rather than anything having to be told. **`drive_steer` "
        "is positive for right**, and it turns the same way in reverse as going "
        "forward. The two halves of a bridge can never both be driving, because "
        "one Sign split only passes a value above zero and the other only below "
        "it. "
        "Nothing moves until `drive_armed` is 1, and disarming it starves the "
        "Watchdog, which takes every duty to zero within 400ms and resets the "
        "Ramp so re-arming starts from a standstill rather than lurching back "
        "to speed. **The four Sign splits run between 25% and 100% duty** — "
        "`floor` is 25 because a geared wheel under load buzzes and warms "
        "below about a quarter duty instead of turning, and `scale` is 100, so "
        "full throttle is full power. Lower `scale` on all four is how you "
        "make it timid while you learn it. What this does NOT do is notice a stale command: the tick is local "
        "to the board, so if the host disappears while armed the last speed "
        "keeps being applied. Bench flow, wheels clear, disarm before you walk "
        "away — a real deadman needs the command to arrive as a stream, which "
        "is what the link is for.",
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
         # D27/D25 are the right bridge's two inputs, D33/D32 the left's.
         _n("rf", "math.split", 7, 0, part="forward only", full=1, scale=100,
            floor=25, invert="no", decimals=2),
         _n("rb", "math.split", 7, 1, part="back only", full=1, scale=100,
            floor=25, invert="no", decimals=2),
         _n("lf", "math.split", 7, 2, part="forward only", full=1, scale=100,
            floor=25, invert="no", decimals=2),
         _n("lb", "math.split", 7, 3, part="back only", full=1, scale=100,
            floor=25, invert="no", decimals=2),
         _n("p27", "pwm.out", 8, 0, gpio=27, action="start", mode="software",
            freq=1000, units="percent", duty="{{payload}}", channel=0),
         _n("p25", "pwm.out", 8, 1, gpio=25, action="start", mode="software",
            freq=1000, units="percent", duty="{{payload}}", channel=0),
         _n("p33", "pwm.out", 8, 2, gpio=33, action="start", mode="software",
            freq=1000, units="percent", duty="{{payload}}", channel=0),
         _n("p32", "pwm.out", 8, 3, gpio=32, action="start", mode="software",
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
        tags=[{"name": "drive_armed", "type": "number", "share": True,
               "boot_reset": True, "initial": 0,
               "desc": "1 lets the wheels turn. Anything else stops them."},
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
        "ex_motor_pad", "Motor drive — from a controller",
        "**Motor drive**, with the tags replaced by a gamepad. The right "
        "trigger is throttle and the left stick is steering; **A** arms and "
        "**B** disarms. Everything from the Dead zone down is the same chain, "
        "with "
        "the same 25–100% band and the same Watchdog.\n\n"
        "The **Controller** node opens the pad, so it sits at the head and "
        "everything after it reports what the pad last sent. Steering goes "
        "into the two Formulas as `{{pad.lx}}` rather than through a node, "
        "which is why there is no steer wire: the mixers read it at the moment "
        "they run.\n\n"
        "**This one has a deadman, and that is the point of it.** Controller "
        "axis emits *nothing* when the pad has not reported inside "
        "`stale_ms` — not zero, nothing — so the Watchdog starves and takes "
        "every duty to zero. Walk out of range, flatten the battery, turn it "
        "off mid-corner: the wheels stop on their own. The arm is a latch rather than a held button, so it "
        "survives a moment of interference; if you would rather the arm itself "
        "fell away, point one Controller button at *held* and wire it straight "
        "to the If, with no Toggle. Two buttons rather than one A that toggles, "
        "so pressing B always means stop, whatever state you think it is in."
        "\n\n**A board cannot hold a pad yet** — its Bluetooth transport is "
        "not written. Deployed today, the Controller node says so once and "
        "the wheels never move. On this host, pair a pad on the IoT screen's "
        "Bluetooth tab.",
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
         _n("p27", "pwm.out", 9, 0, gpio=27, action="start", mode="software",
            freq=1000, units="percent", duty="{{payload}}", channel=0),
         _n("p25", "pwm.out", 9, 1, gpio=25, action="start", mode="software",
            freq=1000, units="percent", duty="{{payload}}", channel=0),
         _n("p33", "pwm.out", 9, 2, gpio=33, action="start", mode="software",
            freq=1000, units="percent", duty="{{payload}}", channel=0),
         _n("p32", "pwm.out", 9, 3, gpio=32, action="start", mode="software",
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
        tags=[{"name": "drive_armed", "type": "number", "share": True,
               "boot_reset": True, "initial": 0,
               "desc": "1 lets the wheels turn. Anything else stops them."}])


def motor_arm():
    """The arm switch for `motor_drive`, joined to it by a tag and nothing
    else."""
    return _flow(
        "ex_motor_arm", "Arm before it drives",
        "A Latch between you and the wheels, writing the `drive_armed` tag that "
        "**Motor drive** reads. Nothing moves until Arm is fired, Disarm drops "
        "it at once, and a Watchdog on the reset input drops it the moment the "
        "feed stops — so after a dropout the drive does not come back on its own "
        "when the signal returns. It has to be armed again on purpose, which is "
        "the difference between a machine that recovers and one that lunges. "
        "Nothing wires these two flows together: the tag does, which is why the "
        "arm can live here and the drive can live on the board.",
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
        tags=[{"name": "drive_armed", "type": "number", "share": True,
               "boot_reset": True, "initial": 0,
               "desc": "1 lets the wheels turn. Anything else stops them."}])


CATALOGUE = [blink, button_latch, webhook_to_pin, count_and_act,
             door_left_open, overheat, pump_control, tag_decoupling,
             fill_heat_drain, heartbeat, heartbeat_watch, camera_on_motion, device_round_trip,
             motor_bench, motor_drive, motor_pad, motor_arm]


def catalogue():
    """Every example, built fresh. Host-only ones first, then device flows."""
    out = [fn() for fn in CATALOGUE]
    for ex in out:
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
