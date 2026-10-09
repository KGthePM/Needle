#!/usr/bin/env bash
# Installs the Needle widget for KDE Plasma (Plasma 6, KF6).
# Usage: ./plasma/install-plasma.sh [--update]
set -euo pipefail

UPDATE=0
[ "${1:-}" = "--update" ] && UPDATE=1

HERE="$(cd "$(dirname "$0")" && pwd)"
PLASMOID_ID="com.pinecompute.needle"
DEST="$HOME/.local/share/plasma/plasmoids/$PLASMOID_ID"
BIN="$HOME/.local/bin/needle"
CONF_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/needle"
CONF="$CONF_DIR/config.json"

command -v python3 >/dev/null || { echo "python3 is required"; exit 1; }

# The fetcher and keys file are shared with the Cinnamon applet / other frontends.
mkdir -p "$(dirname "$BIN")" "$CONF_DIR" "$DEST"
install -m 755 "$HERE/../fetcher/needle.py" "$BIN"
if [ ! -f "$CONF" ]; then
    install -m 600 "$HERE/../fetcher/config.example.json" "$CONF"
    NEW_CONF=1
fi
chmod 600 "$CONF"

# Replace any previous copy of the widget package.
rm -rf "$DEST"
cp -r "$HERE/plasmoid/$PLASMOID_ID" "$DEST"

echo "Installed."
[ "$UPDATE" = 1 ] && exit 0
echo
echo "  Widget   $DEST"
echo "  Fetcher  $BIN"
echo "  Keys     $CONF"
echo
if [ "${NEW_CONF:-0}" = 1 ]; then
    echo "Next: right-click the panel, choose Add Widgets, find \"Needle\", and drag it to your panel."
    echo "Then open Needle and choose Add a service."
fi
echo "Test from a terminal any time with:  python3 $BIN --text"
