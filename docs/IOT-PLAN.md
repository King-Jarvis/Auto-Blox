# IOT — plan for a device fleet run from this board

*Shape of the whole system: `docs/ARCHITECTURE.md`.*

The extension: this board becomes a **host** for small wireless devices that run
a tiny server pulled from this repo. A new `IOT` screen manages the wireless
network they join, tracks which board each device is, and binds a device to a
flow so Flow Studio knows what hardware it is talking to.

Everything in §1 was probed on this machine on 2026-09-19. §2 onward is the
design as planned, with **As built** notes where the result differs;
`ARCHITECTURE.md` and `CONTEXT.md` describe what exists now.

---

## 1. What the hardware actually allows (verified 2026-09-19)

| Fact | Consequence for this plan |
|---|---|
| `wlan0` is a **UNISOC/Spreadtrum `sprdwl_ng`** radio, `phy0` | Out-of-tree driver. Treat every wireless feature as "verify on the metal", not "the docs say so". |
| Supported modes: `IBSS, managed, AP, P2P-client, P2P-GO, P2P-device` | **AP mode exists.** The core idea works. |
| `valid interface combinations: #{ managed, AP } <= 1` | **The radio is an AP or a client, never both.** Fine here — the uplink is wired ethernet (the default route), so `wlan0` can be a dedicated IoT AP. But it means the board cannot join a WiFi network while hosting devices. |
| Band 1 has 14 channels; band 2 (5 GHz) is present | ESP32 and every ESP32-S3/C3 are **2.4 GHz only**. The AP must run on band 1 — channel 1, 6 or 11. A 5 GHz AP would be invisible to the fleet. |
| `phy0` is **soft-blocked in rfkill**, `wlan0` DOWN, NM reports WiFi disabled | First step of any AP work is unblocking the radio. Nothing wireless works until then. |
| `nmcli general permissions` for the console user: `enable-disable-wifi: no`, `wifi.share.protected: no`, everything else `auth` | **The console cannot configure wireless through NetworkManager.** It runs as a non-seat service user; polkit denies it outright or demands interactive auth. This is the single biggest design constraint — see §3. |
| `hostapd` **not installed**; `dnsmasq` installed; `iw`, `wpa_supplicant`, `bridge`, `iptables` present; `nft` absent | One `apt install hostapd` (candidate `2:2.11`) away. All other pieces are already here. |
| `8021q` module available (not loaded); `ip_forward = 0` | VLAN is possible; routing and NAT need enabling. Both privileged. |
| `/etc/netplan/` holds `30-wifis-dhcp.yaml` + NM-rendered files, and is **root-only readable** | Two config owners for `wlan0`: netplan and NetworkManager. A third (hostapd) will fight both unless `wlan0` is explicitly handed over. |
| `esptool 4.7`, `python3-serial`, `micropython 1.26` all in apt | Flashing a device from this board is supported, one install away. |
| No `/dev/ttyUSB*` or `/dev/ttyACM*`; a CH340-vendor USB hub is present with the ethernet adapter | No microcontroller is plugged in yet. There is a free hub port for one. |
| `sudo` requires a password | The console can never do any of the privileged steps itself. Every one is handed over as a single command, exactly as `CONTEXT.md` §1 requires. |

### Two things this plan cannot deliver as literally described

- **Per-device VLANs on the WiFi side.** Dynamic VLAN assignment needs hostapd
  with RADIUS, or one AP interface per VLAN. This radio allows **one** AP
  interface. What is achievable: **one SSID → one isolated subnet**, tagged onto
  the wired uplink as a VLAN if there is a managed switch upstream, plus station
  isolation and firewall separation from the LAN. Multiple isolated IoT networks
  would need a second radio (a USB WiFi dongle is the cheap fix — and it would
  also let the board stay a WiFi client).
- **An Arduino Nano running this.** The classic Nano is an ATmega328P: 2 KB SRAM,
  32 KB flash, no radio. It cannot hold a network stack, let alone fetch modules
  over HTTP. It can be a *peripheral* behind an ESP32 over UART or I2C. The Nano
  ESP32, ESP32-S3, C3 and C6 are all fine. The Nano 33 IoT (SAMD21 + NINA) would
  need a completely different agent in C and is out of scope for v1.

---

## 2. Shape of the thing

Four pieces, in dependency order:

```
        ┌─────────────────────────── this board (host) ──────────────────────────┐
        │                                                                        │
        │  /iot screen ── iot.py ── netctl helper (root) ── hostapd + dnsmasq     │
        │      │            │                                    │               │
        │      │            ├── device registry (iot.json)       └── wlan0 AP    │
        │      │            ├── board profiles (data/boards/*)        2.4 GHz    │
        │      │            └── agent source + manifest                │         │
        │      │                                                       │         │
        │  /flows ── flows.py ── device-bound nodes ─────────────────┐  │         │
        └────────────────────────────────────────────────────────────┼──┼─────────┘
                                                                     │  │
                                    ┌────────────────────────────────┴──┴──┐
                                    │  ESP32-class device                  │
                                    │  boot.py + agent.py (loader)         │
                                    │  + only the modules its flow needs   │
                                    └──────────────────────────────────────┘
```

**The agent is a puller, not a server.** The device holds a loader that knows one
URL. It asks the host what it should be, downloads only those modules, then
holds one long-lived HTTP connection for commands and posts events back. No
broker, no MQTT stack on the device, no new service on the host — it reuses the
token auth, the SSE bus and the flow engine that already exist. MQTT stays what
it is today: how *this board* talks to someone else's broker.

---

## 3. The privilege decision (make this one first)

The console cannot touch wireless config. Three ways out:

| Option | Cost | Verdict |
|---|---|---|
| polkit rule granting the console user the NM wifi actions | one file in `/etc/polkit-1/rules.d/`; NM keeps owning `wlan0` | Fights netplan; NM's hotspot mode is a black box on this driver; grants broad NM power |
| Narrow `sudoers` NOPASSWD entry for **one root-owned helper** with a fixed verb set | one file in `/etc/sudoers.d/`, one script in `packaging/` | **Recommended.** Smallest auditable surface, no NM involvement, testable offline |
| Run the console as root | nothing to install | No. It already exposes a shell; root would make that fatal |

**Recommended: `packaging/iot-netctl`,** root-owned `0755`, invoked as
`sudo -n /usr/local/sbin/iot-netctl <verb>`. Verbs: `up`, `down`, `status`,
`clients`, `reload`. It takes **no free-form arguments** — it reads
`/etc/zero2w-console/iot-net.json`, which the console writes and the helper
**re-validates** (SSID charset and length, PSK 8–63 chars, channel in
`{1,6,11}`, CIDR inside RFC1918, interface name against a whitelist). Nothing
from that file is ever interpolated into a shell command; hostapd and dnsmasq
configs are generated by writing files, then the units are restarted by name.

This matters because the token in front of `/api/exec` is already the only thing
between the LAN and a shell as the console user. A NOPASSWD verb set keeps the new
privilege bounded to "bring an AP up or down"; a NOPASSWD shell would not.

**As built:** the console's `NoNewPrivileges=yes` makes every `sudo` from it
fail, so the helper runs as its own root service (`zero2w-iotnet`) listening on
`/run/zero2w-iot/ctl.sock`, with nine verbs; the sudoers file is only a
fallback.

---

## 4. Phases

### Phase 0 — prerequisites (one sitting, mostly the user's sudo)  ✅ **done**

1. ~~Land the **flow document version check** first.~~ **Done.** Flows are
   about to carry device bindings, and an overwrite that drops a binding does
   not just lose text — it points a flow at the wrong hardware. The document now
   carries a `rev`; a POST naming a stale one gets 409 plus the current
   document, and the editor offers *Load theirs* / *Keep mine*. A POST with no
   `rev` is still trusted, so restoring a backup file works unchanged.
2. `sudo apt install hostapd esptool python3-serial` — and mask hostapd's own
   unit (`sudo systemctl mask hostapd`), because the helper drives an instance
   with its own config file.
3. `sudo usermod -aG dialout <user>` (install.sh does this) (flashing over USB), then a re-login — the
   supplementary-group trap in `CONTEXT.md` applies to the console service too.
4. Decide VLAN vs routed subnet (§7 question 2). It changes the helper's
   network template, nothing else.

**Done when:** the repo has a version-checked flow save with a test, and the
packages are present.

### Phase 1 — the IOT screen, read-only  ✅ **done**

A third page, `/iot`, alongside `/` and `/flows`; one more entry in
`buildDock()` in `app.js` (the dock is a plain list — `{ label, href, glyph }`).
New module `zero2w_console/iot.py` with the same shape as `buses.py`: a
`capabilities()` that reports honestly what is missing and the exact command
that fixes it — radio soft-blocked, hostapd absent, helper not installed, no
devices enrolled.

Widgets: **Radio** (phy, modes, the AP-or-client constraint stated in words),
**Network** (SSID, channel, subnet, clients — empty until Phase 2), **Devices**
(a card index like the flow index: board, IP, RSSI, uptime, last seen, assigned
flow), **Enrollment** (a window that can be opened and closed).

Routes: `GET /api/iot/status`, `GET /api/iot/devices`. Everything read-only, no
privilege, no new risk. Ships something real on day one and makes the constraints
visible instead of surprising.

**Done when:** `/iot` renders on the phone and on the desktop, says exactly what
is not yet possible and how to enable it, and lists zero devices without
looking broken.

### Phase 2 — the network  ✅ **done — see docs/RADIO.md for what it took**

`packaging/iot-netctl` + `packaging/zero2w-iot.sudoers` + an install note in the
README. Config written by the console to `/etc/zero2w-console/iot-net.json`;
templates for `hostapd.conf` (WPA2-PSK/CCMP only — WPA3/SAE is not worth
gambling on with this driver, and ESP32 support for it is patchy;
`ap_isolate=1`; channel 1/6/11; `country_code` set) and `dnsmasq` (its own
instance, bound to `wlan0` only, a small DHCP pool, the host as gateway and DNS).

Isolation is the point: forwarding on, a NAT rule for the IoT subnet toward the
uplink, and firewall rules that let the IoT subnet reach **the host's console
port and the internet, and nothing else on the LAN**. If the upstream switch is
VLAN-capable, the AP bridges to `<uplink>.<vid>` instead and the isolation
is the switch's job.

UI: SSID, passphrase, channel, subnet, an up/down switch, live client list with
MAC, IP, RSSI and which device it maps to. The Radio widget carries its own
on/off switch, which goes straight to `/dev/rfkill` when logind's ACL allows it
and through the helper when it does not.

**Traps to expect here** (all already paid for once elsewhere on this board):
handing `wlan0` to hostapd means telling NM to leave it alone
(`nmcli device set wlan0 managed no`, persisted) *and* checking netplan is not
re-rendering it; the radio must be rfkill-unblocked before hostapd starts; and
`sprdwl_ng` may refuse a channel width or a country code that hostapd accepts —
hostapd's own log is the only honest source.

**Done when:** a phone can join the SSID, get a lease, load the console, and
*fail* to reach anything else on the LAN.

**Where it stands:** `packaging/iot-netctl` and `packaging/zero2w-iot.sudoers`
exist, with 18 tests over the validator, the generated hostapd and dnsmasq
configs, the command surface and the sudoers entry. The console writes the
network through `POST /api/iot/network` and starts or stops it through
`/api/iot/network/{up,down}`, and the IOT screen has the form. **None of it is
installed** — that needs the four sudo commands in the README, and until they
run the page shows the buttons disabled and says why. Nothing about AP mode on
this driver has been proven on the metal yet; that is the next thing to do, and
risk 1 below is still open.

### The workflow this is built around

Stated by the person who will use it, and it is the contract the phases below
follow:

> Plug in a dev board, **the scan determines what board it is**. Make a config
> for it in the IOT screen, then flash. In Flow Studio, **see the various boards**
> and build flows against their pinout and peripherals, **link a flow to an IOT
> config**, and **flash that flow wirelessly** — with two-way communication for
> control of that field device.

Two things in that sentence decide the architecture.

**Detection comes before configuration.** Nothing is typed in that the board can
be asked. The scan reports the chip, its flash, its PSRAM and its MAC; the
config is created *from* that answer, not validated against it afterwards.

**"Flash that flow wirelessly" means the device runs the flow.** Not the host
driving pins over WiFi — a field device that keeps working when the host reboots
or the link drops. That is the right call for the job, and it costs: the flow
engine needs a second implementation in MicroPython, and the node registry gains
a `runs` field (`host`, `device`, or `both`). A flow bound to a board may only
use nodes the board can run, which the editor enforces the same way it already
enforces pins. Nodes that read the journal, run a shell command or report host
metrics stay host-only, and the palette says so.

Three objects, deliberately separate:

| Object | What it is | Lives in |
|---|---|---|
| **Board profile** | a chip's pinout and peripherals — static, generated | `data/boards/*.json` |
| **Device config** | one physical board: name, profile, network, token, deployed flow | `~/.config/zero2w-console/iot.json` |
| **Flow** | a graph authored **for a profile** (`board`), deployed **to a config** (`device`) | `flows.json` |

One flow written for an ESP32-S3 can be deployed to any S3 you own. That is why
the flow names a *profile* at design time and a *device* at deploy time.

### Phase 3 — scan, configure, flash  ✅ **done, on real hardware**

**Scan** (`GET /api/iot/scan`) walks `/dev/ttyUSB*` and `/dev/ttyACM*`, and for
each one asks esptool what it is — chip, revision, flash size, PSRAM, MAC. The
USB bridge drivers (`ch341`, `cp210x`, `ftdi_sio`, `cdc_acm`) are all present on
this kernel, and the console user is in `dialout`, so nothing needs installing but
esptool itself.

**Configure.** A scan result becomes a device config on the IOT screen: a name, a
role, the profile the scan matched, and the network it should join. It is a
record, not a device — the board does not exist to the console until it enrolls.

**Flash** (`scripts/iot-flash.py`, driven from that screen): fetch the right
MicroPython build for the detected chip (cached under `~/.cache`), `erase_flash`,
`write_flash`, then write `boot.py` over the REPL carrying the SSID, the host
URL, the config's id and a **one-time enrollment token**. Progress streams to the
page; esptool is slow and silence looks like a hang.

**Done when:** an unknown board is plugged in, the screen names it, and two
clicks later it is flashed and waiting to join.

### Phase 4 — board profiles  ✅ **done — esp32, s3, c3, cam**

`zero2w_console/data/boards/*.json`, one per chip, in the same spirit as the
existing pinout: **generated, never hand-typed**, each carrying the vendor
document it came from. `scripts/build_boards.py` generates them and asserts the
invariants; `tests/` re-checks every one.

Each pin carries its capability set — `in`, `out`, `pwm`, `adc1`, `adc2`,
`touch`, `dac`, `rtc` — plus `usable` and a note when something is true of it
that will otherwise cost an evening:

- **GPIO 6–11 are SPI flash** on classic ESP32 and are not brought out.
- **34–39 are input-only** and have no pull resistors.
- **0, 2, 12, 15 are strapping pins** — holding one at boot changes how the chip
  starts.
- **ADC2 cannot be read while WiFi is on.** Every ADC2 pin says so.
- Only **RTC-capable** pins wake the chip from deep sleep.
- **S3 and C3 renumber everything**, and the S3's 33–37 belong to octal PSRAM on
  `-R2`/`-R8` modules but are free on plain ones — so that one is confirmed by
  the device's own probe rather than assumed.

The profile is **claimed** by the config and **checked** against what the device
reports at enrollment. A mismatch is shown as a mismatch.

**Done when:** two different chips are configured at once and each offers only
its own legal pins.

### Phase 5 — Flow Studio, and deploying a flow to a device  ✅ **done; the agent runs on a board**

- **Two new keys on a flow:** `board` (a profile — what it was written for) and
  `device` (a config — where it runs). Flows are `{id, name, enabled, nodes,
  edges}` today, so both are additive and old flows keep working as host flows.
- **The flow index gains a board picker.** Choosing one filters the palette to
  nodes that board can run, and every pin dropdown to pins that board has, with
  the illegal ones disabled and labelled — the same treatment reserved host pins
  already get. The editor already builds pin dropdowns from one array and the
  palette from one registry, so this is a substitution, not a rewrite.
- **Deploy** (`POST /api/iot/devices/<id>/deploy`) hands the device the flow as
  JSON. It writes it to flash and runs it from boot, so the field device keeps
  working through a host reboot or a dropped link.
- **Two-way, over the connection the device already holds open.** Up: node
  firings, pin edges, sensor readings and errors, landing on the same SSE bus the
  dashboard uses, so the studio's run log shows a remote flow exactly as it shows
  a local one. Down: manual triggers, pin overrides, enable/disable, redeploy.
- **New node types**, each a registry entry plus a branch in both engines:
  `iot.event`, `iot.gpio`, `iot.pwm`, `iot.read`, `iot.status`. The last one
  fires when a device goes quiet, which in a fleet is the alert that matters.
- **New variables**: `device.id`, `device.board`, `device.ip`, `device.rssi`,
  `device.uptime`, `device.last_seen`.

**As built:** the ordinary GPIO, PWM and I2C nodes run on a device instead of
`iot.*` copies; a device talks to the host through **Tell the host** and
**Device event**, and is told things by **Device command**. There is no
went-quiet trigger — `{{device.online}}` answers it. The variable is
`device.seen`, not `device.last_seen`. The device polls rather than holding a
connection open, unless it is switched onto the link.

**Done when:** a flow is drawn against a profile, deployed wirelessly to a board
on the bench, keeps running after the host is rebooted, and its firings still
appear in the studio's log when the host comes back.

### Phase 6 — later, deliberately  ⏳ **partly done**

Camera streaming (MJPEG from an ESP32-CAM, PSRAM required, and a widget that can
survive a 4×A53 decoding nothing); a **Linux** device class so a Pi 5 or CM4
speaks the same protocol with a CPython agent — that is where "PCIe slots"
belongs, since no MCU has PCIe; OTA agent updates by manifest sha; a mosquitto
broker for third-party MQTT gear; a second radio for real per-network VLANs.

**As built:** the camera works, as RGB565 frames the host turns into PNG,
because the OV3660 will not produce JPEG on this build; agent updates go over
the air by manifest sha. The Linux device class, the broker and the second
radio are not started.

---

## 5. What gets added to the repo

```
zero2w_console/iot.py               device registry, AP state, helper calls
zero2w_console/agent/               MicroPython loader + feature modules
zero2w_console/data/boards/*.json   GENERATED board profiles
zero2w_console/static/iot.html|.js|.css
scripts/build_boards.py             profile generator
scripts/iot-flash.py                flash + provision over USB
packaging/iot-netctl                root helper, fixed verb set
packaging/zero2w-iot.sudoers        the one NOPASSWD line
tests/test_iot.py                   profiles, manifest hashes, config validation
docs/IOT-PLAN.md                    this file
```

State lives beside the existing files: `~/.config/zero2w-console/iot.json`
(devices and their tokens, `0600`) and `/etc/zero2w-console/iot-net.json` (the
network config the helper validates). Nothing in the repo holds a secret.

---

## 6. Risks, ranked

1. ~~**`sprdwl_ng` AP mode may be unreliable in practice.**~~ **Settled.** It
   works, but only when nothing else is holding the radio — the firmware
   asserts if an AP channel is opened alongside a live station claim. See
   `docs/RADIO.md`, which also records the four paths that were not the answer.
2. **One radio, one role.** Turning the IoT AP on ends any chance of the board
   joining a WiFi network. Acceptable while the uplink is USB ethernet; it should
   be stated on screen, not buried.
3. **VLAN may have nowhere to go.** Tagging is pointless without a managed switch
   upstream. The routed-subnet-plus-firewall design works either way, which is
   why it is the default.
4. **Thermals.** No cooling. An AP, a DHCP server, a fleet of pollers and a
   browser on four A53s is more than this board does today. The long-poll design
   is chosen partly for this: one idle socket per device costs almost nothing,
   while per-second polling would not.
5. **Flow save races.** Already real, now with hardware consequences. Hence
   Phase 0.
6. **A device is a credential on a wall.** Per-device tokens, a closed
   enrollment window by default, and an IoT subnet that cannot reach the LAN.

---

## 7. Open questions — with my recommendation, so nothing blocks

1. **Which device do you actually have, or want to buy?** Recommendation: an
   **ESP32-S3 devkit** (native USB, plenty of RAM, no ADC2 problem) as the
   reference board, plus a **C3** as the cheap second board to prove profiles
   work. Avoid the classic Nano entirely; if you own one, it can hang off an
   ESP32 later.
2. **Is the switch between this board and the ISP VLAN-capable?** If unsure, I
   build the routed isolated subnet — it works on any network and the VLAN
   template is a later drop-in.
3. **MicroPython or C?** Recommendation: **MicroPython.** "Pull only what it
   needs when it needs it" is trivially true of `.py` modules over HTTP and
   nearly impossible with a compiled image, which is the whole design. C is
   faster and smaller; nothing here needs either.

**As built:** MicroPython on a routed subnet, as recommended; the fleet is a
classic ESP32 and an ESP32-CAM rather than an S3 and a C3.
