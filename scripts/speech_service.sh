#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
role="${1:?speech role required}"
case "$role" in
  stt) port=8185; image=jarvis-stt-unified:local ;;
  tts) port=8186; image=jarvis-tts:local ;;
  *) exit 2 ;;
esac
exec docker run --rm --name "jarvis-$role" --gpus all --shm-size 2g \
 -p "127.0.0.1:$port:8000" -v "$PWD/models/speech:/models" \
 -v "$PWD/src/jarvis/speech_service.py:/app/speech_service.py:ro" \
 -v "$PWD/src/jarvis/speech_cache.py:/app/speech_cache.py:ro" \
 -v "$PWD/src/jarvis/voice_profiles.py:/app/voice_profiles.py:ro" \
 -e HF_HUB_DISABLE_IMPLICIT_TOKEN=1 "$image"
