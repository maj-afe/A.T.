# audio-source — Microphone Gateway

`audio-source` is the first service in the AT voice loop. It owns the ALSA capture device, reads continuously, frames the stream into 30 ms PCM chunks, and fans them out to every subscriber over a Unix domain socket.

---

## Design

- **arecord subprocess, stdlib only.** No Python ALSA bindings; the capture command is:
  ```
  arecord -D default -f S16_LE -r 16000 -c 1 -t raw -q
  ```
- **Raw PCM over stdout.** Reads arrive in arbitrary chunk sizes (pipe buffering, ALSA periods), so `FrameAssembler` re-aligns them into exact 960-byte frames — 480 samples × 2 bytes = 30 ms at 16 kHz mono 16-bit.
- **No partial frames, ever.** A trailing partial frame is discarded at stream end (device unplug, arecord crash, SIGTERM) and is never broadcast. Partial frames are also never emitted mid-stream.
- **Real-time fanout.** Each frame is broadcast as an `audio_frame` event with `drop_on_backpressure=True`: a slow subscriber loses frames instead of stalling the microphone.
- **Hot device.** With no subscribers the service keeps reading and discards frames, so the first subscriber gets audio immediately.
- **Self-healing.** arecord start failures and unexpected exits are logged and retried every 2 s.

## Socket

`/run/at/audio-source.sock` (override with `--socket-path`). On connect, at-ipc's ready protocol sends `ready` before frames begin.

## Events

| Event | Payload | Description |
|---|---|---|
| `audio_frame` | 960 raw bytes | One 30 ms PCM frame (S16_LE, 16 kHz, mono) |

## Shutdown

SIGTERM/SIGINT terminates arecord cleanly (SIGTERM, escalating to SIGKILL after 2 s), stops the IPC server, unlinks the socket, and exits 0.

## Files

- `capture.py` — arecord subprocess lifecycle and fixed-size framing (`FrameAssembler`, `AudioCapture`)
- `main.py` — service loop, signal handling, CLI (`AudioSourceService`, `main`)
- `__init__.py` — public exports

## Run

```
PYTHONPATH=src python -m audio_source.main
```

Options: `--device` (ALSA device, default `default`), `--socket-path` (default `/run/at/audio-source.sock`).

## Tests

```
pytest tests/unit/test_framing.py tests/integration/test_capture.py -v
```

`tests/unit/test_framing.py` is pure logic (no subprocess). `tests/integration/test_capture.py` drives arecord for real and is skipped when `arecord` or a default capture device is unavailable.
