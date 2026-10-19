# at-ipc — IPC Protocol and Shared Library

**Version:** 0.1 (frozen)
**Status:** Foundation. Every service depends on this.

---

## Purpose

Provide async Unix domain socket communication between AT services. Standardize the message envelope, the ready protocol, command/response correlation, and event broadcast. Support both JSON control messages and raw-byte audio payloads.

## Scope

**In scope:**
- Async Unix socket server
- Async Unix socket client
- Message envelope encode/decode
- Correlation ID tracking
- Ready protocol
- Disconnect detection
- Request/response with timeout
- Event broadcast
- JSON and raw-byte payloads

**Out of scope:**
- AT-specific message names
- Reconnection logic
- Message persistence or queues
- Encryption (Unix sockets are local-only)
- Network transports

## Transport

Unix domain sockets at `/run/at/{module}.sock`. Permissions `0600`, owned by AT user.

## Message Envelope

Every control message is a single JSON line (newline-terminated):

| Field | Type | Notes |
|---|---|---|
| `msg_id` | UUID string | Unique per message |
| `correlation_id` | UUID string | Empty for events, populated for commands and their responses |
| `from` | string | Module name |
| `to` | string | Module name |
| `type` | enum | `event`, `command`, `response`, `error` |
| `name` | string | Specific message name |
| `timestamp` | ISO 8601 UTC | |
| `payload` | object | Schema depends on message name |

## Audio Payload Format

For payloads >64 KB (audio buffers):

1. JSON header line, newline-terminated, with the envelope above plus `bytes: <N>`.
2. Exactly N raw bytes follow.

Receiver reads the header, then reads exactly N bytes. No encoding overhead.

## Ready Protocol

Every service emits `ready` on its socket immediately after startup. Loop Controller waits for `ready` from all services before entering first IDLE.

## Death Protocol

Socket disconnect = module dead. Client and server both detect. Application logs, marks dead, handles recovery.

## API Surface (conceptual)

**Server:**
- Start listening on socket path
- Register handler for incoming messages by name
- Auto-respond to commands if handler returns value
- Emit `ready` once listening
- Notify app on client disconnect
- Clean shutdown on SIGTERM

**Client:**
- Connect to socket path
- Send command, await response with timeout
- Send event (fire and forget)
- Subscribe to incoming events
- Notify app on server disconnect
- Raise clear exception on failure

## Timeouts

- Command response: 10 s default, configurable per call
- Connection attempt: 2 s
- Read assembly: 30 s

## Constraints

- Python 3.12, asyncio
- **Stdlib only** — `asyncio`, `json`, `uuid`, `datetime`, `pathlib`, `logging`, `socket`, `signal`
- No busy loops, no polling
- No hidden threads
- Slow clients handled via backpressure, not blocking

## Acceptance Criteria

1. Two services exchange commands and responses
2. 100 concurrent commands correlate correctly
3. `ready` emitted before first command accepted
4. Non-existent socket raises clear error within 2 s
5. Server kill → client detects within 100 ms
6. Client kill → server detects within 100 ms
7. 2 MB audio payload intact
8. Malformed JSON logged, server continues
9. Command timeout raises exception carrying `correlation_id`
10. 1000 sequential events delivered without loss
11. SIGTERM closes cleanly, no fd leaks
12. 24 hours, no memory/fd/task leaks

## Non-Goals

- No AT-specific names
- No reconnection
- No queues
- No topic filtering
- No auth
- No compression
- No metrics