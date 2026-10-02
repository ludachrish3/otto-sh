"""A failure that ends a command is documented in both log files, through the real ``entry()``.

A subprocess, like ``test_entry_cache_rebuild.py``, because the behaviour sits
where no ``CliRunner`` reaches: ``entry()``'s frames around ``app()``, Typer's
excepthook, and the exit of the process with the logging listener still
running. A run that leaves the traceback in the queue at exit fails here,
which an in-process test cannot show.

The repo registers lab-bound commands from its init module. The marker values
live in module constants so that they reach a log file only through a frame's
LOCALS panel, never through the three lines of source Rich prints around each
frame.
"""

import os
import re
import subprocess
import sys

import pytest

from tests._fixtures.labdata import write_lab_json
from tests._fixtures.sutrepo import make_sut_repo

_ENTRY = [sys.executable, "-c", "from otto.cli.main import entry; entry()"]

_RUN_TIMEOUT = 120
"""Seconds one ``otto`` subprocess may take: a wedged exit fails the test, never the lane."""

_INIT = """\
import asyncio
import logging

import typer

from otto.cli.registry import cli_command
from otto.errors import OttoError

MARKUP_MARKER = "[bold]kept[/bold] verbatim"
LIST_MARKER = [1, 2, 3]
DICT_MARKER = {"k": "[red]v[/red]"}
QUIET_MARKER = "a value no clean exit may write"


class Refused(OttoError, ValueError):
    pass


def explode_in_a_command(marker_local):
    raise RuntimeError("the crash message")


def refuse_in_a_command(marker_local):
    raise Refused("the refusal message")


def raise_and_log(marker_local):
    raise KeyError("the logged key")


def cancel_in_a_command(marker_local):
    raise asyncio.CancelledError("the leaked cancellation")


def exit_in_a_command(marker_local):
    raise typer.Exit(3)


def misuse_in_a_command(marker_local):
    raise typer.BadParameter("not a value this takes")


@cli_command(name="crash")
async def crash() -> None:
    explode_in_a_command(MARKUP_MARKER)


@cli_command(name="refuse")
async def refuse() -> None:
    refuse_in_a_command(LIST_MARKER)


@cli_command(name="logexc")
async def logexc() -> None:
    try:
        raise_and_log(DICT_MARKER)
    except KeyError:
        logging.getLogger("acme").exception("caught and carried on")


@cli_command(name="cancel")
async def cancel() -> None:
    cancel_in_a_command(MARKUP_MARKER)


@cli_command(name="quit")
async def quit_() -> None:
    exit_in_a_command(QUIET_MARKER)


@cli_command(name="misuse")
async def misuse() -> None:
    misuse_in_a_command(QUIET_MARKER)
"""


@pytest.fixture
def workspace(tmp_path):
    lab_data = tmp_path / "lab_data"
    lab_data.mkdir()
    write_lab_json(
        lab_data / "lab.json",
        [
            {
                "ip": "10.0.0.1",
                "element": "host1",
                "labs": ["test_lab"],
                "creds": [{"login": "admin", "password": "pass"}],
            }
        ],
    )
    repo = make_sut_repo(
        tmp_path / "sut",
        name="acme",
        extra=(
            'libs = ["lib"]\ninit = ["acme_tb_init"]\n\n'
            '[[lab.sources]]\nbackend = "json"\npaths = ["../lab_data"]'
        ),
        files={"lib/acme_tb_init.py": _INIT},
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("OTTO_")}
    env["OTTO_SUT_DIRS"] = str(repo)
    env["OTTO_HOME"] = str(tmp_path / "home")
    env["OTTO_XDIR"] = str(tmp_path / "xdir")
    return tmp_path, env


def _run(workspace, *argv: str) -> "tuple[subprocess.CompletedProcess[str], list[dict[str, str]]]":
    """Run ``otto --lab test_lab -R <argv>``; return the process and every run's two log files."""
    tmp_path, env = workspace
    p = subprocess.run(
        [*_ENTRY, "--lab", "test_lab", "-R", *argv],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=_RUN_TIMEOUT,
    )
    runs = [
        {name: (log.parent / name).read_text() for name in ("console.log", "verbose.log")}
        for log in sorted((tmp_path / "xdir").glob("*/*/console.log"))
    ]
    return p, runs


def _otto(workspace, *argv: str) -> "tuple[subprocess.CompletedProcess[str], dict[str, str]]":
    """Like :func:`_run`, for a command that is expected to create exactly one run directory."""
    p, runs = _run(workspace, *argv)
    assert len(runs) == 1, f"expected one run directory, found {len(runs)}\n{p.stdout}\n{p.stderr}"
    return p, runs[0]


def _assert_traceback(files: dict[str, str], *, headline: str, frame: str, local: str) -> None:
    for name, text in files.items():
        assert text.count(headline) == 1, f"{name} should carry {headline!r} once:\n{text}"
        assert f"in {frame}" in text, f"{name}: the frame {frame!r} is not named:\n{text}"
        assert re.search(rf"marker_local\s*=\s*{re.escape(local)}", text), (
            f"{name} has no locals panel for {frame!r}:\n{text}"
        )
        assert "\x1b" not in text, f"{name} carries ANSI without --rich-log-file"


def _assert_no_traceback(files: dict[str, str]) -> None:
    for name, text in files.items():
        assert "Traceback" not in text, f"{name} carries a traceback:\n{text}"
        assert "a value no clean exit may write" not in text, f"{name} carries a local:\n{text}"


def test_a_crash_writes_its_traceback_to_both_log_files(workspace):
    p, files = _otto(workspace, "crash")

    assert p.returncode == 1, p.stderr
    _assert_traceback(
        files,
        headline="RuntimeError: the crash message",
        frame="explode_in_a_command",
        local="'[bold]kept[/bold] verbatim'",
    )
    for name, text in files.items():
        assert "uncaught RuntimeError ended the command" in text, name
    # The terminal is unchanged: Typer's own traceback, once, on stderr.
    assert p.stderr.count("RuntimeError: the crash message") == 1, p.stderr
    assert "the crash message" not in p.stdout


def test_a_leaked_cancellation_is_recorded_as_a_crash(workspace):
    """``CancelledError`` is a BaseException, and one that leaves the command is a failure."""
    p, files = _otto(workspace, "cancel")

    assert p.returncode != 0, p.stderr
    _assert_traceback(
        files,
        headline="CancelledError: the leaked cancellation",
        frame="cancel_in_a_command",
        local="'[bold]kept[/bold] verbatim'",
    )


def test_an_otto_error_writes_its_traceback_to_both_log_files(workspace):
    p, files = _otto(workspace, "refuse")

    assert p.returncode == 1, p.stderr
    _assert_traceback(
        files,
        headline="Refused: the refusal message",
        frame="refuse_in_a_command",
        local="[1, 2, 3]",
    )
    for name, text in files.items():
        assert "error: the refusal message" in text, name
    # The terminal is unchanged: the clean error line, and no traceback below DEBUG.
    assert "error: the refusal message" in p.stdout + p.stderr
    assert "Traceback" not in p.stdout + p.stderr


def test_an_otto_error_writes_its_traceback_even_at_log_level_critical(workspace):
    """``--log-level CRITICAL`` admits no ERROR record, and the failure is still recorded."""
    p, files = _otto(workspace, "--log-level", "CRITICAL", "refuse")

    assert p.returncode == 1, p.stderr
    _assert_traceback(
        files,
        headline="Refused: the refusal message",
        frame="refuse_in_a_command",
        local="[1, 2, 3]",
    )
    assert "Traceback" not in p.stdout + p.stderr


def test_a_logged_exception_reaches_both_files_and_the_console_without_locals(workspace):
    p, files = _otto(workspace, "logexc")

    assert p.returncode == 0, p.stderr
    _assert_traceback(
        files,
        headline="KeyError: 'the logged key'",
        frame="raise_and_log",
        local="{'k': '[red]v[/red]'}",
    )
    # The console shows a Rich traceback, without the locals the files keep.
    assert "in raise_and_log" in p.stdout, p.stdout
    assert "KeyError" in p.stdout, p.stdout
    assert "[red]v[/red]" not in p.stdout, f"the console printed a locals panel:\n{p.stdout}"


def test_typer_exit_writes_no_traceback(workspace):
    p, files = _otto(workspace, "quit")

    assert p.returncode == 3, p.stderr
    _assert_no_traceback(files)


def test_a_usage_error_from_a_command_body_writes_no_traceback(workspace):
    p, files = _otto(workspace, "misuse")

    assert p.returncode == 2, p.stderr
    assert "not a value this takes" in p.stderr
    _assert_no_traceback(files)


@pytest.mark.parametrize(
    "argv", [["crash", "--no-such-flag"], ["crash", "--help"]], ids=["parse-error", "help"]
)
def test_a_parse_error_or_help_writes_no_traceback(workspace, argv):
    """Both end inside click's parser, before the command could create its run directory."""
    p, runs = _run(workspace, *argv)

    assert p.returncode in (0, 2), p.stderr
    for files in runs:
        _assert_no_traceback(files)
    assert "Traceback" not in p.stdout + p.stderr
