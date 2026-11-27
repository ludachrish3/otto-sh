"""Run ``otto <argv>`` as a user's shell does: the console script, and a TAB.

:func:`otto_cli` is the fixture the CLI tests share for error-text assertions:
``entry()`` is where otto renders a failure, so a test that pins an ``error:``
line goes through it. ``tests/unit/cli/conftest.py`` provides it.
:func:`complete` asks otto's app for the words a bash TAB would get.
"""

import contextvars
import dataclasses
import os
from collections.abc import Callable, Mapping

import pytest
import typer
from typer.testing import CliRunner

from otto.config.env import is_otto_env_var


@dataclasses.dataclass(frozen=True)
class CliRun:
    """What one console-script run printed and how it ended."""

    code: int
    stdout: str
    stderr: str

    @property
    def text(self) -> str:
        """Everything printed, the terminal's wrapping undone."""
        return " ".join(line.strip() for line in (self.stdout + self.stderr).splitlines())


@pytest.fixture
def otto_cli(monkeypatch, tmp_path) -> "Callable[..., CliRun]":
    """``otto_cli(argv, **env)``: run ``otto <argv>`` through the console-script ``entry()``.

    Every ``OTTO_*`` variable is unset, ``OTTO_XDIR`` points into *tmp_path*
    and ``COLUMNS`` is 200. *env* is set for that one run (``None`` unsets).
    Each run starts in a fresh ``contextvars.Context``, as a new process
    does: nothing a test or an earlier run set in a context variable is seen.
    """
    for key in list(os.environ):
        if is_otto_env_var(key):
            monkeypatch.delenv(key)
    monkeypatch.setenv("OTTO_XDIR", str(tmp_path / "xdir"))
    monkeypatch.setenv("COLUMNS", "200")

    wrapper = typer.Typer()

    @wrapper.command()
    def otto_entry() -> None:
        from otto.cli.main import entry

        entry()

    def run(argv: "list[str]", **env: "str | None") -> CliRun:
        monkeypatch.setattr("sys.argv", ["otto", *argv])
        out = contextvars.Context().run(CliRunner().invoke, wrapper, [], env=env or None)
        if out.exception is not None and not isinstance(out.exception, SystemExit):
            raise out.exception
        return CliRun(code=out.exit_code, stdout=out.stdout, stderr=out.stderr)

    return run


def bash_tab(words: str) -> "dict[str, str]":
    """The variables bash sets for a TAB on the command line *words*.

    *words* is the line as typed (``"otto flash erase --"``); a trailing space
    completes a new word, so ``COMP_CWORD`` indexes the word after the last.
    The one home of that rule: every helper that derives ``COMP_CWORD`` from
    a typed line calls this.
    """
    cword = len(words.split()) - (0 if words.endswith(" ") else 1)
    return {"_OTTO_COMPLETE": "complete_bash", "COMP_WORDS": words, "COMP_CWORD": str(cword)}


def complete(words: str, env: "Mapping[str, str | None] | None" = None) -> "list[str]":
    """Return the words a bash TAB on the command line *words* gets from otto's app.

    *words* is the line as typed (:func:`bash_tab`). *env* is the environment
    of that one run. The completion must succeed: exit 0 and no exception but
    ``SystemExit``.
    """
    from otto.cli.main import app

    env = {**(env or {}), **bash_tab(words)}
    result = CliRunner().invoke(app, [], env=env, prog_name="otto")
    assert result.exit_code == 0, (result.output, result.exception)
    assert result.exception is None or isinstance(result.exception, SystemExit)
    return result.output.split()
