"""The console-script shim: `otto --version` must never import the CLI.

otto's framework import graph costs ~2400 path syscalls, and every one of them
is paid at MODULE IMPORT — before `otto.cli.main.entry` runs a line. No change
inside `entry()` can remove that cost, because the entry module IS the cost.
Moving the console-script entry point earlier is the only thing that does, so
these tests assert on the SHAPE OF sys.modules in a fresh child, never on
wall-clock.
"""

import ast
import json
import os
import subprocess
import sys

import pytest

from tests._fixtures.paths import PROJECT_ROOT

# Runs the shim exactly as the console script does, then reports which otto
# modules the interpreter ended up carrying. A subprocess is mandatory: the
# pytest process has already imported half of otto, so an in-process check
# would pass no matter what the shim does.
_SHIM_CHILD = """
import sys, json
sys.argv = ["otto", "--version"]
from otto import _shim
try:
    _shim.main()
except SystemExit:
    pass
print(json.dumps(sorted(m for m in sys.modules if m.startswith("otto"))))
"""

# Same report, but reached through `otto/__main__.py` — the SECOND entry path.
# `runpy.run_module(..., run_name="__main__")` executes the very file
# `python -m otto` executes, so this pins that path to the shim too. Without
# it `otto --version` gets fast while `python -m otto --version` stays slow.
_DUNDER_MAIN_CHILD = """
import sys, json, runpy
sys.argv = ["otto", "--version"]
try:
    runpy.run_module("otto", run_name="__main__", alter_sys=True)
except SystemExit:
    pass
print(json.dumps(sorted(m for m in sys.modules if m.startswith("otto"))))
"""


def _loaded_otto_modules(tmp_path, source, name):
    child = tmp_path / name
    child.write_text(source)
    out = subprocess.run([sys.executable, str(child)], capture_output=True, text=True, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1]), out.stdout


@pytest.mark.parametrize(
    ("source", "name"),
    [(_SHIM_CHILD, "child.py"), (_DUNDER_MAIN_CHILD, "dunder.py")],
    ids=["console_script", "python_m_otto"],
)
def test_shim_answers_version_without_importing_the_cli(tmp_path, source, name):
    """Both entry paths answer --version off the lazy `otto` package alone.

    `otto/cli/__init__.py` is `from .main import app`, so ANY import under
    `otto.cli` loads 440 modules before the shim's first line — which is why
    the shim lives at `otto._shim` and why `otto.cli` absent from sys.modules
    is the whole assertion.
    """
    loaded, raw = _loaded_otto_modules(tmp_path, source, name)
    assert "otto.cli" not in loaded, f"shim imported the CLI package: {loaded}"
    assert "otto.cli.main" not in loaded, raw
    assert "otto.config" not in loaded, raw


def test_shim_version_output_matches_the_cli(tmp_path):
    """The shim must print exactly what the Typer callback prints.

    `version_callback` (`otto/cli/main.py`) emits `otto version: {v}` through
    rich's `rprint`; for a plain string rich adds nothing, so builtin `print`
    is byte-identical — and importing rich would defeat the whole point.
    """
    from otto.version import get_version

    child = tmp_path / "v.py"
    child.write_text(
        'import sys\nsys.argv = ["otto", "--version"]\n'
        "from otto import _shim\n"
        "try:\n    _shim.main()\nexcept SystemExit:\n    pass\n"
    )
    out = subprocess.run([sys.executable, str(child)], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == f"otto version: {get_version()}"


def test_shim_exits_zero_on_version(tmp_path):
    """`--version` is a successful invocation, not an aborted one."""
    child = tmp_path / "rc.py"
    child.write_text(
        'import sys\nsys.argv = ["otto", "--version"]\nfrom otto import _shim\n_shim.main()\n'
    )
    out = subprocess.run([sys.executable, str(child)], capture_output=True, text=True, check=False)
    assert out.returncode == 0, out.stderr


@pytest.mark.parametrize(
    ("argv", "cli_prints_version"),
    [
        (["otto", "host", "put", "--version"], False),
        (["otto", "run", "--version"], False),
        # NOT a hijack: Typer's `--version` callback is eager, so the REAL CLI
        # answers this one and exits 0. Verified against the unmodified console
        # script before the shim existed, and the shim must not change it —
        # which it cannot, because it hands this argv straight through.
        (["otto", "--version", "extra"], True),
    ],
    ids=["subcommand", "run_subcommand", "trailing_arg"],
)
def test_shim_hands_every_other_argv_to_the_full_cli(tmp_path, argv, cli_prints_version):
    """Exact match, never membership.

    `--version` appearing ANYWHERE in argv is not the fast path: `otto host put
    --version` is a real subcommand invocation that needs the registry, and a
    membership test would hijack it into printing the version instead. The
    invariant is therefore about WHO answers, not about what is printed — the
    CLI graph must be loaded in every case here.
    """
    child = tmp_path / "sub.py"
    child.write_text(
        f"import sys, json\nsys.argv = {argv!r}\n"
        "from otto import _shim\n"
        "try:\n    _shim.main()\nexcept BaseException:\n    pass\n"
        'print(json.dumps({"cli": "otto.cli.main" in sys.modules}))\n'
    )
    out = subprocess.run([sys.executable, str(child)], capture_output=True, text=True, check=False)
    assert json.loads(out.stdout.strip().splitlines()[-1])["cli"], (
        f"{argv} never reached the CLI: {out.stdout}\n{out.stderr}"
    )
    printed = "otto version:" in out.stdout
    assert printed is cli_prints_version, f"unexpected version output for {argv}: {out.stdout}"


def test_shim_imports_nothing_from_otto_at_module_scope(tmp_path):
    """Importing the shim must not be the thing that costs.

    A `from .version import ...` that drifted to module scope would still pass
    every test above (the version path imports it anyway) while re-loading the
    graph for the hand-off path. Pinning the post-import module set is what
    stops that drift.
    """
    child = tmp_path / "scope.py"
    child.write_text(
        "import sys, json\nfrom otto import _shim\n"
        'print(json.dumps(sorted(m for m in sys.modules if m.startswith("otto"))))\n'
    )
    out = subprocess.run([sys.executable, str(child)], capture_output=True, text=True, check=True)
    loaded = json.loads(out.stdout.strip().splitlines()[-1])
    assert loaded == ["otto", "otto._shim"], f"shim pulled more than itself: {loaded}"


def _colour_enabling_env():
    """Env in which rich WOULD colourise — the suite globally stops it.

    `tests/conftest.py` sets `NO_COLOR=1` and `TERM=dumb` in `os.environ` at
    import time so rich escapes never pollute assertions, and every subprocess
    INHERITS that. A pty on its own therefore proves nothing about this
    codebase: the child prints plainly whatever `version_callback` uses. This
    undoes the suppression for one child, and the positive control below
    proves the undoing worked.
    """
    import os

    env = dict(os.environ)
    for var in ("NO_COLOR", "CLICOLOR", "CLICOLOR_FORCE", "PY_COLORS", "FORCE_COLOR"):
        env.pop(var, None)
    env["TERM"] = "xterm-256color"
    return env


def _run_on_a_pty(argv, env):
    """Run *argv* with stdout on a real pty; return the raw bytes.

    A pipe cannot see this test's subject: rich auto-disables colour when
    stdout is not a tty, so every other subprocess assertion in this file runs
    on the one code path where highlighted and plain output are identical.
    """
    import os
    import pty

    master, slave = pty.openpty()
    try:
        proc = subprocess.Popen(argv, stdout=slave, stderr=subprocess.DEVNULL, stdin=slave, env=env)
        os.close(slave)
        slave = None
        chunks = []
        while True:
            try:
                chunk = os.read(master, 4096)
            except OSError:  # EIO on Linux once the child closes the pty
                break
            if not chunk:
                break
            chunks.append(chunk)
        proc.wait()
    finally:
        if slave is not None:
            os.close(slave)
        os.close(master)
    return b"".join(chunks)


def test_version_output_is_plain_on_a_tty_from_both_paths():
    """The two `--version` answers must agree BYTE FOR BYTE on a terminal.

    `otto --version` is answered by the shim; `otto --version extra` is
    answered by Typer's eager callback in `otto.cli.main`. Two code paths, one
    binary, one user-visible string — they must not diverge.

    They DID. While `version_callback` used rich's `rprint`, its
    `ReprHighlighter` colourised the version's digits on a tty, so the same
    binary printed `otto version: 0.9.0` for one and
    `otto version: \x1b[1;36m0.9\x1b[0m.\x1b[1;36m0\x1b[0m` for the other.
    Nothing in the suite could observe it: every other test reads through a
    pipe, where rich turns colour off by itself.

    Pins the PLAIN form deliberately, not merely agreement. `--version` is a
    machine-readable one-liner people pipe into `cut`/`awk`, and rich would
    additionally read `[...]` in a local/dev version string as console markup.
    """
    from otto.version import get_version

    env = _colour_enabling_env()

    # POSITIVE CONTROL, and it is not optional. `tests/conftest.py` disables
    # rich colour process-wide; without proving that this env re-enables it,
    # the assertions below would pass against a `rprint` that simply had no
    # colour to emit — a dead test that "proves" a fix nobody made. Verified:
    # with `version_callback` reverted to `rprint`, the run below is escaped
    # and the test fails.
    control = _run_on_a_pty(
        [sys.executable, "-c", "import rich; rich.print('otto version: 9.9.9')"], env
    )
    assert b"\x1b" in control, f"colour is off even with NO_COLOR cleared: {control!r}"

    expected = f"otto version: {get_version()}\r\n".encode()
    shim = _run_on_a_pty([sys.executable, "-m", "otto", "--version"], env)
    cli = _run_on_a_pty([sys.executable, "-m", "otto", "--version", "extra"], env)

    assert shim == expected, f"shim path is not plain on a tty: {shim!r}"
    assert cli == expected, f"CLI path is not plain on a tty: {cli!r}"
    assert shim == cli, f"the two --version paths disagree on a tty: {shim!r} vs {cli!r}"
    assert b"\x1b" not in shim + cli, "escape sequences leaked into --version output"


# Two snapshots, not one. `import otto` alone drags in whatever the package
# init costs on THIS interpreter, and that is not the shim's bill; only what
# `from otto import _shim` adds on top of it is. Reporting both lets the pin
# below subtract the baseline instead of measuring the interpreter.
_TAB_CHILD = """
import json, os, sys
sys.argv = ["otto"]
import otto
baseline = sorted(sys.modules)
from otto import _shim
try:
    _shim.main()
except SystemExit:
    pass
sys.stdout.write(
    "__MODULES__"
    + json.dumps({"baseline": baseline, "final": sorted(sys.modules)})
    + "\\n"
)
"""


def _tab(tmp_path, env, name):
    child = tmp_path / name
    child.write_text(_TAB_CHILD)
    out = subprocess.run(
        [sys.executable, str(child)],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, **env, "_OTTO_COMPLETE": "complete_bash"},
    )
    answer, _, modules = out.stdout.partition("__MODULES__")
    # ``{"baseline": [...], "final": [...]}`` — the module lists either side of
    # ``from otto import _shim``.
    return answer, json.loads(modules), out.stderr


def test_warm_tab_is_answered_without_the_cli_or_config_packages(tmp_path):
    """The whole point: the second TAB loads otto, otto._shim, otto._shim_complete, nothing else."""
    from tests._fixtures.shim_repo import make_shim_repo

    repo = make_shim_repo(tmp_path)
    env = {
        "OTTO_SUT_DIRS": str(repo),
        "OTTO_HOME": str(tmp_path / "home"),
        "COMP_WORDS": "otto host ",
        "COMP_CWORD": "2",
    }
    cold, cold_modules, err = _tab(tmp_path, env, "cold.py")
    assert "otto.cli.main" in cold_modules["final"], err
    warm, warm_modules, err = _tab(tmp_path, env, "warm.py")
    assert warm == cold, (warm, cold)
    assert {m for m in warm_modules["final"] if m.startswith("otto")} == {
        "otto",
        "otto._shim",
        "otto._shim_complete",
    }, err
    # The resolver's own imports: no dataclasses (+inspect) and no pathlib; `typing`
    # is already loaded by otto/__init__ (TYPE_CHECKING), so it is free.
    #
    # MARGINAL cost, hence the subtraction: `import otto` pulls `logging`, and on a
    # newer interpreter `logging` reaches `traceback` -> `_colorize` -> `dataclasses`
    # -> `inspect` before the shim is asked for anything. Those are the package
    # init's bill on every invocation, shim or not, and a pin that read the final
    # module set alone would call them the resolver's — failing on the interpreter
    # where the baseline happens to be fatter while saying nothing about the shim.
    added = set(warm_modules["final"]) - set(warm_modules["baseline"])
    assert {"dataclasses", "pathlib", "inspect"}.isdisjoint(added), err
    assert cold.endswith("\n")
    assert "dut1" in cold.split()


# ── the same guarantee, statically: what the two shim modules may import ─────
#
# The dynamic pin above measures ONE interpreter's marginal cost. This one reads
# the source, so it holds on every interpreter and on a machine where the cache
# never warms: a `dataclasses` import added to the resolver fails here whatever
# `logging` happens to drag in that year.
_SHIM_PACKAGE = "otto"
_SHIM_SRC = PROJECT_ROOT / "src" / "otto"


def _imported_names(node: "ast.Import | ast.ImportFrom", filename: str) -> set:
    """The module names *node* imports, as `sys.modules` would key them.

    ``import os.path`` costs ``os``, and ``from typing import Any`` costs
    ``typing``, so an absolute import is recorded by its ROOT — the two shapes
    must not be able to smuggle a package past the pin by spelling. A relative
    import is recorded in FULL (the shim's own siblings are the interesting
    names, and rooting them all at ``otto`` would say nothing), resolved against
    the package the file lives in, which is ``otto`` for both of them:

    * ``from .cli.main import entry``   -> ``otto.cli.main``
    * ``from . import _shim_complete``  -> ``otto._shim_complete``

    The second spelling is why the names are read rather than the module: with
    no ``node.module`` it is the ALIASES that name the submodules, and reporting
    a bare ``otto`` there would hide which sibling was reached.

    ``from .. import x`` has no valid meaning for a member of a top-level
    package, so rather than mislabel it this raises — the pin includes "that
    spelling does not occur".
    """
    if isinstance(node, ast.Import):
        return {alias.name.split(".")[0] for alias in node.names}
    if not node.level:
        return {node.module.split(".")[0]}
    if node.level > 1:
        raise AssertionError(
            f"src/otto/{filename} line {node.lineno}: "
            f"`from {'.' * node.level}{node.module or ''} import …` reaches above "
            f"the {_SHIM_PACKAGE!r} package, which cannot resolve — the shim modules "
            f"are top-level members of it."
        )
    if node.module:
        return {f"{_SHIM_PACKAGE}.{node.module}"}
    return {f"{_SHIM_PACKAGE}.{alias.name}" for alias in node.names}


_DEFERRING_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _shim_imports(filename: str) -> tuple:
    """Return ``(module_level, deferred)`` import names for ``src/otto/<filename>``.

    "Module level" is everything NOT inside a ``def``/``async def``/``class``
    body, not merely everything in ``tree.body``: an import under a module-scope
    ``if``, ``try`` or ``with`` is paid on EVERY invocation exactly like a bare
    one, and bucketing it by top-level statement membership would wave a guarded
    ``try: import dataclasses`` straight past the pin.
    """
    tree = ast.parse((_SHIM_SRC / filename).read_text(encoding="utf-8"))
    module_level: set = set()
    deferred: set = set()

    def collect(node, inside_a_scope: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                bucket = deferred if inside_a_scope else module_level
                bucket.update(_imported_names(child, filename))
            collect(child, inside_a_scope or isinstance(child, _DEFERRING_SCOPES))

    collect(tree, False)
    return module_level, deferred


def test_the_resolver_imports_exactly_the_seven_cheap_stdlib_modules():
    """`otto/_shim_complete.py` imports these and nothing else, deferred or not.

    Every name here is either already loaded by the time the shim runs or costs
    a handful of syscalls. The two the list exists to exclude are `dataclasses`
    (which drags in `inspect`, `dis`, `ast`) and `pathlib`; `os.path` does the
    resolver's path work instead, which is why this file carries a per-file
    `PTH` exemption in .ruff.toml.
    """
    module_level, deferred = _shim_imports("_shim_complete.py")

    assert module_level == {"hashlib", "json", "os", "re", "shlex", "time", "typing"}
    assert deferred == set()  # nothing hidden inside a function either


def test_the_shim_entry_imports_only_stdlib_at_module_level():
    """`otto/_shim.py` is imported on EVERY invocation, so its module level is bare.

    Everything otto costs is deferred into the branch that needs it: the resolver
    and the version reader on the answered paths, and `otto.cli.main` — the whole
    framework — only once the shim has decided to hand over. Pinning both halves
    says which names may appear at all, and that none of them is paid up front.
    """
    module_level, deferred = _shim_imports("_shim.py")

    assert module_level == {"os", "sys"}
    assert deferred == {"otto._shim_complete", "otto.cli.main", "otto.version"}


def test_a_candidate_stdout_cannot_encode_falls_through_to_the_full_path(monkeypatch):
    """An ASCII stdout and a non-ASCII candidate: click's echo degrades with
    ``errors="replace"``, so the shim must not traceback into the shell instead.
    ``TextIOWrapper`` encodes before it buffers, so nothing partial reached the
    terminal and the full path can answer the same TAB."""
    import io

    from otto import _shim

    monkeypatch.setattr(sys, "argv", ["otto"])
    monkeypatch.setenv("_OTTO_COMPLETE", "complete_bash")
    monkeypatch.setattr("otto._shim_complete.answer", lambda environ: "café")
    stdout = io.TextIOWrapper(io.BytesIO(), encoding="ascii")
    monkeypatch.setattr(sys, "stdout", stdout)
    fell_through = []
    monkeypatch.setattr("otto.cli.main.entry", lambda: fell_through.append(True))

    _shim.main()  # no SystemExit(0), and no UnicodeEncodeError

    assert fell_through == [True]
    assert stdout.buffer.getvalue() == b""


def test_a_non_bash_shell_takes_the_full_path(tmp_path):
    from tests._fixtures.shim_repo import make_shim_repo

    repo = make_shim_repo(tmp_path)
    env = {
        "OTTO_SUT_DIRS": str(repo),
        "OTTO_HOME": str(tmp_path / "home"),
        "_TYPER_COMPLETE_ARGS": "otto host ",
        "COMP_WORDS": "otto host ",
        "COMP_CWORD": "2",
    }
    _tab(tmp_path, env, "seed.py")
    child = tmp_path / "zsh.py"
    child.write_text(_TAB_CHILD)
    out = subprocess.run(
        [sys.executable, str(child)],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, **env, "_OTTO_COMPLETE": "complete_zsh"},
    )
    assert "otto.cli.main" in out.stdout
