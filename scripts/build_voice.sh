#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
docker build -f infra/voice/Dockerfile --target stt -t jarvis-stt:local .
docker build -f infra/voice/Dockerfile --target tts -t jarvis-tts:local .
docker build -f infra/voice/Dockerfile.unified -t jarvis-stt-unified:local .
if [ ! -f models/speech/voices/original-british-chief-of-staff.wav ]; then
  docker build -f infra/voice/Dockerfile.reference -t jarvis-reference:local .
  docker run --rm --name jarvis-reference-builder -v "$PWD/models/speech:/models" jarvis-reference:local
fi
