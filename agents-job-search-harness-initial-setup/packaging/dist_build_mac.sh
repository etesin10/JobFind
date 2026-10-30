#!/usr/bin/env bash
# Rebuild this portable macOS distribution from the bundled source snapshot.
# Running the app does NOT need this; building needs Python 3.11 on a Mac.
set -euo pipefail
cd "$(dirname "$0")/source"
bash build_mac.sh
