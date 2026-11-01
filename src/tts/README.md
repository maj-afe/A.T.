# TTS Module

Text-to-Speech service for the AT voice assistant using Piper TTS.

## Architecture

- **Library**: `piper-tts` Python library (not CLI subprocess)
- **Voice**: `en_GB-alan-medium.onnx` (22050 Hz sample rate)
- **Playback**: `paplay` per-sentence WAV files
- **IPC**: Unix domain socket at `/run/at/tts.sock`

### Why not CLI subprocess?

The original spec proposed a CLI subprocess approach. Benchmarking showed the CLI reloads the model per call (~1.5–2.5s latency). Using `PiperVoice` loaded once at service startup reduces per-sentence synthesis to ~50–150ms.

## Configuration

```toml
[tts]
voice_path = "/opt/at/models/tts/en_GB-alan-medium.onnx"
length_scale = 0.95
noise_scale = 0.7
noise_w = 1.2
fade_ms = 100
playback_command = "paplay"
```

| Parameter | Description | Default |
|-----------|-------------|---------|
| `voice_path` | Path to Piper .onnx model | required |
| `length_scale` | Speech speed (lower = faster) | 0.95 |
| `noise_scale` | Noise variance for prosody | 0.7 |
| `noise_w` | Phoneme duration noise width | 1.2 |
| `fade_ms` | Linear fade-out duration (ms) | 100 |
| `playback_command` | WAV player command | "paplay" |

## IPC Interface

### Command: `speak_sentence`

```json
{
  "text": "Good evening, sir."
}
```

Response:
```json
{
  "played": true,
  "queued": true
}
```

### Events

- `playback_started`: `{"text": "..."}`
- `playback_finished`: `{"text": "...", "duration_ms": 1234}` or `{"text": "...", "error": "..."}`

### Queue Behavior

- Sequential processing: one sentence at a time, no overlap
- Max queue depth: 10
- Beyond depth 10: oldest dropped with logged error
- Pipelining: sentence N+1 synthesized while sentence N plays

## Text Preprocessing

`A.T.` → `ay tee` (case-insensitive, handles `A.T.`, `A.T`, `a.t.`, etc.)

Prevents mispronunciation of the assistant's name as the word "at".

## Fade-Out

100ms linear fade-out applied to the last 100ms of PCM samples.
Prevents abrupt WAV end causing VAD/wake-word spurious triggers.

## Running

```bash
# Service mode
python -m tts.main

# Smoke test (synthesize + play once)
python -m tts.main --smoke "Good evening, sir. All systems nominal."

# Custom socket
python -m tts.main --socket-path /tmp/tts.sock

# Custom model
python -m tts.main --model-path /path/to/model.onnx
```

## Signal Handling

- `SIGTERM`: Stops in-flight paplay, cleans up temp files, closes IPC server, exits 0

## Known Limitations

### Sentence Gap

With per-sentence `paplay` invocations, expect ~30-50ms gap between sentences.
Acceptable for MVP. If perceptible in manual testing, switch to a persistent
`paplay --raw` streaming subprocess — but do not implement that now, just note
it here as a future optimization.

## Testing

```bash
# Unit tests (mocked)
pytest tests/unit/test_synthesizer.py -v
pytest tests/unit/test_player.py -v
pytest tests/unit/test_main.py -v

# Integration tests (real PiperVoice + paplay)
pytest tests/integration/test_tts.py -v

# Full suite
pytest tests/ -v
```