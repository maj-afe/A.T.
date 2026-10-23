# AT

A local-only, voice-first companion. Calm, capable, quietly present. Air-gapped. No internet. No telemetry. No data leaves the machine.

AT is a Jarvis-like assistant with a single emotional register: mild, gently sarcastic, fully servile disapproval. It speaks in British male (Piper `en_GB-alan-medium`), addresses the user as "sir," and never claims emotions, subjectivity, or capabilities it doesn't have.

## Status

**Spec phase: complete.** Eleven frozen specs under `specs/`. Build phase: in progress.

Current milestone: `at-ipc` (foundation library).

See `progress.md` for live status.

## Hardware

- Dell Latitude 7490, 8th Gen Intel (i5-8250U or i5-8350U)
- 8 GB RAM, 4 GB zram (zstd, zswap disabled)
- 128 GB SSD
- Arch Linux minimal, no GUI, headless
- nftables blocks all outbound

## Architecture

Seven systemd services communicate over Unix domain sockets:
- **audio-source** — microphone gateway
- **wake-word** — "A.T." detection (openWakeWord, custom-trained)
- **vad** — Silero voice activity detection
- **stt** — whisper.cpp base.en
- **llm** — Qwen2.5 3B Instruct Q4_K_M (llama.cpp)
- **tts** — Piper en_GB-alan-medium
- **loop** — state machine and coordinator

Two shared libraries:
- **at-ipc** — Unix socket messaging
- **at-config** — TOML configuration

See `docs/architecture.md` for the full topology.

## Repo Structure

- `specs/` — frozen specs, one file per module
- `src/` — implementation
- `tests/` — unit, integration, acceptance
- `config/` — `at.toml` (global), `modules.toml` (per-module)
- `personalities/` — personality prompt for the LLM
- `models/` — model files (gitignored)
- `docs/` — decisions log and architecture overview
- `scripts/` — build, deploy, training helpers
- `progress.md` — live status

## Build Order

1. `at-ipc` (foundation)
2. `at-config`
3. `audio-source`
4. `wake-word` (longest — training cycles)
5. `vad`
6. `stt`
7. `tts`
8. `llm`
9. `loop`
10. Integration + tuning

## Agent Workflow

Each module: spec → agent prompt → agent plan → approval → code + tests → review → merge → update `progress.md`.

Cross-family review mandatory. Top-tier models (opus, sonnet, gemini) for architectural modules. Mid-tier for standard services. Free-tier for boilerplate.

See `docs/decisions.md` for full rationale.

## Non-Negotiables

Air-gapped. Local-only. Private. No cloud. No telemetry. No auto-updates. No external logging.

# Wake Word Training Data

**Collected:** 2026-10-23
**Target:** 200-500 positives, 100+ negatives
**Current:** 108 positives, 66 negatives

## Provenance
- Laptop ALC256 mic: 78 positives, 66 negatives
- Samsung phone mic: 30+ positives

## Conditions covered
- Distance: [1ft, 3ft, 6ft, 10ft — check which you actually recorded]
- Tone: [normal, quiet, tired, hurried — check]
- Background: [silence, fan, music, TV — check]

## Gaps
- [Whatever you didn't cover — e.g., "no recordings with music background"]

## Processed directory
16kHz mono 16-bit WAV, converted with sox.

