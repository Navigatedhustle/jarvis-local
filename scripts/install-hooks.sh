#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
git config --local core.hooksPath .githooks
echo "Hooks installed; install Gitleaks and run python3 scripts/security_check.py --history before every push."
