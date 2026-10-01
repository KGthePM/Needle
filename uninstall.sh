#!/usr/bin/env bash
# Removes the applet and fetcher. Your keys file is kept unless you pass --purge.
set -euo pipefail
UUID="needle@pinecompute"
rm -rf "$HOME/.local/share/cinnamon/applets/$UUID" "$HOME/.local/bin/needle" "${XDG_CACHE_HOME:-$HOME/.cache}/needle"
if [ "${1:-}" = "--purge" ]; then rm -rf "${XDG_CONFIG_HOME:-$HOME/.config}/needle"; echo "Removed, including keys."
else echo "Removed. Keys kept in ~/.config/needle (use --purge to delete them)."; fi
