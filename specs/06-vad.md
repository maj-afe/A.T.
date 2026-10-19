# vad — Voice Activity Detection

**Version:** 0.2 (frozen)
**Status:** Timing layer between wake and transcription.

---

## Purpose

Detect when the user is speaking and when they have stopped. Mark speech start/end. Buffer the utterance. Hand off to STT.

## Model

Silero VAD (ONNX, CPU-friendly). ~2 MB, fast, accurate on conversational speech, works across accents.

## Configuration

- Sensitivity threshold: 0.5 (default, user-tunable)
- Silence duration to end utterance: 750 ms
- Minimum speech duration: 250 ms
- Frame size: 30 ms (Silero native)

## State Machine

Two states:
- **SILENT** — listening for first speech frame
- **SPEAKING** — accumulating frames

Transitions:
- SILENT → SPEAKING: first frame above threshold → `speech_started`
- SPEAKING → SILENT: 750 ms silence → `speech_ended`
- SPEAKING → SILENT (short): accumulated <250 ms → `speech_rejected`

Brief silences during speech (<750 ms) do not end the utterance.

## Events

- `speech_started`
- `speech_ended` (with buffer passed to STT via raw bytes)
- `speech_rejected` (too short)
- `vad_ready`
- `vad_error`

## Constraints

- RAM ≤ 50 MB
- CPU ≤ 2% always-on
- Latency: speech end detection ≤ 800 ms
- 60 s max utterance (matching Voice Loop spec)
- Woken VAD — only active between `wake_detected` and `speech_ended`
- Muted during SPEAK and COOLDOWN states
- No audio stored on disk

## Acceptance Criteria

1. Full sentence → `speech_started` within 200 ms of first syllable
2. Stop speaking → `speech_ended` 750 ms ±100 ms after last syllable
3. 500 ms pause mid-sentence → not ended
4. 1 s pause mid-sentence → ended
5. Sub-250 ms cough → rejected
6. "yes" → accepted
7. Fan noise 10 min → 0 false starts
8. Music with vocals 10 min → ≤3 false starts
9. 24 hours → no leaks, no spikes

## Non-Goals

No speaker ID. No emotion detection. No language detection. No noise suppression. No echo cancellation. No transcription.

## Risks

Fan noise → false starts. Mitigate with threshold. TV/music with vocals → VAD cannot distinguish source; cooldown + user muting help.