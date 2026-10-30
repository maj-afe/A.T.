# stt — Speech-to-Text

`stt` is the speech-to-text bridge connecting the AT voice loop to the language model. It wraps `whisper.cpp` (`whisper-cli`) as a subprocess, converting 16 kHz 16-bit mono PCM utterances into text with full performance metrics.

---

## Architecture

- **`transcriber.py`** — `WhisperTranscriber`:
  - Receives PCM bytes (or pre-formatted WAV).
  - Validates audio format (16 kHz, mono, 16-bit PCM).
  - Wraps raw PCM in a valid 44-byte WAV header (RIFF, WAVE, fmt chunk, data chunk).
  - Writes audio to a temporary file (`tempfile.NamedTemporaryFile` with `.wav` suffix).
  - Executes `whisper-cli` via `subprocess.run` with a 15-second timeout.
  - Parses transcription text from `stdout` (normalizing whitespace).
  - Parses timing metrics (`encode time`, `decode time`, `total time`) from `stderr`.
  - Computes Real-Time Factor (RTF = total time / utterance duration).
  - Safely unlinks the temporary file in a `finally` block on success or failure.
  - Returns `TranscriptionResult(text, duration_ms, encode_ms, decode_ms, total_ms, rtf)`.
  - Raises `STTError` on missing model, missing binary, non-zero process exit, or timeout.

- **`main.py`** — `STTService`:
  - Reads configuration via `at_config` (`stt.model_path`, `stt.threads`, `stt.beam`, `stt.language`).
  - Starts an `IPCServer("stt")` listening on `/run/at/stt.sock` (overridable via `--socket-path`).
  - Registers handler for the `transcribe_pcm` command.
  - Returns `{"text": ..., "metrics": {...}}` or `{"text": "", "empty": true, "metrics": {...}}` if blank/whitespace.
  - Logs each transcription with all 7 metrics: duration, encode time, decode time, total time, RTF, text length, empty flag.
  - Cleanly shuts down on `SIGTERM` / `SIGINT` with exit code 0.

---

## Subprocess Parameters

Benchmarked and settled configuration:
- **Model**: `ggml-base.en-q5_1.bin` (quantized, 57 MB)
- **Flags**: `-m <model> -t 4 -bs 1 -nt -l en`
  - `-t 4`: 4 threads for computation
  - `-bs 1`: beam size 1 (greedy search)
  - `-nt`: no timestamps in text output
  - `-l en`: forced English transcription
  - Note: `-np` is omitted to preserve timing output on `stderr` for metrics logging.
- **Timeout**: 15 seconds (measured dev-machine STT is ~4.5s; 15s gives 3x headroom).

---

## Input Contract

- **Audio format**: 16 kHz mono 16-bit S16_LE PCM.
- When raw PCM is received, `WhisperTranscriber` automatically adds the 44-byte WAV header.
- Pre-wrapped WAV files (starting with `RIFF`) are validated for 16 kHz mono 16-bit structure.
- Malformed audio or unsupported sample rates raise `STTError`.

---

## Command Interface

### `transcribe_pcm`

- **Sender**: Loop Controller
- **Buffer**: Sent as `raw_data` bytes.
- **Success Response Payload**:
  ```json
  {
    "text": "Hello world",
    "metrics": {
      "duration_ms": 1000,
      "encode_ms": 4455,
      "decode_ms": 20,
      "total_ms": 4500,
      "rtf": 4.5
    }
  }
  ```
- **Empty Response Payload**:
  ```json
  {
    "text": "",
    "empty": true,
    "metrics": {
      "duration_ms": 1000,
      "encode_ms": 4455,
      "decode_ms": 0,
      "total_ms": 4455,
      "rtf": 4.455
    }
  }
  ```
- **Error Response**:
  If `whisper-cli` fails or times out, an IPC error response is returned with the error message and exception type.
