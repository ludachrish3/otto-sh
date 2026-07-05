"""Unit tests for the AppShell parse engine (Task 12).

Covers ``Parsed`` and the ``parse_one`` / ``parse_all`` / ``apply_parse``
functions in :mod:`otto.host.app_shell`:

* class-definition-time guards (compiled ``pattern`` required; the pattern's
  named groups must be a subset of the model's fields and a superset of its
  required fields — pattern/model drift is impossible);
* regex-driven type conversion via pydantic;
* the three ``parse=`` forms (single model / ``list[Model]`` / callable escape
  hatch);
* nested region-recursion: a field typed as another ``Parsed`` (or
  ``list[Sub]`` / ``Sub | None``) is parsed from the region its group captured.

The nested ``Row``/``Table``/``QueryStats``/``Select`` models are taken
verbatim from spec §9 and exercised against a representative mysql ``SELECT``
output block.
"""

import asyncio
import re

import pytest
from typing_extensions import override

from otto.host.app_shell import (
    AppShell,
    AppShellActiveError,
    AppShellTimeoutError,
    Parsed,
    ParseMismatch,
    apply_parse,
    parse_all,
    parse_one,
)
from otto.host.host import BaseHost
from otto.host.session import ShellSession
from otto.result import ShellResult
from otto.utils import Status


class Kv(Parsed):
    """``key=number`` pair — the workhorse fixture for the flat-parse tests."""

    pattern = re.compile(r"(?P<key>\w+)=(?P<n>\d+)")
    key: str
    n: int


# --------------------------------------------------------------------------- #
# class-definition-time guards
# --------------------------------------------------------------------------- #
def test_parsed_requires_pattern():
    with pytest.raises(TypeError, match="pattern"):

        class NoPattern(Parsed):
            x: int


def test_parsed_group_field_drift_is_class_def_error():
    with pytest.raises(TypeError, match="named groups"):

        class Drift(Parsed):
            pattern = re.compile(r"(?P<typo>\d+)")
            x: int


def test_parsed_required_field_without_group_is_class_def_error():
    with pytest.raises(TypeError, match="required fields"):

        class Missing(Parsed):
            pattern = re.compile(r"(?P<key>\w+)")
            key: str
            n: int  # required, but no (?P<n>...) group -> drift the other way


# --------------------------------------------------------------------------- #
# parse_one — single search + pydantic conversion
# --------------------------------------------------------------------------- #
def test_parse_one_converts_types():
    result = parse_one(Kv, "a=5")
    assert isinstance(result, Kv)
    assert result.key == "a"
    assert result.n == 5  # str "5" -> int 5 via pydantic
    assert isinstance(result.n, int)


def test_parse_one_mismatch_raises():
    with pytest.raises(ParseMismatch) as excinfo:
        parse_one(Kv, "nothing to match here")
    # The offending pattern is surfaced (repr'd) in the message for debugging.
    assert repr(Kv.pattern.pattern) in str(excinfo.value)


# --------------------------------------------------------------------------- #
# parse_all — finditer over the whole text
# --------------------------------------------------------------------------- #
def test_parse_all_returns_one_per_match():
    rows = parse_all(Kv, "a=1 b=2 c=3")
    assert [(r.key, r.n) for r in rows] == [("a", 1), ("b", 2), ("c", 3)]


def test_parse_all_empty_is_valid():
    # No match anywhere -> the empty list is a valid "zero rows" answer,
    # NOT a mismatch.
    assert parse_all(Kv, "no key value pairs present") == []


# --------------------------------------------------------------------------- #
# optional groups -> None
# --------------------------------------------------------------------------- #
def test_optional_group_maps_to_none():
    class Opt(Parsed):
        pattern = re.compile(r"x(?P<opt>\d+)?")
        opt: str | None = None

    # "x" matches but the optional group never participates -> None.
    assert parse_one(Opt, "x").opt is None
    # When it does participate the group text flows through pydantic.
    assert parse_one(Opt, "x42").opt == "42"


# --------------------------------------------------------------------------- #
# nested region-recursion — the spec §9 mysql SELECT example (verbatim models)
# --------------------------------------------------------------------------- #
class Row(Parsed):
    pattern = re.compile(r"^\|(?P<cells>.+)\|$", re.MULTILINE)
    cells: str  # or one named group per column


class Table(Parsed):
    pattern = re.compile(r"(?P<rows>(?:^\|.+\|$\n?)+)", re.MULTILINE)
    rows: list[Row]


class QueryStats(Parsed):
    pattern = re.compile(r"(?P<count>\d+) rows? in set \((?P<seconds>[\d.]+) sec\)")
    count: int
    seconds: float


class Select(Parsed):
    pattern = re.compile(
        r"(?P<table>^\+-[\s\S]+?^\+-[^\n]*$)\s*(?P<stats>\d+ rows? in set[^\n]*)",
        re.MULTILINE,
    )
    table: Table
    stats: QueryStats


# A representative mysql SELECT block: a `+--+` bordered table with exactly
# five `| ... |` data rows followed by the stats line. It is deliberately
# header-less: the verbatim ``Table`` pattern greedily captures the *first*
# contiguous run of pipe-lines, so a column header separated from the data by
# a `+--+` border rule would be captured instead of the rows. Keeping the five
# data rows as the sole pipe-run matches the spec's own annotation that
# ``result.value.table.rows[0]`` is a *data* row (Alice), not a header.
SELECT_OUTPUT = (
    "+----+---------+-------------+\n"
    "|  1 | Alice   | Engineering |\n"
    "|  2 | Bob     | Sales       |\n"
    "|  3 | Carol   | Marketing   |\n"
    "|  4 | Dave    | Engineering |\n"
    "|  5 | Eve     | Support     |\n"
    "+----+---------+-------------+\n"
    "5 rows in set (0.00 sec)"
)


def test_nested_select_example():
    value = apply_parse(Select, SELECT_OUTPUT)

    assert isinstance(value, Select)
    # trailing stats line parsed by QueryStats' own pattern over its region
    assert value.stats.count == 5
    assert value.stats.seconds == 0.0
    # the table -> rows region recursed all the way down to five Row objects
    assert len(value.table.rows) == 5
    assert all(isinstance(r, Row) for r in value.table.rows)
    # rows[0] is the first *data* row, not a header
    assert "Alice" in value.table.rows[0].cells


# --------------------------------------------------------------------------- #
# apply_parse dispatch — list form and callable escape hatch
# --------------------------------------------------------------------------- #
def test_apply_parse_list_form():
    rows = apply_parse(list[Kv], "a=1 b=2")
    assert [(r.key, r.n) for r in rows] == [("a", 1), ("b", 2)]


def test_apply_parse_single_model_form():
    value = apply_parse(Kv, "a=7")
    assert isinstance(value, Kv)
    assert value.n == 7


def test_apply_parse_callable_form():
    assert apply_parse(str.upper, "x") == "X"


def test_apply_parse_callable_exception_is_wrapped():
    def boom(_text: str) -> str:
        raise RuntimeError("callable blew up")

    with pytest.raises(ParseMismatch, match="callable blew up"):
        apply_parse(boom, "anything")


def test_apply_parse_list_of_non_parsed_is_type_error():
    # list[...] is reserved for list[Parsed]; a scalar element is a misuse.
    with pytest.raises(TypeError, match="Parsed subclass"):
        apply_parse(list[str], "a=1")


def test_apply_parse_unsupported_spec_is_type_error():
    # Not a Parsed subclass, not list[Parsed], not callable -> rejected.
    with pytest.raises(TypeError, match="unsupported parse spec"):
        apply_parse(42, "anything")


# =========================================================================== #
# AppShell core + session locking (Task 13)
# =========================================================================== #
#
# The AppShell tests drive a FAKE HostSession over a scripted ShellSession. The
# ShellSession fake is a *real* ``ShellSession`` subclass so it carries the real
# ``run_cmd`` guard and the real ``_app_shell`` lock slot; only its I/O and the
# ``_recover_session`` handshake are stubbed. The HostSession fake mirrors the
# thin ``send``/``expect``/``run`` delegation the real one performs.


class _ReachedEnsureReadyError(Exception):
    """Sentinel proving ``run_cmd`` fell through the lock guard to real work."""


class FakeShellSession(ShellSession):
    """A real ShellSession whose I/O + recovery are stubbed.

    Instantiating it exercises the real ``ShellSession.__init__`` (so
    ``_app_shell`` defaults to ``None``) and inherits the real ``run_cmd`` lock
    guard. ``_recover_session`` records that it ran; ``_ensure_ready`` raises a
    sentinel so a guard-passthrough can be observed without real transport I/O.
    """

    def __init__(self):
        super().__init__()
        self.recovered = False

    @override
    async def _open(self):
        raise NotImplementedError

    @override
    async def _write(self, data):
        raise NotImplementedError

    @override
    async def _read_until_pattern(self, pattern):
        raise NotImplementedError

    @override
    async def close(self):
        pass

    @override
    async def _ensure_ready(self):
        raise _ReachedEnsureReadyError

    @override
    async def _recover_session(self):
        self.recovered = True
        return ""


class FakeHostSession:
    """Duck-typed HostSession: records sends, scripts expect, delegates run.

    ``expect`` pops items off ``script`` — a str is returned, a BaseException is
    raised (to script a prompt timeout). ``run`` funnels through the underlying
    ShellSession's real ``run_cmd`` so the lock guard is exercised end-to-end.
    """

    def __init__(self, session, script=None):
        self._session = session
        self.sent = []
        self.expected = []
        self._script = list(script or [])

    async def send(self, text, log=None):
        self.sent.append(text)

    async def expect(self, pattern, timeout=10.0):
        self.expected.append((pattern, timeout))
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def run(self, cmds, *args, **kwargs):
        return await self._session.run_cmd(str(cmds))


class DemoShell(AppShell):
    """Minimal AppShell fixture: mysql-like launch/prompt/quit."""

    launch = "mysql"
    prompt = re.compile(r"mysql> \Z")
    quit_cmd = "quit"


def _demo(script):
    """Build a (FakeHostSession, FakeShellSession) pair scripted with ``script``."""
    inner = FakeShellSession()
    return FakeHostSession(inner, script=script), inner


# --------------------------------------------------------------------------- #
# class-definition-time guards
# --------------------------------------------------------------------------- #
def test_appshell_requires_launch():
    with pytest.raises(TypeError, match="launch"):

        class NoLaunch(AppShell):
            prompt = re.compile(r">>> ")


def test_appshell_requires_prompt():
    with pytest.raises(TypeError, match="prompt"):

        class NoPrompt(AppShell):
            launch = "python3"


def test_appshell_normalizes_str_prompt_to_pattern():
    class StrPrompt(AppShell):
        launch = "python3"
        prompt = r">>> \Z"

    assert isinstance(StrPrompt.prompt, re.Pattern)
    assert StrPrompt.prompt.pattern == r">>> \Z"


def test_appshell_subclass_inherits_launch_and_prompt():
    # A sub-subclass that redefines nothing inherits the (already-compiled)
    # ClassVars without re-tripping the class-def guard.
    class Child(DemoShell):
        quit_cmd = "\\q"

    assert Child.launch == "mysql"
    assert isinstance(Child.prompt, re.Pattern)


# --------------------------------------------------------------------------- #
# attach — launch, yield instance, quit + recover + unlock on exit
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_attach_launches_yields_and_cleans_up():
    session, inner = _demo(["\nmysql> "])
    async with DemoShell.attach(session) as shell:
        assert isinstance(shell, DemoShell)
        # The lock is held: the underlying session names this shell.
        assert inner._app_shell is shell

    # Launch sent, then quit sent, in order.
    assert session.sent == ["mysql\n", "quit\n"]
    # Frame recovery ran to confirm the POSIX shell is back.
    assert inner.recovered is True
    # Lock always released on exit.
    assert inner._app_shell is None


# --------------------------------------------------------------------------- #
# session locking — run()/run_cmd blocked while attached
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_run_blocked_while_attached_names_shell_class():
    session, inner = _demo(["\nmysql> "])
    async with DemoShell.attach(session):
        # Raw run_cmd on the locked ShellSession is rejected, naming the shell.
        with pytest.raises(AppShellActiveError, match="DemoShell"):
            await inner.run_cmd("ls -la")
        # And through the HostSession.run delegation too.
        with pytest.raises(AppShellActiveError, match="DemoShell"):
            await session.run("whoami")
    # Once the shell exits the lock is gone; the guard no longer fires.
    assert inner._app_shell is None


@pytest.mark.asyncio
async def test_run_cmd_guard_is_passthrough_when_unlocked():
    inner = FakeShellSession()  # _app_shell defaults to None
    # With no shell attached the guard must not fire — execution falls through
    # to _ensure_ready (here a sentinel) instead of raising AppShellActiveError.
    with pytest.raises(_ReachedEnsureReadyError):
        await inner.run_cmd("ls")


@pytest.mark.asyncio
async def test_nested_attach_raises_without_disturbing_first_lock():
    session, inner = _demo(["\nmysql> "])
    async with DemoShell.attach(session) as first:
        with pytest.raises(AppShellActiveError, match="DemoShell"):
            async with DemoShell.attach(session):
                pass
        # The first shell still owns the lock; the failed nested attach did not
        # clear it or run recovery.
        assert inner._app_shell is first
        assert inner.recovered is False
    assert inner._app_shell is None


# --------------------------------------------------------------------------- #
# cmd — echo + prompt stripping, parse dispatch, parse-mismatch semantics
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_cmd_strips_echo_and_prompt():
    session, _inner = _demo(["\nmysql> ", "SELECT 1\n1\nmysql> "])
    async with DemoShell.attach(session) as shell:
        result = await shell.cmd("SELECT 1")

    assert isinstance(result, ShellResult)
    assert result.status is Status.Success
    assert result.command == "SELECT 1"
    # Echoed "SELECT 1" line and trailing "mysql> " prompt removed.
    assert result.output == "1\n"
    assert result.value == "1\n"


@pytest.mark.asyncio
async def test_cmd_handles_echo_off_apps():
    # No echoed command line — only the output and the prompt.
    session, _inner = _demo(["\nmysql> ", "42\nmysql> "])
    async with DemoShell.attach(session) as shell:
        result = await shell.cmd("SELECT 42")
    assert result.output == "42\n"


@pytest.mark.asyncio
async def test_cmd_strips_ansi_sequences():
    session, _inner = _demo(["\nmysql> ", "SELECT 1\n\x1b[32mgreen\x1b[0m\nmysql> "])
    async with DemoShell.attach(session) as shell:
        result = await shell.cmd("SELECT 1")
    assert result.output == "green\n"


@pytest.mark.asyncio
async def test_cmd_with_parse_returns_typed_value():
    session, _inner = _demo(["\nmysql> ", "SELECT 1\na=5\nmysql> "])
    async with DemoShell.attach(session) as shell:
        result = await shell.cmd("SELECT 1", parse=Kv)
    assert result.status is Status.Success
    assert isinstance(result.value, Kv)
    assert (result.value.key, result.value.n) == ("a", 5)
    assert result.output == "a=5\n"


@pytest.mark.asyncio
async def test_cmd_parse_mismatch_is_failed_result_not_exception():
    session, _inner = _demo(["\nmysql> ", "SELECT 1\nnope\nmysql> "])
    async with DemoShell.attach(session) as shell:
        result = await shell.cmd("SELECT 1", parse=Kv)
    # A parse mismatch is a DATA problem: failed ShellResult, not a raise.
    assert result.status is Status.Failed
    assert result.value is None
    assert result.msg  # names what didn't match
    # Output is preserved for debugging.
    assert result.output == "nope\n"


# --------------------------------------------------------------------------- #
# failure semantics — prompt timeout raises; state handled on unwind
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_launch_timeout_unlocks_and_does_not_recover():
    # The launch prompt never arrives -> AppShellTimeoutError from _enter.
    session, inner = _demo([asyncio.TimeoutError()])
    with pytest.raises(AppShellTimeoutError):
        async with DemoShell.attach(session):
            pytest.fail("body must not run when launch times out")
    # _enter released the lock itself; _exit (and thus recovery) never ran.
    assert inner._app_shell is None
    assert inner.recovered is False


@pytest.mark.asyncio
async def test_non_timeout_launch_failure_releases_lock():
    # A dead transport during launch is NOT a timeout: the real error must
    # surface and the lock must be released (else run() is wrongly blocked).
    session, inner = _demo([ConnectionError("boom")])
    with pytest.raises(ConnectionError, match="boom"):
        async with DemoShell.attach(session):
            pass
    assert inner._app_shell is None
    assert inner.recovered is False


@pytest.mark.asyncio
async def test_cmd_timeout_marks_broken_then_exit_recovers_without_quit():
    session, inner = _demo(["\nmysql> ", asyncio.TimeoutError()])
    with pytest.raises(AppShellTimeoutError):
        async with DemoShell.attach(session) as shell:
            await shell.cmd("SELECT 1")

    # The cmd line was sent before the timeout; quit_cmd was NOT (shell broken).
    assert "mysql\n" in session.sent
    assert "SELECT 1\n" in session.sent
    assert "quit\n" not in session.sent
    # Broken exit still recovers the POSIX shell and releases the lock.
    assert inner.recovered is True
    assert inner._app_shell is None


# =========================================================================== #
# BaseHost.app_shell() — dedicated session lifecycle (Task 14)
# =========================================================================== #
#
# These tests drive a fake BaseHost whose `open_session` hands back a
# recording HostSession fake (built on the same FakeShellSession used above),
# so `AppShell.attach`'s own launch/quit machinery runs for real while
# `open_session`/`switch_user`/`close` calls are captured for assertion.


class RecordingHostSession(FakeHostSession):
    """FakeHostSession that also records switch_user/close calls (and sends).

    ``calls`` interleaves ``send``/``switch_user`` in real call order so tests
    can assert *ordering* (switch_user before the launch line), not just that
    each happened somewhere.
    """

    def __init__(self, inner, script=None):
        super().__init__(inner, script=script)
        self.calls = []
        self.closed = False

    @override
    async def send(self, text, log=None):
        self.calls.append(("send", text))
        await super().send(text, log=log)

    async def switch_user(self, user):
        self.calls.append(("switch_user", user))

    async def close(self):
        self.calls.append(("close", None))
        self.closed = True


class RecordingHost(BaseHost):
    """Minimal BaseHost whose open_session hands back a fixed recording session."""

    def __init__(self, session):
        self._session = session
        self.opened_names = []

    @override
    async def open_session(self, name):
        self.opened_names.append(name)
        return self._session


def _recording_demo(script=None):
    """Build a (RecordingHost, RecordingHostSession) pair scripted with ``script``."""
    inner = FakeShellSession()
    session = RecordingHostSession(inner, script=script or ["\nmysql> "])
    host = RecordingHost(session)
    return host, session


@pytest.mark.asyncio
async def test_app_shell_yields_attached_shell_on_fresh_session_and_closes():
    host, session = _recording_demo()
    async with host.app_shell(DemoShell) as shell:
        assert isinstance(shell, DemoShell)
        # The shell is genuinely attached: the underlying lock names it.
        assert session._session._app_shell is shell

    # A single, collision-safe session name was opened for this shell class.
    assert len(host.opened_names) == 1
    name = host.opened_names[0]
    assert name.startswith("__appshell_demoshell_")
    assert name.endswith("__")
    # app_shell always closes the session it opened, on top of attach's own
    # quit/recover cleanup.
    assert session.closed is True


@pytest.mark.asyncio
async def test_app_shell_switch_user_runs_before_launch():
    host, session = _recording_demo()
    async with host.app_shell(DemoShell, user="mysql"):
        pass
    # Call ORDER matters: switch_user must land before the launch line is sent.
    assert session.calls == [
        ("switch_user", "mysql"),
        ("send", "mysql\n"),
        ("send", "quit\n"),
        ("close", None),
    ]


@pytest.mark.asyncio
async def test_app_shell_uses_shell_cls_user_default_when_user_kwarg_omitted():
    class UserShell(DemoShell):
        user = "dbadmin"

    host, session = _recording_demo()
    async with host.app_shell(UserShell):
        pass
    assert session.calls[0] == ("switch_user", "dbadmin")


@pytest.mark.asyncio
async def test_app_shell_no_switch_user_when_target_is_none():
    host, session = _recording_demo()
    async with host.app_shell(DemoShell):
        pass
    # DemoShell.user is None and no user= override was given: switch_user must
    # never be called; only the launch/quit sends (and the final close) happen.
    assert session.calls == [("send", "mysql\n"), ("send", "quit\n"), ("close", None)]


@pytest.mark.asyncio
async def test_app_shell_closes_session_even_if_attach_raises():
    host, session = _recording_demo([asyncio.TimeoutError()])
    with pytest.raises(AppShellTimeoutError):
        async with host.app_shell(DemoShell):
            pytest.fail("body must not run when launch times out")
    assert session.closed is True


# =========================================================================== #
# per-session timeout override — attach(timeout=)/app_shell(timeout=) (Task 18)
# =========================================================================== #
#
# Three-tier timeout model: cmd_timeout (ClassVar default) < attach/app_shell
# timeout= (per-session default) < cmd(timeout=) (per-command override, wins).
# These tests assert the actual timeout VALUE that reached ``expect``, not just
# that a call happened.


@pytest.mark.asyncio
async def test_attach_timeout_overrides_launch_expect():
    session, _inner = _demo(["\nmysql> "])
    async with DemoShell.attach(session, timeout=90.0):
        pass
    # The launch `expect` (first call) used the session override, not cmd_timeout.
    assert session.expected[0] == (DemoShell.prompt, 90.0)


@pytest.mark.asyncio
async def test_attach_timeout_reported_in_launch_timeout_message():
    # A launch-prompt timeout under a session override must report the
    # session's timeout value in the error message, not the ClassVar default.
    session, _inner = _demo([asyncio.TimeoutError()])
    with pytest.raises(AppShellTimeoutError, match=r"within 90\.0s of launch"):
        async with DemoShell.attach(session, timeout=90.0):
            pytest.fail("body must not run when launch times out")


@pytest.mark.asyncio
async def test_host_app_shell_timeout_threads_through_to_attach():
    host, session = _recording_demo()
    async with host.app_shell(DemoShell, timeout=45.0):
        pass
    assert session.expected[0] == (DemoShell.prompt, 45.0)


@pytest.mark.asyncio
async def test_cmd_uses_session_timeout_but_per_command_wins():
    session, _inner = _demo(["\nmysql> ", "1\nmysql> ", "1\nmysql> "])
    async with DemoShell.attach(session, timeout=50.0) as shell:
        await shell.cmd("SELECT 1")
        # No per-command timeout -> falls back to the session default (50.0).
        assert session.expected[-1] == (DemoShell.prompt, 50.0)
        await shell.cmd("SELECT 1", timeout=5.0)
        # Per-command timeout still wins over the session default.
        assert session.expected[-1] == (DemoShell.prompt, 5.0)


@pytest.mark.asyncio
async def test_cmd_default_unchanged_without_session_override():
    # Regression guard: with no session timeout and no per-command timeout,
    # the ClassVar cmd_timeout (30.0) still governs both launch and cmd.
    session, _inner = _demo(["\nmysql> ", "1\nmysql> "])
    async with DemoShell.attach(session) as shell:
        await shell.cmd("SELECT 1")
    assert session.expected[0] == (DemoShell.prompt, 30.0)
    assert session.expected[1] == (DemoShell.prompt, 30.0)
