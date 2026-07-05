"""Hostless AppShell concurrency/soak tests — ``python3`` REPL over ``LocalHost``.

No Vagrant bed, no network transport: every test here drives a real
``python3 -u -i`` subprocess through :class:`~otto.host.local_host.LocalHost`.
Flushes flakiness in the AppShell lifecycle (launch/cmd/quit +
``_recover_session``-on-exit), the session-lock (``AppShellActiveError`` while
attached), and cross-session isolation under concurrent fan-out. Runs via
``make stability-unit`` (``-m concurrency``, repeated by ``pytest-repeat
--count``) and also stays in ``make coverage`` (fast, single pass).

``PyRepl``/``Version`` mirror the local-only fixtures in
``tests/e2e/host/test_app_shell_e2e.py`` (self-contained here rather than
imported, matching that module's own self-contained mysql fixtures).
"""

import asyncio
import re
from collections.abc import AsyncIterator
from typing import cast

import pytest
import pytest_asyncio

from otto import AppShell, Parsed
from otto.host.app_shell import AppShellActiveError
from otto.host.local_host import LocalHost

pytestmark = [pytest.mark.concurrency]


class PyRepl(AppShell):
    """The stock CPython interactive interpreter as an :class:`otto.AppShell`."""

    launch = "python3 -u -i"
    prompt = re.compile(r">>> \Z")
    quit_cmd = "exit()"


class Version(Parsed):
    """``major.minor`` extracted from a ``sys.version_info`` print."""

    pattern = re.compile(r"(?P<major>\d+)\.(?P<minor>\d+)")
    major: int
    minor: int


class Square(Parsed):
    """A single printed integer — used to give each concurrent REPL a distinct value."""

    pattern = re.compile(r"(?P<value>\d+)")
    value: int


@pytest_asyncio.fixture
async def local_host() -> AsyncIterator[LocalHost]:
    """A :class:`~otto.host.local_host.LocalHost`, closed on teardown."""
    host = LocalHost()
    try:
        yield host
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_pyrepl_cycle(local_host: LocalHost) -> None:
    """Full launch/cmd/quit lifecycle, repeated by ``--count`` to soak recovery.

    ``async with local_host.app_shell(PyRepl)`` launches ``python3 -u -i``,
    runs one line printing the interpreter's ``major.minor`` version parsed
    into a typed :class:`Version`, then quits. The closing ``local_host.run``
    uses the host's SEPARATE default session, so its success proves the
    app-shell round trip (launch, command framing, quit, ``_recover_session``)
    did not corrupt the host — not incidentally, since a wedged recovery would
    leave that default session's own handshake broken too.
    """
    async with local_host.app_shell(PyRepl) as py:
        result = await py.cmd(
            "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')",
            parse=Version,
        )
        assert result, f"parse failed: {result.msg} (output={result.output!r})"
        assert result.value.major == 3

    assert (await local_host.run("echo back")).only.value.strip() == "back"


@pytest.mark.asyncio
async def test_run_blocked_while_attached(local_host: LocalHost) -> None:
    """Lock-out while attached AND the same-session recovery proof after detach.

    ``open_session`` + :meth:`AppShell.attach` keeps a handle on the exact
    session the REPL runs in, so both guarantees land on the SAME shell
    process: (1) while ``PyRepl`` is attached, that session's sentinel-framed
    ``run`` raises :class:`AppShellActiveError` — the command frame must never
    be typed into the app; (2) after the block exits, the SAME session's
    ``run`` succeeds again, proving the underlying POSIX shell was restored. A
    broken ``_recover_session`` would leave the shell wedged inside python3,
    so the ``echo ok`` command frame would be typed into the dead REPL and
    this ``run`` would time out rather than pass.
    """
    session = await local_host.open_session("appshell_lock_probe")
    try:
        async with PyRepl.attach(session):
            with pytest.raises(AppShellActiveError):
                await session.run("echo nope")
        assert (await session.run("echo ok")).only.value.strip() == "ok"
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_concurrent_independent_pyrepls() -> None:
    """N independent ``LocalHost``+``PyRepl`` lifecycles run concurrently, isolated.

    Each of N independent hosts runs its OWN app-shell lifecycle concurrently,
    each printing a value distinct to its index (``i * i``). Every result
    must match its own expected value with no exceptions — cross-contamination
    (one instance's REPL output leaking into another's parse) would surface as
    a mismatched or wrong-typed value here.
    """
    N = 4  # noqa: N806 — single-letter math dimension, matches session-concurrency convention

    async def _run_one(i: int) -> int:
        host = LocalHost()
        try:
            async with host.app_shell(PyRepl) as py:
                result = await py.cmd(f"print({i} * {i})", parse=Square)
                assert result, f"parse failed: {result.msg} (output={result.output!r})"
                return result.value.value
        finally:
            await host.close()

    results = await asyncio.gather(*(_run_one(i) for i in range(N)), return_exceptions=True)

    exceptions = [r for r in results if isinstance(r, BaseException)]
    assert not exceptions, (
        f"{len(exceptions)} concurrent PyRepl lifecycle(s) raised; first: {exceptions[0]!r}"
    )
    values = cast("list[int]", results)
    assert values == [i * i for i in range(N)], "cross-session contamination in concurrent PyRepls"
