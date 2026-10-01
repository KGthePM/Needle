#!/usr/bin/env bash
# Installs the Needle SwiftBar plugin on macOS.
# Usage: ./install-mac.sh [path/to/your/SwiftBar/plugin/folder]
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
BIN="$HOME/.local/bin/needle"
CONF_DIR="$HOME/.config/needle"
CONF="$CONF_DIR/config.json"
PLUGIN="needle.5m.py"

# 1. python3 comes with Apple's Command Line Tools.
if ! xcode-select -p >/dev/null 2>&1; then
  echo "Python 3 isn't installed yet. A window will offer to install Apple's Command Line Tools."
  xcode-select --install >/dev/null 2>&1 || true
  echo "Run this script again once that finishes."
  exit 1
fi

# 2. SwiftBar itself.
if ! open -Ra SwiftBar >/dev/null 2>&1; then
  if command -v brew >/dev/null 2>&1; then
    echo "Installing SwiftBar with Homebrew..."
    brew install --cask swiftbar
  else
    echo "Install SwiftBar from https://swiftbar.app, open it once to pick a plugin folder,"
    echo "then run this script again."
    exit 1
  fi
fi

# 3. Find the plugin folder (argument wins, otherwise ask SwiftBar's settings).
PLUGIN_DIR="${1:-$(defaults read com.ameba.SwiftBar PluginDirectory 2>/dev/null || true)}"
PLUGIN_DIR="${PLUGIN_DIR/#\~/$HOME}"
if [ -z "$PLUGIN_DIR" ] || [ ! -d "$PLUGIN_DIR" ]; then
  open -a SwiftBar || true
  echo "Couldn't find your SwiftBar plugin folder."
  echo "Open SwiftBar, choose a plugin folder when it asks, then run:"
  echo "  ./install-mac.sh /path/to/that/folder"
  exit 1
fi

# 4. Fetcher, keys file, plugin. Needle used to be called ai-usage: keep its keys
#    file, drop the old plugin and fetcher so the menu bar doesn't show both.
OLD_CONF="$HOME/.config/ai-usage/config.json"
if [ -f "$OLD_CONF" ] && [ ! -f "$CONF" ]; then
  mkdir -p "$CONF_DIR" && mv "$OLD_CONF" "$CONF" && rmdir "$(dirname "$OLD_CONF")" 2>/dev/null || true
fi
rm -f "$PLUGIN_DIR/ai-usage.5m.py" "$HOME/.local/bin/ai-usage"
rm -rf "$HOME/.cache/ai-usage"
mkdir -p "$(dirname "$BIN")" "$CONF_DIR"
install -m 755 "$ROOT/fetcher/needle.py" "$BIN"
if [ ! -f "$CONF" ]; then
  install -m 600 "$ROOT/fetcher/config.example.json" "$CONF"
  NEW_CONF=1
fi
chmod 600 "$CONF"
install -m 755 "$HERE/$PLUGIN" "$PLUGIN_DIR/$PLUGIN"

# 5. One-time Keychain permission for Claude's sign-in, so the menu bar never prompts later.
if security find-generic-password -s "Claude Code-credentials" >/dev/null 2>&1; then
  echo
  echo "macOS will now ask to let \"security\" read your Claude Code sign-in."
  echo "Enter your password and click Always Allow so the menu bar can refresh on its own."
  if ! security find-generic-password -s "Claude Code-credentials" -w >/dev/null; then
    echo "Skipped. Claude will show as not signed in until you allow it (rerun this script)."
  fi
else
  echo
  echo "No Claude Code sign-in found in the Keychain. Run \`claude\` and log in, then Refresh."
fi

open -g "swiftbar://refreshallplugins" >/dev/null 2>&1 || true

echo
echo "Installed."
echo "  Plugin   $PLUGIN_DIR/$PLUGIN"
echo "  Fetcher  $BIN"
echo "  Keys     $CONF"
if [ "${NEW_CONF:-0}" = 1 ]; then
  echo
  echo "Next: add your z.ai and OpenRouter keys (menu bar > Edit keys). Claude and Codex need nothing."
fi
