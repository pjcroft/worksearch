#!/bin/bash
# Removes WorkSearch from this Mac account. Your files are never touched.
#   bash uninstall-mac.sh           # keeps the search index and settings
#   bash uninstall-mac.sh --purge   # also deletes the index and settings
set -euo pipefail
DATA="$HOME/Library/Application Support/WorkSearch"
LABEL="com.pjcroft.worksearch"
launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
pkill -f "$DATA/app/worksearch.py" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
rm -rf "$HOME/Applications/WorkSearch.app" "$DATA/app" "$DATA/venv"
if [ "${1:-}" = "--purge" ]; then
  rm -rf "$DATA"
  echo "WorkSearch, its index and its settings were removed."
else
  echo "WorkSearch was removed. The index and settings are kept in $DATA (use --purge to delete them)."
fi
