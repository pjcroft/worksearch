#!/bin/bash
# Double-click this file on a Mac to install WorkSearch.
cd "$(dirname "$0")" || exit 1
bash ./install-mac.sh
echo
read -r -n 1 -p "Press any key to close this window."
