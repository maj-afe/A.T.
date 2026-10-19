# wake-word — Wake Word Detection

**Version:** 0.2 (frozen)
**Status:** Trigger for the entire voice loop.

---

## Purpose

Detect spoken wake phrase "A.T." (letter by letter) from continuous audio. Emit a single clean event when detected. Ignore everything else.

## Model

openWakeWord, custom-trained on your voice and synthetic negatives.

**Training data:**
- Positives: 200–500 samples of you saying "A.T." — varied distance, tone, background
- Negatives: similar-sounding words (eighty, eighty-eight, hate, mate, wait, ate, date, rate, plate, skate, state, weight, late, fate) + your own normal speech + public English corpora
- Total: 2,000–5,000 samples
- Training machine: main laptop, one college-session batch

## Detection Pipeline

1. Subscribe to Audio Source frames (16 kHz mono)
2. Frame scored by model → confidence 0.0–1.0
3. Confidence ≥ threshold → `wake_detected`
4. Debounce: 400 ms local cooldown, collapse multi-frame fires to one event

## Threshold

Initial 0.5. Tuned empirically for ≥95% true positive, ≤1 false positive/hour quiet.

## Events

- `wake_detected` — confidence above threshold
- `wake_rejected` — above floor, below threshold (logged only)
- `wake_engine_ready` — model loaded
- `wake_engine_error` — model or stream failure

## Constraints

- RAM ≤ 100 MB
- CPU ≤ 5% on i5-8350U
- Latency: detection → event ≤ 100 ms
- No audio storage. Frames read, scored, discarded.
- No audio output

## Acceptance Criteria

1. From 1 ft → ≥95% detection
2. From 10 ft → ≥85% detection
3. Quiet → ≥80% detection
4. "eighty" × 20 → 0 false positives
5. "eighty-eight" × 20 → 0 false positives
6. 1 hour speech → ≤5 false positives
7. 1 hour silence → 0 false positives
8. Wired earphones plugged mid-session → reconnects within 2 s
9. Device unplugged → falls back within 2 s
10. 24 hours → no leaks, no missed due to memory

## Non-Goals

No speaker ID. No emotion detection. No transcription. No Bluetooth profile management (user switches manually).

## Bluetooth Note

Bluetooth headsets must be manually switched to HFP mode by the user. AT does not manage profiles. In A2DP mode, headset has no mic and AT hears nothing from it. No error, no prompt.