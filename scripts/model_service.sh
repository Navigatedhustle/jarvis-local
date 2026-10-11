#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
role="${1:?model role required}"
image='vllm/vllm-openai@sha256:18372a7224938643461b846fb64c5c9d3d6e9727e82caf2dc3043e620c9d4d7a'
case "$role" in
  primary)
    port=8181; budget=0.45; length=32768
    extra=(--reasoning-parser qwen3 --enable-auto-tool-choice --tool-call-parser qwen3_xml --middleware model_middleware.SpeechPriorityMiddleware)
    ;;
  background)
    port=8184; budget=0.12; length=8192
    extra=(--enforce-eager)
    ;;
  *) echo 'Unknown model role' >&2; exit 2 ;;
esac
exec docker run --rm --name "jarvis-$role" --gpus all --shm-size 4g \
  -p "127.0.0.1:$port:8000" \
  -v "$PWD/models/$role:/model:ro" \
  -v "$PWD/runtime/huggingface:/root/.cache/huggingface" \
  -v "$PWD/runtime/vllm-$role:/root/.cache/vllm" \
  -v "$PWD/runtime/triton-$role:/root/.triton" \
  -v "$PWD/src/jarvis/model_middleware.py:/opt/jarvis/model_middleware.py:ro" \
  -e PYTHONPATH=/opt/jarvis \
  -e HF_HUB_OFFLINE=1 -e VLLM_NO_USAGE_STATS=1 -e DO_NOT_TRACK=1 \
  "$image" /model --served-model-name "jarvis-$role" \
  --host 0.0.0.0 --port 8000 --max-model-len "$length" \
  --gpu-memory-utilization "$budget" --max-num-seqs 2 \
  "${extra[@]}"
