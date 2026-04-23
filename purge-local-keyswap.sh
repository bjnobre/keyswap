#!/usr/bin/env bash
set -Eeuo pipefail

APP_NAME="keyswap"
USER_SERVICE="${HOME}/.config/systemd/user/${APP_NAME}.service"
UDEV_RULE="/etc/udev/rules.d/70-${APP_NAME}.rules"

echo "Stopping and disabling user service if present..."
systemctl --user disable --now "${APP_NAME}.service" 2>/dev/null || true
systemctl --user reset-failed "${APP_NAME}.service" 2>/dev/null || true

echo "Removing user service file..."
rm -f "${USER_SERVICE}"

echo "Reloading user systemd..."
systemctl --user daemon-reload

echo "Removing udev rule (requires sudo)..."
sudo rm -f "${UDEV_RULE}"

echo "Reloading udev and triggering devices..."
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=input || true
sudo udevadm trigger --name-match=uinput || true

echo
echo "Purged local keyswap setup."
echo "Kept config file:"
echo "  ${HOME}/.config/keyswap/config.json"
echo
echo "Not removed:"
echo "  - your repo directory"
echo "  - ${HOME}/.config/keyswap/config.json"
echo "  - group memberships (input/uinput)"
