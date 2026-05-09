# nc Transfer Failure-Injection Tests

## Context

Current nc stability coverage (Tier 2 + existing unit tests) exercises the
**happy paths under load**: concurrent puts, concurrent gets, fan-out,
listener-leak after success or after orchestrator-side cancellation. What
isn't covered is **deliberate failure injection** — verifying otto's
error-handling code paths actually run cleanly when something on the
network or the remote breaks.

This is the "highest-risk surface area" follow-up Chris flagged when we
added the rest of the nc stability tests. Tracked here for a later
focused change rather than bundled into the initial round.

## Failure modes worth covering

Each maps to a real-world flake class otto users have seen or could see.

### 1. Remote listener fails to start

**Scenario:** the `nc -l <port>` command fails immediately on the remote
(port already bound by a different process, `nc` binary missing, shell
returns non-zero before binding).

**Why it matters:** `_put_files_nc` launches the listener as a
`create_task` and waits for it via `_wait_for_remote_listener`. If the
listener exits before that probe sees the LISTEN socket, the probe times
out and we fall through to a connect attempt that races with the failed
process. Code path under [transfer.py:1140-1180](../src/otto/host/transfer.py#L1140-L1180).

**How to inject:** mock `_exec_cmd` (or whatever runs the listener) to
return a non-zero exit immediately. Since `_exec_cmd` is an injected
callable on `FileTransfer`, this is unit-testable without VMs.

**Expected behavior:**
- `transfer.put()` returns `Status.Error` with an actionable message.
- No orphaned tasks on the asyncio loop.
- Subsequent puts on the same host work (no poisoned state).

### 2. Peer disconnects mid-transfer

**Scenario:** the remote nc listener exits while otto is still writing
data into the connection (e.g. remote killed `nc`, remote disk full, OOM
killer).

**Why it matters:** the writer-loop at [transfer.py:1186-1204](../src/otto/host/transfer.py#L1186-L1204)
is a tight loop of `writer.write` + `await writer.drain`. A peer-side
close surfaces as `ConnectionResetError` on the next drain, but the
listener task may still be in `_exec_cmd` waiting for the SSH/telnet
session to return — that task must be cancelled and awaited.

**How to inject:** spin up a real local `nc -l` listener, wire otto to
connect to it, then `kill -9` the listener process partway through.
Simpler at the unit level: replace `writer.drain` with an `AsyncMock`
that raises `ConnectionResetError` after N calls.

**Expected behavior:**
- `Status.Error` with "connection reset" substring.
- `listen_task` is cancelled and awaited.
- No leftover transports / tasks.

### 3. Slow listener (timeout in `_wait_for_remote_listener`)

**Scenario:** remote system is loaded; `nc -l` takes longer than the
listener-wait budget to start accepting.

**Why it matters:** [`_wait_for_remote_listener`](../src/otto/host/transfer.py#L858)
has tunable timeout/poll knobs. If it times out, we should still attempt
the connect with retry logic — `_connect_with_retry` has its own budget.
The interplay isn't directly tested.

**How to inject:** mock `_wait_for_remote_listener` to sleep past the
timeout, raising `TimeoutError`. Verify `_put_files_nc` behaves
gracefully (not stack-traces, retries appropriately, or returns a clean
error).

### 4. Network drops mid-transfer

**Scenario:** TCP RST or ICMP unreachable mid-stream.

**Why it matters:** different from peer-disconnect (#2) because the kernel
may surface this as `BrokenPipeError`, `ConnectionAbortedError`, or
`OSError` with various errnos — coverage should ensure all three are
handled equivalently.

**How to inject:** use `iptables` (root) to drop traffic on the listener
port partway through. Or at the unit level, mock the writer to raise
each error class in turn.

### 5. Cancellation during `_wait_for_remote_listener`

**Scenario:** parent task is cancelled while we're polling for the
listener to come up (between spawn and connect).

**Why it matters:** different cancellation point than #2 — the listener
is up but otto hasn't connected yet. The spawned `listen_task` should
still be cancelled, but the cleanup path is reached via a different
branch than the post-connect cancel.

**How to inject:** set up a fake `_exec_cmd` that succeeds in spawning
the listener but never returns (simulates a long-running listener).
Cancel the parent via `wait_for(timeout)`. Verify the spawned task is
cancelled.

## Test structure recommendation

Lives at [tests/unit/host/test_transfer_nc_failure.py](../tests/unit/host/test_transfer_nc_failure.py)
(new file), unit-level (no `@pytest.mark.integration`), uses the same
mocked `FileTransfer` construction pattern as
[test_transfer_nc_put.py](../tests/unit/host/test_transfer_nc_put.py).

One test per failure mode above. Each test:
1. Constructs a `FileTransfer` with mocked `_exec_cmd`, `_open_session`,
   `_connections`.
2. Triggers the failure (mock raises / sleeps / returns error).
3. Asserts: clean error return + no leaked tasks (`asyncio.all_tasks()`
   has only the test runner) + no leftover state.

Estimated effort: 1-2 days. Each failure mode is a small focused test;
most of the work is the shared mock harness for injecting failures into
the right point in the code path.

## Out of scope for this follow-up

- Multi-hop failure scenarios (`hop_host` fixture). Follows the same
  patterns but adds VM-dependency complexity; defer further.
- Real TCP RST injection via `iptables`. Requires root in the test
  environment — keep failure injection at the asyncio-mock layer.
