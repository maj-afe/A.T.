# vad — Voice Activity Detection

`vad` is the timing layer between wake word and transcription. It wraps Silero VAD (ONNX, CPU) in a two-state machine that marks speech start/end, buffers one utterance in memory, and rejects utterances that are too short to be worth transcribing.

This is a **pure inference core**: no IPC server, no subprocess, no audio written to disk.

---

## Architecture

- **`detector.py`** — `SileroVAD`: loads the ONNX model with onnxruntime and converts 960-byte PCM frames into one speech probability per frame. Supports both Silero ONNX interfaces:
  - v4/v3: `input`/`sr`/`h`/`c` → `output`/`hn`/`cn`
  - v5: `input`/`state`/`sr` → `output`/`stateN`
- **`state_machine.py`** — `VADStateMachine`: SILENT/SPEAKING logic, utterance buffering, and event emission.

Silero's native analysis window is 512 samples; the detector keeps a rolling buffer of the most recent 512 samples and scores it on each 30 ms frame. Inference runs synchronously in the caller's async context (no threads or executors).

## Input contract

- Frames are exactly **960 bytes** = 480 samples × 2 bytes = 30 ms of 16 kHz mono S16_LE PCM (the audio-source frame format).
- A frame of any other size raises `ValueError`. The core does **not** reframe input; that is the caller's responsibility.

## Configuration

Defaults match `config/modules.toml` `[vad]`:

| Setting | Default | Meaning |
|---|---|---|
| `sensitivity` | `0.5` | Speech probability threshold |
| `silence_ms` | `750` | Continuous silence that ends an utterance |
| `min_speech_ms` | `250` | Minimum voiced audio for an utterance to be accepted |

`VADStateMachine.from_config(config, scorer)` reads these from an `at_config.Config`.

## State machine

- **SILENT** — every frame is scored; the first frame with `probability >= threshold` emits `speech_started`, starts the buffer, and switches to SPEAKING.
- **SPEAKING** — every frame is buffered (including trailing silence) and scored. A voiced frame resets the silence run; a 750 ms continuous silence run ends the utterance:
  - `< 250 ms` of voiced audio → `speech_rejected` (buffer discarded, no audio emitted)
  - otherwise → `speech_ended` with the full utterance buffer
- Pauses shorter than 750 ms (e.g. 500 ms) do **not** split an utterance.
- The buffer is capped at 1920 frames (the spec's 60 s force-end rule). Reaching the cap force-emits `speech_ended` and resets, so runaway speech can never grow the buffer unbounded.

## Events

| Event | Payload |
|---|---|
| `speech_started` | `frame_index`, `probability` |
| `speech_ended` | `frame_index`, `probability`, `duration_ms`, `voiced_ms`, `audio` (full utterance bytes) |
| `speech_rejected` | `frame_index`, `probability`, `duration_ms`, `voiced_ms`, `audio=None` |

`VADEvent` is a frozen dataclass. Events are produced by `process_frame()` (returns a list) or the async `process_frames(frames)` iterator, which accepts both sync and async iterables.

## Model file (air-gapped)

The model is **not** downloaded by this code. Place the Silero VAD ONNX file at:

```
models/vad/silero_vad.onnx
```

`SileroVAD(...)` raises `VADModelError` with a clear message if the file is missing. The model file is gitignored (`models/`).

## Tests

```
pytest tests/unit/test_state_machine.py tests/unit/test_detector.py -v
```

- `test_state_machine.py` uses a deterministic mock scorer; it never touches the model.
- `test_detector.py` uses the real model and procedurally generated WAV files. It is skipped with a clear message when `models/vad/silero_vad.onnx` is absent.

## Non-goals

No speaker ID, emotion detection, language detection, noise suppression, echo cancellation, or transcription.
