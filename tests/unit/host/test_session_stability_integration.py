"""Tier 2 stability tests — real telnet/SSH against the Vagrant lab.

Mirrors the race scenarios in ``test_session_concurrency.py`` but exercises
real transports (asyncssh / telnetlib3 / aioftp / scp / nc), catching what
the FakeSession path can't: asyncssh connection-pool dynamics, real
cancellation paths, FD/process leaks, transfer-protocol stability under
fan-out.

All tests are gated by ``@pytest.mark.integration`` so the existing
``-m "not integration and not hops"`` filter excludes them from
``make ci``. Run via ``make stability-local`` (requires ``vagrant up``).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast

import pytest

from otto.host.remoteHost import RemoteHost
from otto.utils import CommandStatus, Status

from tests.unit.host._transfer_retry import transfer_with_retry


# Real I/O is meaningfully slower than mocked; bump the per-test ceiling.
# Filter the unraisable-warnings cascade: when the recreate race leaks
# transports, their __del__ raises ResourceWarning later, which pytest's
# unraisableexception plugin attributes to whichever test happens to be
# running at GC time. That mis-attribution drowns the real RED test signal,
# so suppress it here. The leaks themselves remain visible via the
# OTTO_DETECT_ASYNCIO_LEAKS fixture (which prints, not raises).
pytestmark = [
    pytest.mark.timeout(60),
    pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning"),
]


_SSH_AND_TELNET = pytest.mark.parametrize("host1", ["ssh", "telnet"], indirect=True)
_SSH_ONLY = pytest.mark.parametrize("host1", ["ssh"], indirect=True)
_TELNET_ONLY = pytest.mark.parametrize("host1", ["telnet"], indirect=True)

_TRANSFERS = pytest.mark.parametrize(
    "transfer_host",
    ["scp", "sftp", "ftp", pytest.param(("nc", "telnet"), id="nc-telnet")],
    indirect=True,
)

_NC_TELNET = pytest.mark.parametrize(
    "transfer_host",
    [pytest.param(("nc", "telnet"), id="nc-telnet")],
    indirect=True,
)


# ── 1. Oneshot pool fan-out ───────────────────────────────────────────────────

@_SSH_AND_TELNET
@pytest.mark.asyncio
@pytest.mark.integration
async def test_real_oneshot_pool_high_fanout(host1: RemoteHost) -> None:
    """8 concurrent oneshots over real transport must complete with intact output.

    Mangled output (call ``i``'s marker showing up in call ``j``'s result)
    is the smoking gun for double-checkout of a pooled telnet session or
    cross-channel contamination on SSH.

    N=8 sits below sshd's default ``MaxSessions=10`` per-connection channel
    cap so this test focuses on otto's pool / cache correctness rather than
    on the daemon's connection-limit backpressure (a different, infrastructure-
    level concern that surfaces at N>=10 on a default sshd).
    """
    N = 8
    results = await asyncio.gather(
        *(host1.oneshot(f'echo concurrent_{i}') for i in range(N)),
        return_exceptions=True,
    )

    exceptions = [r for r in results if isinstance(r, BaseException)]
    assert not exceptions, f"{len(exceptions)} oneshots raised; first: {exceptions[0]!r}"

    statuses = cast(list[CommandStatus], results)
    failed = [(i, r) for i, r in enumerate(statuses) if not r.status.is_ok]
    assert not failed, f"{len(failed)} non-ok statuses; first: {failed[0]}"

    for i, r in enumerate(statuses):
        assert f'concurrent_{i}' in r.output, (
            f"oneshot {i} got mangled output: {r.output!r}"
        )


# ── 2. Named session resurrection ─────────────────────────────────────────────

@_SSH_ONLY
@pytest.mark.asyncio
@pytest.mark.integration
async def test_real_named_session_resurrect(host1: RemoteHost) -> None:
    """``open_session(name)`` must return a fresh live session after transport death."""
    s1 = await host1.open_session('resurrect_test')
    initial_id = id(s1._session)

    result = (await s1.run('echo first')).only
    assert result.status.is_ok
    assert s1.alive

    # Simulate transport death by closing the underlying ShellSession (this
    # closes the real TCP/process, mirroring server-side close + flips alive).
    await s1._session.close()

    s2 = await host1.open_session('resurrect_test')
    try:
        assert id(s2._session) != initial_id, "open_session returned the dead session"
        result = (await s2.run('echo recovered')).only
        assert result.status.is_ok
        assert 'recovered' in result.output
    finally:
        await s2.close()


# ── 3. Default-session recreation under load ──────────────────────────────────

@_SSH_AND_TELNET
@pytest.mark.asyncio
@pytest.mark.integration
async def test_real_default_session_recreate_under_load(host1: RemoteHost) -> None:
    """Default-session recreation under concurrent ``_ensure_session`` is robust.

    Probes the recreate path directly (rather than via concurrent ``run()``,
    which is sequential-only by design and would corrupt shell output even
    when the recreation logic is correct). The Tier 1 test
    ``test_ensure_default_session_recreation_race`` covers the
    "exactly one recreate" invariant via factory-call counting; this test
    proves the lock-protected path holds up under real I/O without
    hangs / exceptions / broken-session aftermath.
    """
    await host1.run('echo init')
    mgr = host1._session_mgr
    sess = mgr._session
    assert sess is not None and sess.alive
    initial_id = id(sess)
    await sess.close()

    M = 20
    results = await asyncio.gather(
        *(mgr._ensure_session() for _ in range(M)),
        return_exceptions=True,
    )

    exceptions = [r for r in results if isinstance(r, BaseException)]
    assert not exceptions, (
        f"{len(exceptions)} _ensure_session calls raised; first: {exceptions[0]!r}"
    )

    new_sess = mgr._session
    assert new_sess is not None
    assert new_sess.alive, "session was not alive after recreation storm"
    assert id(new_sess) != initial_id, "session was not actually recreated"

    # Sequential commands prove the recreated session is fully usable.
    for i in range(3):
        result = (await host1.run(f'echo serial_{i}')).only
        assert result.status.is_ok, f"serial command {i} failed: {result}"
        assert f'serial_{i}' in result.output, (
            f"serial command {i} got mangled output: {result.output!r}"
        )


# ── 4. Long telnet oneshot vs concurrent short oneshots ───────────────────────

@_TELNET_ONLY
@pytest.mark.asyncio
@pytest.mark.integration
async def test_real_long_telnet_oneshot_vs_concurrent(host1: RemoteHost) -> None:
    """Real-transport version of the test_oneshot_telnet_concurrent regression.

    The mocked counterpart in ``test_remoteHost.py`` proves the manager's
    pool dispatch is sane; this version proves the *actual* telnetlib3
    reader/writer state holds up under the same workload.
    """
    long_task = asyncio.create_task(host1.oneshot('sleep 5', timeout=None))
    try:
        # Give the long oneshot a moment to acquire its session.
        await asyncio.sleep(0.5)

        # 10 short oneshots must complete while the long one is still pinned.
        results = await asyncio.wait_for(
            asyncio.gather(
                *(host1.oneshot(f'echo short_{i}', timeout=10.0) for i in range(10)),
                return_exceptions=True,
            ),
            timeout=15.0,
        )
        exceptions = [r for r in results if isinstance(r, BaseException)]
        assert not exceptions, f"{len(exceptions)} short oneshots raised; first: {exceptions[0]!r}"
        statuses = cast(list[CommandStatus], results)
        for i, r in enumerate(statuses):
            assert r.status.is_ok, f"short oneshot {i} failed: {r}"
            assert f'short_{i}' in r.output
    finally:
        # Wait for the long oneshot to finish so the pool isn't left mid-flight.
        await asyncio.wait_for(long_task, timeout=15.0)


# ── 5. Concurrent file transfers ──────────────────────────────────────────────

@_TRANSFERS
@pytest.mark.asyncio
@pytest.mark.integration
async def test_real_concurrent_transfers(
    transfer_host: RemoteHost, tmp_path: Path,
) -> None:
    """5 concurrent ``put`` calls over each transfer protocol must all complete.

    For nc transfers, also verifies no leftover ``nc -l`` listeners on the
    remote — the listener-leak class of bug.
    """
    files = []
    for i in range(5):
        src = tmp_path / f'concurrent_{i}_{transfer_host.transfer}_{transfer_host.term}.txt'
        src.write_text(f'content_{i}')
        files.append(src)

    statuses = await asyncio.gather(
        *(transfer_with_retry(lambda f=f: transfer_host.put([f], Path('/tmp')))
          for f in files),
        return_exceptions=True,
    )

    exceptions = [s for s in statuses if isinstance(s, BaseException)]
    assert not exceptions, f"{len(exceptions)} transfers raised; first: {exceptions[0]!r}"

    for i, item in enumerate(statuses):
        status, msg = cast(tuple[Status, str], item)
        assert status == Status.Success, f"transfer {i} failed: {msg}"

    # Verify all files arrived intact.
    for i, src in enumerate(files):
        remote_path = f'/tmp/{src.name}'
        result = (await transfer_host.run(f'cat {remote_path}')).only
        assert f'content_{i}' in result.output, f"file {i} corrupt: {result.output!r}"
        await transfer_host.run(f'rm -f {remote_path}')

    # nc-only leak check: any leftover listener processes are a bug.
    if transfer_host.transfer == 'nc':
        result = (await transfer_host.run(
            'pgrep -af "nc -l" | grep -v pgrep | grep -v "$$" || true'
        )).only
        leftover = result.output.strip()
        assert not leftover, f"leftover nc listeners after concurrent put: {leftover}"


# ── 6. Cancellation recovery ──────────────────────────────────────────────────

@_SSH_AND_TELNET
@pytest.mark.asyncio
@pytest.mark.integration
async def test_real_cancel_mid_run_recovers(host1: RemoteHost) -> None:
    """Session must recover from external ``wait_for`` cancellation.

    ``host.run(timeout=…)`` has a built-in recovery path; this exercises the
    *external* cancellation path (``asyncio.wait_for`` cancels the
    coroutine task), which routes through different code than the in-band
    timeout.
    """
    try:
        await asyncio.wait_for(host1.run('sleep 30'), timeout=0.5)
    except asyncio.TimeoutError:
        pass  # expected
    except Exception as e:
        pytest.fail(f"unexpected exception during wait_for: {e!r}")

    # 5 successive commands must all succeed — proves the prompt-reading
    # state recovered cleanly. If any command hangs or returns garbage,
    # the cancellation left the session in a bad state.
    for i in range(5):
        result = (await host1.run(f'echo recovered_{i}', timeout=10.0)).only
        assert result.status.is_ok, f"recovery iter {i} failed: {result}"
        assert f'recovered_{i}' in result.output, (
            f"recovery iter {i} got mangled output: {result.output!r}"
        )


# ── 7. nc — concurrent gets ───────────────────────────────────────────────────

@_NC_TELNET
@pytest.mark.asyncio
@pytest.mark.integration
async def test_real_nc_concurrent_gets(
    transfer_host: RemoteHost, tmp_path: Path,
) -> None:
    """5 concurrent nc gets must all complete with intact content.

    Mirrors ``test_real_concurrent_transfers`` for the GET path —
    ``_get_files_nc`` is a separate ~80-line code path (local listener
    rather than remote) and its concurrent behavior wasn't otherwise
    exercised.
    """
    remote_paths = []
    for i in range(5):
        remote = f'/tmp/get_test_{i}_{transfer_host.term}.txt'
        await transfer_host.run(f'echo content_{i} > {remote}')
        remote_paths.append(Path(remote))

    try:
        statuses = await asyncio.gather(
            *(transfer_with_retry(lambda p=p: transfer_host.get([p], tmp_path))
              for p in remote_paths),
            return_exceptions=True,
        )

        exceptions = [s for s in statuses if isinstance(s, BaseException)]
        assert not exceptions, (
            f"{len(exceptions)} gets raised; first: {exceptions[0]!r}"
        )

        for i, item in enumerate(statuses):
            status, msg = cast(tuple[Status, str], item)
            assert status == Status.Success, f"get {i} failed: {msg}"

        for i, p in enumerate(remote_paths):
            local = tmp_path / p.name
            assert local.exists(), f"local file {local} missing after get"
            content = local.read_text().strip()
            assert content == f'content_{i}', (
                f"get {i} content mismatch: got {content!r}"
            )

        # Local-listener leak check: with ``_get_files_nc`` we listen locally
        # and have the remote ``nc`` connect to us. Any leftover ``nc -l``
        # *on the local box* would indicate the orchestrator's local listener
        # wasn't reaped.
        import subprocess
        local_listeners = subprocess.run(
            ['pgrep', '-af', 'nc -l'],
            capture_output=True, text=True,
        ).stdout.strip()
        assert not local_listeners, (
            f"leftover local nc listeners after concurrent get: {local_listeners}"
        )
    finally:
        for p in remote_paths:
            await transfer_host.run(f'rm -f {p}')


# ── 8. nc — high fan-out put ──────────────────────────────────────────────────

@_NC_TELNET
@pytest.mark.asyncio
@pytest.mark.integration
async def test_real_nc_high_fanout_put(
    transfer_host: RemoteHost, tmp_path: Path,
) -> None:
    """20 concurrent nc puts stress port allocation + listener cleanup.

    The cross-protocol ``test_real_concurrent_transfers`` runs at N=5 to
    avoid sshd MaxSessions backpressure for non-nc transfers; nc uses
    ephemeral remote ports rather than SSH channels so it can scale
    further without infrastructure noise. N=20 pushes ``_find_free_port``
    contention and per-transfer listener spin-up/teardown harder than the
    N=5 baseline.
    """
    N = 20
    files = []
    for i in range(N):
        src = tmp_path / f'fanout_{i}_{transfer_host.term}.txt'
        src.write_text(f'fanout_content_{i}')
        files.append(src)

    statuses = await asyncio.gather(
        *(transfer_with_retry(lambda f=f: transfer_host.put([f], Path('/tmp')))
          for f in files),
        return_exceptions=True,
    )

    exceptions = [s for s in statuses if isinstance(s, BaseException)]
    assert not exceptions, f"{len(exceptions)} puts raised; first: {exceptions[0]!r}"

    for i, item in enumerate(statuses):
        status, msg = cast(tuple[Status, str], item)
        assert status == Status.Success, f"put {i} failed: {msg}"

    # Verify all files arrived intact.
    for i, src in enumerate(files):
        remote_path = f'/tmp/{src.name}'
        result = (await transfer_host.run(f'cat {remote_path}')).only
        assert f'fanout_content_{i}' in result.output, (
            f"file {i} corrupt: {result.output!r}"
        )
        await transfer_host.run(f'rm -f {remote_path}')

    result = (await transfer_host.run(
        'pgrep -af "nc -l" | grep -v pgrep | grep -v "$$" || true'
    )).only
    leftover = result.output.strip()
    assert not leftover, (
        f"leftover nc listeners after N={N} concurrent puts: {leftover}"
    )


# ── 9. nc — cancellation cleans up listener ───────────────────────────────────

@_NC_TELNET
@pytest.mark.asyncio
@pytest.mark.integration
async def test_real_nc_cancel_cleans_up_listener(
    transfer_host: RemoteHost, tmp_path: Path,
) -> None:
    """External cancellation mid-transfer must reap the spawned ``nc -l``.

    ``_put_files_nc`` spawns the remote listener as an ``asyncio.Task``;
    when the parent task is cancelled, the spawned task isn't
    automatically cancelled, so a careless cleanup path leaves an
    orphaned ``nc -l <port>`` running on the remote indefinitely.
    """
    # 20MB file: large enough that the transfer is reliably mid-flight when
    # the cancel timeout fires, small enough not to slow the test down.
    src = tmp_path / 'cancel_target.bin'
    with open(src, 'wb') as f:
        f.seek(20 * 1024 * 1024 - 1)
        f.write(b'\0')

    # Snapshot listener count before — if the remote already has stray
    # listeners from a prior test, attribute that separately.
    before = (await transfer_host.run(
        'pgrep -af "nc -l" | grep -v pgrep | grep -v "$$" || true'
    )).only.output.strip().splitlines()

    try:
        await asyncio.wait_for(
            transfer_host.put([src], Path('/tmp')),
            timeout=0.2,
        )
    except (asyncio.TimeoutError, asyncio.CancelledError):
        pass  # expected — we cancelled mid-transfer

    # Allow a brief grace period for cleanup to settle before checking.
    await asyncio.sleep(2.0)

    after = (await transfer_host.run(
        'pgrep -af "nc -l" | grep -v pgrep | grep -v "$$" || true'
    )).only.output.strip().splitlines()
    new_listeners = [line for line in after if line not in before]
    assert not new_listeners, (
        f"cancellation orphaned remote nc listener(s): {new_listeners}"
    )

    # Cleanup any partial file from the cancelled put.
    await transfer_host.run('rm -f /tmp/cancel_target.bin')
