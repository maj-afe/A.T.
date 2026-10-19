# audio-source — Microphone Gateway

**Version:** 0.1 (frozen)
**Status:** First service in the chain.

---

## Purpose

Own the microphone. Read continuously. Frame audio. Fan out to subscribers via Unix socket.

## Inputs

ALSA default capture device, 16 kHz mono 16-bit PCM.

## Outputs

30 ms framed PCM chunks (960 bytes each), broadcast to all subscribers.

## Socket

`/run/at/audio-source.sock`

## Behavior

- Opens device at startup. Retries every 2 s if unavailable.
- Reads continuously. Frames into 480-sample chunks.
- Broadcasts each frame to all connected subscribers.
- With no subscribers: still reads, discards frames. Keeps device hot.
- On unplug: logs error, retries. OS handles default device switch.
- On new subscriber connect: sends `ready`, then begins frames.

## Constraints

- RAM ≤ 15 MB
- CPU ≤ 2% streaming, ≤ 1% idle
- Latency ≤ 10 ms audio-to-fanout
- No device selection, no volume, no resampling, no echo cancellation

## Acceptance Criteria

1. Mic plugged in at start → frames within 1 s
2. Subscriber connects → first frame within 100 ms
3. Subscriber disconnects → others unaffected
4. Device unplugged mid-stream → logged, retries, auto-recovers
5. 24 hours → no growth, no drops

## Non-Goals

No device selection. No volume. No resampling. No echo cancellation. No noise suppression.

## Risks

ALSA "device busy" if another app holds the mic. Log clearly. systemd restart. OS-level issue, not AT's.