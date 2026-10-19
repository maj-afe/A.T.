# stt — Speech-to-Text

**Version:** 0.2 (frozen)
**Status:** Bridge from audio to language.

---

## Purpose

Convert PCM utterance to text via whisper.cpp.

## Engine

whisper.cpp, `base.en` model.

- ~142 MB on disk, ~250 MB resident
- English-only, good balance for conversational speech
- Load once at startup, keep resident
- Warmup not needed (first call ~same as subsequent)
- Downloaded on main laptop, transferred via USB

## Processing

1. VAD sends buffer via Unix socket (raw bytes with JSON header)
2. Validate format: 16 kHz mono 16-bit, ≤60 s
3. Invoke `whisper-cli` subprocess (not binding)
4. Beam size 1 (greedy), temp 0.0, forced English, no initial prompt
5. Whisper's internal VAD disabled (we use Silero upstream)
6. Concatenate segments, normalize whitespace
7. Emit `transcription_ready` with text + metrics, or `transcription_empty` if blank

## Events

- `transcription_ready` — text + metrics (inference time, duration, RTF, avg_logprob)
- `transcription_empty`
- `stt_ready`
- `stt_error`

## Threading

3 threads of 8 logical cores. LLM uses 6, never overlap.

## Metrics Logged

- Utterance duration (ms)
- Inference time (ms)
- Real-time factor
- Thread count used
- `avg_logprob`
- Text length
- Empty flag

## Constraints

- RAM ≤ 300 MB resident
- CPU ~3 threads during inference
- 5 s utterance → ≤1.5 s inference
- Real-time factor ≤0.3
- No internet
- No audio storage
- No streaming (full utterances only)

## Acceptance Criteria

1. Clear 5 s sentence → correct, under 1.5 s
2. "What's the weather, sir?" → expected
3. "Yes" / "No" → transcribed
4. Mumbled → plausible or `transcription_empty`
5. Silence → `transcription_empty`
6. 100 utterances → no growth
7. 24 hours → stable ~250–300 MB
8. Kill mid-inference → clean error, no zombie threads

## Non-Goals

No streaming. No translation. No diarization. No confidence gating (log `avg_logprob` only). No custom vocabulary. No punctuation restoration beyond whisper's native.

## Risks

Beam=1 accuracy loss → tunable. 3 threads slow → bump to 4. Buffer overflow → 60 s cap enforced by VAD, STT validates.

## Open Questions Resolved

1. Subprocess, not binding
2. Reject non-16kHz buffers with `stt_error`
3. Empty result → `transcription_empty`, Loop handles response
4. Model path in global `at.toml` under `[paths]`
5. Confidence logged only, no gating