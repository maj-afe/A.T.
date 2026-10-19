# at-config — Configuration Loader

**Version:** 0.1 (frozen)
**Status:** Shared library. Every service imports it.

---

## Purpose

Load, validate, and expose typed config to all services. Two files, TOML, read-only at runtime.

## Files

- `/etc/at/at.toml` — global settings
- `/etc/at/modules.toml` — per-module settings

## Structure

**`at.toml`:**

- `[paths]` — model paths, SQLite path, socket directory, personality file
- `[logging]` — level, directory, rotation
- `[audio]` — sample rate, frame size
- `[session]` — boundary duration

**`modules.toml`:**

- `[wake_word]` — threshold, model path, cooldown, acknowledgment weights
- `[vad]` — sensitivity, silence_ms, min_speech_ms
- `[stt]` — model path, threads, beam, language
- `[llm]` — model path, threads, context window, sampling, budgets
- `[tts]` — voice path, length_scale, fade_ms, playback command
- `[loop]` — cooldown, weights, boot greeting rules
- `[ipc]` — socket paths per module

## Behavior

- Loads both files at service startup
- Validates required keys and types
- Missing key or wrong type = fatal startup error
- Unknown keys = warning, not fatal
- Read-only during runtime
- No hot-reload

## API

Typed accessors: `config.get("vad", "silence_ms")` → int.

## Constraints

- Python 3.12
- stdlib `tomllib` only
- Load once at startup
- Zero cost when idle

## Non-Goals

- No hot-reload
- No encryption
- No remote config
- No UI