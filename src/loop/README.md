# Loop Controller (`src/loop`)

The Loop Controller coordinates the AT voice loop state machine across wake-word, VAD, STT, LLM, and TTS services over Unix domain sockets using `at-ipc`.

## State Machine

The loop implements seven states:
```
IDLE → ACKNOWLEDGING → LISTENING → TRANSCRIBING → THINKING → SPEAKING → COOLDOWN → IDLE
```
If transcription is empty:
```
TRANSCRIBING → (empty response) → SPEAKING → COOLDOWN → IDLE
```

### States
- **IDLE**: Awaiting `wake_detected` event from wake-word service.
- **ACKNOWLEDGING**: TTS plays wake acknowledgment selected via weighted `ShuffleBag`.
- **LISTENING**: VAD buffers speech until `speech_ended` event is received.
- **TRANSCRIBING**: STT transcribes PCM audio via `transcribe_pcm` command.
- **THINKING**: LLM generates chat response via `generate` command.
- **SPEAKING**: Sentence chunker pipelines sentences to TTS via `speak_sentence`.
- **COOLDOWN**: 400ms silence period to prevent self-triggering before returning to IDLE.

## Known Design Limitations (MVP)

### Session Turn Writing
The Loop Controller does **NOT** directly write turns to SQLite in this MVP release.
Instead, the LLM module writes the turn directly via its `SessionReader.write_turn()` during `generate`.

**Implication / Trade-off:**
If TTS fails or gets cancelled after LLM generation succeeds, the turn is already persisted in the SQLite database even though it was never spoken aloud to the user. On subsequent turns, the LLM context will include this "phantom" turn.

This behavior is accepted for MVP stability. Phase 8 will migrate the SQLite session write responsibility directly into the Loop Controller following successful TTS playback.

## CLI Options

- `--config-dir`: Directory containing `at.toml` and `modules.toml` (defaults to `/etc/at` with fallback to `config/`).
- `--socket-dir`: Custom socket directory override.
- `--demo-turn "text"`: Bypass wake/vad/stt and run LLM → TTS pipeline for the given input text.
- `--smoke`: Connect to TTS, play startup boot greeting, and exit immediately.
- `--no-wake`: Run without requiring wake-word service connection (useful for development).
