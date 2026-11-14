"""A command's output comes back exactly, on every term and both routes (#711, #713).

``CommandResult.value`` is the command's output with exactly one trailing
newline removed: blank lines and the program's own carriage returns kept,
no telnet NULs, never stripped and never padded. Two different kinds of
defect broke it, so the ssh ``exec`` cell runs each command 20 times:

* timing: asyncssh's exec channel handed back an empty partial line when
  end-of-file landed after the last line (#711), so a single run proves
  nothing;
* translation: the shell route trimmed blank lines, deleted every ``\\r`` and
  leaked telnet's ``CR NUL`` (#713). The session now undoes exactly what the
  telnet protocol and the terminal's ``onlcr`` added, measured at its
  handshake (``terminal_mapping``).

Each cell also checks the host's output log never gains an empty ``@host >``
line the command did not print.

The module covers ``ssh`` and ``telnet`` (UnixHost test1) as well as
``console`` (test2's serial getty), plus one ssh ``exec`` cell that takes the
pooled shell route (a host with a session-setup hook). It lives in
``tests/e2e/console`` because the console cell needs this directory's
``test2_console`` guard (the lease on test2 and the proof, after the test,
that the line is back at ``login:``), and one module over all the terms
keeps the contract in one place.
"""

import logging
from collections.abc import Iterator

import pytest
from rich.markup import escape

from otto.host.exec_route import ExecRoute
from otto.host.host import Host
from otto.host.session_setup import SessionSetup
from otto.result import CommandResult
from otto.testing import EXACT_OUTPUT_CASES, OutputCase
from tests._fixtures.labdata import make_host
from tests._fixtures.session_setup_hooks import noop_session_setup

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    # The slowest cells are the ssh channel cell (every command 20 times) and
    # the console cell, whose 100 KB single-line case alone takes about 9 s
    # at 115200 baud, plus the guard's login-prompt check after the test.
    pytest.mark.timeout(75),
]

_REPEATS = 20
"""Runs per command on the SSH exec channel: enough for a 1-in-20 timing flake to show."""


def blank_lines(case: OutputCase) -> int:
    """Blank lines the command really prints: the most empty log lines it may add."""
    if case.writes == b"":
        return 0
    return sum(1 for line in case.value.split("\n") if line == "")


def _repeats(host: Host, route: str) -> int:
    """Runs per command: only the SSH exec channel carries the #711 timing defect."""
    return (
        _REPEATS
        if route == "exec"
        and host._exec_plan(user=None, needs_shell=False).route is ExecRoute.SSH_CHANNEL
        else 1
    )


_NOOP_HOOK = "exact-output-noop"
"""The name prefix of a session-setup hook that does nothing. Declaring ANY hook sends ``exec``
to the pooled shell route, which is the route this hook is here to take."""


@pytest.fixture
def _console_guard(request: pytest.FixtureRequest) -> None:
    """Take the ``test2_console`` guard for the console cell only.

    A ``usefixtures`` fixture is set up before the test's arguments, so the
    guard is set up before ``host1`` and torn down after it: its login-prompt
    check must run after the host has logged out.
    """
    if request.node.callspec.params["host1"] == "console":
        request.getfixturevalue("test2_console")


@pytest.fixture
def noop_hook() -> Iterator[str]:
    with noop_session_setup(_NOOP_HOOK) as name:
        yield name


def _empty_output_lines(records: list[logging.LogRecord], host: Host) -> int:
    """Count the empty ``@host >`` output lines among *records*.

    An output record joins one ``[yellow]@name > | `` prefix per line; an
    empty line is the bare prefix inside a multi-line record, or an empty
    message when it was logged alone.
    """
    prefix = f"[yellow]@{escape(host.name)} > | "
    count = 0
    for record in records:
        if getattr(record, "host", None) is not host:
            continue
        message = record.getMessage()
        if message.startswith("[bold]"):
            continue  # the command's own echo line
        if message == "":
            count += 1
            continue
        count += sum(1 for line in message.split("\n") if line == prefix)
    return count


async def _run(host: Host, route: str, cmd: str) -> CommandResult:
    if route == "exec":
        return await host.exec(cmd)
    return (await host.run(cmd)).only


async def _check_every_case(
    host: Host, route: str, cell: str, caplog: pytest.LogCaptureFixture
) -> list[str]:
    failures: list[str] = []
    with caplog.at_level(logging.INFO):
        repeats = _repeats(host, route)
        for case in EXACT_OUTPUT_CASES:
            for iteration in range(1, repeats + 1):
                caplog.clear()
                result = await _run(host, route, case.command)
                where = (
                    f"{cell} route={route} cmd={case.command!r} iteration={iteration}/{repeats} "
                    f"(terminal mapping: {host.terminal_mapping})"
                )
                if result.retcode != case.retcode:
                    failures.append(
                        f"{where}: retcode {result.retcode}, expected {case.retcode}; "
                        f"value={result.value!r}"
                    )
                if result.value != case.value:
                    failures.append(f"{where}: value={result.value!r}, expected {case.value!r}")
                empties = _empty_output_lines(caplog.records, host)
                if empties > blank_lines(case):
                    failures.append(
                        f"{where}: logged {empties} empty output line(s), "
                        f"the command prints {blank_lines(case)}; value={result.value!r}"
                    )
    return failures


def _report(failures: list[str]) -> str:
    shown = failures[:60]
    more = f"\n... and {len(failures) - len(shown)} more" if len(failures) > len(shown) else ""
    return f"{len(failures)} mismatch(es):\n" + "\n".join(shown) + more


@pytest.mark.parametrize("route", ["exec", "run"])
@pytest.mark.parametrize(
    "host1",
    [
        "ssh",
        "telnet",
        # One worker with the other console tests: they drive the same single-client line.
        pytest.param("console", marks=pytest.mark.xdist_group("console_e2e")),
    ],
    indirect=True,
)
# The guard comes first, so it is set up before host1 and torn down after it.
@pytest.mark.usefixtures("_console_guard")
async def test_value_is_the_exact_output_every_time(
    host1: Host,
    route: str,
    request: pytest.FixtureRequest,
    caplog: pytest.LogCaptureFixture,
) -> None:
    term = request.node.callspec.params["host1"]
    failures = await _check_every_case(host1, route, f"term={term}", caplog)
    assert not failures, _report(failures)


async def test_pooled_exec_over_ssh_is_exact_too(
    noop_hook: str, caplog: pytest.LogCaptureFixture
) -> None:
    """``exec`` on a hooked ssh host runs on a pooled shell session, not the exec channel."""
    host = make_host("test1", term="ssh", session_setup=SessionSetup(name=noop_hook))
    try:
        assert host._exec_plan(user=None, needs_shell=False).route is ExecRoute.POOLED_SHELL
        failures = await _check_every_case(host, "exec", "term=ssh pooled", caplog)
    finally:
        await host.close()
    assert not failures, _report(failures)
