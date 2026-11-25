"""`SessionManager.exec_pooled`: one free-list per user, each session opened for its
user, and the keep-or-discard rule that protects them; and
`SessionManager.exec_on_default_session` on a console."""

import asyncio
import logging
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from pydantic import SecretStr

from otto.host import process_stop
from otto.host.command_frame import BashFrame, ExecInput
from otto.host.host import DEFAULT_COMMAND_TIMEOUT
from otto.host.local_host import LocalHost
from otto.host.login_proxy import Cred, LoginProxyError
from otto.host.session import _EXEC_POOL_PREFIX, NEVER_RAN, SessionManager
from otto.logger.mode import LogMode
from otto.result import CommandResult
from otto.utils import Status, wait_for_async
from tests.unit.host._sessions import session_manager
from tests.unit.host.test_session import _proxy_connections, _StubExecSession, idle_exec_sessions
from tests.unit.host.test_terminal_switch import _EitherConnection, _unix

_PW = SecretStr("pw")
"""The password every fake ``credentials`` pair here carries."""


def _mgr(
    *,
    hops=(),
    term="ssh",
    creds=None,
    login_target="admin",
    credentials=("admin", _PW),
):
    # login_target/credentials match `_StubExecSession`'s hardcoded
    # `ShellModel(user="admin", ...)` seed: with no proxy hops, nothing
    # replays a real `su` into the model at session-open time, so the
    # model's own identity tracking (used by as_user's resync) only agrees
    # with `current_user` (stamped straight from login_target) when the two
    # start equal. Callers that don't care about that resync (e.g. the
    # ambient-identity tests below) may pass their own login_target/credentials.
    conn = _proxy_connections(list(hops), login_target=login_target, credentials=credentials)
    conn.term = term
    conn.ssh = AsyncMock()
    return session_manager(
        connections=conn,
        session_factory=_StubExecSession,
        host_id="h",
        creds=creds,
    )


async def _exec(
    mgr: SessionManager,
    *,
    cmd: str,
    timeout: float = DEFAULT_COMMAND_TIMEOUT,
    log: LogMode = LogMode.NORMAL,
    **kw,
) -> CommandResult:
    """Run *cmd* on a pooled session, with ``exec``'s own defaults for the rest."""
    return await mgr.exec_pooled(cmd=cmd, timeout=timeout, log=log, **kw)


async def _exec_default(
    mgr: SessionManager,
    *,
    cmd: str,
    timeout: float = DEFAULT_COMMAND_TIMEOUT,
    log: LogMode = LogMode.NORMAL,
    **kw,
) -> CommandResult:
    """Run *cmd* on the default session, with ``exec``'s own defaults for the rest."""
    return await mgr.exec_on_default_session(cmd=cmd, timeout=timeout, log=log, **kw)


def test_the_plain_pooled_session_is_unbudgeted():
    # A budget query must not be perturbed by what one call asks for: a
    # call that needs nothing is planned without a terminal.
    assert _mgr().line_budget(shared=False, plain=True) is None


@pytest.mark.asyncio
async def test_an_exec_on_a_host_of_an_unknown_term_is_refused_by_name():
    """The one refusal is where a pooled session's transport is chosen: nothing is opened."""
    mgr = _mgr(term="serial9")
    mgr._session_factory = None
    mgr._named_session_factory = None
    with pytest.raises(ValueError, match=r'unsupported terminal type "serial9"$'):
        await _exec(mgr, cmd="id")
    assert idle_exec_sessions(mgr) == []
    assert all(not pool for pool in mgr._exec_pools.values())
    assert not mgr._named_sessions
    mgr._connections.ssh.assert_not_awaited()


def test_a_host_of_an_unknown_term_is_budgeted_as_a_terminal():
    """The line budget is answered before the refusal: an unknown term types into a terminal."""
    unknown, telnet = _mgr(term="serial9"), _mgr(term="telnet")
    for shared in (False, True):
        assert unknown.line_budget(shared=shared) is not None
        assert unknown.line_budget(shared=shared) == telnet.line_budget(shared=shared)
        assert unknown.line_room(cmd="id", shared=shared) == telnet.line_room(
            cmd="id", shared=shared
        )
    assert unknown.line_budget(shared=False, plain=True) is None


@pytest.fixture
def _fast_resync_settle(monkeypatch):
    from otto.host import login_proxy

    monkeypatch.setattr(login_proxy, "_RESYNC_SETTLE", 0.0)


class TestPooledExec:
    @pytest.mark.asyncio
    async def test_plain_pooled_exec_returns_the_session(self):
        mgr = _mgr(term="telnet")
        result = await _exec(mgr, cmd="id")
        assert result.value == "OUT"
        assert len(idle_exec_sessions(mgr)) == 1
        assert idle_exec_sessions(mgr)[0]._session.run_cmd_calls == ["id"]

    @pytest.mark.asyncio
    async def test_expects_ride_the_pooled_run(self):
        """The prompt answers have to REACH the shell — the command alone
        arriving proves only the route, and a hop that dropped `expects`
        would leave the pooled `sudo -S` waiting on a password nobody typed.
        """
        seen: list[object] = []

        class _RecordingExpects(_StubExecSession):
            async def run_cmd(self, cmd, expects=None, **kw):
                seen.append(expects)
                return await super().run_cmd(cmd=cmd, expects=expects, **kw)

        mgr = _mgr(term="telnet")
        mgr._session_factory = _RecordingExpects
        await _exec(mgr, cmd="sudo -S id", expects=[("assword", "pw\n")])
        assert idle_exec_sessions(mgr)[0]._session.run_cmd_calls == ["sudo -S id"]
        assert seen == [[("assword", "pw\n")]]

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_fast_resync_settle")
    async def test_user_runs_on_a_session_opened_as_them_and_kept_as_them(self):
        mgr = _mgr(term="telnet", creds=[Cred(login="root", password="rootpw")])
        seen: list[tuple[str, str]] = []

        class _Switching(_StubExecSession):
            async def run_cmd(self, cmd, **kw):
                seen.append((cmd, self.current_user))
                return await super().run_cmd(cmd=cmd, **kw)

        mgr._session_factory = _Switching
        result = await _exec(mgr, cmd="id", user="root")
        assert result.value == "OUT"
        assert seen[-1] == ("id", "root")
        assert [s.current_user for s in mgr._exec_pools[("root", False)]] == ["root"]
        assert ("admin", False) not in mgr._exec_pools, "no session was opened as the login"

    @pytest.mark.asyncio
    async def test_a_session_that_ran_as_someone_else_is_closed_not_pooled(self, caplog):
        """A command that changed who the shell is (an ``su`` it ran) costs the session."""
        mgr = _mgr(term="telnet")

        class _Becomes(_StubExecSession):
            async def run_cmd(self, cmd, **kw):
                self.current_user = "root"
                return await super().run_cmd(cmd=cmd, **kw)

        mgr._session_factory = _Becomes
        with caplog.at_level(logging.WARNING, logger="otto.host.session"):
            result = await _exec(mgr, cmd="su -")
        assert result.value == "OUT"
        assert idle_exec_sessions(mgr) == []
        assert "opened for 'admin'" in caplog.text, caplog.text

    @pytest.mark.asyncio
    async def test_the_default_user_and_the_named_login_share_one_bucket(self):
        mgr = _mgr()
        await _exec(mgr, cmd="a")
        await _exec(mgr, cmd="b", user="admin")
        assert list(mgr._exec_pools) == [("admin", False)]
        assert len(mgr._exec_pools[("admin", False)]) == 1

    @pytest.mark.asyncio
    async def test_a_session_for_x_is_x_before_setup_runs(self, open_steps):
        mgr = _mgr(creds=[Cred(login="alice", password="pw")])
        await _exec(mgr, cmd="c", user="alice")
        # No hops, so no "proxy" step.
        assert open_steps == ["handshake", "switch:alice", "setup:exec_pool(user=alice)"]
        assert mgr._exec_pools[("alice", False)][0].current_user == "alice"

    @pytest.mark.asyncio
    async def test_a_proxied_login_s_session_for_x_replays_the_hops_then_switches(self, open_steps):
        hop = Cred(login="mysql", proxy="su", via="admin")
        mgr = _mgr(hops=[hop], login_target="mysql", creds=[hop, Cred(login="alice")])
        await _exec(mgr, cmd="c", user="alice")
        assert open_steps == ["handshake", "proxy", "switch:alice", "setup:exec_pool(user=alice)"]
        assert mgr._exec_pools[("alice", False)][0].current_user == "alice"

    @pytest.mark.asyncio
    async def test_a_direct_cred_x_on_ssh_opens_on_xs_own_connection_and_is_stamped_x(
        self, open_steps
    ):
        mgr = _mgr(creds=[Cred(login="alice", password="pw")])
        mgr._connections.has_direct_cred = lambda *, user: user == "alice"
        await _exec(mgr, cmd="c", user="alice")
        assert open_steps == ["ssh_as:alice", "handshake", "setup:exec_pool(user=alice)"]
        assert mgr._exec_pools[("alice", False)][0].current_user == "alice"

    @pytest.mark.asyncio
    async def test_a_direct_cred_x_on_a_proxied_ssh_login_skips_the_hops(self, open_steps):
        hop = Cred(login="mysql", proxy="su", via="admin")
        mgr = _mgr(hops=[hop], login_target="mysql", creds=[hop, Cred(login="alice")])
        mgr._connections.has_direct_cred = lambda *, user: user == "alice"
        await _exec(mgr, cmd="c", user="alice")
        assert open_steps == ["ssh_as:alice", "handshake", "setup:exec_pool(user=alice)"]
        assert mgr._exec_pools[("alice", False)][0].current_user == "alice"

    @pytest.mark.asyncio
    async def test_a_direct_cred_x_on_telnet_switches_instead(self, open_steps):
        mgr = _mgr(term="telnet", creds=[Cred(login="alice", password="pw")])
        mgr._connections.has_direct_cred = lambda *, user: True
        await _exec(mgr, cmd="c", user="alice")
        assert "ssh_as:alice" not in open_steps
        assert "switch:alice" in open_steps
        assert mgr._exec_pools[("alice", False)][0].current_user == "alice"

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_fast_resync_settle")
    async def test_alternating_users_keep_their_own_sessions(self):
        mgr = _mgr(creds=[Cred(login="alice", password="pw"), Cred(login="bob", password="pw")])
        for user in ["alice", "bob", "alice", "bob"]:
            await _exec(mgr, cmd="id -un", user=user)
        assert {k: len(v) for k, v in mgr._exec_pools.items()} == {
            ("alice", False): 1,
            ("bob", False): 1,
        }
        assert mgr._exec_pool_count == 2, "each user's session was opened once and reused"

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_fast_resync_settle")
    async def test_concurrent_same_user_calls_each_get_a_session_and_both_are_kept(self):
        # Telnet: an ssh pool keeps one idle session (the one-idle tests below).
        mgr = _mgr(term="telnet", creds=[Cred(login="alice", password="pw")])
        await asyncio.gather(*(_exec(mgr, cmd="c", user="alice") for _ in range(2)))
        # Retention is the user's peak of concurrent calls.
        assert len(mgr._exec_pools[("alice", False)]) == 2

    @pytest.mark.asyncio
    async def test_a_failed_switch_fails_at_open_and_leaves_nothing(self, open_steps):
        mgr = _mgr()
        open_steps.switch_raises = LoginProxyError("no password for bob")
        with pytest.raises(LoginProxyError):
            await _exec(mgr, cmd="c", user="bob")
        assert mgr._exec_pools.get(("bob", False), []) == []
        assert not any(n.startswith("__exec_pool_") for n in mgr._named_sessions)
        assert "setup:exec_pool(user=bob)" not in open_steps, "setup never ran as the wrong user"

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_fast_resync_settle")
    async def test_close_all_clears_every_bucket_and_reopens_cleanly(self):
        mgr = _mgr(creds=[Cred(login="alice", password="pw")])
        await _exec(mgr, cmd="c", user="alice")
        await mgr.close_all()
        assert mgr._exec_pools == {}
        await _exec(mgr, cmd="c", user="alice")
        assert len(mgr._exec_pools[("alice", False)]) == 1

    @pytest.mark.asyncio
    async def test_a_dead_session_is_closed_not_pooled(self):
        mgr = _mgr(term="telnet")

        class _Dies(_StubExecSession):
            async def run_cmd(self, cmd, **kw):
                self._alive = False  # whatever `alive` reads — see ShellSession.alive
                return await super().run_cmd(cmd=cmd, **kw)

        mgr._session_factory = _Dies
        await _exec(mgr, cmd="id")
        assert idle_exec_sessions(mgr) == []

    @pytest.mark.asyncio
    async def test_a_session_whose_terminal_record_was_lost_is_closed_not_pooled(self):
        mgr = _mgr(term="telnet")

        class _LosesRecord(_StubExecSession):
            async def run_cmd(self, cmd, **kw):
                self._exec_baseline_lost = True  # whatever `exec_baseline_lost` reads
                return await super().run_cmd(cmd=cmd, **kw)

        mgr._session_factory = _LosesRecord
        result = await _exec(mgr, cmd="id")
        assert result.value == "OUT"
        assert idle_exec_sessions(mgr) == []


class TestAmbientUser:
    @pytest.mark.asyncio
    async def test_no_default_session_means_no_ambient_user(self):
        mgr = _mgr(term="telnet")
        assert mgr.ambient_user is None

    @pytest.mark.asyncio
    async def test_the_user_the_session_opened_as_is_not_ambient(self):
        mgr = _mgr(
            term="telnet",
            login_target="alice",
            credentials=("alice", _PW),
        )
        await mgr.run_cmd(cmd="id")  # opens the default session as the login user
        assert mgr.current_user == "alice"
        assert mgr.ambient_user is None

    @pytest.mark.asyncio
    async def test_a_proxied_login_target_is_the_baseline_not_an_ambient_user(self):
        hop = Cred(login="mysql", proxy="su", via="alice")
        mgr = _mgr(
            term="telnet",
            hops=[hop],
            login_target="mysql",
            credentials=("alice", _PW),
        )
        await mgr.run_cmd(cmd="id")
        assert mgr.current_user == "mysql", "proxy hops replayed at open"
        assert mgr.ambient_user is None, "the login target is what the session opened as"

    @pytest.mark.asyncio
    async def test_a_switched_default_session_is_ambient_until_switched_back(self):
        mgr = _mgr(
            term="telnet",
            login_target="alice",
            credentials=("alice", _PW),
        )
        await mgr.run_cmd(cmd="id")
        mgr._set_current_user("root")  # what PosixPrivilege.as_user records after a switch
        assert mgr.ambient_user == "root"
        mgr._set_current_user("alice")
        assert mgr.ambient_user is None

    @pytest.mark.asyncio
    async def test_close_all_forgets_the_baseline(self):
        mgr = _mgr(
            term="telnet",
            login_target="alice",
            credentials=("alice", _PW),
        )
        await mgr.run_cmd(cmd="id")
        await mgr.close_all()
        assert mgr.ambient_user is None


class TestAnSshExecRunsOnAPooledSession:
    """Every ssh exec runs on a pooled session, and a call that needs a shell
    gets one that gives it what it needs: the user it switches to, the
    prompts it answers. The host-side half (that a user or a prompt IS a
    reason to need a shell) is pinned on the host's session mode, in
    ``test_exec_session_choice.py`` and ``test_unix_host.py``.
    """

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_fast_resync_settle")
    async def test_an_ssh_exec_as_a_user_runs_on_a_switched_pooled_shell(self):
        mgr = _mgr(term="ssh", creds=[Cred(login="root", password="rootpw")])
        seen: list[tuple[str, str]] = []

        class _Switching(_StubExecSession):
            async def run_cmd(self, cmd, **kw):
                seen.append((cmd, self.current_user))
                return await super().run_cmd(cmd=cmd, **kw)

        mgr._session_factory = _Switching
        result = await _exec(mgr, cmd="id", user="root")

        assert result.value == "OUT"
        # As root, not as the login user.
        assert seen[-1] == ("id", "root")
        assert len(idle_exec_sessions(mgr)) == 1

    @pytest.mark.asyncio
    async def test_expects_reach_the_pooled_ssh_session(self):
        """The answers have to reach the pooled shell the command runs on."""
        mgr = _mgr(term="ssh")
        seen: list[object] = []

        class _RecordingExpects(_StubExecSession):
            async def run_cmd(self, cmd, expects=None, **kw):
                seen.append(expects)
                return await super().run_cmd(cmd=cmd, expects=expects, **kw)

        mgr._session_factory = _RecordingExpects
        result = await _exec(mgr, cmd="sudo -S id", expects=[("assword", "pw\n")])

        assert result.value == "OUT"
        assert idle_exec_sessions(mgr)[0]._session.run_cmd_calls == ["sudo -S id"]
        assert seen == [[("assword", "pw\n")]], "the answers have to REACH the shell"

    @pytest.mark.asyncio
    async def test_a_plain_ssh_exec_runs_on_a_pooled_session(self):
        """The control: a call that needs nothing still pools. Every
        ssh exec runs on a session; what a call that needs nothing gives up is
        the terminal (the host's ``plain``, pinned in ``test_exec_session_choice.py``),
        never the session."""
        mgr = _mgr(term="ssh")
        result = await _exec(mgr, cmd="id")

        assert result.value == "OUT"
        assert idle_exec_sessions(mgr)[0]._session.run_cmd_calls == ["id"]


class TestConsoleTermExecRunsOnTheDefaultSession:
    """A console serves one client: exec runs on the default session; no pool is ever built."""

    @staticmethod
    def _console_mgr() -> "tuple[SessionManager, AsyncMock]":
        from types import SimpleNamespace

        from otto.host.options import ConsoleOptions

        console = AsyncMock()
        conn = SimpleNamespace(
            term="console",
            console_options=ConsoleOptions(server="test1", port=4001, dial="direct"),
            console=console,
            proxy_hops=[],
            login_target="test",
        )
        return session_manager(connections=conn, name="test2"), console

    @staticmethod
    def _spy_default(mgr: SessionManager, monkeypatch: pytest.MonkeyPatch) -> list:
        ran: list = []

        async def fake_run_cmd(
            cmd, expects=None, timeout=0.0, log=None, write_progress=None, exec_input=None
        ):
            ran.append((cmd, expects, timeout))
            return CommandResult(status=Status.Success, value="out", command=cmd, retcode=0)

        async def no_pool(name):
            raise AssertionError(f"a console must never open a second session ({name})")

        monkeypatch.setattr(mgr, "run_cmd", fake_run_cmd)
        monkeypatch.setattr(mgr, "open_session", no_pool)
        return ran

    @pytest.mark.asyncio
    async def test_a_plain_exec_runs_on_the_default_session(self, monkeypatch):
        mgr, _console = self._console_mgr()
        ran = self._spy_default(mgr, monkeypatch)

        result = await _exec_default(mgr, cmd="id -un", timeout=5.0)

        assert ran == [("id -un", None, 5.0)]
        assert result.value == "out"
        assert idle_exec_sessions(mgr) == []
        assert mgr._exec_pool_count == 0, "no pool session was ever asked for"

    @pytest.mark.asyncio
    async def test_an_exec_that_needs_a_shell_answers_on_the_default_session(self, monkeypatch):
        mgr, _console = self._console_mgr()
        ran = self._spy_default(mgr, monkeypatch)
        expects = [("Password:", "pw")]

        await _exec_default(mgr, cmd="sudo -S true", expects=expects)

        assert [(cmd, exp) for cmd, exp, _t in ran] == [("sudo -S true", expects)]
        assert mgr._exec_pool_count == 0

    @pytest.mark.asyncio
    async def test_exec_as_another_user_on_the_default_session_is_refused_by_name(
        self, monkeypatch
    ):
        """The host switches the one session before it calls in; a bare other user here is a bug."""
        from otto.host.errors import ConsoleError

        mgr, console = self._console_mgr()
        ran = self._spy_default(mgr, monkeypatch)

        with pytest.raises(ConsoleError, match=r"^test2: console is single-client.*'test'.*'root'"):
            await _exec_default(mgr, cmd="id -un", user="root")

        assert ran == []
        console.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_exec_as_the_session_user_is_the_session(self, monkeypatch):
        mgr, _console = self._console_mgr()
        ran = self._spy_default(mgr, monkeypatch)

        await _exec_default(mgr, cmd="id -un", user="test")

        assert [cmd for cmd, _e, _t in ran] == ["id -un"]

    def test_the_line_budget_is_the_typed_one(self):
        """exec types into the console's shell, so a transfer must size its lines to it."""
        from otto.host.command_frame import ExecInput
        from otto.host.session import typed_line_budget

        mgr, _console = self._console_mgr()
        typed = ExecInput(stdin_eof=True, restore=SessionManager._exec_restore_for(True))
        assert mgr.line_budget(shared=True) == typed_line_budget(
            mgr._command_frame, echoes=True, exec_input=typed
        )


class TestOpenSessionAsUser:
    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_fast_resync_settle")
    async def test_an_existing_session_is_returned_to_the_user_it_is(self):
        mgr = _mgr(creds=[Cred(login="alice", password="pw")])
        first = await mgr.open_session(name="aux", as_user="alice")
        assert await mgr.open_session(name="aux", as_user="alice") is first
        assert await mgr.open_session(name="aux") is first

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_fast_resync_settle")
    async def test_an_existing_session_asked_for_as_another_user_is_refused_by_name(self):
        mgr = _mgr(creds=[Cred(login="alice", password="pw")])
        first = await mgr.open_session(name="aux", as_user="alice")
        typed = list(first._session.writes)
        with pytest.raises(ValueError, match=r"'aux' is open as 'alice', not 'admin'"):
            await mgr.open_session(name="aux", as_user="admin")
        assert first._session.writes == typed, "nothing was typed"
        assert mgr._named_sessions["aux"] is first


class _SwitchedShellGone(_StubExecSession):
    """A pooled session whose switched shell may have gone, leaving the login's underneath.

    What the shell does with the guard is the frame's business (a real shell
    runs it in ``test_exec_identity_guard``); this double answers a guarded
    exec the way the session reads a skipped one, so the pool's handling of
    it is what is under test.
    """

    def __init__(self, *, gone: bool) -> None:
        super().__init__()
        self.gone = gone
        self.exec_inputs: list[object] = []

    async def run_cmd(  # ty: ignore[invalid-method-override]
        self,
        cmd: str,
        expects=None,
        timeout=None,
        on_output=None,
        redact=False,
        write_progress=None,
        exec_input=None,
    ) -> CommandResult:
        self.run_cmd_calls.append(cmd)
        self.exec_inputs.append(exec_input)
        if exec_input is not None and exec_input.identity is not None and self.gone:
            self._identity_lost = True
            return CommandResult(status=Status.Failed, value=NEVER_RAN, command=cmd, retcode=-1)
        if on_output is not None:
            on_output("OUT")  # a command that ran relays its line, as a session does
        return CommandResult(status=Status.Success, value="OUT", command=cmd, retcode=0)


def _gone_mgr(*gone: bool) -> tuple[SessionManager, list[_SwitchedShellGone]]:
    mgr = _mgr(creds=[Cred(login="alice", password="pw")])
    built: list[_SwitchedShellGone] = []
    states = iter(gone)

    def factory(**_kw: object) -> _SwitchedShellGone:
        built.append(_SwitchedShellGone(gone=next(states)))
        return built[-1]

    mgr._session_factory = factory
    return mgr, built


_EXEC = ExecInput(stdin_eof=True)
"""What ``BaseHost.exec`` hands every pooled exec: an exec, so its frame can carry the guard."""


class TestSwitchedShellIdentity:
    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_fast_resync_settle")
    async def test_a_switched_session_is_marked_at_open_and_every_exec_is_guarded(self):
        mgr, built = _gone_mgr(False)
        await _exec(mgr, cmd="a", user="alice", exec_input=_EXEC)
        await _exec(mgr, cmd="b", user="alice", exec_input=_EXEC)
        (shell,) = built
        assert shell.identity_mark is not None
        assert shell.run_cmd_calls == ["a", "b"]
        marking = BashFrame().identity_mark_statement(mark=shell.identity_mark)
        proofs = [w for w in shell.writes if marking in w]
        assert proofs
        assert all("$(id -un)" in w for w in proofs), "the mark rides the identity proof's line"
        assert [getattr(i, "identity", None) for i in shell.exec_inputs] == [
            shell.identity_mark
        ] * 2

    @pytest.mark.asyncio
    async def test_a_session_reached_by_a_login_proxy_hop_alone_is_guarded(self):
        """`exec()` on a proxied login: no switch to a user, but the hop put a shell on top."""
        hop = Cred(login="mysql", proxy="su", via="admin")
        mgr = _mgr(hops=[hop], login_target="mysql", creds=[hop])
        built: list[_SwitchedShellGone] = []

        def factory(**_kw: object) -> _SwitchedShellGone:
            built.append(_SwitchedShellGone(gone=False))
            return built[-1]

        mgr._session_factory = factory
        await _exec(mgr, cmd="a", exec_input=_EXEC)
        (shell,) = built
        assert shell.reached_by_switch
        assert shell.identity_mark is not None
        assert getattr(shell.exec_inputs[-1], "identity", None) == shell.identity_mark

    @pytest.mark.asyncio
    async def test_a_session_never_switched_carries_no_mark_and_no_guard(self):
        mgr, built = _gone_mgr(False)
        await _exec(mgr, cmd="a", exec_input=_EXEC)
        (shell,) = built
        assert shell.identity_mark is None
        assert shell.run_cmd_calls == ["a"]
        assert getattr(shell.exec_inputs[0], "identity", None) is None

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_fast_resync_settle")
    async def test_a_session_whose_switched_shell_has_gone_is_discarded_and_the_call_reopens_once(
        self,
    ):
        mgr, built = _gone_mgr(True, False)
        result = await _exec(mgr, cmd="c", user="alice", exec_input=_EXEC)
        assert result.value == "OUT"
        gone, fresh = built
        assert gone.identity_lost
        assert not gone.alive
        assert [s._session for s in mgr._exec_pools[("alice", False)]] == [fresh]
        assert fresh.run_cmd_calls[-1] == "c"

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_fast_resync_settle")
    async def test_a_fresh_session_that_loses_its_user_too_raises_and_runs_nothing(self):
        mgr, built = _gone_mgr(True, True)
        with pytest.raises(LoginProxyError, match="lost that user before its command ran"):
            await _exec(mgr, cmd="c", user="alice", exec_input=_EXEC)
        assert len(built) == 2
        assert all(not s.alive for s in built)
        assert mgr._exec_pools.get(("alice", False), []) == []


@pytest_asyncio.fixture
async def shells(tmp_path):
    """An ssh host's session manager whose connection opens real local shells."""
    host = _unix()
    conn = _EitherConnection(tmp_path)
    host._connections.ssh = AsyncMock(return_value=conn)  # ty: ignore[invalid-assignment]
    yield host._session_mgr
    await host.close()
    await conn.reap()


async def _exec_mode(mgr: SessionManager, cmd: str, *, plain: bool) -> CommandResult:
    """*cmd* on a pooled session in mode *plain*, with a rendered exec frame."""
    return await mgr.exec_pooled(
        cmd=cmd,
        timeout=DEFAULT_COMMAND_TIMEOUT,
        log=LogMode.QUIET,
        exec_input=ExecInput(stdin_eof=True),
        plain=plain,
    )


class TestPoolMode:
    """The free-lists are keyed by user AND terminal mode; ssh keeps one idle session."""

    @pytest.mark.asyncio
    async def test_the_two_modes_never_share_a_session(self, shells):
        await _exec_mode(shells, "true", plain=True)
        await _exec_mode(shells, "true", plain=False)
        (plain,) = shells._exec_pools[("user", True)]
        (term,) = shells._exec_pools[("user", False)]
        assert plain is not term
        assert not plain.reads_a_terminal
        assert term.reads_a_terminal

    @pytest.mark.asyncio
    async def test_serial_plain_execs_reuse_one_session(self, shells):
        first = await _exec_mode(shells, "echo $OTTO_SESSION", plain=True)
        second = await _exec_mode(shells, "echo $OTTO_SESSION", plain=True)
        assert first.value == second.value
        assert shells._exec_pool_count == 1

    @pytest.mark.asyncio
    async def test_a_concurrent_burst_leaves_one_idle_session_on_ssh(self, shells):
        await asyncio.gather(*(_exec_mode(shells, "sleep 0.2", plain=True) for _ in range(3)))
        assert shells._exec_pool_count == 3, "each concurrent call had its own session"
        (kept,) = shells._exec_pools[("user", True)]
        assert kept.alive
        live = [s for s in shells._named_sessions.values() if s.alive]
        assert live == [kept], "the released extras were closed"

    @pytest.mark.asyncio
    async def test_the_lost_identity_retry_opens_a_session_of_the_same_mode(
        self, shells, monkeypatch
    ):
        opened: list[bool] = []
        real_open = shells._open_exec_session
        real_run = shells._exec_on_pooled

        async def _open(key, *, plain=False):
            opened.append(plain)
            return await real_open(key, plain=plain)

        async def _run(session, cmd, **kw):
            if len(opened) == 1:
                session._session._identity_lost = True
            return await real_run(session, cmd, **kw)

        monkeypatch.setattr(shells, "_open_exec_session", _open)
        monkeypatch.setattr(shells, "_exec_on_pooled", _run)
        result = await _exec_mode(shells, "echo hi", plain=True)
        assert result.value == "hi"
        assert opened == [True, True]
        (kept,) = shells._exec_pools[("user", True)]
        assert not kept.reads_a_terminal


class TestIdleSessionsKept:
    """Which managers keep no idle exec session: only the local host's, whose shells read a pipe."""

    def test_the_local_host_keeps_none(self):
        from otto.host.local_host import LocalHost

        assert LocalHost()._session_mgr._idle_exec_sessions_kept() == 0

    def test_a_container_without_a_transport_is_not_taken_for_the_local_host(self):
        from tests.unit.host.test_docker_host import _make_container

        mgr = _make_container()._session_mgr
        assert mgr._connections is None, "premise: a container's manager has no transport"
        assert mgr._idle_exec_sessions_kept() is None

    def test_telnet_keeps_every_idle_session_and_ssh_one(self):
        assert _mgr(term="telnet")._idle_exec_sessions_kept() is None
        assert _mgr(term="ssh")._idle_exec_sessions_kept() == 1


def _pool_names(names) -> list[str]:
    return [n for n in names if n.startswith(_EXEC_POOL_PREFIX)]


class TestPooledSessionsLeaveNothingBehind:
    """A pooled session's registry entry and per-name lock go with it, however it ends.

    A pool name is never opened again, so a lock left behind is a leak of
    one lock per call on the local host (one session per ``exec``).
    """

    @pytest.mark.asyncio
    async def test_local_execs_leave_no_pool_name_behind(self, tmp_path):
        host = LocalHost()
        mgr = host._session_mgr
        typed = tmp_path / "typed"
        try:
            assert (await host.exec("true")).status is Status.Success
            assert (await host.exec("exec sleep 30", timeout=0.2)).timed_out
            running = asyncio.ensure_future(host.exec(f"touch {typed}; exec sleep 30"))
            await wait_for_async(typed.exists, 10.0, interval=0.01, on_timeout="never typed")
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running
            assert _pool_names(mgr._named_sessions) == []
            assert _pool_names(mgr._named_session_locks) == []
        finally:
            await host.close()

    @pytest.mark.asyncio
    async def test_a_failed_open_leaves_no_pool_lock(self, open_steps):
        mgr = _mgr()
        open_steps.switch_raises = LoginProxyError("no password for bob")
        with pytest.raises(LoginProxyError):
            await _exec(mgr, cmd="c", user="bob")
        assert _pool_names(mgr._named_session_locks) == []

    @pytest.mark.asyncio
    async def test_a_cancelled_open_leaves_no_pool_lock(self):
        mgr = _mgr(term="telnet")
        opening = asyncio.Event()

        class _NeverOpens(_StubExecSession):
            async def _ensure_initialized(self) -> None:
                opening.set()
                await asyncio.Event().wait()

        mgr._session_factory = _NeverOpens
        task = asyncio.ensure_future(_exec(mgr, cmd="c"))
        await opening.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert _pool_names(mgr._named_sessions) == []
        assert _pool_names(mgr._named_session_locks) == []

    @pytest.mark.asyncio
    async def test_a_second_cancel_during_the_local_stop_leaves_no_dead_entry(
        self, tmp_path, monkeypatch
    ):
        # The first cancel stops the command; the second lands while the
        # session's close is stopping its group, and the close re-raises it.
        monkeypatch.setattr(process_stop, "_EXEC_REAP_TIMEOUT", 0.5)
        real = process_stop.stop_process
        stopping = asyncio.Event()

        async def flagged(p):
            stopping.set()
            await real(p)

        monkeypatch.setattr(process_stop, "stop_process", flagged)
        host = LocalHost()
        mgr = host._session_mgr
        typed = tmp_path / "typed"
        try:
            running = asyncio.ensure_future(
                host.exec(f"trap '' TERM; touch {typed}; exec sleep 30")
            )
            await wait_for_async(typed.exists, 10.0, interval=0.01, on_timeout="never typed")
            running.cancel()
            await stopping.wait()
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running
            # Before: the close raised past its deregister, leaving the dead
            # session registered under its pool name.
            assert _pool_names(mgr._named_sessions) == []
            assert _pool_names(mgr._named_session_locks) == []
        finally:
            await host.close()
