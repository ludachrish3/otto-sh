"""Git plumbing used by coverage captures. All repos live in tmp_path."""

import ast
import subprocess
from pathlib import Path
from typing import ClassVar

import pytest

from otto.coverage.capture import gitio as gitio_module
from otto.coverage.capture.gitio import (
    GitUnavailableError,
    blob_exists,
    blob_sha,
    cat_blob,
    diff_no_index_u0,
    diff_worktree_file_u0,
    hash_object,
    head_commit,
    is_dirty,
    rev_list_first_parent,
    rev_parse_commit,
)


def _git_env(tmp_path: Path) -> dict[str, str]:
    """Return a hermetic git environment for testing."""
    return {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@x",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@x",
        "HOME": str(tmp_path),
        "PATH": "/usr/bin:/bin",
    }


def _make_repo(tmp_path: Path) -> tuple[Path, str]:
    """Initialize a repo with one commit, return (path, full_sha)."""
    root = tmp_path / "sut"
    root.mkdir()

    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            env=_git_env(tmp_path),
        )
        return result.stdout

    git("init", "-q")
    (root / "a.txt").write_text("initial content\n")
    git("add", "a.txt")
    git("commit", "-qm", "init")
    sha = head_commit(root)
    return root, sha


def _commit_all(repo_root: Path) -> str:
    """Stage and commit all changes, return new HEAD sha."""
    subprocess.run(
        ["git", "add", "-A"],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-qm", "change"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        env=_git_env(repo_root.parent),
    )
    return head_commit(repo_root)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "sut"
    root.mkdir()

    def git(*args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=root,
            check=True,
            capture_output=True,
            env=_git_env(tmp_path),
        )

    git("init", "-q")
    (root / "a.c").write_text("line1\nline2\nline3\n")
    git("add", "a.c")
    git("commit", "-qm", "init")
    return root


def test_head_and_dirty(repo: Path) -> None:
    sha = head_commit(repo)
    assert len(sha) == 40
    assert is_dirty(repo) is False
    (repo / "a.c").write_text("line1\nX\nline3\n")
    assert is_dirty(repo) is True


def test_blob_roundtrip(repo: Path) -> None:
    sha = blob_sha(repo, Path("a.c"))
    assert sha is not None
    assert blob_exists(repo, sha)
    assert cat_blob(repo, sha) == b"line1\nline2\nline3\n"
    assert hash_object(repo, repo / "a.c") == sha
    assert blob_sha(repo, Path("missing.c")) is None


def test_blob_sha_from_nested_subdir(repo: Path) -> None:
    # git resolves "REV:<path>" against the repo TOPLEVEL, not the cwd, so a
    # sut_dir nested inside a larger repo (the e2e bed: tests/repo1 inside
    # otto-sh) must still anchor its sources.  The cwd-relative "REV:./<path>"
    # spelling is required for this to work.
    nested = repo / "nested" / "sut"
    product = nested / "product"
    product.mkdir(parents=True)
    (product / "main.c").write_text("int main;\n")
    subprocess.run(
        ["git", "add", "nested"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-qm", "nest"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    sha = blob_sha(nested, Path("product/main.c"))

    assert sha == hash_object(repo, product / "main.c")
    assert blob_sha(nested, Path("product/missing.c")) is None


def test_worktree_diff_u0(repo: Path) -> None:
    (repo / "a.c").write_text("line1\nADDED\nline2\nline3\n")
    out = diff_worktree_file_u0(repo, Path("a.c"))
    assert "@@" in out
    assert "+ADDED" in out


def test_worktree_diff_ignores_whitespace_only_change(repo: Path) -> None:
    # A reindent of an untouched line produces no hunk: -w hides
    # whitespace-only modifications so the remapper never stales the line.
    (repo / "a.c").write_text("line1\n    line2\nline3\n")
    assert diff_worktree_file_u0(repo, Path("a.c")) == ""


def test_worktree_diff_ignores_whitespace_but_keeps_real_change(repo: Path) -> None:
    # Whitespace-only reindent of line 2 PLUS a real insertion: only the
    # insertion survives, with count-accurate coordinates for the remapper.
    # (line2 may appear in a hunk's @@ section-context header; what must NOT
    # appear is a +/- content line touching it.)
    (repo / "a.c").write_text("line1\n\tline2\nADDED\nline3\n")
    out = diff_worktree_file_u0(repo, Path("a.c"))
    assert "+ADDED" in out
    changed = [ln for ln in out.splitlines() if ln[:1] in "+-" and ln[:3] not in ("+++", "---")]
    assert changed == ["+ADDED"]  # the reindented line is not reported as changed


def test_no_index_diff_exit_1_ok(tmp_path: Path) -> None:
    a = tmp_path / "a.txt"
    a.write_text("x\n")
    b = tmp_path / "b.txt"
    b.write_text("y\n")
    out = diff_no_index_u0(a, b)
    assert "@@" in out
    assert diff_no_index_u0(a, a) == ""


def test_no_index_diff_ignores_whitespace_only(tmp_path: Path) -> None:
    a = tmp_path / "a.txt"
    a.write_text("int a;\nint b;\n")
    b = tmp_path / "b.txt"
    b.write_text("int a;\n        int b;\n")
    assert diff_no_index_u0(a, b) == ""


def test_not_a_repo_raises(tmp_path: Path) -> None:
    with pytest.raises(GitUnavailableError):
        head_commit(tmp_path)


def test_blob_sha_not_a_repo_raises(tmp_path: Path) -> None:
    with pytest.raises(GitUnavailableError):
        blob_sha(tmp_path, Path("a.c"))


def test_blob_exists_not_a_repo_raises(tmp_path: Path) -> None:
    with pytest.raises(GitUnavailableError):
        blob_exists(tmp_path, "0" * 40)


def test_rev_parse_commit_resolves_abbreviated_sha(tmp_path: Path) -> None:
    path, full = _make_repo(tmp_path)
    assert rev_parse_commit(path, full[:8]) == full


def test_rev_parse_commit_unresolvable_raises(tmp_path: Path) -> None:
    path, _ = _make_repo(tmp_path)
    with pytest.raises(GitUnavailableError):
        rev_parse_commit(path, "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef")


def test_rev_parse_commit_non_commit_raises(tmp_path: Path) -> None:
    path, _ = _make_repo(tmp_path)
    with pytest.raises(GitUnavailableError):
        rev_parse_commit(path, "not-a-ref")


def test_rev_list_first_parent_newest_first(tmp_path: Path) -> None:
    path, first = _make_repo(tmp_path)
    (path / "b.txt").write_text("x")
    second = _commit_all(path)
    assert rev_list_first_parent(path) == [second, first]


class TestEveryPorcelainCallIsPinned:
    """Structural guard: a new porcelain git call cannot skip the config pins.

    The ``_pin``/``_pin_no_index`` wrappers exist because porcelain output
    is shaped by the invoking user's ``~/.gitconfig`` (``color.ui``,
    ``diff.mnemonicPrefix``, ``diff.external``, ``log.showSignature``), and
    every failure of that class found so far has been *silent* — a parser
    that matches nothing reads as "no changes" rather than raising. Nothing
    stopped the next ``_run(["diff", ...])`` from reappearing unpinned, and
    no behavioural test can catch it: the whole suite would have to run
    under a decorated global config to notice.

    So this asserts on the module's own AST instead. Every call to a
    ``_run*`` helper must pass either a ``_pin``/``_pin_no_index`` call or
    an argv whose subcommand is *plumbing* — ``rev-parse``, ``cat-file``,
    ``hash-object``, ``status --porcelain`` and friends have output
    contracts git commits to keeping stable and machine-readable, which is
    exactly what porcelain lacks.
    """

    RUNNERS: ClassVar[set[str]] = {"_run", "_run_raw", "_run_raw_input"}
    PINNERS: ClassVar[set[str]] = {"_pin", "_pin_no_index"}
    # Output shaped by user config -> must be pinned.
    PORCELAIN: ClassVar[set[str]] = {"diff", "log", "show", "whatchanged", "blame", "shortlog"}

    def _run_calls(self):
        tree = ast.parse(Path(gitio_module.__file__).read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in self.RUNNERS
                and node.args
            ):
                yield node, node.args[0]

    def test_at_least_one_call_of_each_kind_is_present(self):
        """Fixture property: the walk must actually see both shapes.

        Without this the guard below would pass vacuously if the AST walk
        silently stopped matching (a renamed runner, a refactor to kwargs).
        """
        kinds = {type(arg).__name__ for _node, arg in self._run_calls()}
        assert {"Call", "List"} <= kinds, f"AST walk found only {kinds}"

    def test_no_porcelain_call_bypasses_the_pin(self):
        offenders = []
        for _node, arg in self._run_calls():
            if isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name):
                if arg.func.id in self.PINNERS:
                    continue
                offenders.append(f"unrecognised argv builder {arg.func.id}()")
                continue
            if isinstance(arg, ast.List) and arg.elts:
                first = arg.elts[0]
                if isinstance(first, ast.Constant) and first.value in self.PORCELAIN:
                    offenders.append(
                        f"line {arg.lineno}: git {first.value} runs unpinned — "
                        f"wrap it in _pin()/_pin_no_index()"
                    )
        assert not offenders, "porcelain git calls bypassing the config pin:\n" + "\n".join(
            offenders
        )
