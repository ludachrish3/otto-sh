"""Regression tests for the connection lazy-init race.

Concurrent callers of ``ConnectionManager.ssh`` / ``telnet`` / ``ftp`` /
``sftp`` (and ``SshHopTransport.get_tunnel``) used to each open their own
real protocol connection on the first call before any of them had cached
the result. The losers were orphaned: nothing ever called ``close()`` on
their transports, so they leaked across the test boundary and surfaced as
``PytestUnraisableExceptionWarning`` on a later, unrelated test — see the
"LEAK after …" diagnostic in :mod:`tests.conftest`.

Each test below stalls every concurrent caller inside the mocked
underlying-connection call until they've all entered, then releases them
and asserts that only **one** real connection was opened. Without the
double-checked-locking fix the assertions read ``N`` instead of ``1``.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from asyncssh import SFTPClient, SSHClientConnection

from otto.host.connections import ConnectionManager
from otto.host.login_proxy import Cred
from otto.host.options import FtpOptions, SftpOptions, SshOptions, TelnetOptions
from otto.host.telnet import TelnetClient
from otto.host.transport import SshHopTransport

N = 16


async def _let_tasks_settle() -> None:
    """Yield enough times for every concurrent task to either enter the
    locked body or queue on the lock."""
    for _ in range(N + 4):
        await asyncio.sleep(0)


def _tracked(coro_fn):
    """Wrap a 0-arg async method with an entry counter — the launch
    positive-control. Honest scope (opus W16 review): `entered == N` proves
    all N task BODIES started before `ready.set()` — a zero/omitted settle
    is caught loudly. It does NOT prove the tasks queued on the lock: an
    await inserted before the lock would park them all mid-method with the
    premise green. The ssh test below adds the true contention proof (lock
    held + N-1 waiters); the other four transports share the same
    ConnectionManager lock pattern and lean on that exemplar."""
    state = {"entered": 0}

    async def call():
        state["entered"] += 1
        return await coro_fn()

    return state, call


@pytest.mark.asyncio
async def test_concurrent_ssh_opens_one_connection(monkeypatch):
    ready = asyncio.Event()
    calls = 0

    async def fake_connect(*args, **kwargs):
        nonlocal calls
        calls += 1
        await ready.wait()
        return AsyncMock(spec=SSHClientConnection)

    monkeypatch.setattr("otto.host.connections.ssh_connect", fake_connect)

    cm = ConnectionManager(
        ip="1.2.3.4",
        creds=[Cred(login="u", password="p")],
        user=None,
        term="ssh",
        name="t",
        ssh_options=SshOptions(),
    )
    entered, tracked_ssh = _tracked(cm.ssh)
    tasks = [asyncio.create_task(tracked_ssh()) for _ in range(N)]
    await _let_tasks_settle()
    assert entered["entered"] == N, f"contention premise: {entered['entered']}/{N} tasks in flight"
    # True contention, not just launch: the winner holds the lock (blocked in
    # fake_connect on `ready`) and every other task is QUEUED on it. Reads a
    # private asyncio.Lock attribute — if CPython renames `_waiters`, this
    # fails loudly by name, which is the acceptable failure mode.
    assert cm._ssh_lock.locked(), "no task holds the connection lock"
    waiters = cm._ssh_lock._waiters
    assert waiters is not None, "lock has no waiter queue — the race is not racing"
    assert len(waiters) == N - 1, (
        f"expected {N - 1} tasks queued on the lock, found {len(waiters)} — the race is not racing"
    )
    ready.set()
    results = await asyncio.gather(*tasks)

    assert calls == 1, f"ssh_connect should be called once, got {calls}"
    assert all(r is results[0] for r in results)
    assert cm._ssh_conn is results[0]


@pytest.mark.asyncio
async def test_concurrent_sftp_opens_one_client(monkeypatch):
    sftp_calls = 0
    ready = asyncio.Event()

    fake_sftp = AsyncMock(spec=SFTPClient)
    fake_ssh = AsyncMock(spec=SSHClientConnection)

    async def fake_start_sftp_client(*args, **kwargs):
        nonlocal sftp_calls
        sftp_calls += 1
        await ready.wait()
        return fake_sftp

    fake_ssh.start_sftp_client = fake_start_sftp_client

    async def fake_connect(*args, **kwargs):
        return fake_ssh

    monkeypatch.setattr("otto.host.connections.ssh_connect", fake_connect)

    cm = ConnectionManager(
        ip="1.2.3.4",
        creds=[Cred(login="u", password="p")],
        user=None,
        term="ssh",
        name="t",
        sftp_options=SftpOptions(),
    )
    entered, tracked_sftp = _tracked(cm.sftp)
    tasks = [asyncio.create_task(tracked_sftp()) for _ in range(N)]
    await _let_tasks_settle()
    assert entered["entered"] == N, f"contention premise: {entered['entered']}/{N} tasks in flight"
    ready.set()
    results = await asyncio.gather(*tasks)

    assert sftp_calls == 1, f"start_sftp_client should be called once, got {sftp_calls}"
    assert all(r is fake_sftp for r in results)


@pytest.mark.asyncio
async def test_concurrent_ftp_opens_one_client(monkeypatch):
    ready = asyncio.Event()
    instances: list[MagicMock] = []

    async def gated_connect(*args, **kwargs):
        await ready.wait()

    def fake_client_factory(**kwargs):
        client = MagicMock()
        client.connect = gated_connect
        client.login = AsyncMock()
        instances.append(client)
        return client

    monkeypatch.setattr("aioftp.Client", fake_client_factory)

    cm = ConnectionManager(
        ip="1.2.3.4",
        creds=[Cred(login="u", password="p")],
        user=None,
        term="ssh",
        name="t",
        ftp_options=FtpOptions(),
    )
    entered, tracked_ftp = _tracked(cm.ftp)
    tasks = [asyncio.create_task(tracked_ftp()) for _ in range(N)]
    await _let_tasks_settle()
    assert entered["entered"] == N, f"contention premise: {entered['entered']}/{N} tasks in flight"
    ready.set()
    results = await asyncio.gather(*tasks)

    assert len(instances) == 1, f"aioftp.Client should be built once, got {len(instances)}"
    assert all(r is instances[0] for r in results)


@pytest.mark.asyncio
async def test_concurrent_telnet_opens_one_client(monkeypatch):
    ready = asyncio.Event()
    instances: list[MagicMock] = []

    async def gated_connect(*args, **kwargs):
        await ready.wait()

    def fake_telnet_factory(*args, **kwargs):
        client = MagicMock(spec=TelnetClient)
        client.alive = True
        client.connect = gated_connect
        client.close = AsyncMock()
        instances.append(client)
        return client

    monkeypatch.setattr("otto.host.connections.TelnetClient", fake_telnet_factory)

    cm = ConnectionManager(
        ip="1.2.3.4",
        creds=[Cred(login="u", password="p")],
        user=None,
        term="telnet",
        name="t",
        telnet_options=TelnetOptions(),
    )
    entered, tracked_telnet = _tracked(cm.telnet)
    tasks = [asyncio.create_task(tracked_telnet()) for _ in range(N)]
    await _let_tasks_settle()
    assert entered["entered"] == N, f"contention premise: {entered['entered']}/{N} tasks in flight"
    ready.set()
    results = await asyncio.gather(*tasks)

    assert len(instances) == 1, f"TelnetClient should be built once, got {len(instances)}"
    assert all(r is instances[0] for r in results)


@pytest.mark.asyncio
async def test_concurrent_hop_tunnel_opens_one_connection():
    ready = asyncio.Event()
    calls = 0

    async def factory(_visited=None):
        nonlocal calls
        calls += 1
        await ready.wait()
        return AsyncMock(spec=SSHClientConnection)

    hop = SshHopTransport(factory)
    entered, tracked_tunnel = _tracked(hop.get_tunnel)
    tasks = [asyncio.create_task(tracked_tunnel()) for _ in range(N)]
    await _let_tasks_settle()
    assert entered["entered"] == N, f"contention premise: {entered['entered']}/{N} tasks in flight"
    ready.set()
    results = await asyncio.gather(*tasks)

    assert calls == 1, f"hop factory should be called once, got {calls}"
    assert all(r is results[0] for r in results)
    assert hop._conn is results[0]
