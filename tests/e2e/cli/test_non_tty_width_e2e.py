"""The width otto renders at with no terminal attached, through the real binary (#785).

Every run goes through ``run_otto``, whose environment carries no ``COLUMNS``,
``LINES``, ``TERM``, ``CI`` or ``BUILD_NUMBER``, so the width under test is the
one otto chooses. ANSI is stripped before anything is measured.
"""

import contextlib
import json
import os
import select
import subprocess
import sys
from pathlib import Path

import pytest

from otto.host.console import strip_ansi
from tests._fixtures.controlling_pty import set_winsize
from tests._fixtures.literal_text import rejoin_column
from tests._fixtures.paths import CHECKOUT_ROOT
from tests._fixtures.project_scaffold import make_project
from tests.e2e._otto_subprocess import otto_subprocess_env, run_otto

pytestmark = pytest.mark.hostless

LONG_ARTIFACT = "build/" + "/".join(["segment" + "x" * 23] * 8)[:244]
"""250 ASCII path characters with no spaces: wider than any width it is rendered at."""

_SETTINGS = f"""\
libs = ["pylib"]
init = ["widthprobe_instructions"]

[project]
lab_patterns = [".*"]

[[lab.sources]]
backend = "json"
paths = ["{CHECKOUT_ROOT / "tests" / "_fixtures" / "lab_data" / "tech1"}"]

[[products]]
name = "telemetry_agent_with_a_long_name"
kind = "shell"
artifact = "build/telemetry/agent-release-candidate.tar.gz"
stage_dir = "/opt/telemetry/stage/agent-release-candidate"
match = {{ id = "test1" }}

[[products]]
name = "deep"
kind = "shell"
artifact = "{LONG_ARTIFACT}"
match = {{ id = "test1" }}
"""


_INSTRUCTIONS = """\
import os

from otto.instructions import instruction
from otto.result import CommandResult


@instruction()
async def columns() -> CommandResult:
    \"\"\"Write the COLUMNS a process otto starts sees to $WIDTH_PROBE_OUT.\"\"\"
    from otto.host.local_host import LocalHost

    host = LocalHost()
    try:
        result = (await host.run("printenv COLUMNS")).only
    finally:
        await host.close()
    with open(os.environ["WIDTH_PROBE_OUT"], "w") as out:
        out.write(result.value)
    return result
"""

_FAILING_TEST = 'def test_w():\n    assert False, "m" * 150\n'
"""Node id ``tests/test_w.py::test_w``: 23 characters, so pytest's summary line fits in 200."""


@pytest.fixture(scope="module")
def wide_project(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One project for every case here, on the tech1 lab.

    It declares a product whose row is wide and one whose artifact is 250
    characters, a ``columns`` instruction that reports a child's ``COLUMNS``,
    and one failing test with a 150-character message.
    """
    return make_project(
        tmp_path_factory.mktemp("width") / "widthproj",
        tests=["tests"],
        extra=_SETTINGS,
        files={
            "pylib/widthprobe_instructions.py": _INSTRUCTIONS,
            "tests/test_w.py": _FAILING_TEST,
        },
    )


def test_the_fixture_artifact_is_250_characters() -> None:
    assert len(LONG_ARTIFACT) == 250
    assert " " not in LONG_ARTIFACT


def test_an_explicit_columns_is_kept_and_ids_fold(wide_project: Path) -> None:
    """Preservation and regression: ``COLUMNS=60`` renders 60 wide, and ids fold whole."""
    result = run_otto(
        ["--lab", "unix", "--list-products"], roots=wide_project, extra_env={"COLUMNS": "60"}
    )
    out = strip_ansi(result.stdout)
    assert result.returncode == 0, result.stdout + result.stderr
    assert all(len(line.rstrip()) <= 60 for line in out.splitlines()), out
    # The identifier columns (name, variant, project, hosts, artifact, stage dir)
    # fold; the two prose columns keep Rich's default and may still end in "…".
    for index in (0, 2, 4, 5, 6, 7):
        assert "…" not in rejoin_column(out, index), out
    assert LONG_ARTIFACT in rejoin_column(out, 6)


# ── the width default (D1) ───────────────────────────────────────────────────

_SHIM_PROBE = """\
import json
import os
import sys

result_path, argv = sys.argv[1], sys.argv[2:]
events = []


def _record(event, args):
    # Observes only: an audit hook imports nothing and replaces nothing.
    if event == "import" and args[0] in ("otto.console", "rich.console"):
        cli = sys.modules.get("otto.cli.main")
        events.append(
            {
                "module": args[0],
                "columns": os.environ.get("COLUMNS"),
                "cli_imported": hasattr(cli, "entry"),
            }
        )


sys.addaudithook(_record)
if argv[:1] == ["--console-first"]:
    argv = argv[1:]
    import otto.console  # the control: the console is built before the shim runs

import otto._shim

sys.argv = ["otto", *argv]
try:
    otto._shim.main()
except SystemExit:
    pass
console = sys.modules["otto.console"].CONSOLE
with open(result_path, "w") as out:
    json.dump(
        {
            "events": events,
            "stored_width": console._width,
            "width": console.width,
            "columns": os.environ.get("COLUMNS"),
        },
        out,
    )
"""


def _drain_until_exit(masters: "list[int]", proc: subprocess.Popen) -> None:
    """Read every pty master until the child exits, so it never blocks on a full pty."""
    while proc.poll() is None:
        ready, _, _ = select.select(masters, [], [], 0.1)
        for fd in ready:
            with contextlib.suppress(OSError):
                os.read(fd, 65536)


def _probe_shim(
    tmp_path: Path,
    project: Path,
    *,
    console_first: bool = False,
    stdout_pty: "tuple[int, int] | None" = None,
    stderr_pty: "tuple[int, int] | None" = None,
    columns: "str | None" = None,
) -> dict:
    """Run the real shim on ``--list-products`` in a fresh interpreter; return what it saw.

    A pty whose size is ``(0, 0)`` is left as a fresh pty reports itself.
    """
    probe = tmp_path / "probe.py"
    probe.write_text(_SHIM_PROBE)
    result = tmp_path / "result.json"
    argv = [sys.executable, str(probe), str(result)]
    argv += ["--console-first"] if console_first else []
    argv += ["--list-products"]
    env = otto_subprocess_env(roots=project)
    if columns is not None:
        env["COLUMNS"] = columns
    masters, slaves = [], []
    streams: dict = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE}
    for name, size in (("stdout", stdout_pty), ("stderr", stderr_pty)):
        if size is None:
            continue
        master, slave = os.openpty()
        if size != (0, 0):
            set_winsize(slave, *size)
        masters.append(master)
        slaves.append(slave)
        streams[name] = slave
    try:
        proc = subprocess.Popen(
            argv, env=env, cwd=str(project), stdin=subprocess.DEVNULL, **streams
        )
    finally:
        for slave in slaves:
            os.close(slave)
    try:
        if masters:
            _drain_until_exit(masters, proc)
        proc.communicate(timeout=120)
    finally:
        for master in masters:
            os.close(master)
    seen = json.loads(result.read_text())
    consoles = [e for e in seen["events"] if e["module"] == "otto.console"]
    assert len(consoles) == 1, f"the verb must build otto's console exactly once: {seen}"
    seen["console_event"] = consoles[0]
    return seen


_SHIM_ROWS = {
    "no-terminal": ({}, {"stored_width": 200, "event_columns": "200"}),
    "zero-stdout-100-stderr": (
        {"stdout_pty": (0, 0), "stderr_pty": (100, 30)},
        {"stored_width": 100, "event_columns": "100"},
    ),
    # The shim sets nothing for a stdout terminal; otto.console's size pin
    # stores the terminal's width, so a later non-tty fd 1 cannot shrink it.
    "stdout-tty-no-columns": (
        {"stdout_pty": (120, 30)},
        {"stored_width": 120, "event_columns": None},
    ),
    "explicit-columns-beats-a-stdout-tty": (
        {"stdout_pty": (120, 30), "columns": "60"},
        {"stored_width": 60, "event_columns": "60"},
    ),
    # The control: a console built before the shim misses the default even
    # though COLUMNS is 200 by the end. It proves the observable tells
    # set-before-construction from set-after, which `.width == 200` cannot.
    "control-console-first": (
        {"console_first": True},
        {"stored_width": None, "event_columns": None, "final_columns": "200"},
    ),
}


@pytest.mark.parametrize("row", list(_SHIM_ROWS))
def test_the_console_is_built_at_the_shims_width(tmp_path, wide_project, row) -> None:
    """Spec test 2: the width default is in place before the first console is constructed.

    Read from what ``Console.__init__`` stored (``_width``), never ``.width``: a
    console built with no ``COLUMNS`` re-reads the environment on every
    ``.width``, so a value set after construction would still report 200.
    """
    kwargs, expected = _SHIM_ROWS[row]
    seen = _probe_shim(tmp_path, wide_project, **kwargs)
    assert seen["console_event"]["columns"] == expected["event_columns"], seen
    assert seen["stored_width"] == expected["stored_width"], seen
    if "final_columns" in expected:
        assert seen["columns"] == expected["final_columns"], seen


def test_list_products_fits_at_200_with_no_terminal(wide_project: Path) -> None:
    """Spec test 3: with no terminal and no ``COLUMNS``, the listing renders 200 wide."""
    result = run_otto(["--lab", "unix", "--list-products"], roots=wide_project)
    out = strip_ansi(result.stdout)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "…" not in out
    [row] = [line for line in out.splitlines() if "telemetry_agent_with_a_long_name" in line]
    assert "build/telemetry/agent-release-candidate.tar.gz" in row, out
    assert "/opt/telemetry/stage/agent-release-candidate" in row, out
    assert 80 < len(row.rstrip()) <= 200
    assert LONG_ARTIFACT in rejoin_column(out, 6)


def test_root_help_names_every_option_whole() -> None:
    """Spec test 4: Typer's help no longer cuts option names to ``--include-projec…``."""
    result = run_otto(["--help"])
    out = strip_ansi(result.stdout)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "--include-projects" in out
    assert "--no-rich-log-file" in out


def test_otto_test_summary_holds_the_whole_message(tmp_path: Path, wide_project: Path) -> None:
    """Spec test 6: pytest's short summary keeps a 150-character failure message.

    pytest trims ``FAILED <nodeid> - <msg>`` to the terminal width unless it
    fits, so the fixture's node id is kept short enough for the line to fit in
    200; the first assertion checks the fixture, not the product.
    """
    result = run_otto(
        ["-R", "test", "test_w"],
        xdir=tmp_path,
        roots=wide_project,
        cwd=wide_project,
        extra_env={"OTTO_LAB": "unix"},
    )
    out = strip_ansi(result.stdout)
    [line] = [ln for ln in out.splitlines() if ln.startswith("FAILED ")]
    nodeid = line[len("FAILED ") :].split(" - ", 1)[0]
    budget = len("FAILED " + nodeid + " - AssertionError: ") + 150
    assert budget <= 200, f"the fixture overran: {line!r} needs {budget} columns"
    assert 80 < len(line) <= 200, line
    assert "m" * 150 in line, line


def test_children_inherit_the_columns_default(tmp_path: Path, wide_project: Path) -> None:
    """Spec test 8: a process otto starts sees the shim's ``COLUMNS``, as under an export."""
    probe_out = tmp_path / "columns.txt"
    result = run_otto(
        ["-R", "run", "columns"],
        xdir=tmp_path,
        roots=wide_project,
        extra_env={"OTTO_LAB": "unix", "WIDTH_PROBE_OUT": str(probe_out)},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert probe_out.read_text().strip() == "200"
