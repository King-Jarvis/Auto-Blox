#!/usr/bin/env bash
# Grant this user access to the GPIO character devices.
#
#   sudo ./setup-gpio.sh
#
# /dev/gpiochip* ships root-only, so the console cannot read or drive any pin
# until a group owns them. This creates a `gpio` group, hands it the chips via
# udev, and adds the invoking user. Log out and back in afterwards (or run
# `newgrp gpio`) for the new group to apply to your session.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "run me with sudo: sudo $0" >&2
  exit 1
fi

TARGET_USER="${SUDO_USER:-$(logname 2>/dev/null || echo root)}"
RULE=/etc/udev/rules.d/99-gpio-console.rules

getent group gpio >/dev/null || { groupadd -r gpio; echo "created group: gpio"; }
getent group i2c  >/dev/null || { groupadd -r i2c;  echo "created group: i2c"; }

cat > "$RULE" <<'RULES'
# Auto-Blox: let the gpio group use the GPIO character devices.
SUBSYSTEM=="gpio", KERNEL=="gpiochip[0-9]*", GROUP="gpio", MODE="0660"
# I2C character devices, for the I2C flow nodes.
SUBSYSTEM=="i2c-dev", KERNEL=="i2c-[0-9]*", GROUP="i2c", MODE="0660"
# Hardware PWM sysfs, so a pwm channel can be exported without root.
SUBSYSTEM=="pwm", ACTION=="add", \
  RUN+="/bin/sh -c 'chgrp -R gpio /sys%p; chmod -R g=u /sys%p'"
RULES
echo "wrote $RULE"

usermod -aG gpio,i2c "$TARGET_USER"
echo "added $TARGET_USER to groups: gpio, i2c"

udevadm control --reload-rules
udevadm trigger --subsystem-match=gpio
udevadm trigger --subsystem-match=i2c-dev
udevadm trigger --subsystem-match=pwm
# udev does not always relabel already-created nodes; do it directly too.
chgrp gpio /dev/gpiochip* 2>/dev/null || true
chmod 660  /dev/gpiochip* 2>/dev/null || true
chgrp i2c  /dev/i2c-*     2>/dev/null || true
chmod 660  /dev/i2c-*     2>/dev/null || true
if [ -d /sys/class/pwm/pwmchip0 ]; then
  chgrp -R gpio /sys/class/pwm/pwmchip0/ 2>/dev/null || true
  chmod -R g=u  /sys/class/pwm/pwmchip0/ 2>/dev/null || true
fi

echo
ls -l /dev/gpiochip* /dev/i2c-* 2>/dev/null
echo
echo "Now log out and back in (or run: newgrp gpio), then check with:"
echo "  gpiodetect"
