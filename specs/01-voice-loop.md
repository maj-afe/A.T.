# AT — Voice Loop MVP

**Version:** 0.4 (frozen)
**Status:** Top-level architecture for the MVP.

---

## Scope

The voice loop is AT's entire body in the MVP. It hears, understands, responds, and goes quiet. Everything else — memory, personality, tools — either feeds into it or is downstream of it.

## Architecture

Process-isolated. Seven services communicate over Unix domain sockets. All local. All air-gapped.

### Services

| Service | Role |
|---|---|
| Audio Source | Owns the microphone, fans out audio frames |
| Wake Word | Detects "A.T.", emits wake events |
| VAD | Marks speech start/end, buffers utterances |
| STT | Transcribes utterances via whisper.cpp |
| LLM Runtime | Generates responses via llama.cpp |
| TTS | Synthesizes speech via Piper |
| Loop Controller | Orchestrates the state machine |

### Shared Libraries

| Library | Role |
|---|---|
| at-ipc | Unix socket messaging, envelope, correlation |
| at-config | TOML config loading, validation |

## Data Flow

1. Audio Source reads mic, frames to 30 ms chunks, broadcasts.
2. Wake Word subscribes, scores frames, emits `wake_detected`.
3. Loop Controller plays acknowledgment via TTS, then commands VAD to listen.
4. VAD subscribes to Audio Source, buffers until silence, sends buffer to STT.
5. STT transcribes, sends text to Loop Controller.
6. Loop Controller sends prompt + context to LLM Runtime.
7. LLM generates tokens; Loop Controller accumulates, chunks into sentences, sends to TTS.
8. TTS synthesizes and plays each sentence.
9. Loop Controller writes turn to SQLite, enters cooldown, returns to IDLE.

## MVP Module List

1. `at-ipc` — shared library
2. `at-config` — shared library
3. `audio-source` — service
4. `wake-word` — service
5. `vad` — service
6. `stt` — service
7. `llm` — service
8. `tts` — service
9. `loop` — service

## Locked Decisions

| Item | Decision |
|---|---|
| OS | Arch Linux minimal, no GUI, headless |
| Hardware | Dell Latitude 7490, 8th gen Intel, 8 GB RAM, 128 GB SSD |
| Swap | zram only, zstd, `ram / 2`, zswap disabled |
| Network | nftables blocks all outbound, air-gapped |
| Audio stack | PipeWire + pipewire-pulse |
| IPC | Unix domain sockets, JSON + raw byte frames |
| Supervision | systemd, one unit per service |
| Config | TOML, two files (`at.toml`, `modules.toml`) |
| STT | whisper.cpp base.en, 3 threads, subprocess |
| LLM | Qwen2.5 3B Instruct Q4_K_M, 6 threads, prompt caching, streaming tokens |
| TTS | Piper, en_GB-alan-medium, length_scale 1.1 |
| Wake word | openWakeWord, custom "A.T." model |
| VAD | Silero VAD, threshold 0.5, 750 ms silence, 250 ms min speech |
| Context | 16K tokens, sliding window with anchor |
| Session | 30 min silence or explicit reset |
| Cooldown | 400 ms after AT speaks |
| Max user utterance | 60 s |
| Max AT reply | 150 tokens default, 400 on elaboration |

## Non-Goals for MVP

- Barge-in
- Long-term memory
- Vector retrieval
- Tools and automation
- Vision
- Bluetooth input management
- Cross-device streaming
- Multi-user
- Cloud fallback (ever, by design)

## Latency Targets

| Stage | Target |
|---|---|
| Wake → acknowledgment | ≤ 300 ms |
| Speech end → STT complete | ≤ 1.5 s |
| STT → first LLM token | ≤ 500 ms (steady state) |
| LLM complete → TTS start | ≤ 500 ms |
| Total end-of-speech → start-of-reply | ≤ 5 s |
| Full 150-token response | ≤ 30 s |