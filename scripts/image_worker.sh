#!/usr/bin/env bash
# Fixed local worker only. No model-generated command/URL is ever executed.
set -euo pipefail
cd "$(dirname "$0")/.."
edition="${1:?edition required}"
[[ "$edition" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || exit 2
job="$PWD/data/content_packets/$edition"
[[ "$(realpath "$job")" == "$job" ]] || exit 2
exec 9>data/image-worker.lock
flock -n 9 || exit 75
restore=0
cleanup() {
 docker stop -t 1 jarvis-image-job >/dev/null 2>&1 || true
 if [[ "$restore" == 1 && ! -f data/demo-mode ]]; then
   systemctl --user start jarvis-background-model.service || true
 fi
}
trap cleanup EXIT
trap 'exit 75' TERM INT
[[ ! -f data/demo-mode ]] || exit 75
if systemctl --user is-active --quiet jarvis-background-model.service; then
 restore=1
 systemctl --user stop jarvis-background-model.service
fi
# Leave the primary and both voice services running. Avoid swap thrashing.
available=$(awk '/MemAvailable:/{print $2}' /proc/meminfo)
[[ "$available" -gt 29360128 ]] || exit 75
timeout --signal=TERM --kill-after=10 900 docker run --rm --name jarvis-image-job \
 --label "jarvis.packet=$edition" \
 --network none --gpus all --shm-size 1g --memory 27g --memory-swap 27g --pids-limit 256 \
 --cap-drop ALL --security-opt no-new-privileges --user "$(id -u):$(id -g)" \
 -e HF_HUB_OFFLINE=1 -e TRANSFORMERS_OFFLINE=1 -e PYTHONDONTWRITEBYTECODE=1 \
 -v "$PWD/models/image:/models:ro" -v "$job:/job" \
 -v "$PWD/scripts/image_render.py:/app/render.py:ro" jarvis-image:local python /app/render.py
