# at-ipc Implementation Plan (Updated & Hardened)

> [!IMPORTANT]
> This revised plan incorporates all 9 concurrency, safety, and lifecycle fixes identified during architectural review. Ready for implementation approval.

---

## 1. File Structure

```
src/at_ipc/
├── __init__.py          # Public API re-exports
├── envelope.py          # Message envelope: dataclass, encode, decode, validation
├── protocol.py          # Wire protocol: framing, reading, writing (JSON + raw-byte), write lock
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
│   └── test_ping_pong.py   # Two-service round-trip, concurrency, audio streaming, disconnect
└── conftest.py              # Shared fixtures (temp socket paths, loop lifecycle)
```

---

## 2. Module Breakdown & Key Mechanisms

### 2.1 `errors.py` — Exception Hierarchy

```python
class IPCError(Exception):
    """Base exception for all at-ipc errors."""

class IPCConnectionError(IPCError):
    """Failed to connect to Unix socket within timeout or target not found."""

class IPCDisconnectError(IPCError):
    """Peer dropped or closed connection (including clean EOF)."""

class IPCTimeoutError(IPCError):
    """Command response or read timed out."""
    def __init__(self, message: str, correlation_id: str | None = None) -> None:
        super().__init__(message)
        self.correlation_id = correlation_id

class IPCProtocolError(IPCError):
    """Malformed envelope, schema mismatch, or truncated payload."""

class IPCSendError(IPCError):
    """Write failed because socket is closed or broken."""

class IPCResponseError(IPCError):
    """Remote peer returned an explicit error response envelope."""
    def __init__(self, message: str, correlation_id: str | None = None, error_payload: dict | None = None) -> None:
        super().__init__(message)
        self.correlation_id = correlation_id
        self.error_payload = error_payload or {}
```

---

### 2.2 `_constants.py` — Shared Constants

```python
from pathlib import Path

DEFAULT_SOCKET_DIR = Path("/run/at")
SOCKET_PERMISSIONS = 0o600
DEFAULT_COMMAND_TIMEOUT = 10.0      # seconds
CONNECTION_TIMEOUT = 2.0            # seconds
READ_TIMEOUT = 30.0                 # seconds
MAX_JSON_SIZE = 64 * 1024           # 64 KB threshold
STREAM_READER_LIMIT = 1024 * 1024   # 1 MB buffer limit for StreamReader
MSG_TYPES = frozenset({"event", "command", "response", "error"})
```

---

### 2.3 `envelope.py` — Message Envelope

**Dataclass: `Envelope`**
- `msg_id: str` (UUID4 string, auto-generated)
- `correlation_id: str` (UUID4 string, empty string for events)
- `from_module: str` (maps to `"from"` in JSON)
- `to_module: str` (maps to `"to"` in JSON)
- `type: str` (`"event"`, `"command"`, `"response"`, or `"error"`)
- `name: str` (message name, e.g., `"ready"`, `"ping"`, `"audio_frame"`)
- `timestamp: str` (ISO 8601 UTC string via `datetime.now(timezone.utc).isoformat()`)
- `payload: dict` (JSON object)
- `bytes_length: int | None` (maps to `"bytes"` in JSON header when raw bytes follow)

**Functions:**
- `create_envelope(from_module, to_module, type, name, payload={}, correlation_id="", bytes_length=None) -> Envelope`
- `envelope_to_dict(env: Envelope) -> dict`
- `dict_to_envelope(d: dict) -> Envelope` (validates all fields; raises `IPCProtocolError` on missing/invalid)
- `validate_envelope(env: Envelope) -> None`

---

### 2.4 `protocol.py` — Wire Framing & Stream Concurrency

This module owns byte serialization, line framing, raw-byte reads/writes, and write synchronization.

```python
class ConnectionWriter:
    """Wraps StreamWriter with an asyncio.Lock to ensure serialized atomic writes."""
    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self.writer = writer
        self.lock = asyncio.Lock()

    async def write_message(self, envelope: Envelope, raw_data: bytes | None = None) -> None:
        if raw_data is not None:
            envelope.bytes_length = len(raw_data)
        
        header_json = json.dumps(envelope_to_dict(envelope), separators=(',', ':'))
        header_bytes = header_json.encode('utf-8') + b'\n'
        
        async with self.lock:
            try:
                self.writer.write(header_bytes)
                if raw_data is not None:
                    self.writer.write(raw_data)
                await self.writer.drain()
            except (ConnectionResetError, BrokenPipeError) as exc:
                raise IPCSendError(f"Failed to send message: {exc}") from exc

async def read_message(reader: asyncio.StreamReader) -> tuple[Envelope, bytes | None]:
    """Reads one message. Handles EOF, JSON parsing, and optional raw payload read."""
    try:
        # Step 1: Read newline-terminated JSON header line with timeout
        line = await asyncio.wait_for(reader.readline(), timeout=READ_TIMEOUT)
    except asyncio.TimeoutError as exc:
        raise IPCTimeoutError(f"Read assembly timed out after {READ_TIMEOUT}s") from exc
    except ConnectionResetError as exc:
        raise IPCDisconnectError("Connection reset by peer") from exc

    # Crucial EOF check: prevents 100% CPU infinite loop on disconnect
    if not line:
        raise IPCDisconnectError("Connection closed by peer (EOF)")

    # Step 2: Decode JSON header
    try:
        header_dict = json.loads(line.decode('utf-8'))
        envelope = dict_to_envelope(header_dict)
    except (json.JSONDecodeError, UnicodeDecodeError, IPCProtocolError) as exc:
        raise IPCProtocolError(f"Malformed message header: {exc}") from exc

    # Step 3: Read raw bytes if specified
    raw_data: bytes | None = None
    if envelope.bytes_length is not None and envelope.bytes_length > 0:
        try:
            raw_data = await asyncio.wait_for(
                reader.readexactly(envelope.bytes_length),
                timeout=READ_TIMEOUT
            )
        except asyncio.IncompleteReadError as exc:
            raise IPCProtocolError(
                f"Truncated message: expected {envelope.bytes_length} bytes, got {len(exc.partial)}"
            ) from exc
        except asyncio.TimeoutError as exc:
            raise IPCTimeoutError(
                f"Timed out reading raw payload ({envelope.bytes_length} bytes)"
            ) from exc

    return envelope, raw_data
```

---

### 2.5 `server.py` — `IPCServer`

**Responsibilities:**
- Create and manage the Unix socket at `/run/at/{module}.sock` (or custom override).
- Send `ready` to every newly connected client.
- Dispatch inbound messages to registered handlers without blocking the read loop.
- Support audio & event fanout to multiple subscribers via `broadcast()`.
- Handle clean SIGTERM shutdown and idempotent socket unlinking.

```python
class IPCServer:
    def __init__(
        self,
        module_name: str,
        socket_path: Path | None = None,
        install_signal_handlers: bool = True,
    ) -> None:
        self.module_name = module_name
        self.socket_path = socket_path or (DEFAULT_SOCKET_DIR / f"{module_name}.sock")
        self.install_signal_handlers = install_signal_handlers
        
        self._handlers: dict[str, Callable] = {}
        self._clients: dict[int, ConnectionWriter] = {}  # id(writer) -> ConnectionWriter
        self._client_peers: dict[int, str] = {}          # id(writer) -> peer module name
        self._on_disconnect_cb: Callable[[str | None], Awaitable[None]] | None = None
        self._server: asyncio.Server | None = None
        self._background_tasks: set[asyncio.Task] = set()
        self._running = False
        self._ready = False
```

**Key Execution Flows:**

1. **`start()`:**
   - Probes socket file: if exists, attempts connection. If active, raises `IPCError("Socket already in use")`; if connection refused, unlinks stale socket.
   - Ensures directory exists.
   - Sets umask or chmods to `0600` immediately.
   - Starts server with `limit=STREAM_READER_LIMIT`.
   - If `install_signal_handlers` is `True`, adds `SIGTERM` handler.
   - Sets `_ready = True` and `_running = True`.

2. **Client Handling (`_handle_client(reader, writer)`):**
   - Wraps writer in `conn_writer = ConnectionWriter(writer)`.
   - Stores in `self._clients[id(writer)] = conn_writer`.
   - Sends `ready` event envelope to this new client immediately.
   - In loop:
     - `envelope, raw_data = await read_message(reader)`
     - Records peer name: `_client_peers[id(writer)] = envelope.from_module`.
     - Spawns dispatch task in `_background_tasks` to ensure concurrent command processing:
       ```python
       task = asyncio.create_task(self._dispatch(envelope, raw_data, conn_writer))
       self._track_task(task)
       ```
   - On `IPCProtocolError`: Logs warning, continues if connection stream is still healthy; if stream truncated, breaks.
   - On `IPCDisconnectError` / EOF: Breaks loop.
   - `finally`:
     - Removes from `_clients`.
     - Cleans up writer.
     - Invokes `_on_disconnect_cb(peer_name)`.

3. **`_dispatch(envelope, raw_data, conn_writer)`:**
   - Resolves handler for `envelope.name`.
   - If handler missing and `type == "command"`: sends `error` envelope.
   - Executes handler: `res = await handler(envelope, raw_data)`.
   - If command:
     - If `res` is `dict`: sends response envelope with `payload=res`.
     - If `res` is `tuple[dict, bytes]`: sends response with `payload=res[0]`, `raw_data=res[1]`.
     - If `res` is `None`: sends response with `payload={}`.
   - If handler raises exception: logs and sends `error` envelope with exception details.

4. **`broadcast(name, payload={}, raw_data=None)`:**
   - Prepares envelope with `from_module=self.module_name`, `to_module="*", type="event"`.
   - Fans out concurrently to all active client writers using `asyncio.gather(*[...], return_exceptions=True)`.
   - Any client whose write fails or times out is evicted and closed, protecting other subscribers (e.g. `audio-source` broadcasting to `vad` and `wake-word`).

5. **`stop()`:**
   - Removes signal handler if registered.
   - Closes server and awaits `wait_closed()`.
   - Cancels and awaits all tasks in `_background_tasks`.
   - Closes all client writers and awaits `wait_closed()`.
   - Unlinks `socket_path` cleanly.

---

### 2.6 `client.py` — `IPCClient`

**Responsibilities:**
- Connect to target Unix socket within 2-second timeout.
- Correlate outbound commands to inbound responses via UUID4 `correlation_id`.
- Protect stream writes with `ConnectionWriter`.
- Expose `wait_ready()` without startup race conditions.
- Cleanly detect server disconnect in `< 100 ms`.
- Zero memory or task leaks on external cancellation.

```python
class IPCClient:
    def __init__(
        self,
        module_name: str,
        target_module: str,
        socket_path: Path | None = None,
    ) -> None:
        self.module_name = module_name
        self.target_module = target_module
        self.socket_path = socket_path or (DEFAULT_SOCKET_DIR / f"{target_module}.sock")
        
        self._conn_writer: ConnectionWriter | None = None
        self._reader: asyncio.StreamReader | None = None
        self._pending: dict[str, asyncio.Future[Envelope]] = {}
        self._event_handlers: dict[str, list[Callable]] = {}
        self._on_disconnect_cb: Callable[[], Awaitable[None]] | None = None
        
        self._read_task: asyncio.Task | None = None
        self._background_tasks: set[asyncio.Task] = set()
        
        # Initialized early to eliminate startup race condition
        self._ready_event = asyncio.Event()
        self._connected = False
```

**Key Execution Flows:**

1. **`connect()`:**
   - Uses `asyncio.wait_for(asyncio.open_unix_connection(self.socket_path, limit=STREAM_READER_LIMIT), timeout=CONNECTION_TIMEOUT)`.
   - Catches `FileNotFoundError`, `ConnectionRefusedError`, `asyncio.TimeoutError` → raises `IPCConnectionError`.
   - Sets `self._conn_writer = ConnectionWriter(writer)`.
   - Spawns `self._read_task = asyncio.create_task(self._read_loop())`.
   - Sets `_connected = True`.

2. **`wait_ready(timeout=10.0)`:**
   - `await asyncio.wait_for(self._ready_event.wait(), timeout=timeout)`
   - If `ready` arrived prior to `wait_ready()` call, `_ready_event` is already set and returns immediately.

3. **`send_command(name, payload={}, timeout=10.0, raw_data=None) -> Envelope`:**
   - Creates `correlation_id = str(uuid.uuid4())`.
   - Registers `future = asyncio.get_running_loop().create_future()` in `self._pending[correlation_id]`.
   - Creates command envelope.
   - Writes via `await self._conn_writer.write_message(envelope, raw_data)`.
   - Awaits future with `asyncio.wait_for(future, timeout=timeout)`.
   - **Cleanup guarantee:**
     ```python
     try:
         resp = await asyncio.wait_for(future, timeout=timeout)
         if resp.type == "error":
             raise IPCResponseError(resp.payload.get("error", "Remote command error"), correlation_id=correlation_id, error_payload=resp.payload)
         return resp
     except asyncio.TimeoutError as exc:
         raise IPCTimeoutError(f"Command '{name}' timed out after {timeout}s", correlation_id=correlation_id) from exc
     finally:
         self._pending.pop(correlation_id, None)
     ```

4. **`_read_loop()`:**
   - In loop:
     - `envelope, raw_data = await read_message(self._reader)`
     - If `envelope.name == "ready"`: `self._ready_event.set()`
     - If `envelope.type in ("response", "error")` and `envelope.correlation_id`:
       - `future = self._pending.get(envelope.correlation_id)`
       - If future and not future.done(): `future.set_result(envelope)`
     - If `envelope.type == "event"`:
       - Dispatches to registered event callbacks as tracked background tasks.
   - On `IPCDisconnectError` / EOF:
     - Marks `_connected = False`.
     - Fails all remaining `_pending` futures with `IPCDisconnectError("Server disconnected")`.
     - If `_on_disconnect_cb` registered: runs callback.

5. **`close()`:**
   - Cancels and awaits `_read_task`.
   - Cancels and awaits all background tasks.
   - Fails all pending futures.
   - Closes writer and awaits `wait_closed()`.

---

## 3. Protocol Framing & Data Rules

1. **JSON-only messages:**
   - Envelope serialized to a single line terminated with `\n`.
   - No `bytes` field (or `bytes` is omitted / `None`).
2. **Audio & Raw Binary payloads:**
   - JSON envelope header line terminated with `\n`, containing `"bytes": N`.
   - Exactly N raw bytes immediately follow. No base64, no escaping, no trailing newline.
   - Used for all buffers where `raw_data` is supplied (both >64 KB STT utterances and 960-byte real-time PCM frames).
3. **StreamReader safety:**
   - `limit=1024*1024` (1 MB) passed to `open_unix_connection` and `start_unix_server` to prevent buffer overflow on large headers.

---

## 4. Implementation Steps & Verification Plan

```mermaid
flowchart LR
    Step1["Step 1: errors & constants"] --> Step2["Step 2: envelope.py"]
    Step2 --> Step3["Step 3: protocol.py"]
    Step3 --> Step4["Step 4: server.py"]
    Step4 --> Step5["Step 5: client.py"]
    Step5 --> Step6["Step 6: __init__.py"]
    Step6 --> Step7["Step 7: Ping-Pong & Acceptance Tests"]
    Step7 --> Step8["Step 8: README & Docs"]
```

### Step 1: `errors.py` and `_constants.py`
- Define full exception hierarchy including `IPCResponseError` and `IPCTimeoutError.correlation_id`.
- Unit tests: verify exception attributes and inheritance.

### Step 2: `envelope.py`
- Implement `Envelope`, serialization, deserialization, and ISO 8601 UTC timestamp generation (`datetime.now(timezone.utc)`).
- Unit tests: round-trip encoding/decoding, validation failures, schema edge cases (empty payload, special characters).

### Step 3: `protocol.py`
- Implement `ConnectionWriter` (with `asyncio.Lock`) and `read_message`.
- Unit tests:
  - Framing JSON messages.
  - Framing raw-byte messages (960 bytes, 64 KB, 2 MB).
  - EOF detection without busy-looping.
  - Truncated payload raises `IPCProtocolError`.
  - Malformed JSON raises `IPCProtocolError`.

### Step 4: `server.py`
- Implement `IPCServer` with client tracking, concurrent dispatch, audio broadcast, and signal teardown.
- Unit tests:
  - Socket creation with `0600` permissions.
  - Ready protocol emission to connecting clients.
  - Command handling, auto-response, and error responses.
  - Disconnect detection within 100 ms.
  - Broadcast to multiple clients with slow-client isolation.

### Step 5: `client.py`
- Implement `IPCClient` with correlation tracking, write locking, early `_ready_event`, and background task tracking.
- Unit tests:
  - Connecting to non-existent socket fails in <2s with `IPCConnectionError`.
  - Command round-trip and timeout carrying `correlation_id`.
  - `wait_ready()` resolving immediately when server is already ready.
  - Server disconnect detected in <100 ms.
  - Correlation cleanup on cancellation.

### Step 6: `__init__.py`
- Re-export clean public API: `IPCServer`, `IPCClient`, `Envelope`, `create_envelope`, and all exceptions.

### Step 7: Acceptance & Integration Suite (`tests/integration/test_ping_pong.py`)
Validate all 13 spec criteria:
1. Two services (`ping` and `pong`) exchange commands over sockets.
2. Ping-pong round-trip completes within 10 ms.
3. 100 concurrent commands match correlation IDs under load without stream corruption.
4. `ready` emitted before first command accepted.
5. Non-existent socket raises clear error within 2 seconds.
6. Killing server process triggers client disconnect detection within 100 ms.
7. Killing client process triggers server disconnect detection within 100 ms.
8. 2 MB audio payload transmits and receives byte-for-byte intact.
9. Malformed JSON does not crash server (logged and continues serving).
10. Command timeout fires correctly, carrying `correlation_id`.
11. 1000 sequential events delivered without loss.
12. Clean socket teardown, no fd or task leaks.

### Step 8: `README.md` & Public Docstrings
- Full API documentation with runnable code examples for server, client, command/response, event broadcast, and audio streaming.
- Type annotations and docstrings on every public class and method.

---

## 5. Review Resolution Checklist

- [x] **Race condition in `wait_ready()`:** `_ready_event` initialized in `__init__`.
- [x] **Concurrent write corruption:** `ConnectionWriter` with `asyncio.Lock` wraps all socket writes.
- [x] **Infinite 100% CPU on disconnect:** Immediate EOF check (`if not line: raise IPCDisconnectError(...)`).
- [x] **Server audio broadcast:** `broadcast(name, payload={}, raw_data=None)` implemented.
- [x] **Slow client broadcast blockage:** Concurrent fanout via `asyncio.gather(..., return_exceptions=True)`.
- [x] **Task & Memory leaks:** Strong reference set `_background_tasks` and `_pending.pop` in `finally`.
- [x] **Signal handler test pollution:** Optional `install_signal_handlers` with clean removal on `stop()`.
- [x] **Python 3.12 compatibility:** UTC timezone-aware timestamps, no deprecated calls.

*Awaiting approval to begin Step 1.*
