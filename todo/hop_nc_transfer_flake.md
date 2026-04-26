# Flaky nc transfer through SSH hop

`tests/unit/host/test_hop_integration.py::TestFileTransferThroughHop::test_nc_put_through_hop`
and `::test_nc_get_through_hop` intermittently hang for the full 30 s budget
enforced by `_transfer_retry.transfer_with_retry`, surfacing as
`asyncio.exceptions.TimeoutError`. None of the other transfer modes through a
hop (scp / sftp / ftp) flake — only nc.

The two tests are currently band-aided with `@pytest.mark.retry(3)` (custom
marker registered in `tests/conftest.py`). This unblocks CI but does not fix
the underlying race.

## Suspected race

For PUT (see [src/otto/host/transfer.py:1094](../src/otto/host/transfer.py#L1094)):

1. `_wait_for_remote_listener` confirms the kernel has the socket in LISTEN
   state.
2. `_connections.forward_port(port)` opens an asyncssh local-forward to the
   remote listener.
3. `_connect_with_retry('localhost', local_port)` opens a TCP connection to
   that forward.

Existing comments at [transfer.py:1179-1186](../src/otto/host/transfer.py#L1179-L1186)
already flag the LISTEN-vs-accept-loop window — the kernel can be LISTENing
before nc itself has called `accept()`. The single-hop, non-tunneled case
verifies the destination size and retries once; through an SSH hop, asyncssh
appears to silently swallow the dropped connection and the next `await` (the
`writer.drain()` or `listen_task` await) hangs with no asyncio-level deadline
of its own.

GET-tunneled has the analogous shape at [transfer.py:1051-1066](../src/otto/host/transfer.py#L1051-L1066).

## Investigation plan

1. Add structured logging at each await point inside `_attempt` (PUT) and
   `_get_one` tunneled (GET). Re-run the suite under load until the flake
   reproduces, then identify *which* await is stuck when the 30 s budget
   expires. Candidates, in order of likelihood:
   - `_wait_for_remote_listener` (probe loop itself stalled on `_control_run`)
   - `_connections.forward_port` (asyncssh forward setup)
   - `_connect_with_retry` (the actual TCP connect)
   - `writer.drain()` / `writer.wait_closed()` (data path stuck on a
     half-open tunnel)
   - `await listen_task` (remote nc never returned)

2. Once identified, the fix is most likely either:
   - Tighten the listener-readiness check to verify nc is past `accept()`,
     not just LISTENing (e.g. probe with a throwaway connect to the local
     forward, not just an `ss`/`netstat` LISTEN check).
   - Wrap the data-phase awaits in a per-step `asyncio.wait_for` so a
     stalled tunnel surfaces as an error instead of consuming the whole
     30 s budget on a single attempt.
   - Validate that asyncssh local forwards correctly propagate close from
     the remote side; if not, file/work around upstream.

3. After the root cause is fixed, drop `@pytest.mark.retry(3)` from the two
   tests and remove this file.

## Related

- Single-hop non-tunneled nc PUT already has an internal one-shot retry
  inside `_put_one` ([transfer.py:1194-1204](../src/otto/host/transfer.py#L1194-L1204)) — the
  tunneled path does not, because the failure mode there is a hang, not a
  ghost-empty-file.
- `transfer_with_retry`'s docstring explicitly disclaims retry; do not
  re-introduce retry there as a substitute for fixing the race.
- The `retry` pytest marker is registered globally in `pyproject.toml`
  but the hook implementation lives in two places: `OttoPlugin`
  (production) and `tests/conftest.py` (dev runs).
