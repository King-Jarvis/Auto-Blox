# The wifi chip, and what it actually needed

*Shape of the whole system: `docs/ARCHITECTURE.md`. Read this one before
touching anything that brings the radio up.*

`wlan0` on this board is a UNISOC **uwe5622** ("Marlin3") wifi/Bluetooth combo
on SDIO, driven by the out-of-tree `sprdwl_ng` with `uwe5622_bsp_sdio`
underneath it. The firmware blob is `/lib/firmware/uwe5622/wcnmodem.bin`.

**It hosts an access point.** The console runs one on it, an ESP32 holds a
lease on it, and frames come back over it. This file exists because getting
there took most of a day down four wrong paths, and each of them looked
plausible at the time.

---

## The failure

Starting hostapd killed the radio:

```
wlan0: interface state UNINITIALIZED->COUNTRY_UPDATE
Failed to set beacon parameters
Interface initialization failed
wlan0: Unable to setup interface.
```

and, in the kernel log:

```
WCN: mdbg_assert_read:WCN Assert in mchn.c line 41, exp=0 info=[bus_chn_init, chn0, hif_type0]
WCN_ERR: chip reset & notify every subsystem...
WCN: wcn chip start power off!
```

hostapd's message is the *consequence*: by the time it complains, the chip has
reset itself out from under it. `mchn.c` is UNISOC's bus-channel layer and
`bus_chn_init` is it opening a data channel.

## The cause

**The chip has one bus channel to give, and something else already had it.**

Three things claim this radio on a stock Armbian desktop image, all of them
before anything of ours runs:

- NetworkManager, which manages `wlan0` unless told otherwise;
- the system `wpa_supplicant`, started at boot as a service;
- a **P2P device** that supplicant creates and never releases — visible as an
  unnamed `type P2P-device` wdev in `iw dev`.

Taking `wlan0` off NetworkManager is not enough, because the supplicant and its
P2P device keep their claim on the chip regardless. Opening an AP channel
alongside a live station claim is what the firmware cannot survive.

## The fix

Before hostapd starts, the radio is taken properly (`release_radio` in
`packaging/iot-netctl`):

1. `rfkill unblock wifi`
2. `systemctl stop wpa_supplicant` — remembered, and started again on `down`
3. `nmcli device set wlan0 managed no`
4. every `type P2P-device` wdev deleted with `iw wdev <id> del`
5. the interface set back to `type managed`, then down, then hostapd's

That is the whole change. No firmware swap, no kernel change, no different
tooling.

---

## Four things that were not the problem

Kept so they are not tried again.

**`country_code` and `ieee80211n`.** `COUNTRY_UPDATE` is the state hostapd sits
in while it waits, not where it fails. Removing both changed nothing, on
channel 1 and on channel 6. The fallback that used to drop them has been
deleted.

**wpa_supplicant's AP mode.** Not compiled into the Debian build at all — the
binary contains no `AP-ENABLED` string. `mode=2` was never an option here.

**A separate `ap0` interface.** `iw phy phy0 info` lists **no** software
interface modes, so the driver cannot add one. There is nothing to run hostapd
on but `wlan0` itself.

**A Wi-Fi Direct group owner.** The driver advertises `P2P-GO`, and a group
owner is an ordinary WPA2 access point from the outside — a genuinely different
path through the same firmware. It failed with
`nl80211: Could not set interface 'p2p-dev-wlan0' UP`, because the driver
allows `#{P2P-device} <= 1` and NetworkManager's supplicant already held it.
Our own attempts then leaked more P2P devices, which `iw wdev del` would not
remove while the driver was loaded, so each retry failed for a different reason
than the last. On a clean slate after a reboot, the attempt coincided with the
board resetting — and with `/var/log` on zram, the evidence went with it. That
code is removed.

## Things worth knowing about this radio

- **AP or station, never both**: `#{ managed, AP } <= 1`. Hosting the IoT
  network means this board cannot also join a wifi network. A USB adapter lifts
  that, and `iot-netctl` will use it without any other change.
- **2.4 GHz only for the fleet.** The radio has 25 usable 5 GHz channels; no
  ESP32 can see any of them. Channels are restricted to 1, 6 and 11.
- **A failed attempt leaves the interface in `type AP`**, and nothing resets
  it. A radio in AP mode cannot scan — that is why a board once reported zero
  networks in range with 39 of them there. `down` now leaves a station behind.
- **The firmware asserting takes Bluetooth with it.** They are the same chip.
- **An active LE scan does not appear to disturb a running AP.** Open until
  2026-09-26, when a ~45 second `bluetoothctl` discovery session plus a pairing
  attempt ran while `wlan0` hosted the IoT network: zero WCN asserts, `wlan0`
  still `type AP` with its address, and the ESP32-Cam still reporting every
  second through it. One observation is not a guarantee, so the console still
  treats a scan as a deliberate act bounded by `--timeout`.
- **`/var/log` is zram**, so a reset destroys the journal. `kern.log` under
  `/var/log.hdd` survives, and needs `adm` to read.
