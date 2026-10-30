# AT Project — Decision Log

Every architectural decision, with rationale and alternatives considered. Update after every significant choice. This is how you defend against agent drift and memory loss.

Format: **Date — Decision.** Rationale. Alternatives considered.

---

## Hardware & OS

**2026-XX-XX — Dell Latitude 7490 (LA-F322P), 8th Gen Intel.** Quad-core with HT. Arch Wiki lists 7490 as fully compatible. Lenovo E41-25 rejected — it's AMD A4/E2, not i5, cannot run local LLM. Alternatives: none viable at this budget.

**2026-XX-XX — 8 GB RAM now, upgrade to 16 GB later.** Current setup runs 3B Q4 comfortably with 4 GB zram. 16 GB would unlock 7B and 32K context.

**2026-XX-XX — 128 GB SSD.** Ample for OS, models, code, logs. ~110 GB free after install.

**2026-XX-XX — Arch Linux minimal, no GUI, headless.** Low overhead, full control, systemd-native. No Firefox. SSH for configuration. Alternatives: Debian (more bloat), NixOS (steeper curve).

**2026-XX-XX — zram only, zstd, `ram/2`, zswap disabled.** No disk swap. 4 GB zram with 1:2–1:3 ratio gives 6–12 GB effective. zswap disabled via kernel cmdline to prevent cache interference.

**2026-XX-XX — nftables, block all outbound.** Air-gap enforced at the kernel level. No DNS, no NTP, no auto-update, no telemetry.

**2026-XX-XX — PipeWire + pipewire-pulse.** Modern Arch default, handles device switching, hot-plug, volume. Alternatives: PulseAudio (older), ALSA direct (breaks device management principle).

## Architecture

**2026-XX-XX — Local-only, air-gapped.** No cloud, no telemetry, no external calls. Non-negotiable.

**2026-XX-XX — Python for orchestration, C++ engines under the hood.** whisper.cpp, llama.cpp, Piper, Silero, openWakeWord all have C++ cores. Python is glue. Fast iteration, AI-agent friendly, performance cost negligible versus inference.

**2026-XX-XX — Process isolation, one systemd unit per service.** Crash containment, per-module visibility, clean restart. Cost: IPC overhead (~1–2 ms per turn, negligible) and added complexity. Alternative: single-process asyncio (simpler, less robust).

**2026-XX-XX — Unix domain sockets, JSON control + raw byte audio.** Local-only by construction, fast (~50–100 µs), stdlib-only. Alternatives: ZeroMQ (extra dep), gRPC (heavy), Redis (forbidden by RAM).

**2026-XX-XX — Shared `at-ipc` library.** Every service uses it. One envelope, one ready protocol, one death protocol. Written once, tested once.

**2026-XX-XX — Audio Source as eighth module.** Process isolation forces one owner per resource. Audio Source owns the mic and fans out to subscribers. Wake Word and VAD subscribe. Alternative: Wake Word brokers frames (violates single-responsibility).

**2026-XX-XX — Shared `at-config` library.** Two files: `at.toml` global, `modules.toml` per-module. TOML, read-only at runtime. No hot-reload.

**2026-XX-XX — Hybrid config: two files.** Global config changes rarely; module config changes often. Smaller tuning file is easier to edit.

## Voice & Personality

**2026-XX-XX — AT Vision Document v0.4 frozen.** One-note register: mild, gently sarcastic, fully servile disapproval. No emotions claimed, no subjectivity, no abstraction. 20 boundaries. Address: "sir."

**2026-XX-XX — Voice: Piper `en_GB-alan-medium`.** Closest to JARVIS (Paul Bettany). British male, measured, warm. Alternatives: northern_english_male (too regional), vctk (no clear analog).

**2026-XX-XX — `length_scale = 1.1`.** 10% slower than default. JARVIS never rushes. Tunable.

**2026-XX-XX — No exclamation marks, no apologies for limits.** Flat limit phrasings: "I can't do that, sir.", "That's beyond me, sir.", "I'd rather not, sir.", "Not something I do, sir."

**2026-XX-XX — 8 wake acknowledgments, weighted.** "Sir?" ≤30%, "On your command, sir." ≤5% (rarest). No back-to-back repeats. Shuffle bag, not random.

**2026-XX-XX — Boot greeting time-aware.** Good morning/afternoon/evening. Unclean shutdown → "I'm here, sir." Cold boot → "All systems nominal, sir." (20% chance).

**2026-XX-XX — Empty transcription: shuffle bag of 4.** "I didn't catch that, sir.", "Say again, sir.", "Nothing came through, sir.", "No audio, sir." No apology.

**2026-XX-XX — AT writes own name as `A.T.` with periods.** TTS preprocesses `A.T.` → `ay tee` for reliable pronunciation.

## Voice Loop

**2026-XX-XX — 16K context ceiling.** Fits in 8 GB without KV cache paging. 32K would risk paging under load; 8K is too conservative.

**2026-XX-XX — Session boundary: 30 min silence.** Fresh LLM context per session. Previous session preserved in SQLite.

**2026-XX-XX — Cooldown 400 ms.** Between AT speaking and wake re-arming. Prevents self-trigger from TTS tail.

**2026-XX-XX — Max user utterance 60 s.** Longer than that, the user is monologuing.

**2026-XX-XX — Max AT reply 150 tokens (400 on elaboration).** Enforces concise personality.

## Models

**2026-XX-XX — STT: whisper.cpp base.en.** Good balance for conversational speech. Alternatives: tiny.en (too inaccurate), small.en (too slow/RAM), multilingual (wasted capacity). Subprocess, not binding — cleaner isolation.

**2026-XX-XX — LLM: Qwen2.5 3B Instruct Q4_K_M.** Better at strict negative constraints than Llama 3.2. Llama is more naturally conversational but that's a liability for AT's register. Binding (`llama-cpp-python`) not subprocess — streaming matters.

**2026-XX-XX — TTS: Piper `en_GB-alan-medium`.** Subprocess, model stays in page cache.

**2026-XX-XX — Wake: openWakeWord, custom-trained.** Letter-by-letter "A.T." (A. T. with periods). Trained on user's voice + similar-word negatives.

**2026-XX-XX — VAD: Silero VAD.** Threshold 0.5, 750 ms silence, 250 ms min speech. Woken VAD only (not continuous).

## Infrastructure

**2026-XX-XX — Loop Controller as coordinator, not implementer.** Enforces thin controller: it coordinates, never implements module logic. Prevents monolith creep.

**2026-XX-XX — One correlation ID per command, per-request timeouts.** Matches responses to requests under concurrency.

**2026-XX-XX — Audio buffers bypass Loop Controller.** VAD → STT direct. Loop tells VAD to listen and STT to expect buffer. 1.9 MB blobs never touch the coordinator.

**2026-XX-XX — `paplay` over `pw-play`.** Sidesteps raw-PCM `--raw` gotcha. Latency difference irrelevant for AT. Robustness beats native purity.

**2026-XX-XX — Grace fade: 100 ms linear fade-out.** Prevents VAD/wake spikes from abrupt WAV ends.

**2026-XX-XX — Sentence chunking in Loop Controller, not TTS.** Accumulate tokens, split on punctuation, send complete sentences to TTS. Enables synthesis-during-playback pipeline.

**2026-XX-XX — WiFi hotspot + local audio server for cross-device streaming.** Later phase. No internet needed — phone connects to laptop's hotspot. Browser-based PWA client.

**2026-XX-XX — Bluetooth input: manual profile switching by user.** AT does not manage HFP/A2DP. In A2DP mode, headset has no mic; AT hears nothing. No error, no prompt.

## Agent Strategy

**2026-XX-XX — Tier map for AI agents.** Architectural (opus4.6, sonnet4.6, gemini3.8-flash) for at-ipc, loop, llm, at-config. Standard (gpt-oss120B, gemma4-36b-A4b, gemma4-31b) for audio-source, vad, stt, tts. Boilerplate (nemotron3, poolside, Inkling, lightning) for tests, docstrings, README.

**2026-XX-XX — Cross-family review mandatory.** When opus implements, gemini reviews. Same-family review reinforces blind spots.

**2026-XX-XX — `at-ipc` implementer: claude-opus4.6(thinking).** Concurrency-sensitive, foundational, small surface. Small models miss edge cases. Reviewer: gemini3.8-flash(effort=max).

**2026-XX-XX — Personality prompt stays local.** Sensitive content. Edit `personalities/personality.md` on the Dell or offline. Do not paste into cloud agents.

**2026-10-21 — at-ipc bug fixes and adversarial test coverage.**

Gemini 3.8 Flash (Antigravity, High effort) ran adversarial probes against
at-ipc and found 4 bugs:
1. drop_on_backpressure=True buffered frames before timeout, so frames were
   delivered not dropped. Fixed by checking is_congested() before write.
2. Disconnect callback fired twice on eviction. Fixed by removing the call
   from broadcast; _handle_client.finally is single source of truth.
3. BrokenPipeError from wait_closed() on dead socket. Fixed with try/except
   in wait_closed AND mapping transport OSError in read_message to
   IPCDisconnectError (extra root cause Gemini and Deepseek both found).
4. Silent clients appeared as peer_name="unknown" on disconnect. Fixed by
   IPCClient.connect() sending a hello event, consumed by _handle_client.

Sonnet 4.6 test-quality audit had predicted GAPs 2, 3, 5, 6, 7 — all four
of Gemini's findings fell within those predictions. This validates the
multi-model review pipeline: frontier test-quality review flags where bugs
are likely; adversarial probes then discover the specific bugs.

Deepseek V4.1 Flash (High effort) applied fixes and wrote 6 adversarial
tests. Deepseek also independently confirmed tests 2 and 3 fail against
pre-fix code (revert-test-revert methodology), proving the tests are
genuinely adversarial rather than confirmatory.

is_congested() does NOT check the write lock — lock is for atomicity,
congestion is only buffer backpressure. Clarified after Deepseek flagged
in its "least confident" section.

Final state: 61/61 tests passing, stable across 5 consecutive runs.



**2026-10-22 — Correction: audio-source was tested against real hardware.**

Deepseek's implementation report incorrectly stated the dev machine has no
physical soundcard and that `default` resolves to a virtual capture device.
Verification with `arecord -l` shows a real ALC256 Analog capture device
(card 0: PCH, HDA Intel PCH). Tests therefore exercised real ADC samples,
not a virtual device.

Implication: audio-source framing, subprocess lifecycle, and IPC broadcast
are proven against real hardware. The remaining uncertainty is acoustic
capture (does the mic pick up speech, or just room tone) — verify with
`arecord -d 3 /tmp/mic_test.wav && aplay /tmp/mic_test.wav` before Phase 8.

The 128ms buffering measurement and 66-67 frame count concern stand — they
were measured on real hardware, so they reflect real behavior.



**2026-10-24 — VAD chunking: 30ms frames with 512-sample rolling window.**

Silero VAD is trained on 512-sample (32ms) chunks. AT's audio pipeline
produces 960-byte (480-sample, 30ms) frames. The detector maintains a
rolling 512-sample buffer and scores every 30ms step (32-sample overlap).

This is a deliberate adaptation, not Silero's canonical chunking. It works
for AT (unit tests pass, real model inference passes on synthetic WAVs) but
real-speech sensitivity may differ slightly from upstream.

Verify on real audio before Phase 8. If detection quality is poor, the fix
is either (a) reframe audio to 512-sample chunks in audio-source, or (b)
adjust Silero's threshold to compensate.

**2026-10-30 — STT module complete.**

Modules built: transcriber (PCM→WAV wrap, subprocess whisper-cli, timing
parse), main (IPC service on stt.sock). Model = ggml-base.en-q5_1.bin.
Flags = -t 4 -bs 1 -nt -l en. Timeout = 15s.

Testing: 13 adversarial tests covering WAV validity, text parse, timing
parse, timeout, nonzero exit, missing model, temp cleanup, empty text,
IPC handler, empty flag, real-model hello world, silence, malformed input.

Three test-bugs found and fixed during implementation (missing IPCClient
arg, wrong method name, startup race) — none indicated code bugs.

Latency: dev machine ~4.5s for 3s audio (RTF 1.2). Dell projection
~2.0-2.5s. Loop turn will be ~6-7s end-of-speech to first audio.

Deferred to Phase 8:
- Long-utterance timing verification (30-60s inputs)
- Dell hardware benchmarking
