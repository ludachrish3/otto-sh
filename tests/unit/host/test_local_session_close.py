"""Closing a local shell session stops its process group, under the shared stop.

The shell leads its own session and process group (``start_new_session``), so
close reaches what it started; what survives is the shared stop's survivor
rule (:func:`otto.host.process_stop.stop_local_session_group` with
:class:`~otto.host.process_stop.LocalStoppable`): a member that ignores
``TERM`` when the shell exits promptly, a process that left the group, and
everything once the shell's own exit is already recorded.

Each stimulus writes a witness ``<pid> <starttime>`` so a test reads the very
process it started, never a reused pid, through the one witness writer,
observer and cleanup (:mod:`tests._fixtures.witness_shell`).
"""

import asyncio
import contextlib
import os
import signal
from collections.abc import Iterator
from pathlib import Path
from typing import Literal

import pytest

import otto.host.session as session_mod
from otto.host import process_stop
from otto.host.session import LocalSession
from otto.utils import Status, wait_for_async
from scripts.process_group import proc_stat, signal_group
from tests._fixtures.witness_shell import (
    NO_WITNESS,
    RUNNING,
    STOPPED,
    clean,
    observation,
    observe,
    witness_write_template,
)
from tests.unit.host._sessions import session_manager

pytestmark = [pytest.mark.asyncio]

_PGRP = 2  # field of proc_stat() after the `pid (comm)` prefix


def _stamp(w: Path) -> str:
    """Shell text that writes the running shell's witness to *w* (the one writer)."""
    return witness_write_template(str(w))


async def _await_witness(w: Path) -> int:
    """Wait for the witness at *w*, check its process is running, and return its pid."""
    await wait_for_async(
        lambda: observe(w) != NO_WITNESS, 2.0, interval=0.01, on_timeout=f"no witness at {w}"
    )
    seen = observation(w)
    assert seen.answer == RUNNING, seen
    assert seen.pid is not None
    return seen.pid


@contextlib.contextmanager
def _reaping(witnesses: "list[Path]") -> Iterator[None]:
    """Clean every witness when the test ends (the one cleanup; test-only)."""
    try:
        yield
    finally:
        for w in witnesses:
            clean(w)


async def _gone(w: Path) -> None:
    await wait_for_async(
        lambda: observe(w) == STOPPED, 2.0, interval=0.01, on_timeout=f"{w}'s process still alive"
    )


@pytest.fixture(autouse=True)
def reap_session_groups(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Kill every shell group a test opened whose shell is still unreaped (test-only cleanup).

    A failing test then leaks nothing it started inside a session's group; a
    process that left the group is the witness cleanup's (:func:`_reaping`).
    """
    opened: "list[asyncio.subprocess.Process]" = []
    open_transport = LocalSession.open_transport

    async def recording(self: LocalSession) -> None:
        await open_transport(self)
        assert self._process is not None
        opened.append(self._process)

    monkeypatch.setattr(LocalSession, "open_transport", recording)
    try:
        yield
    finally:
        for proc in opened:
            if proc.returncode is None:
                signal_group(proc.pid)


@pytest.fixture
def short_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_mod, "_RECOVERY_TIMEOUT", 1.0)
    monkeypatch.setattr(session_mod, "_RECOVERY_PROBE_TIMEOUT", 0.3)


async def test_close_stops_a_background_member_that_obeys_term(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    s = LocalSession()
    with _reaping([a, b]):
        try:
            await s.run_cmd(cmd=f"sh -c '{_stamp(a)}; exec sleep 30' &")
            await s.run_cmd(cmd=f"nohup sh -c '{_stamp(b)}; exec sleep 30' >/dev/null 2>&1 &")
            await _await_witness(a)
            await _await_witness(b)
        finally:
            await s.close()
        await _gone(a)
        await _gone(b)


async def test_a_member_that_ignores_term_is_killed_after_the_grace_when_the_leader_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The shell exits on TERM at once; the member that ignores it is what is
    # left of the group once the grace is over, and gets KILL.
    monkeypatch.setattr(process_stop, "_EXEC_REAP_TIMEOUT", 0.5)
    w = tmp_path / "w"
    s = LocalSession()
    with _reaping([w]):
        try:
            await s.run_cmd(cmd=f"""sh -c 'trap "" TERM; {_stamp(w)}; exec sleep 30' &""")
            pid = await _await_witness(w)
            shell = s._pid
            fields = proc_stat(pid)
            assert fields is not None
            assert fields[_PGRP] == str(shell), "the member is in the shell's group"
        finally:
            await s.close()
        await _gone(w)


async def test_a_member_that_left_the_group_survives(tmp_path: Path) -> None:
    w = tmp_path / "w"
    s = LocalSession()
    with _reaping([w]):
        try:
            await s.run_cmd(cmd=f"setsid sh -c '{_stamp(w)}; exec sleep 30' &")
            await _await_witness(w)
        finally:
            await s.close()
        assert observe(w) == RUNNING


async def test_close_stops_the_running_command_of_a_busy_session(
    tmp_path: Path, short_recovery: None
) -> None:
    w = tmp_path / "w"
    s = LocalSession()
    with _reaping([w]):
        try:
            result = await s.run_cmd(
                cmd=f"sh -c '{_stamp(w)}; exec sleep 30' & while :; do sleep 1; done", timeout=0.3
            )
            assert result.status == Status.Error
            assert not s.alive
            await _await_witness(w)
        finally:
            await s.close()
        await _gone(w)


async def test_a_dead_session_is_stopped_by_the_next_default_session_operation(
    tmp_path: Path, short_recovery: None
) -> None:
    w = tmp_path / "w"
    mgr = session_manager(name="local", session_factory=LocalSession)
    with _reaping([w]):
        try:
            result = await mgr.run_cmd(
                cmd=f"sh -c '{_stamp(w)}; exec sleep 30' & while :; do sleep 1; done", timeout=0.3
            )
            assert result.status == Status.Error
            await _await_witness(w)
            assert observe(w) == RUNNING  # the given-up session's group is not yet stopped
            ok = await mgr.run_cmd(cmd="echo ok")
            assert ok.value == "ok"
            await _gone(w)
        finally:
            await mgr.close_all()


async def test_a_cancel_landing_during_close_is_held_and_the_pipes_are_released(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process_stop, "_EXEC_REAP_TIMEOUT", 0.2)
    termed = asyncio.Event()
    send = process_stop.LocalStoppable.send

    def send_and_note(self: process_stop.LocalStoppable, sig: Literal["TERM", "KILL"]) -> None:
        send(self, sig)
        termed.set()

    monkeypatch.setattr(process_stop.LocalStoppable, "send", send_and_note)
    s = LocalSession()
    await s.run_cmd(cmd="trap '' TERM")
    proc = s._process
    assert proc is not None
    task = asyncio.create_task(s.close())
    try:
        await asyncio.wait_for(termed.wait(), 2.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), 2.0)
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    assert proc.returncode == -signal.SIGKILL
    assert s._process is None
    assert s._transport is None


async def test_a_cancelled_run_is_stopped_before_the_cancel_returns(tmp_path: Path) -> None:
    w = tmp_path / "w"
    s = LocalSession()
    with _reaping([w]):
        try:
            await s.run_cmd(cmd="echo init")
            task = asyncio.create_task(s.run_cmd(cmd=f"sh -c '{_stamp(w)}; exec sleep 30'"))
            await _await_witness(w)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(task), 10.0)
            # the shell's child was interrupted and the shell confirmed, the session kept
            assert observe(w) == STOPPED
            assert s.alive
        finally:
            await s.close()


async def test_close_twice_is_quiet() -> None:
    s = LocalSession()
    await s.run_cmd(cmd="echo init")
    await s.close()
    await s.close()
    assert not s.alive


async def test_a_shell_that_already_exited_closes_quietly(monkeypatch: pytest.MonkeyPatch) -> None:
    s = LocalSession()
    await s.run_cmd(cmd="echo init")
    proc = s._process
    assert proc is not None
    await s.send("exit\n")
    await asyncio.wait_for(proc.wait(), 5.0)
    signalled: "list[int]" = []

    def killpg(pgid: int, sig: int) -> None:
        signalled.append(sig)

    monkeypatch.setattr(process_stop.os, "killpg", killpg)
    await s.close()
    assert signalled == []
    assert s._process is None


def _group_gone(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    return False


async def test_a_cancel_during_the_local_session_s_spawn_leaves_no_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The spawn is held after the shell started and before asyncio has
    # connected its pipes (its stdin pipe's connect waits on a gate); the
    # opener is cancelled there. The shell leads its own group, so the group
    # id is its pid.
    from asyncio import unix_events

    loop = asyncio.get_running_loop()
    spawned: list[int] = []
    connecting, gate = asyncio.Event(), asyncio.Event()
    start = unix_events._UnixSubprocessTransport._start
    connect_write_pipe = loop.connect_write_pipe

    def recording_start(self, *args, **kwargs) -> None:
        start(self, *args, **kwargs)
        spawned.append(self._proc.pid)

    async def gated_connect_write_pipe(*args, **kwargs):
        connecting.set()
        await gate.wait()
        return await connect_write_pipe(*args, **kwargs)

    monkeypatch.setattr(unix_events._UnixSubprocessTransport, "_start", recording_start)
    monkeypatch.setattr(loop, "connect_write_pipe", gated_connect_write_pipe)
    s = LocalSession()
    opener = asyncio.ensure_future(s.open_transport())
    try:
        await connecting.wait()
        assert len(spawned) == 1
        opener.cancel()
        gate.set()
        with pytest.raises(asyncio.CancelledError):
            await opener
        await wait_for_async(
            lambda: _group_gone(spawned[0]),
            2.0,
            interval=0.01,
            on_timeout="the cancelled spawn's process group is still alive",
        )
    finally:
        gate.set()
        for pgid in spawned:
            signal_group(pgid)
