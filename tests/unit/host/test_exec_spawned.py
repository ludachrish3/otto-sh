"""``on_typed`` and ``BaseHost._exec_spawned``: an exec's task, once its command is typed.

Driven over the real local shells behind ``piped``'s asyncssh surface, so the
session's open, its handshake and the command's own effects are real.
"""

import asyncio
from pathlib import Path
from typing import Any

import pytest

from otto.host.session import ShellSession
from otto.logger.mode import LogMode
from otto.result import CommandResult
from otto.utils import Status
from tests.conftest import active_context


def _gated(gate: Path) -> str:
    """A command that waits for *gate*/go, then leaves *gate*/done behind."""
    return f"while [ ! -e {gate}/go ]; do sleep 0.02; done; touch {gate}/done; echo finished"


@pytest.mark.parametrize("terminal", [None, True], ids=["no-terminal", "terminal"])
@pytest.mark.asyncio
async def test_on_typed_fires_once_after_the_open_and_before_the_command_ends(
    piped, tmp_path, terminal
):
    host, conn = piped
    gate = tmp_path / "gate"
    gate.mkdir()
    seen: list[tuple[int, bool]] = []

    def typed() -> None:
        seen.append((len(conn.processes), (gate / "done").exists()))
        (gate / "go").touch()

    kw: dict[str, Any] = {} if terminal is None else {"terminal": terminal}
    async with host._host_entry("exec"):
        result = await host._exec_in_turn(
            _gated(gate),
            expects=None,
            timeout=30.0,
            log=LogMode.QUIET,
            sudo=False,
            user=None,
            on_typed=typed,
            **kw,
        )
    assert (result.status, result.value) == (Status.Success, "finished")
    # One call, one callback: its session was open, its command not yet done.
    assert seen == [(1, False)]


@pytest.mark.asyncio
async def test_a_run_carries_no_on_typed(piped, monkeypatch):
    host, _conn = piped
    inputs: list[Any] = []
    real = ShellSession._run_cmd_inner

    async def recording(self: ShellSession, *a: Any, **kw: Any) -> CommandResult:
        inputs.append(kw.get("exec_input"))
        return await real(self, *a, **kw)

    monkeypatch.setattr(ShellSession, "_run_cmd_inner", recording)
    assert (await host.run("true")).only.status is Status.Success
    assert inputs
    assert all(x is None or x.on_typed is None for x in inputs)


@pytest.mark.asyncio
async def test_the_spawned_task_is_returned_running_once_its_command_is_typed(piped, tmp_path):
    host, conn = piped
    gate = tmp_path / "gate"
    gate.mkdir()
    task = await host._exec_spawned(_gated(gate), timeout=30.0)
    try:
        # Typed into an open session, and still running: nothing let it end.
        assert len(conn.processes) == 1
        assert not task.done()
        assert not (gate / "done").exists()
    finally:
        (gate / "go").touch()
    result = await task
    assert (result.status, result.value) == (Status.Success, "finished")
    assert (gate / "done").exists()


@pytest.mark.asyncio
async def test_a_call_that_types_nothing_returns_its_ended_task(tmp_path):
    from tests.unit.host.test_terminal_switch import _unix

    with active_context(dry_run=True):
        host = _unix()
        task = await host._exec_spawned("true", timeout=5.0)
        assert task.done()
        assert task.result().status is Status.NotRun


@pytest.mark.asyncio
async def test_a_spawn_cancelled_while_its_session_opens_leaves_no_exec_running(piped):
    host, conn = piped
    opening = asyncio.Event()
    release = asyncio.Event()
    real = conn.create_process

    async def blocked(*args: Any, **kwargs: Any) -> Any:
        opening.set()
        await release.wait()
        return await real(*args, **kwargs)

    conn.create_process = blocked
    before = asyncio.all_tasks()
    spawn = asyncio.ensure_future(host._exec_spawned("echo never", timeout=30.0))
    await opening.wait()
    spawn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await spawn
    # The exec the spawn started has ended with it, before the cancel was raised.
    left = [t for t in asyncio.all_tasks() - before if t is not spawn and not t.done()]
    assert left == []
    assert conn.processes == []
    assert not [s for s in host._session_mgr._named_sessions.values() if s.alive]
