#!/usr/bin/env bash
# Removes the Needle Plasma widget. Keeps your keys (--purge removes them too).
set -euo pipefail

PLASMOID_ID="com.pinecompute.needle"
DEST="$HOME/.local/share/plasma/plasmoids/$PLASMOID_ID"
CONF_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/needle"

rm -rf "$DEST"
echo "Removed the Needle widget."

if [ "${1:-}" = "--purge" ]; then
    rm -rf "$CONF_DIR"
    echo "Removed the keys and settings too."
fi
