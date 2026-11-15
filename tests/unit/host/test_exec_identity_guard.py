"""The identity guard of a pooled session reached by a switch, in real shells on a pty.

The open proves who the shell it will type into is and, on that same probe
line, gives it a shell-local mark that is never exported
(``CommandFrame.identity_mark_statement`` through
``login_proxy.prove_identity``); each ``exec`` is then typed with the frame's
guard (``CommandFrame.identity_guard``). While the switched shell answers, the
command runs; once it has gone and the shell underneath answers, the command
is not run and the session records the identity lost.

Each shell here is a :data:`~tests.unit.host._pty_session.PTY_SHELLS` member,
spawned on its own pty (a real ``su`` session is on a terminal too); the
"switched shell" is a second, non-interactive shell of the same kind started
inside it, as ``su`` starts one.
"""

import getpass
import shlex
from pathlib import Path

import pytest

from otto.host.command_frame import IDENTITY_MARK_PREFIX, BashFrame, ExecInput
from otto.host.login_proxy import LoginProxyError, prove_identity
from otto.host.session import NEVER_RAN, _SessionProxyIO
from tests._fixtures.interactive_shell import SHELLS
from tests.unit.host._pty_session import PTY_SHELLS, PtySession, pty_session

GUARD_SHELLS = [name for name in PTY_SHELLS if name in {"bash", "dash", "busybox-sh", "mksh"}]
"""The shells the guard is checked on: bash, dash, BusyBox ash and mksh, as the pty matrix has
them (installed, as every pty-matrix test takes them; all four are on the dev VM, and CI's
runner lacks mksh)."""

_ME = getpass.getuser()


def _inner(name: str) -> str:
    """The command that starts a non-interactive shell of *name*'s kind: the switched shell."""
    return shlex.join(dict(SHELLS)[name])


async def _switch_and_mark(
    session: PtySession, name: str, *, before: str = "", user: str = _ME
) -> str:
    """Start the switched shell, run *before* in it, then prove and mark it; return the mark."""
    await session.send(f"{_inner(name)}\n")
    if before:
        done = await session.run_cmd(cmd=before, timeout=5)
        assert done.retcode == 0, done
    mark = "0123abcd4567ef89"
    await prove_identity(
        _SessionProxyIO(session, lambda *_: None, lambda *_: None),
        host_id="pty",
        user=user,
        marking=BashFrame().identity_mark_statement(mark=mark),
    )
    return mark


@pytest.mark.asyncio
@pytest.mark.parametrize("name", GUARD_SHELLS)
async def test_a_guarded_exec_runs_only_in_the_shell_that_carries_the_mark(
    name: str, tmp_path: Path
):
    async with pty_session(name) as session:
        guarded = ExecInput(stdin_eof=True, identity=await _switch_and_mark(session, name))

        ran = await session.run_cmd(cmd=f"echo ran > {tmp_path}/first", exec_input=guarded)
        assert ran.retcode == 0, ran
        assert (tmp_path / "first").read_text() == "ran\n"
        assert not session.identity_lost

        # The switched shell ends; the shell underneath answers.
        await session.send("exit\n")
        skipped = await session.run_cmd(
            cmd=f"echo ran > {tmp_path}/second", exec_input=guarded, timeout=10
        )
        assert skipped.value == NEVER_RAN
        assert not (tmp_path / "second").exists()
        assert session.identity_lost
        assert session.alive, "the shell underneath still answers: only the mark tells them apart"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", GUARD_SHELLS)
@pytest.mark.parametrize(
    "before",
    ["set -a", "export __otto_as_0123abcd4567ef89=stale"],
    ids=["allexport", "name-already-exported"],
)
async def test_the_mark_is_never_exported_and_the_shell_s_options_are_kept(name: str, before: str):
    async with pty_session(name) as session:
        mark = await _switch_and_mark(session, name, before=before)
        probe = f"alias {IDENTITY_MARK_PREFIX}{mark} >/dev/null 2>&1 && echo has || echo none"
        own = await session.run_cmd(cmd=probe, timeout=5)
        assert own.value == "has"
        child = await session.run_cmd(cmd=f"{_inner(name)} -c {shlex.quote(probe)}", timeout=5)
        assert child.value == "none", "a program the shell starts never has the mark"
        options = await session.run_cmd(cmd="echo opts=$-", timeout=5)
        assert ("a" in options.value.removeprefix("opts=")) == (before == "set -a")


@pytest.mark.asyncio
@pytest.mark.parametrize("name", GUARD_SHELLS)
async def test_only_the_alias_answers_the_guard(name: str, tmp_path: Path):
    """Under the switched shell, a function and an executable on PATH named like the mark
    do not pass the guard; inside the guarded exec, `unalias -a` does not remove the mark."""
    mark = "0123abcd4567ef89"
    named = f"{IDENTITY_MARK_PREFIX}{mark}"
    planted = tmp_path / "bin"
    planted.mkdir()
    (planted / named).write_text("#!/bin/sh\nexit 0\n")
    (planted / named).chmod(0o755)
    async with pty_session(name) as session:
        underneath = f"PATH={planted}:$PATH; {named}() {{ :; }}"
        assert (await session.run_cmd(cmd=underneath, timeout=5)).retcode == 0
        guarded = ExecInput(stdin_eof=True, identity=await _switch_and_mark(session, name))
        cleared = await session.run_cmd(cmd="unalias -a", exec_input=guarded)
        assert cleared.retcode == 0, cleared
        again = await session.run_cmd(cmd=f"echo ran > {tmp_path}/kept", exec_input=guarded)
        assert again.retcode == 0, "the exec's own unalias stays in its subshell"
        await session.send("exit\n")
        skipped = await session.run_cmd(
            cmd=f"echo ran > {tmp_path}/below", exec_input=guarded, timeout=10
        )
        assert skipped.value == NEVER_RAN
        assert not (tmp_path / "below").exists()


@pytest.mark.asyncio
async def test_a_function_exported_to_a_child_bash_does_not_pass(tmp_path: Path):
    mark = "0123abcd4567ef89"
    named = f"{IDENTITY_MARK_PREFIX}{mark}"
    async with pty_session("bash") as session:
        probe = f"alias {named} >/dev/null 2>&1 && echo has || echo none"
        cmd = f"{named}() {{ :; }}; export -f {named}; bash -c {shlex.quote(probe)}"
        child = await session.run_cmd(cmd=cmd, timeout=5)
        assert child.value == "none"


@pytest.mark.asyncio
async def test_a_shell_that_cannot_say_who_it_is_is_no_proof():
    """`id` missing prints nothing: the proof reports the identity unproven."""
    async with pty_session("bash") as session:
        await session.send(f"{_inner('bash')}\n")
        assert (await session.run_cmd(cmd="PATH=/nonexistent", timeout=5)).retcode == 0
        proven = await prove_identity(
            _SessionProxyIO(session, lambda *_: None, lambda *_: None),
            host_id="pty",
            user=_ME,
            marking=BashFrame().identity_mark_statement(mark="0123abcd4567ef89"),
        )
        assert proven is False


@pytest.mark.asyncio
async def test_a_failed_proof_after_setup_names_the_hook():
    async with pty_session("bash") as session:
        with pytest.raises(LoginProxyError, match="session_setup hook is the likely cause"):
            await _switch_and_mark(session, "bash", user="someone-else")


def test_a_mark_that_is_not_a_plain_word_is_refused():
    with pytest.raises(ValueError, match="letters and digits"):
        BashFrame().identity_mark_statement(mark="a;b")


def test_a_frame_without_a_guard_declares_none():
    from otto.host.command_frame import RawFrame, ZephyrFrame

    assert BashFrame().guards_identity
    assert not ZephyrFrame().guards_identity
    assert not RawFrame().guards_identity


_SUBSHELL = "test-identity-subshell"
"""A login proxy that 'switches' by starting a shell inside the login's: a shell on top of one."""

_HOOK = "test-identity-replace-shell"


async def _start_subshell(io, ctx) -> None:
    await io.send(f"{_inner('bash')}\n")


async def _replace_the_shell(session, ctx) -> None:
    # The hook replaces the switched shell with a new one: a mark set before
    # the hook would be gone.
    await session.send(f"exec {_inner('bash')}\n")


@pytest.fixture
def pool():
    """A manager whose sessions are bash on a pty, reached by one login-proxy hop."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from otto.host.login_proxy import LOGIN_PROXIES, Cred, register_login_proxy
    from otto.host.session import SessionManager
    from otto.host.session_setup import SESSION_SETUPS, SessionSetup, register_session_setup

    register_login_proxy(_SUBSHELL, _start_subshell, overwrite=True)
    register_session_setup(_HOOK, _replace_the_shell, overwrite=True)
    managers: list[SessionManager] = []

    def build(*, hooked: bool) -> SessionManager:
        hop = Cred(login=_ME, proxy=_SUBSHELL, via="login")
        conn = SimpleNamespace(
            credentials=("login", None),
            login_target=_ME,
            proxy_hops=[hop],
            term="ssh",
            ssh=AsyncMock(),
        )
        mgr = SessionManager(
            connections=conn,
            session_factory=lambda **_kw: PtySession("bash"),
            host_id="pty",
            creds=[hop],
            session_setup=SessionSetup(name=_HOOK) if hooked else None,
        )
        managers.append(mgr)
        return mgr

    yield build
    LOGIN_PROXIES.unregister(name=_SUBSHELL)
    SESSION_SETUPS.unregister(name=_HOOK)


async def _exec(mgr, cmd: str):
    from otto.host.exec_route import ExecRoute
    from otto.logger.mode import LogMode

    return await mgr.exec_on_route(
        route=ExecRoute.POOLED_SHELL,
        cmd=cmd,
        timeout=10.0,
        log=LogMode.NORMAL,
        exec_input=ExecInput(stdin_eof=True),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("hooked", [False, True], ids=["hop", "hop-then-hook-replaces-shell"])
async def test_a_session_reached_by_a_login_proxy_hop_is_marked_and_kept(pool, hooked: bool):
    mgr = pool(hooked=hooked)
    try:
        first = await _exec(mgr, "echo one")
        second = await _exec(mgr, "echo two")
        assert (first.value, second.value) == ("one", "two")
        (kept,) = mgr._exec_pools[_ME]
        assert kept._session.reached_by_switch
        assert kept._session.identity_mark is not None
        assert not kept._session.identity_lost
        assert mgr._exec_pool_count == 1, "one session, reused"
    finally:
        await mgr.close_all()


def test_the_mark_rides_the_identity_proof_s_own_line():
    """One physical line assigns the mark and proves the user: the shell that answers holds it."""
    from otto.host.command_frame import SessionMarkers
    from otto.host.login_proxy import _identity_probe

    marking = BashFrame().identity_mark_statement(mark="0123abcd")
    line = _identity_probe(SessionMarkers.for_session(session_id="probe"), "", marking)
    assert line.count("\n") == 1
    assert line.endswith("\n")
    assert marking in line
    assert line.index(marking) < line.index("$(id -un)")
