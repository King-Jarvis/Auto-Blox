#!/usr/bin/env bash
# Install the console on an Orange Pi Zero 2W, for the user who runs this.
#
#   sudo scripts/install.sh                 the console service, GPIO/I2C access
#   sudo scripts/install.sh --iot           ... and the access point helper
#   sudo scripts/install.sh --desktop       ... and a desktop launcher
#   sudo scripts/install.sh --mpy           ... and build mpy-cross, so boards
#                                           get compiled code (a few minutes)
#   scripts/install.sh --render DIR         only write the filled-in units to DIR
#
# The console runs from this checkout, as the user who invoked sudo. Move the
# checkout and run this again. Nothing here is specific to one machine: the
# user, their group and this directory are filled into the templates in
# packaging/ when they are installed.
set -euo pipefail

APPDIR="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
TARGET_USER="${SUDO_USER:-}"
IOT=0 DESKTOP=0 MPY=0 APT=1 RENDER=""

usage() { sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --iot) IOT=1 ;;
    --desktop) DESKTOP=1 ;;
    --mpy) MPY=1 ;;
    --no-apt) APT=0 ;;
    --user) TARGET_USER="$2"; shift ;;
    --render) RENDER="$2"; shift ;;
    -h|--help) usage ;;
    *) echo "unknown option: $1" >&2; usage 1 ;;
  esac
  shift
done

if [[ -z "$TARGET_USER" ]]; then
  if [[ -n "$RENDER" ]]; then TARGET_USER="$(id -un)"; else
    echo "run me with sudo from the user who will own the console: sudo $0" >&2
    exit 1
  fi
fi
if [[ "$TARGET_USER" == root ]]; then
  echo "the console should not run as root; run this with sudo from your own user" >&2
  exit 1
fi
TARGET_GROUP="$(id -gn "$TARGET_USER")"
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"

render() {  # template -> stdout, with this machine's values
  local esc_dir=${APPDIR//\\/\\\\}; esc_dir=${esc_dir//&/\\&}; esc_dir=${esc_dir//|/\\|}
  sed -e '/^# A template:/d' -e "s|@USER@|$TARGET_USER|g" \
      -e "s|@GROUP@|$TARGET_GROUP|g" -e "s|@APPDIR@|$esc_dir|g" "$1"
}

if [[ -n "$RENDER" ]]; then
  mkdir -p "$RENDER"
  for f in zero2w-console.service zero2w-iotnet.service zero2w-console.desktop zero2w-iot.sudoers; do
    render "$APPDIR/packaging/$f" > "$RENDER/$f"
  done
  echo "rendered for $TARGET_USER:$TARGET_GROUP at $APPDIR into $RENDER"
  exit 0
fi

if [[ $EUID -ne 0 ]]; then
  echo "run me with sudo: sudo $0 $*" >&2
  exit 1
fi

echo "installing the console for $TARGET_USER, running from $APPDIR"

# -- packages ------------------------------------------------------------------
if [[ $APT == 1 ]]; then
  pkgs=(python3 bluez libglib2.0-bin gpiod esptool curl openssl)
  [[ $IOT == 1 ]] && pkgs+=(hostapd dnsmasq-base iw rfkill iptables)
  [[ $MPY == 1 ]] && pkgs+=(git build-essential)
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${pkgs[@]}"
fi

# -- device access ---------------------------------------------------------------
# gpio and i2c groups, and the udev rule that hands them the devices.
SUDO_USER="$TARGET_USER" bash "$APPDIR/scripts/setup-gpio.sh"
# journal: the log widget · input: controllers · bluetooth: BlueZ · dialout: flashing
for g in systemd-journal input bluetooth dialout; do
  if getent group "$g" >/dev/null; then
    usermod -aG "$g" "$TARGET_USER"
  fi
done

# -- the console service -------------------------------------------------------
render "$APPDIR/packaging/zero2w-console.service" > /etc/systemd/system/zero2w-console.service
systemctl daemon-reload
systemctl enable zero2w-console >/dev/null
systemctl restart zero2w-console
echo "installed zero2w-console.service"

# -- the access point helper -------------------------------------------------------
if [[ $IOT == 1 ]]; then
  # The helper runs its own hostapd against a generated config; the packaged
  # unit would fight it for wlan0.
  systemctl mask hostapd >/dev/null 2>&1 || true
  install -m 0755 -o root -g root "$APPDIR/packaging/iot-netctl" /usr/local/sbin/iot-netctl
  render "$APPDIR/packaging/zero2w-iotnet.service" > /etc/systemd/system/zero2w-iotnet.service
  systemctl daemon-reload
  systemctl enable zero2w-iotnet >/dev/null
  systemctl restart zero2w-iotnet
  echo "installed iot-netctl and zero2w-iotnet.service"
fi

# -- the device compiler -----------------------------------------------------------
if [[ $MPY == 1 ]]; then
  # Built as the user, into their ~/.cache, where the console looks for it.
  sudo -u "$TARGET_USER" -H sh "$APPDIR/scripts/build-mpy-cross.sh"
  echo "built mpy-cross"
fi

# -- the desktop launcher ----------------------------------------------------------
if [[ $DESKTOP == 1 ]]; then
  apps="$TARGET_HOME/.local/share/applications"
  install -d -o "$TARGET_USER" -g "$TARGET_GROUP" "$apps"
  render "$APPDIR/packaging/zero2w-console.desktop" > "$apps/zero2w-console.desktop"
  chown "$TARGET_USER:$TARGET_GROUP" "$apps/zero2w-console.desktop"
  echo "installed the desktop launcher"
fi

cat <<DONE

Done. Log out and back in once, so $TARGET_USER picks up the new groups.

The console is at http://$(hostname).local:8787/ (or this board's IP address).
Its token is created on first start, in $TARGET_HOME/.config/zero2w-console/token;
open the page once as http://<address>:8787/?t=<token> and it remembers you.
DONE
