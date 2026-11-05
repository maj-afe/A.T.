#!/usr/bin/env bash
# Launch all AT services for local dev testing
set -euo pipefail

AT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$AT_ROOT"
source .venv/bin/activate

mkdir -p /tmp/at
rm -f /tmp/at/*.sock
export PYTHONPATH="$AT_ROOT/src"

python -m audio_source.main --socket-path /tmp/at/audio-source.sock &
if [ -f "$AT_ROOT/src/vad/main.py" ]; then
    python -m vad.main --socket-path /tmp/at/vad.sock &
fi
python -m stt.main --socket-path /tmp/at/stt.sock &
python -m llm.main --socket-path /tmp/at/llm.sock &
python -m tts.main --socket-path /tmp/at/tts.sock &
sleep 2
python -m loop.main --config-dir config &
wait
