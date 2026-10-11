#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
python3 scripts/security_check.py --history
python3 scripts/check_destination.py
exec git push -u origin main
