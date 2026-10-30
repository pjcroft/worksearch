#!/usr/bin/env bash
# Removes WorkSearch from this Linux account. Your files are never touched.
#   bash uninstall-linux.sh           # keeps the search index and settings
#   bash uninstall-linux.sh --purge   # also deletes the index and settings
set -euo pipefail
DATA="${XDG_DATA_HOME:-$HOME/.local/share}/worksearch"
pkill -f "$DATA/app/worksearch.py" 2>/dev/null || true
rm -f "$HOME/.local/bin/worksearch" \
      "${XDG_DATA_HOME:-$HOME/.local/share}/applications/worksearch.desktop" \
      "${XDG_CONFIG_HOME:-$HOME/.config}/autostart/worksearch.desktop"
rm -rf "$DATA/app" "$DATA/venv"
if [ "${1:-}" = "--purge" ]; then
  rm -rf "$DATA"
  echo "WorkSearch, its index and its settings were removed."
else
  echo "WorkSearch was removed. The index and settings are kept in $DATA (use --purge to delete them)."
fi
