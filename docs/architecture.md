# AT — Architecture

One-page overview. For detail, see `specs/`.

---

## Topology
┌─────────────────┐
│ Microphone │
└────────┬────────┘
│ ALSA
▼
┌─────────────────┐
│ Audio Source │──┐
└────────┬────────┘ │
│ frames │ frames
┌────────▼──────┐ ┌──▼──────┐
│ Wake Word │ │ VAD │
└────────┬──────┘ └──┬──────┘
│ │ buffer
│ wake ▼
│ ┌─────────┐
│ │ STT │
│ └────┬────┘
│ │ text
┌────────▼───────────▼───────┐
│ Loop Controller │
│ (state machine, IPC) │
└───┬────────┬────────┬──────┘
│ │ │
│ │ │
▼ ▼ ▼
┌──────┐ ┌──────┐ ┌──────┐
│ LLM │ │ TTS │ │SQLite│
└──────┘ └──────┘ └──────┘


## Process Model

Seven systemd services. Each owns one socket in `/run/at/`. Communication is Unix domain sockets with JSON control messages and raw byte audio payloads. No service talks to any other service directly except through `at-ipc`.

## Data Flow (one turn)

1. **Audio Source** reads mic, frames to 30 ms, broadcasts.
2. **Wake Word** scores frames, emits `wake_detected`.
3. **Loop Controller** plays acknowledgment via **TTS**, commands **VAD** to listen.
4. **VAD** buffers speech, on silence sends buffer directly to **STT**.
5. **STT** transcribes, sends text to **Loop Controller**.
6. **Loop Controller** fetches session context from **SQLite**, sends prompt to **LLM**.
7. **LLM** streams tokens; **Loop Controller** chunks sentences, sends to **TTS**.
8. **TTS** synthesizes and plays.
9. **Loop Controller** writes turn to **SQLite**, enters cooldown, returns to IDLE.

## Shared Libraries

- **at-ipc** — Unix socket client/server, envelope, correlation, ready protocol, death detection
- **at-config** — TOML loading, schema validation, typed accessors

## Hardware

- Dell Latitude 7490, 8th Gen Intel (i5-8250U or i5-8350U)
- 8 GB RAM, 4 GB zram (zstd, zswap disabled)
- 128 GB SSD
- Arch Linux minimal, no GUI
- nftables blocks all outbound

## Memory Budget (peak)

| Component | RAM |
|---|---|
| LLM weights (Qwen2.5 3B Q4) | 2.0 GB |
| KV cache (16K) | 1.0 GB |
| whisper.cpp base.en | 250 MB |
| Piper TTS | 200 MB |
| Wake word + VAD | 150 MB |
| Python + orchestration | 300 MB |
| OS + systemd + logs | 400 MB |
| **Total** | **~4.3 GB** |
| **Headroom** | **~3.7 GB** |

## Latency Budget (end-to-end, steady state)

| Stage | Target |
|---|---|
| Wake → ack | ≤ 300 ms |
| Speech end → STT | ≤ 1.5 s |
| STT → first LLM token | ≤ 500 ms |
| LLM complete → TTS start | ≤ 500 ms |
| **Total end-of-speech → start-of-reply** | **≤ 5 s** |
| 150-token response | ≤ 30 s |
