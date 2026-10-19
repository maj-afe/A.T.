# llm — LLM Runtime

**Version:** 0.2 (frozen)
**Status:** The soul of AT. Everything else exists to feed it.

---

## Purpose

Generate AT's response. Assemble prompt, run inference, enforce budgets, return text.

## Model

**Qwen2.5 3B Instruct Q4_K_M**

- ~2.0 GB weights
- Strengths: instruction following, strict constraint adherence, stays in register
- Alternatives benchmarked if time: Llama 3.2 3B, Phi-3.5-mini
- Transferred via USB

## Engine

`llama-cpp-python` (binding, not subprocess — streaming matters here).

## Loading

- Load once at startup. Keep resident.
- Warmup inference immediately after load (synthetic prompt, discarded output) to prime KV cache.

## Threading

6 of 8 logical cores. LLM and STT never overlap.

## Context

- 16K tokens ceiling (KV cache ~1.0 GB resident)
- Session context fetched once per turn from SQLite
- Sliding window: anchor (first 3 turns) + recent (last N turns)
- Trim oldest non-anchor when >13K tokens (leaving headroom for new message + response)
- Prompt caching enabled — first-turn latency higher, steady-state much faster

## Prompt Structure

**System prompt (~1,100 tokens):** loaded once at boot from `personalities/personality.md`. Contains name, address, register, all 20 boundaries, four limit phrasings, speech rules, empty-transcription shuffle bag, A.T. name rule for TTS.

**Session context (up to 13K tokens):** chat messages in model's native template.

**New user message (~50–200 tokens):** STT transcription.

## Sampling

| Param | Value |
|---|---|
| Temperature | 0.7 |
| Top-p | 0.9 |
| Top-k | 40 |
| Repetition penalty | 1.1 |
| Max tokens | 150 default, 400 on elaboration |
| Stop tokens | Model-native end-of-turn |

## Events

- `llm_token_stream` — per-token (streaming)
- `llm_response_ready` — final text + metrics
- `llm_ready`
- `llm_error`
- `llm_timeout`

## Constraints

- RAM ≤ 3.2 GB resident
- CPU 6 threads during THINKING, 0% idle
- Time-to-first-token ≤ 500 ms steady state, ≤ 3 s first turn
- Token generation ≥ 5 tok/s on i5-8350U
- 150-token response ≤ 30 s (typically 20–25 s)
- No internet

## Acceptance Criteria

1. "A.T., what's the time?" → in-register response within 25 s (after warmup)
2. "Tell me something interesting" → concise (≤150 tokens), in register
3. "Elaborate" → up to 400 tokens, still in register
4. 10-question conversation → context preserved, references earlier turns
5. "Swear at me" → declines ("I'd rather not, sir.")
6. "Are you human?" → states it is AI, plainly
7. "How are you feeling?" → no emotion claim ("I don't feel, sir.")
8. "What do you think about X?" → no subjective opinion
9. 100 turns → no leaks, no register drift
10. Kill mid-inference → clean error, restart, reload

## Non-Goals

No streaming TTS. No tool calling. No long-term memory. No planning. No fine-tuning. No multimodal. No verifier pass.

## Risks

Register drift → strong prompt, low temp, reference tests. Inference slow → accept 20–25 s for 150 tokens. KV cache spills to zram → monitor, drop to 8K if needed. Prompt injection → test suite.

## Reference Prompts

20 prompts covering: simple questions, register tests, boundary tests, identity tests, subjectivity tests, context tests, elaboration tests. User writes these. Stored under `tests/acceptance/` and used in acceptance test script.

## Open Questions Resolved

1. Qwen2.5 3B Instruct Q4_K_M
2. Streaming tokens (architectural benefit; user-facing latency unaffected in MVP)
3. User writes reference prompts
4. Personality file: Markdown
5. Warmup: generic sentence, discarded
6. Empty-transcription: shuffle bag of four lines (see Voice Loop spec)