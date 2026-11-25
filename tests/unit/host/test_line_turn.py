"""The line turn: one call at a time on a host whose commands share one line.

A console and an embedded host run ``run`` and ``exec`` on one session, so
concurrent calls take turns (``BaseHost._line_turn``); the turn is held
across a ``run(user=...)``'s switch. A ``session_setup`` hook that calls
``host.run()``/``host.exec()`` on its own host while its session opens is
refused by name, before it could wait on anything.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from otto.host import session as session_mod
from otto.host.element import Element
from otto.host.embedded_host import ZephyrHost
from otto.host.errors import SessionSetupError
from otto.host.host import BaseHost
from otto.host.session_setup import SESSION_SETUPS, SessionSetup, register_session_setup
from otto.host.unix_host import UnixHost
from otto.logger.mode import LogMode
from tests.unit.host._sessions import session_manager
from tests.unit.host.conftest import InFlight, recording_as_user
from tests.unit.host.test_session_manager_setup import _recording_transport, _term_conn

_HOOK = "line-turn-reentry"


def embedded_host() -> ZephyrHost:
    return ZephyrHost(ip="192.0.2.1", element=Element(name="d"), log=LogMode.QUIET)


def spy_acquisitions(
    host: BaseHost, monkeypatch: pytest.MonkeyPatch, *, also: "list[str] | None" = None
) -> list[str]:
    """Record, in order, each entry into the line turn, the console lock and each named method."""
    order: list[str] = []
    turn = host._line_turn

    @asynccontextmanager
    async def line_turn() -> AsyncIterator[None]:
        order.append("line_turn")
        async with turn():
            yield

    monkeypatch.setattr(host, "_line_turn", line_turn)
    for name in also or []:
        real = getattr(host, name)

        def spied(*a: Any, _real: Callable[..., Any] = real, _name: str = name, **kw: Any) -> Any:
            order.append(_name)
            return _real(*a, **kw)

        monkeypatch.setattr(host, name, spied)
    conns = getattr(host, "_connections", None)
    if conns is not None and hasattr(conns, "_console_lock"):

        class _StopError(RuntimeError):
            pass

        class _SpyLock:
            async def __aenter__(self) -> None:
                order.append("console_lock")
                raise _StopError("the spy stops before any dial")

            async def __aexit__(self, *exc: object) -> None:
                return None

        conns._console_lock = _SpyLock()
    return order


@pytest.fixture
def register_hook() -> "AsyncIterator[Callable[[Callable[..., Awaitable[object]]], SessionSetup]]":
    def register(fn: "Callable[..., Awaitable[object]]") -> SessionSetup:
        register_session_setup(_HOOK, fn, overwrite=True)
        return SessionSetup(name=_HOOK)

    yield register
    SESSION_SETUPS.unregister(name=_HOOK)


def _install_mgr(host: UnixHost, setup: SessionSetup, monkeypatch: pytest.MonkeyPatch) -> None:
    """Give *host* a real SessionManager that opens a no-I/O session and runs *setup*'s hook."""
    recorded = _recording_transport([])
    monkeypatch.setattr(session_mod, "SshSession", recorded)
    host._session_mgr = session_manager(
        connections=_term_conn("ssh"),
        name=host.name,
        host_id=host.id,
        session_setup=setup,
    )


async def _touch(host: BaseHost) -> None:
    probe = InFlight()
    mgr = host._live_session_mgr()
    mgr.run_cmd = probe.run_cmd  # ty: ignore[invalid-assignment]
    mgr.exec_on_default_session = probe.exec_on_default_session  # ty: ignore[invalid-assignment]
    await host.exec(":")


@pytest.mark.asyncio
async def test_gathered_calls_on_an_embedded_host_never_interleave(monkeypatch):
    host = embedded_host()
    probe = InFlight()
    monkeypatch.setattr(host._live_session_mgr(), "run_cmd", probe.run_cmd)
    await asyncio.gather(
        *(host.exec(f"c{i}") for i in range(4)),
        host.run("r"),
        host.exists("/lfs/a"),
        host.ls("/lfs"),
        host.rm("/lfs/b"),
    )
    assert probe.max_in_flight == 1
    assert len(probe.seq) == 8


@pytest.mark.asyncio
async def test_gathered_calls_on_ssh_still_overlap(unix_host_ssh, monkeypatch):
    """The control: a host whose exec has channels of its own takes no turn."""
    probe = InFlight()
    monkeypatch.setattr(unix_host_ssh._live_session_mgr(), "exec_pooled", probe.exec_pooled)
    monkeypatch.setattr(unix_host_ssh._live_session_mgr(), "run_cmd", probe.run_cmd)
    await asyncio.gather(*(unix_host_ssh.exec(f"c{i}") for i in range(4)), unix_host_ssh.run("r"))
    assert probe.max_in_flight == 5
    assert unix_host_ssh._line_lock is None


@pytest.mark.asyncio
async def test_run_as_user_on_a_console_holds_the_turn_across_the_switch(
    console_unix_host, monkeypatch
):
    seq: list[str] = []
    probe = InFlight(seq)
    monkeypatch.setattr(console_unix_host, "as_user", recording_as_user(seq))
    monkeypatch.setattr(console_unix_host._live_session_mgr(), "run_cmd", probe.run_cmd)
    monkeypatch.setattr(
        console_unix_host._live_session_mgr(),
        "exec_on_default_session",
        probe.exec_on_default_session,
    )
    await asyncio.gather(console_unix_host.run("a", user="alice"), console_unix_host.exec("b"))
    assert seq in (
        ["switch:alice", "cmd:a", "switch:back", "cmd:b"],
        ["cmd:b", "switch:alice", "cmd:a", "switch:back"],
    )


@pytest.mark.asyncio
async def test_a_hook_that_calls_host_exec_while_its_session_opens_is_refused_by_name(
    unix_host_ssh, monkeypatch, register_hook
):
    host = unix_host_ssh

    async def hook(handle: object, ctx: object) -> None:
        await host.exec("x")

    _install_mgr(host, register_hook(hook), monkeypatch)
    with pytest.raises(SessionSetupError, match="while the session it sets up is opening"):
        await asyncio.wait_for(host.run("x"), 5)  # a bound on the failing path only


@pytest.mark.asyncio
async def test_the_refusal_names_only_the_opening_manager_and_only_during_the_hook(
    unix_host_ssh, monkeypatch, register_hook
):
    """The control: the hook sees its own manager marked, and nothing is marked after it."""
    seen: list[bool] = []

    async def hook(handle: object, ctx: object) -> None:
        seen.append(unix_host_ssh.id in session_mod._ESTABLISHING.live_keys())

    _install_mgr(unix_host_ssh, register_hook(hook), monkeypatch)
    # The no-I/O transport stops at the handshake after the hook; that is
    # where this control ends.
    with pytest.raises(AssertionError, match="must not reach a handshake"):
        await unix_host_ssh._live_session_mgr()._ensure_session()
    assert seen == [True]
    assert session_mod._ESTABLISHING.live_keys() == []


@pytest.mark.asyncio
async def test_a_console_hook_re_entry_is_refused_before_it_waits_on_the_turn(
    console_unix_host, monkeypatch, register_hook
):
    host = console_unix_host

    async def hook(handle: object, ctx: object) -> None:
        await host.exec("x")

    _install_mgr(host, register_hook(hook), monkeypatch)
    order = spy_acquisitions(host, monkeypatch)
    with pytest.raises(SessionSetupError, match="while the session it sets up is opening"):
        await asyncio.wait_for(host.run("x"), 5)
    assert order.count("line_turn") == 1


@pytest.mark.asyncio
async def test_a_child_task_of_the_hook_inherits_the_refusal(
    console_unix_host, monkeypatch, register_hook
):
    host = console_unix_host

    async def hook(handle: object, ctx: object) -> None:
        await asyncio.ensure_future(host.exec("x"))

    _install_mgr(host, register_hook(hook), monkeypatch)
    with pytest.raises(SessionSetupError, match="while the session it sets up is opening"):
        await asyncio.wait_for(host.run("x"), 5)


def test_the_turn_is_taken_after_the_loop_claim_and_the_refusal(console_unix_host, monkeypatch):
    async def go() -> list[str]:
        probe = InFlight()
        mgr = console_unix_host._live_session_mgr()
        monkeypatch.setattr(mgr, "exec_on_default_session", probe.exec_on_default_session)
        order = spy_acquisitions(
            console_unix_host, monkeypatch, also=["_claim_loop", "_refuse_establishing"]
        )
        await console_unix_host.exec(":")
        return order

    order = asyncio.run(go())
    assert order[:3] == ["_claim_loop", "_refuse_establishing", "line_turn"]


@pytest.mark.asyncio
async def test_a_caller_cancelled_while_waiting_for_the_turn_never_takes_it(
    console_unix_host, monkeypatch
):
    seq: list[str] = []
    probe = InFlight(seq, hold=True)
    monkeypatch.setattr(
        console_unix_host._live_session_mgr(),
        "exec_on_default_session",
        probe.exec_on_default_session,
    )
    first = asyncio.ensure_future(console_unix_host.exec("a"))
    await probe.started.wait()
    second = asyncio.ensure_future(console_unix_host.exec("b"))
    await asyncio.sleep(0)
    second.cancel()
    probe.release()
    await first
    with pytest.raises(asyncio.CancelledError):
        await second
    assert seq == ["cmd:a"]
    assert not console_unix_host._line_lock._lock.locked()


@pytest.mark.asyncio
async def test_a_call_that_raises_while_holding_the_turn_releases_it(
    console_unix_host, monkeypatch
):
    calls: list[str] = []

    async def boom(*, cmd: str, **kw: Any) -> Any:
        calls.append(cmd)
        if cmd == "boom":
            raise ConnectionError("line dropped mid-command")
        return await InFlight().exec_on_default_session(cmd=cmd, **kw)

    monkeypatch.setattr(console_unix_host._live_session_mgr(), "exec_on_default_session", boom)
    with pytest.raises(ConnectionError):
        await console_unix_host.exec("boom")
    await asyncio.wait_for(console_unix_host.exec("next"), 5)
    assert calls == ["boom", "next"]


def test_the_turn_is_new_on_a_new_loop(console_unix_host):
    asyncio.run(_touch(console_unix_host))
    first = console_unix_host._line_lock
    assert first is not None
    asyncio.run(_touch(console_unix_host))
    assert console_unix_host._line_lock is not first


def test_one_line_decision(console_unix_host, unix_host_ssh):
    assert console_unix_host._shares_one_line()
    assert not unix_host_ssh._shares_one_line()
    assert embedded_host()._shares_one_line()


@pytest.mark.asyncio
async def test_the_turn_is_taken_before_the_connection_lock(console_unix_host, monkeypatch):
    order = spy_acquisitions(console_unix_host, monkeypatch)
    with pytest.raises(RuntimeError, match="the spy stops"):
        await console_unix_host.exec(":")
    assert order[:2] == ["line_turn", "console_lock"]


@pytest.mark.asyncio
async def test_an_embedded_transfer_command_takes_the_turn(monkeypatch):
    """A transfer's and the loader's commands reach the session past run/exec; they take turns."""
    host = embedded_host()
    probe = InFlight()
    monkeypatch.setattr(host._live_session_mgr(), "run_cmd", probe.run_cmd)
    await asyncio.gather(
        host._run_one_in_turn("transfer", "t1", timeout=5.0),
        host._run_one_in_turn("transfer", "t2", timeout=5.0),
        host.exec("e"),
    )
    assert probe.max_in_flight == 1


@pytest.mark.asyncio
async def test_a_sudo_probe_and_the_exec_it_serves_are_one_turn(console_unix_host, monkeypatch):
    """The probe is an exec on the session too: it re-enters the turn its exec holds."""
    from otto.host.host import _prepared

    host = console_unix_host
    seq: list[str] = []
    probe = InFlight(seq)
    monkeypatch.setattr(
        host._live_session_mgr(), "exec_on_default_session", probe.exec_on_default_session
    )

    async def prepare(user: "str | None" = None) -> None:
        await host._exec_one("probe", timeout=5.0, needs_shell=True)

    monkeypatch.setattr(host, "_prepare_exec_elevation", prepare)
    monkeypatch.setattr(host, "_apply_sudo", lambda sc, user=None, exec_input=None: _prepared(sc))
    await asyncio.wait_for(asyncio.gather(host.exec("a", sudo=True), host.exec("b")), 5)
    assert seq in (["cmd:probe", "cmd:a", "cmd:b"], ["cmd:b", "cmd:probe", "cmd:a"])


@pytest.mark.asyncio
async def test_a_first_run_sudo_and_a_concurrent_exec_sudo_on_a_console_both_complete(
    console_unix_host, monkeypatch
):
    """The userland's probes are execs on the line, asked from inside the caller's turn.

    On Python 3.10 and 3.11 each probe runs in the task ``asyncio.wait_for``
    creates; it re-enters the turn. The turn is taken before the userland's
    lock on both paths, so the two first elevated calls cannot deadlock.
    """
    seq: list[str] = []
    probe = InFlight(seq)
    mgr = console_unix_host._live_session_mgr()
    monkeypatch.setattr(mgr, "exec_on_default_session", probe.exec_on_default_session)
    monkeypatch.setattr(mgr, "run_cmd", probe.run_cmd)
    await asyncio.wait_for(  # a bound on the failing path only
        asyncio.gather(
            console_unix_host.run("otto-run-cmd", sudo=True),
            console_unix_host.exec("otto-exec-cmd", sudo=True),
        ),
        30,
    )
    assert probe.max_in_flight == 1
    assert len(seq) == len(set(seq)), f"a question reached the line twice: {seq}"
    ran = [entry for entry in seq if "otto-run-cmd" in entry or "otto-exec-cmd" in entry]
    assert len(ran) == 2, seq
    assert len(seq) > 2, "the userland asked nothing"


@pytest.mark.asyncio
async def test_a_task_created_inside_the_turn_types_without_waiting(console_unix_host, monkeypatch):
    seq: list[str] = []
    probe = InFlight(seq)
    monkeypatch.setattr(
        console_unix_host._live_session_mgr(),
        "exec_on_default_session",
        probe.exec_on_default_session,
    )
    async with console_unix_host._line_turn():
        await asyncio.wait_for(asyncio.ensure_future(console_unix_host.exec("child")), 5)
    assert seq == ["cmd:child"]


@pytest.mark.asyncio
async def test_a_task_that_outlives_its_holders_turn_waits_behind_the_next_holder(
    console_unix_host, monkeypatch
):
    seq: list[str] = []
    probe = InFlight(seq, hold=True)
    monkeypatch.setattr(
        console_unix_host._live_session_mgr(),
        "exec_on_default_session",
        probe.exec_on_default_session,
    )
    gate = asyncio.Event()

    async def stray() -> None:
        await gate.wait()
        await console_unix_host.exec("stray")

    async with console_unix_host._line_turn():
        child = asyncio.ensure_future(stray())  # spawned inside the turn, not awaited
    nxt = asyncio.ensure_future(console_unix_host.exec("next"))
    await probe.started.wait()  # the next holder is on the line
    gate.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert seq == ["cmd:next"], "the stray child typed beside the next holder"
    assert probe.max_in_flight == 1
    probe.release()
    await nxt
    await asyncio.wait_for(child, 5)
    assert seq == ["cmd:next", "cmd:stray"]
    assert probe.max_in_flight == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", ["session-manager", "login-bridge"])
async def test_both_callers_of_the_hook_refuse_its_host_level_re_entry(
    caller, unix_host_ssh, monkeypatch, register_hook
):
    """The mark is set where the hook runs, so the manager and the login bridge refuse alike."""
    host = unix_host_ssh

    async def hook(handle: object, ctx: object) -> None:
        await host.exec("x")

    setup = register_hook(hook)
    if caller == "session-manager":
        _install_mgr(host, setup, monkeypatch)
        opening = host.run("x")
    else:
        from otto.host import interact
        from otto.host.command_frame import BashFrame
        from otto.host.login_proxy import Cred
        from otto.host.session import BridgeSetup
        from tests._fixtures.dialect_shell import DialectShell
        from tests.unit.host.test_interact_session_setup import _Console

        console = _Console(DialectShell())
        opening = interact._run_session_setup_on_bridge(
            write_remote=console.write_remote,
            read_remote=console.read_remote,
            newline=b"\n",
            host_name=host.name,
            host_id=host.id,
            bridge=BridgeSetup(
                setup=setup,
                landing_frame=None,
                target_frame=BashFrame(),
                creds=[Cred(login="admin", password="pw")],
                init_timeout=None,
            ),
            proxy_hops=[],
            via_login="admin",
            log_line=lambda _l: None,
        )
    with pytest.raises(SessionSetupError, match="while the session it sets up is opening"):
        await asyncio.wait_for(opening, 5)  # a bound on the failing path only
    assert session_mod._ESTABLISHING.live_keys() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["raises", "cancelled"])
async def test_the_refusal_mark_is_cleared_when_the_hook_ends_badly(
    ending, unix_host_ssh, monkeypatch, register_hook
):
    """After a hook that raised or was cancelled, a later host call is not refused."""
    host = unix_host_ssh

    async def hook(handle: object, ctx: object) -> None:
        if ending == "raises":
            raise RuntimeError("the hook failed")
        raise asyncio.CancelledError

    _install_mgr(host, register_hook(hook), monkeypatch)
    expected = SessionSetupError if ending == "raises" else asyncio.CancelledError
    with pytest.raises(expected):
        await host.run("x")
    assert session_mod._ESTABLISHING.live_keys() == []
    probe = InFlight()
    monkeypatch.setattr(host._live_session_mgr(), "run_cmd", probe.run_cmd)
    await host.run("later")  # same task, same context: not refused
    assert probe.seq == ["cmd:later"]


@pytest.mark.asyncio
async def test_a_hook_on_one_host_may_call_another_host(
    unix_host_ssh, console_unix_host, monkeypatch, register_hook
):
    other = console_unix_host
    probe = InFlight()
    monkeypatch.setattr(
        other._live_session_mgr(), "exec_on_default_session", probe.exec_on_default_session
    )

    async def hook(handle: object, ctx: object) -> None:
        await other.exec("on-the-other-host")

    _install_mgr(unix_host_ssh, register_hook(hook), monkeypatch)
    # The no-I/O transport stops at the handshake after the hook.
    with pytest.raises(AssertionError, match="must not reach a handshake"):
        await unix_host_ssh._live_session_mgr()._ensure_session()
    assert probe.seq == ["cmd:on-the-other-host"]


@pytest.mark.asyncio
async def test_a_hook_cycle_through_another_host_is_refused_instead_of_deadlocking(
    unix_host_ssh, monkeypatch, register_hook
):
    """Host A's hook calls host B, whose hook calls back into A: A is still opening, so refused."""
    a = unix_host_ssh
    b = UnixHost(
        ip="10.0.0.3",
        element=Element(name="other"),
        creds=a.creds,
        log=LogMode.QUIET,
    )

    async def hook(handle: object, ctx: Any) -> None:
        await (b if ctx.host_id == a.id else a).run("x")

    setup = register_hook(hook)
    _install_mgr(a, setup, monkeypatch)
    _install_mgr(b, setup, monkeypatch)
    with pytest.raises(SessionSetupError, match=rf"^{a.name}: session_setup called host\.run\(\)"):
        await asyncio.wait_for(a.run("x"), 5)  # a bound on the failing path only
    assert session_mod._ESTABLISHING.live_keys() == []


@pytest.mark.asyncio
async def test_a_task_the_hook_started_is_not_refused_once_the_hook_has_ended(
    unix_host_ssh, monkeypatch, register_hook
):
    host = unix_host_ssh
    setup_done = asyncio.Event()
    children: list[asyncio.Task[object]] = []

    async def later() -> object:
        await setup_done.wait()
        return await host.exec("later")

    async def hook(handle: object, ctx: object) -> None:
        children.append(asyncio.ensure_future(later()))

    _install_mgr(host, register_hook(hook), monkeypatch)
    with pytest.raises(AssertionError, match="must not reach a handshake"):
        await host._live_session_mgr()._ensure_session()
    probe = InFlight()
    monkeypatch.setattr(host._live_session_mgr(), "exec_pooled", probe.exec_pooled)
    setup_done.set()
    await asyncio.wait_for(children[0], 5)  # a bound on the failing path only
    assert probe.seq == ["cmd:later"]


@pytest.mark.asyncio
@pytest.mark.parametrize("verb", ["exists", "ls", "rm"])
async def test_an_embedded_hook_re_entry_through_a_file_op_is_refused_by_name(
    verb, monkeypatch, register_hook
):
    host = embedded_host()

    async def hook(handle: object, ctx: object) -> None:
        await getattr(host, verb)("/lfs/a")

    recorded = _recording_transport([])
    monkeypatch.setattr(session_mod, "SshSession", recorded)
    host._session_mgr = session_manager(
        connections=_term_conn("ssh"),
        name=host.name,
        host_id=host.id,
        session_setup=register_hook(hook),
    )
    with pytest.raises(SessionSetupError, match=rf"called host\.{verb}\(\) while the session"):
        await asyncio.wait_for(host.run("x"), 5)  # a bound on the failing path only


@pytest.mark.asyncio
@pytest.mark.parametrize("verb", ["send", "expect"])
async def test_a_hook_raw_io_on_its_own_opening_host_is_refused_by_name(
    verb, unix_host_ssh, monkeypatch, register_hook
):
    host = unix_host_ssh

    async def hook(handle: object, ctx: object) -> None:
        await getattr(host, verb)("x")

    _install_mgr(host, register_hook(hook), monkeypatch)
    with pytest.raises(SessionSetupError, match=rf"called host\.{verb}\(\) while the session"):
        await asyncio.wait_for(host.run("x"), 5)  # a bound on the failing path only


@pytest.mark.asyncio
async def test_a_turn_left_from_another_context_is_released():
    """A holder finalised in a task of its own (an abandoned generator) still frees the line."""
    from otto.host.host import _LineTurn

    turn = _LineTurn()
    held = turn.held()
    await asyncio.ensure_future(held.__aenter__())
    await asyncio.ensure_future(held.__aexit__(None, None, None))
    assert not turn._lock.locked()


@pytest.mark.asyncio
@pytest.mark.parametrize("verb", ["put", "get", "as_user", "switch_user", "run-sudo"])
async def test_the_refusal_names_the_verb_the_hook_called(
    verb, unix_host_ssh, monkeypatch, register_hook, tmp_path
):
    host = unix_host_ssh

    async def call() -> None:
        if verb == "put":
            await host.put(tmp_path, Path("/tmp"))
        elif verb == "get":
            await host.get(Path("/tmp/x"), tmp_path)
        elif verb == "as_user":
            async with host.as_user(user="root"):
                pass
        elif verb == "switch_user":
            await host.switch_user(user="root")
        else:
            await host.run("x", sudo=True)

    async def hook(handle: object, ctx: object) -> None:
        await call()

    named = "run" if verb == "run-sudo" else verb
    _install_mgr(host, register_hook(hook), monkeypatch)
    with pytest.raises(SessionSetupError, match=rf"called host\.{named}\(\) while the session"):
        await asyncio.wait_for(host.run("x"), 5)  # a bound on the failing path only
