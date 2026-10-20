# at-ipc — Final Implementation Plan

> [!IMPORTANT]
> This document contains three sections: (A) item-by-item evaluation of Gemini's critique, (B) holistic evaluation of Gemini's revised plan, and (C) the final consolidated plan ready for implementation approval.

---

# Part A — Critique Evaluation

## 1. Race Condition in `wait_ready()` — **ACCEPT**

Gemini is right. My original plan instantiated `_ready_event` inside `wait_ready()`, but the `_read_loop` starts in `connect()`. The server sends `ready` immediately on connection — the read loop would receive it before `wait_ready()` is ever called, and the event would be lost. Then `wait_ready()` would create a brand-new unset `Event` and hang until timeout.

**Fix adopted:** `self._ready_event = asyncio.Event()` is initialized in `__init__`. The read loop sets it; `wait_ready()` just waits on it. If `ready` arrived earlier, `wait()` returns instantly. Clean and correct.

---

## 2. Stream Corruption Under Concurrency — **MODIFY**

Gemini correctly identified the risk, but the technical reasoning is slightly off. In asyncio's single-threaded model, `writer.write()` is synchronous — it buffers data without yielding. Two back-to-back `write()` calls (header + raw bytes) with no `await` between them are effectively atomic. The data lands in the transport's write buffer in one contiguous block. Interleaving can only happen at `await` points, and the first `await` in my original `write_message` was `drain()`, which comes *after* both writes.

So the original code would not produce interleaved bytes on the wire. Two concurrent tasks would produce `[header_A][raw_A][header_B][raw_B]` — each message intact.

**However, I am still adopting the lock.** Reasons:

1. **Future-proofing.** If someone adds an `await` between writes (e.g., chunked streaming for very large payloads), the invariant breaks silently. The lock makes correctness obvious rather than subtle.
2. **Serialized drain.** Without the lock, 100 concurrent `send_command` calls all buffer their writes, then 100 `drain()` calls run concurrently. The kernel handles this fine, but serialized writes produce more predictable flow control behavior.
3. **Cheap.** An uncontended `asyncio.Lock` is ~2 µs. Contended, it serializes, which is exactly what we want.

**What I'm changing vs. Gemini's version:** Nothing functional — I'm adopting `ConnectionWriter` with the lock. But I want the plan to record *why* it's defense-in-depth, not a critical fix, so we don't make wrong assumptions about asyncio's memory model later.

---

## 3. Infinite 100% CPU Busy Loop on Client Disconnect — **ACCEPT**

Gemini is right and this is the most dangerous bug in the original plan. The chain is exactly as described:

1. Client disconnects → `reader.readline()` returns `b""`.
2. `json.loads("")` raises `JSONDecodeError`.
3. My parser wraps it as `IPCProtocolError`.
4. Server read loop catches `IPCProtocolError`, logs, calls `continue`.
5. `readline()` immediately returns `b""` again → 100% CPU infinite loop.

This would have been a production kill. The EOF check must come before any parsing:

```python
line = await reader.readline()
if not line:
    raise IPCDisconnectError("Connection closed by peer (EOF)")
```

**Fix adopted as-is.**

---

## 4. Server Cannot Broadcast Audio Frames — **ACCEPT**

Gemini is right. My original `broadcast()` signature was `(name: str, payload: dict)` — no `raw_data` parameter. The [audio-source spec](file:///home/aniruddh_sen/Documents/Kate/at/specs/04-audio-source.md) explicitly says it "broadcasts each frame to all connected subscribers" and frames are 960-byte raw PCM chunks.

Even though 960 bytes is under the 64 KB JSON threshold, encoding raw PCM as base64 inside JSON adds ~33% overhead at 33 frames/second. The spec's design intent (from [decisions.md](file:///home/aniruddh_sen/Documents/Kate/at/docs/decisions.md): "JSON control + raw byte audio") is clear: audio always uses the raw-byte format.

**Fix adopted:** `broadcast(name, payload={}, raw_data=None)`.

---

## 5. Broadcast Backpressure Blocking the Server — **ACCEPT**

Gemini is right. Sequential `await write_message(client)` for each subscriber means one stalled client blocks the broadcast loop. For audio-source at 33 Hz, this would cause frame drops and latency spikes for all subscribers.

**Fix adopted:** `asyncio.gather(*[write_to_client(c) for c in clients], return_exceptions=True)` with failed/slow clients evicted and closed. I'll add a per-client broadcast write timeout (short, e.g., 1 second) so truly stuck clients don't stall the gather indefinitely — `wait_for` each individual write inside the gather.

---

## 6. Task Leaks & Silent GC Cancellation — **ACCEPT**

Gemini is right. This is a well-documented Python 3.12+ behavior: `asyncio.create_task()` returns the only reference; if the caller discards it, the garbage collector can destroy the running task. My original plan used fire-and-forget `asyncio.create_task(handler(...))` for event dispatch without retaining references.

**Fix adopted:** Strong reference set `_background_tasks` with `add_done_callback(discard)` for automatic cleanup. Both `server.stop()` and `client.close()` cancel and await all tracked tasks.

---

## 7. Memory Leak on Command Cancellation — **ACCEPT**

Gemini is right. My original code:

```python
try:
    response = await asyncio.wait_for(future, timeout=timeout)
except asyncio.TimeoutError:
    del self._pending[correlation_id]
    raise IPCTimeoutError(...)
return response
```

If the caller cancels the outer coroutine (e.g., `task.cancel()`), `CancelledError` propagates without hitting the `except asyncio.TimeoutError` block. The `self._pending[correlation_id]` entry is never cleaned up — a memory leak that accumulates over 24 hours.

**Fix adopted:** `self._pending.pop(correlation_id, None)` in a `finally` block.

---

## 8. SIGTERM & Signal Handler Test Pollution — **ACCEPT**

Gemini is right. In the integration test, two servers (ping and pong) run in the same event loop. Each calling `loop.add_signal_handler(signal.SIGTERM, ...)` overwrites the previous handler. After tests finish, the stale handler remains active, potentially interfering with the test runner.

**Fix adopted:**
- `install_signal_handlers` parameter on `IPCServer.__init__`, defaults to `True`.
- Test fixtures pass `install_signal_handlers=False`.
- `stop()` calls `loop.remove_signal_handler(signal.SIGTERM)` if it installed one.

---

## 9. Python 3.12 Deprecation — **ACCEPT**

Gemini is right. `datetime.datetime.utcnow()` raises `DeprecationWarning` in Python 3.12.

**Fix adopted:** `datetime.datetime.now(datetime.timezone.utc).isoformat()`.

---

# Part B — Holistic Evaluation of Gemini's Revised Plan

## What Gemini got right

All 9 fixes are correctly integrated. The `ConnectionWriter` abstraction is clean. The `_background_tasks` lifecycle pattern is correct. The stale socket probe on startup is a good addition my original plan lacked.

## Problems introduced by Gemini's revised plan

### B1. Missing `send_event()` and `send_audio()` from client public API

My original plan had three distinct client send methods:

| Method | Purpose |
|---|---|
| `send_command(name, payload, timeout)` | Send command, await correlated response |
| `send_event(name, payload)` | Fire and forget, no correlation, no await |
| `send_audio(name, payload_header, raw_data)` | Send raw-byte payload |

Gemini's revised plan lists `send_command(name, payload={}, timeout=10.0, raw_data=None)` but never explicitly defines `send_event()` or `send_audio()` in the client API. The `_read_loop` dispatches events but there's no documented way to *send* them. This is a significant omission — the Loop Controller sends fire-and-forget events constantly (e.g., `state_changed`, `mute`/`unmute` commands to wake-word).

**Fix:** Restore explicit `send_event(name, payload={}, raw_data=None)` to the client API. Collapse `send_audio` into `send_event` since it's just `send_event` with `raw_data` — no separate method needed.

### B2. Concurrent dispatch changes ordering guarantees (undocumented)

My original plan awaited `_dispatch()` inline in the read loop — commands from the same client were processed sequentially. Gemini's revised plan spawns each dispatch as a separate task. This means:

- Two commands from the same client can execute their handlers concurrently.
- Responses may arrive out of order relative to send order.

This is probably fine (correlation IDs handle matching), and it's better for throughput under 100 concurrent commands. But it's a behavioral change that should be documented explicitly, because a handler that modifies shared server state could race against itself.

**Fix:** Document this in the plan. Note that handlers are responsible for their own synchronization if they touch shared state.

### B3. `to_module="*"` for broadcasts is a protocol convention that needs documenting

Gemini's plan uses `to_module="*"` for broadcast envelopes. The spec says `to` is a "module name string." Using `"*"` is a reasonable convention but it's an invention — downstream services might validate that `to` matches their own module name and reject broadcasts.

**Fix:** Document `"*"` as the canonical broadcast target. Clients should accept messages where `to` is either their module name or `"*"`.

### B4. `IPCResponseError` auto-raise is new behavior that changes the return contract

Gemini added `IPCResponseError` and has `send_command()` auto-raise when the response envelope has `type == "error"`. My original plan returned the envelope and let the caller inspect it. Gemini's approach is arguably better (callers can't silently ignore errors), but it changes the contract: `send_command()` now either returns a `response` envelope or raises. The caller must use try/except rather than checking `response.type`.

**Assessment:** I'll adopt this. It's cleaner API design — errors are exceptional, not a return variant. The `error_payload` attribute on the exception preserves access to the error details.

### B5. No explicit `on_disconnect` registration method shown for server

Gemini's plan mentions `_on_disconnect_cb` as internal state but doesn't show the public API method to register it. My original plan had:

```python
def on_disconnect(self, callback: Callable[[str], Awaitable[None]]) -> None
```

**Fix:** Restore this in the public API table.

### B6. Server handler registration `on()` method not explicitly shown

Similarly, the `on(name, handler)` method is mentioned in flows but not in a public API summary table.

**Fix:** Add a complete public API table for both server and client.

---

# Part C — Final Consolidated Plan

## 1. File Structure

```
src/at_ipc/
├── __init__.py          # Public API re-exports
├── envelope.py          # Message envelope: dataclass, encode, decode, validation
├── protocol.py          # Wire protocol: framing, reading, writing, ConnectionWriter
├── server.py            # IPCServer: listen, accept, dispatch, ready, broadcast, shutdown
├── client.py            # IPCClient: connect, send_command, send_event, subscribe
├── errors.py            # Exception hierarchy
└── _constants.py        # Shared constants (timeouts, size thresholds, socket dir)

tests/
├── unit/
│   ├── test_envelope.py
│   ├── test_protocol.py
│   ├── test_server.py
│   └── test_client.py
├── integration/
│   └── test_ping_pong.py
└── conftest.py
```

Five source modules, each under 300 lines. `envelope.py` and `protocol.py` are pure logic — easy to test without real sockets. `server.py` and `client.py` own all asyncio I/O. `errors.py` is tiny but avoids circular imports.

---

## 2. Module Breakdown

### 2.1 `errors.py` — Exception Hierarchy

```
IPCError (base)
├── IPCConnectionError    — socket connect failure, target not found, connection timeout
├── IPCDisconnectError    — peer dropped unexpectedly, clean EOF
├── IPCTimeoutError       — command response timeout (carries correlation_id)
├── IPCProtocolError      — malformed envelope, truncated payload, bad JSON
├── IPCSendError          — write to closed/broken socket
└── IPCResponseError      — remote returned error envelope (carries correlation_id + error_payload)
```

Key attributes:
- `IPCTimeoutError.correlation_id: str | None` — identifies which request timed out
- `IPCResponseError.correlation_id: str | None` and `error_payload: dict` — carries the remote error details

---

### 2.2 `_constants.py`

```python
from pathlib import Path

DEFAULT_SOCKET_DIR = Path("/run/at")
SOCKET_PERMISSIONS = 0o600
DEFAULT_COMMAND_TIMEOUT = 10.0      # seconds
CONNECTION_TIMEOUT = 2.0            # seconds
READ_TIMEOUT = 30.0                 # seconds
MAX_JSON_SIZE = 64 * 1024           # 64 KB — spec threshold
MAX_PAYLOAD_SIZE = 10 * 1024 * 1024 # 10 MB — defensive cap on bytes_length
STREAM_READER_LIMIT = 1_048_576     # 1 MB buffer limit for StreamReader
MSG_TYPES = frozenset({"event", "command", "response", "error"})
BROADCAST_TARGET = "*"              # canonical "to" value for broadcast envelopes
```

---

### 2.3 `envelope.py`

**Dataclass: `Envelope`**

| Field | Type | Default | JSON Key |
|---|---|---|---|
| `msg_id` | `str` | UUID4 | `msg_id` |
| `correlation_id` | `str` | `""` | `correlation_id` |
| `from_module` | `str` | Required | `from` |
| `to_module` | `str` | Required | `to` |
| `type` | `str` | Required | `type` |
| `name` | `str` | Required | `name` |
| `timestamp` | `str` | UTC ISO 8601 | `timestamp` |
| `payload` | `dict` | `{}` | `payload` |
| `bytes_length` | `int | None` | `None` | `bytes` (omitted if None) |

`from` is a Python keyword — the field is `from_module`, serialized as `"from"`.

Timestamps use `datetime.now(timezone.utc).isoformat()` (Python 3.12 compatible, no deprecated `utcnow()`).

**Functions:**

- `create_envelope(from_module, to_module, type, name, payload={}, correlation_id="", bytes_length=None) → Envelope`
- `envelope_to_dict(env: Envelope) → dict` — serializes, mapping field names to JSON keys, omitting `bytes` if None
- `dict_to_envelope(d: dict) → Envelope` — deserializes, validates required fields, raises `IPCProtocolError` on missing/invalid
- `validate_envelope(env: Envelope) → None` — checks type ∈ MSG_TYPES, msg_id present, timestamp present, bytes_length ≤ MAX_PAYLOAD_SIZE if set

---

### 2.4 `protocol.py` — Wire Framing

**`ConnectionWriter`** — wraps a `StreamWriter` with an `asyncio.Lock` to guarantee atomic multi-part writes. This is defense-in-depth: in asyncio's single-threaded model, synchronous `write()` calls don't interleave. The lock protects against future refactors that might add `await` points between writes, and serializes `drain()` calls for predictable flow control.

```python
class ConnectionWriter:
    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self._writer = writer
        self._lock = asyncio.Lock()

    async def write_message(self, envelope: Envelope, raw_data: bytes | None = None) -> None:
        if raw_data is not None:
            envelope.bytes_length = len(raw_data)

        header_line = json.dumps(envelope_to_dict(envelope), separators=(',', ':')).encode('utf-8') + b'\n'

        async with self._lock:
            try:
                self._writer.write(header_line)
                if raw_data is not None:
                    self._writer.write(raw_data)
                await self._writer.drain()
            except (ConnectionResetError, BrokenPipeError, OSError) as exc:
                raise IPCSendError(f"Write failed: {exc}") from exc

    def close(self) -> None:
        self._writer.close()

    async def wait_closed(self) -> None:
        await self._writer.wait_closed()
```

**`read_message`** — reads one complete message from a `StreamReader`. Handles EOF, JSON parsing, optional raw bytes.

```python
async def read_message(reader: asyncio.StreamReader) -> tuple[Envelope, bytes | None]:
    # Step 1: Read newline-terminated header line
    try:
        line = await asyncio.wait_for(reader.readline(), timeout=READ_TIMEOUT)
    except asyncio.TimeoutError:
        raise IPCTimeoutError("Read timed out")
    except ConnectionResetError:
        raise IPCDisconnectError("Connection reset by peer")

    # CRITICAL: EOF check before parsing — prevents 100% CPU spin loop
    if not line:
        raise IPCDisconnectError("Connection closed (EOF)")

    # Step 2: Parse JSON header
    try:
        d = json.loads(line)
        envelope = dict_to_envelope(d)
    except (json.JSONDecodeError, UnicodeDecodeError, IPCProtocolError) as exc:
        raise IPCProtocolError(f"Malformed header: {exc}") from exc

    # Step 3: Read raw payload if specified
    raw_data = None
    if envelope.bytes_length is not None and envelope.bytes_length > 0:
        try:
            raw_data = await asyncio.wait_for(
                reader.readexactly(envelope.bytes_length), timeout=READ_TIMEOUT
            )
        except asyncio.IncompleteReadError as exc:
            raise IPCProtocolError(
                f"Truncated: expected {envelope.bytes_length} bytes, got {len(exc.partial)}"
            ) from exc
        except asyncio.TimeoutError:
            raise IPCTimeoutError(f"Timed out reading {envelope.bytes_length} byte payload")

    return envelope, raw_data
```

---

### 2.5 `server.py` — `IPCServer`

```python
class IPCServer:
    def __init__(
        self,
        module_name: str,
        socket_path: Path | None = None,
        install_signal_handlers: bool = True,
    ) -> None
```

**Public API:**

| Method | Signature | Description |
|---|---|---|
| `on` | `(name: str, handler: Callable) → None` | Register async handler for message name. Handler receives `(envelope, raw_data)` and may return `dict`, `tuple[dict, bytes]`, or `None`. |
| `on_disconnect` | `(callback: Callable[[str \| None], Awaitable[None]]) → None` | Register disconnect callback, receives peer module name. |
| `start` | `async () → None` | Bind socket, start serving, mark ready. |
| `stop` | `async () → None` | Close all connections, cancel tasks, unlink socket. |
| `broadcast` | `async (name: str, payload: dict = {}, raw_data: bytes \| None = None) → None` | Send event to all connected clients concurrently. |

**Internal state:**
- `_handlers: dict[str, Callable]`
- `_clients: dict[int, ConnectionWriter]` — `id(writer) → ConnectionWriter`
- `_client_peers: dict[int, str]` — `id(writer) → peer module name` (learned from first message)
- `_background_tasks: set[asyncio.Task]`
- `_server: asyncio.Server | None`
- `_ready: bool`
- `_running: bool`
- `_signal_installed: bool`

**`start()` flow:**

1. If `socket_path` exists: attempt connection. If accepted → raise `IPCError("Socket already in use")`. If `ConnectionRefusedError` → stale, unlink. If `FileNotFoundError` → fine.
2. Ensure parent directory exists (don't create `/run/at/` — systemd owns it).
3. `asyncio.start_unix_server(self._handle_client, path=self.socket_path, limit=STREAM_READER_LIMIT)`.
4. `os.chmod(self.socket_path, SOCKET_PERMISSIONS)`.
5. If `install_signal_handlers`: `loop.add_signal_handler(SIGTERM, ...)`, set `_signal_installed = True`.
6. Set `_ready = True`, `_running = True`.

**`_handle_client(reader, writer)` flow:**

1. Wrap: `conn_writer = ConnectionWriter(writer)`.
2. Register: `_clients[id(writer)] = conn_writer`.
3. Send `ready` event to this client (server is already ready; any client connecting after startup still needs to know).
4. Read loop:
   ```
   while self._running:
       try:
           envelope, raw_data = await read_message(reader)
       except IPCDisconnectError:
           break  # client gone
       except IPCProtocolError:
           log.warning("Malformed message, ignoring")
           continue  # safe: stream position is correct (full line was consumed)

       _client_peers[id(writer)] = envelope.from_module  # learn peer identity

       # Spawn dispatch as tracked task — concurrent command processing
       task = asyncio.create_task(self._dispatch(envelope, raw_data, conn_writer))
       self._background_tasks.add(task)
       task.add_done_callback(self._background_tasks.discard)
   ```
5. `finally`: remove from `_clients`, `_client_peers`, close writer, invoke `_on_disconnect_cb(peer_name)`.

> [!NOTE]
> **Concurrent dispatch:** Each inbound message dispatches as a separate task. This means multiple commands from the same client can execute their handlers concurrently and responses may arrive out of order. This is correct — correlation IDs handle matching. **Handlers that modify shared server state are responsible for their own synchronization.**

**`_dispatch(envelope, raw_data, conn_writer)` flow:**

1. Look up handler: `handler = self._handlers.get(envelope.name)`.
2. No handler + command → send error response with `correlation_id`.
3. No handler + event → log debug, drop.
4. Handler found:
   - `try: result = await handler(envelope, raw_data)`
   - If command:
     - `result` is `dict` → send response with `payload=result`.
     - `result` is `tuple[dict, bytes]` → send response with `payload=result[0]`, `raw_data=result[1]`.
     - `result` is `None` → send response with `payload={}`.
   - `except Exception as exc:` → log error, send error response if command.

Response envelope: `from_module=self.module_name`, `to_module=envelope.from_module`, `type="response"`, `correlation_id=envelope.correlation_id`.

**`broadcast(name, payload={}, raw_data=None)` flow:**

1. Build event envelope: `from_module=self.module_name`, `to_module="*"`, `type="event"`.
2. Fan out concurrently with per-client write timeout:
   ```python
   async def _write_to_client(cw: ConnectionWriter) -> None:
       await asyncio.wait_for(cw.write_message(envelope, raw_data), timeout=1.0)

   results = await asyncio.gather(
       *[_write_to_client(cw) for cw in self._clients.values()],
       return_exceptions=True
   )
   # Evict clients whose writes failed
   ```
3. Any client whose write raises (`IPCSendError`, `TimeoutError`, etc.) is closed and removed from `_clients`. Other clients are unaffected.

**`stop()` flow:**

1. Set `_running = False`.
2. If `_signal_installed`: `loop.remove_signal_handler(signal.SIGTERM)`.
3. `_server.close()`, `await _server.wait_closed()`.
4. Cancel all tasks in `_background_tasks`, `await asyncio.gather(*tasks, return_exceptions=True)`.
5. Close all client `ConnectionWriter`s.
6. Unlink `socket_path` (ignore `FileNotFoundError`).

**SIGTERM handling:**

```python
loop.add_signal_handler(signal.SIGTERM, lambda: asyncio.ensure_future(self.stop()))
```

---

### 2.6 `client.py` — `IPCClient`

```python
class IPCClient:
    def __init__(
        self,
        module_name: str,
        target_module: str,
        socket_path: Path | None = None,
    ) -> None
```

**Public API:**

| Method | Signature | Description |
|---|---|---|
| `connect` | `async () → None` | Connect with 2s timeout, start background read loop. |
| `close` | `async () → None` | Cancel read loop, cancel tasks, fail pending futures, close socket. |
| `send_command` | `async (name: str, payload: dict = {}, *, timeout: float = 10.0, raw_data: bytes \| None = None) → Envelope` | Send command, await correlated response. Raises `IPCTimeoutError` or `IPCResponseError`. |
| `send_event` | `async (name: str, payload: dict = {}, raw_data: bytes \| None = None) → None` | Fire and forget. No correlation, no response expected. |
| `on` | `(name: str, callback: Callable) → None` | Subscribe to inbound events by name. |
| `on_disconnect` | `(callback: Callable[[], Awaitable[None]]) → None` | Register disconnect callback. |
| `wait_ready` | `async (timeout: float = 10.0) → None` | Block until server sends `ready`. |

**Internal state:**
- `_conn_writer: ConnectionWriter | None`
- `_reader: asyncio.StreamReader | None`
- `_pending: dict[str, asyncio.Future[Envelope]]` — correlation_id → Future
- `_event_handlers: dict[str, list[Callable]]`
- `_on_disconnect_cb: Callable | None`
- `_read_task: asyncio.Task | None`
- `_background_tasks: set[asyncio.Task]`
- `_ready_event: asyncio.Event` — **initialized in `__init__`, not in `wait_ready()`**
- `_connected: bool`

**`connect()` flow:**

```python
async def connect(self) -> None:
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(self.socket_path), limit=STREAM_READER_LIMIT),
            timeout=CONNECTION_TIMEOUT,
        )
    except FileNotFoundError:
        raise IPCConnectionError(f"Socket not found: {self.socket_path}")
    except ConnectionRefusedError:
        raise IPCConnectionError(f"Connection refused: {self.socket_path}")
    except asyncio.TimeoutError:
        raise IPCConnectionError(f"Connection timed out after {CONNECTION_TIMEOUT}s")

    self._reader = reader
    self._conn_writer = ConnectionWriter(writer)
    self._read_task = asyncio.create_task(self._read_loop())
    self._connected = True
```

**`send_command()` — correlation tracking with guaranteed cleanup:**

```python
async def send_command(self, name: str, payload: dict = {}, *,
                       timeout: float = DEFAULT_COMMAND_TIMEOUT,
                       raw_data: bytes | None = None) -> Envelope:
    if not self._connected:
        raise IPCSendError("Not connected")

    correlation_id = str(uuid.uuid4())
    future = asyncio.get_running_loop().create_future()
    self._pending[correlation_id] = future

    envelope = create_envelope(
        from_module=self.module_name,
        to_module=self.target_module,
        type="command",
        name=name,
        payload=payload,
        correlation_id=correlation_id,
    )

    await self._conn_writer.write_message(envelope, raw_data)

    try:
        response = await asyncio.wait_for(future, timeout=timeout)
    except asyncio.TimeoutError:
        raise IPCTimeoutError(
            f"Command '{name}' timed out after {timeout}s",
            correlation_id=correlation_id,
        )
    finally:
        # Guarantees cleanup on timeout, cancellation, or disconnect
        self._pending.pop(correlation_id, None)

    # Auto-raise on error response
    if response.type == "error":
        raise IPCResponseError(
            response.payload.get("error", "Remote error"),
            correlation_id=correlation_id,
            error_payload=response.payload,
        )

    return response
```

**`send_event()` — fire and forget:**

```python
async def send_event(self, name: str, payload: dict = {},
                     raw_data: bytes | None = None) -> None:
    if not self._connected:
        raise IPCSendError("Not connected")

    envelope = create_envelope(
        from_module=self.module_name,
        to_module=self.target_module,
        type="event",
        name=name,
        payload=payload,
    )
    await self._conn_writer.write_message(envelope, raw_data)
```

**`_read_loop()` — background reader:**

```python
async def _read_loop(self) -> None:
    try:
        while True:
            envelope, raw_data = await read_message(self._reader)

            # Ready protocol
            if envelope.name == "ready":
                self._ready_event.set()
                continue

            # Correlation matching for command responses
            if envelope.type in ("response", "error") and envelope.correlation_id:
                future = self._pending.get(envelope.correlation_id)
                if future is not None and not future.done():
                    future.set_result(envelope)
                elif future is None:
                    log.warning("Response with unknown correlation_id: %s",
                                envelope.correlation_id)
                continue

            # Event dispatch
            handlers = self._event_handlers.get(envelope.name, [])
            for handler in handlers:
                task = asyncio.create_task(handler(envelope, raw_data))
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)

    except (IPCDisconnectError, ConnectionResetError, asyncio.IncompleteReadError):
        pass  # server disconnected — handled in finally
    except IPCProtocolError as exc:
        log.warning("Protocol error in read loop: %s", exc)
    finally:
        self._connected = False
        # Fail all pending command futures
        for future in self._pending.values():
            if not future.done():
                future.set_exception(IPCDisconnectError("Server disconnected"))
        self._pending.clear()
        # Notify application
        if self._on_disconnect_cb:
            try:
                await self._on_disconnect_cb()
            except Exception:
                log.exception("Disconnect callback failed")
```

**Disconnect detection timing:** Unix domain sockets are kernel-local. When the server process dies, the kernel delivers EOF to all connected clients immediately. asyncio surfaces this on the next event loop iteration — typically sub-millisecond. Well within the 100 ms requirement.

**`wait_ready()`:**

```python
async def wait_ready(self, timeout: float = 10.0) -> None:
    await asyncio.wait_for(self._ready_event.wait(), timeout=timeout)
```

Because `_ready_event` is initialized in `__init__`, if `ready` was received before this call, `wait()` returns instantly.

**`close()` flow:**

1. Cancel `_read_task`, await it.
2. Cancel all `_background_tasks`, await gather.
3. Fail all remaining `_pending` futures with `IPCDisconnectError`.
4. Close `_conn_writer`.
5. Set `_connected = False`.

---

## 3. Protocol Framing

### JSON-only messages

```
{"msg_id":"...","correlation_id":"","from":"loop","to":"tts","type":"command","name":"speak","timestamp":"...","payload":{"text":"Yes, sir."}}\n
```

Single JSON line, newline-terminated. Reader calls `readline()`.

### Binary payload messages

```
{"msg_id":"...","from":"vad","to":"stt","type":"event","name":"audio_buffer","timestamp":"...","payload":{},"bytes":1920000}\n
<exactly 1920000 raw bytes>
```

JSON header line (newline-terminated) with `"bytes": N`, followed by exactly N raw bytes. No base64, no escaping, no trailing newline.

### When to use which format

The spec says "Messages under 64 KB use JSON only" and ">64 KB use JSON-header-plus-raw-bytes." In practice, the protocol layer doesn't enforce this as a hard boundary — it's driven by whether the caller passes `raw_data`. If you pass `raw_data`, you get the binary format. If you don't, you get JSON-only. Audio frames (960 bytes) use the binary format despite being small, because they're raw PCM — encoding them in JSON would require base64.

### Defensive limits

- `StreamReader` limit: 1 MB (prevents buffer overflow from pathological headers).
- `bytes_length` cap: 10 MB (defensive, well above the 2 MB max audio buffer). `validate_envelope` rejects anything larger.

---

## 4. Ready Protocol

**Decision:** The server sends a `ready` event to every client upon connection, if the server is in the ready state.

**Rationale:** At startup, no clients are connected. The spec says "Every service emits `ready` on its socket immediately after startup." The practical meaning is: the server marks itself ready after binding, and sends `ready` to each client as they connect. A client connecting 5 minutes after startup still needs to know the server is ready.

This matches the Loop Controller spec: "Ready gate: wait for all seven `ready` events before first IDLE (30 s timeout)." The Loop Controller connects to each service and calls `wait_ready()` — the service sends `ready` upon connection.

---

## 5. Broadcast Protocol

**Convention:** Broadcast envelopes use `to_module = "*"`. Clients should accept messages where `to` is either their own module name or `"*"`.

---

## 6. Timeout Summary

| Timeout | Where | Mechanism | Default |
|---|---|---|---|
| Command response | `IPCClient.send_command()` | `asyncio.wait_for(future, timeout)` | 10 s |
| Connection attempt | `IPCClient.connect()` | `asyncio.wait_for(open_unix_connection(...), timeout)` | 2 s |
| Read assembly | `protocol.read_message()` | `asyncio.wait_for(readline/readexactly, timeout)` | 30 s |
| Broadcast per-client write | `IPCServer.broadcast()` | `asyncio.wait_for(write_message, timeout)` | 1 s |

All use `asyncio.wait_for()`. No timers, no polling, no threads.

---

## 7. Disconnect Propagation

### Server detects client disconnect

1. `read_message()` returns EOF → raises `IPCDisconnectError`.
2. `_handle_client()` exits read loop.
3. `finally`: remove from `_clients`, close writer, invoke `_on_disconnect_cb(peer_name)`.

### Client detects server disconnect

1. `read_message()` in `_read_loop()` returns EOF → raises `IPCDisconnectError`.
2. Read loop exits.
3. `finally`: set `_connected = False`, fail all pending futures, invoke `_on_disconnect_cb()`.

Both sub-millisecond on Unix domain sockets.

---

## 8. Implementation Order

| Step | Files | Tests |
|---|---|---|
| 1 | `errors.py`, `_constants.py` | Inheritance, attributes |
| 2 | `envelope.py` | Round-trip, validation, field mapping, edge cases |
| 3 | `protocol.py` | JSON framing, binary framing (960B/64KB/2MB), EOF detection, truncation, malformed JSON, write locking |
| 4 | `server.py` | Socket permissions, ready emission, dispatch, disconnect detection, broadcast with slow-client isolation, SIGTERM |
| 5 | `client.py` | Connect/timeout, command round-trip, correlation, command timeout with `correlation_id`, `wait_ready` race-free, disconnect detection, cancel cleanup |
| 6 | `__init__.py` | Import smoke test |
| 7 | `tests/integration/test_ping_pong.py` | All 12 acceptance criteria |
| 8 | `README.md` | API docs with examples |

Tests written alongside each step, not after.

---

## 9. Test Strategy

**Framework:** pytest + pytest-asyncio. The library has zero dependencies (stdlib only). The test suite uses pytest — this matches the spec's "pytest preferred" note. If you disagree, I'll use `unittest` + `asyncio` exclusively.

**Socket paths:** Tests use `/tmp/at_test_{uuid}/` to avoid needing root access to `/run/at/`. Both `IPCServer` and `IPCClient` accept `socket_path` overrides.

**Signal handlers:** Test fixtures pass `install_signal_handlers=False`. One dedicated test covers SIGTERM behavior.

**Integration test (`test_ping_pong.py`):**
1. Ping + Pong exchange commands and responses.
2. 100 concurrent commands with unique correlation IDs — no corruption.
3. Ready protocol: client receives `ready` before sending commands.
4. Non-existent socket: `IPCConnectionError` within 2 seconds.
5. Kill server → client detects within 100 ms.
6. Kill client → server detects within 100 ms.
7. 2 MB audio payload byte-for-byte intact.
8. Malformed JSON: server logs, continues serving.
9. Command timeout with `correlation_id` on exception.
10. 1000 sequential events delivered without loss.
11. Clean teardown: no fd, task, or memory leaks.
12. Multiple concurrent clients to same server.

---

## 10. What I'm Least Confident About

1. **Broadcast write timeout (1 second).** If a client's socket buffer is genuinely full, 1 second may be too short for a legitimate slow consumer, or too long for a real-time 33 Hz audio stream. The timeout is configurable but the default needs empirical tuning. For audio frames at 960 bytes each, a 1-second stall means ~33 dropped frames for all other subscribers if we were sequential — but with `gather`, only the slow client is affected. I think 1 second is safe. Flag if you want a different default.

2. **Concurrent dispatch ordering.** Multiple commands from the same client can execute out of order. This is correct for AT (correlation IDs match) but could surprise someone expecting FIFO semantics. Documented in the plan. Flag if any AT service depends on same-client command ordering.

3. **Read timeout on partial binary payload.** If a sender sends the JSON header with `bytes: 2000000` then stops, `readexactly` blocks until the 30-second timeout. The partial read leaves the stream unrecoverable — we close the connection, which is correct (the sender is broken). But 30 seconds is a long time to wait for a dead sender. Could reduce to 5 seconds for the raw-byte read specifically, but 30 seconds matches the spec.

---

*Awaiting approval to begin implementation.*
