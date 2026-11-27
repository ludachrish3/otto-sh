"""Run ``otto`` as a user's shell does, in a subprocess, against a project on disk.

:func:`run_otto` runs the real console entry (``otto._shim:main``);
:func:`typer_tab` asks the Typer fast path (``otto.cli.main.entry()`` itself,
the path the shim hands a TAB over to) for the words of a bash TAB, and
:func:`shim_answer` asks the shim. A subprocess, because ``entry()`` decides
from ``sys.argv`` before Typer parses anything, the caches are keyed on a
workspace home, and bootstrap is a process-global singleton: in-process would
prove nothing about either path.
"""

import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from otto.config.env import is_otto_env_var
from tests._fixtures.entry_runner import bash_tab

if TYPE_CHECKING:
    from otto._shim_complete import Outcome

ENTRY = [sys.executable, "-c", "from otto._shim import main; main()"]
"""The console script, as the installed ``otto`` runs it."""

TYPER_ENTRY = "import sys; sys.argv = ['otto']; from otto.cli.main import entry; entry()"
"""The Typer fast path's entry, without the shim in front of it."""

INSTALLERS = ("--install-completion", "--show-completion")
"""Typer's completion installers, which some paths offer on a ``run`` leaf and others do not.

The difference exists without any options class; tests hold it apart until it
is fixed. Refs #1383"""


def cli_env(ottopath: "Path | str", home: Path) -> "dict[str, str]":
    """The environment of an ``otto`` over the projects of *ottopath*, with its own *home*.

    Every ``OTTO_*`` variable of this process is dropped; bytecode is never
    written into the fixture tree, which would make a second run differ from
    the first; ``COLUMNS`` pins the render width so two runs compare byte for
    byte.
    """
    env = {k: v for k, v in os.environ.items() if not is_otto_env_var(k)}
    env["OTTOPATH"] = str(ottopath)
    env["OTTO_HOME"] = str(home)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["COLUMNS"] = "100"
    return env


def run_otto(env: "dict[str, str]", *argv: str) -> subprocess.CompletedProcess:
    """Run ``otto <argv>`` through the console script under *env*."""
    return subprocess.run([*ENTRY, *argv], env=env, capture_output=True, text=True, check=False)


def tab_env(env: "dict[str, str]", words: str) -> "dict[str, str]":
    """*env* with the variables bash sets for a TAB on the line *words* (:func:`bash_tab`)."""
    return {**env, **bash_tab(words)}


def typer_tab(env: "dict[str, str]", words: str) -> "list[str]":
    """The words the Typer fast path gives the bash TAB *words* (``entry()`` itself, no shim)."""
    result = subprocess.run(
        [sys.executable, "-c", TYPER_ENTRY],
        env=tab_env(env, words),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Traceback" not in result.stderr, result.stderr
    return result.stdout.split()


def cold_tab(env: "dict[str, str]", home: Path, words: str) -> "list[str]":
    """The same TAB with no cache at all: a full load in a home no run has written."""
    return typer_tab({**env, "OTTO_HOME": str(home)}, words)


def shim_answer(env: "dict[str, str]", words: str) -> "Outcome":
    """The bash shim's outcome for *words* against *env*'s cache: words, or why it hands over."""
    from otto import _shim_complete as sc

    return sc.answer_or_reason(tab_env(env, words))


def shim_tab(env: "dict[str, str]", words: str) -> "list[str]":
    """The bash shim's answer to *words*; it must answer, not hand over."""
    out = shim_answer(env, words)
    assert out.items is not None, out.reason
    return out.items


def without_installers(words: "list[str]") -> "list[str]":
    """*words* without Typer's completion installers (:data:`INSTALLERS`)."""
    return [w for w in words if w not in INSTALLERS]
