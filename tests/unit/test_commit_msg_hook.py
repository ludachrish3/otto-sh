"""The commit-msg hook's own plumbing, with no dependence on node or ``web/node_modules``.

The hook is ``.githooks/commit-msg``: it runs commitlint with the repository-root
``commitlint.config.mjs``. WHAT the config accepts and refuses is pinned where
node_modules exists, in ``web/src/__tests__/commitlintConfig.test.ts`` (CI's
``check-ts`` job). This module runs a copy of the REAL hook script in a scratch
root, with a fake ``commitlint`` in place of the linter, and pins what the hook
itself does: the arguments it hands the linter, the exit code it passes
through, and that it fails closed, naming the remedy, when commitlint or node
is missing.
"""

import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from tests._fixtures.paths import CHECKOUT_ROOT

pytestmark = pytest.mark.interpreter_agnostic

_HOOK = CHECKOUT_ROOT / ".githooks" / "commit-msg"


def _root(tmp_path: Path, *, fake_exit: int | None) -> tuple[Path, Path]:
    """A scratch root holding a copy of the real hook and, optionally, a fake commitlint.

    The fake records its argv, one argument per line, in ``argv.txt`` and exits
    with *fake_exit*. Returns ``(hook, message file)``.
    """
    hooks = tmp_path / ".githooks"
    hooks.mkdir()
    hook = hooks / "commit-msg"
    shutil.copy(_HOOK, hook)
    if fake_exit is not None:
        binary = tmp_path / "web" / "node_modules" / ".bin" / "commitlint"
        binary.parent.mkdir(parents=True)
        binary.write_text(
            f'#!/bin/sh\nprintf \'%s\\n\' "$@" > "{tmp_path}/argv.txt"\nexit {fake_exit}\n'
        )
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    message = tmp_path / "MSG"
    message.write_text("fix: x\n")
    return hook, message


def _run(hook: Path, message: Path, *, path: str | None = None):
    # bash by absolute path: the PATH under test may hold no node (or anything).
    bash = shutil.which("bash")
    assert bash is not None
    env = {"PATH": path} if path is not None else None
    return subprocess.run(
        [bash, str(hook), str(message)], capture_output=True, text=True, check=False, env=env
    )


@pytest.mark.parametrize("code", [0, 1])
def test_hook_hands_commitlint_the_root_config_and_message_and_passes_its_exit_through(
    tmp_path, code
):
    hook, message = _root(tmp_path, fake_exit=code)
    # The fake commitlint is a shell script, so the hook's node probe is the only
    # node dependence; a PATH that resolves node keeps this about the arguments.
    node = shutil.which("node")
    assert node is not None, "the commit-msg hook needs `node` on PATH"
    result = _run(hook, message, path=f"/usr/bin:/bin:{Path(node).parent}")
    assert result.returncode == code, result.stderr
    assert (tmp_path / "argv.txt").read_text().splitlines() == [
        "--config",
        str(tmp_path / "commitlint.config.mjs"),
        "--edit",
        str(message),
    ]


def test_hook_fails_closed_naming_the_remedy_when_commitlint_is_absent(tmp_path):
    hook, message = _root(tmp_path, fake_exit=None)
    result = _run(hook, message)
    assert result.returncode != 0
    assert "make web-install" in result.stderr


def test_hook_fails_closed_naming_the_remedy_when_node_is_not_on_path(tmp_path):
    hook, message = _root(tmp_path, fake_exit=0)
    # A PATH holding only what the hook's own prologue needs (dirname), never node.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    dirname = shutil.which("dirname")
    assert dirname is not None
    (bin_dir / "dirname").symlink_to(dirname)
    result = _run(hook, message, path=str(bin_dir))
    assert result.returncode != 0
    assert "make web-install" in result.stderr
    assert not (tmp_path / "argv.txt").exists()
