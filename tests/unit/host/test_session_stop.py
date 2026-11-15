"""The shell-route stop of a command otto gives up on: one bound, owned by ``compensate``.

A timed-out, password-refused or cancelled command is interrupted and the
shell confirmed under one bound, before the call returns; a stop that cannot
confirm leaves the session dead, and a cancellation landing during it is held
until it ends.
"""

import asyncio

import pytest
from pydantic import SecretStr

import otto.host.session as session_mod
from otto.host.command_frame import ZephyrFrame
from otto.host.errors import PasswordRefusedError
from otto.host.exec_route import ExecRoute
from otto.host.host import CredentialExpect
from otto.host.session import ShellSession
from otto.utils import wait_for_async
from tests._fixtures.session_stop import old_mark_only
from tests.unit.host._session_feed import scripted_shell, scripted_shell_unopened
from tests.unit.host.test_session import idle_exec_sessions
from tests.unit.host.test_session_exec_pooled import _exec, _mgr

pytestmark = [pytest.mark.asyncio]


def _stop_tasks() -> list[asyncio.Task]:
    return [t for t in asyncio.all_tasks() if "_stop_interrupted_command" in repr(t.get_coro())]


def _answer(pattern: str, secret: str) -> CredentialExpect:
    # host.py: user and host are required
    return CredentialExpect(
        pattern, SecretStr(secret), user="admin", host="box", refused_on_repeat=True
    )


async def test_a_started_command_that_times_out_is_stopped_under_the_bound():
    s = await scripted_shell()
    r = await s.run_cmd(cmd="c", timeout=0.2)
    assert s.began.is_set()
    assert r.timed_out
    assert s.ops.index("write:'\\x03'") > s.ops.index(next(o for o in s.ops if "BEGIN" in o))
    assert s.ops.count("write:'\\x03'") == 1
    assert not s._needs_recovery
    assert s.alive
    assert _stop_tasks() == []


async def test_a_zero_budget_still_runs_the_one_stop():
    s = await scripted_shell()
    r = await s.run_cmd(cmd="c", timeout=0)
    assert r.timed_out
    assert s.ops.count("write:'\\x03'") == 1
    assert s.alive
    assert _stop_tasks() == []


async def test_the_value_is_frozen_before_the_stop_and_recovery_adds_nothing():
    s = await scripted_shell(after_interrupt="^C\r\n$ ")
    r = await s.run_cmd(cmd="c", timeout=0.2)
    assert r.value == "Command timed out after 0.2s"


@pytest.mark.timeout(10)
async def test_a_blocked_interrupt_write_is_bounded_and_leaves_the_session_dead(monkeypatch):
    for name in ("_RECOVERY_TIMEOUT", "_RECOVERY_SETTLE", "_RECOVERY_PROBE_TIMEOUT"):
        monkeypatch.setattr(session_mod, name, 0.01)
    s = await scripted_shell(write_blocks_from="\x03")
    r = await s.run_cmd(cmd="c", timeout=0.2)
    assert r.timed_out
    assert s._needs_recovery
    assert not s.alive
    assert _stop_tasks() == []


async def test_an_unconfirmed_recovery_leaves_the_session_dead():
    s = await scripted_shell(confirms=False)
    r = await s.run_cmd(cmd="c", timeout=0.2)
    assert r.timed_out
    assert not s.alive


async def test_a_cancel_landing_during_the_timeout_stop_is_held_until_the_stop_resolves():
    s = await scripted_shell(hold_probe=True)
    task = asyncio.ensure_future(s.run_cmd(cmd="c", timeout=0.2))
    await s.probe_sent.wait()
    task.cancel()
    s.probe_reply.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not s._needs_recovery
    assert s.alive
    assert _stop_tasks() == []


async def test_a_refused_password_is_stopped_the_same_way():
    s = await scripted_shell(prompt_twice="pw:")
    with pytest.raises(PasswordRefusedError):
        await s.run_cmd(cmd="c", timeout=5, expects=[_answer("pw:", "x")])
    assert s.ops.count("write:'\\x03'") == 1
    assert not s._needs_recovery
    assert s.alive


async def test_an_ordinary_command_completes_without_a_stop():
    # the fixture's sunny path
    s = await scripted_shell(replies={"true": ("", 0)})
    r = await s.run_cmd(cmd="true", timeout=5)
    assert (r.timed_out, r.retcode) == (False, 0)
    assert "write:'\\x03'" not in s.ops


async def test_a_timed_out_pooled_exec_whose_stop_fails_is_closed_not_pooled():
    mgr = _mgr()
    mgr._session_factory = lambda **kw: scripted_shell_unopened(confirms=False)
    r = await _exec(mgr, ExecRoute.POOLED_SHELL, cmd="c", timeout=0.2)
    assert r.timed_out
    assert idle_exec_sessions(mgr) == []


async def test_a_timed_out_zephyr_command_is_stopped_under_the_same_bound():
    s = await scripted_shell(frame=ZephyrFrame())
    r = await s.run_cmd(cmd="c", timeout=0.2)
    assert r.timed_out
    assert s.ops.count("write:'\\x03'") == 1
    assert not s._needs_recovery
    assert s.alive


async def _cancel_once_begun(s, task):
    await s.began.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.timeout(10)
async def test_a_cancel_stops_the_command_before_the_cancel_returns():
    s = await scripted_shell()
    await _cancel_once_begun(s, asyncio.ensure_future(s.run_cmd(cmd="c", timeout=60)))
    assert s.ops.count("write:'\\x03'") == 1
    assert not s._needs_recovery
    assert s.alive
    assert _stop_tasks() == []


@pytest.mark.timeout(10)
async def test_a_second_cancel_is_held_until_the_stop_resolves():
    s = await scripted_shell(hold_probe=True)
    task = asyncio.ensure_future(s.run_cmd(cmd="c", timeout=60))
    await s.began.wait()
    task.cancel()
    await s.probe_sent.wait()
    task.cancel()
    s.probe_reply.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not s._needs_recovery
    assert s.alive
    assert _stop_tasks() == []


@pytest.mark.timeout(10)
async def test_a_blocked_interrupt_on_cancel_is_bounded_and_leaves_the_session_marked(
    monkeypatch,
):
    for name in ("_RECOVERY_TIMEOUT", "_RECOVERY_SETTLE", "_RECOVERY_PROBE_TIMEOUT"):
        monkeypatch.setattr(session_mod, name, 0.01)
    # the command is written; the interrupt blocks
    s = await scripted_shell(write_blocks_from="\x03")
    await _cancel_once_begun(s, asyncio.ensure_future(s.run_cmd(cmd="c", timeout=60)))
    assert s._needs_recovery
    assert not s.alive
    assert _stop_tasks() == []
    with pytest.raises(RuntimeError, match="session is not alive"):
        await s.run_cmd(cmd="true", timeout=5)


@pytest.mark.timeout(10)
@pytest.mark.parametrize("op", ["run_cmd", "send", "expect"])
async def test_a_session_a_cancel_left_dead_receives_nothing_further(op, monkeypatch):
    for name in ("_RECOVERY_TIMEOUT", "_RECOVERY_SETTLE", "_RECOVERY_PROBE_TIMEOUT"):
        monkeypatch.setattr(session_mod, name, 0.01)
    s = await scripted_shell(write_blocks_from="\x03")
    await _cancel_once_begun(s, asyncio.ensure_future(s.run_cmd(cmd="c", timeout=60)))
    assert not s.alive
    before = list(s.ops)
    calls = {
        "run_cmd": lambda: s.run_cmd(cmd="true", timeout=5),
        "send": lambda: s.send("x\n"),
        "expect": lambda: s.expect("x", timeout=5),
    }
    with pytest.raises(RuntimeError, match="session is not alive"):
        await calls[op]()
    assert s.ops == before


@pytest.mark.timeout(10)
async def test_a_pooled_exec_cancelled_with_a_blocked_interrupt_is_disposed(monkeypatch):
    for name in ("_RECOVERY_TIMEOUT", "_RECOVERY_SETTLE", "_RECOVERY_PROBE_TIMEOUT"):
        monkeypatch.setattr(session_mod, name, 0.01)
    mgr = _mgr()
    made = []

    def factory(**kw):
        made.append(scripted_shell_unopened(write_blocks_from="\x03"))
        return made[-1]

    mgr._session_factory = factory
    task = asyncio.ensure_future(_exec(mgr, ExecRoute.POOLED_SHELL, cmd="c", timeout=60))
    await wait_for_async(
        lambda: bool(made) and made[-1].began.is_set(),
        10.0,
        interval=0.01,
        on_timeout="the pooled command never began",
    )
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not made[-1].alive
    assert idle_exec_sessions(mgr) == []


@pytest.mark.timeout(10)
async def test_nothing_touches_the_transport_after_the_call_returns():
    s = await scripted_shell()
    await _cancel_once_begun(s, asyncio.ensure_future(s.run_cmd(cmd="c", timeout=60)))
    s.ops.append("<returned>")
    for _ in range(10):
        await asyncio.sleep(0)
    assert s.ops[-1] == "<returned>"


@pytest.mark.timeout(10)
async def test_control_with_the_old_mark_only_branch_the_command_is_not_stopped(monkeypatch):
    monkeypatch.setattr(ShellSession, "_stop_interrupted_command", old_mark_only)
    s = await scripted_shell()
    await _cancel_once_begun(s, asyncio.ensure_future(s.run_cmd(cmd="c", timeout=60)))
    assert "write:'\\x03'" not in s.ops
