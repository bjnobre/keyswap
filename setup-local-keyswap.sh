#!/usr/bin/env bash
set -Eeuo pipefail

APP_NAME="keyswap"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APP_SCRIPT="${SCRIPT_DIR}/keyswap.py"
USER_CONFIG_DIR="${HOME}/.config/${APP_NAME}"
USER_CONFIG_FILE="${USER_CONFIG_DIR}/config.json"
USER_SYSTEMD_DIR="${HOME}/.config/systemd/user"
USER_SERVICE_FILE="${USER_SYSTEMD_DIR}/${APP_NAME}.service"

DEVICES=()
FORCE_CONFIG=0
NO_SERVICE=0

usage() {
  cat <<'EOF'
Usage:
  ./setup-local-keyswap.sh /dev/input/event0 [/dev/input/event19 ...]
  ./setup-local-keyswap.sh --force-config /dev/input/event0 /dev/input/event19
  ./setup-local-keyswap.sh --no-service /dev/input/event0 /dev/input/event19

What it does:
  - checks for python3-evdev
  - creates ~/.config/keyswap/config.json if absent
  - creates ~/.config/systemd/user/keyswap.service
  - optionally enables and starts the user service

Options:
  --force-config   overwrite ~/.config/keyswap/config.json
  --no-service     do not enable/start the user service
  -h, --help       show this help
EOF
}

die() {
  echo "error: $*" >&2
  exit 1
}

parse_args() {
  while (($#)); do
    case "$1" in
      --force-config)
        FORCE_CONFIG=1
        shift
        ;;
      --no-service)
        NO_SERVICE=1
        shift
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      /dev/input/event*)
        DEVICES+=("$1")
        shift
        ;;
      *)
        die "unknown argument: $1"
        ;;
    esac
  done
}

validate() {
  [[ -f "${APP_SCRIPT}" ]] || die "missing ${APP_SCRIPT}"
  ((${#DEVICES[@]} > 0)) || die "no event devices provided"

  for dev in "${DEVICES[@]}"; do
    [[ -e "${dev}" ]] || die "device does not exist: ${dev}"
  done

  python3 - <<'PY' >/dev/null 2>&1 || die "python3-evdev is not installed. Install it with: sudo apt install python3-evdev"
import evdev
PY
}

write_config() {
  mkdir -p "${USER_CONFIG_DIR}"

  if [[ -f "${USER_CONFIG_FILE}" && "${FORCE_CONFIG}" -ne 1 ]]; then
    echo "Keeping existing config: ${USER_CONFIG_FILE}"
    return
  fi

  {
    echo "{"
    echo '  "devices": ['
    for i in "${!DEVICES[@]}"; do
      comma=","
      [[ $i -eq $((${#DEVICES[@]} - 1)) ]] && comma=""
      printf '    "%s"%s\n' "${DEVICES[$i]}" "${comma}"
    done
    cat <<'EOF'
  ],
  "substitutions": {
    "C-nk_minus": "=",
    "C-nk_delete": ".",
    "A-C-S-g": "great!!!"
  }
}
EOF
  } > "${USER_CONFIG_FILE}"

  echo "Wrote config: ${USER_CONFIG_FILE}"
}

write_service() {
  mkdir -p "${USER_SYSTEMD_DIR}"

  cat > "${USER_SERVICE_FILE}" <<EOF
[Unit]
Description=Keyswap keyboard substitution daemon

[Service]
Type=simple
ExecStart=${APP_SCRIPT}
WorkingDirectory=${SCRIPT_DIR}
Restart=on-failure
RestartSec=1

[Install]
WantedBy=default.target
EOF

  echo "Wrote user service: ${USER_SERVICE_FILE}"
}

manual_test_hint() {
  cat <<EOF

Manual test first:
  python3 ${APP_SCRIPT} --verbose

If that works, the service should work too.
EOF
}

start_service() {
  systemctl --user daemon-reload
  systemctl --user enable --now "${APP_NAME}.service"
  systemctl --user reset-failed "${APP_NAME}.service" || true

  echo
  systemctl --user status "${APP_NAME}.service" --no-pager
}

show_summary() {
  cat <<EOF

Setup complete.

Config:
  ${USER_CONFIG_FILE}

Service:
  ${USER_SERVICE_FILE}

Useful commands:
  systemctl --user restart ${APP_NAME}.service
  systemctl --user status ${APP_NAME}.service
  journalctl --user -u ${APP_NAME}.service -f

Edit mappings:
  ${USER_CONFIG_FILE}
EOF
}

main() {
  parse_args "$@"
  validate
  write_config
  write_service
  manual_test_hint

  if [[ "${NO_SERVICE}" -eq 0 ]]; then
    start_service
  fi

  show_summary
}

main "$@"
