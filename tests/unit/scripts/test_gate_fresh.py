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
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from scripts import check_breaking_marks, gate_fresh
from scripts import gate_scope as gs
from scripts import gate_tip as gt
from scripts.gate_fresh import (
    GateFreshError,
    GateResult,
    PushedPair,
    gate,
    main,
    ref_to_gate,
    resolve_ref,
    tree_state,
)
from tests._fixtures.gitrepo import TmpGitRepo, git_env
from tests._fixtures.paths import CHECKOUT_ROOT
from tests.unit.scripts.test_check_breaking_marks import GOLDEN_HEADER, _seed

pytestmark = pytest.mark.interpreter_agnostic


def _git(repo: Path, *args: str) -> str:
    """Run one git command against *repo*, hermetically.

    The env is the suite's one builder (``tests/_fixtures/gitrepo.py``): these
    spawns used to inherit the developer's full environment, so a global
    ``commit.gpgsign`` or an ``/etc/gitconfig`` ``core.hooksPath`` failed the
    repo builder below with an opaque ``CalledProcessError``. Identity now
    comes from ``GIT_AUTHOR_*``/``GIT_COMMITTER_*``, which is why ``_repo``
    no longer runs ``git config user.email``/``user.name``. The gate itself
    (``scripts/gate_fresh.py``) spawns its own git; that is product behaviour
    and deliberately not touched here.
    """
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True, env=git_env(repo)
    ).stdout


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
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

        def runner(wt: Path, targets: list[str], make_args: list[str]) -> bool:
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
        result = gate(repo, resolve_ref(repo, "HEAD"), runner=lambda wt, targets, make_args: False)
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
            runner=lambda wt, targets, make_args: captured.append(targets) or True,
        )
        assert captured == [
            [
                "lint-python",
                "lint-arch",
                "check-api-snapshot",
                "check-api-teaching",
                "typecheck-python",
                "collect-check",
                "docs",
            ]
        ]

    def test_an_exception_mid_run_leaks_no_registered_worktree(self, tmp_path: Path):
        repo = _repo(tmp_path)

        def boom(wt: Path, targets: list[str], make_args: list[str]) -> bool:
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
        gate(repo, resolve_ref(repo, "HEAD"), runner=lambda wt, targets, make_args: True)

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

        def unreachable(wt: Path, targets: list[str], make_args: list[str]) -> bool:
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

        def boom(wt: Path, targets: list[str], make_args: list[str]) -> bool:
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

        def passes_but_corrupts(wt: Path, targets: list[str], make_args: list[str]) -> bool:
            (wt / ".git").unlink()
            return True

        with pytest.raises(GateFreshError, match="worktree remove"):
            gate(repo, resolve_ref(repo, "HEAD"), runner=passes_but_corrupts)

        assert track_holders, "gate() must call tempfile.mkdtemp"
        assert not track_holders[0].exists(), "a cleanup failure must not leak the holder dir"
        worktree_list = _git(repo, "worktree", "list")
        assert worktree_list.count("\n") == 1, "only the main checkout remains"
        assert "prunable" not in worktree_list, "no stale worktree registration should survive"


class TestLanesFinishBeforeCleanup:
    """The runner returns only when everything the lanes spawned has exited.

    ``subprocess.run`` returns when its direct child exits; ``make`` exits the
    moment its recipe does, while the recipe's own children (xdist workers, a
    test's subprocesses) can outlive it — and that gap is where ``git worktree
    remove`` used to run, racing a writer and blocking a push from ``main`` on
    2026-09-03. The runner now waits on the lanes' whole process group.
    """

    def test_a_backgrounded_grandchild_is_waited_for(self, tmp_path: Path):
        """The direct child exits at once, leaving a grandchild behind; the
        runner must not return until that grandchild is gone.

        Deterministic, not timed: the grandchild records its own pid, and the
        assertion is that the pid no longer exists when the runner returns —
        a runner that returned on the direct child's exit would find it alive.
        """
        import os

        pid_file = tmp_path / "grandchild.pid"
        script = f"sleep 2 & echo $! > {pid_file}; exit 0"
        rc = gate_fresh._run_awaiting_descendants(["sh", "-c", script], tmp_path)
        assert rc == 0
        pid = int(pid_file.read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)

    @pytest.mark.timeout(20)
    def test_an_interrupt_is_relayed_to_the_group_and_still_waited_for(
        self, tmp_path: Path, monkeypatch
    ):
        """Ctrl+C during the lanes must reach the lanes, and the runner must
        still not return (raise) until they are gone.

        ``start_new_session`` takes the child out of the terminal's foreground
        group, so the SIGINT the runner receives is NOT delivered to the child
        by the terminal any more — the runner relays it. Injected: the first
        ``Popen.wait`` raises ``KeyboardInterrupt`` once the grandchild has
        recorded its pid. Without the relay the child (waiting on a 30s
        ``sleep``) lives on past this test's 20s timeout; with it, the group
        is interrupted, drained, and the pid is gone on re-raise.

        The child is a Python spawner, not ``sh -c "sleep & wait"``: a
        non-interactive shell starts ``&`` jobs with SIGINT IGNORED (POSIX
        async-list rule), so such a grandchild can never be interrupted and
        the test would fail for a reason that is not the runner's. Real lane
        descendants (xdist workers, test subprocesses) are spawned the
        Python way and take the default disposition.
        """
        import os
        import sys

        pid_file = tmp_path / "grandchild.pid"
        real_wait = subprocess.Popen.wait
        interrupted: list[int] = []

        def wait_then_interrupt(self, timeout=None):
            if not interrupted:
                interrupted.append(self.pid)
                while not pid_file.exists():  # the grandchild exists before we interrupt
                    time.sleep(0.02)
                raise KeyboardInterrupt
            return real_wait(self, timeout)

        monkeypatch.setattr(subprocess.Popen, "wait", wait_then_interrupt)
        spawner = (
            "import subprocess, pathlib; p = subprocess.Popen(['sleep', '30']); "
            f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid)); p.wait()"
        )
        with pytest.raises(KeyboardInterrupt):
            gate_fresh._run_awaiting_descendants([sys.executable, "-c", spawner], tmp_path)
        pid = int(pid_file.read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)

    @pytest.mark.timeout(20)
    def test_an_interrupt_during_the_lingering_wait_is_relayed_too(
        self, tmp_path: Path, monkeypatch
    ):
        """The phase this change exists for — direct child gone, descendants
        alive, the runner polling the group — must relay a Ctrl+C as well.

        Injected on the first poll of the post-child wait: the spawner
        ``os._exit``s right after recording its grandchild's pid, so the
        direct child has exited when ``time.sleep`` in the wait loop first
        runs and raises. A relay that only wrapped the child's own wait
        would propagate here with the grandchild alive (the review's probe).
        """
        import os
        import sys

        pid_file = tmp_path / "grandchild.pid"
        real_sleep = time.sleep
        raised: list[bool] = []

        def sleep_then_interrupt(seconds):
            if not raised:
                raised.append(True)
                raise KeyboardInterrupt
            real_sleep(seconds)

        monkeypatch.setattr(gate_fresh.time, "sleep", sleep_then_interrupt)
        spawner = (
            "import subprocess, pathlib, os; p = subprocess.Popen(['sleep', '30']); "
            f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid)); os._exit(0)"
        )
        with pytest.raises(KeyboardInterrupt):
            gate_fresh._run_awaiting_descendants([sys.executable, "-c", spawner], tmp_path)
        assert raised, "the interrupt was never injected — the wait loop did not poll"
        pid = int(pid_file.read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)

    def test_the_direct_childs_exit_code_is_reported(self, tmp_path: Path):
        rc = gate_fresh._run_awaiting_descendants(["sh", "-c", "exit 3"], tmp_path)
        assert rc == 3

    def test_run_targets_stops_at_a_failed_sync(self, tmp_path: Path, monkeypatch):
        """``_run_targets`` still short-circuits on a failed ``uv sync --locked`` —
        the wait wrapper must not have changed the two-step control flow."""
        calls: list[list[str]] = []

        def fake(cmd: list[str], cwd: Path) -> int:
            calls.append(cmd)
            return 1

        monkeypatch.setattr(gate_fresh, "_run_awaiting_descendants", fake)
        assert gate_fresh._run_targets(tmp_path, ["lint-python"]) is False
        assert calls == [["uv", "sync", "--locked"]]


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
        repo, _bare_repo, remote_sha, pushed_sha = _published(tmp_path)
        _stamp_tip(repo, remote_sha, pushed_sha)
        (repo / "tracked.txt").write_text("v2\n")
        stdin_line = f"refs/heads/main {pushed_sha} refs/heads/main {remote_sha}\n"
        monkeypatch.setattr(gate_fresh.sys, "stdin", io.StringIO(stdin_line))
        gated_shas: list[str] = []
        monkeypatch.setattr(
            gate_fresh,
            "gate",
            lambda repo, sha, *, make_args=None: (
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
    def test_pushing_main_returns_the_remote_and_local_shas(self):
        line = f"refs/heads/main {SHA_A} refs/heads/main {SHA_B}\n"
        assert ref_to_gate(line) == PushedPair(SHA_B, SHA_A)

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
        assert ref_to_gate(line) == PushedPair(SHA_B, SHA_A)

    def test_a_mixed_push_gates_the_main_line(self):
        lines = (
            f"refs/heads/feat {SHA_B} refs/heads/feat {SHA_B}\n"
            f"refs/heads/main {SHA_A} refs/heads/main {SHA_B}\n"
        )
        assert ref_to_gate(lines) == PushedPair(SHA_B, SHA_A)

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

    _REPO_ROOT = CHECKOUT_ROOT

    def test_hook_is_committed_executable(self, tmp_path: Path):
        # Checked via `git ls-files -s` (the INDEX/committed mode), not a
        # filesystem stat: a checkout can carry a working-tree file whose
        # mode git itself does not record, and it is the committed mode that
        # decides what every future clone or checkout gets.
        # Reads the REAL repo's index, but still under the hermetic env: the
        # answer must come from this checkout, never from a developer's
        # global config.  Known trade: neutering global config also discards
        # any `safe.directory` entry, so a foreign-uid checkout (container
        # bind-mount) would fail "dubious ownership" here — fine on the dev
        # VM and ubuntu-latest CI, where the checkout owner runs the tests.
        output = subprocess.run(
            ["git", "ls-files", "-s", ".githooks/pre-push"],
            cwd=self._REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
            env=git_env(tmp_path),
        ).stdout
        assert output, ".githooks/pre-push must be tracked by git"
        mode = output.split()[0]
        assert mode == "100755", f".githooks/pre-push must be committed executable, got {mode}"

    def test_hook_invokes_the_gate_with_the_pre_push_flag(self):
        # If someone rewrites the shim to drop --pre-push, the hook would gate
        # HEAD instead of the pushed sha — a silent wrong-tree gate.
        content = (self._REPO_ROOT / ".githooks" / "pre-push").read_text()
        assert 'gate_fresh.py --pre-push --remote "$1"' in content


# ---------------------------------------------------------------------------
# The pre-push check: stamped lanes, the pushed range and the history checks
# ---------------------------------------------------------------------------


def _bare(tmp_path: Path, name: str) -> Path:
    bare = tmp_path / f"{name}.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True, env=git_env(tmp_path))
    return bare


def _commit_file(repo: Path, path: str, text: str, message: str) -> str:
    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    (repo / path).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD").strip()


def _published(tmp_path: Path, remote: str = "origin") -> "tuple[Path, Path, str, str]":
    """Three commits: _repo's, then pyproject (published as the remote's main = A), then a
    tests-leaf commit on top (the tip = B).

    Returns (repo, bare, remote_sha = A, tip_sha = B). The oracle's tip list for A..B is the
    tests-leaf shape: `make quick PATHS="tests/unit/cli"`, ruff over the module and
    `make quick-guards` (the three stamped lanes), plus the live check-breaking, lint-arch
    and gate-fresh lines.
    """
    repo = _repo(tmp_path)
    (repo / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "pyproject")
    remote_sha = _git(repo, "rev-parse", "HEAD").strip()
    bare = _bare(tmp_path, remote)
    _git(repo, "remote", "add", remote, str(bare))
    _git(repo, "push", "-q", remote, "HEAD:refs/heads/main")
    tip_sha = _commit_file(repo, "tests/unit/cli/test_b.py", "def test_b(): pass\n", "tip")
    return repo, bare, remote_sha, tip_sha


def _stamp_tip(repo: Path, remote_sha: str, tip_sha: str) -> None:
    """Stamp, at tip_sha's tree, every stamped lane the oracle names for remote_sha..tip_sha."""
    root = gt.stamps_root(repo, gt.STORE)
    tree = gt.tree_hash(repo, tip_sha)
    for line in gs.tip_commands(repo, remote_sha, tip_sha, git_env(repo)):
        if not gt.is_live(line):
            gt.write_stamp(
                root,
                tree=tree,
                key=gt.lane_key(line),
                base=remote_sha,
                measured={},
                checkout=str(repo),
            )


def _pre_push(
    monkeypatch, repo: Path, remote: str, remote_sha: str, local_sha: str, *argv: str
) -> int:
    monkeypatch.setattr(
        "sys.stdin", io.StringIO(f"refs/heads/main {local_sha} refs/heads/main {remote_sha}\n")
    )
    return gate_fresh.main(["--repo", str(repo), "--pre-push", "--remote", remote, *argv])


def _sentinel_gate(monkeypatch) -> None:
    monkeypatch.setattr(gate_fresh, "gate", lambda *a, **k: pytest.fail("gate() reached"))


def _recording_gate(monkeypatch, tmp_path: Path) -> "list[tuple[str, list[str]]]":
    calls: "list[tuple[str, list[str]]]" = []

    def fake(repo, sha, *, targets=None, make_args=None):
        # targets: the full gate (None) or, over a tree a pass already stamped, the history
        # lane alone (TestTreeStamps pins which).
        calls.append((sha, make_args or []))
        return GateResult(passed=True, worktree=tmp_path / "wt", kept=False)

    monkeypatch.setattr(gate_fresh, "gate", fake)
    return calls


def _stamped(repo: Path, remote_sha: str, tip_sha: str) -> "list[str]":
    lines = gs.tip_commands(repo, remote_sha, tip_sha, git_env(repo))
    return [line for line in lines if not gt.is_live(line)]


class TestPrePushCheck:
    def test_the_check_runs_before_the_worktree_gate_and_refuses_with_the_text(
        self, tmp_path, capsys, monkeypatch
    ):
        repo, _bare_repo, remote_sha, tip = _published(tmp_path)
        _sentinel_gate(monkeypatch)
        assert _pre_push(monkeypatch, repo, "origin", remote_sha, tip) == 1
        err = capsys.readouterr().err
        tree = gt.tree_hash(repo, tip)
        assert err.startswith(
            f"gate-tip: refusing to push {tip[:12]} to main — its gate is not stamped on tree "
            f"{tree[:12]} (range {remote_sha[:12]}..{tip[:12]}):"
        )
        stamped = _stamped(repo, remote_sha, tip)
        assert stamped
        assert f"  {stamped[0]}: no stamp" in err
        assert (
            "run: git fetch origin main && make gate-tip BASE=FETCH_HEAD   "
            f"(on commit {tip[:12]}), then push again."
        ) in err
        assert (
            "git push --no-verify skips this check; CI then runs only its own lanes — "
            "never make coverage or the beds."
        ) in err

    def test_a_stamped_tree_is_honoured_by_a_squash_and_a_message_amend(
        self, tmp_path, monkeypatch
    ):
        assert "lint-arch" in gate_fresh.GATED_TARGETS
        repo, _bare_repo, remote_sha, tip = _published(tmp_path)
        _stamp_tip(repo, remote_sha, tip)
        calls = _recording_gate(monkeypatch, tmp_path)
        tree = gt.tree_hash(repo, tip)
        squash = _git(repo, "commit-tree", tree, "-p", remote_sha, "-m", "squash").strip()
        assert _pre_push(monkeypatch, repo, "origin", remote_sha, squash) == 0
        _git(repo, "commit", "--amend", "-qm", "tip, reworded")
        amended = _git(repo, "rev-parse", "HEAD").strip()
        assert amended != tip
        assert _pre_push(monkeypatch, repo, "origin", remote_sha, amended) == 0
        make_args = [f"RANGE={remote_sha}..HEAD"]
        assert calls == [(squash, make_args), (amended, make_args)]

    def test_the_amended_message_that_drops_a_breaking_marker_is_rejected_by_the_live_check(
        self, tmp_path
    ):
        repo = TmpGitRepo(tmp_path / "breaking")
        base = _seed(repo, ["otto:Alpha", "otto:Beta"])
        repo.write("golden.txt", GOLDEN_HEADER + "otto:Alpha\n")
        tip = repo.commit("feat(api)!: drop Beta")
        argv = ["--repo", str(repo.root), "--golden", "golden.txt"]
        assert check_breaking_marks.main([f"{base}..{tip}", *argv]) == 0
        repo.git("commit", "--amend", "-q", "-m", "feat(api): drop Beta")
        amended = repo.git("rev-parse", "HEAD").strip()
        assert check_breaking_marks.main([f"{base}..{amended}", *argv]) == 1

    def test_the_pristine_make_carries_the_pushed_range(self, tmp_path, monkeypatch):
        calls: "list[list[str]]" = []

        def fake(cmd: "list[str]", cwd: Path) -> int:
            calls.append(cmd)
            return 0

        monkeypatch.setattr(gate_fresh, "_run_awaiting_descendants", fake)
        assert gate_fresh._run_targets(tmp_path, ["lint-arch"], ("RANGE=a..HEAD",)) is True
        assert calls == [["uv", "sync", "--locked"], ["make", "lint-arch", "RANGE=a..HEAD"]]

    def test_the_check_reads_the_pushed_sha_not_head(self, tmp_path, capsys, monkeypatch):
        repo, _bare_repo, remote_sha, tip = _published(tmp_path)
        _stamp_tip(repo, remote_sha, tip)
        later = _commit_file(repo, "src/otto/cli/x.py", "X = 1\n", "later")
        calls = _recording_gate(monkeypatch, tmp_path)
        assert _pre_push(monkeypatch, repo, "origin", remote_sha, tip) == 0
        assert [sha for sha, _ in calls] == [tip]
        assert _pre_push(monkeypatch, repo, "origin", remote_sha, later) == 1
        err = capsys.readouterr().err
        assert f"refusing to push {later[:12]}" in err
        owed = _stamped(repo, remote_sha, later)
        assert owed
        for line in owed:
            assert f"  {line}: " in err

    def test_an_oracle_failure_is_reported_before_the_worktree_gate(
        self, tmp_path, capsys, monkeypatch
    ):
        repo, _bare_repo, remote_sha, tip = _published(tmp_path)

        def boom(*a, **k):
            raise gs.GateScopeError("boom")

        monkeypatch.setattr(gs, "tip_commands", boom)
        _sentinel_gate(monkeypatch)
        assert _pre_push(monkeypatch, repo, "origin", remote_sha, tip) == 1
        err = capsys.readouterr().err
        assert "gate-fresh: the oracle could not classify" in err
        assert "boom" in err

    def test_live_goals_are_never_demanded(self, tmp_path):
        repo, _bare_repo, remote_sha, tip = _published(tmp_path)
        _stamp_tip(repo, remote_sha, tip)
        lines = gs.tip_commands(repo, remote_sha, tip, git_env(repo))
        assert any(gt.is_live(line) for line in lines), "the range owes live lines too"
        assert gt.missing_stamps(repo, "origin", remote_sha, tip).missing == []

    def test_a_lane_the_oracle_started_requiring_is_no_stamp(self, tmp_path, monkeypatch):
        repo, _bare_repo, remote_sha, base_tip = _published(tmp_path)
        tip = _commit_file(repo, "schemas/unrowed.json", "{}\n", "unrowed")
        assert "make coverage" in gs.tip_commands(repo, remote_sha, tip, git_env(repo))
        _stamp_tip(repo, remote_sha, tip)
        assert base_tip != tip
        monkeypatch.setattr(gs, "FULL_GATE", [*gs.FULL_GATE, "make dashboard-soak"])
        verdict = gt.missing_stamps(repo, "origin", remote_sha, tip)
        assert verdict.missing == [gt.Missing("make dashboard-soak", "no stamp")]

    def test_a_lane_this_runner_does_not_know_refuses_with_one_line(
        self, tmp_path, capsys, monkeypatch
    ):
        repo, _bare_repo, remote_sha, _base_tip = _published(tmp_path)
        tip = _commit_file(repo, "schemas/unrowed.json", "{}\n", "unrowed")
        monkeypatch.setattr(gs, "FULL_GATE", [*gs.FULL_GATE, "make a-goal-this-runner-never-knew"])
        _sentinel_gate(monkeypatch)
        assert _pre_push(monkeypatch, repo, "origin", remote_sha, tip) == 1
        err = capsys.readouterr().err
        assert "gate-fresh: the oracle emitted a lane this runner does not know" in err
        assert "make a-goal-this-runner-never-knew" in err
        assert "Traceback" not in err

    def test_a_multi_commit_push_demands_the_last_tree_only(self, tmp_path, monkeypatch):
        repo, _bare_repo, remote_sha, _tip = _published(tmp_path)
        _commit_file(repo, "tests/unit/cli/test_c.py", "def test_c(): pass\n", "second")
        last = _commit_file(repo, "tests/unit/cli/test_d.py", "def test_d(): pass\n", "third")
        _stamp_tip(repo, remote_sha, last)
        calls = _recording_gate(monkeypatch, tmp_path)
        assert _pre_push(monkeypatch, repo, "origin", remote_sha, last) == 0
        assert [sha for sha, _ in calls] == [last]

    def test_a_remote_sha_absent_locally(self, tmp_path, capsys, monkeypatch):
        repo, _bare_repo, _remote_sha, tip = _published(tmp_path)
        _sentinel_gate(monkeypatch)
        assert _pre_push(monkeypatch, repo, "origin", "c" * 40, tip) == 1
        assert "is not in this clone: git fetch origin main" in capsys.readouterr().err

    def test_a_non_fast_forward(self, tmp_path, capsys, monkeypatch):
        repo, _bare_repo, _remote_sha, tip = _published(tmp_path)
        stranger = _git(repo, "commit-tree", gt.tree_hash(repo, tip), "-m", "other").strip()
        _sentinel_gate(monkeypatch)
        assert _pre_push(monkeypatch, repo, "origin", stranger, tip) == 1
        assert "not a fast-forward of origin's main" in capsys.readouterr().err

    def test_the_all_zero_remote_sha(self, tmp_path, capsys, monkeypatch):
        repo, _bare_repo, _remote_sha, tip = _published(tmp_path)
        _sentinel_gate(monkeypatch)
        assert _pre_push(monkeypatch, repo, "origin", gate_fresh.ZERO_SHA, tip) == 1
        assert "has no main to compare against" in capsys.readouterr().err

    def test_a_remote_reset_behind_the_tracking_ref_demands_the_larger_list(
        self, tmp_path, capsys, monkeypatch
    ):
        repo, bare, a_sha, _tip0 = _published(tmp_path)
        b_sha = _commit_file(repo, "schemas/unrowed.json", "{}\n", "measure")
        c_sha = _commit_file(repo, "tests/unit/cli/test_c.py", "def test_c(): pass\n", "tip")
        _git(repo, "push", "-q", "origin", f"{b_sha}:refs/heads/main")
        _stamp_tip(repo, b_sha, c_sha)
        _git(repo, "--git-dir", str(bare), "update-ref", "refs/heads/main", a_sha)
        _sentinel_gate(monkeypatch)
        assert _pre_push(monkeypatch, repo, "origin", a_sha, c_sha) == 1
        err = capsys.readouterr().err
        assert f"(range {a_sha[:12]}..{c_sha[:12]}):" in err
        larger = _stamped(repo, a_sha, c_sha)
        assert "make coverage" in larger
        assert "nox -s tests_hostless-3.14" in larger
        bc = _stamped(repo, b_sha, c_sha)
        assert "make coverage" not in bc
        assert "nox -s tests_hostless-3.14" not in bc
        assert not set(larger) <= set(bc)
        verdict = gt.missing_stamps(repo, "origin", a_sha, c_sha)
        assert [m.lane for m in verdict.missing] == larger

    def test_a_push_to_an_upstream_remote_and_the_remedy_classifies_the_same_range(
        self, tmp_path, capsys, monkeypatch
    ):
        repo, _bare_repo, remote_sha, tip = _published(tmp_path, "upstream")
        _sentinel_gate(monkeypatch)
        assert _pre_push(monkeypatch, repo, "upstream", remote_sha, tip) == 1
        err = capsys.readouterr().err
        assert "git fetch upstream main" in err
        assert (
            err.rstrip()
            .splitlines()[-2]
            .endswith(f"make gate-tip BASE=FETCH_HEAD   (on commit {tip[:12]}), then push again.")
        )
        _git(repo, "fetch", "-q", "upstream", "main")
        assert gt._tip_list(repo, "FETCH_HEAD") == (
            remote_sha,
            gs.tip_commands(repo, remote_sha, tip, git_env(repo)),
        )

    def test_a_push_to_a_bare_path(self, tmp_path, capsys, monkeypatch):
        repo, bare, remote_sha, tip = _published(tmp_path)
        _sentinel_gate(monkeypatch)
        assert _pre_push(monkeypatch, repo, str(bare), remote_sha, tip) == 1
        err = capsys.readouterr().err
        assert f"run: git fetch {bare} main && make gate-tip BASE=FETCH_HEAD" in err
        _git(repo, "fetch", "-q", str(bare), "main")
        assert gt._tip_list(repo, "FETCH_HEAD") == (
            remote_sha,
            gs.tip_commands(repo, remote_sha, tip, git_env(repo)),
        )

    def test_a_push_to_a_non_main_ref_runs_no_check(self, tmp_path, monkeypatch):
        repo = _repo(tmp_path)
        sha = resolve_ref(repo, "HEAD")
        monkeypatch.setattr(gt, "missing_stamps", lambda *a: pytest.fail("checked"))
        _sentinel_gate(monkeypatch)
        monkeypatch.setattr(
            "sys.stdin", io.StringIO(f"refs/heads/feature {sha} refs/heads/feature {sha}\n")
        )
        assert gate_fresh.main(["--repo", str(repo), "--pre-push", "--remote", "origin"]) == 0


class TestMeasuredOutputsThroughTheCheck:
    """A tip differing from a stamped tree by a recorded blob is honoured; any other is not."""

    MATRIX = "schemas/support_matrix.json"

    def _stamped_s(self, tmp_path: Path, measured_text: str):
        """Return (repo, remote sha, S sha, the blob a lane recorded for measured_text)."""
        repo, _bare_repo, remote_sha, _tip = _published(tmp_path)
        s_sha = _commit_file(repo, self.MATRIX, '{"as_of": "2026-10-01"}\n', "matrix")
        _stamp_tip(repo, remote_sha, s_sha)
        (repo / self.MATRIX).write_text(measured_text)
        blob = _git(repo, "hash-object", self.MATRIX).strip()
        _git(repo, "checkout", "--", self.MATRIX)
        assert "make coverage" in gs.tip_commands(repo, remote_sha, s_sha, git_env(repo))
        lane = _stamped(repo, remote_sha, s_sha)[0]
        gt.write_stamp(
            gt.stamps_root(repo, gt.STORE),
            tree=gt.tree_hash(repo, s_sha),
            key=gt.lane_key(lane),
            base=remote_sha,
            measured={self.MATRIX: blob},
            checkout=str(repo),
        )
        return repo, remote_sha, s_sha

    def test_a_tip_that_differs_only_by_a_recorded_measured_blob_is_honoured(self, tmp_path):
        text = '{"as_of": "2026-10-16"}\n'
        repo, remote_sha, s_sha = self._stamped_s(tmp_path, text)
        t_sha = _commit_file(repo, self.MATRIX, text, "measured")
        assert gs.tip_commands(repo, remote_sha, s_sha, git_env(repo)) == gs.tip_commands(
            repo, remote_sha, t_sha, git_env(repo)
        )
        assert gt.missing_stamps(repo, "origin", remote_sha, t_sha).missing == []

    def test_a_tip_whose_matrix_bytes_differ_from_the_recorded_blob_is_refused(self, tmp_path):
        repo, remote_sha, s_sha = self._stamped_s(tmp_path, '{"as_of": "2026-10-16"}\n')
        t_sha = _commit_file(repo, self.MATRIX, '{"as_of": "hand edited"}\n', "edited")
        verdict = gt.missing_stamps(repo, "origin", remote_sha, t_sha)
        assert [m.lane for m in verdict.missing] == _stamped(repo, remote_sha, t_sha)
        reason = (
            f"stamped for tree {gt.tree_hash(repo, s_sha)[:12]}, which differs from the tip's "
            f"beyond the measured outputs (stamped in {repo}, base {remote_sha[:12]})"
        )
        assert {m.reason for m in verdict.missing} == {reason}

    def test_one_that_differs_elsewhere_names_the_stamp_checkout_and_base(self, tmp_path):
        repo, _bare_repo, remote_sha, s_sha = _published(tmp_path)
        _stamp_tip(repo, remote_sha, s_sha)
        t_sha = _commit_file(repo, "tests/unit/cli/test_b.py", "def test_b(): assert 1\n", "edit")
        verdict = gt.missing_stamps(repo, "origin", remote_sha, t_sha)
        assert [m.lane for m in verdict.missing] == _stamped(repo, remote_sha, t_sha)
        reason = (
            f"stamped for tree {gt.tree_hash(repo, s_sha)[:12]}, which differs from the tip's "
            f"beyond the measured outputs (stamped in {repo}, base {remote_sha[:12]})"
        )
        assert {m.reason for m in verdict.missing} == {reason}


# ---------------------------------------------------------------------------
# Tree stamps: a tree that passed the gate is not gated again at the push
# ---------------------------------------------------------------------------


def _targets_gate(
    monkeypatch, tmp_path: Path, *, passed: bool = True
) -> "list[tuple[str, list[str] | None, list[str]]]":
    """Fake gate() recording (sha, targets, make_args); ``targets`` None is the full gate."""
    calls: "list[tuple[str, list[str] | None, list[str]]]" = []

    def fake(repo, sha, *, targets=None, make_args=None):
        calls.append((sha, targets, make_args or []))
        return GateResult(passed=passed, worktree=tmp_path / "wt", kept=not passed)

    monkeypatch.setattr(gate_fresh, "gate", fake)
    return calls


class TestTreeStamps:
    def test_a_pass_stamps_the_gated_tree_with_the_targets_it_ran(self, tmp_path, monkeypatch):
        repo = _repo(tmp_path)
        _targets_gate(monkeypatch, tmp_path)
        assert main(["--repo", str(repo)]) == 0
        import json

        tree = gt.tree_hash(repo, "HEAD")
        assert gate_fresh.stamped(repo, tree)
        record = json.loads(gate_fresh.stamp_path(repo, tree).read_text())
        assert record["tree"] == tree
        assert record["targets"] == gate_fresh.GATED_TARGETS

    def test_a_failure_stamps_nothing(self, tmp_path, monkeypatch):
        repo = _repo(tmp_path)
        _targets_gate(monkeypatch, tmp_path, passed=False)
        assert main(["--repo", str(repo)]) == 1
        assert not gate_fresh.stamped(repo, gt.tree_hash(repo, "HEAD"))

    def test_a_stamped_tree_runs_only_the_history_lane_at_the_push(
        self, tmp_path, capsys, monkeypatch
    ):
        repo, _bare_repo, remote_sha, tip = _published(tmp_path)
        _stamp_tip(repo, remote_sha, tip)
        gate_fresh.write_stamp(repo, gt.tree_hash(repo, tip))
        calls = _targets_gate(monkeypatch, tmp_path)
        assert _pre_push(monkeypatch, repo, "origin", remote_sha, tip) == 0
        assert calls == [(tip, gate_fresh.HISTORY_TARGETS, [f"RANGE={remote_sha}..HEAD"])]
        assert "already passed gate-fresh" in capsys.readouterr().out

    def test_an_amend_that_keeps_the_tree_still_runs_the_history_lane(self, tmp_path, monkeypatch):
        """The stamp is for the tree; the commit messages the push carries are checked anew."""
        repo, _bare_repo, remote_sha, tip = _published(tmp_path)
        _stamp_tip(repo, remote_sha, tip)
        gate_fresh.write_stamp(repo, gt.tree_hash(repo, tip))
        _git(repo, "commit", "--amend", "-qm", "tip, reworded")
        amended = _git(repo, "rev-parse", "HEAD").strip()
        calls = _targets_gate(monkeypatch, tmp_path)
        assert _pre_push(monkeypatch, repo, "origin", remote_sha, amended) == 0
        assert calls == [(amended, gate_fresh.HISTORY_TARGETS, [f"RANGE={remote_sha}..HEAD"])]

    def test_an_unstamped_tree_runs_the_full_gate_and_is_stamped_by_it(self, tmp_path, monkeypatch):
        repo, _bare_repo, remote_sha, tip = _published(tmp_path)
        _stamp_tip(repo, remote_sha, tip)
        calls = _targets_gate(monkeypatch, tmp_path)
        assert _pre_push(monkeypatch, repo, "origin", remote_sha, tip) == 0
        assert calls == [(tip, None, [f"RANGE={remote_sha}..HEAD"])]
        assert gate_fresh.stamped(repo, gt.tree_hash(repo, tip))

    @pytest.mark.parametrize(
        "body",
        [
            "not json",
            '{"format": 1, "tree": "TREE", "targets": ["docs"]}',
            '{"format": 2, "tree": "TREE", "targets": TARGETS}',
            '{"format": 1, "tree": "another", "targets": TARGETS}',
        ],
        ids=["unreadable", "another-target-list", "another-format", "another-tree"],
    )
    def test_a_stamp_that_is_not_this_gate_s_evidence_runs_the_full_gate(
        self, tmp_path, monkeypatch, body
    ):
        import json

        repo, _bare_repo, remote_sha, tip = _published(tmp_path)
        _stamp_tip(repo, remote_sha, tip)
        tree = gt.tree_hash(repo, tip)
        path = gate_fresh.stamp_path(repo, tree)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            body.replace("TREE", tree).replace("TARGETS", json.dumps(gate_fresh.GATED_TARGETS))
        )
        calls = _targets_gate(monkeypatch, tmp_path)
        assert _pre_push(monkeypatch, repo, "origin", remote_sha, tip) == 0
        assert [targets for _, targets, _ in calls] == [None]

    def test_the_history_lane_holds_every_make_target_that_reads_the_range(self):
        """A target reading ``$(RANGE)`` depends on the pushed commits, not the tree: one the
        stamp shortcut does not run would go unchecked on every stamped push."""
        import re

        readers: "set[str]" = set()
        target = None
        for line in (CHECKOUT_ROOT / "Makefile").read_text().splitlines():
            head = re.match(r"^([A-Za-z0-9_.-]+):(?!=)", line)
            if head:
                target = head.group(1)
            elif line.startswith("\t") and "$(RANGE)" in line and target is not None:
                readers.add(target)
        assert readers, "no Makefile recipe reads $(RANGE): the scan is broken"
        assert readers <= set(gate_fresh.HISTORY_TARGETS), readers
