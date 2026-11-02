# LLM Module

LLM runtime service for the AT voice assistant using `llama-cpp-python` and Qwen2.5 3B Instruct Q4_K_M.

## Architecture

- **Engine**: `llama-cpp-python` resident in-process binding
- **Model**: `qwen2.5-3b-instruct-q4_k_m.gguf` (~2.1 GB)
- **Context window**: 16,384 tokens
- **IPC**: Unix domain socket at `/run/at/llm.sock`
- **Session context**: SQLite-backed history with sliding window trimming (anchor + recent turns)
- **Prompt assembly**: Loaded from `personalities/personality.md` at boot

## Configuration

```toml
[llm]
model_path = "/opt/at/models/llm/qwen2.5-3b-instruct-q4_k_m.gguf"
threads = 4
context_window = 16384

[llm.sampling]
temperature = 0.5
top_p = 0.9
top_k = 40
repetition_penalty = 1.1

[llm.budgets]
max_tokens = 150
max_tokens_elaboration = 400
```

| Parameter | Description | Default |
|-----------|-------------|---------|
| `model_path` | Path to GGUF model file | required |
| `threads` | CPU inference threads | 4 |
| `context_window` | KV cache context window size | 16384 |
| `temperature` | Sampling temperature | 0.5 |
| `top_p` | Nucleus sampling probability | 0.9 |
| `top_k` | Top-k sampling limit | 40 |
| `repetition_penalty`| Repetition penalty | 1.1 |
| `max_tokens` | Max generation tokens (standard) | 150 |
| `max_tokens_elaboration` | Max tokens when user asks for elaboration | 400 |

## Prompt Structure & Trimming

- **System prompt**: System prompt loaded once from `personalities/personality.md`.
- **Sliding window budget**: 13,000 token context budget.
- **Trimming strategy**:
  1. Preserves first 3 turns as anchor context.
  2. Preserves most recent turns up to budget.
  3. Drops middle turns when exceeding 13K tokens.
- **Elaboration detection**: Flags elaboration when message contains "elaborate", "in detail", or "explain fully", expanding `max_tokens` to 400.

## Output Post-Processing

- Replaces exclamation marks `!` with `.`.
- Normalizes mid-sentence capitalized `Sir` to lowercase `sir`.
- Strips leading AI disclaimers and banned apology phrases (e.g., "As an AI, ", "I'm sorry, but ").
- Enforces maximum sentence cap (default 3 sentences).
- Preserves punctuation boundaries (`.` and `?`).
- **Refusal shuffler**: Detects repeated identical refusal phrasings within the same session and randomly swaps to another in-register refusal phrasing.

## IPC Interface

### Command: `generate`

Request:
```json
{
  "user_text": "What's the time?",
  "session_id": "optional-session-id"
}
```

Response:
```json
{
  "text": "I can't see a clock, sir.",
  "metrics": {
    "prompt_tokens": 1150,
    "completion_tokens": 8,
    "time_to_first_token_ms": 320.5,
    "total_time_ms": 850.2,
    "tokens_per_second": 9.4,
    "truncated": false,
    "shuffled": false,
    "banned_stripped": false,
    "timed_out": false,
    "elaboration": false
  },
  "elaboration": false
}
```

### Events

- `llm_ready`: Broadcast on service startup after warmup inference.
- `llm_token_stream`: Broadcast per-token: `{"token": str, "index": int}`.
- `llm_response_ready`: Broadcast on completion: `{"text": str, "metrics": {...}, "elaboration": bool}`.
- `llm_timeout`: Broadcast when inference reaches 90-second timeout ceiling.

## Running

```bash
# Service mode
python -m llm.main

# Smoke test (single generation and print to stdout)
python -m llm.main --smoke "What's the time?"

# Custom socket
python -m llm.main --socket-path /tmp/llm.sock

# Custom model
python -m llm.main --model-path models/llm/qwen2.5-3b-instruct-q4_k_m.gguf
```

## Signal Handling

- `SIGTERM`: Finishes current generation (up to 30s grace period), flushes SQLite turns, unlinks socket, exits 0.
