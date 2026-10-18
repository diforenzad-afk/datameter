#!/usr/bin/env bash
# Install the polkit rule that lets the desktop user start/stop vnstat
# without a password prompt (needed for datameter's daemon toggle on Hyprland,
# where no polkit authentication agent is running).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RULE="50-datameter-vnstat.rules"
DEST="/usr/share/polkit-1/rules.d/$RULE"

echo "Installing $RULE -> $DEST (needs sudo once)"
sudo install -m 0644 "$HERE/$RULE" "$DEST"
echo "Done. Test with:  systemctl stop vnstat && systemctl start vnstat"
