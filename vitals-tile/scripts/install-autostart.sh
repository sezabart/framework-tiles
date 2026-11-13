#!/usr/bin/env bash
# Install (or remove) the user service that starts the vitals tile at login.
#   scripts/install-autostart.sh
#   scripts/install-autostart.sh --uninstall
set -euo pipefail
cd "$(dirname "$0")/.."
PROJECT_DIR="$(pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT="$UNIT_DIR/vitals-tile.service"

if ! systemctl --user show-environment >/dev/null 2>&1; then
  echo "No systemd user session found - cannot install the service." >&2
  exit 1
fi

if [ "${1:-}" = "--uninstall" ]; then
  systemctl --user disable --now vitals-tile.service 2>/dev/null || true
  rm -f "$UNIT"
  systemctl --user daemon-reload
  echo "Removed $UNIT"
  exit 0
fi

mkdir -p "$UNIT_DIR"
cat > "$UNIT" <<UNIT_EOF
[Unit]
Description=Framework Desktop vitals OLED tile

[Service]
Type=simple
WorkingDirectory=$PROJECT_DIR
ExecStart=$PROJECT_DIR/run.sh --quiet
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
UNIT_EOF

systemctl --user daemon-reload
systemctl --user enable --now vitals-tile.service
systemctl --user restart vitals-tile.service
sleep 2
systemctl --user --no-pager --lines=8 status vitals-tile.service || true
echo
echo "Logs: journalctl --user -u vitals-tile -f"
echo "Stop: systemctl --user stop vitals-tile   (before running ./run.sh manually)"
