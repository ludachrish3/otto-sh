"""`SessionManager.exec_on_route` on the pooled route: switching user for one call, and
the keep-or-discard rule that protects the free-list (spec D3/D4/D5, §4.2)."""

import logging
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from otto.host.exec_route import ExecRoute
from otto.host.host import DEFAULT_COMMAND_TIMEOUT
from otto.host.login_proxy import Cred, LoginProxyError
from otto.host.session import SessionManager
from otto.logger.mode import LogMode
from otto.result import CommandResult
from otto.utils import Status
from tests.unit.host.test_session import _proxy_connections, _StubExecSession

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
    return SessionManager(
        connections=conn,
        session_factory=_StubExecSession,
        host_id="h",
        creds=creds,
    )


async def _exec(
    mgr: SessionManager,
    route: ExecRoute,
    *,
    cmd: str,
    timeout: float = DEFAULT_COMMAND_TIMEOUT,
    log: LogMode = LogMode.NORMAL,
    **kw,
) -> CommandResult:
    """Dispatch *cmd* on *route*, with ``exec``'s own defaults for the rest."""
    return await mgr.exec_on_route(route=route, cmd=cmd, timeout=timeout, log=log, **kw)


class TestRoute:
    def test_needs_shell_leaves_the_ssh_channel_for_the_pool(self):
        assert _mgr().exec_route() is ExecRoute.SSH_CHANNEL
        assert _mgr().exec_route(needs_shell=True) is ExecRoute.POOLED_SHELL

    def test_hops_pool_regardless(self):
        hop = Cred(login="mysql", proxy="su", via="alice")
        assert _mgr(hops=[hop]).exec_route() is ExecRoute.POOLED_SHELL

    def test_telnet_pools_regardless(self):
        assert _mgr(term="telnet").exec_route() is ExecRoute.POOLED_SHELL

    def test_the_plain_route_is_unbudgeted(self):
        # A budget query must not be perturbed by what one call asks for.
        mgr = _mgr()
        assert mgr.line_budget(route=mgr.exec_route()) is None


@pytest.fixture
def _fast_resync_settle(monkeypatch):
    from otto.host import login_proxy

    monkeypatch.setattr(login_proxy, "_RESYNC_SETTLE", 0.0)


class TestPooledExec:
    @pytest.mark.asyncio
    async def test_plain_pooled_exec_returns_the_session(self):
        mgr = _mgr(term="telnet")
        result = await _exec(mgr, ExecRoute.POOLED_SHELL, cmd="id")
        assert result.value == "OUT"
        assert len(mgr._exec_pool) == 1
        assert mgr._exec_pool[0]._session.run_cmd_calls == ["id"]

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
        await _exec(mgr, ExecRoute.POOLED_SHELL, cmd="sudo -S id", expects=[("assword", "pw\n")])
        assert mgr._exec_pool[0]._session.run_cmd_calls == ["sudo -S id"]
        assert seen == [[("assword", "pw\n")]]

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_fast_resync_settle")
    async def test_user_switches_for_the_call_and_the_session_comes_back(self):
        mgr = _mgr(term="telnet", creds=[Cred(login="root", password="rootpw")])
        seen: list[tuple[str, str]] = []

        class _Switching(_StubExecSession):
            async def run_cmd(self, cmd, **kw):
                seen.append((cmd, self.current_user))
                return await super().run_cmd(cmd=cmd, **kw)

        mgr._session_factory = _Switching
        result = await _exec(mgr, ExecRoute.POOLED_SHELL, cmd="id", user="root")
        assert result.value == "OUT"
        # The identity half is the point: the command ran INSIDE the switch,
        # not before it and not after the undo.
        assert seen[-1] == ("id", "root")
        pooled = mgr._exec_pool
        assert len(pooled) == 1
        assert pooled[0].current_user == "admin", "switch-back restored the acquired identity"

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_fast_resync_settle")
    async def test_a_failed_undo_discards_the_session_and_still_returns_the_result(
        self, monkeypatch, caplog
    ):
        """The command RAN — spec §4.2 step 4: its result is what `exec` owes
        the caller, and a broken switch-back is the session's problem, not the
        result's. The session is discarded (it is still running as the switched
        user) and the failure is reported as a warning naming host, user and
        the undo's own error."""
        mgr = _mgr(term="telnet", creds=[Cred(login="root", password="rootpw")])
        from otto.host import session as session_mod

        async def _broken_undo(io, hop, via, host_id, history_prefix=""):
            raise RuntimeError("undo exploded")

        monkeypatch.setattr(session_mod, "run_undo", _broken_undo)
        closed: list[str] = []
        real_close = session_mod.HostSession.close

        async def _spy_close(self):
            closed.append(self.name)
            await real_close(self)

        monkeypatch.setattr(session_mod.HostSession, "close", _spy_close)

        with caplog.at_level(logging.WARNING, logger="otto.host.session"):
            result = await _exec(mgr, ExecRoute.POOLED_SHELL, cmd="id", user="root")
        assert result.value == "OUT", "the command ran; its result is not the undo's to lose"
        assert mgr._exec_pool == [], "a session that could not switch back is never pooled"
        assert closed == ["__exec_pool_1__"]
        undo_warnings = [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.WARNING and "undo exploded" in r.getMessage()
        ]
        assert undo_warnings, f"no warning named the undo failure: {caplog.text}"
        assert "root" in undo_warnings[0], undo_warnings[0]
        assert "h" in undo_warnings[0], undo_warnings[0]

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_fast_resync_settle")
    async def test_a_failed_switch_discards_the_session_and_raises(self, monkeypatch):
        mgr = _mgr(term="telnet", creds=[Cred(login="root", password="rootpw")])
        from otto.host import session as session_mod

        async def _broken_switch(*a, **kw):
            raise LoginProxyError("hop failed")

        monkeypatch.setattr(session_mod, "perform_switch", _broken_switch)
        with pytest.raises(LoginProxyError):
            await _exec(mgr, ExecRoute.POOLED_SHELL, cmd="id", user="root")
        assert mgr._exec_pool == []

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_fast_resync_settle")
    async def test_the_next_exec_after_a_discard_opens_a_fresh_session(self, monkeypatch):
        mgr = _mgr(term="telnet", creds=[Cred(login="root", password="rootpw")])
        from otto.host import session as session_mod

        async def _broken_undo(io, hop, via, host_id, history_prefix=""):
            raise RuntimeError("undo exploded")

        monkeypatch.setattr(session_mod, "run_undo", _broken_undo)
        await _exec(
            mgr, ExecRoute.POOLED_SHELL, cmd="id", user="root"
        )  # the undo fails; the result still comes back
        monkeypatch.undo()
        await _exec(mgr, ExecRoute.POOLED_SHELL, cmd="id")
        assert mgr._exec_pool_count == 2
        assert [s.name for s in mgr._exec_pool] == ["__exec_pool_2__"]

    @pytest.mark.asyncio
    async def test_a_dead_session_is_closed_not_pooled(self):
        mgr = _mgr(term="telnet")

        class _Dies(_StubExecSession):
            async def run_cmd(self, cmd, **kw):
                self._alive = False  # whatever `alive` reads — see ShellSession.alive
                return await super().run_cmd(cmd=cmd, **kw)

        mgr._session_factory = _Dies
        await _exec(mgr, ExecRoute.POOLED_SHELL, cmd="id")
        assert mgr._exec_pool == []


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


class TestTheSshRowPromotesOffTheRawChannel:
    """Spec §4.1's ssh row: the raw exec channel is for calls that need
    NOTHING a shell provides.

    The telnet cases above can never observe this, because telnet has no raw
    route to be promoted off — every call there pools regardless. On ssh the
    promotion is the whole contract: a call that needs a shell (a user to
    switch to, prompts to answer) reads ``exec_route(needs_shell=True)``, and
    an answer other than the pooled shell would send both calls below down
    the raw channel, where the identity switch and the prompt answers simply
    do not exist (the command would run as the login user, and a `sudo -S`
    would hang on a password nobody typed). The host-side half — that a user
    or a prompt IS a reason to need a shell — is pinned on the plan, in
    ``test_exec_plan.py`` and ``test_unix_host.py``.
    """

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_fast_resync_settle")
    async def test_user_promotes_an_ssh_exec_onto_a_switched_pooled_shell(self):
        mgr = _mgr(term="ssh", creds=[Cred(login="root", password="rootpw")])
        seen: list[tuple[str, str]] = []

        class _Switching(_StubExecSession):
            async def run_cmd(self, cmd, **kw):
                seen.append((cmd, self.current_user))
                return await super().run_cmd(cmd=cmd, **kw)

        mgr._session_factory = _Switching
        route = mgr.exec_route(needs_shell=True)
        assert route is ExecRoute.POOLED_SHELL
        result = await _exec(mgr, route, cmd="id", user="root")

        assert result.value == "OUT"
        # INSIDE the switch: the raw route would have run it as the login user.
        assert seen[-1] == ("id", "root")
        assert len(mgr._exec_pool) == 1
        assert not mgr._connections.ssh.await_count, "the raw exec channel was opened anyway"

    @pytest.mark.asyncio
    async def test_expects_reach_the_promoted_ssh_route(self):
        """The answers have to reach the pooled shell the route promotes to."""
        mgr = _mgr(term="ssh")
        seen: list[object] = []

        class _RecordingExpects(_StubExecSession):
            async def run_cmd(self, cmd, expects=None, **kw):
                seen.append(expects)
                return await super().run_cmd(cmd=cmd, expects=expects, **kw)

        mgr._session_factory = _RecordingExpects
        route = mgr.exec_route(needs_shell=True)
        assert route is ExecRoute.POOLED_SHELL
        result = await _exec(mgr, route, cmd="sudo -S id", expects=[("assword", "pw\n")])

        assert result.value == "OUT"
        assert mgr._exec_pool[0]._session.run_cmd_calls == ["sudo -S id"]
        assert seen == [[("assword", "pw\n")]], "the answers have to REACH the shell"
        mgr._connections.ssh.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_plain_ssh_exec_still_takes_the_raw_channel(self, monkeypatch):
        """The control: with nothing to promote, the cheap route is still the
        one taken — otherwise the two tests above would pass on a product that
        simply pooled everything."""
        mgr = _mgr(term="ssh")
        ran: list[str] = []

        async def _fake_exec_on(conn, cmd, timeout=None, log=None):
            ran.append(cmd)
            return CommandResult(status=Status.Success, value="RAW", command=cmd, retcode=0)

        monkeypatch.setattr(mgr, "exec_on", _fake_exec_on)
        route = mgr.exec_route()
        assert route is ExecRoute.SSH_CHANNEL
        result = await _exec(mgr, route, cmd="id")

        assert result.value == "RAW"
        assert ran == ["id"]
        assert mgr._exec_pool == [], "a plain ssh exec must never open a pooled shell"
        mgr._connections.ssh.assert_awaited_once()


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
        return SessionManager(connections=conn, name="test2"), console

    @staticmethod
    def _spy_default(mgr: SessionManager, monkeypatch: pytest.MonkeyPatch) -> list:
        ran: list = []

        async def fake_run_cmd(cmd, expects=None, timeout=0.0, log=None, write_progress=None):
            ran.append((cmd, expects, timeout))
            return CommandResult(status=Status.Success, value="out", command=cmd, retcode=0)

        async def no_pool(name):
            raise AssertionError(f"a console must never open a second session ({name})")

        monkeypatch.setattr(mgr, "run_cmd", fake_run_cmd)
        monkeypatch.setattr(mgr, "open_session", no_pool)
        return ran

    def test_the_route_is_the_default_session_whatever_the_call_needs(self):
        mgr, _console = self._console_mgr()
        assert mgr.exec_route() is ExecRoute.DEFAULT_SESSION
        assert mgr.exec_route(needs_shell=True) is ExecRoute.DEFAULT_SESSION

    @pytest.mark.asyncio
    async def test_a_plain_exec_runs_on_the_default_session(self, monkeypatch):
        mgr, _console = self._console_mgr()
        ran = self._spy_default(mgr, monkeypatch)

        result = await _exec(mgr, ExecRoute.DEFAULT_SESSION, cmd="id -un", timeout=5.0)

        assert ran == [("id -un", None, 5.0)]
        assert result.value == "out"
        assert mgr._exec_pool == []
        assert mgr._exec_pool_count == 0, "no pool session was ever asked for"

    @pytest.mark.asyncio
    async def test_an_exec_that_needs_a_shell_answers_on_the_default_session(self, monkeypatch):
        mgr, _console = self._console_mgr()
        ran = self._spy_default(mgr, monkeypatch)
        expects = [("Password:", "pw")]

        await _exec(mgr, ExecRoute.DEFAULT_SESSION, cmd="sudo -S true", expects=expects)

        assert [(cmd, exp) for cmd, exp, _t in ran] == [("sudo -S true", expects)]
        assert mgr._exec_pool_count == 0

    @pytest.mark.asyncio
    async def test_exec_as_another_user_is_refused_by_name_at_this_layer(self, monkeypatch):
        """The host switches the one session before it calls in; a bare other user here is a bug."""
        from otto.host.errors import ConsoleError

        mgr, console = self._console_mgr()
        ran = self._spy_default(mgr, monkeypatch)

        with pytest.raises(ConsoleError, match=r"^test2: console is single-client.*'test'.*'root'"):
            await _exec(mgr, ExecRoute.DEFAULT_SESSION, cmd="id -un", user="root")

        assert ran == []
        console.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_exec_as_the_session_user_is_the_session(self, monkeypatch):
        mgr, _console = self._console_mgr()
        ran = self._spy_default(mgr, monkeypatch)

        await _exec(mgr, ExecRoute.DEFAULT_SESSION, cmd="id -un", user="test")

        assert [cmd for cmd, _e, _t in ran] == ["id -un"]

    def test_the_line_budget_is_the_typed_one(self):
        """exec types into the console's shell, so a transfer must size its lines to it."""
        from otto.host.session import typed_line_budget

        mgr, _console = self._console_mgr()
        assert mgr.line_budget(route=mgr.exec_route()) == typed_line_budget(
            mgr._command_frame, echoes=True
        )
