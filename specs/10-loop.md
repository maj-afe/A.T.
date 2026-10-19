# loop — Loop Controller

**Version:** 0.2 (frozen)
**Status:** The conductor. Everything else is a specialist; this is the coordinator.

---

## Purpose

Orchestrate the entire voice loop. Hold state machine. Coordinate handoffs. Enforce cooldown. Recover from errors. Never get stuck.

## State Machine

Seven states, strict transitions:

IDLE → ACKNOWLEDGING → LISTENING → TRANSCRIBING → THINKING → SPEAKING → COOLDOWN → IDLE
↓
(empty transcription)
↓
SPEAKING → COOLDOWN → IDLE


### State Definitions

**IDLE** — Wake Word listening. All others idle. On `wake_detected` → select acknowledgment line → ACKNOWLEDGING.

**ACKNOWLEDGING** — TTS plays ack. VAD muted. On `playback_finished` → LISTENING. Timeout: 2 s → IDLE + error.

**LISTENING** — VAD active, buffering. Wake Word muted. On `speech_ended` → pass buffer to STT → TRANSCRIBING. Timeout: 15 s no speech → IDLE. Timeout: 60 s never ends → force-end, pass buffer.

**TRANSCRIBING** — STT running. On `transcription_ready` → LLM → THINKING. On `transcription_empty` → empty-response line → SPEAKING. Timeout: 10 s → IDLE + error.

**THINKING** — LLM generating. Tokens accumulate, sentence boundaries send to TTS. On `llm_response_ready` → flush buffer as final sentence → SPEAKING (if TTS hasn't already started). Timeout: 90 s → abort LLM, speak fallback, → SPEAKING.

**SPEAKING** — TTS playing. No new input. Wake Word muted. On `playback_finished` → write turn to SQLite → COOLDOWN. No timeout (queue cap prevents runaway).

**COOLDOWN** — 400 ms silence, all muted. Timer → IDLE.

Any error → IDLE + log. No trap states. No retries. systemd handles persistent failures.

## Wake Acknowledgment Selection

Shuffle bag with weights and no back-to-back repeats.

| Line | Weight |
|---|---|
| "Sir?" | 30% max |
| "Yes, sir." | ~10% |
| "Listening, sir." | ~10% |
| "Go ahead, sir." | ~10% |
| "At your service, sir." | ~10% |
| "Ready, sir." | ~10% |
| "Sir." | ~10% |
| "On your command, sir." | ≤5%, ≤1 in 20 |

Rules:
- "Sir?" ≤30% across any 20-wake window
- "On your command, sir." ≤1 in 20
- No back-to-back repeats
- Short lines preferred within 60 s of last wake
- Weighted shuffle bag, refilled when empty

State held in memory, resets on service restart.

## Boot Greeting

Time-aware, plays once at service start.

| Time | Line |
|---|---|
| Before 12:00 | "Good morning, sir." |
| 12:00–17:00 | "Good afternoon, sir." |
| After 17:00 | "Good evening, sir." |
| Unclean shutdown | "I'm here, sir." |
| Cold boot, 20% chance | "All systems nominal, sir." |

Clean shutdown detection: startup flag file written at boot, removed on graceful exit. Present = unclean.

Bypasses state machine — plays as preamble before first IDLE. Failure is logged, does not block loop.

## Empty Transcription Handling

When STT returns `transcription_empty`, skip LLM. Select from shuffle bag:

- "I didn't catch that, sir."
- "Say again, sir."
- "Nothing came through, sir."
- "No audio, sir."

Same no-back-to-back rules. Send selected line to TTS, → SPEAKING.

## Session Turn Writing

After SPEAKING completes, write to SQLite:

- Timestamp (ISO 8601)
- Session ID (per service lifetime or per 30-min boundary)
- User text
- AT text
- Latency metrics (wake-to-ack, ack-to-speech-end, stt-time, llm-ttft, llm-total, tts-total, total)
- Empty flag
- Error flag

No audio stored. Text + metrics only.

## Session Boundary

- 30 min elapsed with no turns → rotate session ID
- Service restart → new session
- Explicit reset phrase → later phase

On rotation, LLM context starts fresh. Previous session stays in SQLite.

## Error Handling

Every module's `*_error` event:
1. Log with full context
2. Emit `state_changed` to IDLE
3. Do NOT speak an error response
4. Return to IDLE, continue listening

Exceptions triggering `loop_error` (service restart):
- SQLite write failure
- Module init failure
- Loop Controller internal exception

## Timeouts

| State | Timeout | On expiry |
|---|---|---|
| ACKNOWLEDGING | 2 s | → IDLE + log |
| LISTENING (no speech) | 15 s | → IDLE + log |
| LISTENING (never ends) | 60 s | Force-end |
| TRANSCRIBING | 10 s | → IDLE + log |
| THINKING | 90 s | Abort, speak fallback, → SPEAKING |
| SPEAKING | None | — |
| COOLDOWN | 400 ms | → IDLE |

THINKING fallback line: "That's taking longer than expected, sir."

## Graceful Shutdown (SIGTERM)

1. Mute wake word
2. Let SPEAKING finish (up to 30 s)
3. Abort other states
4. Close SQLite
5. Remove startup flag file
6. Exit 0

SIGKILL → flag stays → next boot uses "I'm here, sir."

## IPC Behavior

- Client connections to all module sockets
- Ready gate: wait for all seven `ready` events before first IDLE (30 s timeout → `loop_error`)
- Death detection: on socket disconnect → mark dead, log, → IDLE, wait for reconnect
- Correlation IDs on all commands
- Audio buffers never touch Loop Controller (VAD → STT direct)

## Constraints

- RAM ≤ 100 MB (coordination state only)
- CPU ~0.1% IDLE
- Latency added ≤ 20 ms per transition
- No internet
- SQLite writes only on turn completion
- No retries within a turn

## Acceptance Criteria

1. Full turn <5 s after warmup
2. 15 s silence after wake → IDLE + log
3. Mumble → empty-response line → IDLE
4. Turn written to SQLite with all metrics
5. Two turns in a row → second LLM call includes first in context
6. 30 min silence → new session on next turn
7. Kill STT mid-transcription → recover, IDLE, error logged
8. Kill LLM mid-generation → timeout → fallback → IDLE
9. Unplug audio mid-turn → TTS fails → IDLE, logged
10. 24 hours → no growth, no stuck states, reasonable SQLite size

## Non-Goals

No barge-in. No multi-user. No tools. No cross-device. No long-term memory. No hot-reload. No CLI. No GUI.

## Risks

State machine stuck → systemd restart. Monolith creep → enforce thin controller, coordinates only. Session ID bug → test explicitly, log IDs. Chunker splits wrong → abbreviation list, decimal check. SQLite blocks loop → async write, drop oldest on backlog.

## Open Questions Resolved

1. Separate processes (not single-process async)
2. LLM timeout uses abort callback
3. Wake during ACKNOWLEDGING → ignore
4. Boot greeting failure → log, proceed to IDLE
5. SQLite write failure → log, `loop_error`, → IDLE, continue
6. Metrics: state = info, module = debug, token = trace (off default). Turn metrics = info always.
