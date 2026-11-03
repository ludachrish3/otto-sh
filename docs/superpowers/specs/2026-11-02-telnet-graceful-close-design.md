# Every release of a single-client console drains it to quiet first (#386)

**Status:** draft v7, for approval, 2026-11-02, after Astra and Fable reviews of v1–v6 and the bed diagnostic (arms A–E). Top priority for 0.19.0: an external user reproduces #386 nightly (comments from 2026-10-26 to 2026-11-01).

## Terms

- **Telnet-framed stream:** a telnetlib3 reader/writer pair. It is either a `TelnetClient` (term `telnet`: Unix telnetd and every Zephyr guest's `shell_telnet`) or a `ConsoleClient` (term `console`: a serial line behind ser2net).
- **Single-client console:** a console that accepts one client at a time. Zephyr `shell_telnet` is one (`options.single_client_console`, `src/otto/host/options.py:431`), and so is ser2net.
- **The hop:** on the bed, otto never speaks TCP to a Zephyr guest directly. Its socket is a local asyncssh forward, and the hop's Linux stack opens and closes the TCP connection to the guest.

## Problem

Zephyr's `shell_telnet` backend wedges when a send fails because its client has gone. `zsock_send` fails, the guest logs `shell_telnet: Failed to send …, shutting down` and tears its backend down (`shell_telnet.c:199-214`). On 2.7 the e1000 driver then starves (`eth_e1000: Out of buffers`). On 3.7 and 4.4 the backend's restart sometimes leaves it refusing every connection (a timing-dependent `EADDRINUSE`, #324). A `Failed to send` is necessary for the wedge but not sufficient: the hop journal shows it a few times on 3.7 and 4.4 with no consequence (`_FANOUT_EXCLUDED` text, `tests/integration/host/test_embedded_host_integration.py:326-342`).

On the bed, the first wedge on a guest fails every later test on it (`embedded bed unhealthy — an earlier test found this console wedged`). The reporter counts 68–127 such errors per night. On a real device, the same failure leaves its telnet shell dead until a power cycle.

### Mechanism

Zephyr's TCP has no half-close. A FIN received while ESTABLISHED is answered with FIN|ACK, and the state moves straight to LAST_ACK. After that, `net_tcp_queue` refuses every send with `-ENOTCONN` (3.7 `tcp.c:3147-3150, 3745`; 4.4 `tcp.c:3468-3481` "Half-close is not supported", `3839`). The 2.7 wedge text in the tree records `Failed to send -128`, which is ENOTCONN, the same gate. 2.7's source is not checked on this VM.

So the guest's send fails whenever the connection leaves ESTABLISHED while the guest still has queued output. That can be a FIN, an RST or the hop's close. **The close's shape does not matter; its timing does.** #324 reached the same conclusion from source and packet capture: "FIN or RST, it doesn't matter." What otto controls is when it releases the stream relative to the guest's output.

### Prior art: #322 and #324

#324 (closed 2026-09-13) added exactly the cure: **drain the console to quiet before releasing it.**
- `TelnetSession._quiesce` (`src/otto/host/session.py:1588-1620`) reads and discards until the line has been silent for `_CONSOLE_QUIESCE_QUIET_WINDOW` (0.2 s), capped at `_CONSOLE_QUIESCE_BUDGET` (2.0 s) (`session.py:170-180`).
- `SessionManager.open_budget` counts that budget (`session.py:2892-2911`).

But the drain runs in one place only: `ShellSession._fail_init` (`session.py:870-882`), the failed-handshake teardown. **Every other release of a single-client console skips it.**

### The release sites

| Site | Who reaches it | Drains first today? |
|---|---|---|
| `TelnetSession.close` with no client (`session.py:1660-1673`; the session is built on `ConnectionManager.telnet()`'s cached client, `session.py:3276-3282`) | **the main Zephyr path:** every command through a Zephyr host's default session | no |
| `TelnetClient.close` (`src/otto/host/telnet.py:358-380`) | named and exec-pool sessions (owned clients), and the manager's teardown of the cached client | no |
| `ConsoleClient.close` → `_release` (`src/otto/host/console.py:568-631`) | ser2net serial consoles (unix hosts on the `console` term) | no |
| `ConsoleClient.abandon` (`console.py:597-620`) | after a reboot; sync | n/a: no loop, the line is dead |
| `abort_console_transports()` (`telnet.py:163-180`) | the pytest-timeout emergency sweep; sync | n/a |
| `ShellSession._fail_init` → `_quiesce` | a failed readiness handshake | **yes (#324)** |

### First bed diagnostic (2026-11-02, before this redesign)

The diagnostic script ran on the four guests named in #386, 30 cycles per arm, reading the hop journal by cursor per unit.

| Arm | 2.7-fat | 3.7-fat | 3.7-lfs | 4.4-lfs |
|---|---|---|---|---|
| C: normal close, immediate re-open | 30/30, 0 faults, re-open 0.70 s | 30/30, 0, 0.23 s | 30/30, 0, 0.22 s | 30/30, 0, 0.23 s |
| B: quiesce, then close (output in flight) | 30/30, 0 faults, quiesce 0.35 s | 30/30, 0, 0.34 s | 30/30, 0, 0.34 s | 30/30, 0, 0.34 s |

- A normal close does not leave the slot held. Re-open succeeds at once and the first handshake never failed. That rules out the "held slot" alternative for the close path.
- Quiescing first is clean and costs about 0.34 s. Arm B quiesced and then closed with today's code, which ends in `abort()`, and got 0 faults in 120 closes. That is direct evidence that once the line is quiet, the shape of the close does not matter.

**The RED: arms A and D on today's code (2026-11-02, the same guests, 30 cycles):**

| Arm | 2.7-fat | 3.7-fat | 3.7-lfs | 4.4-lfs |
|---|---|---|---|---|
| A: today's close with output in flight | 30/30, **8** Failed to send | not reached (wedged in D) | 30/30, **30** Failed to send | 30/30, **30** Failed to send |
| D (as run): a short command runs to completion, then an immediate close | 30/30, 0 | **WEDGED at cycle 23**: 1 Failed to send, 2 socket errors, a backend re-init, 2 fatal lines | 30/30, 0 Failed to send, 1 socket error | 30/30, 0 Failed to send, 2 socket errors |

- **Arm A:** a send failure on almost every close of a talking guest, against 0 in 120 for arm B (quiesce first). The close path's timing is the trigger.
- **Arm D was meant to cancel a running command, but it never did.** Every recorded cycle has `cancelled_mid_run: false`: `help` finished before the 0.15 s cancel. What it measured is the end of every ordinary bed test, a short command to completion and then an immediate close, and that **reproduced the wedge**. 3.7-fat's journal reads `Telnet socket 7 error (104)`, then `Failed to send 128, shutting down` about 0.9 s after a 1–2 ms close (the prompt's trailing output), then `Telnet shell backend initialized`. After that the guest refused every connection until its unit was restarted, which is the reporter's exact signature.
- **The true cancellation path is not yet measured.** On it, a caller's `wait_for` cancels `run_cmd` mid-output. The in-flight read is cancelled (telnetlib3 clears its waiter), and `run_cmd` marks the session `_needs_recovery` and re-raises (`session.py:1049-1056`). Nothing closes the stream until `host.close()`, which today releases it through `TelnetSession.close`'s bare `self._writer.close()` (`session.py:1661-1673`). Under this design that becomes the client's close, which drains first. So the design reaches the cancellation path; the evidence for it comes from arm E below.
- **Arm E (run 2026-11-02): a true mid-output cancel cannot be produced on these guests.** The arm sent `demo hexdump otto000 … otto059`, one 492-byte command line with 5.3 KB of output, and cancelled at its first output line. Every cycle on all four guests was void with "run completed before cancel": otto had already received all 5,368 bytes by then. The stock `shell_module` sample emits a command's output in one burst within milliseconds. No command in it produces output slowly: no sleep, no repeat count, and no net shell. So on these guests a caller's cancellation lands either before the output (the guest is silent and still computing) or after it (only the trailing prompt is left). That is arm D's shape, and the drain covers it.
- **Output that outlasts the drain budget** can still happen on a device whose command streams slowly. On Zephyr a shell command runs synchronously and Ctrl-C cannot interrupt it, so the guest goes quiet only when its command finishes. That case is covered hostless: a fake peer keeps sending for longer than the budget, and the release aborts at the deadline. The cost is today's outcome, reached 2 s later. If a field report shows it, the one budget constant is raised; nothing else changes. `close()` does not gain a recovery step.
- **A session closing with `_needs_recovery = True`** closes through the same one line as every other session, with no pre-step. The call-point test includes such a session, so no recovery branch creeps into `close()`.

Raw results: the session scratchpad `386-diag/result-*.json`, and `wedge-zephyr37_fat-journal.txt`.

## Goals

1. **One release decision, one place.** Every release of a telnet-framed stream goes through one async function. For a single-client console, that function drains to quiet before anything closes. The emergency paths that have no loop go through one sync abort function.
2. **The drain is #324's drain, moved, not copied.** `TelnetSession._quiesce`, the `ShellSession._quiesce` hook and `_fail_init`'s explicit call are deleted. `_fail_init` gets the drain through `close()`. The constants stay: one quiet window and one budget.
3. **The slot is always freed.** Every path out of the release ends with the transport closed or aborted, including cancellation and errors. The original transport is captured before anything closes, because telnetlib3's `TelnetWriter.close()` clears `writer.transport`.
4. **One closer per stream.** A shared stream is released once. A half-released stream is never handed out again by the connection cache. Any `close()` returning means the stream is gone, so a parent never tears down a dependency under a drain still running.
5. **Bed proof:** with today's close, arm A and arm D show `Failed to send` and wedges. After the fix, the same arms show zero on all four guests, and a full `coverage-bed` passes with no `embedded bed unhealthy`.

## Non-goals

- **#761** (the console login handshake race on ser2net). It is a separate decision, and it lands after this fix. Its test2 console bed run also re-checks this fix on ser2net.
- **Lifting `_FANOUT_EXCLUDED["zephyr_27_fat"]`.** 2.7 starves after any send failure, including one from the pytest-timeout abort sweep, which stays. Lifting it is a follow-up landing gated on consecutive green nightlies.
- **The reporter's side asks:** auto-restarting a wedged guest between modules, and reporting follow-on errors as skips or one summary. Those are test infrastructure.
- **`survey/dial.py`** (`dial_direct`, ~108-136): it reads a banner from a port to classify it and is not a console session. It keeps its plain close, and a comment says why.
- **The nc data channels** (`src/otto/host/transfer/nc.py`): not consoles, and they must never drain. A drain could swallow bytes a size check must see. `_close_writer_bounded` already has the bounded-close-then-abort shape. Sharing a core with it is not worth a behaviour change on a transfer path (its `OSError` arm logs and does not abort), so it stays as it is.
- **Releasing a transport whose event loop has stopped or closed** (Chris, 2026-11-02). The emergency sweep keeps today's behaviour, and the fd goes at garbage collection or process exit. A v6 helper that closed asyncio's private `transport._sock` released the fd, but it left the transport unfinalized, and it raised `ResourceWarning: unclosed transport` at GC under `-W error` (Astra v6). Doing it properly needs deeper private-asyncio access. The wedge happens with a running loop, and this fix covers every running-loop path. This is tracked as #828.

## Design

### The two functions

Both live in `src/otto/host/telnet.py`, beside the emergency registry they maintain (`_register_console_transport` / `_unregister_console_transport`). The quiesce constants move there from `session.py`, and `open_budget` imports them from `telnet.py`. Putting them in `src/otto/host/transport.py` instead would create a module-level cycle: `telnet.py` would call into `transport.py`, which would unregister through `telnet.py`. It would also make `otto.host.session` and `console` load `transport.py` on the import-budgeted surfaces (`host_local_exec`, `get`, `put`, `dispatch_local_warm`), whose ceilings are exact counts.

```python
CONSOLE_QUIESCE_QUIET_WINDOW: float = 0.2   # was session._CONSOLE_QUIESCE_QUIET_WINDOW
CONSOLE_QUIESCE_BUDGET: float = 2.0         # was session._CONSOLE_QUIESCE_BUDGET

async def release_telnet_stream(
    reader, writer, transport, *, single_client: bool, reads_idle: "ReadGate | None" = None,
    deadline: float | None = None,  # absolute monotonic; None = now + CONSOLE_QUIESCE_BUDGET
) -> None:
    """Release a stream the caller has already TAKEN. Never raises except CancelledError."""

def abort_telnet_stream(writer, transport) -> None:
    """Release now, synchronously, no drain. Never raises."""
```

**Take, then release.** Every caller first *takes* the stream before its first `await`: it reads `reader`, `writer`, `transport = writer.transport` and `sock = transport.get_extra_info("socket")` into locals, sets its own attributes to `None`, and clears nothing else. **`_released` lifecycle:** each client creates `_released = asyncio.Event()` in `__init__`, already set. `connect()`/`open()` clear it when they install a stream. The release's `finally` and the sync `abort()` set it. So a `close()` on a never-connected or already-aborted client returns at once, and every second closer can await it unconditionally. Whoever took the stream owns its release to the end, and sets `_released` whether the release drained, aborted or was cancelled. There is no shared release task, nothing to shield and nothing to start (Astra v3 N3). A cancellation before the take leaves the stream on its owner, and that owner's own cancellation arm aborts it (see "Cancellation").

**A second closer waits for the first.** It finds nothing to take and awaits `_released`. It does not return at once: a parent's `close()` returning means the stream is gone, so `ConnectionManager.close` cannot close the SSH hop (`connections.py:1279-1285`) under a drain another closer is still running (Astra v4 item 3). The wait is bounded, because the first release has its own deadline and its `finally` always runs. A second closer that is itself cancelled while waiting re-raises; the first closer's release continues. Each client has one sync `abort()`: it takes whatever the client still holds and calls `abort_telnet_stream`, and on a client that has already been taken it does nothing. `ConsoleClient.abandon` becomes this method, and `TelnetClient` gains the same one.

`release_telnet_stream`, in order, under one absolute monotonic deadline (`now + CONSOLE_QUIESCE_BUDGET`, taken on entry):
1. **Close the read gate, then wait for in-flight reads to finish** (single-client only), when the caller passes its `ReadGate`: `await reads_idle.wait_idle(deadline)`. Reads run inline in their callers' tasks, so a close in another task cannot cancel them. otto counts its own reads instead (see "The read gate"). At the deadline the release aborts, with a debug log. telnetlib3's internal `_waiter` is not consulted: it is cleared when data arrives, before the reading task resumes, and a buffered `read()` bypasses it (Astra v3 N2, reproduced).
2. **Drain to quiet** (single-client only): read and discard in fixed 4 KiB reads until the line has been silent for `CONSOLE_QUIESCE_QUIET_WINDOW`, it reaches EOF (`if not data`; streams are bytes-mode), or the deadline. This is the step that prevents the wedge. It runs while the connection is ESTABLISHED, so the guest's queued output is sent and read rather than refused. It reduces the risk of closing while output is in flight. It is not a packet-shape guarantee, but arm B measured 0 send failures in 120 closes with it.
3. **Close:** `writer.close()`. No half-close step: on Zephyr a FIN is as fatal as an RST while output is queued.
4. **Wait for otto's socket to be released:** telnetlib3 clears `writer.transport` and reports the writer closed before asyncio has flushed and released the socket. Its `connection_lost`/closed future cannot be used either, because `writer.close()` invokes it proactively. An empty buffer plus `is_closing()` is not enough: with an empty buffer that predicate is true before asyncio's scheduled `_call_connection_lost` closes the fd (Astra v4 item 1, Fable v4 N-b, probed with a real `TelnetWriter` over a socket pair). So the release polls through `wait_for_async` until the deadline, waiting for `transport.get_write_buffer_size() == 0` and the captured `sock` to report `fileno() == -1`. `sock` is None only for a transport without a socket (an asyncssh channel or a test fake); there `is_closing()` and an empty buffer are the best signal available. It does not rely on `writer.wait_closed()` or the writer's flag (Astra v3 N6).
   - **What this proves:** otto's local socket is released. Behind an asyncssh forward, that does not prove the hop's guest-facing socket has closed or that the console slot is free. The spec claims only local release. Remote availability is measured on the bed, by arm C's reconnect and by the promoted bed test's reconnect after every arm.
5. **Unregister** the transport from the emergency registry, and only now. Until then, a pytest-timeout sweep must still be able to abort it.
6. **On a deadline expiry in steps 1, 2 or 4, any exception, or CancelledError:** `abort_telnet_stream(writer, transport)` requests the abort, *without* unregistering. Then the same completion wait as step 4 runs: poll for `fileno() == -1` for a short bounded grace of a few loop turns (asyncio's selector abort closes the fd on the next turn). Only then does step 5 unregister. On CancelledError the arm catches the cancellation, runs that bounded wait, and re-raises. A second cancellation during the grace re-raises at once.

**One completion policy for every exit** (Astra v4/v5 item 1). The transport is unregistered, and `_released` is set, only once local release has been *observed* (`fileno() == -1`, or no socket to observe). If release is not observed in time (repeated cancellation, or a stopped loop), the transport **stays registered**. The registry is then the hand-off: the emergency sweep owns it. `_released` is still set in the taker's `finally`, so no closer waits forever. The guarantee a returning `close()` gives is therefore "released, or handed to the sweep", never a silent leak. The deadline and cancellation tests inspect the captured fd and the registry, not only that `abort()` was called.

A non-single-client stream skips steps 1–2 and costs only the close and the flush.

### The read gate

`ReadGate` lives in `telnet.py` with the release. It is the stream's, so the **client owns it** (`TelnetClient._read_gate`, `ConsoleClient._read_gate`), and the session borrows it at construction. A client that `ConnectionManager.telnet()` rebuilds gets a fresh gate. The grep finds no existing in-flight read marker in otto (the session locks decide creation and named opens; the dial locks decide dials), so this is a new decision with one home (Fable v4).

- **State:** an in-flight counter and a `closed` flag. Admission is decided by the flag, which is set synchronously, so there is no window between "closed" and "no new reader".
- **`enter()` / `leave()`**, used as a context manager around each ordinary read. `enter()` on a closed gate raises an error that `_connection_lost_errors()` classifies as a lost connection, so `expect` and `run_cmd` mark the session dead, as on EOF. A command whose next read arrives after the close fails like a dropped line, and never collides with the closer's own `read()` (telnetlib3 `RuntimeError("already waiting")`, `stream_reader.py:157-161`). A read that entered before the close keeps the reader it captured until it finishes.
- **`wait_idle(deadline) -> bool`:** sets `closed`, then polls `in_flight == 0` with `otto.utils.wait_for_async(lambda: gate.in_flight == 0, remaining, interval=0.02, …)`. The predicate is a plain counter check, never an awaitable: `wait_for_async` awaits an awaitable predicate to completion before checking its deadline (`utils.py:522-526`), so an `Event.wait` predicate would hang on a read that never finishes (Fable v4 B2). It returns False at the deadline. It is idempotent, so the console close and step 1 can both call it.
- **After `wait_idle` returns True, the closer has exclusive use of the stream.** Admission is shut and no ordinary read is in flight. The closer's own reads (logout, the drain) call the ungated read core directly, on the taken reader.
- **Every ordinary application read of the stream enters the gate** (Astra v4 item 2). There are three read entry points, each gated once:
  - **`TelnetSession.read_transport_until`**, which ordinary session reads and `finish_character()` go through. It binds `reader = self._reader` once and wraps **both** of its reads (`session.py:1577`, `:1586`). The session's attributes are never nulled mid-read; the closed gate, not a `None` attribute, keeps new reads out. A running `run_cmd` issues several reads per command (`:950`, `:1155`), so a "reader is None" check would not stop its next read (Fable v4 B1). `TelnetSession._reader` being a separate reference from `client.reader` therefore does not matter.
  - **`ConsoleClient._read_chunk`** (`console.py:204`): every console read goes through it, including login, classification, settling, refusal handling, the session's `_refused_login()` and `console_login()` → `login_sequence()` (`session.py:1634`). It splits into a gated `_read_chunk` and the ungated core it wraps. Logout and the drain call the core.
  - **`TelnetClient.login`'s `reader.readuntil()`** (`telnet.py:331`). Echo negotiation waits on writer state, not the reader, so it is not a consumer.
- **The interactive bridge's `_BridgeReader`** reads outside the gate (`interact.py:1070`). It stays ungated, but only because of an invariant this change makes true: **both bridge pumps are cancelled and awaited before `client.close()` on every exit.** Today `_run_bridge` cancels them only on the normal `asyncio.wait` path (`interact.py:817-834`). A cancellation while that wait is suspended jumps to the `finally` with both pumps still pending, and `unix_host.py:855-858` / `:946-948` then close the client under a live `reader.read(4096)` (Astra v5, reproduced). The pump cancel-and-await moves into `_run_bridge`'s `finally`. A test cancels the bridge with a remote read pending, then closes the client, and asserts no "already waiting" and a clean drain. Do not add a second gate there.

**Why not half-close then drain to EOF (v1)?** On Zephyr the FIN is the trigger. v1's sequence would have caused the wedge it was meant to cure.

### The sites after the change

| Site | After |
|---|---|
| `TelnetSession.close` | The session always holds the client whose stream it uses. Today the shared-client session is built from the reader/writer only (`session.py:3276-3282`), so its construction passes the cached client too, as `_client`, and borrows that client's `ReadGate`. Close is then one line: `await self._client.close()` for a `TelnetClient`, or `await self.console_client.close()` for a console. The bare `self._writer.close()` is deleted. Whether the session owns the client (named or exec-pool) or shares it (the default session), the client's `close()` is the one closer. A session marked `_needs_recovery` closes the same way, with no pre-step. |
| `TelnetClient.close` | Takes the stream (see "Take, then release"), then `await release_telnet_stream(reader, writer, transport, single_client=self.options.single_client_console, reads_idle=self._read_gate)` whenever a writer was taken. There is no "established" flag: a stream cancelled mid-negotiation has a live guest typing its prompt and gets the same drain. Step 6's abort-on-cancel keeps the fd-release guarantee that 363b60f4c added `abort()` for. `alive` is False once the stream is taken. |
| `ConsoleClient.close` / `_release` | Logout and release are one operation, behind the take. The order is: take the stream (before any await, as every closer does), then `await self._read_gate.wait_idle(idle_deadline)`, then `logout(reader, writer)`, then `release_telnet_stream(…, deadline=release_deadline)`, whose step 1 calls the idempotent `wait_idle` again at no cost. Everything after the take runs inside the taker's `try/finally`, so the console's CancelledError arm always holds the taken stream. **If the idle wait returns False** (a read never finished), the close skips logout and the drain and goes straight to the abort-and-observe path of step 6. Running logout then would read concurrently with the outstanding reader. **Deadlines, counted once:** the idle wait and the drain share one `CONSOLE_QUIESCE_BUDGET`. Logout's time is not charged to it, because logout has its own allowance. So `idle_deadline = take time + CONSOLE_QUIESCE_BUDGET`, and `release_deadline = idle_deadline + (time logout took)`. `release_allowance` counts that budget once. Logout's own allowance stays separate in `connect_allowance`. The gate wait comes before logout because an in-flight session read during logout's `_collect` would hit the same "already waiting" error, and logout's never-raises contract would swallow that into a silent failed logout. Logout is `logout(reader, writer)`: logout takes the streams as parameters, since today it reads `self.reader`/`self.writer` (`console.py:449-475`), and it reads through the ungated core. Logout keeps its **existing** budget, unchanged: one `login_timeout` collection, or, when `_shell_unheard`, a `login_timeout` collection, a settle and a second `login_timeout` collection. `connect_allowance` already counts that as `2 * login_timeout + settle` (`connections.py:766`), so the allowance is not restated anywhere else. After logout, `close()` runs `release_telnet_stream(…, single_client=True)` under its own `CONSOLE_QUIESCE_BUDGET`. A second `close()` finds nothing to take, so logout never runs twice. `_release` becomes async. |
| `ConsoleClient.abandon` | Becomes the shared client `abort()` (see "Take, then release"): it takes the stream, then calls `abort_telnet_stream(writer, transport)`. Same behaviour, one copy. |
| `abort_console_transports()` | Unchanged: a sync sweep of the registry. Its claim is now stated precisely. asyncio's selector `abort()` *schedules* the fd's release, so with a running loop the fd goes on the loop's next turn. With a stopped or closed loop, the fd is released when the transport is garbage-collected or the process exits, as today. Releasing it sooner is a non-goal (see Non-goals). |
| `ShellSession._fail_init` | Drops its `_quiesce()` call, since `close()` drains now. `_quiesce` and its hook are deleted. |

### One closer, and no drain under a pending read

Astra (v1 review item 1) and Fable (item 8) found two ways v1 would fall back to abort silently on the main Zephyr path:
- **Two closers.** The default Zephyr session closes the manager's cached stream, then the manager closes it again. That is already true today.
- **A pending read.** If the session's own read is still waiting on the `StreamReader`, a second `read()` raises `RuntimeError` ("already waiting"), and v1's catch-all would abort.

The design:
- **The client's `close()` is the one closer, and it takes before it awaits.** The first `close()` takes the stream. Every later one finds `None` and waits on `_released`, so it returns only once the stream is gone. `alive` is False once the stream is taken, so `ConnectionManager.telnet()` never hands out a stream mid-release, and its stale check finds a client with nothing left to close. Reconnect after a released client is a hostless test.
- **In-flight reads are counted by otto, not inferred from telnetlib3.** See step 1. A hostless test covers four cases: a read pending on an empty buffer, a read with buffered unmatched data, a wakeup before the reading task resumes, and a read that never finishes (abort at the deadline).

### Cancellation

- **Cancelled during `ConnectionManager.close()`:** keep its documented take-then-clear contract (`connections.py:1234-1247`, mirrored in `SshHopTransport`). Today three awaits (sftp, ssh, ftp) run before `telnet.close()`, and a cancellation in any of them leaks the taken telnet and console clients. Nothing runs "next": `host.py:2580` drops managers unclosed. So the manager releases its telnet and console clients **first**, before any other await. Its `except CancelledError` arm covers the whole interval from detaching the clients to the end. It calls each detached client's sync `abort()` and then re-raises. A client whose `close()` never started still owns its streams, so `abort()` takes and aborts them. A client already taken is a no-op. No aborted stream stays installed in a client attribute (Astra v4 item 3).
- **Cancelled during logout (`ConsoleClient`):** the stream is already taken. The `except CancelledError` arm aborts the taken stream and re-raises. A cancelled close does not start a fresh 2 s drain; the caller asked to stop.
- **Cancelled during the drain or the flush wait:** abort the taken stream and re-raise.
- **Cancelled at asyncio shutdown:** `asyncio.run` cancels every pending task. A release that has started aborts in its cancellation arm. A close task cancelled before it ever starts runs no code, so its stream stays on its owner and stays registered. `asyncio.run()` returning therefore does not guarantee the socket was released. The stream stays registered, so the emergency sweep still sees it. Once the loop has stopped, the fd goes at garbage collection or process exit, as today (see Non-goals). The shutdown test asserts that a release which had *started* aborts in its cancellation arm. It does not claim that `asyncio.run()` returning releases a never-started close's socket.
- **A cancellation inside telnetlib3's own opener**, before otto has the writer (`telnetlib3/client.py:683-698`): a bare `try/except` around the call has nothing to close. otto passes a protocol factory that wraps the existing NVT factory composition (it does not replace it) and records the protocol it builds. Registration has one site: the wrapper registers an exclusive transport with the emergency registry in `connection_made()` (`nvt.py:110` already has that hook). `TelnetClient.connect`'s post-open `_register_console_transport` (`telnet.py:284`) is deleted. The wrapper takes the exclusive flag from `_open_kwargs()`, for both `TelnetClient.connect` and `ConsoleClient.open` (`console.py:550`), which use the same opener. There are two windows (Astra v4 item 5), and each has its own test:
  - **Establishment:** asyncio's `_create_connection_transport` is still awaiting its setup waiter (CPython `base_events.py`). A cancellation there makes asyncio close the transport itself before the error reaches otto. otto's arm finds a transport that is already closing, so no drain is possible. It aborts, waits for the socket's release within the budget, unregisters and re-raises. This runtime-initiated close is accepted: telnet has not yet been negotiated, and that window is a single loop turn.
  - **Negotiation:** the transport is up and telnetlib3 is negotiating. otto takes the recorded protocol's reader, writer and transport and routes them through `release_telnet_stream`, with the same single-client policy as any other release. The guest is already sending its banner, so it gets the drain too, even though the caller is cancelling. That is arm A's shape, and the drain is what avoids the wedge. The cost: an `asyncio.wait_for` caller's timeout is extended by up to `CONSOLE_QUIESCE_BUDGET`, and a second cancellation during that drain aborts (step 6). With no usable stream it aborts the transport. The regression asserts drain-before-close ordering, not only the absence of a `ResourceWarning` under `-W error`.

### Which streams are single-client: one source

- The drain and the emergency registration are the same decision: this stream holds an exclusive slot. For a `TelnetClient` that is `options.single_client_console` (`options.py:431`), which already decides registration (`telnet.py:284`). For a `ConsoleClient` it is always true, since ser2net is exclusive by nature. The command frame's `single_client_console` attribute is diagnostic only, by its own docstring (`command_frame.py:110-130`), and is not consulted.
- `open_budget` (`session.py:2907-2910`) currently adds the quiesce budget only when the frame's flag is set, so after this change a unix host on the `console` term would drain on every release without that time being counted. A new `ConnectionManager.release_allowance(term)` beside `connect_allowance` answers from the same source as the drain, and `open_budget` uses it.

### Teardown time

- Healthy single-client console: +0.2–0.35 s per stream (measured 0.34 s). Everything else: +0.
- A wedged or still-talking guest: up to `CONSOLE_QUIESCE_BUDGET` (2 s) per stream before the abort.
- A ser2net console adds its logout first, under logout's existing `login_timeout` allowance, which is unchanged and already counted by `connect_allowance`.
- `BaseHost._close` releases the default session, then the named sessions concurrently (`session.py:3934-3945`), then the manager's clients. That is at most about 4 s per wedged single-client host. Within one dependency layer, hosts close concurrently. `invocation._sweep()` (`invocation.py:702`) serializes layers, and its 10 s deadline bounds the whole sweep by cancellation, not by a guarantee that every graceful close finishes. A host still draining at the deadline is cancelled, and its cancellation arm aborts the stream: today's outcome, reached later. The plan adds a test that tears down four single-client hosts that never go quiet, in one layer, and asserts the sweep ends within the deadline and every transport is aborted or closed.

## Tests

**Hostless (fake reader/writer, plus a real `TelnetWriter` over a loopback pair where the library's behaviour matters):**
- `release_telnet_stream`, single-client: waits for in-flight reads, drains to quiet, closes, waits for the flush, unregisters; no abort. Non-single-client: no wait and no drain.
- A peer that never goes quiet: abort at the deadline, using the captured transport. Run with the real `TelnetWriter`, so the "transport cleared by close()" bug cannot come back.
- A quiet reader with blocked outgoing writes (a stalled fake transport under a real `TelnetWriter`): abort at the deadline, and the transport stays registered until then.
- CancelledError during the read wait, the drain or the flush wait: abort, then re-raise.
- The `ReadGate` cases from "One closer", plus: a read that enters after `wait_idle` closed the gate fails as a lost connection, and the drain's `read()` never sees "already waiting"; a console close with an in-flight read waits for it before logout.
- The emergency sweep during a blocked drain still aborts the transport.
- Two concurrent `close()` calls: one take, one release. The second returns only after the first's release ends, and logout runs once. A session close and a manager close running concurrently: the SSH hop closes only after the console stream's release ends (Astra v4 item 3).
- The release returns only once the captured socket reports `fileno() == -1`. The test uses a real `TelnetWriter` over a socket pair, so a predicate that fires before `_call_connection_lost` would fail it.
- `ConnectionManager.close` cancelled between detaching its clients and closing the first: every detached client's streams are aborted, including a console whose `close()` never started, and no client attribute still holds an aborted stream.
- A deadline expiry and a cancellation each leave the fd at -1 and the transport unregistered. A cancellation repeated during the grace leaves the transport still registered, and a later sweep releases it.
- `ConsoleClient.close`: a cancellation during the initial idle wait aborts the taken stream; two closers during that wait give one release; an idle wait that times out skips logout and the drain.
- The bridge cancelled with a remote read pending, then the client closed: the pumps are gone before the close, and there is no "already waiting".
- Logout's budget: `_shell_unheard` costs at most `2 * login_timeout + settle`, matching `connect_allowance`.
- Every read entry point (`read_transport_until`, `ConsoleClient._read_chunk`, `TelnetClient.login`) is rejected after `wait_idle` closes the gate. A read that entered before the close finishes on its captured reader.
- `alive` is False once the stream is taken. Reconnect after a released client works.
- Cancellation before the take, during logout, and at `asyncio.run` shutdown: the stream is aborted, never leaked.
- The cancel-then-close sequence (`run_cmd` cancelled mid-output, then `host.close()`): drain before the first close or abort, and a successful reconnect afterwards.
- A cancellation inside `open_telnet_connection`, in each window:
  - **Establishment:** the transport is aborted, its socket is released and it is unregistered.
  - **Negotiation:** drain-before-close ordering.
  - **Both:** the exclusive transport is registered from `connection_made()` on, the NVT factory composition is preserved, and there is no `ResourceWarning` under `-W error`.
- `_fail_init` still drains, now through `close()`.
- **One test drives every async site through the same fake stream and asserts the same sequence**, including a session with `_needs_recovery = True`. This is the call-point guard.

**Bed (the diagnostic script, promoted to an integration test marked for the Zephyr cells):**
- **RED, arms A and D: done** (see "The RED" above). Arm A: 68 `Failed to send` across three guests. Arm D (a short command, then an immediate close): one wedge on 3.7-fat, plus socket errors on two more.
- **Arm E: not producible on these guests** (see above). The slow-stream case is a hostless test instead, and the promoted bed test keeps arm E with its void guard, for a future guest with a slow command.
- **GREEN:** on the fixed tree, arms A–D all show zero `Failed to send`, zero socket errors and no wedge on all four guests (30 cycles each), plus a clean `coverage-bed`. If any arm faults, the landing does not proceed.
- ser2net's console path is re-checked by #761's bed run next.

## Docs

- `docs/configuration/host-options.md:364` (`single_client_console`, which is also where the emergency registry is documented): otto drains the console to quiet before every release, unregistering happens at release, and the emergency sweep can preempt a drain.
- `docs/architecture/testing.md:489`: the same, for the bed's wedge guard.

No user-facing settings change.

## Risks

- A healthy Zephyr session close is about 0.3 s slower. That is acceptable against a dead console.
- A ser2net line that never goes quiet (a chatty serial console) pays the full 2 s budget, then aborts. That is accepted, and `release_allowance` counts it.
- **A reconnect during another task's release** waits. `alive` is False at the take, so a concurrent task's `ConnectionManager.telnet()` takes its stale branch. That branch's `await self._telnet_conn.close()` (`connections.py:1052`, under `_telnet_lock`) is a second closer, so it waits on `_released`. The reconnect therefore waits up to the release budget (plus logout on a console) and then dials a freed slot.
- `ConnectionManager.close()` releasing telnet and console first changes its step order but not its take-then-clear contract. The existing teardown tests cover it, plus the cancellation test above.
