# tts — Text-to-Speech

**Version:** 0.2 (frozen)
**Status:** Last link in the chain — the voice.

---

## Purpose

Turn AT's generated text into spoken audio via Piper. Clear, in register, responsive.

## Engine

Piper, voice `en_GB-alan-medium`.

- ~60 MB disk, ~80 MB page-cached
- British male, measured, warm
- Subprocess invocation, model stays in page cache
- `length_scale = 1.1` (slightly slower than default)

## Audio Stack

PipeWire + `pipewire-pulse`. Playback via `paplay` (robustness choice over `pw-play` — sidesteps raw-PCM `--raw` gotcha).

## Processing

1. Loop sends completed sentence (≤500 chars)
2. TTS invokes `piper` subprocess, gets WAV bytes
3. Apply 100 ms linear fade-out (grace fade to prevent VAD spikes)
4. Preprocess text: replace `A.T.` (with periods) → `ay tee` (only if present)
5. Enqueue WAV in FIFO playback queue
6. Playback thread drains queue sequentially via `paplay`
7. When queue empty and last WAV finished → `playback_finished`

Synthesis of sentence N+1 happens while sentence N plays. No gap.

## Queue

- FIFO, sequential, no overlap
- Max depth 10; oldest dropped with error logged on overflow (should never happen)
- Empty queue >100 ms after last playback → `playback_finished`

## Events

- `tts_ready`
- `synthesis_started`
- `synthesis_finished`
- `playback_started`
- `sentence_playing`
- `playback_finished`
- `tts_error`

## Sentence Chunking

Loop Controller's responsibility, documented for cross-reference:
- Split on `.`, `?`, `!`, `;` followed by space or end
- Skip abbreviations (Mr., Mrs., Dr., etc., i.e., e.g.)
- Skip decimals ("3.14")
- Force split at word boundary if >200 chars without boundary
- Flush buffer as final sentence on LLM generation end

## Constraints

- RAM ≤ 200 MB
- CPU ~1 thread synthesis, ~0.5% playback
- First sentence → first audio ≤ 500 ms
- Subsequent sentences: no gaps
- No internet
- No audio recording
- Fixed voice, no changes

## Acceptance Criteria

1. Single sentence → plays clearly, intelligibly
2. 3 rapid sentences → sequential, no gaps or overlap
3. Long sentence (~200 chars) → full synthesis and playback
4. Wake ack ("Sir?") → ≤500 ms from request to first audio
5. 3-sentence LLM response → continuous speech, no stutter
6. Earphones plugged during playback → continues (OS handles)
7. Earphones unplugged during playback → continues (OS handles)
8. 24 hours → no growth, no zombie Piper processes
9. Empty string → no synthesis, no error
10. 600-char string → split by Loop, TTS plays chunks

## Non-Goals

No voice cloning. No emotion/prosody beyond length_scale. No multi-voice. No streaming TTS. No cross-device. No lip-sync. No rate adjustment mid-sentence. No pitch adjustment.

## Risks

Piper subprocess overhead → page cache keeps it fast. Mispronounces uncommon words → accept for MVP. Acronym "A.T." → preprocess to "ay tee". Queue backs up → cap at 10.

## Open Questions Resolved

1. PipeWire
2. `paplay`
3. Preprocess "A.T." → "ay tee"
4. No extra sentence gap
5. No volume normalization
6. Skip failed sentence, abort only if >50% fail