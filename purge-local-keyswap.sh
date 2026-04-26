#!/usr/bin/env bash
set -Eeuo pipefail

APP_NAME="keyswap"
USER_SERVICE="${HOME}/.config/systemd/user/${APP_NAME}.service"
UDEV_RULE="/etc/udev/rules.d/70-${APP_NAME}.rules"
USER_CONFIG="${HOME}/.config/keyswap/config.json"

echo "Stopping and disabling local user service if present..."
systemctl --user disable --now "${APP_NAME}.service" 2>/dev/null || true
systemctl --user reset-failed "${APP_NAME}.service" 2>/dev/null || true

echo "Removing user service file..."
rm -f "${USER_SERVICE}"

echo "Reloading user systemd..."
systemctl --user daemon-reload

echo "Removing local udev rule (requires sudo)..."
sudo rm -f "${UDEV_RULE}"

echo "Reloading udev and triggering devices..."
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=input || true
sudo udevadm trigger --name-match=uinput || true

echo
echo "Purged local keyswap setup."
echo "Kept user config:"
echo "  ${USER_CONFIG}"
echo
echo "Not removed:"
echo "  - your repo directory"
echo "  - ${USER_CONFIG}"
echo "  - group memberships (input/uinput)"
echo "  - any packaged installation of keyswap"
