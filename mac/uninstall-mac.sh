#!/usr/bin/env bash
# Removes the plugin and fetcher. Keys are kept unless you pass --purge.
# Usage: ./uninstall-mac.sh [--purge] [plugin-folder]
set -euo pipefail
PURGE=0; [ "${1:-}" = "--purge" ] && { PURGE=1; shift; }
PLUGIN_DIR="${1:-$(defaults read com.ameba.SwiftBar PluginDirectory 2>/dev/null || true)}"
PLUGIN_DIR="${PLUGIN_DIR/#\~/$HOME}"
[ -n "$PLUGIN_DIR" ] && rm -f "$PLUGIN_DIR/needle.5m.py"
rm -rf "$HOME/.local/bin/needle" "$HOME/.cache/needle"
[ "$PURGE" = 1 ] && rm -rf "$HOME/.config/needle"
open -g "swiftbar://refreshallplugins" >/dev/null 2>&1 || true
if [ "$PURGE" = 1 ]; then echo "Removed, including keys."; else echo "Removed. Keys kept in ~/.config/needle (use --purge to delete them)."; fi
