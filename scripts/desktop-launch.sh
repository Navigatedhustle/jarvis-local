#!/usr/bin/env bash
set -euo pipefail

systemctl --user start jarvis-api.service jarvis-dashboard.service

for i in $(seq 1 30); do
    if curl -fsS http://127.0.0.1:3100 >/dev/null 2>&1; then
        exec xdg-open http://127.0.0.1:3100
    fi
    sleep 1
done

notify-send "JARVIS" "JARVIS dashboard did not become ready on port 3100."
exit 1
