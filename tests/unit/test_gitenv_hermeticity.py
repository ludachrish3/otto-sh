"""Hermetic-git-env policy: one env builder, no hand-rolled copies (review §7.2).

The defect class: at adoption 22 files spawned git their own way, in five
spellings — 19 re-typed an env dict (module ``_GIT_ENV`` constants, local
``_git_env()`` builders, inline dicts, ``{**_GIT_ENV, "HOME": …}`` merges)
and only 6 of those neutered ``GIT_CONFIG_GLOBAL``/``GIT_CONFIG_SYSTEM``;
3 more passed no env at all (full ambient inheritance).  Every un-neutered
spawn leaves a developer's global ``commit.gpgsign`` or an ``/etc/gitconfig``
``core.hooksPath`` live — an opaque ``CalledProcessError`` waiting for the
right workstation (``test_changelog_rendering.py`` documented the hazard; it
never propagated).  Hermeticity must be ONE decision in
``tests/_fixtures/gitrepo.py``, because copies drop the scar key first (the
``run_otto`` lesson, §7.1).

Scan arms:

- dict literal with a ``"GIT_*"`` string key  (the dominant shape)
- ``dict(GIT_AUTHOR_NAME=..., ...)`` keyword form
- subscript writes ``env["GIT_..."] = ...`` (incremental construction)
- a ``subprocess``-style call whose argv (positional or ``args=``, list or
  tuple) literally starts with ``"git"`` and whose ``env=`` is AMBIENT:
  missing entirely, ``None``, ``os.environ``, ``os.environ.copy()``,
  ``dict(os.environ)``, or a dict display unpacking ``**os.environ`` — the
  ``env={**os.environ, "LC_ALL": "C"}`` idiom inherits the developer's
  gitconfig exactly like passing nothing (found as a review escape, not
  live; ``.copy()`` was fable's)

Blind spots, stated: keys built by string concatenation or variables
(``"GIT_" + name``), ``os.environ.update({...})`` with a pre-built variable,
argv lists bound to a variable before the call (``cmd = ["git", ...]``),
``shell=True`` string commands, a merge of a VARIABLE that happens to hold
ambient env (``env={**base}``), PEP-584 ``os.environ | {...}`` and nested
ambient copies (``{**dict(os.environ)}``), and git spawned outside
``tests/`` — each grepped zero at adoption.  The scan is a tripwire against the observed drift
shapes, not a proof system.  ``monkeypatch.setenv("GIT_...")`` is
deliberately NOT flagged: patching the ambient process env is a different
(sanctioned) idiom with its own guard.
"""

import ast
from pathlib import Path

import pytest

from tests._fixtures.paths import TESTS_ROOT

_FIXTURE_MODULE = TESTS_ROOT / "_fixtures" / "gitrepo.py"

# Fixture SUT repos + firmware: user-example input data, not otto's tests —
# the same carve-out every tests-scoped structural rule makes (the house
# denylist; see test_bed_oracle_honesty and the tests-scoped ast-grep rules).
_EXCLUDED_TREES = ("repo1", "repo2", "repo3", "repo4", "repo_broken", "repo_e2e", "firmware")


def _parsed_or_fail(path: Path) -> ast.AST:
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as e:
        pytest.fail(
            f"{path} is unparseable ({e}) — if it is corpus input data, add its "
            "top-level tree to _EXCLUDED_TREES; if it is suite code, fix it"
        )


def _suite_files() -> list[Path]:
    self_path = Path(__file__).resolve()
    files = [
        path
        for path in sorted(TESTS_ROOT.rglob("*.py"))
        if not any(part in _EXCLUDED_TREES for part in path.relative_to(TESTS_ROOT).parts)
        and path.resolve() != self_path
        and path != _FIXTURE_MODULE
    ]
    # Anti-vacuity: the tree is large; a scan that suddenly sees far fewer
    # files has lost its corpus, not fixed the offenders.
    assert len(files) > 400, f"scan corpus collapsed: only {len(files)} files seen"
    return files


_SPAWN_NAMES = {"run", "check_output", "check_call", "call", "Popen"}


def _is_os_environ(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "environ"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def _is_ambient_env(value: ast.AST) -> bool:
    """env= spellings that still inherit the developer's gitconfig."""
    if isinstance(value, ast.Constant) and value.value is None:
        return True
    if _is_os_environ(value):
        return True
    if (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Attribute)
        and value.func.attr == "copy"
        and _is_os_environ(value.func.value)
    ):
        return True
    if (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id == "dict"
        and value.args
        and _is_os_environ(value.args[0])
    ):
        return True
    if isinstance(value, ast.Dict):
        return any(
            key is None and _is_os_environ(val)
            for key, val in zip(value.keys, value.values, strict=True)
        )
    return False


def _is_ambient_git_spawn(node: ast.Call) -> bool:
    """A subprocess-style call whose argv literally starts with "git" and
    whose child would see the developer's environment (no ``env=``, or an
    ``env=`` that reproduces ``os.environ``)."""
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
    if name not in _SPAWN_NAMES:
        return False
    argv = node.args[0] if node.args else None
    if argv is None:
        argv = next((kw.value for kw in node.keywords if kw.arg == "args"), None)
    if not (isinstance(argv, (ast.List, ast.Tuple)) and argv.elts):
        return False
    first = argv.elts[0]
    if not (isinstance(first, ast.Constant) and first.value == "git"):
        return False
    env_kw = next((kw for kw in node.keywords if kw.arg == "env"), None)
    if env_kw is None:
        return True
    return _is_ambient_env(env_kw.value)


def _git_env_offences(tree: ast.AST) -> list[int]:
    """Line numbers of GIT_* env-construction / ambient-git-spawn sites."""
    hits: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key in node.keys:
                if (
                    isinstance(key, ast.Constant)
                    and isinstance(key.value, str)
                    and key.value.startswith("GIT_")
                ):
                    hits.append(node.lineno)
                    break
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == "dict":
                if any(kw.arg is not None and kw.arg.startswith("GIT_") for kw in node.keywords):
                    hits.append(node.lineno)
            elif _is_ambient_git_spawn(node):
                hits.append(node.lineno)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.slice, ast.Constant)
                    and isinstance(target.slice.value, str)
                    and target.slice.value.startswith("GIT_")
                ):
                    hits.append(node.lineno)
                    break
    return sorted(set(hits))


def test_no_hand_rolled_git_envs_outside_the_fixture():
    offenders: list[str] = []
    for path in _suite_files():
        tree = _parsed_or_fail(path)
        offenders.extend(
            f"{path.relative_to(TESTS_ROOT)}:{lineno}" for lineno in _git_env_offences(tree)
        )
    assert offenders == [], (
        f"{len(offenders)} hand-rolled GIT_* env site(s) outside "
        "tests/_fixtures/gitrepo.py — build the env via gitrepo.git_env(home) "
        "(hermeticity is one decision, copies drop the scar key first):\n  "
        + "\n  ".join(offenders)
    )


@pytest.mark.parametrize(
    ("snippet", "expected_lines"),
    [
        ('E = {\n "GIT_AUTHOR_NAME": "t",\n}\n', [1]),
        ('e = dict(GIT_CONFIG_GLOBAL="/dev/null")\n', [1]),
        ('env = {}\nenv["GIT_AUTHOR_DATE"] = "x"\n', [2]),
        ('subprocess.run(["git", "init"], cwd=p)\n', [1]),
        ('from subprocess import run\nrun(["git", *args], check=True)\n', [2]),
        # Ambient env= spellings inherit gitconfig exactly like no env=.
        ('subprocess.run(["git", "s"], env={**os.environ, "LC_ALL": "C"})\n', [1]),
        ('subprocess.run(["git", "s"], env=os.environ)\n', [1]),
        ('subprocess.run(["git", "s"], env=dict(os.environ))\n', [1]),
        ('subprocess.run(["git", "s"], env=os.environ.copy())\n', [1]),
        ('subprocess.run(["git", "s"], env=None)\n', [1]),
        ('subprocess.run(("git", "init"), cwd=p)\n', [1]),
        ('subprocess.run(args=["git", "init"], cwd=p)\n', [1]),
        # Negative controls: non-GIT keys, non-env dicts, hermetic and
        # non-git spawns must not fire.
        ('E = {\n "PATH": "/usr/bin",\n}\n', []),
        ('e = dict(HOME="/tmp")\nother["PATH"] = "x"\n', []),
        ('subprocess.run(["git", "init"], env=git_env(p))\n', []),
        ('subprocess.run(["git", "s"], env={**base, "HOME": str(p)})\n', []),
        ('subprocess.run(["otto", "test"], cwd=p)\n', []),
    ],
)
def test_scanner_positive_and_negative_controls(snippet, expected_lines):
    assert _git_env_offences(ast.parse(snippet)) == expected_lines
