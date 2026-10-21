Add six adversarial tests to at-ipc. These tests exist to prove guarantees the current test suite only lightly touches. If a test fails, fix the code, not the test.

Test 1 — FIFO command execution under concurrent send (in tests/integration/test_ping_pong.py):

Existing test_per_connection_fifo_guarantee tests events, sequentially — it can't detect a FIFO violation. Write a new test test_command_fifo_under_concurrent_burst:

    Server handler: appends env.payload["i"] to a shared exec_log, then await asyncio.sleep(0.005), then returns {"i": env.payload["i"]}.

    Client: fire 20 commands concurrently via asyncio.gather(*[client.send_command("cmd", {"i": i}) for i in range(20)]).

    Assertions:

        All 20 responses arrive.

        exec_log == list(range(20)) in exact order.

        Additionally assert: no interleaving occurred — meaning between any two consecutive appends to exec_log, no second handler started. Instrument with exec_start_log and exec_end_log; assert exec_end_log == exec_start_log (each handler fully completes before next begins).

If FIFO is genuinely guaranteed by inline await self._dispatch(...), this test passes cleanly. If the code uses create_task anywhere in the dispatch path, it fails.

Test 2 — Broadcast drop_on_backpressure=True (in tests/unit/test_server.py or new tests/unit/test_broadcast.py):

Write test_broadcast_drop_on_backpressure_keeps_slow_client_connected:

    Create one slow client: connects, then does not read from its socket for the test duration (use a raw socket connect via asyncio.open_unix_connection, do not start a read loop).

    Create two fast clients: connect and start reading immediately into a list.

    Server calls broadcast(name="frame", raw_data=b"x" * 100_000, drop_on_backpressure=True) 15 times in a loop (1.5 MB total, forces kernel buffer pressure on the slow client).

    Assertions:

        Both fast clients received 15 frames each.

        Slow client received fewer than 15 frames (proves drops happened).

        Slow client's socket is still open (assert via not writer.is_closing() or by checking the slow client's wait_for on a subsequent small write succeeds).

        Server's _clients dict still contains the slow client.

Test 3 — Broadcast drop_on_backpressure=False evicts (same file):

Write test_broadcast_no_drop_evicts_slow_client:

    Same setup: one slow, two fast clients.

    Register server.on_disconnect(callback) that appends to a list.

    Server calls broadcast(name="frame", raw_data=b"x" * 100_000, drop_on_backpressure=False) 15 times.

    Assertions:

        Disconnect callback fired for the slow client (proves eviction path executed).

        Fast clients received 15 frames each (unaffected).

        id(slow_client_writer) not in server._clients after the test.

Test 5 — Ready race (in tests/unit/test_client.py):

Write test_wait_ready_blocks_until_server_ready:

    Create an IPCServer but monkey-patch IPCServer.start() to await asyncio.sleep(0.2) after socket bind but before _ready = True — or more simply, add a small await asyncio.sleep(0.15) between socket bind and _ready = True in a subclass used only by this test (do not modify production code).

Actually simpler:

    Start server normally in a task.

    Connect client immediately via asyncio.open_unix_connection (bypassing client.connect() which handles the wait). Then create an IPCClient, call connect(), and immediately await client.wait_ready(timeout=2.0).

    Measure elapsed time from connect-start to wait_ready-return.

    Assertion: elapsed ≥ 50ms (server was still starting, ready hadn't fired yet) AND elapsed ≤ 500ms (didn't hang).

    Do NOT just connect after await server.start() — that's what the existing test does and it can't detect the race.

Test 6 — Raw payload timeout (in tests/unit/test_protocol.py):

Write test_raw_payload_read_timeout_fires_at_3_seconds:

    Create a raw Unix socket pair: asyncio.start_unix_server on server side, asyncio.open_unix_connection on client side.

    Client sends a valid JSON header: {"msg_id":"x","from":"a","to":"b","type":"event","name":"t","timestamp":"...","payload":{},"bytes":1000000}\n followed by nothing — do not send the 1MB.

    Server calls read_message(server_reader).

    Wrap in time.monotonic() before/after.

    Assertions:

        Raises IPCTimeoutError.

        Elapsed time between 2.5s and 3.8s (allows scheduling jitter, but proves it's the 3s constant, not 30s).

    Keep socket open the entire time — do not close it (that would test truncation, not timeout).

Test 7 — EOF read-loop termination (in tests/unit/test_server.py):

Write test_read_loop_exits_on_client_eof:

    Subclass or instrument IPCServer._handle_client to increment a counter per read-loop iteration. Add this via a subclass in the test, do not modify production code.

    Start server, connect one client, then close the client socket abruptly (writer.close() without sending anything).

    await asyncio.sleep(0.2).

    Assertions:

        The instrumented _handle_client task completed (check via task.done() if you kept a reference, or verify the loop iteration counter for that connection is exactly 1 — one read_message call that raised IPCDisconnectError, then break).

        The counter is not increasing anymore (would prove 100% CPU spin if it kept incrementing).

        id(client_writer) not in server._clients after cleanup.

If instrumentation is too invasive, alternative: use tracemalloc or resource.getrusage(resource.RUSAGE_SELF) to measure CPU time before/after a 200ms window post-disconnect. If CPU usage grows by more than ~1ms in that window, the loop is spinning.

Process:

    Read specs/02-ipc.md and docs/plans/at-ipc-v2-final.md for the guarantees.

    Read the six failing-coverage areas above.

    Write each test.

    Before running, predict what the test would catch. If you cannot articulate a bug that the test would catch, the test is too weak — rewrite it.

    Run pytest tests/unit/ tests/integration/ -v.

    If any test fails: fix the CODE, not the test. Unless the test itself is provably wrong (e.g., timing threshold too tight for CI), do not soften assertions.

    Re-run until all tests pass.

    Do not declare done until you have pasted the full pytest output showing 55 + 6 = 61 tests, all passing.

Output at the end:

    Full pytest output.

    For each of the 6 new tests: name, what it verifies, pass/fail.

    Any test that initially failed and required a code fix: describe the bug found and the fix applied.

    Anything you're least confident about.

Start now.

You are adding 6 adversarial tests to an existing at-ipc library at /home/aniruddh_sen/Documents/Kate/at/. The library already has 55 passing tests. These 6 tests exist to prove guarantees that current coverage only lightly touches. If a test fails, fix the code, not the test.

Critical framing: Each test must be designed to fail if the specific guarantee is broken. Before writing each test, mentally simulate: "What bug would this catch?" If you cannot articulate a bug, the test is too weak — rewrite it. Do not write confirmatory tests that just re-verify the happy path.
