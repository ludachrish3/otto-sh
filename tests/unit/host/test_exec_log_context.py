"""The per-call log context a delegating host hands its parent's exec.

``ExecLogContext`` names whose log one delegated ``exec`` call's lines go to
and under which mode. It rides on the call's ``ExecInput`` through
``BaseHost._exec_delegated``; the parent logs the wrapped line it really runs
once, at DEBUG, under the delegated mode.
"""

import asyncio
import dataclasses
import logging
from unittest.mock import AsyncMock

import pytest

from otto.host.command_frame import ExecInput
from otto.host.element import Element
from otto.host.exec_route import ExecRoute
from otto.host.local_host import LocalHost
from otto.host.login_proxy import Cred
from otto.host.options import ConsoleOptions
from otto.host.session import resolve_exec_log
from otto.host.unix_host import UnixHost
from otto.logger.mode import ExecLogContext, LogMode
from otto.utils import wait_for_async
from tests.conformance._log import _watching_records, output_record, records_containing
from tests.unit.host._exec_log import (
    SU_LOGIN,
    all_records,
    close_ssh_parent,
    records_of,
    ssh_parent_over_shells,
)
from tests.unit.host.conftest import unix_host_over_scripted
from tests.unit.host.test_session_exec_pooled import (
    _fast_resync_settle,  # noqa: F401 — the fixture, used by name
    _gone_mgr,
)


def _ctx(mode: LogMode, sink: list) -> ExecLogContext:
    return ExecLogContext(
        command="echo shown",
        mode=mode,
        log_command=lambda text, m: sink.append(("cmd", text, m)),
        log_output=lambda text, m: sink.append(("out", text, m)),
    )


def test_context_is_immutable_and_defaults_off() -> None:
    ctx = _ctx(LogMode.NORMAL, [])
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.mode = LogMode.NEVER  # ty: ignore[invalid-assignment]
    assert ExecInput(stdin_eof=True).exec_log is None


@pytest.mark.asyncio
async def test_delegated_entry_reaches_run_plan_on_exec_input(monkeypatch) -> None:
    parent = LocalHost()
    seen: list = []
    original = parent._run_plan

    async def spy(plan, **kwargs):  # today's signature, by keyword
        seen.append(kwargs["exec_input"].exec_log)
        return await original(plan, **kwargs)

    monkeypatch.setattr(parent, "_run_plan", spy)
    ctx = _ctx(LogMode.NORMAL, [])
    await parent._exec_delegated("printf 'a\\n'", exec_log=ctx, expects=None, timeout=10.0)
    await parent.exec("true")
    assert seen == [ctx, None]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected"),
    [(LogMode.NORMAL, 1), (LogMode.QUIET, 1), (LogMode.NEVER, 0)],
)
async def test_wrapper_line_once_on_the_parent_at_debug(mode: LogMode, expected: int) -> None:
    parent = LocalHost()
    wrapped = "printf 'a\\n'"
    records: list[logging.LogRecord] = []
    with _watching_records(records.append, level=logging.DEBUG):
        await parent._exec_delegated(wrapped, exec_log=_ctx(mode, []), expects=None, timeout=10.0)
    wrappers = [
        r
        for r in records
        if r.levelno == logging.DEBUG
        and getattr(r, "host", None) is parent
        and getattr(r, "relayed", None) == wrapped
    ]
    assert [getattr(r, "log_mode", None) for r in wrappers] == [mode] * expected


# ---------------------------------------------------------------------------
# One log resolution, every route
# ---------------------------------------------------------------------------

_OPPOSED = [(LogMode.NEVER, LogMode.NORMAL), (LogMode.NORMAL, LogMode.NEVER)]
"""(the call's own ``log``, the context's mode): the context must win either way."""

_ISSUED = "issued-by-the-container"
"""The command text a context names: what the caller of the delegating host issued."""


def _recording_ctx(container: LocalHost, mode: LogMode) -> ExecLogContext:
    """A context whose sinks are *container*'s own, so its lines are records tagged with it."""
    return ExecLogContext(
        command=_ISSUED,
        mode=mode,
        log_command=container._log_command,
        log_output=container._log_output,
    )


def _assert_logged_as_context(
    records: "list[logging.LogRecord]",
    *,
    parent: object,
    container: LocalHost,
    ctx_mode: LogMode,
    wire: str,
    out_token: str,
) -> None:
    """The container holds the issued command and the output; the parent only the DEBUG wrapper."""
    mine = records_of(records, container)
    outputs = [found for r in mine if (found := output_record(r, container)) is not None]
    commands = [r for r in mine if output_record(r, container) is None]
    parent_records = [(r.levelno, getattr(r, "relayed", None)) for r in records_of(records, parent)]
    if ctx_mode is LogMode.NEVER:
        assert mine == [], [r.getMessage() for r in mine]
        assert parent_records == []
        return
    assert [(r.levelno, getattr(r, "relayed", None), r.log_mode) for r in commands] == [
        (logging.INFO, _ISSUED, ctx_mode)
    ]
    assert out_token in "".join(o.text for o in outputs), outputs
    assert {o.log_mode for o in outputs} == {ctx_mode}
    assert parent_records == [(logging.DEBUG, wire)], "the parent keeps only the wrapper"


def test_the_resolver_answers_the_route_s_own_for_an_ordinary_call() -> None:
    def cmd_sink(_t: str, _m: LogMode) -> None: ...

    def out_sink(_t: str, _m: LogMode) -> None: ...

    for mode in LogMode:
        for exec_input in (None, ExecInput(stdin_eof=True)):
            xl = resolve_exec_log(
                cmd="c", mode=mode, log_command=cmd_sink, log_output=out_sink, exec_input=exec_input
            )
            assert (
                xl.context.command,
                xl.context.log_command,
                xl.context.log_output,
                xl.context.mode,
                xl.redact,
            ) == (
                "c",
                cmd_sink,
                out_sink,
                mode,
                mode is LogMode.NEVER,
            )


def test_the_resolver_answers_the_context_s_for_a_delegated_call() -> None:
    def own(_t: str, _m: LogMode) -> None: ...

    for mode in LogMode:
        for ctx_mode in LogMode:
            ctx = _ctx(ctx_mode, [])
            xl = resolve_exec_log(
                cmd="wire",
                mode=mode,
                log_command=own,
                log_output=own,
                exec_input=ExecInput(stdin_eof=True, exec_log=ctx),
            )
            assert (
                xl.context.command,
                xl.context.log_command,
                xl.context.log_output,
                xl.context.mode,
                xl.redact,
            ) == (
                ctx.command,
                ctx.log_command,
                ctx.log_output,
                ctx_mode,
                ctx_mode is LogMode.NEVER,
            )


_HELD = 'echo outtok-a; while [ ! -e "$HOME/release" ]; do sleep 0.05; done; echo outtok-b'
"""A ``sh`` body that prints one line, then the second once the test creates ``$HOME/release``."""


@pytest.mark.asyncio
@pytest.mark.parametrize(("log", "ctx_mode"), _OPPOSED)
@pytest.mark.parametrize("as_user", [False, True], ids=["login", "direct-user"])
async def test_an_ssh_exec_logs_as_the_context_says_live(
    log: LogMode, ctx_mode: LogMode, as_user: bool, tmp_path
) -> None:
    from tests.unit.host.test_terminal_switch import _EitherConnection

    parent, dial = ssh_parent_over_shells(
        UnixHost,
        tmp_path,
        {"wire-ssh": _HELD},
        creds=[Cred(login="u", password="p"), Cred(login="postgres", password="pw")],
    )
    own = _EitherConnection(tmp_path)
    ssh_as = AsyncMock(return_value=own)
    parent._connections.ssh_as = ssh_as  # ty: ignore[invalid-assignment]
    user = "postgres" if as_user else None
    container = LocalHost()
    first_line = asyncio.Event()

    def line_seen(record: logging.LogRecord) -> None:
        if "outtok-a" in str(getattr(record, "relayed", "")):
            first_line.set()

    ctx = _recording_ctx(container, ctx_mode)
    try:
        # The session is opened first, so the records below are the call's alone.
        await parent.exec("true", user=user, log=LogMode.NEVER)
        with all_records() as records, _watching_records(line_seen, level=logging.DEBUG):
            call = asyncio.create_task(
                parent._exec_one(
                    "wire-ssh",
                    10.0,
                    log=log,
                    user=user,
                    exec_input=ExecInput(stdin_eof=True, exec_log=ctx),
                )
            )
            try:
                if ctx_mode is not LogMode.NEVER:
                    # "a" is relayed while "b" is still held back: the lines are live.
                    await wait_for_async(
                        first_line.is_set,
                        timeout=10,
                        interval=0.01,
                        on_timeout="no line was relayed",
                    )
                    assert not call.done()
                    assert [
                        o.text
                        for r in records_of(records, container)
                        if (o := output_record(r, container))
                    ]
                    assert "outtok-b" not in "".join(
                        str(getattr(r, "relayed", "")) for r in records
                    )
                (tmp_path / "release").touch()
                result = await call
            finally:
                # A failed check above must not leave the call running.
                (tmp_path / "release").touch()
                call.cancel()
                await asyncio.gather(call, return_exceptions=True)
    finally:
        await close_ssh_parent(parent, dial)
        await own.reap()
    assert (result.route, result.retcode, result.value) == (
        ExecRoute.POOLED_SHELL.value,
        0,
        "outtok-a\nouttok-b",
    )
    assert ssh_as.await_count == (1 if as_user else 0)
    _assert_logged_as_context(
        records,
        parent=parent,
        container=container,
        ctx_mode=ctx_mode,
        wire="wire-ssh",
        out_token="outtok-b",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("log", "ctx_mode"), _OPPOSED)
@pytest.mark.parametrize(
    ("term", "route"),
    [("telnet", ExecRoute.POOLED_SHELL), ("console", ExecRoute.DEFAULT_SESSION)],
)
async def test_a_shell_session_logs_as_the_context_says(
    log: LogMode, ctx_mode: LogMode, term: str, route: ExecRoute
) -> None:
    wire = f"cmdtok-{term}"
    out = f"outtok-{term}"
    console = (
        {"valid_terms": ["console"], "console_options": ConsoleOptions(server="test1", port=4001)}
        if term == "console"
        else {}
    )
    parent, _ = await unix_host_over_scripted(
        term, elevation="su", creds=SU_LOGIN, replies={wire: (out, 0)}, **console
    )
    parent.log = LogMode.NORMAL
    container = LocalHost()
    ctx = _recording_ctx(container, ctx_mode)
    try:
        with all_records() as records:
            result = await parent._exec_one(
                wire,
                10.0,
                log=log,
                needs_shell=True,
                exec_input=ExecInput(stdin_eof=False, exec_log=ctx),
            )
    finally:
        await parent.close()
    assert (result.route, result.retcode, result.value) == (route.value, 0, out)
    _assert_logged_as_context(
        records, parent=parent, container=container, ctx_mode=ctx_mode, wire=wire, out_token=out
    )
    if ctx_mode is LogMode.NEVER:
        # The real frame's own diagnostics are redacted too: nothing at any level.
        leaked = [
            r.getMessage()
            for r in records
            if wire in f"{r.getMessage()} {getattr(r, 'relayed', '')}"
            or out in f"{r.getMessage()} {getattr(r, 'relayed', '')}"
        ]
        assert leaked == []


@pytest.mark.asyncio
async def test_a_never_context_leaves_no_record_of_command_or_output_on_a_real_frame() -> None:
    parent, _ = await unix_host_over_scripted(
        "telnet", elevation="su", creds=SU_LOGIN, replies={"cmdtok-n": ("outtok-n", 0)}
    )
    parent.log = LogMode.NORMAL
    ctx = _recording_ctx(LocalHost(), LogMode.NEVER)
    try:
        with records_containing("cmdtok-n") as cmd_seen, records_containing("outtok-n") as out_seen:
            result = await parent._exec_one(
                "cmdtok-n",
                10.0,
                log=LogMode.NORMAL,
                needs_shell=True,
                exec_input=ExecInput(stdin_eof=False, exec_log=ctx),
            )
    finally:
        await parent.close()
    assert (result.retcode, result.value) == (0, "outtok-n")
    assert (cmd_seen, out_seen) == ([], [])


@pytest.mark.asyncio
@pytest.mark.parametrize(("log", "ctx_mode"), _OPPOSED)
async def test_a_local_exec_logs_as_the_context_says(log: LogMode, ctx_mode: LogMode) -> None:
    parent = LocalHost()
    parent.log = LogMode.NORMAL
    container = LocalHost()
    wire = "printf 'outtok-local\\n'"
    ctx = _recording_ctx(container, ctx_mode)
    with all_records() as records:
        result = await parent._exec_one(
            wire, 10.0, log=log, exec_input=ExecInput(stdin_eof=True, exec_log=ctx)
        )
    assert (result.route, result.retcode, result.value) == (
        ExecRoute.POOLED_SHELL.value,
        0,
        "outtok-local",
    )
    _assert_logged_as_context(
        records,
        parent=parent,
        container=container,
        ctx_mode=ctx_mode,
        wire=wire,
        out_token="outtok-local",
    )


def _trace(records: "list[logging.LogRecord]", host: object) -> "list[tuple]":
    """*host*'s records as (level, kind, text, mode): what a reader of its log sees."""
    return [
        (
            r.levelno,
            "out" if output_record(r, host) is not None else "cmd",  # ty: ignore[invalid-argument-type]
            getattr(r, "relayed", None),
            getattr(r, "log_mode", None),
        )
        for r in records_of(records, host)
    ]


async def _lost_then_fresh_host() -> "tuple[UnixHost, list]":
    """A recording host whose manager's first pooled shell has lost its user, the next has not."""
    mgr, built = _gone_mgr(True, False)
    host = UnixHost(
        ip="10.0.0.1",
        element=Element(name="box"),
        creds=[Cred(login="admin", password="pw"), Cred(login="alice", password="pw")],
        term="ssh",
        log=LogMode.NORMAL,
    )
    mgr._log_command = host._log_command
    mgr._log_output = host._log_output
    host._session_mgr = mgr
    return host, built


@pytest.mark.asyncio
@pytest.mark.usefixtures("_fast_resync_settle")
async def test_a_pooled_retry_logs_on_the_container_what_an_ordinary_call_logs() -> None:
    ordinary, ordinary_built = await _lost_then_fresh_host()
    parent, built = await _lost_then_fresh_host()
    container = LocalHost()
    ctx = ExecLogContext(
        command=_ISSUED,
        mode=LogMode.NORMAL,
        log_command=container._log_command,
        log_output=container._log_output,
    )
    with all_records() as records:
        plain = await ordinary._exec_one(
            "wire-c", 10.0, user="alice", exec_input=ExecInput(stdin_eof=True)
        )
        delegated = await parent._exec_one(
            "wire-c", 10.0, user="alice", exec_input=ExecInput(stdin_eof=True, exec_log=ctx)
        )
    for shells, result in ((ordinary_built, plain), (built, delegated)):
        gone, fresh = shells
        assert (gone.identity_lost, fresh.run_cmd_calls[-1], result.value) == (
            True,
            "wire-c",
            "OUT",
        )
    # The exec's own records; the opening of each shell (its switch to the
    # user, its probes) is the session's, and stays on the host that runs it.
    expected = [
        (level, kind, _ISSUED if text == "wire-c" else text, mode)
        for level, kind, text, mode in _trace(records, ordinary)
        if text == "wire-c" or (kind, text) == ("out", "OUT")
    ]
    assert [t for _, _, t, _ in expected] == ["issued-by-the-container"] * 2 + ["OUT"], expected
    assert _trace(records, container) == expected
    # Whatever else the parent logs is the opening of its shells; the wire
    # command itself appears there only in the one DEBUG wrapper.
    on_parent = [t for t in _trace(records, parent) if t[2] is not None and "wire-c" in t[2]]
    assert on_parent == [(logging.DEBUG, "cmd", "wire-c", LogMode.NORMAL)]
    assert not [t for t in _trace(records, parent) if t[2] == "OUT"]


_BOUND = 2.0
"""The timed-out call's bound: its command prints its two lines at once, then never ends.

The session is warm, so the bound times the command alone, and is long
beside the two lines' relay.
"""


@pytest.mark.asyncio
async def test_a_timed_out_call_logs_its_lines_once_on_the_container_as_an_ordinary_call(
    tmp_path,
) -> None:
    script = {"wire-t": "echo tl-1; echo tl-2; exec sleep 3600"}
    ordinary, ordinary_dial = ssh_parent_over_shells(UnixHost, tmp_path / "o", script)
    parent, dial = ssh_parent_over_shells(UnixHost, tmp_path / "p", script)
    container = LocalHost()
    ctx = _recording_ctx(container, LogMode.NORMAL)
    try:
        # Each session is opened first, so the bound below times the command alone.
        await ordinary.exec("true", log=LogMode.NEVER)
        await parent.exec("true", log=LogMode.NEVER)
        with all_records() as records:
            plain = await ordinary._exec_one("wire-t", _BOUND, exec_input=ExecInput(stdin_eof=True))
            result = await parent._exec_one(
                "wire-t", _BOUND, exec_input=ExecInput(stdin_eof=True, exec_log=ctx)
            )
    finally:
        await close_ssh_parent(ordinary, ordinary_dial)
        await close_ssh_parent(parent, dial)
    assert (plain.timed_out, result.timed_out) == (True, True)
    assert result.value.startswith("Command timed out after"), result.value
    assert [
        o.text for r in records_of(records, container) if (o := output_record(r, container))
    ] == [
        "tl-1",
        "tl-2",
    ]
    expected = [
        (level, kind, _ISSUED if text == "wire-t" else text, mode)
        for level, kind, text, mode in _trace(records, ordinary)
    ]
    assert _trace(records, container) == expected
    assert _trace(records, parent) == [(logging.DEBUG, "cmd", "wire-t", LogMode.NORMAL)]
    assert not [r for r in records if "timed out after" in r.getMessage()]
