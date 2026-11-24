"""The netcat ladders, declared once in ``nc_scripts`` and run by ``nc``.

The probes run for real under ``/bin/sh`` with ``PATH`` scoped to a stub
directory; the rung runners are driven through a fake runner.
"""

import ast
import itertools
import re
import subprocess
from pathlib import Path

import pytest

from otto.host.errors import CommandValueError, HostCommandError, HostUnreachableError
from otto.host.transfer import nc as nc_module
from otto.host.transfer.nc import NcRungToolError, available_rungs, run_listener_rung, run_port_rung
from otto.host.transfer.nc_scripts import (
    LISTENER_LADDER,
    PORT_LADDER,
    TOOL_FAILED,
    availability_probe,
    presence_probe,
)
from otto.result import CommandResult
from otto.utils import Status

# The probe `prepare` ran before the ladders were declared in one table,
# kept verbatim as the reference the derived probe must answer like.
_OLD_STRATEGY_PROBE = (
    "port=proc; listener=proc; "
    "if command -v ss >/dev/null 2>&1; then port=ss; listener=ss; "
    "elif command -v netstat >/dev/null 2>&1; then port=netstat; listener=netstat; "
    "elif command -v python >/dev/null 2>&1 || command -v python3 >/dev/null 2>&1; "
    "then port=python; "
    "fi; "
    'echo "$port $listener"'
)

_TOOLS = ("ss", "netstat", "python", "python3")
_COMBINATIONS = [
    frozenset(t for t, on in zip(_TOOLS, bits, strict=True) if on)
    for bits in itertools.product((False, True), repeat=len(_TOOLS))
]
_PORT_RUNGS = {r.name: r for r in PORT_LADDER}


def _ok(value: str = "") -> CommandResult:
    return CommandResult(command="", value=value, status=Status.Success, retcode=0)


def _exit(retcode: int, value: str = "") -> CommandResult:
    return CommandResult(command="", value=value, status=Status.Failed, retcode=retcode)


def _timed_out() -> CommandResult:
    return CommandResult(command="", value="", status=Status.Failed, retcode=-1, timed_out=True)


def _stub_path(tmp_path: Path, present: frozenset[str]) -> Path:
    d = tmp_path / "bin"
    d.mkdir()
    for name in present:
        tool = d / name
        tool.write_text("#!/bin/sh\nexit 0\n")
        tool.chmod(0o755)
    return d


def _sh(script: str, path: Path) -> str:
    done = subprocess.run(
        ["/bin/sh", "-c", script],
        env={"PATH": str(path)},
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout


class _Runner:
    """A runner that answers each script in turn and records what it ran."""

    def __init__(self, *answers: CommandResult) -> None:
        self.answers = list(answers)
        self.ran: list[str] = []

    async def __call__(self, script: str) -> CommandResult:
        self.ran.append(script)
        return self.answers.pop(0)


def test_the_ladders_name_the_rungs_in_order() -> None:
    assert [r.name for r in PORT_LADDER] == ["ss", "netstat", "python", "proc"]
    assert [r.name for r in LISTENER_LADDER] == ["ss", "netstat", "proc"]


@pytest.mark.parametrize("present", _COMBINATIONS, ids=lambda p: "+".join(sorted(p)) or "none")
def test_the_presence_probe_answers_as_the_old_probe_did(
    tmp_path: Path, present: frozenset[str]
) -> None:
    path = _stub_path(tmp_path, present)
    assert _sh(presence_probe(), path) == _sh(_OLD_STRATEGY_PROBE, path)


_LISTENER_RUNGS = {r.name: r for r in LISTENER_LADDER}


@pytest.mark.parametrize(
    ("port_order", "listener_order", "answer"),
    [
        (("netstat", "ss", "python", "proc"), ("ss", "netstat", "proc"), "netstat ss\n"),
        (("ss", "netstat", "python", "proc"), ("netstat", "ss", "proc"), "ss netstat\n"),
    ],
    ids=["port-reordered", "listener-reordered"],
)
def test_each_ladders_own_order_picks_its_strategy(
    tmp_path: Path, port_order: tuple[str, ...], listener_order: tuple[str, ...], answer: str
) -> None:
    """With ss and netstat both present, each ladder's own first present rung wins."""
    path = _stub_path(tmp_path, frozenset({"ss", "netstat"}))
    probe = presence_probe(
        port_ladder=tuple(_PORT_RUNGS[n] for n in port_order),
        listener_ladder=tuple(_LISTENER_RUNGS[n] for n in listener_order),
    )
    assert _sh(probe, path) == answer


@pytest.mark.parametrize("proc_readable", [False, True])
@pytest.mark.parametrize("present", _COMBINATIONS, ids=lambda p: "+".join(sorted(p)) or "none")
def test_the_availability_probe_names_exactly_the_present_rungs(
    tmp_path: Path, present: frozenset[str], proc_readable: bool
) -> None:
    path = _stub_path(tmp_path, present)
    proc_net = tmp_path / "net"
    proc_net.mkdir()
    if proc_readable:
        (proc_net / "tcp").write_text("")
    expected = {t for t in ("ss", "netstat") if t in present}
    if present & {"python", "python3"}:
        expected.add("python")
    if proc_readable:
        expected.add("proc")
    answer = _sh(availability_probe(proc_net=str(proc_net)), path)
    assert set(answer.split()) == expected
    assert len(answer.split()) == len(expected)


_RUNG_NAMES = frozenset({"ss", "netstat", "python", "proc"})
_TRANSFER_DIR = Path(nc_module.__file__).parent
_ALIAS_SITES = {("base.py", "NcPortStrategy"), ("base.py", "NcListenerCheck")}


def _rung_names_in(node: ast.AST) -> int:
    """How many rung-name string constants *node*'s own elements, keys or patterns hold.

    The shapes read: a tuple, list or set display; a subscript (a ``Literal``);
    a dict display's keys; and a ``match`` statement's ``case`` value patterns,
    over all its cases.
    """
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        elements: list[ast.AST | None] = list(node.elts)
    elif isinstance(node, ast.Subscript):
        index = node.slice
        elements = list(index.elts) if isinstance(index, ast.Tuple) else [index]
    elif isinstance(node, ast.Dict):
        elements = list(node.keys)
    elif isinstance(node, ast.Match):
        elements = [
            pattern.value
            for case in node.cases
            for pattern in ast.walk(case.pattern)
            if isinstance(pattern, ast.MatchValue)
        ]
    else:
        return 0
    return sum(isinstance(e, ast.Constant) and e.value in _RUNG_NAMES for e in elements)


def _where_the_rungs_are_spelt(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    allowed: set[int] = set()
    for stmt in ast.walk(tree):
        if (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
            and (path.name, stmt.targets[0].id) in _ALIAS_SITES
        ):
            allowed.update(id(n) for n in ast.walk(stmt.value))
    return [
        f"{path.name}:{node.lineno}"
        for node in ast.walk(tree)
        if id(node) not in allowed and _rung_names_in(node) >= 2
    ]


def test_the_order_is_spelt_once_in_src() -> None:
    offending = [
        site
        for path in (
            _TRANSFER_DIR / "nc.py",
            _TRANSFER_DIR / "base.py",
            _TRANSFER_DIR.parent / "options.py",
            _TRANSFER_DIR.parent / "survey" / "engine.py",
            _TRANSFER_DIR.parent / "survey" / "report.py",
        )
        for site in _where_the_rungs_are_spelt(path)
    ]
    assert offending == []


@pytest.mark.asyncio
async def test_available_rungs_reads_the_names_the_probe_printed() -> None:
    run = _Runner(_ok("ss python proc\n"))
    assert await available_rungs(run) == frozenset({"ss", "python", "proc"})
    assert run.ran == [availability_probe()]


@pytest.mark.parametrize("rung", ["ss", "netstat", "proc"])
@pytest.mark.asyncio
async def test_run_port_rung_runs_one_script_for_a_working_rung(rung: str) -> None:
    run = _Runner(_ok("40001\n"))
    port = await run_port_rung(_PORT_RUNGS[rung], run, base_port=40000, reserved="")
    assert port == 40001
    assert run.ran == list(_PORT_RUNGS[rung].scripts(40000, ""))
    assert len(run.ran) == 1


@pytest.mark.parametrize("first", [_exit(127), _timed_out()], ids=["non-zero", "timeout"])
@pytest.mark.asyncio
async def test_the_python_rung_tries_python3_after_a_non_zero_exit_and_after_a_timeout(
    first: CommandResult,
) -> None:
    run = _Runner(first, _ok("40007\n"))
    port = await run_port_rung(_PORT_RUNGS["python"], run, base_port=40000, reserved="")
    assert port == 40007
    assert [s.split()[0] for s in run.ran] == ["python", "python3"]


@pytest.mark.asyncio
async def test_the_python_rung_does_not_try_python3_after_output_that_is_not_a_port() -> None:
    run = _Runner(_ok("hello\n"))
    with pytest.raises(CommandValueError):
        await run_port_rung(_PORT_RUNGS["python"], run, base_port=40000, reserved="")
    assert len(run.ran) == 1


@pytest.mark.parametrize(
    ("rung", "text"),
    [
        ("ss", "ss port scan failed"),
        ("netstat", "netstat port scan failed"),
        ("python", "python port discovery failed"),
        ("proc", "/proc/net/tcp port scan failed"),
    ],
)
@pytest.mark.asyncio
async def test_a_failed_rung_keeps_its_message(rung: str, text: str) -> None:
    answers = [_exit(1, "boom")] * len(_PORT_RUNGS[rung].scripts(40000, ""))
    with pytest.raises(HostCommandError, match=re.escape(text)) as caught:
        await run_port_rung(_PORT_RUNGS[rung], _Runner(*answers), base_port=40000, reserved="")
    assert not isinstance(caught.value, HostUnreachableError)
    assert _PORT_RUNGS[rung].failed == text


@pytest.mark.asyncio
async def test_the_python_rung_names_python3_when_both_fail() -> None:
    with pytest.raises(HostUnreachableError, match="python3 timed out"):
        await run_port_rung(
            _PORT_RUNGS["python"], _Runner(_exit(1), _timed_out()), base_port=1, reserved=""
        )
    with pytest.raises(HostCommandError, match=r"python3 printed nothing \(exit status 2\)"):
        await run_port_rung(
            _PORT_RUNGS["python"], _Runner(_exit(1), _exit(2)), base_port=1, reserved=""
        )


@pytest.mark.asyncio
async def test_a_scan_rung_that_printed_the_sentinel_is_a_tool_failure() -> None:
    with pytest.raises(NcRungToolError, match="ss"):
        await run_port_rung(
            _PORT_RUNGS["ss"], _Runner(_exit(3, TOOL_FAILED)), base_port=1, reserved=""
        )


@pytest.mark.asyncio
async def test_run_listener_rung_reads_a_count_and_no_answer_is_none() -> None:
    rung = LISTENER_LADDER[0]
    run = _Runner(_ok("1\n"), _timed_out(), _ok(""), _exit(3, TOOL_FAILED), _ok("x\n"))
    assert await run_listener_rung(rung, run, 8080) == 1
    assert await run_listener_rung(rung, run, 8080) is None
    assert await run_listener_rung(rung, run, 8080) is None
    with pytest.raises(NcRungToolError):
        await run_listener_rung(rung, run, 8080)
    with pytest.raises(CommandValueError):
        await run_listener_rung(rung, run, 8080)
    assert run.ran == [rung.script(8080)] * 5
