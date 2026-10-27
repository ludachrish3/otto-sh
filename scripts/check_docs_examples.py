"""Validate every docs example project the way a reader would, without a lab.

Each project under ``docs/examples`` that has ``.otto/settings.toml`` is copied
(the whole examples tree, so sibling references resolve) into a temporary
directory and run through ``otto --list-products`` and ``otto --lab <lab> -n run
install`` with that copy as its only project. The lab is the one a reader of the
example uses: the project's single literal ``[project] lab_patterns`` entry, or
else the only lab ``otto --list-labs`` reports. A project with a
``CMakeLists.txt`` is also configured, built and has its tests run by
``ctest``. Every example is checked even after one fails; the exit status is 1
if any failed.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

import tomli

from otto.config.env import without_otto_env

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "docs" / "examples"
LITERAL_LAB = re.compile(r"[A-Za-z0-9_.-]+")
LISTED_LAB = re.compile(r"\u2022\s+(\S+)")
NO_LAB = (
    "cannot tell which lab a reader uses: give [project] lab_patterns one "
    "literal lab name, or keep the example to one lab"
)
TIMEOUT_S = 600
NOT_COPIED = {"__pycache__"}
"""Never copied anywhere in the examples tree: bytecode."""
EXAMPLE_BUILD_DIR = "build"
"""An example's own build output, not copied: its CMake cache names the checkout's paths."""


class _ExampleError(Exception):
    """One example cannot be checked; the message says why, prefixed by nothing."""


def _otto() -> str:
    return str(Path(sys.executable).with_name("otto"))


def examples(root: Path) -> "list[Path]":
    """Return every example project under *root*: a directory with ``.otto/settings.toml``."""
    return sorted(p for p in root.iterdir() if (p / ".otto" / "settings.toml").is_file())


def _env(project: Path, scratch: Path) -> "dict[str, str]":
    env = without_otto_env(os.environ)
    env.update(
        OTTOPATH=str(project),
        OTTO_HOME=str(scratch / "home"),
        OTTO_XDIR=str(scratch / "xdir"),
        NO_COLOR="1",
    )
    return env


def _execute(
    argv: "list[str]", cwd: Path, env: "dict[str, str]"
) -> "subprocess.CompletedProcess[str] | str":
    """Run *argv*; return the finished process, or a one-line reason it never finished."""
    try:
        return subprocess.run(  # noqa: S603 — otto, cmake or ctest with argv built here, no shell
            argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=TIMEOUT_S, check=False
        )
    except subprocess.TimeoutExpired:
        return f"timed out after {TIMEOUT_S} s"
    except OSError as exc:
        return f"not found or not runnable: {argv[0]} ({exc.strerror})"


def _run(argv: "list[str]", cwd: Path, env: "dict[str, str]") -> "str | None":
    """Run *argv*; return None on success, else a one-line reason."""
    return _reason(_execute(argv, cwd, env))


def _reason(done: "subprocess.CompletedProcess[str] | str") -> "str | None":
    """Return None for a successful run, else a one-line reason it failed."""
    if isinstance(done, str):
        return done
    if done.returncode == 0:
        return None
    tail = (done.stdout + done.stderr).strip().splitlines()[-1:] or [""]
    return f"exit {done.returncode}: {tail[0]}"


def _lab(project: Path, otto: str, env: "dict[str, str]") -> str:
    """Return the lab a reader of *project* targets.

    Raises:
        _ExampleError: The settings cannot be read, the lab listing failed, or
            neither names exactly one lab.
    """
    try:
        settings = tomli.loads((project / ".otto" / "settings.toml").read_text())
    except (OSError, UnicodeDecodeError, tomli.TOMLDecodeError) as exc:
        raise _ExampleError(f"cannot read .otto/settings.toml: {exc}") from exc
    patterns = settings.get("project", {}).get("lab_patterns", [])
    if len(patterns) == 1 and LITERAL_LAB.fullmatch(patterns[0]):
        return patterns[0]
    listed = _execute([otto, "--list-labs"], project, env)
    reason = _reason(listed)
    if reason is not None:
        raise _ExampleError(f"`otto --list-labs` failed ({reason})")
    labs = LISTED_LAB.findall(listed.stdout) if not isinstance(listed, str) else []
    if len(labs) != 1:
        raise _ExampleError(NO_LAB)
    return labs[0]


def not_copied(examples_root: Path) -> "Callable[[str, list[str]], set[str]]":
    """Return a copytree filter: bytecode anywhere, and each example's own build directory."""

    def ignore(directory: str, names: "list[str]") -> "set[str]":
        skip = NOT_COPIED & set(names)
        if Path(directory).parent == examples_root and EXAMPLE_BUILD_DIR in names:
            skip.add(EXAMPLE_BUILD_DIR)
        return skip

    return ignore


def check(example: Path, examples_root: Path, *, otto: str) -> "list[str]":
    """Check one example in a scratch copy; return one message per failed command."""
    failures: "list[str]" = []
    with tempfile.TemporaryDirectory(prefix="otto-docs-example-") as tmp:
        scratch = Path(tmp)
        copy_root = scratch / "examples"
        shutil.copytree(examples_root, copy_root, ignore=not_copied(examples_root))
        project = copy_root / example.name
        env = _env(project, scratch)
        try:
            lab = _lab(project, otto, env)
        except _ExampleError as exc:
            return [f"{example.name}: {exc}"]
        commands = [
            (["otto", "--list-products"], [otto, "--list-products"]),
            (["otto", "-n", "run", "install"], [otto, "--lab", lab, "-n", "run", "install"]),
        ]
        if (project / "CMakeLists.txt").is_file():
            build = project / "build"
            commands.extend(
                (argv[:2], argv)
                for argv in (
                    ["cmake", "-S", str(project), "-B", str(build)],
                    ["cmake", "--build", str(build)],
                    ["ctest", "--test-dir", str(build), "--output-on-failure", "--no-tests=error"],
                )
            )
        for shown, argv in commands:
            reason = _run(argv, project, env)
            if reason is not None:
                failures.append(f"{example.name}: `{' '.join(shown)}` failed ({reason})")
    return failures


def main(argv: "list[str] | None" = None) -> int:
    """Check every example project; print a line per example and return 1 on any failure."""
    del argv
    found = examples(EXAMPLES)
    failures: "list[str]" = []
    for example in found:
        failed = check(example, EXAMPLES, otto=_otto())
        print(f"{example.name}: {'FAIL' if failed else 'ok'}")
        failures += failed
    for line in failures:
        print(f"  {line}")
    print(f"checked {len(found)} example project(s), {len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
