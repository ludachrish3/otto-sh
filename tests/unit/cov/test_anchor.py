"""AnchorResolver: one tree diff answers the whole capture; blob chain is the fallback."""

import subprocess
from pathlib import Path

import pytest

from otto.coverage.anchor import AnchorResolver
from otto.coverage.capture import gitio
from otto.coverage.capture.model import CaptureFileCov


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "HOME": str(cwd),
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "PATH": "/usr/bin:/bin",
        },
    ).stdout


@pytest.fixture
def repo(tmp_path: Path):
    root = tmp_path / "r"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "a.c").write_text("l1\nl2\nl3\nl4\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    base = _git(root, "rev-parse", "HEAD").strip()
    blob = gitio.blob_sha(root, Path("a.c"))
    return root, base, CaptureFileCov(blob=blob, lines={1: 5, 3: 2})


class TestTreeDiffPath:
    def test_unchanged_file_is_verbatim(self, repo):
        root, base, fc = repo
        r = AnchorResolver(root, base).resolve(Path("a.c"), fc)
        assert (r.new_relpath, r.hunks, r.verifiable) == (Path("a.c"), [], True)

    def test_clean_rename_redirects_path(self, repo):
        root, base, fc = repo
        _git(root, "mv", "a.c", "moved.c")
        _git(root, "commit", "-qm", "mv")
        r = AnchorResolver(root, base).resolve(Path("a.c"), fc)
        assert r.new_relpath == Path("moved.c")
        assert r.verifiable
        assert r.hunks == []

    def test_edit_produces_hunks(self, repo):
        root, base, fc = repo
        (root / "a.c").write_text("l1\nCHANGED\nl3\nl4\n")
        r = AnchorResolver(root, base).resolve(Path("a.c"), fc)
        assert r.verifiable
        assert len(r.hunks) == 1

    def test_deleted_file_is_unverifiable(self, repo):
        root, base, fc = repo
        _git(root, "rm", "-q", "a.c")
        _git(root, "commit", "-qm", "rm")
        r = AnchorResolver(root, base).resolve(Path("a.c"), fc)
        assert not r.verifiable
        assert r.new_relpath is None


class TestFallbackPath:
    def test_missing_base_with_matching_blob_is_verbatim(self, repo):
        # Squash-merge shape: base commit gone, file content unchanged.
        root, _base, fc = repo
        r = AnchorResolver(root, "0" * 40).resolve(Path("a.c"), fc)
        assert (r.new_relpath, r.hunks, r.verifiable) == (Path("a.c"), [], True)

    def test_missing_base_and_changed_blob_diffs_via_blob(self, repo):
        root, _base, fc = repo
        (root / "a.c").write_text("l1\nCHANGED\nl3\nl4\n")
        r = AnchorResolver(root, "0" * 40).resolve(Path("a.c"), fc)
        assert r.verifiable
        assert len(r.hunks) == 1

    def test_missing_base_and_missing_blob_is_unverifiable(self, repo):
        root, _base, _ = repo
        fc = CaptureFileCov(blob="1" * 40, lines={1: 1})
        (root / "a.c").write_text("different\n")
        r = AnchorResolver(root, "0" * 40).resolve(Path("a.c"), fc)
        assert not r.verifiable


class TestBatchedVsLazyParity:
    """Under an unresolvable base, ``files=`` (batched) must match the lazy chain exactly.

    Same scenarios as TestFallbackPath, each resolved twice: once with a
    bare resolver (forces ``_resolve_by_blob``) and once with the capture's
    ``files`` passed through (forces the batched index) — results must be
    byte-identical ``AnchorResult``s.
    """

    UNRESOLVABLE_BASE = "0" * 40

    def _assert_parity(self, root: Path, fc: CaptureFileCov, relpath: Path = Path("a.c")) -> None:
        lazy = AnchorResolver(root, self.UNRESOLVABLE_BASE).resolve(relpath, fc)
        batched = AnchorResolver(
            root, self.UNRESOLVABLE_BASE, files={relpath.as_posix(): fc}
        ).resolve(relpath, fc)
        assert lazy == batched

    def test_unchanged_is_verbatim(self, repo):
        root, _base, fc = repo
        self._assert_parity(root, fc)

    def test_whitespace_only_is_verbatim(self, repo):
        root, _base, fc = repo
        (root / "a.c").write_text("l1\n    l2\nl3\nl4\n")  # reindent only
        self._assert_parity(root, fc)

    def test_modified_yields_same_hunks(self, repo):
        root, _base, fc = repo
        (root / "a.c").write_text("l1\nCHANGED\nl3\nl4\n")
        self._assert_parity(root, fc)

    def test_deleted_current_is_unverifiable(self, repo):
        root, _base, fc = repo
        (root / "a.c").unlink()
        self._assert_parity(root, fc)

    def test_absent_base_blob_is_unverifiable(self, repo):
        root, _base, _fc = repo
        fc = CaptureFileCov(blob="1" * 40, lines={1: 1})
        (root / "a.c").write_text("different\n")
        self._assert_parity(root, fc)
