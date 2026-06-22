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
from otto.host.options import FtpOptions, SftpOptions, SshOptions, TelnetOptions
from otto.host.telnet import TelnetClient
from otto.host.transport import SshHopTransport


N = 16


async def _let_tasks_settle() -> None:
    """Yield enough times for every concurrent task to either enter the
    locked body or queue on the lock."""
    for _ in range(N + 4):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_concurrent_ssh_opens_one_connection(monkeypatch):
    ready = asyncio.Event()
    calls = 0

    async def fake_connect(*args, **kwargs):
        nonlocal calls
        calls += 1
        await ready.wait()
        return AsyncMock(spec=SSHClientConnection)

    monkeypatch.setattr('otto.host.connections.ssh_connect', fake_connect)

    cm = ConnectionManager(
        ip='1.2.3.4', creds={'u': 'p'}, user=None,
        term='ssh', name='t', ssh_options=SshOptions(),
    )
    tasks = [asyncio.create_task(cm.ssh()) for _ in range(N)]
    await _let_tasks_settle()
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

    monkeypatch.setattr('otto.host.connections.ssh_connect', fake_connect)

    cm = ConnectionManager(
        ip='1.2.3.4', creds={'u': 'p'}, user=None,
        term='ssh', name='t', sftp_options=SftpOptions(),
    )
    tasks = [asyncio.create_task(cm.sftp()) for _ in range(N)]
    await _let_tasks_settle()
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

    monkeypatch.setattr('aioftp.Client', fake_client_factory)

    cm = ConnectionManager(
        ip='1.2.3.4', creds={'u': 'p'}, user=None,
        term='ssh', name='t', ftp_options=FtpOptions(),
    )
    tasks = [asyncio.create_task(cm.ftp()) for _ in range(N)]
    await _let_tasks_settle()
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

    monkeypatch.setattr('otto.host.connections.TelnetClient', fake_telnet_factory)

    cm = ConnectionManager(
        ip='1.2.3.4', creds={'u': 'p'}, user=None,
        term='telnet', name='t', telnet_options=TelnetOptions(),
    )
    tasks = [asyncio.create_task(cm.telnet()) for _ in range(N)]
    await _let_tasks_settle()
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
    tasks = [asyncio.create_task(hop.get_tunnel()) for _ in range(N)]
    await _let_tasks_settle()
    ready.set()
    results = await asyncio.gather(*tasks)

    assert calls == 1, f"hop factory should be called once, got {calls}"
    assert all(r is results[0] for r in results)
    assert hop._conn is results[0]
