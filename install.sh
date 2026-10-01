#!/usr/bin/env bash
# Installs the Needle applet for Cinnamon (Linux Mint).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
UUID="needle@pinecompute"
APPLET_DIR="$HOME/.local/share/cinnamon/applets/$UUID"
BIN="$HOME/.local/bin/needle"
CONF_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/needle"
CONF="$CONF_DIR/config.json"

command -v python3 >/dev/null || { echo "python3 is required (sudo apt install python3)"; exit 1; }

# Needle used to be called ai-usage: keep its keys file, drop the old applet and fetcher.
OLD_CONF="${XDG_CONFIG_HOME:-$HOME/.config}/ai-usage/config.json"
if [ -f "$OLD_CONF" ] && [ ! -f "$CONF" ]; then
  mkdir -p "$CONF_DIR" && mv "$OLD_CONF" "$CONF" && rmdir "$(dirname "$OLD_CONF")" 2>/dev/null || true
fi
rm -rf "$HOME/.local/share/cinnamon/applets/ai-usage@kgthepm" "$HOME/.local/bin/ai-usage" \
  "${XDG_CACHE_HOME:-$HOME/.cache}/ai-usage"

mkdir -p "$(dirname "$BIN")" "$CONF_DIR" "$APPLET_DIR"
install -m 755 "$HERE/fetcher/needle.py" "$BIN"
cp -r "$HERE/applet/$UUID/." "$APPLET_DIR/"

if [ ! -f "$CONF" ]; then
  install -m 600 "$HERE/fetcher/config.example.json" "$CONF"
  NEW_CONF=1
fi
chmod 600 "$CONF"

# Reload the applet if it's already on the panel (harmless if it isn't).
dbus-send --session --dest=org.Cinnamon.LookingGlass --type=method_call \
  /org/Cinnamon/LookingGlass org.Cinnamon.LookingGlass.ReloadExtension \
  string:"$UUID" string:'APPLET' >/dev/null 2>&1 || true

echo "Installed."
echo
echo "  Applet   $APPLET_DIR"
echo "  Fetcher  $BIN"
echo "  Keys     $CONF"
echo
if [ "${NEW_CONF:-0}" = 1 ]; then
  echo "Next: add your z.ai and OpenRouter keys to the file above (Claude and Codex need nothing)."
fi
echo "Then right-click your panel > Applets, find \"Needle\", and press +."
echo "Test from a terminal any time with:  python3 $BIN --text"
