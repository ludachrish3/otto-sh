"""AnchorResolver: one tree diff answers the whole capture; blob chain is the fallback."""

import subprocess
from pathlib import Path
from typing import ClassVar

import pytest

from otto.coverage.anchor import AnchorResolver
from otto.coverage.capture import gitio
from otto.coverage.capture.model import CaptureFileCov
from tests._fixtures.gitrepo import git_env


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env=git_env(cwd),
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


class TestHostileGlobalGitConfig:
    """``git diff --no-index`` runs outside any repo but still reads ``~/.gitconfig``.

    The two ``--no-index`` calls behind the blob fallback were originally
    left unpinned on the reasoning that they "run outside any repo on
    throwaway anchor files", so no repo config could reach them. That
    reasoning is wrong: git still loads the invoking user's **global**
    config, and two common settings each corrupt the output these paths
    parse.

    - ``color.ui = always`` wraps every line in ANSI escapes, defeating
      the ``diff --git``/``---``/``@@`` prefix matching in
      :func:`~otto.coverage.capture.treediff.parse_multifile_u0` and
      :func:`~otto.coverage.capture.remap.parse_u0_hunks`.
    - ``diff.mnemonicPrefix = true`` renders ``1/``/``2/`` path prefixes
      here — note the *different letters* from the ``c/``/``w/`` a repo
      diff emits, so the pin added for repo-side diffs does not cover
      this — and the batched index's ``<dir_a.name>/`` lookup then misses.

    Both corruptions land on the same branch: a diff that fails to parse
    yields no hunks, which ``_build_fallback_index`` and
    ``_resolve_by_blob`` read as "absent from the ``-w`` diff, therefore
    whitespace-only/verbatim". The file below really did change on line 2,
    so a silent degradation reports stale manual coverage as still valid.

    The hostile settings are **global** (``GIT_CONFIG_GLOBAL``), not
    repo-local, because that is the only channel that reaches a
    ``--no-index`` invocation. ``_git`` keeps its own hermetic env, so the
    fixture repo is still built under clean config — only the production
    calls under test see the hostile file.

    Asserting on hunk counts rather than lazy/batched parity is
    deliberate: under a corrupting config *both* paths degrade to verbatim
    identically, so a parity assertion stays green and proves nothing.
    """

    UNRESOLVABLE_BASE = "0" * 40

    SETTINGS: ClassVar[list] = [
        # Silent: ANSI escapes defeat every prefix match, so nothing parses.
        pytest.param("[color]\n\tui = always\n", id="color.ui=always"),
        # Silent: 1/ and 2/ prefixes, so the batched index's path lookup misses.
        pytest.param("[diff]\n\tmnemonicPrefix = true\n", id="diff.mnemonicPrefix=true"),
        # Loud: the helper replaces git's diff engine and exits nonzero.
        pytest.param("[diff]\n\texternal = /bin/false\n", id="diff.external"),
    ]

    @pytest.fixture
    def hostile(self, request, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = tmp_path / "hostile.gitconfig"
        cfg.write_text(request.param)
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
        monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")

    @pytest.mark.parametrize("hostile", SETTINGS, indirect=True)
    def test_lazy_blob_chain_still_finds_the_edit(self, repo, hostile):
        root, _base, fc = repo
        (root / "a.c").write_text("l1\nCHANGED\nl3\nl4\n")

        r = AnchorResolver(root, self.UNRESOLVABLE_BASE).resolve(Path("a.c"), fc)

        assert r.verifiable
        assert len(r.hunks) == 1, "hostile global config emptied the per-file blob diff"

    @pytest.mark.parametrize("hostile", SETTINGS, indirect=True)
    def test_batched_fallback_index_still_finds_the_edit(self, repo, hostile):
        root, _base, fc = repo
        (root / "a.c").write_text("l1\nCHANGED\nl3\nl4\n")

        r = AnchorResolver(root, self.UNRESOLVABLE_BASE, files={"a.c": fc}).resolve(Path("a.c"), fc)

        assert r.verifiable
        assert len(r.hunks) == 1, "hostile global config emptied the batched dir diff"


class TestBatchedVsLazyParity:
    """Under an unresolvable base, ``files=`` (batched) must match the lazy chain exactly.

    Same scenarios as TestFallbackPath, each resolved twice: once with a
    bare resolver (forces ``_resolve_by_blob``) and once with the capture's
    ``files`` passed through (forces the batched index) — results must be
    byte-identical ``AnchorResult``s.
    """

    UNRESOLVABLE_BASE = "0" * 40

    def _assert_parity(self, root: Path, fc: CaptureFileCov, relpath: Path = Path("a.c")):
        """Parity AND value: returns the lazy result so each test can assert
        its named scenario's semantic outcome. Parity alone collapses
        two-sidedly — a defect making BOTH arms return the same wrong
        AnchorResult (e.g. unverifiable-for-everything) kept all five tests
        green (review §4.1)."""
        lazy = AnchorResolver(root, self.UNRESOLVABLE_BASE).resolve(relpath, fc)
        batched = AnchorResolver(
            root, self.UNRESOLVABLE_BASE, files={relpath.as_posix(): fc}
        ).resolve(relpath, fc)
        assert lazy == batched
        return lazy

    def test_unchanged_is_verbatim(self, repo):
        root, _base, fc = repo
        r = self._assert_parity(root, fc)
        assert (r.new_relpath, r.hunks, r.verifiable) == (Path("a.c"), [], True)

    def test_whitespace_only_is_verbatim(self, repo):
        root, _base, fc = repo
        (root / "a.c").write_text("l1\n    l2\nl3\nl4\n")  # reindent only
        r = self._assert_parity(root, fc)
        assert r.verifiable
        assert r.hunks == []

    def test_modified_yields_same_hunks(self, repo):
        root, _base, fc = repo
        (root / "a.c").write_text("l1\nCHANGED\nl3\nl4\n")
        r = self._assert_parity(root, fc)
        assert r.verifiable
        assert len(r.hunks) == 1

    def test_deleted_current_is_unverifiable(self, repo):
        root, _base, fc = repo
        (root / "a.c").unlink()
        r = self._assert_parity(root, fc)
        assert not r.verifiable

    def test_absent_base_blob_is_unverifiable(self, repo):
        root, _base, _fc = repo
        fc = CaptureFileCov(blob="1" * 40, lines={1: 1})
        (root / "a.c").write_text("different\n")
        r = self._assert_parity(root, fc)
        assert not r.verifiable
