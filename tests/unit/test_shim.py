"""The console-script shim: `otto --version` must never import the CLI.

otto's framework import graph costs ~2400 path syscalls, and every one of them
is paid at MODULE IMPORT — before `otto.cli.main.entry` runs a line. No change
inside `entry()` can remove that cost, because the entry module IS the cost.
Moving the console-script entry point earlier is the only thing that does, so
these tests assert on the SHAPE OF sys.modules in a fresh child, never on
wall-clock.
"""

import json
import subprocess
import sys

import pytest

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
