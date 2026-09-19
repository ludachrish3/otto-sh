"""An empty colour variable in CI's ``env:`` block is still SET — sanitize it.

.github/workflows/ci.yml's top-level ``env:`` block neutralises the runner's
own colour settings, and GitHub Actions has no way to UNSET an inherited
variable: the best it can do is assign ``""``. A consumer that asks "is it
set?" rather than "is it truthy?" reads that as yes. Node is one of those —
``tty.getColorDepth()`` tests ``env.FORCE_COLOR !== undefined`` — so
``NO_COLOR: "1"`` next to ``FORCE_COLOR: ""`` made Node emit colour, ignore
NO_COLOR, and print

    Warning: The 'NO_COLOR' env is ignored due to the 'FORCE_COLOR' env being set.

on stderr. The web gates fail on any stderr output, so that single line failed
five jobs in their web build step (#387) — and the env block was not doing its
job either, which is the worse half. No local shell sets FORCE_COLOR, so no
local gate could see it; that is what makes this the class of bug a pin has to
cover rather than a run.

So the empties are dropped on our side of the boundary, before any child
starts: ``unexport`` in the Makefile for every recipe, and an ``unset`` in
scripts/build_web_no_warnings.sh, scripts/gen_web_types.sh and
scripts/typecheck_web.sh for a direct call (Read the Docs, a human).

The pins below read the names out of ci.yml on ONE side and out of the
Makefile/scripts (and out of what a real child process receives) on the OTHER,
so neither side is a copy of a list written here. Add a ``""``-valued variable
to ci.yml without sanitizing it and these fail.
"""

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

from tests._fixtures.paths import PROJECT_ROOT

pytestmark = pytest.mark.interpreter_agnostic

_CI_WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"
_MAKEFILE = PROJECT_ROOT / "Makefile"
_SCRIPTS = ("build_web_no_warnings.sh", "gen_web_types.sh", "typecheck_web.sh")

# The npm-script argument build_web_no_warnings.sh needs; the fake npm ignores
# it. The other two scripts take no arguments.
_SCRIPT_ARGS = {"build_web_no_warnings.sh": ["check"]}


def _ci_env() -> dict[str, str]:
    """ci.yml's top-level ``env:`` block, values as the runner would set them.

    A key written with no value at all (``FOO:``) is YAML ``None`` and reaches
    the runner as the empty string, exactly like ``FOO: ""`` — so it counts.
    """
    env = yaml.safe_load(_CI_WORKFLOW.read_text())["env"]
    return {name: "" if value is None else str(value) for name, value in env.items()}


def _ci_empty_vars() -> list[str]:
    empty = sorted(name for name, value in _ci_env().items() if value == "")
    assert empty, (
        "ci.yml's env: block no longer sets anything to the empty string. If "
        "that is deliberate these pins have nothing left to guard and should "
        "go; if a value was merely moved, point them at its new home."
    )
    return empty


def _makefile_sanitized_vars() -> set[str]:
    """The names the Makefile drops from every recipe's environment."""
    makefile = _MAKEFILE.read_text()
    declaration = re.search(r"^COLOR_FORCE_VARS\s*:?=\s*(.+)$", makefile, re.MULTILINE)
    assert declaration, (
        "no `COLOR_FORCE_VARS :=` list in the Makefile — the empty colour "
        "variables are no longer named anywhere for a recipe"
    )
    # The list is inert unless something actually unexports it, and a bare
    # `unexport` (no names) means something else entirely, so require the two
    # together rather than trusting the variable's existence.
    assert re.search(r"\$\(foreach\s+\S+,\s*\$\(COLOR_FORCE_VARS\).*\bunexport\b", makefile), (
        "COLOR_FORCE_VARS is declared but nothing unexports its entries, so "
        "every recipe still hands an empty FORCE_COLOR to its children"
    )
    return set(declaration.group(1).split())


def _script_sanitized_vars(script: str) -> set[str]:
    """The names ``scripts/<script>`` unsets before it starts a child."""
    body = (PROJECT_ROOT / "scripts" / script).read_text()
    loop = re.search(r"^\s*for _var in ([^;]+); do$", body, re.MULTILINE)
    assert loop, (
        f"scripts/{script} no longer has the `for _var in <names>; do` loop "
        "that unsets the empty colour variables"
    )
    assert 'unset -v "$_var"' in body, (
        f"scripts/{script} names the colour variables but never unsets them"
    )
    return set(loop.group(1).split())


def test_every_empty_ci_env_var_is_sanitized_everywhere() -> None:
    """Every ``""`` in ci.yml's env: block is dropped by all four entry points.

    Deliberately not limited to names that look like colour switches: an empty
    value in that block is a contradiction whatever it is called — it reads as
    "off" and arrives as "set" — so the rule is that anything set to "" there
    is either sanitized here or not written there.
    """
    empty = set(_ci_empty_vars())
    sanitized = {"Makefile": _makefile_sanitized_vars()} | {
        f"scripts/{script}": _script_sanitized_vars(script) for script in _SCRIPTS
    }
    missing = {where: sorted(empty - names) for where, names in sanitized.items() if empty - names}
    assert not missing, (
        "ci.yml's env: block sets these to the empty string, which is still "
        "SET for anything that tests `!== undefined`, but they are not dropped "
        f"before our children start: {missing!r}. Add them to the Makefile's "
        "COLOR_FORCE_VARS and to each script's `for _var in ...` loop."
    )


# --------------------------------------------------------------------------
# Behavioural half: what a child process actually receives.
#
# The static pin above proves the names are listed; these prove the listing
# works. Each case puts a recorder on PATH in place of npm/npx/uv, runs the
# real Makefile recipe or the real script under ci.yml's OWN env block, and
# reads back which of the watched names the child was handed.
# --------------------------------------------------------------------------

# Records, for each name in $FAKE_ENV_WATCH, whether it was SET (`+set`, which
# is true for an empty value too — the whole point) and what it held.
_RECORDER = """#!/usr/bin/env bash
: > "$FAKE_ENV_RECORD"
for _name in $FAKE_ENV_WATCH; do
    if [ -n "${!_name+set}" ]; then
        printf '%s=%s\\n' "$_name" "${!_name}" >> "$FAKE_ENV_RECORD"
    fi
done
exit 0
"""

# Directories the Makefile `find`s at parse time; created empty so the parse is
# quiet in the throwaway directory (as in tests/unit/test_web_install_warnings.py).
_PARSE_TIME_DIRS = [
    "web/src",
    "docs",
    "src/otto",
    "tests/e2e/monitor/dashboard",
    "tests/e2e/cov/report_browser",
]


def _fake(bin_dir: Path, name: str) -> None:
    tool = bin_dir / name
    tool.write_text(_RECORDER)
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR)


def _child_env(tmp_path: Path, bin_dir: Path, overrides: dict[str, str]) -> dict[str, str]:
    """This process's environment, scrubbed of the names under test, then
    overlaid with ci.yml's real env: block and the case's overrides."""
    watched = _ci_empty_vars()
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in watched
        # An outer make's flags (-n, -j, ...) must not ride into this one.
        and key not in {"MAKEFLAGS", "MFLAGS", "MAKELEVEL", "MAKEOVERRIDES"}
    }
    env |= _ci_env()
    env |= overrides
    env["PATH"] = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
    env["FAKE_ENV_RECORD"] = str(tmp_path / "child-env")
    env["FAKE_ENV_WATCH"] = " ".join(watched)
    return env


def _recorded(tmp_path: Path) -> dict[str, str]:
    record = tmp_path / "child-env"
    assert record.is_file(), (
        "the recorder never ran, so nothing was measured — the run below did "
        "not reach its npm/npx child"
    )
    return dict(line.split("=", 1) for line in record.read_text().splitlines() if "=" in line)


def _run_recipe(tmp_path: Path, **overrides: str) -> subprocess.CompletedProcess[str]:
    """The real Makefile's web-install recipe, with a recorder for `npm ci`."""
    make = shutil.which("make")
    if make is None:
        pytest.fail("the Makefile colour pins need `make` on PATH")
    for rel in _PARSE_TIME_DIRS:
        (tmp_path / rel).mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake(bin_dir, "npm")
    return subprocess.run(
        [make, "--no-print-directory", "-f", str(_MAKEFILE), "web-install"],
        cwd=tmp_path,
        env=_child_env(tmp_path, bin_dir, overrides),
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def _run_script(tmp_path: Path, script: str, **overrides: str) -> subprocess.CompletedProcess[str]:
    """A COPY of the real script in a throwaway root, with recorders for the
    npm/npx/uv it would otherwise run (gen_web_types.sh and typecheck_web.sh
    resolve the repo from their own location, so the copy needs the layout)."""
    if script == "typecheck_web.sh" and shutil.which("jq") is None:
        pytest.fail("scripts/typecheck_web.sh needs `jq` on PATH; install it")
    root = tmp_path / "root"
    (root / "scripts").mkdir(parents=True)
    (root / "web").mkdir()
    shutil.copy(PROJECT_ROOT / "scripts" / script, root / "scripts" / script)
    (root / "web" / "untitledui.lock.json").write_text('{"paths": ["src/components/**"]}\n')
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("npm", "npx", "uv"):
        _fake(bin_dir, tool)
    return subprocess.run(
        ["bash", str(root / "scripts" / script), *_SCRIPT_ARGS.get(script, [])],
        cwd=root,
        env=_child_env(tmp_path, bin_dir, overrides),
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def test_a_recipe_child_never_sees_an_empty_colour_var(tmp_path: Path) -> None:
    """Under ci.yml's own env block, `make`'s children get none of the empties."""
    result = _run_recipe(tmp_path)
    assert result.returncode == 0, result.stderr
    leaked = _recorded(tmp_path)
    assert not leaked, (
        "a Makefile recipe handed its npm child the empty colour variable(s) "
        f"{leaked!r}; Node reads an empty FORCE_COLOR as SET, ignores NO_COLOR "
        "and warns about it on stderr, which fails the web gates"
    )


@pytest.mark.parametrize("script", _SCRIPTS)
def test_a_script_child_never_sees_an_empty_colour_var(tmp_path: Path, script: str) -> None:
    """Same for a direct call, which is how Read the Docs and humans run them."""
    result = _run_script(tmp_path, script)
    assert result.returncode == 0, result.stderr
    assert result.stderr == "", (
        f"scripts/{script} wrote to stderr under ci.yml's env block: {result.stderr!r}"
    )
    leaked = _recorded(tmp_path)
    assert not leaked, (
        f"scripts/{script} handed its npm/npx child the empty colour "
        f"variable(s) {leaked!r} instead of unsetting them"
    )


def test_a_non_empty_force_color_survives_a_recipe(tmp_path: Path) -> None:
    """Sanitation is for the CONTRADICTION, not for colour: someone who asks
    for colour with a real value keeps it."""
    result = _run_recipe(tmp_path, FORCE_COLOR="1")
    assert result.returncode == 0, result.stderr
    assert _recorded(tmp_path).get("FORCE_COLOR") == "1", (
        "a deliberate FORCE_COLOR=1 must reach a recipe's children unchanged; "
        "only the empty (contradictory) value is dropped"
    )


@pytest.mark.parametrize("script", _SCRIPTS)
def test_a_non_empty_force_color_survives_a_script(tmp_path: Path, script: str) -> None:
    result = _run_script(tmp_path, script, FORCE_COLOR="1")
    assert result.returncode == 0, result.stderr
    assert _recorded(tmp_path).get("FORCE_COLOR") == "1", (
        f"scripts/{script} must pass a deliberate FORCE_COLOR=1 through; only "
        "the empty (contradictory) value is dropped"
    )
