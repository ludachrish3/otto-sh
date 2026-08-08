"""Pins for scripts/gate_fresh.py — the pristine-worktree gate.

The gate exists because the dev tree is a superset of CI's environment
(gitignored build artifacts CI never has), so these tests build real throwaway
git repos in tmp_path rather than inspecting the dev repo, which carries
exactly the dirt under test.
"""

import io
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from scripts import gate_fresh
from scripts.gate_fresh import (
    GateFreshError,
    GateResult,
    gate,
    main,
    ref_to_gate,
    resolve_ref,
    tree_state,
)
from tests._fixtures.paths import PROJECT_ROOT


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "tracked.txt").write_text("v1\n")
    (repo / ".gitignore").write_text("ignored/\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


@pytest.fixture
def track_holders(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[Path]]:
    """Spy on tempfile.mkdtemp, and sweep any holder the test leaves behind.

    ``gate()`` only ever hands back ``worktree`` (the ``holder/"tree"`` leaf),
    never ``holder`` itself, so a test that wants to assert the holder is
    cleaned up has to observe the mkdtemp call directly.

    Teardown is why this is a fixture rather than a helper. The kept-on-failure
    test deliberately leaves its worktree — that is the behaviour under test —
    and ``holder`` lives in system temp, outside ``tmp_path``, so nothing else
    reclaims it: the suite dropped one empty dir into /tmp on every run.
    Sweeping here also covers a test that fails its assertions mid-way.

    It removes ONLY holders this spy recorded, never a glob of
    ``/tmp/otto-gate-fresh-*``. A developer debugging a red gate has a real kept
    worktree sitting under that pattern, and a glob would delete it out from
    under them mid-session.
    """
    created: list[Path] = []
    real_mkdtemp = tempfile.mkdtemp

    def spy(*args: object, **kwargs: object) -> str:
        path = real_mkdtemp(*args, **kwargs)
        created.append(Path(path))
        return path

    monkeypatch.setattr(tempfile, "mkdtemp", spy)
    yield created
    for holder in created:
        shutil.rmtree(holder, ignore_errors=True)


class TestTreeState:
    def test_clean_repo_reports_nothing(self, tmp_path: Path):
        state = tree_state(_repo(tmp_path))
        assert state.tracked_dirty == []
        assert state.untracked == []

    def test_modified_tracked_file_is_reported(self, tmp_path: Path):
        repo = _repo(tmp_path)
        (repo / "tracked.txt").write_text("v2\n")
        assert tree_state(repo).tracked_dirty == ["tracked.txt"]

    def test_staged_tracked_file_is_reported(self, tmp_path: Path):
        repo = _repo(tmp_path)
        (repo / "tracked.txt").write_text("v2\n")
        _git(repo, "add", "tracked.txt")
        assert tree_state(repo).tracked_dirty == ["tracked.txt"]

    def test_renamed_file_reports_destination_path(self, tmp_path: Path):
        repo = _repo(tmp_path)
        _git(repo, "mv", "tracked.txt", "renamed.txt")
        # git mv stages the change, so the rename is tracked_dirty.
        # tree_state should extract the destination path (renamed.txt), not the
        # double-path string (tracked.txt -> renamed.txt).
        assert tree_state(repo).tracked_dirty == ["renamed.txt"]

    def test_renamed_and_modified_file_reports_destination_path(self, tmp_path: Path):
        # A rename staged with `git mv`, then edited again, reports status code
        # "RM" (rename in the index column, modified in the worktree column).
        # A bare `code[0] in ("R", "C")` check still catches this one, but a
        # bare `code in ("R ", "C ")` equality check (the first fix attempt)
        # does not — pinning it here regardless of parsing strategy.
        repo = _repo(tmp_path)
        _git(repo, "mv", "tracked.txt", "renamed.txt")
        _git(repo, "add", "renamed.txt")
        (repo / "renamed.txt").write_text("v2\n")
        assert tree_state(repo).tracked_dirty == ["renamed.txt"]

    def test_rename_detected_only_in_worktree_column_reports_destination(self, tmp_path: Path):
        # The "R" can land in the *second* status column instead of the
        # first: delete a tracked file from the worktree only (a plain `rm`,
        # leaving the index entry alone — an unstaged deletion), then stage a
        # similarly-named new path with `git add -N` (intent-to-add, so its
        # content stays on disk rather than in the index). git's unstaged
        # rename detection then pairs the two, emitting " R" (space, R) for
        # the surviving path. A `code[0] in ("R", "C")` check (the second fix
        # attempt) misses this because the R is in code[1], not code[0].
        repo = _repo(tmp_path)
        (repo / "tracked.txt").unlink()
        (repo / "renamed.txt").write_text("v1\n")
        _git(repo, "add", "-N", "renamed.txt")
        state = tree_state(repo)
        assert state.tracked_dirty == ["renamed.txt"]
        assert state.untracked == []

    def test_modified_file_with_space_in_name_is_reported(self, tmp_path: Path):
        # The newline-delimited porcelain form has no quoting for spaces, so a
        # naive line-based parser can't tell a path's internal space from the
        # "code SP path" separator. `-z` sidesteps this: the path is a single
        # NUL-terminated field regardless of its contents.
        repo = _repo(tmp_path)
        (repo / "has space.txt").write_text("v1\n")
        _git(repo, "add", "has space.txt")
        _git(repo, "commit", "-qm", "add spaced file")
        (repo / "has space.txt").write_text("v2\n")
        assert tree_state(repo).tracked_dirty == ["has space.txt"]

    def test_untracked_file_is_separated_from_tracked_dirt(self, tmp_path: Path):
        # The whole point of the split: untracked files must NOT block, because
        # the gate going red on a forgotten `git add` is the feature.
        repo = _repo(tmp_path)
        (repo / "new.py").write_text("x = 1\n")
        state = tree_state(repo)
        assert state.tracked_dirty == []
        assert state.untracked == ["new.py"]

    def test_gitignored_file_is_neither(self, tmp_path: Path):
        repo = _repo(tmp_path)
        (repo / "ignored").mkdir()
        (repo / "ignored" / "artifact.js").write_text("//\n")
        state = tree_state(repo)
        assert state.tracked_dirty == []
        assert state.untracked == []

    def test_subdirectory_of_repo_works(self, tmp_path: Path):
        repo = _repo(tmp_path)
        subdir = repo / "subdir"
        subdir.mkdir()
        (subdir / "nested.txt").write_text("nested content\n")
        # tree_state should work when called on a subdirectory, since git can
        # resolve the repo root via upward search (using git rev-parse).
        # The untracked directory is reported as "subdir/" when git runs from the
        # repo, or as "subdir/" when git runs from subdir.
        state = tree_state(subdir)
        assert "subdir" in state.untracked[0]

    def test_non_repo_raises(self, tmp_path: Path):
        with pytest.raises(GateFreshError, match="not a git repository"):
            tree_state(tmp_path / "nope")


class TestResolveRef:
    def test_head_resolves_to_a_full_sha(self, tmp_path: Path):
        repo = _repo(tmp_path)
        sha = resolve_ref(repo, "HEAD")
        assert len(sha) == 40
        assert sha == _git(repo, "rev-parse", "HEAD").strip()

    def test_branch_name_resolves(self, tmp_path: Path):
        repo = _repo(tmp_path)
        assert resolve_ref(repo, "main") == resolve_ref(repo, "HEAD")

    def test_unresolvable_ref_refuses_by_name(self, tmp_path: Path):
        repo = _repo(tmp_path)
        with pytest.raises(GateFreshError, match="no-such-ref"):
            resolve_ref(repo, "no-such-ref")

    def test_tag_pointing_at_a_tree_is_refused(self, tmp_path: Path):
        # A tag can point at any object type, not just a commit. Handing
        # `git worktree add` a tree or blob would silently gate the wrong
        # thing, so resolve_ref must peel with ^{commit} and reject a tag
        # that doesn't dereference to one. Verified empirically: a bare
        # `git rev-parse --verify treetag` exits 0 with the tree's own sha,
        # while `git rev-parse --verify treetag^{commit}` exits 128 with
        # "expected commit type, but the object dereferences to tree type".
        repo = _repo(tmp_path)
        tree_sha = _git(repo, "rev-parse", "HEAD^{tree}").strip()
        _git(repo, "tag", "treetag", tree_sha)
        with pytest.raises(GateFreshError, match="treetag"):
            resolve_ref(repo, "treetag")

    def test_annotated_tag_resolves_to_the_underlying_commit(self, tmp_path: Path):
        # An annotated tag is its own object, distinct from the commit it
        # points at: a bare `git rev-parse anno` returns the tag object's
        # own sha, not the commit's. resolve_ref must peel through it so
        # callers get something `git worktree add` can check out.
        repo = _repo(tmp_path)
        _git(repo, "tag", "-a", "anno", "-m", "annotated", "HEAD")
        tag_object_sha = _git(repo, "rev-parse", "anno").strip()
        assert resolve_ref(repo, "anno") == resolve_ref(repo, "HEAD")
        assert resolve_ref(repo, "anno") != tag_object_sha


class TestGateLifecycle:
    def test_worktree_is_removed_on_success(self, tmp_path: Path):
        repo = _repo(tmp_path)
        seen: list[Path] = []

        def runner(wt: Path, targets: list[str]) -> bool:
            seen.append(wt)
            assert (wt / "tracked.txt").exists(), "worktree must hold the gated commit"
            return True

        result = gate(repo, resolve_ref(repo, "HEAD"), runner=runner)
        assert isinstance(result, GateResult)
        assert result.passed is True
        assert result.kept is False
        assert not seen[0].exists(), "a passing gate leaves nothing behind"
        # A leaked registration prints as e.g.
        # "/tmp/otto-gate-fresh-XXXX/tree ... prunable" — it never contains the
        # literal substring "worktrees", so a plain `not in` check here passes
        # even against a rewrite that leaks one on every green run. Count
        # lines instead, following the same idiom the exception-path test
        # below already uses.
        assert _git(repo, "worktree", "list").count("\n") == 1, "only the main checkout remains"

    def test_worktree_is_kept_on_failure(self, tmp_path: Path, track_holders: list[Path]):
        # A red gate should hand back a live tree to debug in, with the failing
        # reports still on disk. track_holders sweeps the holder afterwards:
        # this is the one test that legitimately leaves a worktree behind, and
        # the holder lives in system temp where tmp_path cleanup cannot reach it.
        repo = _repo(tmp_path)
        result = gate(repo, resolve_ref(repo, "HEAD"), runner=lambda wt, targets: False)
        assert result.passed is False
        assert result.kept is True
        assert result.worktree.exists()
        assert track_holders, "gate() must call tempfile.mkdtemp"

    def test_runner_receives_the_gated_targets_in_order(self, tmp_path: Path):
        repo = _repo(tmp_path)
        captured: list[list[str]] = []
        gate(
            repo,
            resolve_ref(repo, "HEAD"),
            runner=lambda wt, targets: captured.append(targets) or True,
        )
        assert captured == [["lint-python", "lint-arch", "typecheck-python", "coverage-hostless"]]

    def test_an_exception_mid_run_leaks_no_registered_worktree(self, tmp_path: Path):
        repo = _repo(tmp_path)

        def boom(wt: Path, targets: list[str]) -> bool:
            raise RuntimeError("harness died")

        with pytest.raises(RuntimeError, match="harness died"):
            gate(repo, resolve_ref(repo, "HEAD"), runner=boom)
        assert _git(repo, "worktree", "list").count("\n") == 1, "only the main checkout remains"

    def test_holder_directory_is_removed_on_success(
        self, tmp_path: Path, track_holders: list[Path]
    ):
        # git worktree remove only unregisters and deletes the "tree" leaf it
        # was given — the tempfile.mkdtemp() parent ("holder") that leaf lives
        # in is never touched by git, so a passing gate must remove it itself
        # or it leaks an empty dir into system temp on every single run.
        repo = _repo(tmp_path)
        gate(repo, resolve_ref(repo, "HEAD"), runner=lambda wt, targets: True)

        assert track_holders, "gate() must call tempfile.mkdtemp"
        assert not track_holders[0].exists(), "the holder dir must not survive a passing gate"

    def test_worktree_add_failure_leaves_no_holder_directory(
        self, tmp_path: Path, track_holders: list[Path]
    ):
        # git worktree add fails before creating anything on disk (observed:
        # "fatal: invalid reference" on a bad sha, no leaf dir created). But
        # the holder dir from tempfile.mkdtemp() is created *before* that
        # call, so a failed add must still clean it up itself.
        repo = _repo(tmp_path)

        def unreachable(wt: Path, targets: list[str]) -> bool:
            raise AssertionError("runner must not run when worktree add itself fails")

        with pytest.raises(GateFreshError, match="invalid reference"):
            gate(repo, "not-a-real-ref-at-all", runner=unreachable)

        assert track_holders, "gate() must call tempfile.mkdtemp before worktree add"
        assert not track_holders[0].exists(), "a failed worktree add must not leak its holder dir"

    def test_cleanup_failure_during_exception_handling_does_not_mask_original_exception(
        self, tmp_path: Path
    ):
        # Corrupting the worktree's `.git` file makes `git worktree remove
        # --force` itself fail (observed: "fatal: validation failed, cannot
        # remove working tree: ... does not exist"). That cleanup failure
        # must not replace the runner's own exception as what the caller
        # sees, AND must not leave a stale "prunable" registration behind
        # either: deleting the holder from disk regardless (so the failed
        # worktree doesn't linger) would otherwise orphan git's own admin
        # metadata at .git/worktrees/tree, forever showing up in
        # `git worktree list` as "prunable gitdir file points to
        # non-existent location" until someone runs `git worktree prune` by
        # hand — the exact registration leak this branch exists to prevent,
        # reintroduced through a different route.
        repo = _repo(tmp_path)

        def boom(wt: Path, targets: list[str]) -> bool:
            (wt / ".git").unlink()
            raise RuntimeError("harness died")

        with pytest.raises(RuntimeError, match="harness died") as exc_info:
            gate(repo, resolve_ref(repo, "HEAD"), runner=boom)
        assert exc_info.value.__context__ is None, (
            "a cleanup failure must not be chained onto the original exception"
        )
        worktree_list = _git(repo, "worktree", "list")
        assert worktree_list.count("\n") == 1, "only the main checkout remains"
        assert "prunable" not in worktree_list, "no stale worktree registration should survive"

    def test_cleanup_failure_after_a_pass_raises_and_leaves_no_registration(
        self, tmp_path: Path, track_holders: list[Path]
    ):
        # Mirrors test_cleanup_failure_during_exception_handling_does_not_mask_original_exception
        # above, but for the PASS branch: corrupting the worktree's `.git`
        # file makes `git worktree remove --force` itself fail even though
        # the run passed. Unlike the exception branch (which is protecting a
        # more important exception already in flight), there is nothing else
        # to protect here, so the cleanup failure must surface as its own
        # GateFreshError rather than reporting itself as a clean PASS — but
        # it must still reconcile the holder dir and git's own admin
        # metadata, exactly as the exception branch does.
        repo = _repo(tmp_path)

        def passes_but_corrupts(wt: Path, targets: list[str]) -> bool:
            (wt / ".git").unlink()
            return True

        with pytest.raises(GateFreshError, match="worktree remove"):
            gate(repo, resolve_ref(repo, "HEAD"), runner=passes_but_corrupts)

        assert track_holders, "gate() must call tempfile.mkdtemp"
        assert not track_holders[0].exists(), "a cleanup failure must not leak the holder dir"
        worktree_list = _git(repo, "worktree", "list")
        assert worktree_list.count("\n") == 1, "only the main checkout remains"
        assert "prunable" not in worktree_list, "no stale worktree registration should survive"


class TestMain:
    def test_tracked_dirt_refuses_and_names_the_file(self, tmp_path, capsys, monkeypatch):
        # Interactive path (no --pre-push): this must keep refusing. Contrast
        # with test_pre_push_proceeds_despite_tracked_dirt below, where the
        # same dirty tree must NOT refuse because --pre-push gates a sha that
        # is already committed and cannot see working-tree dirt.
        repo = _repo(tmp_path)
        (repo / "tracked.txt").write_text("v2\n")
        monkeypatch.setattr(gate_fresh, "gate", lambda *a, **k: pytest.fail("must not run"))
        assert main(["--repo", str(repo)]) == 1
        # ONE readouterr() call — it drains the buffer, so a second call
        # returns empty and would make this assertion vacuous.
        # A refusal is a failure diagnostic, so it belongs on stderr (the
        # same stream gate()'s own cleanup warnings use) — asserted on the
        # specific stream, not out+err summed, so a regression that moves
        # it to the wrong stream cannot pass silently.
        captured = capsys.readouterr()
        assert "tracked.txt" in captured.err
        assert "tracked.txt" not in captured.out

    def test_pre_push_proceeds_despite_tracked_dirt(self, tmp_path, capsys, monkeypatch):
        # A --pre-push run gates the sha git is about to push — already
        # committed — so working-tree dirt in the invoking checkout cannot
        # influence what gets gated. Refusing over it (as the interactive
        # path correctly does, above) would block a routine push of already-
        # committed content for a reason that does not apply, and the natural
        # response is `git push --no-verify`, which defeats the gate.
        repo = _repo(tmp_path)
        (repo / "tracked.txt").write_text("v2\n")
        pushed_sha = resolve_ref(repo, "HEAD")
        stdin_line = f"refs/heads/main {pushed_sha} refs/heads/main {gate_fresh.ZERO_SHA}\n"
        monkeypatch.setattr(gate_fresh.sys, "stdin", io.StringIO(stdin_line))
        gated_shas: list[str] = []
        monkeypatch.setattr(
            gate_fresh,
            "gate",
            lambda repo, sha, **k: (
                gated_shas.append(sha)
                or GateResult(passed=True, worktree=tmp_path / "wt", kept=False)
            ),
        )
        assert main(["--repo", str(repo), "--pre-push"]) == 0
        assert gated_shas == [pushed_sha], "the gate must still run, keyed to the pushed sha"
        captured = capsys.readouterr()
        assert "refusing" not in captured.err
        assert "Commit first" not in captured.err
        # A brief informational note is emitted (chosen over staying silent)
        # so the pushing developer isn't left wondering why local edits
        # aren't reflected in the gate's verdict.
        assert "not part of this gate" in captured.out

    def test_untracked_is_reported_but_does_not_block(self, tmp_path, capsys, monkeypatch):
        repo = _repo(tmp_path)
        (repo / "new.py").write_text("x = 1\n")
        monkeypatch.setattr(
            gate_fresh,
            "gate",
            lambda *a, **k: GateResult(passed=True, worktree=tmp_path / "wt", kept=False),
        )
        assert main(["--repo", str(repo)]) == 0
        # Untracked files are informational, not an error, so the report
        # belongs on stdout, not stderr.
        captured = capsys.readouterr()
        assert "new.py" in captured.out
        assert "new.py" not in captured.err

    def test_failure_prints_the_kept_worktree_path(self, tmp_path, capsys, monkeypatch):
        repo = _repo(tmp_path)
        kept = tmp_path / "kept-tree"
        monkeypatch.setattr(
            gate_fresh, "gate", lambda *a, **k: GateResult(passed=False, worktree=kept, kept=True)
        )
        assert main(["--repo", str(repo)]) == 1
        # A gate FAIL is a failure diagnostic, so it belongs on stderr.
        captured = capsys.readouterr()
        assert str(kept) in captured.err
        assert str(kept) not in captured.out


ZERO = "0" * 40
SHA_A = "a" * 40
SHA_B = "b" * 40


class TestRefToGate:
    def test_pushing_main_returns_the_local_sha(self):
        line = f"refs/heads/main {SHA_A} refs/heads/main {SHA_B}\n"
        assert ref_to_gate(line) == SHA_A

    def test_pushing_a_feature_branch_is_skipped(self):
        line = f"refs/heads/feat {SHA_A} refs/heads/feat {SHA_B}\n"
        assert ref_to_gate(line) is None

    def test_deleting_main_is_skipped(self):
        # All-zero local sha = a delete; there is nothing to gate.
        line = f"(delete) {ZERO} refs/heads/main {SHA_B}\n"
        assert ref_to_gate(line) is None

    def test_the_remote_ref_decides_not_the_local_one(self):
        # Pushing a local branch to main must still gate; keying off the local
        # ref name would miss `git push origin mybranch:main`.
        line = f"refs/heads/mybranch {SHA_A} refs/heads/main {SHA_B}\n"
        assert ref_to_gate(line) == SHA_A

    def test_a_mixed_push_gates_the_main_line(self):
        lines = (
            f"refs/heads/feat {SHA_B} refs/heads/feat {SHA_B}\n"
            f"refs/heads/main {SHA_A} refs/heads/main {SHA_B}\n"
        )
        assert ref_to_gate(lines) == SHA_A

    def test_empty_stdin_is_skipped(self):
        assert ref_to_gate("") is None


class TestPrePushHook:
    """Pins .githooks/pre-push itself.

    A hook that exists and is well-formed but not committed executable is a
    silent no-op: git simply never runs a non-executable hook, and every test
    above (which exercises the Python side directly) stays green regardless.
    Repo root is resolved from this test file's own location, not the cwd, so
    the check is correct no matter where pytest is invoked from.
    """

    _REPO_ROOT = PROJECT_ROOT

    def test_hook_is_committed_executable(self):
        # Checked via `git ls-files -s` (the INDEX/committed mode), not a
        # filesystem stat: a checkout can carry a working-tree file whose
        # mode git itself does not record, and it is the committed mode that
        # decides what every future clone or checkout gets.
        output = subprocess.run(
            ["git", "ls-files", "-s", ".githooks/pre-push"],
            cwd=self._REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert output, ".githooks/pre-push must be tracked by git"
        mode = output.split()[0]
        assert mode == "100755", f".githooks/pre-push must be committed executable, got {mode}"

    def test_hook_invokes_the_gate_with_the_pre_push_flag(self):
        # If someone rewrites the shim to drop --pre-push, the hook would gate
        # HEAD instead of the pushed sha — a silent wrong-tree gate.
        content = (self._REPO_ROOT / ".githooks" / "pre-push").read_text()
        assert "gate_fresh.py --pre-push" in content
