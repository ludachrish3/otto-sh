"""SessionManager._apply_session_setup: order, kinds, frame entry, errors, routing."""

import re
from contextlib import suppress
from types import SimpleNamespace

import pytest

from otto.host import session as session_mod
from otto.host.app_shell import AppShell
from otto.host.command_frame import BashFrame, RawFrame, ZephyrFrame
from otto.host.element import Element
from otto.host.errors import RawLandingError
from otto.host.login_proxy import Cred
from otto.host.session import SessionManager, ShellSession, _ExecRoute
from otto.host.session_setup import (
    SESSION_SETUPS,
    SessionSetup,
    SessionSetupError,
    register_session_setup,
)
from otto.host.unix_host import UnixHost
from otto.logger.mode import LogMode
from otto.utils import Status
from tests._fixtures.dialect_shell import DialectSession, DialectShell
from tests.conftest import active_context


def _conn(hops=(), login_target="admin", credentials=("admin", "pw")):
    return SimpleNamespace(
        credentials=credentials, login_target=login_target, proxy_hops=list(hops), term="ssh"
    )


class _App(AppShell):
    """The smallest AppShell :class:`DialectShell`'s app state answers."""

    launch = "provision-app"
    prompt = re.compile(r"app> ")
    quit_cmd = "quit"


class _Harness:
    """One manager over DialectSessions; records what the hook saw."""

    def __init__(
        self,
        hook,
        *,
        landing=None,
        target=None,
        hops=(),
        setup_params=None,
        menu=False,
        app=False,
        shell_history=True,
        extra_creds=(),
    ):
        self.shells: list[DialectShell] = []
        self.sessions: list[DialectSession] = []
        self.calls: list[tuple[str, str]] = []  # (kind, user)
        name = f"t5-{id(self)}"

        async def wrapped(session, ctx):
            self.calls.append((ctx.kind, ctx.user))
            await hook(session, ctx)

        register_session_setup(name, wrapped, overwrite=True)
        self.name = name
        launch, quit_line = _App.launch + "\n", _App.quit_cmd + "\n"
        app_kw = {"app_launch": launch, "app_quit": quit_line} if app else {}

        def factory():
            shell = DialectShell(menu=menu, **app_kw)
            s = DialectSession(
                shell,
                command_frame=landing or target or BashFrame(),
                shell_history=shell_history,
            )
            s._init_timeout = 0.3
            self.shells.append(shell)
            self.sessions.append(s)
            return s

        self.mgr = SessionManager(
            # login_target is the LAST hop's login, as
            # ConnectionManager.proxy_hops guarantees (both are derived from
            # one resolve_chain): a fixture that left it at the direct cred
            # would model a chain that cannot exist.
            connections=_conn(hops, login_target=hops[-1].login if hops else "admin"),
            name="h",
            session_factory=factory,
            creds=[Cred(login="admin", password="pw"), *hops, *extra_creds],
            host_id="h",
            command_frame=target,
            landing_frame=landing,
            session_setup=SessionSetup(name=name, params=setup_params or {}),
            shell_history=shell_history,
            retry_backoff=0,
        )

    def unregister(self):
        SESSION_SETUPS.unregister(self.name)


async def _export(session, ctx):
    await session.run("export APP_ENV=lab")


async def _enter_python(session, ctx):
    await session.run("which python3")
    await session.send("python3\n")


@pytest.mark.asyncio
async def test_hook_runs_after_the_last_hop_and_sees_the_proxied_user():
    hop = Cred(login="mysql", proxy="su", via="admin")
    h = _Harness(_export, hops=[hop])
    try:
        await h.mgr._ensure_session()
        writes = h.shells[0].writes
        su = next(i for i, w in enumerate(writes) if w == "su - mysql\n")
        hook_cmd = next(i for i, w in enumerate(writes) if "export APP_ENV" in w)
        assert su < hook_cmd
        assert h.calls == [("default", "mysql")]
    finally:
        h.unregister()


@pytest.mark.asyncio
async def test_post_hook_confirmation_runs_unconditionally_on_a_same_frame_host():
    h = _Harness(_export)
    try:
        await h.mgr._ensure_session()
        readies = [w for w in h.shells[0].writes if "_READY__" in w]
        assert len(readies) == 2  # landing handshake + post-hook frame entry
        assert readies[0] != readies[1]  # fresh markers
        assert h.sessions[0].opens == 1  # never reopened
    finally:
        h.unregister()


@pytest.mark.asyncio
async def test_two_dialects_bash_landing_zephyr_target():
    h = _Harness(_enter_python, landing=BashFrame(), target=ZephyrFrame())
    try:
        await h.mgr._ensure_session()
        shell = h.shells[0]
        assert shell.commands[0] == ("bash", "which python3")
        assert isinstance(h.sessions[0]._frame, ZephyrFrame)
        r = await h.mgr.run_cmd("kernel version")
        assert r.value == "zephyr:kernel version"
        assert r.retcode == 0
        assert shell.commands[-1] == ("zephyr", "kernel version")
    finally:
        h.unregister()


@pytest.mark.asyncio
async def test_hook_may_enter_the_frame_itself_and_run_in_the_target_dialect():
    async def hook(session, ctx):
        await session.send("python3\n")
        await session.enter_frame()
        r = (await session.run("kernel version")).only
        assert r.value == "zephyr:kernel version"

    h = _Harness(hook, landing=BashFrame(), target=ZephyrFrame())
    try:
        await h.mgr._ensure_session()
        readies = [w for w in h.shells[0].writes if "_READY__" in w]
        assert len(readies) == 3  # landing + hook's entry + manager's confirmation
    finally:
        h.unregister()


@pytest.mark.asyncio
async def test_hook_that_leaves_the_console_in_the_wrong_shell_fails_at_frame_entry():
    async def hook(session, ctx):
        await session.send("python3\n")  # navigates away; never enters the frame

    h = _Harness(hook)  # same-frame host: target is bash, console now answers zephyr only
    try:
        with pytest.raises(SessionSetupError, match=r"left no shell that answers frame 'bash'"):
            await h.mgr._ensure_session()
        assert h.sessions[0].closed
        assert len(h.sessions) == 1  # no retry
    finally:
        h.unregister()


@pytest.mark.asyncio
async def test_hook_exception_is_wrapped_and_tears_down_without_retry():
    async def hook(session, ctx):
        raise RuntimeError("boom")

    h = _Harness(hook)
    try:
        with pytest.raises(SessionSetupError, match=r"h: session setup .* failed: boom"):
            await h.mgr._ensure_session()
        assert h.sessions[0].closed
        assert len(h.sessions) == 1
    finally:
        h.unregister()


@pytest.mark.asyncio
async def test_raw_landing_writes_nothing_before_the_hook():
    async def hook(session, ctx):
        with pytest.raises(RawLandingError):
            await session.run("ls")
        await session.send("python3\n")

    h = _Harness(hook, landing=RawFrame(), target=ZephyrFrame())
    try:
        await h.mgr._ensure_session()
        assert h.shells[0].writes[0] == "python3\n"
        assert isinstance(h.sessions[0]._frame, ZephyrFrame)
    finally:
        h.unregister()


@pytest.mark.asyncio
async def test_a_raw_landing_run_escapes_as_itself_rather_than_as_a_setup_failure():
    async def hook(session, ctx):
        await session.run("ls")  # framed, in a raw landing: the hook author's bug

    h = _Harness(hook, landing=RawFrame(), target=ZephyrFrame())
    try:
        with pytest.raises(RawLandingError, match="raw landing"):
            await h.mgr._ensure_session()
        assert h.sessions[0].closed
        assert len(h.sessions) == 1  # torn down, not retried — but still named for what it is
    finally:
        h.unregister()


@pytest.mark.asyncio
async def test_a_hook_that_already_lost_the_shell_is_not_relabelled_left_no_shell():
    async def hook(session, ctx):
        # The hook enters the frame itself, the entry fails, and the hook
        # swallows it. The session is dead when the manager's confirmation
        # runs, which is a DIFFERENT diagnosis from "the console answers
        # another dialect" and must not be reported as that one.
        with suppress(ConnectionError):
            await session.enter_frame()

    h = _Harness(hook, landing=RawFrame(), target=ZephyrFrame(), menu=True)
    try:
        with pytest.raises(SessionSetupError, match="already lost the shell"):
            await h.mgr._ensure_session()
        assert h.sessions[0].closed
    finally:
        h.unregister()


@pytest.mark.asyncio
async def test_raw_landing_menu_that_never_moves_fails_at_frame_entry():
    async def hook(session, ctx):
        await session.send("9\n")  # not the transition line

    h = _Harness(hook, landing=RawFrame(), target=ZephyrFrame(), menu=True)
    try:
        with pytest.raises(SessionSetupError, match="left no shell"):
            await h.mgr._ensure_session()
    finally:
        h.unregister()


@pytest.mark.asyncio
async def test_hook_runs_once_per_open_and_named_sessions_get_kind_named():
    h = _Harness(_export)
    try:
        await h.mgr._ensure_session()
        await h.mgr.run_cmd("echo again")
        await h.mgr.open_session("aux")
        assert h.calls == [("default", "admin"), ("named", "admin")]
    finally:
        h.unregister()


@pytest.mark.asyncio
async def test_a_named_session_whose_hook_fails_is_torn_down_and_left_unregistered():
    async def hook(session, ctx):
        raise RuntimeError("boom")

    h = _Harness(hook)
    try:
        with pytest.raises(SessionSetupError, match="failed: boom"):
            await h.mgr.open_session("aux")
        assert h.sessions[0].closed
        assert "aux" not in h.mgr._named_sessions  # no half-built entry left behind
    finally:
        h.unregister()


@pytest.mark.asyncio
async def test_exec_route_is_pooled_on_a_hook_host_and_the_hook_sees_exec_pool():
    h = _Harness(_export)
    try:
        assert h.mgr._exec_route() is _ExecRoute.POOLED_SHELL
        r = await h.mgr.exec("id")
        assert r.retcode == 0
        assert ("exec_pool", "admin") in h.calls
    finally:
        h.unregister()


@pytest.mark.asyncio
async def test_a_host_without_a_hook_is_unchanged():
    conn = _conn()
    built = []

    def factory():
        s = DialectSession(DialectShell())
        built.append(s)
        return s

    mgr = SessionManager(connections=conn, name="h", session_factory=factory, host_id="h")
    await mgr._ensure_session()
    readies = [w for w in built[0].shell.writes if "_READY__" in w]
    assert len(readies) == 1
    assert mgr._exec_route() is _ExecRoute.SSH_CHANNEL


@pytest.mark.asyncio
async def test_history_prefix_renders_from_the_landing_frame():
    h = _Harness(_export, landing=BashFrame(), target=ZephyrFrame(), shell_history=False)
    try:
        # Pinned against the frames themselves rather than a literal: bash's
        # payload leads with the `command` prefix that survives a special
        # builtin's failure, so `startswith("HISTFILE=")` would be false of
        # the very bytes this renders. The pair is the whole point — the
        # LANDING frame has a payload, the command frame renders none, so an
        # empty answer here would mean the command frame was asked.
        assert h.mgr._history_prefix() == BashFrame().quiet_history()
        assert "HISTFILE=/dev/null" in h.mgr._history_prefix()
        assert ZephyrFrame().quiet_history() == ""
    finally:
        h.unregister()


# --- Dry run, AppShell, once-only and the exec pool ------------------------


def _open_counting_transport(events: list[str]) -> type:
    """A ShellSession that records being BUILT and being OPENED, and nothing else."""

    class _Counted(ShellSession):
        def __init__(self, *args, **kw) -> None:
            super().__init__(command_frame=kw.get("command_frame"))
            events.append("built")

        async def _open(self) -> None:
            events.append("open")

        async def _write(self, data: str) -> None: ...

        async def _read_until_pattern(self, pattern):
            raise AssertionError("a dry run must not reach a handshake")

        async def close(self) -> None:
            self._alive = False
            self._initialized = False

    return _Counted


@pytest.mark.asyncio
async def test_a_dry_run_on_a_hooked_host_opens_nothing_and_never_calls_the_hook(monkeypatch):
    """A declared hook changes nothing about a dry run: no transport, no hook.

    The decline happens at the HOST, above the manager, so the hook is
    unreachable rather than skipped — which is why the transport double here
    counts construction as well as ``_open``: a manager that got as far as
    building a session has already lost this property.
    """
    calls: list[str] = []

    async def fn(session, ctx):
        calls.append(ctx.kind)

    register_session_setup("t5-dry", fn, overwrite=True)
    events: list[str] = []
    monkeypatch.setattr(session_mod, "SshSession", _open_counting_transport(events))
    try:
        with active_context(dry_run=True):
            host = UnixHost(
                ip="10.0.0.1",
                element=Element("box"),
                creds=[Cred(login="admin", password="pw")],
                log=LogMode.QUIET,
                session_setup=SessionSetup(name="t5-dry"),
            )
            r = (await host.run("ls -la")).only
        assert r.status == Status.NotRun
        assert events == []
        assert calls == []
    finally:
        SESSION_SETUPS.unregister("t5-dry")


@pytest.mark.asyncio
async def test_a_hook_that_raises_inside_an_app_shell_unlocks_and_closes_the_session():
    """The app-shell lock is released, the session closed, the cause preserved."""

    async def hook(session, ctx):
        async with _App.attach(session):
            raise RuntimeError("boom")

    h = _Harness(hook, app=True)
    try:
        with pytest.raises(SessionSetupError, match=r"failed: boom") as caught:
            await h.mgr._ensure_session()
        assert isinstance(caught.value.__cause__, RuntimeError)
        session = h.sessions[0]
        # A stranded lock is the failure that matters: the session would then
        # refuse every later run() with AppShellActiveError instead of the
        # error the hook actually raised.
        assert session._app_shell is None
        assert session.closed
    finally:
        h.unregister()


@pytest.mark.asyncio
async def test_frame_entry_after_an_app_shell_exit_sends_exactly_one_handshake():
    """The exit's recovery confirmation, then ONE target handshake — not two."""

    async def hook(session, ctx):
        async with _App.attach(session):
            pass

    h = _Harness(hook, app=True)
    try:
        await h.mgr._ensure_session()
        writes = h.shells[0].writes
        assert _App.launch + "\n" in writes  # the app really was attached
        recover = next(i for i, w in enumerate(writes) if "_RECOVER__" in w)
        readies = [i for i, w in enumerate(writes) if "_READY__" in w]
        assert len(readies) == 2, writes  # landing handshake + frame entry
        assert readies[1] > recover  # the app's exit confirmed first
        assert h.sessions[0].opens == 1
    finally:
        h.unregister()


@pytest.mark.asyncio
async def test_frame_entry_carries_the_target_frames_history_prefix():
    """The second handshake's BYTES, sourced from the frame it is entering.

    A raw landing renders no prefix of its own and writes nothing at all, so
    the single write here is frame entry's — and it has to carry BASH's
    suppression payload, which only the target frame can produce.
    """

    async def hook(session, ctx):
        return None

    h = _Harness(hook, landing=RawFrame(), target=BashFrame(), shell_history=False)
    try:
        await h.mgr._ensure_session()
        writes = h.shells[0].writes
        assert len(writes) == 1, writes
        assert "_READY__" in writes[0]
        assert writes[0].startswith(BashFrame().quiet_history())
        assert RawFrame().quiet_history() == ""  # the landing could not have supplied it
    finally:
        h.unregister()


@pytest.mark.asyncio
async def test_as_user_inside_a_hooked_session_does_not_re_run_the_hook():
    """An identity change inside an established session is not a new session.

    Both live sessions are used inside the block — the elevated one through
    its own handle, the default one through the manager — because "the hook
    ran once" is only a claim about a session that is ASKED FOR again. A
    manager that re-established either would re-run the hook here.
    """
    root = Cred(login="root", proxy="su", via="admin")
    h = _Harness(_export, extra_creds=[root])
    try:
        await h.mgr._ensure_session()
        aux = await h.mgr.open_session("aux")
        assert h.calls == [("default", "admin"), ("named", "admin")]
        async with aux.as_user("root"):
            assert aux.current_user == "root"
            assert (await aux.run("id -un")).only.retcode == 0
            assert (await h.mgr.run_cmd("echo hi")).retcode == 0
        assert aux.current_user == "admin"
        assert h.calls == [("default", "admin"), ("named", "admin")]  # never again
        assert [s.opens for s in h.sessions] == [1, 1]
    finally:
        h.unregister()


@pytest.mark.asyncio
async def test_a_hook_that_raises_on_the_exec_pool_surfaces_from_exec():
    """exec() on a hooked host is a pooled session, so a hook failure is exec's."""

    async def hook(session, ctx):
        raise RuntimeError("boom")

    h = _Harness(hook)
    try:
        with pytest.raises(SessionSetupError, match=r"session setup .* failed: boom"):
            await h.mgr.exec("id")
        assert [kind for kind, _user in h.calls] == ["exec_pool"]
    finally:
        h.unregister()


# --- The four BUILD sites -------------------------------------------------
#
# Every test above supplies a `session_factory`, which short-circuits both
# `_build_session` and `open_session`'s transport arms — so the four places
# that actually hand a frame to a constructed session never run. These stand
# the two transports (and telnet's client) up as recorders instead, which is
# the only way "sessions are BUILT with the landing frame" is pinned at all.


def _recording_transport(built: list) -> type:
    """A ShellSession that records the ``command_frame`` it was HANDED.

    The raw keyword, not ``self._frame``: ``ShellSession.__init__`` resolves
    ``None`` to a fresh ``BashFrame()``, which would make "handed nothing" and
    "handed bash" indistinguishable.
    """

    class _Recorded(ShellSession):
        def __init__(self, *args, **kw) -> None:
            super().__init__(command_frame=kw.get("command_frame"))
            self.handed_frame = kw.get("command_frame")
            built.append(self)

        async def _open(self) -> None: ...

        async def _write(self, data: str) -> None: ...

        async def _read_until_pattern(self, pattern):
            raise AssertionError("the build-site test must not reach a handshake")

        async def close(self) -> None:
            self._alive = False
            self._initialized = False

        async def _ensure_initialized(self) -> None:
            self._initialized = True
            self._alive = True

    return _Recorded


class _StubTelnetClient:
    """What ``open_session``'s telnet arm dials, minus the socket."""

    def __init__(self, *args, **kw) -> None:
        self.reader = None
        self.writer = None
        self.options = SimpleNamespace(write_chunk_size=None, write_chunk_delay=None)

    async def connect(self) -> None: ...

    async def close(self) -> None: ...


def _term_conn(term):
    """Exactly what `_build_session` and `open_session` read off a ConnectionManager."""
    opts = SimpleNamespace(write_chunk_size=None, write_chunk_delay=None)

    async def ssh():
        return object()

    async def telnet():
        return SimpleNamespace(reader=None, writer=None, options=opts)

    async def telnet_target():
        return SimpleNamespace(host="10.0.0.1", port=23)

    return SimpleNamespace(
        term=term,
        credentials=("admin", "pw"),
        login_target="admin",
        proxy_hops=[],
        telnet_options=opts,
        ssh=ssh,
        telnet=telnet,
        telnet_target=telnet_target,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("term", ["ssh", "telnet"])
@pytest.mark.parametrize("path", ["default", "named"])
@pytest.mark.parametrize("has_landing", [True, False], ids=["landing", "no-landing"])
async def test_every_transport_arm_builds_the_session_with_the_landing_frame(
    monkeypatch, term, path, has_landing
):
    built: list = []
    recorded = _recording_transport(built)
    monkeypatch.setattr(session_mod, "SshSession", recorded)
    monkeypatch.setattr(session_mod, "TelnetSession", recorded)
    monkeypatch.setattr(session_mod, "TelnetClient", _StubTelnetClient)

    landing = BashFrame() if has_landing else None
    command = ZephyrFrame()
    mgr = SessionManager(
        connections=_term_conn(term),
        name="h",
        host_id="h",
        command_frame=command,
        landing_frame=landing,
    )

    if path == "default":
        await mgr._ensure_session()
    else:
        await mgr.open_session("aux")

    assert len(built) == 1
    # The landing frame when one is declared, the command frame otherwise --
    # identity, not equality, so a site reverted to `self._command_frame`
    # cannot pass by rendering the same bytes.
    assert built[0].handed_frame is (landing if has_landing else command)


def test_an_exec_factory_does_not_win_on_a_hooked_host():
    """The FACTORY route's `and not hooked` clause: a raw primitive cannot run setup."""
    register_session_setup("t5-exec-route", _export, overwrite=True)

    def factory(cmd, timeout):  # pragma: no cover - never routed to
        raise AssertionError("a hooked host must not reach the exec factory")

    try:
        hooked = SessionManager(
            connections=_conn(),
            name="h",
            host_id="h",
            exec_factory=factory,
            session_setup=SessionSetup(name="t5-exec-route"),
        )
        assert hooked._exec_route() is _ExecRoute.POOLED_SHELL
        # The same manager without the hook still takes the fast path, so this
        # cannot be satisfied by pooling everything.
        plain = SessionManager(connections=_conn(), name="h", host_id="h", exec_factory=factory)
        assert plain._exec_route() is _ExecRoute.FACTORY
    finally:
        SESSION_SETUPS.unregister("t5-exec-route")
