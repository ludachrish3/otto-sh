"""Pins for scripts/gate_tip.py — the landing's gate: lane stamps, the runner and the push check."""

import contextlib
import dataclasses
import fcntl
import fnmatch
import functools
import inspect
import io
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scripts import gate_scope as gs
from scripts import gate_stamp_common
from scripts import gate_tip as gt
from scripts import local_gate_status as lgs
from scripts.gate_stamp_common import tree_hash
from scripts.local_gate_status import GhRunner
from scripts.process_group import UndrainedError
from tests._fixtures import fake_gh as fg
from tests._fixtures.gitrepo import git_env
from tests._fixtures.lease_env import lease_free_environ
from tests._fixtures.paths import CHECKOUT_ROOT
from tests.unit.scripts._gate_repos import (
    _bare,
    _commit_file,
    _docs_change,
    _git,
    _hostless_test_change,
    _matrix_change,
    _published,
    _published_src,
    _Rebased,
    _rebased_landing,
    _src_change,
    _stamp_tip,
    _write,
)
from tests.unit.scripts._gate_repos import _repo as _fresh_repo
from tests.unit.scripts.test_gate_scope import _head_paths

pytestmark = pytest.mark.interpreter_agnostic

MAKEFILE = (CHECKOUT_ROOT / "Makefile").read_text()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "schemas").mkdir(parents=True)
    (repo / "tests" / "unit").mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    (repo / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
    (repo / "schemas" / "support_matrix.json").write_text('{"as_of": "2026-10-01"}\n')
    (repo / "tests" / "unit" / "test_a.py").write_text("def test_a():\n    pass\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "remote", "add", "origin", "https://github.com/o/r.git")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    return repo


@pytest.fixture(autouse=True)
def _default_lease(monkeypatch):
    """Every runner test gets a lease backend that never touches a lab file or the bed.

    ``bed_lease.load_cell`` refuses a missing lab file and ``_repo`` writes none; the reap
    is stubbed too, so an interrupted bed lane never reaches a real bed. A test that spies
    on the lease (``_lease_spy``) installs its own doubles over these.
    """

    def acquire(ctx):
        ctx.lease = gt.Lease(cell=None, pid=os.getpid())

    def release(ctx):
        ctx.lease = None

    monkeypatch.setattr(gt, "_acquire_lease", acquire)
    monkeypatch.setattr(gt, "_release_lease", release)
    monkeypatch.setattr("scripts.bed_reap.reap", lambda cell, pid: 0)


def _store(tmp_path: Path) -> Path:
    return tmp_path / "store"


def _live_tree(repo: Path, text: str) -> str:
    """Return a tree the object store holds (so never pruned) that is not HEAD's."""

    def git_in(args: "list[str]", given: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=repo,
            input=given,
            capture_output=True,
            text=True,
            check=True,
            env=git_env(repo),
        ).stdout.strip()

    blob = git_in(["hash-object", "-w", "--stdin"], text)
    return git_in(["mktree"], f"100644 blob {blob}\tf.txt\n")


def _peer_write(root: Path, write, deferred: list) -> None:
    """Act as a peer gate-tip: it writes only under the store lock, waiting while it is held.

    The wait is simulated: a held lock defers *write* to *deferred*, for the test to run once
    the step under test has returned; a free lock lets the write land inside the window.
    """
    import fcntl

    root.mkdir(parents=True, exist_ok=True)
    with (root / gate_stamp_common.LOCK_FILE).open("a") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            deferred.append(write)
            return
        try:
            write()
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _measure(repo: Path, text: str) -> str:
    """Rewrite the support matrix as a lane would and return its blob."""
    (repo / "schemas" / "support_matrix.json").write_text(text)
    return _git(repo, "hash-object", "schemas/support_matrix.json").strip()


class TestGoals:
    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            ("make coverage-bed", ["coverage-bed"]),
            ("make check-ts test-ts", ["check-ts", "test-ts"]),
            ('make quick PATHS="tests/unit/x.py tests/unit/y.py"', ["quick"]),
            ("make check-breaking RANGE=abc..HEAD", ["check-breaking"]),
            (gs.STABILITY_UNIX, ["stability-unix"]),
            ("uv run ruff check tests/unit/x.py", ["ruff"]),
            ("uv run ty check src/otto/host/x.py", ["ty"]),
        ],
    )
    def test_goals_of_each_shape(self, line, expected):
        assert gt.goals(line) == expected

    @pytest.mark.parametrize("goal", ["coverage", "coverage-ts", "gate-fresh"])
    def test_a_retired_goal_line_is_an_unknown_goal(self, goal):
        with pytest.raises(gt.UnknownGoalError, match=goal):
            gt.goals(f"make {goal}")

    def test_an_unknown_shape_or_goal_is_refused(self):
        with pytest.raises(gt.UnknownGoalError):
            gt.goals("python3 -m something")
        with pytest.raises(gt.UnknownGoalError):
            gt.is_live("make frobnicate")

    def test_live_lanes_are_the_range_targets(self):
        assert gt.LIVE_GOALS == gs.RANGE_TARGETS == ["check-breaking"]
        assert not set(gt.LIVE_GOALS) & set(gt.STAMPED_GOALS)
        assert gt.is_live("make check-breaking RANGE=abc..HEAD")
        assert not gt.is_live("make check-python")
        assert not gt.is_live("make coverage-bed")

    def test_a_stored_stamp_for_a_retired_goal_is_not_evidence(self, tmp_path):
        """A `make coverage` stamp written before the goal retired reads as unreadable."""
        repo, root = _repo(tmp_path), _store(tmp_path)
        tree = tree_hash(repo)
        path = gt.stamp_path(root, tree, "make coverage")
        path.parent.mkdir(parents=True)
        stored = {
            "format": 1,
            "tree": tree,
            "command": "make coverage",
            "base": "b" * 40,
            "measured": {},
            "written_at": "now",
            "checkout": str(repo),
        }
        path.write_text(json.dumps(stored))
        assert gt.read_stamp(path) is None
        assert gt.lookup(repo, root, tree, "make coverage").reason == "unreadable stamp"
        path.write_text(json.dumps({**stored, "command": "make coverage-bed"}))
        assert gt.read_stamp(path) is not None, "the control: a known goal reads"


class TestKeysAndFiles:
    def test_the_key_strips_only_the_range_decoration(self):
        assert gt.lane_key("make check-breaking RANGE=abc..HEAD") == "make check-breaking"
        assert gt.lane_key('make quick PATHS="a b"') == 'make quick PATHS="a b"'

    def test_two_keys_the_slug_folds_alike_get_two_files(self, tmp_path):
        a, b = (
            'make quick PATHS="tests/unit/foo/test_a.py"',
            'make quick PATHS="tests/unit/foo_test_a.py"',
        )
        assert gt.slug(a)[:41] == gt.slug(b)[:41], "the folded prefixes collide on purpose"
        assert gt.stamp_path(_store(tmp_path), "t" * 40, a) != gt.stamp_path(
            _store(tmp_path), "t" * 40, b
        )
        assert len(gt.slug("x" * 200)) == 40 + 1 + 8

    def test_a_single_pass_stability_line_is_its_own_key(self, tmp_path):
        """`COUNT=1` is a make variable, not a goal; the key (and its stamp) keeps it."""
        once, soak = "make stability-unit COUNT=1", "make stability-unit"
        assert gt.goals(once) == ["stability-unit"]
        assert not gt.is_live(once)
        assert gt.lane_key(once) == once
        assert gt.lane_key(f"{once} RANGE=abc..HEAD") == once
        assert gt.slug(once).startswith("make_stability-unit_COUNT_1-")
        root = _store(tmp_path)
        assert gt.stamp_path(root, "t" * 40, once) != gt.stamp_path(root, "t" * 40, soak)

    def test_the_tunnel_smoke_line_is_one_goal_and_its_own_key(self, tmp_path):
        """The tunnel tier's single pass also sets its depth: `CYCLES=2` is a variable too."""
        smoke = gs.STABILITY_TUNNEL
        assert smoke == "make stability-tunnel COUNT=1 CYCLES=2"
        assert gt.goals(smoke) == ["stability-tunnel"]
        assert not gt.is_live(smoke)
        assert gt.lane_key(smoke) == smoke
        root = _store(tmp_path)
        assert gt.stamp_path(root, "t" * 40, smoke) != gt.stamp_path(
            root, "t" * 40, "make stability-tunnel COUNT=1"
        )

    def test_a_malformed_stamp_is_unreadable_not_evidence(self, tmp_path):
        root = _store(tmp_path)
        path = gt.stamp_path(root, "t" * 40, "make coverage-bed")
        path.parent.mkdir(parents=True)
        good = {
            "format": 1,
            "tree": "t" * 40,
            "command": "make coverage-bed",
            "base": "b" * 40,
            "measured": {},
            "written_at": "now",
            "checkout": "/x",
        }
        path.write_text(json.dumps(good))
        assert gt.read_stamp(path) is not None
        for bad in (
            "{not json",
            json.dumps({"format": 1, "tree": "t" * 40}),
            json.dumps({**good, "format": 2}),
            json.dumps({**good, "tree": 7}),
            json.dumps({**good, "command": ["make", "coverage"]}),
            json.dumps({**good, "measured": ["schemas/support_matrix.json"]}),
            json.dumps({**good, "measured": {"schemas/support_matrix.json": 1}}),
            json.dumps({**good, "promoted_from": 3}),
            json.dumps({**good, "command": 'make quick PATHS="unbalanced'}),
            json.dumps({**good, "command": "make a-goal-this-runner-never-knew"}),
            json.dumps([good]),
        ):
            path.write_text(bad)
            assert gt.read_stamp(path) is None, bad
        repo = _repo(tmp_path)
        assert gt.lookup(repo, root, "t" * 40, "make coverage-bed").reason == "unreadable stamp"
        store = gt.load_store(root)
        assert store.stamps == []
        assert store.unreadable == [path]

    def test_an_unreadable_file_under_another_tree_is_named_not_no_stamp(self, tmp_path):
        repo, root = _repo(tmp_path), _store(tmp_path)
        other = gt.stamp_path(root, "o" * 40, "make coverage-bed")
        other.parent.mkdir(parents=True)
        other.write_text("{not json")
        assert (
            gt.lookup(repo, root, tree_hash(repo), "make coverage-bed").reason == "unreadable stamp"
        )

    def test_a_corrupt_exact_file_does_not_hide_valid_promoted_evidence(self, tmp_path):
        repo, root = _repo(tmp_path), _store(tmp_path)
        s_tree = tree_hash(repo)
        gt.write_stamp(
            root, tree=s_tree, key="make coverage-bed", base="b", measured={}, checkout=str(repo)
        )
        blob = _measure(repo, '{"as_of": "2026-10-16"}\n')
        gt.write_stamp(
            root,
            tree=s_tree,
            key="make conformance-bed",
            base="b",
            measured={"schemas/support_matrix.json": blob},
            checkout=str(repo),
        )
        _git(repo, "commit", "-qam", "measured")
        t_tree = tree_hash(repo)
        exact = gt.stamp_path(root, t_tree, "make coverage-bed")
        exact.parent.mkdir(parents=True)
        exact.write_text("{crashed mid-write")
        found = gt.lookup(repo, root, t_tree, "make coverage-bed")
        assert found.stamp is not None
        assert found.stamp.tree == s_tree

    def test_a_file_claiming_another_tree_never_satisfies_through_that_trees_witness(
        self, tmp_path
    ):
        repo, root = _repo(tmp_path), _store(tmp_path)
        s_tree = tree_hash(repo)
        blob = _measure(repo, '{"as_of": "2026-10-16"}\n')
        gt.write_stamp(
            root,
            tree=s_tree,
            key="make conformance-bed",
            base="b",
            measured={"schemas/support_matrix.json": blob},
            checkout=str(repo),
        )
        _git(repo, "commit", "-qam", "measured")
        t_tree = tree_hash(repo)
        # a coverage "stamp" filed under <T>/ but claiming S: with S's witness it would promote
        path = gt.write_stamp(
            root, tree=t_tree, key="make coverage-bed", base="b", measured={}, checkout="/x"
        )
        data = json.loads(path.read_text())
        data["tree"] = s_tree
        path.write_text(json.dumps(data))
        assert gt.find_stamp(repo, root, t_tree, "make coverage-bed") is None
        assert path in gt.load_store(root).unreadable

    def test_a_stamp_whose_command_is_not_the_key_is_a_mismatch(self, tmp_path):
        repo, root = _repo(tmp_path), _store(tmp_path)
        path = gt.write_stamp(
            root, tree="t" * 40, key="make coverage-bed", base="b", measured={}, checkout="/x"
        )
        data = json.loads(path.read_text())
        data["command"] = "make kmodcov"  # a digest collision, or a hand edit
        path.write_text(json.dumps(data))
        found = gt.lookup(repo, root, "t" * 40, "make coverage-bed")
        assert found.stamp is None
        assert found.reason.startswith("command mismatch")
        store = gt.load_store(root)
        assert store.stamps == []
        assert store.mismatched == [gt.Mismatch(path, "make kmodcov")]
        assert gt.find_stamp(repo, root, "t" * 40, "make kmodcov") is None, (
            "never evidence for the claimed key either"
        )
        assert gt.invalidate(repo, root, "make coverage-bed", tree="t" * 40) == [path]
        assert not path.exists()

    def test_a_misnamed_file_is_neither_candidate_nor_witness_at_another_tree(self, tmp_path):
        repo, root = _repo(tmp_path), _store(tmp_path)
        s_tree = tree_hash(repo)
        blob = _measure(repo, '{"as_of": "2026-10-16"}\n')
        # a conformance-bed-named file under S edited to claim kmodcov AND to carry the witness
        path = gt.write_stamp(
            root,
            tree=s_tree,
            key="make conformance-bed",
            base="b",
            measured={"schemas/support_matrix.json": blob},
            checkout=str(repo),
        )
        data = json.loads(path.read_text())
        data["command"] = "make kmodcov"
        path.write_text(json.dumps(data))
        gt.write_stamp(
            root, tree=s_tree, key="make coverage-bed", base="b", measured={}, checkout=str(repo)
        )
        _git(repo, "commit", "-qam", "measured")
        t_tree = tree_hash(repo)
        assert gt.find_stamp(repo, root, t_tree, "make kmodcov") is None
        assert gt.find_stamp(repo, root, t_tree, "make coverage-bed") is None, (
            "the misnamed file's blob is no witness"
        )

    def test_exact_evidence_wins_without_a_store_scan(self, tmp_path, monkeypatch):
        repo, root = _repo(tmp_path), _store(tmp_path)
        s_tree = tree_hash(repo)
        # a REAL promotable candidate: S's coverage stamp plus S's measurement witness for T
        gt.write_stamp(
            root, tree=s_tree, key="make coverage-bed", base="b", measured={}, checkout="/elsewhere"
        )
        blob = _measure(repo, '{"as_of": "2026-10-16"}\n')
        gt.write_stamp(
            root,
            tree=s_tree,
            key="make conformance-bed",
            base="b",
            measured={"schemas/support_matrix.json": blob},
            checkout="/elsewhere",
        )
        _git(repo, "commit", "-qam", "measured")
        t_tree = tree_hash(repo)
        assert gt.find_stamp(repo, root, t_tree, "make coverage-bed").checkout == "/elsewhere", (
            "promotable on its own"
        )
        gt.write_stamp(
            root, tree=t_tree, key="make coverage-bed", base="b", measured={}, checkout=str(repo)
        )
        reads = []
        real = gt._read_record
        monkeypatch.setattr(gt, "_read_record", lambda p: (reads.append(p), real(p))[1])
        found = gt.lookup(repo, root, t_tree, "make coverage-bed")
        assert found.stamp is not None
        assert found.stamp.tree == t_tree
        assert found.stamp.checkout == str(repo)
        assert found.store is None, "no store was loaded"
        assert reads == [gt.stamp_path(root, t_tree, "make coverage-bed")], "one read, no scan"

    def test_a_genuine_file_name_collision_is_a_command_mismatch(self, tmp_path, monkeypatch):
        repo, root = _repo(tmp_path), _store(tmp_path)
        monkeypatch.setattr(gt, "slug", lambda key: "collide")  # two known keys, one file name
        tree = tree_hash(repo)
        gt.write_stamp(
            root, tree=tree, key="make kmodcov", base="b", measured={}, checkout=str(repo)
        )
        found = gt.lookup(repo, root, tree, "make coverage-bed")
        assert found.stamp is None
        assert (
            found.reason == "command mismatch (the file at the key's path records 'make kmodcov')"
        )
        assert gt.find_stamp(repo, root, tree, "make kmodcov") is not None, (
            "the file is still its own key's evidence"
        )

    def test_an_exact_path_stamp_that_claims_another_tree_is_not_evidence(self, tmp_path):
        repo, root = _repo(tmp_path), _store(tmp_path)
        path = gt.write_stamp(
            root, tree="t" * 40, key="make coverage-bed", base="b", measured={}, checkout="/x"
        )
        data = json.loads(path.read_text())
        data["tree"] = "u" * 40
        path.write_text(json.dumps(data))
        assert gt.find_stamp(repo, root, "t" * 40, "make coverage-bed") is None


class TestSatisfies:
    def test_the_exact_tree(self, tmp_path):
        repo, root = _repo(tmp_path), _store(tmp_path)
        tree = tree_hash(repo)
        gt.write_stamp(
            root, tree=tree, key="make coverage-bed", base="b", measured={}, checkout=str(repo)
        )
        assert gt.find_stamp(repo, root, tree, "make coverage-bed") is not None

    def test_a_tree_that_differs_only_by_a_recorded_measured_blob(self, tmp_path):
        repo, root = _repo(tmp_path), _store(tmp_path)
        s_tree = tree_hash(repo)
        gt.write_stamp(
            root, tree=s_tree, key="make coverage-bed", base="b", measured={}, checkout=str(repo)
        )
        blob = _measure(repo, '{"as_of": "2026-10-16"}\n')
        gt.write_stamp(
            root,
            tree=s_tree,
            key="make conformance-bed",
            base="b",
            measured={"schemas/support_matrix.json": blob},
            checkout=str(repo),
        )
        _git(repo, "commit", "-qam", "measured")
        t_tree = tree_hash(repo)
        coverage = gt.find_stamp(repo, root, t_tree, "make coverage-bed")
        assert coverage is not None, "promoted through the measuring stamp"
        assert coverage.tree == s_tree
        assert gt.find_stamp(repo, root, t_tree, "make conformance-bed") is not None

    def test_a_recorded_blob_followed_by_different_bytes_inherits_nothing(self, tmp_path):
        repo, root = _repo(tmp_path), _store(tmp_path)
        s_tree = tree_hash(repo)
        gt.write_stamp(
            root, tree=s_tree, key="make coverage-bed", base="b", measured={}, checkout=str(repo)
        )
        blob = _measure(repo, '{"as_of": "2026-10-16"}\n')
        gt.write_stamp(
            root,
            tree=s_tree,
            key="make conformance-bed",
            base="b",
            measured={"schemas/support_matrix.json": blob},
            checkout=str(repo),
        )
        _measure(repo, '{"as_of": "2026-10-16", "hand": "edited"}\n')
        _git(repo, "commit", "-qam", "edited after the measurement")
        found = gt.lookup(repo, root, tree_hash(repo), "make coverage-bed")
        assert found.stamp is None
        assert "beyond the measured outputs" in found.reason
        assert s_tree[:12] in found.reason

    def test_a_matrix_only_commit_with_no_run_behind_it_inherits_nothing(self, tmp_path):
        repo, root = _repo(tmp_path), _store(tmp_path)
        gt.write_stamp(
            root,
            tree=tree_hash(repo),
            key="make coverage-bed",
            base="b",
            measured={},
            checkout=str(repo),
        )
        _measure(repo, '{"as_of": "hand-edited"}\n')
        _git(repo, "commit", "-qam", "edit")
        assert gt.find_stamp(repo, root, tree_hash(repo), "make coverage-bed") is None

    def test_any_witnessed_blob_for_the_path_counts(self, tmp_path):
        """Two lanes under one tree may record different blobs for the same matrix."""
        repo, root = _repo(tmp_path), _store(tmp_path)
        s_tree = tree_hash(repo)
        first = _measure(repo, '{"as_of": "first"}\n')
        gt.write_stamp(
            root,
            tree=s_tree,
            key="make conformance-bed",
            base="b",
            measured={"schemas/support_matrix.json": first},
            checkout=str(repo),
        )
        second = _measure(repo, '{"as_of": "second"}\n')
        gt.write_stamp(
            root,
            tree=s_tree,
            key="make kmodcov",
            base="b",
            measured={"schemas/support_matrix.json": second},
            checkout=str(repo),
        )
        assert gt.witnesses(gt.load_store(root).stamps, s_tree) == {
            "schemas/support_matrix.json": {first, second}
        }
        _measure(repo, '{"as_of": "first"}\n')
        _git(repo, "commit", "-qam", "commit the first measurement")
        assert gt.find_stamp(repo, root, tree_hash(repo), "make kmodcov") is not None

    def test_a_difference_outside_the_measured_outputs_inherits_nothing(self, tmp_path):
        repo, root = _repo(tmp_path), _store(tmp_path)
        s_tree = tree_hash(repo)
        gt.write_stamp(
            root, tree=s_tree, key="make coverage-bed", base="b", measured={}, checkout=str(repo)
        )
        gt.write_stamp(
            root,
            tree=s_tree,
            key="make conformance-bed",
            base="b",
            measured={"schemas/support_matrix.json": "0" * 40},
            checkout=str(repo),
        )
        (repo / "tests" / "unit" / "test_a.py").write_text("def test_a():\n    assert True\n")
        _git(repo, "commit", "-qam", "code")
        assert gt.find_stamp(repo, root, tree_hash(repo), "make coverage-bed") is None

    def test_diff_tree_is_recursive(self, tmp_path):
        repo, root = _repo(tmp_path), _store(tmp_path)
        s_tree = tree_hash(repo)
        blob = _measure(repo, '{"as_of": "2026-10-16"}\n')
        gt.write_stamp(
            root,
            tree=s_tree,
            key="make conformance-bed",
            base="b",
            measured={"schemas/support_matrix.json": blob},
            checkout=str(repo),
        )
        _git(repo, "commit", "-qam", "measured")
        assert gt.find_stamp(repo, root, tree_hash(repo), "make conformance-bed") is not None

    def test_a_pruned_tree_is_unreadable_never_an_exception(self, tmp_path):
        repo, root = _repo(tmp_path), _store(tmp_path)
        gt.write_stamp(
            root,
            tree="f" * 40,
            key="make conformance-bed",
            base="b",
            measured={"schemas/support_matrix.json": "0" * 40},
            checkout=str(repo),
        )
        found = gt.lookup(repo, root, tree_hash(repo), "make conformance-bed")
        assert found.stamp is None
        assert found.reason == "unreadable stamp"
        # the same with NO witness under the pruned tree: still unreadable, never "differs beyond"
        gt.write_stamp(
            root, tree="e" * 40, key="make coverage-bed", base="b", measured={}, checkout=str(repo)
        )
        assert (
            gt.lookup(repo, root, tree_hash(repo), "make coverage-bed").reason == "unreadable stamp"
        )

    def test_a_different_path_list_names_both_bases(self, tmp_path):
        repo, root = _repo(tmp_path), _store(tmp_path)
        gt.write_stamp(
            root,
            tree=tree_hash(repo),
            key='make quick PATHS="tests/unit/a"',
            base="a" * 40,
            measured={},
            checkout=str(repo),
        )
        found = gt.lookup(
            repo, root, tree_hash(repo), 'make quick PATHS="tests/unit/b"', base="r" * 40
        )
        assert found.stamp is None
        assert (
            found.reason
            == f"stamped for a different path list (base {'a' * 12} vs the remote's {'r' * 12})"
        )

    def test_a_different_path_list_without_a_base_names_only_the_stamped_one(self, tmp_path):
        repo, root = _repo(tmp_path), _store(tmp_path)
        tree = tree_hash(repo)
        gt.write_stamp(
            root,
            tree=tree,
            key='make quick PATHS="tests/unit/a"',
            base="a" * 40,
            measured={},
            checkout=str(repo),
        )
        found = gt.lookup(repo, root, tree, 'make quick PATHS="tests/unit/b"')
        assert found.reason == f"stamped for a different path list (base {'a' * 12})"
        assert "vs the remote's" not in found.reason

    def test_a_miss_hands_back_the_store_it_was_given(self, tmp_path):
        repo, root = _repo(tmp_path), _store(tmp_path)
        given = gt.load_store(root)
        found = gt.lookup(repo, root, tree_hash(repo), "make coverage-bed", store=given)
        assert found.stamp is None
        assert found.store is given

    def test_lookup_reads_the_store_once(self, tmp_path, monkeypatch):
        repo, root = _repo(tmp_path), _store(tmp_path)
        for tree in ("a" * 40, "b" * 40, "c" * 40):
            gt.write_stamp(
                root,
                tree=tree,
                key="make kmodcov",
                base="b",
                measured={"schemas/support_matrix.json": "0" * 40},
                checkout="/x",
            )
            gt.write_stamp(
                root, tree=tree, key="make coverage-bed", base="b", measured={}, checkout="/x"
            )
        reads = []
        real = gt._read_record
        monkeypatch.setattr(gt, "_read_record", lambda p: (reads.append(p), real(p))[1])
        gt.lookup(repo, root, tree_hash(repo), "make coverage-bed")
        assert len(reads) == 1 + 6, "the exact probe, then one read per stamp file, no re-reads"

    def test_lookup_reads_each_non_stamp_file_once(self, tmp_path, monkeypatch):
        repo, root = _repo(tmp_path), _store(tmp_path)
        tree = tree_hash(repo)
        gt.write_red(root, tree="a" * 40, key="make coverage-bed", checkout="/x")
        bad = gt.stamp_path(root, "b" * 40, "make coverage-bed")
        bad.parent.mkdir(parents=True)
        bad.write_text("{bad")
        reads = []
        real = gt._read_record
        monkeypatch.setattr(gt, "_read_record", lambda p: (reads.append(p), real(p))[1])
        gt.lookup(repo, root, tree, "make coverage-bed")
        assert sorted(reads) == sorted(
            [
                gt.stamp_path(root, tree, "make coverage-bed"),
                bad,
                gt.stamp_path(root, "a" * 40, "make coverage-bed"),
            ]
        ), "the exact probe once, then each file once"


class TestInvalidate:
    """invalidate deletes only what cannot still be true (#650)."""

    def test_an_unreadable_file_at_the_keys_name_goes_too(self, tmp_path):
        repo, root = _repo(tmp_path), _store(tmp_path)
        path = gt.stamp_path(root, "z" * 40, "make coverage-bed")
        path.parent.mkdir(parents=True)
        path.write_text("{bad")
        assert gt.invalidate(repo, root, "make coverage-bed", tree="z" * 40) == [path]
        assert not path.exists()

    def test_a_pruned_trees_stamps_and_markers_for_the_key_go_whoever_wrote_them(self, tmp_path):
        repo, root = _repo(tmp_path), _store(tmp_path)
        gated = tree_hash(repo)
        gone = [
            gt.write_stamp(
                root, tree="a" * 40, key="make coverage-bed", base="b", measured={}, checkout="/x"
            ),
            gt.write_stamp(
                root, tree="b" * 40, key="make coverage-bed", base="b", measured={}, checkout="/y"
            ),
            gt.write_red(root, tree="c" * 40, key="make coverage-bed", checkout="/y"),
        ]
        kept = gt.write_stamp(
            root, tree="a" * 40, key="make kmodcov", base="b", measured={}, checkout="/x"
        )
        assert sorted(gt.invalidate(repo, root, "make coverage-bed", tree=gated)) == sorted(gone)
        assert not any(p.exists() for p in gone)
        assert gt.read_stamp(kept) is not None, "another key is never touched"

    def test_the_pruned_check_spawns_git_once_however_many_trees(self, tmp_path, monkeypatch):
        """Perf in operations: one ``cat-file --batch-check`` for every tree, under the lock."""
        repo, root = _repo(tmp_path), _store(tmp_path)
        live = [_live_tree(repo, str(n)) for n in range(3)]
        dead = [c * 40 for c in "abcd"]
        for tree in live + dead:
            gt.write_stamp(root, tree=tree, key=LANE, base="b", measured={}, checkout="/x")
        gt.write_red(root, tree="e" * 40, key=LANE, checkout="/y")
        gated = tree_hash(repo)
        spawns = []
        real = subprocess.run

        def counting(argv, *args, **kwargs):
            spawns.append(list(argv))
            return real(argv, *args, **kwargs)

        monkeypatch.setattr(subprocess, "run", counting)
        gone = gt.invalidate(repo, root, LANE, tree=gated)
        monkeypatch.undo()
        assert [argv[:3] for argv in spawns] == [["git", "cat-file", "--batch-check"]]
        assert sorted(gone) == sorted(gt.stamp_path(root, t, LANE) for t in [*dead, "e" * 40])
        assert all(gt.stamp_path(root, t, LANE).exists() for t in live)

    def test_nothing_for_the_key_spawns_no_git(self, tmp_path, monkeypatch):
        repo, root = _repo(tmp_path), _store(tmp_path)
        gt.write_stamp(
            root, tree="a" * 40, key="make kmodcov", base="b", measured={}, checkout="/x"
        )
        spawns = []
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: spawns.append(a))
        assert gt.invalidate(repo, root, LANE, tree="z" * 40) == []
        assert spawns == []

    def test_a_peer_checkouts_stamp_for_another_tree_survives(self, tmp_path):
        """#650: gating tree A from checkout X never deletes tree B's stamp from checkout Y."""
        repo, root = _repo(tmp_path), _store(tmp_path)
        a_tree, b_tree = tree_hash(repo), _live_tree(repo, "b")
        peer = gt.write_stamp(
            root, tree=b_tree, key="make coverage-bed", base="b", measured={}, checkout="/y"
        )
        assert gt.invalidate(repo, root, "make coverage-bed", tree=a_tree) == []
        assert gt.read_stamp(peer) is not None

    def test_gating_landing_c_keeps_landing_as_stamps_from_this_checkout(self, tmp_path):
        """Gate landing A, then landing C in the same worktree before A is pushed."""
        repo, root = _repo(tmp_path), _store(tmp_path)
        a_tree, c_tree = _live_tree(repo, "a"), tree_hash(repo)
        a_stamp = gt.write_stamp(
            root, tree=a_tree, key="make coverage-bed", base="b", measured={}, checkout="/x"
        )
        assert gt.invalidate(repo, root, "make coverage-bed", tree=c_tree) == []
        assert gt.read_stamp(a_stamp) is not None

    def test_the_gated_trees_own_stamp_stays_for_the_lanes_outcome_to_replace(self, tmp_path):
        repo, root = _repo(tmp_path), _store(tmp_path)
        u_tree = tree_hash(repo)
        own = gt.write_stamp(
            root, tree=u_tree, key="make coverage-bed", base="b", measured={}, checkout="/y"
        )
        assert gt.invalidate(repo, root, "make coverage-bed", tree=u_tree) == []
        assert gt.read_stamp(own) is not None

    def test_an_unreadable_or_mismatched_file_under_another_tree_is_left_alone(self, tmp_path):
        repo, root = _repo(tmp_path), _store(tmp_path)
        unreadable = gt.stamp_path(root, "b" * 40, "make coverage-bed")
        unreadable.parent.mkdir(parents=True)
        unreadable.write_text("{bad")
        mismatched = gt.write_stamp(
            root, tree="c" * 40, key="make coverage-bed", base="b", measured={}, checkout="/x"
        )
        data = json.loads(mismatched.read_text())
        data["command"] = "make kmodcov"
        mismatched.write_text(json.dumps(data))
        assert gt.invalidate(repo, root, "make coverage-bed", tree=tree_hash(repo)) == []
        assert unreadable.exists()
        assert mismatched.exists()

    def test_a_peers_write_between_load_and_delete_survives(self, tmp_path, monkeypatch):
        """The load and the delete hold the store lock, so a peer cannot land in between."""
        repo, root = _repo(tmp_path), _store(tmp_path)
        u_tree = tree_hash(repo)
        corrupt = gt.stamp_path(root, u_tree, LANE)
        corrupt.parent.mkdir(parents=True)
        corrupt.write_text("{bad")
        deferred: list = []
        real = gt.load_store

        def load_then_peer(where):
            store = real(where)
            _peer_write(
                root, lambda: gt.write_red(root, tree=u_tree, key=LANE, checkout="/peer"), deferred
            )
            return store

        monkeypatch.setattr(gt, "load_store", load_then_peer)
        gt.invalidate(repo, root, LANE, tree=u_tree)
        monkeypatch.undo()
        for write in deferred:
            write()
        assert [r.checkout for r in gt.load_store(root).red] == ["/peer"], "the peer's marker"
        assert gt.lookup(repo, root, u_tree, LANE).reason.startswith("the lane went red")


def _probe(root: Path, mode: int) -> bool:
    """Return whether a fresh open of the lock file gets *mode* without waiting."""
    import fcntl

    with (root / gate_stamp_common.LOCK_FILE).open("a") as handle:
        try:
            fcntl.flock(handle.fileno(), mode | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return True


class TestStoreLock:
    def test_shared_holders_coexist_and_exclude_a_writer(self, tmp_path):
        import fcntl

        root = _store(tmp_path)
        root.mkdir()
        with gt.store_lock(root, shared=True):
            assert _probe(root, fcntl.LOCK_SH), "shared plus shared does not block"
            assert not _probe(root, fcntl.LOCK_EX), "shared blocks a writer"
        with gt.store_lock(root):
            assert not _probe(root, fcntl.LOCK_SH), "a writer blocks a reader"

    def test_a_shared_lock_without_a_store_creates_nothing(self, tmp_path):
        root = _store(tmp_path)
        with gt.store_lock(root, shared=True):
            pass
        assert not root.exists()

    def test_two_holders_serialize(self, tmp_path):
        root = _store(tmp_path)
        child = (
            "import fcntl, sys\n"
            f"sys.path.insert(0, {str(CHECKOUT_ROOT)!r})\n"
            "from pathlib import Path\n"
            "from scripts import gate_stamp_common, gate_tip as gt\n"
            f"root = Path({str(root)!r})\n"
            "with open(root / gate_stamp_common.LOCK_FILE, 'a') as h:\n"
            "    try:\n"
            "        fcntl.flock(h.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
            "        print('free', flush=True)\n"
            "    except BlockingIOError:\n"
            "        print('blocked', flush=True)\n"
            "with gt.store_lock(root):\n"
            "    print('held', flush=True)\n"
        )
        with gt.store_lock(root):
            assert (root / gate_stamp_common.LOCK_FILE).is_file(), "created if missing"
            proc = subprocess.Popen(
                [sys.executable, "-c", child], stdout=subprocess.PIPE, text=True
            )
            try:
                assert proc.stdout is not None
                assert proc.stdout.readline() == "blocked\n", "the peer waits on our lock"
            except BaseException:
                proc.kill()
                raise
        out, _ = proc.communicate(timeout=60)
        assert out == "held\n", "the peer gets the lock once we release it"
        assert proc.returncode == 0


class TestRedMarker:
    def test_a_red_marker_is_neither_a_stamp_nor_unreadable(self, tmp_path):
        repo, root = _repo(tmp_path), _store(tmp_path)
        tree = tree_hash(repo)
        path = gt.write_red(root, tree=tree, key="make coverage-bed", checkout="/x")
        assert path == gt.stamp_path(root, tree, "make coverage-bed")
        assert gt.read_stamp(path) is None
        assert gt.exact_stamp(root, tree, "make coverage-bed") is None
        store = gt.load_store(root)
        assert (store.stamps, store.unreadable, store.mismatched) == ([], [], [])
        assert [(r.tree, r.command, r.checkout) for r in store.red] == [
            (tree, "make coverage-bed", "/x")
        ]

    def test_a_red_marker_records_the_base_the_lane_went_red_against(self, tmp_path):
        root = tmp_path / "store"
        path = gt.write_red(
            root, tree="t" * 40, key="make coverage-bed", checkout="c", base="b" * 40
        )
        red = gt.read_entry(path)
        assert isinstance(red, gt.Red)
        assert red.base == "b" * 40
        assert json.loads(path.read_text())["base"] == "b" * 40

    def test_a_marker_without_a_base_still_reads_as_a_marker(self, tmp_path):
        path = gt.write_red(
            tmp_path / "store", tree="t" * 40, key="make coverage-bed", checkout="c"
        )
        assert "base" not in json.loads(path.read_text())
        red = gt.read_entry(path)
        assert isinstance(red, gt.Red)
        assert red.base is None

    def test_a_marker_with_a_non_string_base_is_unreadable(self, tmp_path):
        path = gt.write_red(
            tmp_path / "store", tree="t" * 40, key="make coverage-bed", checkout="c"
        )
        record = json.loads(path.read_text())
        record["base"] = 7
        path.write_text(json.dumps(record))
        assert gt.read_entry(path) is None

    def test_a_marker_with_a_null_base_reads_like_one_without(self, tmp_path):
        path = gt.write_red(
            tmp_path / "store", tree="t" * 40, key="make coverage-bed", checkout="c"
        )
        record = json.loads(path.read_text())
        record["base"] = None
        path.write_text(json.dumps(record))
        red = gt.read_entry(path)
        assert isinstance(red, gt.Red)
        assert red.base is None

    def test_the_runner_writes_the_plans_base_into_a_red_marker(self, tmp_path, monkeypatch):
        repo, _bare_repo, base, _tip = _published_src(tmp_path)
        spy = _lease_spy(
            monkeypatch,
            FakeRunner(statuses={"make coverage-bed": 1}),
            fg.scripted_gh([fg.GhReply([], stdout="{}")]),
        )
        opts = _options(repo, spy.runner, gh=spy.gh)
        assert gt.run(opts) == 1
        path = gt.stamp_path(gt.stamps_root(repo, gt.STORE), tree_hash(repo), "make coverage-bed")
        red = gt.read_entry(path)
        assert isinstance(red, gt.Red)
        assert red.base == base

    def test_published_src_is_a_landing_that_reaches_the_bed(self, tmp_path):
        repo, _bare_repo, base, tip = _published_src(tmp_path)
        plan = gt.range_plan(repo, base, tip)
        assert plan.bed is True
        assert "make coverage-bed" in plan.stamped

    def test_a_red_marker_never_promotes_to_another_tree(self, tmp_path):
        repo, root = _repo(tmp_path), _store(tmp_path)
        s_tree = tree_hash(repo)
        blob = _measure(repo, '{"as_of": "2026-10-20"}\n')
        gt.write_stamp(
            root, tree=s_tree, key="make kmodcov", base="b", measured={SUPPORT: blob}, checkout="/x"
        )
        gt.write_red(root, tree=s_tree, key=LANE, checkout="/x")
        _git(repo, "commit", "-qam", "measured")
        u_tree = tree_hash(repo)
        assert gt.find_stamp(repo, root, u_tree, "make kmodcov") is not None, "S's witness holds"
        found = gt.lookup(repo, root, u_tree, LANE)
        assert found.stamp is None
        assert found.reason == "no stamp"

    def test_a_red_marker_on_a_live_tree_survives_invalidate(self, tmp_path):
        """Only the lane's next outcome replaces a marker, so an interrupted re-run keeps it."""
        repo, root = _repo(tmp_path), _store(tmp_path)
        u_tree, c_tree = tree_hash(repo), _live_tree(repo, "c")
        mine = gt.write_red(root, tree=u_tree, key=LANE, checkout="/x")
        other = gt.write_red(root, tree=c_tree, key=LANE, checkout="/x")
        assert gt.invalidate(repo, root, LANE, tree=u_tree) == []
        assert mine.exists()
        assert other.exists()


class FakeRunner:
    """Records every (cmd, cwd, env); answers from a table; runs side effects for a lane."""

    def __init__(self, statuses=None, outputs=None, effects=None):
        self.calls = []  # (cmd as given, cwd, env)
        self.statuses = dict(statuses or {})  # str key -> status; default 0
        self.outputs = dict(outputs or {})  # str key -> output text (argv calls)
        self.effects = dict(effects or {})  # str key -> callable() run before answering

    @staticmethod
    def key(cmd):
        return cmd if isinstance(cmd, str) else " ".join(cmd)

    def __call__(self, cmd, cwd, env):
        key = self.key(cmd)
        self.calls.append((cmd, cwd, dict(env)))
        if key in self.effects:
            self.effects[key]()
        return gt.RunOutcome(self.statuses.get(key, 0), self.outputs.get(key, ""))

    @property
    def keys(self):
        return [self.key(c) for c, _, _ in self.calls]


LANE = gs.STABILITY_UNIX
SUPPORT = "schemas/support_matrix.json"


def _options(repo, runner, **kw):
    kw.setdefault("gh", fg.scripted_gh([fg.GhReply([], stdout="{}")]))
    kw.setdefault("lab", Path("lab.json"))
    return gt.Options(repo=repo, runner=runner, out=io.StringIO(), **kw)


def _oracle(monkeypatch, repo, lines):
    """Make *lines* the oracle's tip for HEAD: a Plan whose base and tip are HEAD itself."""
    head = _git(repo, "rev-parse", "HEAD").strip()
    plan = gs.Plan(
        base_sha=head,
        tip_sha=head,
        tree=tree_hash(repo),
        live=[line for line in lines if gs.is_live(line)],
        stamped=[gs.lane_key(line) for line in lines if not gs.is_live(line)],
        bed=any(gs.is_bed(line) for line in lines),
        classes=[],
    )
    monkeypatch.setattr(gt, "_plan", lambda _opts: plan)
    return head


def _stamp(repo, key, tree=None, **kw):
    gt.write_stamp(
        gt.stamps_root(repo, gt.STORE),
        tree=tree or tree_hash(repo),
        key=key,
        base="b",
        measured=kw.pop("measured", {}),
        checkout=kw.pop("checkout", str(repo)),
        **kw,
    )


def _snapshot(root):
    return {p: p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def _go(monkeypatch, repo, lines, runner, **kw):
    """Run the runner over *lines* and return (status, output, options)."""
    _oracle(monkeypatch, repo, lines)
    opts = _options(repo, runner, **kw)
    return gt.run(opts), opts.out.getvalue(), opts


@dataclasses.dataclass
class _Spy:
    """The order the run touched the lanes, the lease, the reap and ``gh`` in."""

    events: "list[str]"
    runner: object
    gh: object


class _SpiedGh:
    """A ``gh`` that logs ``gh api`` into the spy's events, then answers from *gh*."""

    def __init__(self, events: "list[str]", gh) -> None:
        self._events = events
        self._gh = gh
        self.calls = gh.calls  # what the scripted runner recorded

    def __call__(self, args):
        self._events.append("gh api")
        return self._gh(args)


def _lease_spy(monkeypatch, runner: FakeRunner, gh: GhRunner) -> _Spy:
    """Record every lane run, lease step, reap and gh call, in order, into one list."""
    events: "list[str]" = []

    def spied_runner(cmd, cwd, env):
        events.append(f"run {FakeRunner.key(cmd)}")
        return runner(cmd, cwd, env)

    def acquire(ctx):
        events.append("acquire")
        ctx.lease = gt.Lease(cell=None, pid=os.getpid())

    def release(ctx):
        events.append("release")
        ctx.lease = None

    monkeypatch.setattr(gt, "_acquire_lease", acquire)
    monkeypatch.setattr(gt, "_release_lease", release)
    # by module path: gate_tip imports bed_reap lazily, inside the interrupt arm
    monkeypatch.setattr("scripts.bed_reap.reap", lambda cell, pid: events.append("reap") or 0)
    return _Spy(events, spied_runner, _SpiedGh(events, gh))


def _success_body(monkeypatch, repo, lines) -> str:
    """Return the commit-status JSON of HEAD carrying the success a green gate of *lines* posts."""
    _oracle(monkeypatch, repo, lines)
    desc = lgs.format_description(lgs.expected(gt._plan(_options(repo, FakeRunner()))))
    status = {"context": "local-gate", "state": "success", "description": desc}
    return json.dumps({"statuses": [status]})


PEER_PROBE = (
    "import fcntl, sys\n"
    "h = open(sys.argv[1], 'a')\n"
    "try:\n"
    "    fcntl.flock(h, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
    "    print('free')\n"
    "except BlockingIOError:\n"
    "    print('busy')\n"
)
"""A peer process asking for the store lock exclusively, without waiting: free or busy."""


def _peer_probe(lock: Path) -> str:
    return subprocess.run(
        [sys.executable, "-c", PEER_PROBE, str(lock)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    ).stdout.strip()


def _inject_on_promotion(monkeypatch, key: str, write) -> "list[str]":
    """Run *write* as a peer would, as the runner takes the exclusive lock to promote *key*.

    The runner's unlocked lookup for *key* arms it; the next exclusive take of the store lock
    runs *write* first (once), then takes the real lock. Returns the keys it fired for.
    """
    fired: "list[str]" = []
    armed: "list[bool]" = []
    real_lookup, real_lock = gt.lookup, gt.store_lock

    def spied_lookup(repo, root, tree, asked, *args, **kw):
        found = real_lookup(repo, root, tree, asked, *args, **kw)
        if asked == key and not fired:
            armed.append(True)
        return found

    @contextlib.contextmanager
    def lock(root, *, shared=False, **kw):
        if armed and not shared and not fired:
            armed.clear()
            write()
            fired.append(key)
        with real_lock(root, shared=shared, **kw):
            yield

    monkeypatch.setattr(gt, "lookup", spied_lookup)
    monkeypatch.setattr(gt, "store_lock", lock)
    return fired


class TestRunner:
    def test_the_runner_executes_the_stamped_lines_in_order_and_never_a_live_one(
        self, tmp_path, monkeypatch
    ):
        repo = _repo(tmp_path)
        for name in ("MAKEFLAGS", "MFLAGS", "PYTEST_ADDOPTS", "BASE"):
            monkeypatch.setenv(name, "x")
        budget = ("BED_LEASE_WAIT", "BED_LEASE_TTL", "BED_LEASE_DRAIN")
        for name in budget:
            monkeypatch.setenv(name, "9")
        lines = ["make check-breaking RANGE=x..HEAD", "make coverage-bed", LANE]
        runner = FakeRunner()
        status, out, _ = _go(monkeypatch, repo, lines, runner)
        assert runner.keys == ["make coverage-bed", LANE]
        for _cmd, _cwd, env in runner.calls:
            assert not {"MAKEFLAGS", "MFLAGS", "PYTEST_ADDOPTS", "BASE"} & set(env)
            assert not set(budget) & set(env)
        root, tree = gt.stamps_root(repo, gt.STORE), tree_hash(repo)
        assert gt.exact_stamp(root, tree, "make coverage-bed") is not None
        assert gt.exact_stamp(root, tree, LANE) is not None
        assert "\ngate-tip: 2 stamped, 0 already stamped, 1 live at push, 0 red\n" in out
        assert status == 0

    def test_lane_env_drops_the_runners_lease_budget_knobs(self):
        env = gt.lane_env(
            {
                "BED_LEASE_WAIT": "900",
                "BED_LEASE_TTL": "60",
                "BED_LEASE_DRAIN": "5",
                "PATH": "/bin",
            }
        )
        assert env == {"PATH": "/bin"}

    def test_lane_env_keeps_the_lease_root_so_a_lane_reenters_the_gates_hold(self):
        assert gt.lane_env({"BED_LEASE_ROOT": "/r"}) == {"BED_LEASE_ROOT": "/r"}

    def test_an_overridden_variable_refuses_before_any_lane(self, tmp_path, monkeypatch):
        repo, runner = _repo(tmp_path), FakeRunner()
        status, out, _ = _go(monkeypatch, repo, [LANE], runner, overridden=["BUMP"])
        assert status == 2
        assert runner.calls == []
        assert "came from the command line" in out

    def test_dirt_refuses_before_any_lane(self, tmp_path, monkeypatch):
        repo, runner = _repo(tmp_path), FakeRunner()
        test_a = repo / "tests" / "unit" / "test_a.py"
        test_a.write_text("def test_a():\n    assert True\n")
        status, out, _ = _go(monkeypatch, repo, [LANE], runner)
        assert (status, runner.calls) == (2, [])
        assert "modified or staged" in out
        _git(repo, "checkout", "--", "tests/unit/test_a.py")
        (repo / "tests" / "unit" / "test_new.py").write_text("def test_n():\n    pass\n")
        status, out, _ = _go(monkeypatch, repo, [LANE], runner)
        assert (status, runner.calls) == (2, [])
        (repo / "tests" / "unit" / "test_new.py").unlink()
        (repo / "note.md").write_text("scratch\n")
        status, _out, _ = _go(monkeypatch, repo, [LANE], runner)
        assert status == 0
        assert runner.keys == [LANE]

    def test_dirt_is_rechecked_before_every_lane(self, tmp_path, monkeypatch):
        repo = _repo(tmp_path)
        lines = [LANE, "make quick-guards"]

        def untracked():
            (repo / "tests" / "unit" / "test_new.py").write_text("def test_n():\n    pass\n")

        runner = FakeRunner(effects={LANE: untracked})
        status, out, _ = _go(monkeypatch, repo, lines, runner)
        assert status == 1
        assert runner.keys == [LANE]
        assert "NOT STAMPED" in out
        (repo / "tests" / "unit" / "test_new.py").unlink()

        def tracked():
            (repo / "tests" / "unit" / "test_a.py").write_text("def test_a():\n    assert True\n")

        runner = FakeRunner(effects={LANE: tracked})
        status, out, _ = _go(monkeypatch, repo, lines, runner)
        assert status == 1
        assert runner.keys == [LANE]
        assert "NOT STAMPED" in out

    def test_the_first_red_lane_stops_the_run_and_leaves_only_a_peers_stamp_and_a_red_marker(
        self, tmp_path, monkeypatch
    ):
        repo = _repo(tmp_path)
        root = gt.stamps_root(repo, gt.STORE)
        p_tree = _live_tree(repo, "peer")
        _stamp(repo, "make coverage-bed", tree="a" * 40)  # a and b: trees no object store holds
        _stamp(repo, "make coverage-bed", tree="b" * 40, promoted_from="a" * 40)
        _stamp(repo, "make coverage-bed", tree=p_tree, checkout="/peer")
        lines = [LANE, "make coverage-bed", "make quick-guards"]
        runner = FakeRunner(statuses={"make coverage-bed": 1})
        status, out, _ = _go(monkeypatch, repo, lines, runner, force=True)
        assert status == 1
        store = gt.load_store(root)
        assert [(s.tree, s.checkout) for s in store.stamps if s.command == "make coverage-bed"] == [
            (p_tree, "/peer")
        ], "the pruned trees' stamps go; the peer's stays"
        assert [(r.tree, r.command) for r in store.red] == [(tree_hash(repo), "make coverage-bed")]
        assert "make quick-guards" not in runner.keys
        assert gt.exact_stamp(root, tree_hash(repo), LANE) is not None
        assert "RED (exit 1" in out
        assert "1 stamped, 0 already stamped, 0 live at push, 1 red\n" in out

        def interrupt():
            raise KeyboardInterrupt

        runner = FakeRunner(effects={LANE: interrupt})
        _oracle(monkeypatch, repo, [LANE])
        assert gt.run(_options(repo, runner, force=True)) == gt.EXIT_INTERRUPTED
        assert gt.exact_stamp(root, tree_hash(repo), LANE) is not None, (
            "an interrupted forced re-run keeps the tree's earlier green stamp: still true"
        )

    def test_a_re_run_keeps_a_peer_worktrees_stamp_for_its_own_tree(self, tmp_path, monkeypatch):
        """#650: a forced re-run here never erases what a peer checkout stamped for its tree."""
        repo = _repo(tmp_path)
        root = gt.stamps_root(repo, gt.STORE)
        b_tree, c_tree = _live_tree(repo, "b"), _live_tree(repo, "c")
        _stamp(repo, LANE, tree=b_tree, checkout="/peer")
        _stamp(repo, LANE, tree=c_tree)  # an earlier landing gated here
        _stamp(repo, LANE, tree="d" * 40)  # a tree no object store holds
        runner = FakeRunner(statuses={LANE: 1})
        status, _out, _ = _go(monkeypatch, repo, [LANE], runner, force=True)
        assert status == 1
        assert runner.keys == [LANE]
        assert sorted((s.tree, s.checkout) for s in gt.load_store(root).stamps) == sorted(
            [(b_tree, "/peer"), (c_tree, str(repo))]
        ), "every stamp that can still be true stays; the pruned tree's goes"

    def test_a_forced_red_rerun_keeps_the_peers_stamp_and_promotes_nothing(
        self, tmp_path, monkeypatch
    ):
        """A red lane on U marks U red, so a peer's stamp of S never promotes to U."""
        repo = _repo(tmp_path)
        root = gt.stamps_root(repo, gt.STORE)
        s_tree = tree_hash(repo)
        b2 = _measure(repo, '{"as_of": "2026-10-20"}\n')
        _stamp(repo, LANE, tree=s_tree, checkout="/peer", measured={SUPPORT: b2})
        _git(repo, "commit", "-qam", "measured")
        u_tree = tree_hash(repo)
        assert gt.find_stamp(repo, root, u_tree, LANE) is not None, "promotable before the run"
        runner = FakeRunner(statuses={LANE: 1})
        status, _out, _ = _go(monkeypatch, repo, [LANE], runner, force=True)
        assert status == 1
        assert gt.find_stamp(repo, root, u_tree, LANE) is None
        peer = gt.exact_stamp(root, s_tree, LANE)
        assert peer is not None
        assert peer.checkout == "/peer"
        found = gt.lookup(repo, root, u_tree, LANE)
        assert found.stamp is None
        assert found.reason.startswith("the lane went red on this tree")
        assert str(repo) in found.reason

        runner = FakeRunner()
        status, _out, _ = _go(monkeypatch, repo, [LANE], runner)
        assert status == 0
        assert runner.keys == [LANE], "a red marker is never evidence: the lane runs again"
        stamp = gt.exact_stamp(root, u_tree, LANE)
        assert stamp is not None
        assert stamp.checkout == str(repo)
        assert gt.load_store(root).red == [], "the green run's stamp replaced the marker"

    def test_red_then_interrupted_rerun_still_promotes_nothing(self, tmp_path, monkeypatch):
        repo = _repo(tmp_path)
        root = gt.stamps_root(repo, gt.STORE)
        s_tree = tree_hash(repo)
        b2 = _measure(repo, '{"as_of": "2026-10-20"}\n')
        _stamp(repo, LANE, tree=s_tree, checkout="/peer", measured={SUPPORT: b2})
        _git(repo, "commit", "-qam", "measured")
        u_tree = tree_hash(repo)
        _go(monkeypatch, repo, [LANE], FakeRunner(statuses={LANE: 1}), force=True)
        assert gt.find_stamp(repo, root, u_tree, LANE) is None

        def interrupt():
            raise KeyboardInterrupt

        status, _out, _ = _go(monkeypatch, repo, [LANE], FakeRunner(effects={LANE: interrupt}))
        assert status == gt.EXIT_INTERRUPTED
        assert gt.find_stamp(repo, root, u_tree, LANE) is None, "U never passed"
        assert gt.lookup(repo, root, u_tree, LANE).reason.startswith("the lane went red")
        assert gt.exact_stamp(root, s_tree, LANE) is not None, "the peer's stamp stays"

    @staticmethod
    def _witnessed_landing(tmp_path) -> "tuple[Path, Path, str, str]":
        """Return (repo, root, S, U): U differs from S only by a measured blob S's stamp holds."""
        repo = _repo(tmp_path)
        root = gt.stamps_root(repo, gt.STORE)
        s_tree = tree_hash(repo)
        b2 = _measure(repo, '{"as_of": "2026-10-20"}\n')
        _stamp(repo, LANE, tree=s_tree, checkout="/peer", measured={SUPPORT: b2})
        _git(repo, "commit", "-qam", "measured")
        return repo, root, s_tree, tree_hash(repo)

    def test_a_peers_red_marker_at_the_exact_path_before_promotion_runs_the_lane(
        self, tmp_path, monkeypatch
    ):
        """The scan runs unlocked; the write re-reads the exact path under the lock."""
        repo, root, s_tree, u_tree = self._witnessed_landing(tmp_path)
        injected = _inject_on_promotion(
            monkeypatch, LANE, lambda: gt.write_red(root, tree=u_tree, key=LANE, checkout="/peer")
        )
        runner = FakeRunner(statuses={LANE: 1})
        status, out, _ = _go(monkeypatch, repo, [LANE], runner)
        assert injected == [LANE], "the peer's marker landed between the scan and the lock"
        assert status == 1
        assert runner.keys == [LANE], "the lane ran"
        assert f"{LANE} … already stamped" not in out
        assert "carry abandoned" not in out
        assert isinstance(gt.read_entry(gt.stamp_path(root, u_tree, LANE)), gt.Red)
        assert gt.exact_stamp(root, s_tree, LANE) is not None

    def test_a_peers_red_marker_at_another_tree_before_promotion_abandons_it(
        self, tmp_path, monkeypatch
    ):
        repo, root, _s_tree, u_tree = self._witnessed_landing(tmp_path)
        other = _live_tree(repo, "other")
        injected = _inject_on_promotion(
            monkeypatch, LANE, lambda: gt.write_red(root, tree=other, key=LANE, checkout="/peer")
        )
        runner = FakeRunner()
        status, out, _ = _go(monkeypatch, repo, [LANE], runner)
        assert injected == [LANE]
        assert status == 0
        assert f"{LANE} … carry abandoned (a red marker changed during the scan)" in out
        assert runner.keys == [LANE], "the lane ran"
        assert isinstance(gt.read_entry(gt.stamp_path(root, other, LANE)), gt.Red), "kept"
        stamp = gt.exact_stamp(root, u_tree, LANE)
        assert stamp is not None
        assert stamp.promoted_from is None, "stamped by its own run, not promoted"

    def test_the_lineage_sequence(self, tmp_path, monkeypatch):
        repo = _repo(tmp_path)
        root = gt.stamps_root(repo, gt.STORE)
        s_tree = tree_hash(repo)
        b1 = _measure(repo, '{"as_of": "2026-10-16"}\n')
        _stamp(repo, LANE)
        _stamp(repo, "make kmodcov", measured={SUPPORT: b1})
        _git(repo, "commit", "-qam", "measured")
        t_sha = _git(repo, "rev-parse", "HEAD").strip()
        t_tree = tree_hash(repo)
        bed = "make conformance-bed"
        runner = FakeRunner(
            effects={bed: lambda: _measure(repo, '{"as_of": "2026-10-17"}\n')},
        )
        status, _out, _ = _go(monkeypatch, repo, [LANE, bed], runner)
        assert status == 0
        assert runner.keys == [bed], "the lane is satisfied through the measuring stamp's witness"
        promoted = gt.exact_stamp(root, t_tree, LANE)
        assert promoted is not None
        assert promoted.promoted_from == s_tree
        bed_stamp = gt.exact_stamp(root, t_tree, bed)
        assert bed_stamp is not None
        assert list(bed_stamp.measured) == [SUPPORT]
        assert bed_stamp.measured[SUPPORT] != b1
        _git(repo, "commit", "-qam", "measured again")
        u_tree = tree_hash(repo)

        runner = FakeRunner(statuses={LANE: 1})
        status, _out, _ = _go(monkeypatch, repo, [LANE], runner, force=True)
        assert status == 1
        assert sorted(s.tree for s in gt.load_store(root).stamps if s.command == LANE) == sorted(
            [s_tree, t_tree]
        ), "S's and T's stamps are still true and stay"
        assert gt.find_stamp(repo, root, u_tree, LANE) is None, "U went red"

        _git(repo, "checkout", "-q", t_sha)
        runner = FakeRunner()
        status, out, _ = _go(monkeypatch, repo, [LANE], runner)
        assert status == 0
        assert runner.keys == [], "T's own stamp survived U's red run"
        assert "already stamped" in out
        t_stamp = gt.exact_stamp(root, t_tree, LANE)
        assert t_stamp is not None
        witnessed = gt.witnesses(gt.load_store(root).stamps, t_tree)
        assert gt.satisfies(repo, t_stamp, u_tree, witnessed), "through the bed's witness"
        found = gt.lookup(repo, root, u_tree, LANE)
        assert found.stamp is None, "U went red: nothing promotes to it until it passes"
        assert found.reason.startswith("the lane went red on this tree")

    def test_a_lane_already_stamped_is_skipped_and_a_changed_paths_key_is_run(
        self, tmp_path, monkeypatch
    ):
        repo = _repo(tmp_path)
        a, b = 'make quick PATHS="tests/unit/a"', 'make quick PATHS="tests/unit/b"'
        _stamp(repo, a)
        runner = FakeRunner()
        status, out, _ = _go(monkeypatch, repo, [a, b], runner)
        assert status == 0
        assert runner.keys == [b]
        assert "1 stamped, 1 already stamped" in out

    def test_force_runs_the_bed_half_even_with_a_valid_lane_stamp(self, tmp_path, monkeypatch):
        repo = _repo(tmp_path)
        root = gt.stamps_root(repo, gt.STORE)
        _stamp(repo, "make coverage-bed", checkout="/old")
        runner = FakeRunner()
        status, _out, _ = _go(monkeypatch, repo, ["make coverage-bed"], runner, force=True)
        assert status == 0
        assert runner.keys == ["make coverage-bed"]
        stamp = gt.exact_stamp(root, tree_hash(repo), "make coverage-bed")
        assert stamp is not None
        assert stamp.checkout == str(repo)
        runner = FakeRunner()
        status, out, _ = _go(monkeypatch, repo, ["make coverage-bed"], runner)
        assert (status, runner.calls) == (0, [])
        assert "already stamped" in out

    def test_the_real_runner_returns_merged_stderr(self, tmp_path):
        code = "import sys; sys.stderr.write('only stderr')"
        result = gt._default_runner([sys.executable, "-c", code], tmp_path, dict(os.environ))
        assert result == gt.RunOutcome(0, "only stderr")
        assert gt._default_runner("exit 3", tmp_path, dict(os.environ)) == gt.RunOutcome(3, "")

    def test_a_measuring_lane_stamps_with_the_blob_and_prints_the_review_note(
        self, tmp_path, monkeypatch
    ):
        repo = _repo(tmp_path)
        bed = "make conformance-bed"
        runner = FakeRunner(effects={bed: lambda: _measure(repo, '{"as_of": "2026-10-20"}\n')})
        status, out, _ = _go(monkeypatch, repo, [bed], runner)
        assert status == 0
        stamp = gt.exact_stamp(gt.stamps_root(repo, gt.STORE), tree_hash(repo), bed)
        assert stamp is not None
        blob = _git(repo, "hash-object", SUPPORT).strip()
        assert stamp.measured == {SUPPORT: blob}
        assert "review and commit, or restore" in out

    def test_a_second_lane_after_measured_dirt_stops_with_the_commit_then_rerun_line(
        self, tmp_path, monkeypatch
    ):
        repo = _repo(tmp_path)
        bed = "make conformance-bed"
        runner = FakeRunner(effects={bed: lambda: _measure(repo, '{"as_of": "2026-10-20"}\n')})
        status, out, _ = _go(monkeypatch, repo, [bed, "make kmodcov"], runner)
        assert status == 1
        assert runner.keys == [bed]
        assert "commit the measured outputs" in out
        assert "gate-tip: 1 stamped," in out

    def test_a_red_measuring_lane_names_its_rewritten_matrix(self, tmp_path, monkeypatch):
        repo = _repo(tmp_path)
        bed = "make conformance-bed"
        runner = FakeRunner(
            effects={bed: lambda: _measure(repo, '{"as_of": "2026-10-20"}\n')}, statuses={bed: 1}
        )
        status, out, _ = _go(monkeypatch, repo, [bed], runner)
        assert status == 1
        assert f"still rewrote {SUPPORT}" in out
        assert f"git checkout -- {SUPPORT}" in out

    def test_a_lane_that_changes_the_tree_is_not_stamped_and_counts_red(
        self, tmp_path, monkeypatch
    ):
        repo = _repo(tmp_path)

        def commit():
            (repo / "tests" / "unit" / "test_a.py").write_text("def test_a():\n    assert True\n")
            _git(repo, "commit", "-qam", "moved under the lane")

        runner = FakeRunner(effects={LANE: commit})
        status, out, _ = _go(monkeypatch, repo, [LANE], runner)
        assert status == 1
        assert "NOT STAMPED" in out
        assert " 1 red\n" in out

    def test_dry_run_lists_runs_nothing_and_writes_nothing(self, tmp_path, monkeypatch):
        repo = _repo(tmp_path)
        root = gt.stamps_root(repo, gt.STORE)
        b1 = _measure(repo, '{"as_of": "2026-10-16"}\n')
        _stamp(repo, LANE)
        _stamp(repo, "make kmodcov", measured={SUPPORT: b1})
        _git(repo, "commit", "-qam", "measured")
        with gt.store_lock(root):  # a store written through the runner always has its lock file
            pass
        before = _snapshot(root)
        runner = FakeRunner()
        lines = [LANE, "make quick-guards"]
        status, out, _ = _go(monkeypatch, repo, lines, runner, dry_run=True)
        assert status == 1
        assert runner.calls == []
        assert f"[1/2] {LANE} … already stamped" in out
        assert "[2/2] make quick-guards … missing" in out
        assert _snapshot(root) == before
        status, _out, _ = _go(monkeypatch, repo, [LANE], FakeRunner(), dry_run=True)
        assert status == 0
        assert _snapshot(root) == before

    def test_an_unknown_goal_refuses_the_whole_run(self, tmp_path, monkeypatch):
        repo, runner = _repo(tmp_path), FakeRunner()
        # the real plan: the oracle emitting an unknown goal refuses as it classifies its lines
        monkeypatch.setattr(gs, "commands", lambda *a, **k: [LANE, "make frobnicate"])
        opts = _options(repo, runner, base="HEAD")
        assert gt.run(opts) == 2
        assert runner.calls == []
        assert (
            "the oracle emitted a lane it does not classify: make frobnicate" in opts.out.getvalue()
        )

    def test_a_stamp_written_in_one_worktree_is_honoured_from_another(self, tmp_path):
        repo = _repo(tmp_path)
        other = tmp_path / "other"
        _git(repo, "worktree", "add", "-q", "--detach", str(other), "HEAD")
        _stamp(repo, LANE)
        root = gt.stamps_root(other, gt.STORE)
        assert root == gt.stamps_root(repo, gt.STORE)
        assert gt.find_stamp(other, root, tree_hash(other), LANE) is not None

    def test_the_cli_runs_main(self, tmp_path):
        repo = _repo(tmp_path)
        completed = subprocess.run(
            [
                sys.executable,
                str(CHECKOUT_ROOT / "scripts/gate_tip.py"),
                "--repo",
                str(repo),
                "--dry-run",
            ],
            cwd=repo,
            env={**git_env(repo), "PATH": os.environ["PATH"]},
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        assert "gate-tip:" in completed.stdout

    def test_gate_tip_uses_the_shared_dirt_refusals(self):
        assert gt.dirt_refusals is gate_stamp_common.dirt_refusals


@functools.cache
def _census_scopes():
    """Return [(name, Scope)] for one synthetic diff per kind of path the oracle classifies."""
    head = _head_paths()

    def real(rev, path):
        file = CHECKOUT_ROOT / path
        return file.read_text(encoding="utf-8") if file.is_file() else None

    def scope(path, *, status="M", texts=None, extra=()):
        texts = texts or {}

        def reader(rev, p):
            return texts[(rev, p)] if (rev, p) in texts else real(rev, p)

        paths = set(head) | {path, *extra}
        return gs.classify([gs.Change(path, status)], reader, "base", "head", paths)

    marked = "import pytest\npytestmark = pytest.mark.{}\ndef test_a(): pass\n"
    leaf = "tests/unit/scripts/test_gate_tip.py"
    src_leaf = next(
        p
        for p in sorted(head)
        if gs.classify_path(p) == gs.SRC_LEAF and gs._src_key(p) in gs.UNIT_TREES
    )
    cases = [
        ("none", scope("AGENTS.md")),
        ("docs", scope("docs/contributing.md")),
        ("web", scope("web/src/main.tsx", extra=["web/src/main.tsx"])),
        ("tests-leaf", scope(leaf)),
        (
            "tests-structural",
            scope(leaf, texts={("base", leaf): "def test_removed():\n    pass\n"}),
        ),
        ("src-leaf", scope(src_leaf)),
        ("scripts", scope("scripts/gate_tip.py")),
        ("trigger", scope(min(gs.TRIGGER_FILES))),
        ("unknown", scope("mystery/thing.bin")),
    ]
    cases += [
        (f"special {glob}", scope(next(p for p in sorted(head) if fnmatch.fnmatch(p, glob))))
        for glob, _c, _t in gs.SPECIAL_TREES
    ]
    for mark in gs.LANE_MARKERS:
        path = f"tests/unit/test_lane_{mark}.py"
        text = marked.format(mark)
        cases.append((f"mark {mark}", scope(path, status="A", texts={("head", path): text})))
    cases += [(f"stability {p}", scope(p)) for p in gs.STABILITY_FILES]
    cases += [
        (f"bed {pkg}", scope(f"src/otto/{pkg}/x.py", extra=[f"src/otto/{pkg}/x.py"]))
        for pkg in gs.BED_LANES
    ]
    cases += [(f"build {p}", scope(p)) for p in sorted(gs.BUILD_FILES)]
    cases += [(f"measured {p}", scope(p)) for p in gs.MEASURED_ROWS]
    return cases


def _census():
    """Classify every line the oracle can emit at a tip; raise UnknownGoalError on a stray goal."""
    seen = set()
    for name, scope in _census_scopes():
        for base in ("origin/main", "0123456789abcdef0123456789abcdef01234567"):
            for line in gs.commands(scope, tip=True, base=base):
                for goal in gs.goals(line):
                    assert (goal in gs.LIVE_GOALS) != (goal in gs.STAMPED_GOALS), (name, line)
                    seen.add(goal)
                gs.is_live(line)
    return seen


class TestCensus:
    def test_every_tip_line_the_oracle_can_emit_has_exactly_one_kind(self):
        seen = _census()
        assert seen == set(gs.STATIC_GOALS + gs.BED_GOALS + gs.LIVE_GOALS)
        assert gt.STAMPED_GOALS == gs.STATIC_GOALS + gs.BED_GOALS, "the runner reads the oracle's"
        assert not any("hostless" in g for g in gs.STAMPED_GOALS), "the PR's CI owns them (#795)"

    @pytest.mark.parametrize(
        "goal",
        ["check-python", "conformance-bed", "kmodcov", "docs-captures-check", "coverage-bed"],
    )
    def test_a_stamped_goal_dropped_from_the_set_fails_the_census(self, monkeypatch, goal):
        monkeypatch.setattr(gs, "STAMPED_GOALS", [g for g in gs.STAMPED_GOALS if g != goal])
        with pytest.raises(gs.UnknownGoalError):
            _census()

    def test_a_live_goal_dropped_from_the_set_fails_the_census(self, monkeypatch):
        monkeypatch.setattr(gs, "LIVE_GOALS", [g for g in gs.LIVE_GOALS if g != "check-breaking"])
        with pytest.raises(gs.UnknownGoalError):
            _census()


class TestPushCheck:
    def test_the_hook_reads_each_exact_file_once_and_the_history_at_most_once(
        self, tmp_path, monkeypatch
    ):
        repo, _bare_repo, remote_sha, tip = _published(tmp_path)
        _stamp_tip(repo, remote_sha, tip)
        lanes = [
            gt.lane_key(line)
            for line in gs.tip_commands(repo, remote_sha, tip, git_env(repo))
            if not gt.is_live(line)
        ]
        root = gt.stamps_root(repo, gt.STORE)
        tree = tree_hash(repo, tip)
        reads: "list[Path]" = []
        real = gt._read_record

        def counting(path):
            reads.append(path)
            return real(path)

        monkeypatch.setattr(gt, "_read_record", counting)
        assert gt.missing_stamps(repo, gs.plan(repo, remote_sha, tip)) == []
        assert sorted(reads) == sorted(gt.stamp_path(root, tree, key) for key in lanes)

        reads.clear()
        for key in (lanes[0], lanes[-1]):
            gt.stamp_path(root, tree, key).unlink()
        assert gt.missing_stamps(repo, gs.plan(repo, remote_sha, tip))
        in_store = len(list(root.glob("*/*.json")))
        assert len(reads) == len(lanes) + in_store
        assert max(reads.count(path) for path in set(reads)) <= 2

    def test_a_refused_push_says_the_lane_went_red(self, tmp_path):
        repo, _bare_repo, remote_sha, tip = _published(tmp_path)
        _stamp_tip(repo, remote_sha, tip)
        key = next(
            gt.lane_key(line)
            for line in gs.tip_commands(repo, remote_sha, tip, git_env(repo))
            if not gt.is_live(line)
        )
        root = gt.stamps_root(repo, gt.STORE)
        gt.write_red(root, tree=tree_hash(repo, tip), key=key, checkout="/x")
        missing = gt.missing_stamps(repo, gs.plan(repo, remote_sha, tip))
        assert [m.lane for m in missing] == [key]
        assert "went red on this tree" in missing[0].reason
        assert "/x" in missing[0].reason

    def test_a_red_marker_only_the_scan_holds_still_refuses(self, tmp_path, monkeypatch):
        """lookup heeds a red in the scanned store even when its exact read missed it.

        Inside the check the shared lock keeps peers out (see the next test), so the files are
        written here between the exact read and the scan directly, as a caller without the lock,
        or one holding a cached store, could see them.
        """
        repo, _bare_repo, remote_sha, _tip = _published(tmp_path)
        tip = _commit_file(repo, SUPPORT, '{"as_of": "2026-10-21"}\n', "matrix")
        u_tree = tree_hash(repo, tip)
        u_blob = _git(repo, "rev-parse", f"{tip}:{SUPPORT}").strip()
        (repo / SUPPORT).write_text('{"as_of": "2026-10-20"}\n')
        _git(repo, "add", SUPPORT)
        s_tree = _git(repo, "write-tree").strip()  # U but for the measured matrix
        _git(repo, "reset", "-q", "--hard")
        _stamp_tip(repo, remote_sha, tip)
        root = gt.stamps_root(repo, gt.STORE)
        lanes = [
            gt.lane_key(line)
            for line in gs.tip_commands(repo, remote_sha, tip, git_env(repo))
            if not gt.is_live(line)
        ]
        key = lanes[0]
        gt.stamp_path(root, u_tree, key).unlink()
        real_load = gt.load_store

        def peers_then_scan(where):
            gt.write_red(root, tree=u_tree, key=key, checkout="/peer")
            gt.write_stamp(
                root,
                tree=s_tree,
                key=key,
                base=remote_sha,
                measured={SUPPORT: u_blob},
                checkout="/p",
            )
            return real_load(where)

        reads: "list[Path]" = []
        real_read = gt._read_record

        def counting(path):
            reads.append(path)
            return real_read(path)

        monkeypatch.setattr(gt, "load_store", peers_then_scan)
        monkeypatch.setattr(gt, "_read_record", counting)
        found = gt.lookup(repo, root, u_tree, key)
        monkeypatch.undo()
        assert found.stamp is None
        assert found.reason.startswith("the lane went red")
        in_store = len(list(root.glob("*/*.json")))
        assert len(reads) == 1 + in_store, "the exact probe, then one scan: no new read"
        assert len(lanes) > 1, "the tip's other lanes are stamped too"
        gt.stamp_path(root, u_tree, key).unlink()
        assert gt.find_stamp(repo, root, u_tree, key) is not None, "without the red, S promotes"

    def test_a_peers_red_during_the_checks_git_spawns_waits_for_the_check(
        self, tmp_path, monkeypatch
    ):
        """The check holds the shared lock from its first read to its result: a peer's red that
        would land during ``_pruned``/``satisfies`` waits, so the verdict is of one store state."""
        repo, _bare_repo, remote_sha, _tip = _published(tmp_path)
        tip = _commit_file(repo, SUPPORT, '{"as_of": "2026-10-21"}\n', "matrix")
        u_tree = tree_hash(repo, tip)
        u_blob = _git(repo, "rev-parse", f"{tip}:{SUPPORT}").strip()
        (repo / SUPPORT).write_text('{"as_of": "2026-10-20"}\n')
        _git(repo, "add", SUPPORT)
        s_tree = _git(repo, "write-tree").strip()
        _git(repo, "reset", "-q", "--hard")
        _stamp_tip(repo, remote_sha, tip)
        root = gt.stamps_root(repo, gt.STORE)
        key = next(
            gt.lane_key(line)
            for line in gs.tip_commands(repo, remote_sha, tip, git_env(repo))
            if not gt.is_live(line)
        )
        gt.stamp_path(root, u_tree, key).unlink()
        gt.write_stamp(
            root, tree=s_tree, key=key, base=remote_sha, measured={SUPPORT: u_blob}, checkout="/p"
        )
        deferred: list = []
        real = gt._pruned

        def spawn_then_peer(*args):
            _peer_write(
                root, lambda: gt.write_red(root, tree=u_tree, key=key, checkout="/peer"), deferred
            )
            return real(*args)

        monkeypatch.setattr(gt, "_pruned", spawn_then_peer)
        plan = gs.plan(repo, remote_sha, tip)
        refusal = gt.stamp_refusal(repo, plan, ref="refs/heads/x", who="gate-tip")
        monkeypatch.undo()
        assert len(deferred) == 1, "the peer's write waited for the check"
        assert refusal == [], "the check saw one state: S promotes, no red yet"
        for write in deferred:
            write()
        after = gt.stamp_refusal(repo, plan, ref="refs/heads/x", who="gate-tip")
        assert [line for line in after if "the lane went red" in line] == after[1:2]

    def test_the_check_creates_nothing_without_a_store(self, tmp_path):
        repo, _bare_repo, remote_sha, tip = _published(tmp_path)
        root = gt.stamps_root(repo, gt.STORE)
        assert gt.missing_stamps(repo, gs.plan(repo, remote_sha, tip))
        assert not root.exists()


def _stamped_lanes(repo: Path, remote_sha: str, tip_sha: str) -> "list[str]":
    return gs.plan(repo, remote_sha, tip_sha).stamped


class TestTheOraclesLanes:
    """What the store is asked for: the plan's stamped lanes, never a live one."""

    def test_live_goals_are_never_demanded(self, tmp_path):
        repo, _bare_repo, remote_sha, tip = _published(tmp_path)
        _stamp_tip(repo, remote_sha, tip)
        plan = gs.plan(repo, remote_sha, tip)
        assert plan.live, "the range owes live lines too"
        assert gt.missing_stamps(repo, plan) == []

    def test_a_lane_the_oracle_started_requiring_is_no_stamp(self, tmp_path, monkeypatch):
        repo, _bare_repo, remote_sha, base_tip = _published(tmp_path)
        tip = _commit_file(repo, "schemas/unrowed.json", "{}\n", "unrowed")
        assert "make coverage-bed" in gs.plan(repo, remote_sha, tip).stamped
        _stamp_tip(repo, remote_sha, tip)
        assert base_tip != tip
        monkeypatch.setattr(gs, "TEST_SUITE_LANES", [*gs.TEST_SUITE_LANES, "make chaos"])
        missing = gt.missing_stamps(repo, gs.plan(repo, remote_sha, tip))
        assert missing == [gt.Missing("make chaos", "no stamp")]


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
        assert "make coverage-bed" in gs.tip_commands(repo, remote_sha, s_sha, git_env(repo))
        key = gs.plan(repo, remote_sha, s_sha).stamped[0]
        gt.write_stamp(
            gt.stamps_root(repo, gt.STORE),
            tree=gt.tree_hash(repo, s_sha),
            key=key,
            base=remote_sha,
            measured={self.MATRIX: blob},
            checkout=str(repo),
        )
        return repo, remote_sha, s_sha

    def test_a_tip_that_differs_only_by_a_recorded_measured_blob_is_honoured(self, tmp_path):
        text = '{"as_of": "2026-10-16"}\n'
        repo, remote_sha, s_sha = self._stamped_s(tmp_path, text)
        t_sha = _commit_file(repo, self.MATRIX, text, "measured")
        # the same stamped lanes; only the live line's range names the other tip
        assert _stamped_lanes(repo, remote_sha, s_sha) == _stamped_lanes(repo, remote_sha, t_sha)
        assert gt.missing_stamps(repo, gs.plan(repo, remote_sha, t_sha)) == []

    def test_a_tip_whose_matrix_bytes_differ_from_the_recorded_blob_is_refused(self, tmp_path):
        repo, remote_sha, s_sha = self._stamped_s(tmp_path, '{"as_of": "2026-10-16"}\n')
        t_sha = _commit_file(repo, self.MATRIX, '{"as_of": "hand edited"}\n', "edited")
        missing = gt.missing_stamps(repo, gs.plan(repo, remote_sha, t_sha))
        assert [m.lane for m in missing] == _stamped_lanes(repo, remote_sha, t_sha)
        reason = (
            f"stamped for tree {gt.tree_hash(repo, s_sha)[:12]}, which differs from the tip's "
            f"beyond the measured outputs (stamped in {repo}, base {remote_sha[:12]})"
        )
        assert {m.reason for m in missing} == {reason}

    def test_one_that_differs_elsewhere_names_the_stamp_checkout_and_base(self, tmp_path):
        repo, _bare_repo, remote_sha, s_sha = _published(tmp_path)
        _stamp_tip(repo, remote_sha, s_sha)
        t_sha = _commit_file(repo, "tests/unit/cli/test_b.py", "def test_b(): assert 1\n", "edit")
        missing = gt.missing_stamps(repo, gs.plan(repo, remote_sha, t_sha))
        assert [m.lane for m in missing] == _stamped_lanes(repo, remote_sha, t_sha)
        reason = (
            f"stamped for tree {gt.tree_hash(repo, s_sha)[:12]}, which differs from the tip's "
            f"beyond the measured outputs (stamped in {repo}, base {remote_sha[:12]})"
        )
        assert {m.reason for m in missing} == {reason}


@dataclasses.dataclass(frozen=True)
class _Released:
    repo: Path
    remote_sha: str
    tip: str


class TestReleaseCommits:
    """#660: a green `make release`'s own commits pass the push check with no stamp at all."""

    _PYPROJECT = (
        '[project]\nname = "otto-sh"\nversion = "1.0.0"\n\n'
        '[tool.bumpversion]\ncurrent_version = "1.0.0"\n'
    )
    _LOCK = 'version = 1\n\n[[package]]\nname = "otto-sh"\nversion = "1.0.0"\n'

    def _released(self, tmp_path, *, pyproject_extra: str = "") -> _Released:
        """Publish a base, then add the release's three commits on top of it."""
        repo = _fresh_repo(tmp_path)
        files = {
            "pyproject.toml": self._PYPROJECT,
            "uv.lock": self._LOCK,
            "CHANGELOG.md": "# Changelog\n",
            "schemas/support_matrix.json": '{"cells": {"a": {"as_of": "2026-10-18"}}}\n',
            "schemas/kmodcov_matrix.json": '{"cells": {"b": {"as_of": "2026-10-18"}}}\n',
        }
        for path, text in files.items():
            (repo / path).parent.mkdir(parents=True, exist_ok=True)
            (repo / path).write_text(text)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "base")
        remote_sha = _git(repo, "rev-parse", "HEAD").strip()
        bare = _bare(tmp_path, "origin")
        _git(repo, "remote", "add", "origin", str(bare))
        _git(repo, "push", "-q", "origin", "HEAD:refs/heads/main")
        for matrix in ("schemas/support_matrix.json", "schemas/kmodcov_matrix.json"):
            _commit_file(
                repo, matrix, files[matrix].replace("18", "19"), "chore(matrix): re-measure"
            )
        (repo / "pyproject.toml").write_text(
            self._PYPROJECT.replace('version = "1.0.0"', 'version = "1.1.0"') + pyproject_extra
        )
        (repo / "uv.lock").write_text(self._LOCK.replace("1.0.0", "1.1.0"))
        (repo / "CHANGELOG.md").write_text("# Changelog\n\n## [1.1.0]\n")
        _git(repo, "commit", "-qam", "chore(release): bump version 1.0.0 -> 1.1.0")
        return _Released(repo, remote_sha, _git(repo, "rev-parse", "HEAD").strip())

    def test_the_release_s_own_commits_need_no_stamp(self, tmp_path):
        r = self._released(tmp_path)
        root = gt.stamps_root(r.repo, gt.STORE)
        assert not root.exists() or not any(root.iterdir()), "the store must start empty"
        assert gt.missing_stamps(r.repo, gs.plan(r.repo, r.remote_sha, r.tip)) == []

    def test_a_bump_that_also_edits_another_pyproject_key_still_needs_the_full_gate(self, tmp_path):
        r = self._released(tmp_path, pyproject_extra="allow_dirty = true\n")
        plan = gs.plan(r.repo, r.remote_sha, r.tip)
        lanes = [m.lane for m in gt.missing_stamps(r.repo, plan)]
        assert "make check-python" in lanes
        assert "make coverage-bed" in lanes


# ── Lane timings: what a gate costs, recorded in the stamp and printed as a table ───────


def _junit_effect(repo: Path, group: str):
    """Return an effect that writes the JUnit report a lane of *group* would produce."""

    def write():
        path = repo / "reports" / "junit" / group / f"{group}.xml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('<testsuites><testsuite name="pytest" time="1.0"/></testsuites>\n')

    return write


class TestTimings:
    def test_a_run_lane_records_its_timing_and_the_reports_it_wrote(self, tmp_path, monkeypatch):
        repo = _repo(tmp_path)
        runner = FakeRunner(effects={LANE: _junit_effect(repo, "stability-unix")})
        status, _out, _ = _go(monkeypatch, repo, [LANE], runner)
        assert status == 0
        stamp = gt.exact_stamp(gt.stamps_root(repo, gt.STORE), tree_hash(repo), LANE)
        assert stamp is not None
        timing = stamp.timing
        assert timing is not None
        assert set(timing) == {
            "started",
            "finished",
            "elapsed_s",
            "interpreter",
            "nproc",
            "junit",
        }
        assert timing["started"] <= timing["finished"]
        assert isinstance(timing["elapsed_s"], int)
        assert timing["elapsed_s"] >= 0
        assert timing["interpreter"] == sys.version.split()[0]
        assert timing["nproc"] == gate_stamp_common.nproc()
        assert timing["junit"] == ["reports/junit/stability-unix/stability-unix.xml"]
        record = json.loads(stamp.path.read_text())
        assert record["timing"] == timing
        assert record["measured"] == {}, "the measured outputs are a different field"

    def test_a_report_older_than_the_lane_is_not_the_lanes(self, tmp_path, monkeypatch):
        repo = _repo(tmp_path)
        _junit_effect(repo, "quick")()
        old = repo / "reports" / "junit" / "quick" / "quick.xml"
        os.utime(old, (1, 1))
        runner = FakeRunner(effects={LANE: _junit_effect(repo, "stability-unix")})
        _go(monkeypatch, repo, [LANE], runner)
        stamp = gt.exact_stamp(gt.stamps_root(repo, gt.STORE), tree_hash(repo), LANE)
        assert stamp is not None
        assert stamp.timing is not None
        assert stamp.timing["junit"] == ["reports/junit/stability-unix/stability-unix.xml"]

    def test_a_stamp_without_timing_is_still_evidence(self, tmp_path):
        repo = _repo(tmp_path)
        _stamp(repo, LANE)
        stamp = gt.exact_stamp(gt.stamps_root(repo, gt.STORE), tree_hash(repo), LANE)
        assert stamp is not None
        assert stamp.timing is None
        record = json.loads(stamp.path.read_text())
        assert "timing" not in record

    def test_a_timing_that_is_not_an_object_is_dropped_not_evidence_lost(self, tmp_path):
        repo = _repo(tmp_path)
        _stamp(repo, LANE)
        path = gt.stamp_path(gt.stamps_root(repo, gt.STORE), tree_hash(repo), LANE)
        record = json.loads(path.read_text())
        record["timing"] = "fast"
        path.write_text(json.dumps(record))
        stamp = gt.read_stamp(path)
        assert stamp is not None
        assert stamp.timing is None

    def test_a_promoted_stamp_carries_no_timing(self, tmp_path, monkeypatch):
        repo = _repo(tmp_path)
        other = _live_tree(repo, "other")
        _stamp(repo, LANE, tree=other, measured={SUPPORT: "x" * 40})
        root = gt.stamps_root(repo, gt.STORE)
        # treat the tip as differing from `other` by a witnessed measured blob only
        monkeypatch.setattr(gt, "satisfies", lambda *_a, **_k: True)
        monkeypatch.setattr(gt, "_pruned", lambda *_a: False)
        status, out, _ = _go(monkeypatch, repo, [LANE], FakeRunner())
        assert status == 0
        assert "already stamped" in out
        promoted = gt.exact_stamp(root, tree_hash(repo), LANE)
        assert promoted is not None
        assert promoted.promoted_from == other
        assert promoted.timing is None

    def test_a_red_lane_records_its_timing_on_the_marker(self, tmp_path, monkeypatch):
        repo = _repo(tmp_path)
        runner = FakeRunner(statuses={LANE: 1})
        status, _out, _ = _go(monkeypatch, repo, [LANE], runner)
        assert status == 1
        path = gt.stamp_path(gt.stamps_root(repo, gt.STORE), tree_hash(repo), LANE)
        red = gt.read_entry(path)
        assert isinstance(red, gt.Red)
        assert red.timing is not None
        assert red.timing["elapsed_s"] >= 0

    def test_the_run_ends_with_a_timing_table(self, tmp_path, monkeypatch):
        repo = _repo(tmp_path)
        _stamp(repo, "make quick-guards")
        lines = ["make check-breaking RANGE=x..HEAD", LANE, "make quick-guards", "make quick"]
        runner = FakeRunner(statuses={"make quick": 1})
        status, out, _ = _go(monkeypatch, repo, lines, runner)
        assert status == 1
        summary = "gate-tip: 1 stamped, 1 already stamped, 1 live at push, 1 red"
        table = out[out.index(summary) + len(summary) :]
        rows = [line for line in table.splitlines() if line.strip()]
        assert rows[0] == "gate-tip: timings (elapsed, outcome, lane)"
        assert re.fullmatch(r"\s+live at push\s+make check-breaking", rows[1])
        assert re.fullmatch(rf"\s+\d+ s\s+stamped\s+{re.escape(LANE)}", rows[2])
        assert re.fullmatch(r"\s+already stamped\s+make quick-guards", rows[3])
        assert re.fullmatch(r"\s+\d+ s\s+red\s+make quick", rows[4])
        assert len(rows) == 5

    def test_a_dry_run_lists_outcomes_without_elapsed(self, tmp_path, monkeypatch):
        repo = _repo(tmp_path)
        _stamp(repo, LANE)
        status, out, _ = _go(monkeypatch, repo, [LANE, "make quick"], FakeRunner(), dry_run=True)
        assert status == 1
        assert re.search(r"\n\s+already stamped\s+" + re.escape(LANE) + r"\n", out)
        assert re.search(r"\n\s+missing\s+make quick\n", out)
        assert " s " not in out.split("gate-tip: timings")[1]

    def test_timings_prints_the_stored_table_for_a_tree(self, tmp_path, monkeypatch):
        repo = _repo(tmp_path)
        runner = FakeRunner(statuses={"make quick": 1})
        _go(monkeypatch, repo, [LANE, "make quick"], runner)
        _stamp(repo, "make quick-guards")  # a stamp with no timing (an older record's)
        lines = gt.timings(repo, gt.stamps_root(repo, gt.STORE), tree_hash(repo))
        assert lines[0] == (
            f"gate-tip: timings for tree {tree_hash(repo)[:12]} "
            "(3 lane(s); elapsed, outcome, written at, lane)"
        )
        body = "\n".join(lines[1:])
        assert re.search(rf"^\s+\d+ s\s+stamped\s+\S+\s+{re.escape(LANE)}$", body, re.MULTILINE)
        assert re.search(r"^\s+\d+ s\s+red\s+\S+\s+make quick$", body, re.MULTILINE)
        assert re.search(r"^\s+stamped\s+\S+\s+make quick-guards$", body, re.MULTILINE)
        monkeypatch.chdir(repo)
        assert gt.main(["--timings"]) == 0
        assert gt.main(["--timings", "HEAD"]) == 0
        assert gt.main(["--timings", tree_hash(repo)]) == 0

    def test_timings_for_a_tree_without_stamps_says_so(self, tmp_path, capsys):
        repo = _repo(tmp_path)
        assert gt.main(["--timings", "--repo", str(repo)]) == 0
        assert "no stamps" in capsys.readouterr().out
        with pytest.raises(gate_stamp_common.GateError):
            gt.timings(repo, gt.stamps_root(repo, gt.STORE), "nonsense")

    def test_the_gate_tip_help_names_the_timings_switch(self):
        assert "--timings" in MAKEFILE

    def test_each_lane_lists_only_the_reports_it_wrote(self, tmp_path, monkeypatch):
        """Lane 1's report is on disk when lane 2 starts; lane 2's start is the cut."""
        repo = _repo(tmp_path)
        quick = "make quick"
        runner = FakeRunner(
            effects={
                LANE: _junit_effect(repo, "stability-unix"),
                quick: _junit_effect(repo, "quick"),
            }
        )
        status, _out, _ = _go(monkeypatch, repo, [LANE, quick], runner)
        assert status == 0
        root, tree = gt.stamps_root(repo, gt.STORE), tree_hash(repo)
        first, second = gt.exact_stamp(root, tree, LANE), gt.exact_stamp(root, tree, quick)
        assert first is not None
        assert first.timing is not None
        assert second is not None
        assert second.timing is not None
        assert first.timing["junit"] == ["reports/junit/stability-unix/stability-unix.xml"]
        assert second.timing["junit"] == ["reports/junit/quick/quick.xml"]

    def test_an_untouched_report_is_never_the_lanes_whatever_its_mtime(self, tmp_path, monkeypatch):
        """No clock reference: a report present and unchanged before the lane is excluded."""
        repo = _repo(tmp_path)
        _junit_effect(repo, "quick")()
        stale = repo / "reports" / "junit" / "quick" / "quick.xml"
        future = time.time() + 3600
        os.utime(stale, (future, future))
        runner = FakeRunner(effects={LANE: _junit_effect(repo, "stability-unix")})
        _go(monkeypatch, repo, [LANE], runner)
        stamp = gt.exact_stamp(gt.stamps_root(repo, gt.STORE), tree_hash(repo), LANE)
        assert stamp is not None
        assert stamp.timing is not None
        assert stamp.timing["junit"] == ["reports/junit/stability-unix/stability-unix.xml"]

    def test_a_report_the_lane_rewrites_in_place_is_the_lanes(self, tmp_path, monkeypatch):
        repo = _repo(tmp_path)
        _junit_effect(repo, "stability-unix")()
        report = repo / "reports" / "junit" / "stability-unix" / "stability-unix.xml"
        os.utime(report, (1, 1))

        def rewrite():
            report.write_text(report.read_text() + "<!-- rerun -->\n")

        _go(monkeypatch, repo, [LANE], FakeRunner(effects={LANE: rewrite}))
        stamp = gt.exact_stamp(gt.stamps_root(repo, gt.STORE), tree_hash(repo), LANE)
        assert stamp is not None
        assert stamp.timing is not None
        assert stamp.timing["junit"] == ["reports/junit/stability-unix/stability-unix.xml"]

    def test_a_failure_while_measuring_never_touches_the_verdict(self, tmp_path, monkeypatch):
        """A red lane under --force must leave its marker even when the timing raises."""
        repo = _repo(tmp_path)
        _stamp(repo, LANE)  # the earlier green stamp a forced re-run keeps until the lane ends
        calls = {"n": 0}

        def failing(_repo):
            calls["n"] += 1
            if calls["n"] > 1:  # not an OSError: those are absorbed by design, see below
                raise RuntimeError("a bug in the measuring code")
            return {}

        monkeypatch.setattr(gate_stamp_common, "junit_snapshot", failing)
        status, out, _ = _go(monkeypatch, repo, [LANE], FakeRunner(statuses={LANE: 1}), force=True)
        assert status == 1
        assert "timing not recorded" in out
        entry = gt.read_entry(gt.stamp_path(gt.stamps_root(repo, gt.STORE), tree_hash(repo), LANE))
        assert isinstance(entry, gt.Red)
        assert entry.timing is None
        calls["n"] = 0
        status, out, _ = _go(monkeypatch, repo, [LANE], FakeRunner(), force=True)
        assert status == 0
        stamp = gt.exact_stamp(gt.stamps_root(repo, gt.STORE), tree_hash(repo), LANE)
        assert stamp is not None
        assert stamp.timing is None, "the green lane is stamped, without a timing"

    def test_a_snapshot_that_cannot_be_taken_at_the_start_leaves_junit_empty(
        self, tmp_path, monkeypatch
    ):
        repo = _repo(tmp_path)
        monkeypatch.setattr(
            gate_stamp_common, "junit_snapshot", lambda _repo: (_ for _ in ()).throw(OSError())
        )
        status, _out, _ = _go(monkeypatch, repo, [LANE], FakeRunner())
        assert status == 0
        stamp = gt.exact_stamp(gt.stamps_root(repo, gt.STORE), tree_hash(repo), LANE)
        assert stamp is not None
        assert stamp.timing is not None
        assert stamp.timing["junit"] == []

    def test_nproc_is_the_affinity_mask_with_the_machine_count_as_the_fallback(self, monkeypatch):
        monkeypatch.setattr(os, "cpu_count", lambda: 8)
        monkeypatch.setattr(os, "sched_getaffinity", lambda _pid: {0, 3}, raising=False)
        assert gate_stamp_common.nproc() == 2
        monkeypatch.delattr(os, "sched_getaffinity")
        assert gate_stamp_common.nproc() == 8

    def test_a_lane_that_changes_the_tree_gets_a_not_stamped_row_with_its_elapsed(
        self, tmp_path, monkeypatch
    ):
        repo = _repo(tmp_path)
        runner = FakeRunner(
            effects={
                LANE: lambda: (repo / "tests" / "unit" / "test_a.py").write_text("# changed\n")
            }
        )
        status, out, _ = _go(monkeypatch, repo, [LANE], runner)
        assert status == 1
        assert re.search(rf"\n\s+\d+ s\s+not stamped\s+{re.escape(LANE)}\n", out)

    def test_a_dirty_tree_before_the_lane_gets_a_not_stamped_row_without_elapsed(
        self, tmp_path, monkeypatch
    ):
        """A peer dirties the tree between lane 1's stamp and lane 2's start."""
        repo = _repo(tmp_path)
        real = gt._already_stamped

        def peer_then_lookup(ctx, label, key):
            if key == "make quick":
                (repo / "tests" / "unit" / "test_b.py").write_text("x = 1\n")
            return real(ctx, label, key)

        monkeypatch.setattr(gt, "_already_stamped", peer_then_lookup)
        status, out, _ = _go(monkeypatch, repo, [LANE, "make quick"], FakeRunner())
        assert status == 1
        assert "dirty before the lane" in out
        assert re.search(r"\n\s+not stamped\s+make quick\n", out)
        assert not re.search(r"\d+ s\s+not stamped\s+make quick", out)

    def test_timings_orders_the_rows_by_when_they_were_written(self, tmp_path, monkeypatch):
        repo = _repo(tmp_path)
        monkeypatch.setattr(gt, "utc_now", lambda: "2026-01-01T00:00:09+00:00")
        _stamp(repo, "make quick")
        monkeypatch.setattr(gt, "utc_now", lambda: "2026-01-01T00:00:01+00:00")
        _stamp(repo, LANE)
        lines = gt.timings(repo, gt.stamps_root(repo, gt.STORE), tree_hash(repo))
        assert lines[1].endswith(LANE)
        assert lines[2].endswith("make quick")
        assert "2026-01-01T00:00:01+00:00" in lines[1]

    def test_timings_takes_a_commit_id_as_a_rev(self, tmp_path, capsys):
        repo = _repo(tmp_path)
        _stamp(repo, LANE)
        commit = _git(repo, "rev-parse", "HEAD").strip()
        assert gt.main(["--timings", commit, "--repo", str(repo)]) == 0
        out = capsys.readouterr().out
        assert f"timings for tree {tree_hash(repo)[:12]} (1 lane(s);" in out
        assert out.rstrip().endswith(LANE)

    def test_a_malformed_timing_is_dropped_and_the_table_still_prints(self, tmp_path):
        repo = _repo(tmp_path)
        _stamp(repo, LANE)
        path = gt.stamp_path(gt.stamps_root(repo, gt.STORE), tree_hash(repo), LANE)
        record = json.loads(path.read_text())
        for bad in ({}, {"elapsed_s": "x"}, {"elapsed_s": 1.5}, ["fast"]):
            path.write_text(json.dumps({**record, "timing": bad}))
            stamp = gt.read_stamp(path)
            assert stamp is not None, bad
            assert stamp.timing is None, bad
            lines = gt.timings(repo, gt.stamps_root(repo, gt.STORE), tree_hash(repo))
            assert re.fullmatch(r"\s+stamped\s+\S+\s+" + re.escape(LANE), lines[1]), lines

    def test_the_timings_switch_excludes_the_other_modes(self, tmp_path, capsys):
        repo = _repo(tmp_path)
        for argv in (["--timings", "--dry-run"], ["--timings", "--pre-push"]):
            with pytest.raises(SystemExit) as exc:
                gt.main([*argv, "--repo", str(repo)])
            assert exc.value.code == 2
            assert "not allowed with" in capsys.readouterr().err

    def test_an_empty_rev_means_head_and_never_starts_the_runner(
        self, tmp_path, monkeypatch, capsys
    ):
        repo = _repo(tmp_path)
        monkeypatch.setattr(gt, "run", lambda _opts: pytest.fail("the runner started"))
        assert gt.main(["--timings", "", "--repo", str(repo)]) == 0
        assert "no stamps for tree" in capsys.readouterr().out

    def test_the_junit_directory_is_one_decision(self):
        from noxfile import JUNIT_DIR as NOX_JUNIT_DIR

        assert re.search(r"^JUNIT_DIR := reports/junit\s*$", MAKEFILE, re.MULTILINE)
        assert NOX_JUNIT_DIR == gate_stamp_common.JUNIT_DIR == Path("reports/junit")
        watcher = (CHECKOUT_ROOT / "scripts" / "watch_release.sh").read_text()
        assert 'JUNIT_DIR="$REPO/reports/junit"' in watcher


BED = ["make check-breaking", "make check-python", "make coverage-bed", gs.STABILITY_UNIX]
STATIC = ["make check-breaking", "make check-python", "make collect-check"]


def _spied(monkeypatch, tmp_path, runner=None, replies=None):
    repo = _repo(tmp_path)
    gh = fg.scripted_gh([fg.GhReply([], stdout="{}")] if replies is None else replies)
    return repo, _lease_spy(monkeypatch, runner or FakeRunner(), gh)


def _raise(exc):
    def effect():
        raise exc

    return effect


class TestLease:
    """The runner takes the bed lease itself, only before the first bed lane it runs.

    One owner for the lease's cleanup, ``run()``: a walk that returns releases it before
    any publication; an interrupt whose lanes are gone reaps, then releases; an undrained
    one keeps it (the bed is not idle).
    """

    def test_a_static_tip_takes_no_lease(self, monkeypatch, tmp_path):
        repo, spy = _spied(monkeypatch, tmp_path)
        status, _out, _ = _go(monkeypatch, repo, STATIC, spy.runner, gh=spy.gh)
        assert status == 0
        assert spy.events == ["run make check-python", "run make collect-check", "gh api"]

    def test_the_lease_is_taken_before_the_first_missing_bed_lane_and_released_before_the_post(
        self, monkeypatch, tmp_path
    ):
        repo, spy = _spied(monkeypatch, tmp_path)
        status, _out, _ = _go(monkeypatch, repo, BED, spy.runner, gh=spy.gh)
        assert status == 0
        assert spy.events == [
            "run make check-python",
            "acquire",
            "run make coverage-bed",
            f"run {gs.STABILITY_UNIX}",
            "release",
            "gh api",
        ]

    def test_a_bed_tip_whose_bed_lanes_are_stamped_never_calls_acquire(self, monkeypatch, tmp_path):
        repo, spy = _spied(monkeypatch, tmp_path)
        for key in ("make coverage-bed", gs.STABILITY_UNIX):
            _stamp(repo, key)
        status, _out, _ = _go(monkeypatch, repo, BED, spy.runner, gh=spy.gh)
        assert status == 0
        assert "acquire" not in spy.events

    def test_a_dry_run_takes_no_lease_and_calls_gh_never(self, monkeypatch, tmp_path):
        repo, spy = _spied(monkeypatch, tmp_path, replies=[])
        status, _out, _ = _go(monkeypatch, repo, BED, spy.runner, dry_run=True, gh=spy.gh)
        assert status == 1
        assert spy.events == []
        assert spy.gh.calls == []

    def test_a_red_bed_lane_releases_the_lease_once_before_the_head_status_is_read(
        self, monkeypatch, tmp_path
    ):
        runner = FakeRunner(statuses={gs.STABILITY_UNIX: 1})
        repo, spy = _spied(monkeypatch, tmp_path, runner=runner)
        status, _out, _ = _go(monkeypatch, repo, BED, spy.runner, gh=spy.gh)
        assert status == 1
        assert spy.events[-2:] == ["release", "gh api"]
        assert spy.events.count("release") == 1
        assert "reap" not in spy.events

    def test_an_interrupt_inside_a_bed_lane_reaps_then_releases_and_posts_nothing(
        self, monkeypatch, tmp_path
    ):
        runner = FakeRunner(effects={gs.STABILITY_UNIX: _raise(KeyboardInterrupt())})
        repo, spy = _spied(monkeypatch, tmp_path, runner=runner)
        status, out, _ = _go(monkeypatch, repo, BED, spy.runner, gh=spy.gh)
        assert status == gt.EXIT_INTERRUPTED
        assert spy.events[-3:] == [f"run {gs.STABILITY_UNIX}", "reap", "release"]
        assert "gh api" not in spy.events
        assert "reaping the bed and releasing the lease" in out

    def test_an_interrupt_before_any_bed_lane_reaps_nothing(self, monkeypatch, tmp_path):
        runner = FakeRunner(effects={"make check-python": _raise(KeyboardInterrupt())})
        repo, spy = _spied(monkeypatch, tmp_path, runner=runner)
        status, out, _ = _go(monkeypatch, repo, BED, spy.runner, gh=spy.gh)
        assert status == gt.EXIT_INTERRUPTED
        assert spy.events == ["run make check-python"]
        assert "no lease was taken, nothing to reap" in out

    def test_an_undrained_interrupt_keeps_the_lease(self, monkeypatch, tmp_path):
        # (pgid, pids), as the runner's own UndrainedError carries them
        runner = FakeRunner(effects={gs.STABILITY_UNIX: _raise(UndrainedError(4242, [4243]))})
        repo, spy = _spied(monkeypatch, tmp_path, runner=runner)
        with pytest.raises(UndrainedError):
            _go(monkeypatch, repo, BED, spy.runner, gh=spy.gh)
        assert "acquire" in spy.events
        assert "release" not in spy.events
        assert "reap" not in spy.events

    def test_a_bed_lane_a_dirty_tree_refuses_takes_no_lease(self, monkeypatch, tmp_path):
        repo = _repo(tmp_path)
        # the static lane rewrites a measured output: stamped, but the bed lane after it
        # finds tracked dirt and never runs
        runner = FakeRunner(effects={"make check-python": lambda: _measure(repo, '{"x": 1}\n')})
        spy = _lease_spy(monkeypatch, runner, fg.scripted_gh([]))
        status, out, _ = _go(monkeypatch, repo, BED, spy.runner, gh=spy.gh)
        assert status == 1
        assert "commit the measured outputs" in out
        assert spy.events == ["run make check-python"]

    def test_a_reap_that_raises_keeps_the_lease(self, monkeypatch, tmp_path):
        runner = FakeRunner(effects={gs.STABILITY_UNIX: _raise(KeyboardInterrupt())})
        repo, spy = _spied(monkeypatch, tmp_path, runner=runner)

        def failing_reap(cell, pid):
            spy.events.append("reap")
            raise RuntimeError("console busy")

        monkeypatch.setattr("scripts.bed_reap.reap", failing_reap)
        status, out, _ = _go(monkeypatch, repo, BED, spy.runner, gh=spy.gh)
        assert status == gt.EXIT_INTERRUPTED
        assert spy.events[-2:] == [f"run {gs.STABILITY_UNIX}", "reap"]
        assert "release" not in spy.events
        assert (
            "gate-tip: the reap failed (console busy); the lease is kept — run make bed-reap" in out
        )

    def test_a_second_interrupt_during_the_reap_keeps_the_lease(self, monkeypatch, tmp_path):
        runner = FakeRunner(effects={gs.STABILITY_UNIX: _raise(KeyboardInterrupt())})
        repo, spy = _spied(monkeypatch, tmp_path, runner=runner)

        def interrupted_reap(cell, pid):
            spy.events.append("reap")
            raise KeyboardInterrupt

        monkeypatch.setattr("scripts.bed_reap.reap", interrupted_reap)
        _oracle(monkeypatch, repo, BED)
        opts = _options(repo, spy.runner, gh=spy.gh)
        with pytest.raises(KeyboardInterrupt):
            gt.run(opts)
        assert spy.events[-2:] == [f"run {gs.STABILITY_UNIX}", "reap"]
        assert "release" not in spy.events
        assert (
            "gate-tip: interrupted during the reap; the lease is kept and the bed is not "
            "reaped — run make bed-reap" in opts.out.getvalue()
        )
        # the CLI's unwind exits 130 and does not then claim nothing was held
        opts = _options(repo, spy.runner, gh=spy.gh)
        assert gt._run_interruptible(opts) == gt.EXIT_INTERRUPTED
        assert "release" not in spy.events
        assert "no lease is held" not in opts.out.getvalue()
        assert "the lease is kept" in opts.out.getvalue()

    def test_a_lane_that_raises_releases_the_lease(self, monkeypatch, tmp_path):
        runner = FakeRunner(effects={gs.STABILITY_UNIX: _raise(RuntimeError("boom"))})
        repo, spy = _spied(monkeypatch, tmp_path, runner=runner)
        with pytest.raises(RuntimeError):
            _go(monkeypatch, repo, BED, spy.runner, gh=spy.gh)
        assert spy.events[-2:] == [f"run {gs.STABILITY_UNIX}", "release"]

    def test_acquire_lease_loads_the_lab_and_holds_it_for_this_pid(self, monkeypatch, tmp_path):
        monkeypatch.undo()  # the real _acquire_lease, not the autouse double
        lab = tmp_path / "cell" / "lab.json"
        lab.parent.mkdir()
        lab.write_text(json.dumps({"labs": {"l": {}}}))
        monkeypatch.setattr("scripts.bed_lease.cell_resources", lambda *a: [])
        monkeypatch.setenv("BED_LEASE_ROOT", str(tmp_path / "lease"))
        seen = []
        monkeypatch.setattr(
            "scripts.bed_lease.acquire",
            lambda cell, pid, purpose: seen.append((cell, pid, purpose)) or 0,
        )
        repo = _repo(tmp_path)
        _oracle(monkeypatch, repo, BED)
        opts = _options(repo, FakeRunner(), lab=lab)
        ctx = gt._new_run(opts, gt._plan(opts))
        gt._acquire_lease(ctx)
        [(cell, pid, purpose)] = seen
        assert (cell.lab_file, pid, purpose) == (lab, os.getpid(), "gate-tip")
        assert cell.root == tmp_path / "lease", "the isolated root BED_LEASE_ROOT names"
        assert ctx.lease == gt.Lease(cell, os.getpid())

        monkeypatch.setattr("scripts.bed_lease.acquire", lambda cell, pid, purpose: 1)
        ctx = gt._new_run(opts, gt._plan(opts))
        with pytest.raises(gt.GateError, match="the bed lease was not acquired"):
            gt._acquire_lease(ctx)
        assert ctx.lease is None

        opts = _options(repo, FakeRunner(), lab=None)
        with pytest.raises(gt.GateError, match="--lab is required to run a bed lane"):
            gt._acquire_lease(gt._new_run(opts, gt._plan(opts)))

    def test_a_lab_file_the_lease_refuses_is_a_gate_error(self, monkeypatch, tmp_path):
        monkeypatch.undo()
        monkeypatch.setenv("BED_LEASE_ROOT", str(tmp_path / "lease"))
        repo = _repo(tmp_path)
        _oracle(monkeypatch, repo, BED)
        opts = _options(repo, FakeRunner(), lab=tmp_path / "absent.json")
        with pytest.raises(gt.GateError, match="no lab file at"):
            gt._acquire_lease(gt._new_run(opts, gt._plan(opts)))

    def test_release_lease_releases_this_pid_and_clears_the_lease(self, monkeypatch, tmp_path):
        monkeypatch.undo()
        seen = []
        monkeypatch.setattr(
            "scripts.bed_lease.release",
            lambda cell, pid, *, force: seen.append((cell, pid, force)) or 0,
        )
        repo = _repo(tmp_path)
        _oracle(monkeypatch, repo, BED)
        opts = _options(repo, FakeRunner())
        ctx = gt._new_run(opts, gt._plan(opts))
        cell = object()
        ctx.lease = gt.Lease(cell, 4242)
        gt._release_lease(ctx)
        assert seen == [(cell, 4242, False)]
        assert ctx.lease is None

    def test_the_runner_is_the_lease_taker_the_census_names(self, monkeypatch, tmp_path):
        from tests.unit.test_lane_invariants import LEASE_INSIDE

        assert LEASE_INSIDE == ["gate-tip", "land"]
        monkeypatch.undo()
        purposes = []
        monkeypatch.setattr("scripts.bed_lease.load_cell", lambda lab, root: object())
        monkeypatch.setattr(
            "scripts.bed_lease.acquire", lambda cell, pid, purpose: purposes.append(purpose) or 0
        )
        repo = _repo(tmp_path)
        _oracle(monkeypatch, repo, BED)
        opts = _options(repo, FakeRunner())
        gt._acquire_lease(gt._new_run(opts, gt._plan(opts)))
        assert purposes == ["gate-tip"]

    def test_an_ignored_source_planted_during_a_lane_blocks_its_stamp(self, monkeypatch, tmp_path):
        repo = _repo(tmp_path)
        (repo / ".git/info/exclude").write_text("src/otto/gone/\n")

        def plant():
            (repo / "src/otto/gone").mkdir(parents=True)
            (repo / "src/otto/gone/__init__.py").write_text("")

        runner = FakeRunner(effects={"make check-python": plant})
        status, out, _ = _go(monkeypatch, repo, STATIC, runner)
        assert status == 1
        assert "NOT STAMPED (the tree changed under the lane: src/otto/gone/__init__.py)" in out


class TestStatus:
    """A green gate posts ``local-gate``; a red one flips a success HEAD already carries."""

    def test_a_green_gate_posts_the_status_with_the_plan_s_description(self, monkeypatch, tmp_path):
        repo = _repo(tmp_path)
        gh = fg.scripted_gh([fg.GhReply([], stdout="{}")])
        status, out, opts = _go(monkeypatch, repo, STATIC, FakeRunner(), gh=gh)
        plan = gt._plan(opts)
        assert status == 0
        assert gh.calls[0][:4] == ["api", "-X", "POST", f"repos/o/r/statuses/{plan.tip_sha}"]
        assert f"description={lgs.format_description(lgs.expected(plan))}" in gh.calls[0]
        assert "state=success" in gh.calls[0]
        assert f"gate-tip: posted local-gate on {plan.tip_sha[:12]}" in out

    def test_a_failed_post_does_not_fail_a_green_gate(self, monkeypatch, tmp_path):
        repo = _repo(tmp_path)
        gh = fg.scripted_gh([fg.GhReply([], status=1, stderr="HTTP 422: No commit found for SHA")])
        status, out, _ = _go(monkeypatch, repo, STATIC, FakeRunner(), gh=gh)
        assert status == 0
        assert "local-gate not posted: push the branch, then make gate-status" in out

    def test_a_remote_that_is_not_github_does_not_fail_a_green_gate(self, monkeypatch, tmp_path):
        repo = _repo(tmp_path)
        _git(repo, "remote", "set-url", "origin", str(tmp_path / "bare.git"))
        status, out, opts = _go(monkeypatch, repo, STATIC, FakeRunner())
        assert status == 0
        assert "local-gate not posted: origin (" in out
        assert opts.gh.calls == []

    def test_a_red_lane_posts_a_failure_only_when_head_already_carries_a_success(
        self, monkeypatch, tmp_path
    ):
        repo = _repo(tmp_path)
        gh = fg.scripted_gh(
            [
                fg.GhReply(["commits/"], stdout=_success_body(monkeypatch, repo, STATIC)),
                fg.GhReply(["statuses/"], stdout="{}"),
            ]
        )
        runner = FakeRunner(statuses={"make collect-check": 1})
        status, _out, _ = _go(monkeypatch, repo, STATIC, runner, gh=gh)
        assert status == 1
        assert gh.calls[-1][:3] == ["api", "-X", "POST"]
        assert "state=failure" in gh.calls[-1]
        gh = fg.scripted_gh([fg.GhReply(["commits/"], stdout=json.dumps({"statuses": []}))])
        runner = FakeRunner(statuses={"make collect-check": 1})
        status, _out, _ = _go(monkeypatch, repo, STATIC, runner, gh=gh)
        assert status == 1
        assert all(c[:3] != ["api", "-X", "POST"] for c in gh.calls)

    def test_publish_failure_says_when_the_status_cannot_be_read(self, monkeypatch, tmp_path):
        repo = _repo(tmp_path)
        gh = fg.scripted_gh([fg.GhReply(["commits/"], status=1, stderr="HTTP 403: Forbidden")])
        runner = FakeRunner(statuses={"make collect-check": 1})
        status, out, _ = _go(monkeypatch, repo, STATIC, runner, gh=gh)
        assert status == 1
        assert "local-gate not read: " in out
        assert "HTTP 403" in out
        assert all(c[:3] != ["api", "-X", "POST"] for c in gh.calls)

    def test_a_lane_that_is_only_not_stamped_reads_no_head_status(self, monkeypatch, tmp_path):
        repo = _repo(tmp_path)

        def untracked():
            (repo / "tests" / "unit" / "test_new.py").write_text("def test_n():\n    pass\n")

        runner = FakeRunner(effects={"make check-python": untracked})
        status, _out, opts = _go(monkeypatch, repo, STATIC, runner)
        assert status == 1
        assert opts.gh.calls == [], "no red marker: nothing a failure could name"

    def test_the_post_runs_under_the_exclusive_store_lock(self, monkeypatch, tmp_path):
        repo = _repo(tmp_path)
        root = gt.stamps_root(repo, gt.STORE)
        seen = []

        def gh(args):
            # a reader cannot get in while the publisher holds the lock
            seen.append(_probe(root, fcntl.LOCK_SH))
            return lgs.GhResult(0, "{}", "")

        status, _out, _ = _go(monkeypatch, repo, STATIC, FakeRunner(), gh=gh)
        assert status == 0
        assert seen == [False]

    def test_a_peer_publisher_or_red_writer_waits_behind_a_stalled_post(
        self, monkeypatch, tmp_path
    ):
        # the POST is in flight: a second `gate-status` (exclusive, to re-validate) and a
        # peer's `_mark_red` (exclusive, to write) both block, out of process
        repo = _repo(tmp_path)
        _oracle(monkeypatch, repo, STATIC)
        _stamp(repo, "make check-python")
        _stamp(repo, "make collect-check")
        lock = gt.stamps_root(repo, gt.STORE) / gate_stamp_common.LOCK_FILE
        peer = []

        def stalled_gh(args):
            peer.append(_peer_probe(lock))
            return lgs.GhResult(0, "{}", "")

        opts = _options(repo, FakeRunner(), gh=stalled_gh)
        assert gt.publish(opts, gt._plan(opts), state="success", slug="o/r").posted
        assert peer == ["busy"]
        assert _peer_probe(lock) == "free", "released after the post"

    def test_a_post_that_times_out_releases_the_lock_and_is_reported_not_posted(
        self, monkeypatch, tmp_path
    ):
        # a hung gh holds the store lock for run_gh's timeout at most; afterwards a peer gets
        # in and the gate's exit status is untouched
        repo = _repo(tmp_path)
        lock = gt.stamps_root(repo, gt.STORE) / gate_stamp_common.LOCK_FILE
        # what run_gh returns after its bound (pinned in test_local_gate_status)
        timed_out = fg.scripted_gh([fg.GhReply([], status=124, stderr="gh timed out after 30s")])
        status, out, _ = _go(monkeypatch, repo, STATIC, FakeRunner(), gh=timed_out)
        assert status == 0
        assert "local-gate not posted: gh timed out after 30s" in out
        assert _peer_probe(lock) == "free"

    def test_a_red_marker_that_landed_before_the_lock_refuses_a_success(
        self, monkeypatch, tmp_path
    ):
        repo = _repo(tmp_path)
        _oracle(monkeypatch, repo, STATIC)
        _stamp(repo, "make check-python")
        _stamp(repo, "make collect-check")
        opts = _options(repo, FakeRunner())
        plan = gt._plan(opts)
        # the peer's evidence arrived between the walk and the post
        root = gt.stamps_root(repo, gt.STORE)
        gt.write_red(root, tree=plan.tree, key="make collect-check", checkout="/peer")
        out = gt.publish(opts, plan, state="success", slug="o/r")
        assert not out.posted
        assert "make collect-check" in out.message
        assert opts.gh.calls == []

    def test_a_failure_that_a_later_green_superseded_is_not_published(self, monkeypatch, tmp_path):
        repo = _repo(tmp_path)
        opts = _options(repo, FakeRunner(), gh=fg.scripted_gh([fg.GhReply([], stdout="{}")]))
        _oracle(monkeypatch, repo, STATIC)
        plan = gt._plan(opts)
        root = gt.stamps_root(repo, gt.STORE)
        gt.write_red(root, tree=plan.tree, key="make collect-check", checkout=str(repo))
        _stamp(repo, "make collect-check")  # the green re-run wrote over the red
        _stamp(repo, "make check-python")
        out = gt.publish(opts, plan, state="failure", slug="o/r", red_keys=["make collect-check"])
        assert not out.posted
        assert "no longer red" in out.message
        assert opts.gh.calls == []

    def test_a_failure_whose_lanes_are_still_red_is_published(self, monkeypatch, tmp_path):
        repo = _repo(tmp_path)
        opts = _options(repo, FakeRunner())
        _oracle(monkeypatch, repo, STATIC)
        plan = gt._plan(opts)
        root = gt.stamps_root(repo, gt.STORE)
        gt.write_red(root, tree=plan.tree, key="make collect-check", checkout=str(repo))
        out = gt.publish(opts, plan, state="failure", slug="o/r", red_keys=["make collect-check"])
        assert out == gt.PublishOutcome(True, "")
        assert "state=failure" in opts.gh.calls[0]

    def test_a_failure_that_names_no_red_lane_is_not_published(self, monkeypatch, tmp_path):
        repo = _repo(tmp_path)
        opts = _options(repo, FakeRunner())
        _oracle(monkeypatch, repo, STATIC)
        for red_keys in (None, []):
            out = gt.publish(opts, gt._plan(opts), state="failure", slug="o/r", red_keys=red_keys)
            assert out == gt.PublishOutcome(False, "a failure post names the red lanes")
        assert opts.gh.calls == []

    def test_the_default_gh_runner_is_run_gh_bounded_by_gh_timeout(self):
        assert gt.Options(repo=Path()).gh is lgs.run_gh
        assert lgs.GH_TIMEOUT == 30.0
        assert inspect.signature(lgs.run_gh).parameters["timeout"].default == lgs.GH_TIMEOUT

    def test_the_remedy_names_the_ref_the_lane_and_the_one_command(self, tmp_path):
        plan = gs.Plan(
            base_sha="c" * 40,
            tip_sha="d" * 40,
            tree="e" * 40,
            live=[],
            stamped=["make coverage-bed"],
            bed=True,
            classes=["src-leaf"],
        )
        lines = gt.refusal_lines(
            plan, [gt.Missing("make coverage-bed", "no stamp")], ref="refs/heads/x", who="gate-tip"
        )
        assert lines[0] == (
            "gate-tip: refusing refs/heads/x (dddddddddddd) — its gate is not stamped on tree "
            "eeeeeeeeeeee (range cccccccccccc..dddddddddddd):"
        )
        assert lines[1] == "  make coverage-bed: no stamp"
        assert lines[2] == "run: make gate-tip   (on commit dddddddddddd), then push again."
        assert lines[3] == (
            "git push --no-verify skips this check; the local-gate status then stays absent "
            "and gate-admission holds the pull request's CI."
        )
        assert "to main" not in "\n".join(lines)
        assert "never make coverage" not in "\n".join(lines)

    def test_gate_status_refuses_an_unstamped_tree_posts_a_stamped_one_and_then_admits(
        self, monkeypatch, tmp_path
    ):
        repo = _repo(tmp_path)
        _oracle(monkeypatch, repo, STATIC)
        gh = fg.scripted_gh(
            [fg.GhReply(["statuses/"], stdout="{}"), fg.GhReply(["run", "list"], stdout="[]")]
        )
        opts = _options(repo, FakeRunner(), gh=gh)
        err = io.StringIO()
        monkeypatch.setattr(sys, "stderr", err)
        assert gt.status_command(opts) == 1
        assert "gate-status: refusing HEAD" in err.getvalue()
        assert gh.calls == []
        _stamp(repo, "make check-python")
        _stamp(repo, "make collect-check")
        assert gt.status_command(opts) == 0
        assert [c[:2] for c in gh.calls] == [["api", "-X"], ["run", "list"]]

    def test_gate_status_refuses_a_red_marker(self, monkeypatch, tmp_path):
        repo = _repo(tmp_path)
        _oracle(monkeypatch, repo, STATIC)
        opts = _options(repo, FakeRunner())
        _stamp(repo, "make check-python")
        plan = gt._plan(opts)
        root = gt.stamps_root(repo, gt.STORE)
        gt.write_red(root, tree=plan.tree, key="make collect-check", checkout=str(repo))
        assert gt.status_command(opts) == 1
        assert opts.gh.calls == []

    def test_gate_status_reports_a_post_it_could_not_make(self, monkeypatch, tmp_path, capsys):
        repo = _repo(tmp_path)
        _oracle(monkeypatch, repo, STATIC)
        _stamp(repo, "make check-python")
        _stamp(repo, "make collect-check")
        gh = fg.scripted_gh([fg.GhReply([], status=1, stderr="HTTP 422: No commit found")])
        assert gt.status_command(_options(repo, FakeRunner(), gh=gh)) == 1
        assert (
            "gate-status: local-gate not posted: push the branch, then make gate-status"
            in capsys.readouterr().err
        )

    def test_the_status_cli_runs_status_command(self, monkeypatch, tmp_path):
        repo = _repo(tmp_path)
        seen = []
        monkeypatch.setattr(gt, "status_command", lambda opts: seen.append(opts) or 0)
        assert gt.main(["--repo", str(repo), "--status", "--remote", "origin"]) == 0
        assert (seen[0].repo, seen[0].remote, seen[0].base) == (repo, "origin", None)
        with pytest.raises(SystemExit):
            gt.main(["--repo", str(repo), "--status", "--dry-run"])

    def test_an_interrupted_status_exits_130_without_a_traceback(
        self, monkeypatch, tmp_path, capsys
    ):
        import signal

        repo = _repo(tmp_path)
        before = signal.getsignal(signal.SIGTERM)

        def interrupted(_opts):
            raise KeyboardInterrupt

        monkeypatch.setattr(gt, "status_command", interrupted)
        assert gt.main(["--repo", str(repo), "--status"]) == gt.EXIT_INTERRUPTED
        assert "gate-tip: interrupted; no lease is held" in capsys.readouterr().out
        assert signal.getsignal(signal.SIGTERM) is before


class TestBase:
    """The base is the fetched main unless one is given; a dry run never fetches."""

    def test_the_default_base_is_the_fetched_main(self, monkeypatch, tmp_path):
        repo, _bare_repo, remote_sha, _tip = _published(tmp_path)
        seen = []
        monkeypatch.setattr(
            gt, "fetch_main", lambda r, remote: (seen.append(remote), remote_sha)[1]
        )
        plan = gt._plan(gt.Options(repo=repo, remote="origin"))
        assert seen == ["origin"]
        assert plan.base_sha == remote_sha

    def test_a_dry_run_with_no_base_reads_origin_main_without_fetching(self, monkeypatch, tmp_path):
        repo = _repo(tmp_path)
        monkeypatch.setattr(gt, "fetch_main", lambda *a: pytest.fail("fetched"))
        plan = gt._plan(gt.Options(repo=repo, dry_run=True))
        assert plan.base_sha == _git(repo, "rev-parse", "origin/main").strip()

    def test_a_given_base_is_used_as_given(self, monkeypatch, tmp_path):
        repo = _repo(tmp_path)
        monkeypatch.setattr(gt, "fetch_main", lambda *a: pytest.fail("fetched"))
        head = _git(repo, "rev-parse", "HEAD").strip()
        assert gt.resolve_base(gt.Options(repo=repo, base="HEAD")) == "HEAD"
        assert gt._plan(gt.Options(repo=repo, base="HEAD")).base_sha == head

    def test_an_empty_base_on_the_command_line_means_the_fetched_main(self, monkeypatch, tmp_path):
        repo = _repo(tmp_path)
        seen = []
        monkeypatch.setattr(gt, "_run_interruptible", lambda opts: seen.append(opts) or 0)
        assert gt.main(["--repo", str(repo), "--base", ""]) == 0
        assert seen[0].base is None

    def test_the_recipe_s_argument_shape_plans_without_an_argument_error(self, tmp_path):
        """What `make gate-tip` passes: --lab with no --pid, an empty --base, --remote origin."""
        repo = _repo(tmp_path)
        lab = tmp_path / "cell" / "lab.json"  # never read: a dry run takes no lease
        completed = subprocess.run(
            [
                sys.executable,
                str(CHECKOUT_ROOT / "scripts/gate_tip.py"),
                "--repo",
                str(repo),
                "--lab",
                str(lab),
                "--remote",
                "origin",
                "--base",
                "",
                "--dry-run",
            ],
            cwd=repo,
            env=lease_free_environ(**git_env(repo)),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        assert "error:" not in completed.stderr
        assert "gate-tip: merge-base " in completed.stdout


class TestInterrupt:
    """``_run_interruptible``: SIGTERM is an interrupt; an undrained run keeps the lease.

    The lease's own cleanup belongs to ``run()`` (:class:`TestLease`); the runner's part of
    the policy (the interrupt relayed to the lane's group and waited out, the second one
    killing it) is :mod:`scripts.process_group`'s, pinned in its own tests.
    """

    def test_an_undrained_run_keeps_the_lease_and_names_the_hand_reap(self, tmp_path, monkeypatch):
        repo = _repo(tmp_path)

        def undrained(_opts):
            raise UndrainedError(4242, [4243])

        monkeypatch.setattr(gt, "run", undrained)
        opts = _options(repo, FakeRunner())
        assert gt._run_interruptible(opts) == gt.EXIT_INTERRUPTED
        out = opts.out.getvalue()
        assert "interrupted twice and" in out
        assert "pids 4243" in out
        assert "run `make bed-reap` once they are gone" in out

    def test_an_interrupt_outside_the_walk_exits_130_with_nothing_to_reap(
        self, tmp_path, monkeypatch
    ):
        repo = _repo(tmp_path)

        def interrupted(_opts):
            raise KeyboardInterrupt

        monkeypatch.setattr(gt, "run", interrupted)
        opts = _options(repo, FakeRunner())
        assert gt._run_interruptible(opts) == gt.EXIT_INTERRUPTED
        assert "no lease is held, nothing to reap" in opts.out.getvalue()

    def test_sigterm_is_the_same_unwind_as_an_interrupt(self, tmp_path, monkeypatch):
        import signal

        def terminated():
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(5)
            pytest.fail("SIGTERM did not unwind the lane")

        runner = FakeRunner(effects={gs.STABILITY_UNIX: terminated})
        repo, spy = _spied(monkeypatch, tmp_path, runner=runner)
        _oracle(monkeypatch, repo, BED)
        before = signal.getsignal(signal.SIGTERM)
        opts = _options(repo, spy.runner, gh=spy.gh)
        assert gt._run_interruptible(opts) == gt.EXIT_INTERRUPTED
        assert spy.events[-2:] == ["reap", "release"]
        assert signal.getsignal(signal.SIGTERM) is before, "the handler is restored"

    @pytest.mark.timeout(60)
    def test_a_real_lane_is_interrupted_through_the_shared_core(self, tmp_path, monkeypatch):
        """Ctrl-C mid-lane: the lane's grandchild is gone before the reap runs, and 130."""
        from otto.utils import wait_for
        from tests.unit.scripts.test_process_group import _SPAWNER, _gone

        repo = _repo(tmp_path)
        pid_file = tmp_path / "grandchild.pid"
        order = []
        monkeypatch.setattr(
            "scripts.bed_reap.reap",
            lambda cell, pid: order.append(_gone(int(pid_file.read_text()))) or 0,
        )
        lane = f"{sys.executable} -c {json.dumps(_SPAWNER)} {pid_file}"
        real_wait = subprocess.Popen.wait
        raised = []

        def wait_then_interrupt(self, timeout=None):
            # Only the lane's own wait is the Ctrl-C: the run's other subprocesses wait too.
            if not raised and self.args == lane:
                raised.append(self.pid)
                wait_for(pid_file.exists, 10.0, interval=0.02, on_timeout="no grandchild pid")
                raise KeyboardInterrupt
            return real_wait(self, timeout)

        monkeypatch.setattr(subprocess.Popen, "wait", wait_then_interrupt)
        _oracle(monkeypatch, repo, [LANE])

        def runner(cmd, cwd, env):
            # The oracle's known lane, run as the spawner through the real runner.
            return gt._default_runner(lane if cmd == LANE else cmd, cwd, env)

        opts = _options(repo, runner)
        assert gt._run_interruptible(opts) == gt.EXIT_INTERRUPTED
        assert order == [True], "the grandchild was gone when the reap ran"
        assert gt.exact_stamp(gt.stamps_root(repo, gt.STORE), tree_hash(repo), LANE) is None

    def test_the_lab_option_reaches_the_run(self, tmp_path, monkeypatch):
        repo = _repo(tmp_path)
        seen = []
        monkeypatch.setattr(gt, "_run_interruptible", lambda opts: seen.append(opts) or 0)
        argv = ["--repo", str(repo), "--lab", "tests/lab.json", "--dry-run"]
        assert gt.main(argv) == 0
        assert seen[0].lab == Path("tests/lab.json")
        assert seen[0].dry_run
        assert not hasattr(seen[0], "pid")
        seen.clear()
        assert gt.main(["--repo", str(repo)]) == 0
        assert seen[0].lab is None
        with pytest.raises(SystemExit):
            gt.main(["--repo", str(repo), "--lab", "x", "--pid", "1"])


# ── The pre-push hook: stamps for every branch push, main refused, the live check at HEAD ──

ZERO = "0" * 40
A = "a" * 40
B = "b" * 40


def _branch(ref: str, sha: str, remote_sha: str = ZERO) -> str:
    """One line of git's pre-push stdin pushing *sha* to *ref*."""
    return f"{ref} {sha} {ref} {remote_sha}\n"


def test_pushed_refs_keeps_branch_updates_and_skips_tags_and_deletes():
    text = (
        f"refs/heads/x {A} refs/heads/x {B}\n"
        f"refs/tags/v1 {A} refs/tags/v1 {ZERO}\n"
        f"(delete) {ZERO} refs/heads/old {B}\n"
    )
    assert gt.pushed_refs(text) == [gt.PushedRef("refs/heads/x", A, B)]
    assert gt.pushed_refs("") == []


def test_the_remote_ref_decides_not_the_local_one():
    """`git push origin feature:main` updates main from a branch of another name."""
    refs = gt.pushed_refs(f"refs/heads/feature {A} refs/heads/main {B}\n")
    assert refs == [gt.PushedRef(gt.MAIN_REF, A, B)]


def test_a_tag_only_or_delete_only_push_fetches_nothing(monkeypatch, tmp_path):
    repo = _repo(tmp_path)
    fetched = []
    monkeypatch.setattr(gt, "fetch_main", lambda *a: fetched.append(a))
    text = f"refs/tags/v1 {A} refs/tags/v1 {ZERO}\n(delete) {ZERO} refs/heads/old {B}\n"
    assert gt.pre_push(gt.Options(repo=repo), text) == 0
    assert fetched == []


def test_a_push_to_main_is_refused_before_any_fetch_even_beside_another_ref(monkeypatch, tmp_path):
    repo = _repo(tmp_path)
    fetched = []
    monkeypatch.setattr(gt, "fetch_main", lambda *a: fetched.append(a))
    err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", err)
    text = f"refs/heads/x {A} refs/heads/x {B}\nrefs/heads/main {A} refs/heads/main {B}\n"
    assert gt.pre_push(gt.Options(repo=repo), text) == 1
    assert fetched == []
    assert err.getvalue() == (
        "gate-tip: main takes no direct push; open a pull request (a non-canonical remote such "
        "as a mirror takes --no-verify: the ruleset, not this hook, protects the canonical main)\n"
    )


def test_a_remote_without_main_is_a_refusal_with_the_remedy(monkeypatch, tmp_path):
    repo, _bare_repo, _remote_sha, tip = _published(tmp_path)
    empty = _bare(tmp_path, "empty")
    _git(repo, "remote", "add", "empty", str(empty))
    err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", err)
    opts = gt.Options(repo=repo, remote="empty", runner=FakeRunner())
    assert gt.pre_push(opts, _branch("refs/heads/x", tip)) == 1
    assert "has no main" in err.getvalue()
    assert "git fetch empty main failed" in err.getvalue()


def test_an_unstamped_branch_tip_is_refused_with_the_remedy(monkeypatch, tmp_path):
    repo, _bare_repo, remote_sha, tip = _published(tmp_path)
    err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", err)
    runner = FakeRunner()
    assert gt.pre_push(gt.Options(repo=repo, runner=runner), _branch("refs/heads/x", tip)) == 1
    plan = gs.plan(repo, remote_sha, tip)
    assert plan.stamped, "a tests-leaf tip owes stamped lanes"
    assert err.getvalue().startswith(f"gate-tip: refusing refs/heads/x ({tip[:12]})")
    expected = gt.refusal_lines(
        plan, gt.missing_stamps(repo, plan), ref="refs/heads/x", who="gate-tip"
    )
    assert err.getvalue() == "\n".join(expected) + "\n", "origin's push names no other remote"
    assert f"run: make gate-tip   (on commit {tip[:12]}), then push again." in err.getvalue()
    assert runner.calls == [], "no live check runs for a refused push"


def test_a_push_to_another_remote_names_the_remote_the_remedy_does_not_plan_against(
    monkeypatch, tmp_path
):
    repo, _bare_repo, remote_sha, tip = _published(tmp_path, "upstream")
    err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", err)
    opts = gt.Options(repo=repo, remote="upstream", runner=FakeRunner())
    assert gt.pre_push(opts, _branch("refs/heads/x", tip)) == 1
    lines = err.getvalue().rstrip("\n").splitlines()
    assert lines[0].startswith(f"gate-tip: refusing refs/heads/x ({tip[:12]})")
    assert f"(range {remote_sha[:12]}..{tip[:12]})" in lines[0], "the hook plans upstream's main"
    assert lines[-1] == "the push targets upstream; make gate-tip planned against origin/main"


def test_a_url_push_to_the_canonical_remote_names_no_other_remote(monkeypatch, tmp_path):
    """git hands the hook the URL for `git push <url>`; origin's own URL is origin."""
    repo, bare, _remote_sha, tip = _published(tmp_path)
    err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", err)
    opts = gt.Options(repo=repo, remote=str(bare), runner=FakeRunner())
    assert gt.pre_push(opts, _branch("refs/heads/x", tip)) == 1
    assert err.getvalue().startswith(f"gate-tip: refusing refs/heads/x ({tip[:12]})")
    assert "the push targets" not in err.getvalue()


def test_a_url_push_elsewhere_names_the_url(monkeypatch, tmp_path):
    repo, _bare_repo, remote_sha, tip = _published(tmp_path)
    mirror = _bare(tmp_path, "mirror")
    _git(repo, "push", "-q", str(mirror), f"{remote_sha}:refs/heads/main")
    err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", err)
    opts = gt.Options(repo=repo, remote=str(mirror), runner=FakeRunner())
    assert gt.pre_push(opts, _branch("refs/heads/x", tip)) == 1
    assert err.getvalue().splitlines()[-1] == (
        f"the push targets {mirror}; make gate-tip planned against origin/main"
    )


def test_a_stamped_head_runs_the_plan_s_live_line_through_the_runner(monkeypatch, tmp_path):
    repo, _bare_repo, remote_sha, tip = _published(tmp_path)
    _stamp_tip(repo, remote_sha, tip)
    runner = FakeRunner()
    monkeypatch.setenv("MAKEFLAGS", "-j9")
    assert gt.pre_push(gt.Options(repo=repo, runner=runner), _branch("refs/heads/x", tip)) == 0
    assert [c[0] for c in runner.calls] == [f"make check-breaking RANGE={remote_sha}..{tip}"]
    _cmd, cwd, env = runner.calls[0]
    assert cwd == repo
    assert "MAKEFLAGS" not in env, "the live line runs in the lane environment"


def test_a_red_live_check_is_the_hook_s_status(monkeypatch, tmp_path):
    repo, _bare_repo, remote_sha, tip = _published(tmp_path)
    _stamp_tip(repo, remote_sha, tip)
    runner = FakeRunner(statuses={f"make check-breaking RANGE={remote_sha}..{tip}": 2})
    assert gt.pre_push(gt.Options(repo=repo, runner=runner), _branch("refs/heads/x", tip)) == 2


def test_a_stamped_tip_that_is_not_head_passes_with_the_ci_note(monkeypatch, tmp_path, capsys):
    repo, _bare_repo, remote_sha, tip = _published(tmp_path)
    _stamp_tip(repo, remote_sha, tip)
    other = _commit_file(repo, "peer.txt", "p", "peer commit")
    _git(repo, "checkout", "-q", tip)  # HEAD is tip; push the peer's sha
    _stamp_tip(repo, remote_sha, other)
    runner = FakeRunner()
    assert gt.pre_push(gt.Options(repo=repo, runner=runner), _branch("refs/heads/peer", other)) == 0
    assert runner.calls == []
    assert capsys.readouterr().out == (
        f"gate-tip: refs/heads/peer ({other[:12]}) is not the checked-out tip; CI's lint-python "
        "runs check-breaking-marks over the pull request's range\n"
    )


def test_a_two_ref_push_checks_both_and_runs_the_history_check_for_head(monkeypatch, tmp_path):
    repo, _bare_repo, remote_sha, tip = _published(tmp_path)
    other = _commit_file(repo, "peer.txt", "p", "peer")
    _git(repo, "checkout", "-q", tip)
    _stamp_tip(repo, remote_sha, tip)
    _stamp_tip(repo, remote_sha, other)
    runner = FakeRunner()
    text = _branch("refs/heads/peer", other) + _branch("refs/heads/x", tip)
    assert gt.pre_push(gt.Options(repo=repo, runner=runner), text) == 0
    assert [c[0] for c in runner.calls] == [f"make check-breaking RANGE={remote_sha}..{tip}"]


def test_a_two_ref_push_with_one_unstamped_ref_is_refused_before_any_live_check(
    monkeypatch, tmp_path
):
    repo, _bare_repo, remote_sha, tip = _published(tmp_path)
    other = _commit_file(repo, "tests/unit/cli/test_p.py", "def test_p(): pass\n", "peer")
    _git(repo, "checkout", "-q", tip)
    _stamp_tip(repo, remote_sha, tip)
    runner = FakeRunner()
    err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", err)
    text = _branch("refs/heads/x", tip) + _branch("refs/heads/peer", other)
    assert gt.pre_push(gt.Options(repo=repo, runner=runner), text) == 1
    assert err.getvalue().startswith(f"gate-tip: refusing refs/heads/peer ({other[:12]})")
    assert runner.calls == []


def test_two_refs_at_head_run_the_history_check_once(monkeypatch, tmp_path):
    repo, _bare_repo, remote_sha, tip = _published(tmp_path)
    _stamp_tip(repo, remote_sha, tip)
    runner = FakeRunner()
    text = _branch("refs/heads/x", tip) + _branch("refs/heads/y", tip)
    assert gt.pre_push(gt.Options(repo=repo, runner=runner), text) == 0
    assert len(runner.calls) == 1


@pytest.mark.parametrize("plant", ["tracked", "untracked-src", "ignored-src"])
def test_dirt_that_the_history_check_would_read_refuses_before_it_runs(
    monkeypatch, tmp_path, plant
):
    repo, _bare_repo, remote_sha, tip = _published(tmp_path)
    _stamp_tip(repo, remote_sha, tip)
    if plant == "tracked":
        (repo / "tracked.txt").write_text("dirty")
    elif plant == "untracked-src":
        (repo / "src/otto").mkdir(parents=True, exist_ok=True)
        (repo / "src/otto/restored.py").write_text("")
    else:
        (repo / ".git/info/exclude").write_text("src/otto/gone/\n")
        (repo / "src/otto/gone").mkdir(parents=True)
        (repo / "src/otto/gone/__init__.py").write_text("")
    runner = FakeRunner()
    err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", err)
    assert gt.pre_push(gt.Options(repo=repo, runner=runner), _branch("refs/heads/x", tip)) == 1
    assert runner.calls == []
    assert err.getvalue().startswith("gate-tip: refusing — ")
    assert "(the check reads the tree on disk)" in err.getvalue()


def test_dirt_does_not_refuse_a_push_of_a_tip_that_is_not_head(tmp_path):
    repo, _bare_repo, remote_sha, tip = _published(tmp_path)
    other = _commit_file(repo, "peer.txt", "p", "peer")
    _git(repo, "checkout", "-q", tip)
    _stamp_tip(repo, remote_sha, other)
    (repo / "tracked.txt").write_text("dirty")
    opts = gt.Options(repo=repo, runner=FakeRunner())
    assert gt.pre_push(opts, _branch("refs/heads/p", other)) == 0


def test_an_identical_tree_first_push_passes_on_the_live_line_alone(monkeypatch, tmp_path):
    repo, _bare_repo, remote_sha, _tip = _published(tmp_path)
    _git(repo, "checkout", "-q", "-b", "release/next", remote_sha)
    runner = FakeRunner()
    text = _branch("refs/heads/release/next", remote_sha)
    assert gt.pre_push(gt.Options(repo=repo, runner=runner), text) == 0
    assert runner.calls[0][0] == f"make check-breaking RANGE={remote_sha}..{remote_sha}"


def test_the_base_is_fetch_head_on_a_path_remote(tmp_path):
    repo, bare, remote_sha, tip = _published(tmp_path)
    _stamp_tip(repo, remote_sha, tip)
    runner = FakeRunner()
    # no refs/remotes/<path>/main exists; FETCH_HEAD is what resolves
    opts = gt.Options(repo=repo, remote=str(bare), runner=runner)
    assert gt.pre_push(opts, _branch("refs/heads/x", tip)) == 0
    assert runner.calls[0][0] == f"make check-breaking RANGE={remote_sha}..{tip}"


def test_the_base_is_the_remote_s_main_now_not_the_tracking_ref(monkeypatch, tmp_path):
    """A remote main reset behind the clone's tracking ref: the hook fetches, so the
    larger range's lanes are demanded."""
    repo, bare, a_sha, _tip0 = _published(tmp_path)
    b_sha = _commit_file(repo, "schemas/unrowed.json", "{}\n", "measure")
    c_sha = _commit_file(repo, "tests/unit/cli/test_c.py", "def test_c(): pass\n", "tip")
    _git(repo, "push", "-q", "origin", f"{b_sha}:refs/heads/main")
    _stamp_tip(repo, b_sha, c_sha)
    _git(repo, "--git-dir", str(bare), "update-ref", "refs/heads/main", a_sha)
    err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", err)
    opts = gt.Options(repo=repo, runner=FakeRunner())
    assert gt.pre_push(opts, _branch("refs/heads/x", c_sha)) == 1
    assert f"(range {a_sha[:12]}..{c_sha[:12]}):" in err.getvalue()
    assert "  make coverage-bed: no stamp" in err.getvalue()


def test_a_multi_commit_push_demands_the_last_tree_only(tmp_path):
    repo, _bare_repo, remote_sha, _tip = _published(tmp_path)
    _commit_file(repo, "tests/unit/cli/test_c.py", "def test_c(): pass\n", "second")
    last = _commit_file(repo, "tests/unit/cli/test_d.py", "def test_d(): pass\n", "third")
    _stamp_tip(repo, remote_sha, last)
    runner = FakeRunner()
    assert gt.pre_push(gt.Options(repo=repo, runner=runner), _branch("refs/heads/x", last)) == 0
    assert len(runner.calls) == 1


def test_a_stamped_tree_is_honoured_by_a_squash_and_a_message_amend(tmp_path):
    repo, _bare_repo, remote_sha, tip = _published(tmp_path)
    _stamp_tip(repo, remote_sha, tip)
    squash = _git(repo, "commit-tree", tree_hash(repo, tip), "-p", remote_sha, "-m", "s").strip()
    runner = FakeRunner()
    assert gt.pre_push(gt.Options(repo=repo, runner=runner), _branch("refs/heads/s", squash)) == 0
    _git(repo, "commit", "--amend", "-qm", "tip, reworded")
    amended = _git(repo, "rev-parse", "HEAD").strip()
    assert amended != tip
    assert gt.pre_push(gt.Options(repo=repo, runner=runner), _branch("refs/heads/x", amended)) == 0
    assert [c[0] for c in runner.calls] == [f"make check-breaking RANGE={remote_sha}..{amended}"]


def test_an_oracle_failure_names_the_ref_and_range(monkeypatch, tmp_path):
    repo, _bare_repo, remote_sha, tip = _published(tmp_path)

    def boom(*_a, **_k):
        raise gs.GateScopeError("boom")

    monkeypatch.setattr(gs, "plan", boom)
    err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", err)
    runner = FakeRunner()
    assert gt.pre_push(gt.Options(repo=repo, runner=runner), _branch("refs/heads/x", tip)) == 1
    assert err.getvalue() == f"gate-tip: refs/heads/x ({remote_sha[:12]}..{tip[:12]}): boom\n"
    assert runner.calls == []


def test_a_lane_this_runner_does_not_know_refuses_with_one_line(monkeypatch, tmp_path):
    repo, _bare_repo, remote_sha, tip = _published(tmp_path)
    # the real plan: the oracle emitting an unknown goal refuses as it classifies its lines
    monkeypatch.setattr(
        gs, "commands", lambda *a, **k: [LANE, "make a-goal-this-runner-never-knew"]
    )
    err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", err)
    runner = FakeRunner()
    assert gt.pre_push(gt.Options(repo=repo, runner=runner), _branch("refs/heads/x", tip)) == 1
    (line,) = err.getvalue().splitlines()
    assert line.startswith(f"gate-tip: refs/heads/x ({remote_sha[:12]}..{tip[:12]}): ")
    assert line.endswith(
        "the oracle emitted a lane it does not classify: make a-goal-this-runner-never-knew"
    )
    assert runner.calls == []


def test_the_pre_push_cli_reads_stdin(monkeypatch, tmp_path):
    repo = _repo(tmp_path)
    seen = []
    monkeypatch.setattr(gt, "pre_push", lambda opts, text: seen.append((opts, text)) or 7)
    monkeypatch.setattr(sys, "stdin", io.StringIO("refs/heads/x a refs/heads/x b\n"))
    assert gt.main(["--repo", str(repo), "--pre-push", "--remote", "up"]) == 7
    ((opts, text),) = seen
    assert (opts.repo, opts.remote, text) == (repo, "up", "refs/heads/x a refs/heads/x b\n")


@pytest.mark.parametrize("mode", ["--dry-run", "--status", "--timings"])
def test_the_pre_push_switch_excludes_the_other_modes(tmp_path, capsys, mode):
    with pytest.raises(SystemExit) as exc:
        gt.main(["--repo", str(tmp_path), "--pre-push", mode])
    assert exc.value.code == 2
    assert "not allowed with argument --pre-push" in capsys.readouterr().err


def test_the_check_flag_is_gone(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        gt.main(["--repo", str(tmp_path), "--check", A, B])
    assert exc.value.code == 2
    assert "unrecognized arguments: --check" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("raised", "says"),
    [
        (KeyboardInterrupt(), "gate-tip: interrupted; no lease is held, nothing to reap\n"),
        (UndrainedError(4242, [4243]), "pids 4243"),
    ],
)
def test_an_interrupted_hook_exits_130_with_one_line(monkeypatch, tmp_path, capsys, raised, says):
    repo, _bare_repo, _remote_sha, tip = _published(tmp_path)

    def interrupted(*_a):
        raise raised

    monkeypatch.setattr(gt, "fetch_main", interrupted)
    monkeypatch.setattr(sys, "stdin", io.StringIO(_branch("refs/heads/x", tip)))
    assert gt.main(["--repo", str(repo), "--pre-push"]) == gt.EXIT_INTERRUPTED
    out, err = capsys.readouterr()
    assert says in out
    assert len(out.splitlines()) == 1
    assert "Traceback" not in out + err


def test_the_hook_file_is_executable_and_runs_the_locked_pre_push():
    hook = CHECKOUT_ROOT / ".githooks/pre-push"
    assert hook.stat().st_mode & 0o111
    assert (
        hook.read_text()
        .rstrip()
        .endswith('exec uv run --locked python scripts/gate_tip.py --pre-push --remote "$1"')
    )


def test_the_hook_is_committed_executable(tmp_path):
    """git never runs a non-executable hook; the committed mode is what every clone gets."""
    output = subprocess.run(
        ["git", "ls-files", "-s", ".githooks/pre-push"],
        cwd=CHECKOUT_ROOT,
        capture_output=True,
        text=True,
        check=True,
        env=git_env(tmp_path),
    ).stdout
    assert output.split()[0] == "100755", output


def test_gate_fresh_is_gone():
    assert not (CHECKOUT_ROOT / "scripts/gate_fresh.py").exists()
    assert "gate-fresh:" not in (CHECKOUT_ROOT / "Makefile").read_text()
    assert not hasattr(gt, "_ancestry_refusal")


def test_gate_status_refuses_an_unstamped_tree_before_it_reads_the_remote(monkeypatch, tmp_path):
    repo = _repo(tmp_path)
    elsewhere = tmp_path / "elsewhere.git"
    _git(repo, "remote", "set-url", "origin", str(elsewhere))
    _oracle(monkeypatch, repo, STATIC)
    err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", err)
    opts = _options(repo, FakeRunner())
    assert gt.status_command(opts) == 1
    assert err.getvalue().startswith("gate-status: refusing HEAD")
    _stamp(repo, "make check-python")
    _stamp(repo, "make collect-check")
    err.truncate(0)
    err.seek(0)
    assert gt.status_command(opts) == 1
    assert err.getvalue() == (
        f"gate-status: local-gate not posted: origin ({elsewhere}) is not a GitHub remote; "
        "the status needs owner/repo\n"
    )
    assert opts.gh.calls == []


def _tree(repo: Path, rev: str) -> str:
    return _git(repo, "rev-parse", f"{rev}^{{tree}}").strip()


def _commit_all(repo: Path, files: "dict[str, str | bytes | None]", message: str) -> str:
    """Write (a str or bytes) or delete (None) each path, commit them as one, return the sha."""
    for path, content in files.items():
        target = repo / path
        if content is None:
            target.unlink()
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD").strip()


@dataclasses.dataclass
class _Landing:
    """B0 → T0 on the ``landing`` branch, B1 on main; ``landing`` checked out."""

    repo: Path
    b0: str
    t0: str
    b1: str

    def rebase(self) -> str:
        """Rebase ``landing`` onto main and return T1."""
        _git(self.repo, "rebase", "-q", "main")
        return _git(self.repo, "rev-parse", "HEAD").strip()

    def amend(self, path: str, content: "str | bytes") -> str:
        """Amend HEAD with *path* rewritten to *content* and return the amended sha."""
        if isinstance(content, bytes):
            (self.repo / path).write_bytes(content)
        else:
            (self.repo / path).write_text(content)
        _git(self.repo, "commit", "-q", "-a", "--amend", "--no-edit")
        return _git(self.repo, "rev-parse", "HEAD").strip()

    def replay(self) -> "str | None":
        return gt.replays_to(self.repo, self.b0, _tree(self.repo, self.t0), self.b1)


def _landing(
    tmp_path: Path,
    seed: "dict[str, str | bytes | None]",
    landing: "dict[str, str | bytes | None]",
    main: "dict[str, str | bytes | None]",
    *,
    executable: "list[str] | None" = None,
) -> _Landing:
    """Seed B0, commit *landing* on a branch (chmod +x *executable* first), *main* on main."""
    repo = _fresh_repo(tmp_path)
    b0 = _commit_all(repo, seed, "seed")
    _git(repo, "checkout", "-q", "-b", "landing")
    for path in executable or []:
        (repo / path).chmod(0o755)
    t0 = _commit_all(repo, landing, "landing")
    _git(repo, "checkout", "-q", "main")
    b1 = _commit_all(repo, main, "main")
    _git(repo, "checkout", "-q", "landing")
    return _Landing(repo, b0, t0, b1)


_DOCS: "dict[str, str | bytes | None]" = {"docs/note.md": "a docs change\n"}
_LINES = "".join(f"line {n}\n" for n in range(1, 31))


def _patch_id(repo: Path, old: str, new: str) -> str:
    diff = _git(repo, "diff", old, new)
    out = subprocess.run(
        ["git", "patch-id", "--verbatim"],
        cwd=repo,
        input=diff,
        capture_output=True,
        text=True,
        check=True,
        env=git_env(repo),
    ).stdout
    return out.split()[0]


def _simple(tmp_path: Path) -> _Landing:
    """A one-line landing over a docs commit on main."""
    return _landing(tmp_path, {"src/m.py": "VALUE = 1\n"}, {"src/m.py": "VALUE = 2\n"}, _DOCS)


class TestReplay:
    """``replays_to``: the landing's patch applied onto a new base, in a throwaway index."""

    def test_a_clean_rebase_over_a_docs_commit_replays_to_the_rebased_tree(self, tmp_path):
        case = _simple(tmp_path)
        t1 = case.rebase()
        assert case.replay() == _tree(case.repo, t1)

    def test_an_amended_landing_replays_to_another_tree(self, tmp_path):
        case = _simple(tmp_path)
        case.rebase()
        t1 = case.amend("src/m.py", "VALUE = 3\n")
        replayed = case.replay()
        assert replayed is not None
        assert replayed != _tree(case.repo, t1)

    def test_a_whitespace_only_amendment_replays_to_another_tree(self, tmp_path):
        case = _simple(tmp_path)
        case.rebase()
        t1 = case.amend("src/m.py", "VALUE = 2 \n")
        replayed = case.replay()
        assert replayed is not None
        assert replayed != _tree(case.repo, t1)

    def test_the_same_edit_moved_to_an_identical_block_replays_to_another_tree(self, tmp_path):
        block = "a = 1\na = 1\na = 1\nx = 1\nb = 2\nb = 2\nb = 2\n"
        text = block + "# sep\n" * 4 + block + "c = 3\n"
        first = block.replace("x = 1", "x = 2") + "# sep\n" * 4 + block + "c = 3\n"
        second = block + "# sep\n" * 4 + block.replace("x = 1", "x = 2") + "c = 3\n"
        case = _landing(tmp_path, {"src/m.py": text}, {"src/m.py": first}, _DOCS)
        case.rebase()
        t1 = case.amend("src/m.py", second)
        # the collision the replay must still tell apart: patch-id calls the two diffs one patch
        assert _patch_id(case.repo, case.b0, case.t0) == _patch_id(case.repo, case.b1, t1)
        replayed = case.replay()
        assert replayed is not None
        assert replayed != _tree(case.repo, t1)

    def test_a_main_edit_in_the_same_file_above_the_hunk_replays(self, tmp_path):
        case = _landing(
            tmp_path,
            {"src/m.py": _LINES},
            {"src/m.py": _LINES.replace("line 20\n", "line twenty\n")},
            {"src/m.py": _LINES.replace("line 1\n", "line one\n")},
        )
        t1 = case.rebase()
        assert case.replay() == _tree(case.repo, t1)

    def test_a_main_edit_inside_the_hunks_context_returns_none(self, tmp_path):
        case = _landing(
            tmp_path,
            {"src/m.py": _LINES},
            {"src/m.py": _LINES.replace("line 10\n", "line ten\n")},
            {"src/m.py": _LINES.replace("line 8\n", "line eight\n")},
        )
        t1 = case.rebase()  # a clean 3-way merge: the rebase succeeds
        assert "line eight\nline 9\nline ten\n" in _git(case.repo, "show", f"{t1}:src/m.py")
        assert case.replay() is None  # git apply refuses: its context no longer matches

    def test_a_binary_change_a_mode_change_an_added_and_a_deleted_file_replay(self, tmp_path):
        case = _landing(
            tmp_path,
            {"blob.bin": b"\x00\x01\x02" * 50, "run.sh": "echo hi\n", "gone.txt": "bye\n"},
            {"blob.bin": b"\x00\xff\x02" * 60, "new.txt": "hello\n", "gone.txt": None},
            _DOCS,
            executable=["run.sh"],
        )
        t1 = case.rebase()
        assert _git(case.repo, "ls-tree", t1, "run.sh").startswith("100755")
        assert case.replay() == _tree(case.repo, t1)

    def test_a_deleted_file_main_also_edited_returns_none(self, tmp_path):
        # built without the rebase: it would conflict (modify/delete)
        case = _landing(
            tmp_path, {"src/m.py": "VALUE = 1\n"}, {"src/m.py": None}, {"src/m.py": "VALUE = 9\n"}
        )
        assert case.replay() is None

    def test_an_empty_diff_never_replays(self, tmp_path):
        case = _simple(tmp_path)
        assert gt.replays_to(case.repo, case.t0, _tree(case.repo, case.t0), case.b1) is None

    def test_an_unknown_object_returns_none_without_raising(self, tmp_path):
        case = _simple(tmp_path)
        tree0 = _tree(case.repo, case.t0)
        assert gt.replays_to(case.repo, "f" * 40, tree0, case.b1) is None
        assert gt.replays_to(case.repo, case.b0, tree0, "f" * 40) is None

    def test_a_spawn_failure_returns_none(self, tmp_path, monkeypatch):
        case = _simple(tmp_path)
        tree0 = _tree(case.repo, case.t0)

        def no_spawn(*_args, **_kwargs):
            raise OSError(7, "Argument list too long")

        monkeypatch.setattr(gate_stamp_common.subprocess, "run", no_spawn)
        assert gt.replays_to(case.repo, case.b0, tree0, case.b1) is None

    def test_the_replay_leaves_the_working_tree_and_the_real_index_untouched(self, tmp_path):
        case = _simple(tmp_path)
        index_tree = _git(case.repo, "write-tree").strip()
        assert _git(case.repo, "status", "--porcelain") == ""
        assert case.replay() is not None
        assert _git(case.repo, "status", "--porcelain") == ""
        assert _git(case.repo, "write-tree").strip() == index_tree

    def test_is_ancestor_distinguishes_no_from_unknown(self, tmp_path):
        repo = _fresh_repo(tmp_path)
        parent = _git(repo, "rev-parse", "HEAD").strip()
        child = _commit_file(repo, "src/m.py", "VALUE = 1\n", "child")
        assert gt.is_ancestor(repo, parent, child) is True
        assert gt.is_ancestor(repo, child, parent) is False
        assert gt.is_ancestor(repo, "f" * 40, child) is None

    def test_is_ancestor_is_none_when_git_cannot_be_spawned(self, tmp_path, monkeypatch):
        repo = _fresh_repo(tmp_path)
        parent = _git(repo, "rev-parse", "HEAD").strip()
        child = _commit_file(repo, "src/m.py", "VALUE = 1\n", "child")

        def no_spawn(*_args, **_kwargs):
            raise OSError(7, "Argument list too long")

        monkeypatch.setattr(gate_stamp_common.subprocess, "run", no_spawn)
        assert gt.is_ancestor(repo, parent, child) is None

    def test_a_crlf_line_replays_to_the_rebased_tree(self, tmp_path):
        case = _landing(
            tmp_path, {"src/m.py": b"VALUE = 1\n"}, {"src/m.py": b"VALUE = 2\r\n"}, _DOCS
        )
        t1 = case.rebase()
        assert _git(case.repo, "cat-file", "-s", f"{t1}:src/m.py").strip() == "11"  # CR kept
        assert case.replay() == _tree(case.repo, t1)

    def test_a_crlf_line_amended_to_lf_replays_to_another_tree(self, tmp_path):
        case = _landing(
            tmp_path, {"src/m.py": b"VALUE = 1\n"}, {"src/m.py": b"VALUE = 2\r\n"}, _DOCS
        )
        case.rebase()
        t1 = case.amend("src/m.py", b"VALUE = 2\n")
        replayed = case.replay()
        assert replayed is not None
        assert replayed != _tree(case.repo, t1)

    def test_a_non_utf8_line_amended_to_its_replaced_text_replays_to_another_tree(self, tmp_path):
        case = _landing(tmp_path, {"src/m.txt": b"cafe\n"}, {"src/m.txt": b"caf\xe9\n"}, _DOCS)
        case.rebase()
        t1 = case.amend("src/m.txt", "caf\N{REPLACEMENT CHARACTER}\n".encode())
        replayed = case.replay()
        assert replayed is not None
        assert replayed != _tree(case.repo, t1)

    def test_a_scratch_directory_failure_returns_none(self, tmp_path, monkeypatch):
        case = _simple(tmp_path)
        tree0 = _tree(case.repo, case.t0)

        def no_scratch(*_args, **_kwargs):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(gt.tempfile, "TemporaryDirectory", no_scratch)
        assert gt.replays_to(case.repo, case.b0, tree0, case.b1) is None


K = "make coverage-bed"


def _at(path: Path, written_at: str) -> Path:
    """Rewrite the store record at *path* as written at *written_at*; return *path*."""
    record = json.loads(path.read_text())
    record["written_at"] = written_at
    path.write_text(json.dumps(record))
    return path


def _stamp_at(root: Path, tree: str, base: str, key: str = K, written_at: str = "") -> Path:
    path = gt.write_stamp(root, tree=tree, key=key, base=base, measured={}, checkout="/x")
    return _at(path, written_at) if written_at else path


def _red_at(root: Path, tree: str, base: "str | None", written_at: str = "") -> Path:
    path = gt.write_red(root, tree=tree, key=K, checkout="/x", base=base)
    return _at(path, written_at) if written_at else path


def _when(n: int) -> str:
    return f"2026-11-02T10:{n:02d}:00Z"


def _rebase_again(case: _Rebased, change) -> "tuple[str, str]":
    """Land *change* on main (pushed), rebase ``landing`` onto it; return (B2, T2's tree)."""
    repo = case.repo
    _git(repo, "checkout", "-q", "main")
    change(repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "main change 2")
    b2 = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "push", "-q", "origin", "main")
    _git(repo, "checkout", "-q", "landing")
    _git(repo, "rebase", "-q", "main")
    return b2, tree_hash(repo)


def _second_docs_change(repo: Path) -> None:
    _write(repo, "docs/second.md", "another docs change\n")


def _other_patch_trees(repo: Path, base: str, n: int) -> "list[str]":
    """Return *n* trees, each a different edit of the landing's module over *base*."""
    trees = []
    for i in range(n):
        _git(repo, "checkout", "-q", "-B", f"other{i}", base)
        trees.append(
            _tree(repo, _commit_file(repo, "src/otto/host/unix.py", f"VALUE = {i + 10}\n", "o"))
        )
    _git(repo, "checkout", "-q", "landing")
    return trees


def _recorded_replays(monkeypatch) -> "list[tuple[str, str, str]]":
    """Record every (base0, tree0, base1) the carry asks ``replays_to`` about."""
    calls: "list[tuple[str, str, str]]" = []
    real = gt.replays_to

    def spy(repo, base0, tree0, base1):
        calls.append((base0, tree0, base1))
        return real(repo, base0, tree0, base1)

    monkeypatch.setattr(gt, "replays_to", spy)
    return calls


class TestCarry:
    """A bed stamp carried across a rebase: the patch replays and main's delta has no bed lane."""

    @staticmethod
    def _carry(case, tree=None, key=K, base=None):
        base = base or case.b1
        root = gt.stamps_root(case.repo, gt.STORE)
        ctx = gt.CarryContext(case.repo, base)
        return gt.lookup(case.repo, root, tree or case.t1, key, base=base, carry=ctx)

    def test_a_bed_stamp_carries_across_a_rebase_over_a_docs_commit(self, tmp_path):
        case = _rebased_landing(tmp_path, main_change=_docs_change)
        assert K in case.plan0.stamped
        assert K in case.plan1.stamped
        assert case.t0 != case.t1
        _stamp_at(gt.stamps_root(case.repo, gt.STORE), case.t0, case.b0)
        found = self._carry(case)
        assert found.stamp is not None
        assert found.stamp.tree == case.t0
        assert found.promotion == "carried"
        assert found.reason == ""

    def test_the_carry_never_fires_without_a_carry_context(self, tmp_path):
        case = _rebased_landing(tmp_path, main_change=_docs_change)
        root = gt.stamps_root(case.repo, gt.STORE)
        _stamp_at(root, case.t0, case.b0)
        assert self._carry(case).stamp is not None, "the control: with a context it carries"
        found = gt.lookup(case.repo, root, case.t1, K, base=case.b1)
        assert found.stamp is None
        assert found.promotion == ""
        assert gt.find_stamp(case.repo, root, case.t1, K, base=case.b1) is None

    def test_a_carry_context_for_another_base_is_refused(self, tmp_path):
        case = _rebased_landing(tmp_path, main_change=_docs_change)
        root = gt.stamps_root(case.repo, gt.STORE)
        ctx = gt.CarryContext(case.repo, case.b0)
        with pytest.raises(ValueError, match="is not the lookup's"):
            gt.lookup(case.repo, root, case.t1, K, base=case.b1, carry=ctx)

    def test_find_stamp_passes_the_carry_context_through(self, tmp_path):
        case = _rebased_landing(tmp_path, main_change=_docs_change)
        root = gt.stamps_root(case.repo, gt.STORE)
        _stamp_at(root, case.t0, case.b0)
        ctx = gt.CarryContext(case.repo, case.b1)
        found = gt.find_stamp(case.repo, root, case.t1, K, base=case.b1, carry=ctx)
        assert found is not None
        assert found.tree == case.t0

    def test_a_static_key_is_never_carried(self, tmp_path):
        case = _rebased_landing(tmp_path, main_change=_docs_change)
        root = gt.stamps_root(case.repo, gt.STORE)
        _stamp_at(root, case.t0, case.b0)
        _stamp_at(root, case.t0, case.b0, key="make check-python")
        assert self._carry(case).stamp is not None, "the control: the bed key carries"
        assert self._carry(case, key="make check-python").stamp is None

    def test_an_amended_landing_does_not_carry_and_the_reason_says_the_patch_differs(
        self, tmp_path
    ):
        case = _rebased_landing(
            tmp_path,
            main_change=_docs_change,
            amend=lambda repo: _write(repo, "src/otto/host/unix.py", "VALUE = 3\n"),
        )
        _stamp_at(gt.stamps_root(case.repo, gt.STORE), case.t0, case.b0)
        found = self._carry(case)
        assert found.stamp is None
        assert found.reason == f"the patch differs on {case.b1[:12]}"

    def test_a_main_delta_with_a_bed_lane_does_not_carry_and_the_reason_names_its_classes(
        self, tmp_path
    ):
        case = _rebased_landing(tmp_path, main_change=_src_change)
        delta = gs.plan(case.repo, case.b0, case.b1)
        assert delta.bed is True
        _stamp_at(gt.stamps_root(case.repo, gt.STORE), case.t0, case.b0)
        found = self._carry(case)
        assert found.stamp is None
        assert found.reason == (
            f"main's delta {case.b0[:12]}..{case.b1[:12]} reaches the bed: "
            + ", ".join(delta.classes)
        )
        assert "src-leaf" in found.reason

    def test_a_main_delta_of_a_hostless_test_module_carries(self, tmp_path):
        case = _rebased_landing(tmp_path, main_change=_hostless_test_change)
        assert gs.plan(case.repo, case.b0, case.b1).bed is False
        _stamp_at(gt.stamps_root(case.repo, gt.STORE), case.t0, case.b0)
        found = self._carry(case)
        assert found.stamp is not None
        assert found.stamp.tree == case.t0

    def test_a_main_delta_of_a_script_carries_unless_the_bed_half_runs_on_the_script(
        self, tmp_path
    ):
        def hostless_script(repo: Path) -> None:
            _write(repo, "scripts/gate_scope.py", "CHANGED = 1\n")

        def bed_script(repo: Path) -> None:
            _write(repo, "scripts/lab_health.py", "CHANGED = 1\n")

        for name in ("hostless", "bed"):
            (tmp_path / name).mkdir()
        case = _rebased_landing(tmp_path / "hostless", main_change=hostless_script)
        delta = gs.plan(case.repo, case.b0, case.b1)
        assert (delta.classes, delta.bed) == (["scripts"], False)
        _stamp_at(gt.stamps_root(case.repo, gt.STORE), case.t0, case.b0)
        assert self._carry(case).stamp is not None

        case = _rebased_landing(tmp_path / "bed", main_change=bed_script)
        delta = gs.plan(case.repo, case.b0, case.b1)
        assert (delta.classes, delta.bed) == (["scripts"], True)
        _stamp_at(gt.stamps_root(case.repo, gt.STORE), case.t0, case.b0)
        found = self._carry(case)
        assert found.stamp is None
        assert "reaches the bed: scripts" in found.reason

    def test_a_main_delta_editing_a_measured_matrix_carries(self, tmp_path):
        case = _rebased_landing(tmp_path, main_change=_matrix_change)
        delta = gs.plan(case.repo, case.b0, case.b1)
        assert delta.classes == ["tests-leaf"]
        assert delta.bed is False
        _stamp_at(gt.stamps_root(case.repo, gt.STORE), case.t0, case.b0)
        found = self._carry(case)
        assert found.stamp is not None
        assert found.stamp.tree == case.t0

    def test_a_candidate_whose_base_is_not_behind_the_new_base_is_skipped(self, tmp_path):
        case = _rebased_landing(tmp_path, main_change=_docs_change)
        _git(case.repo, "checkout", "-q", "-b", "side", case.b0)
        side = _commit_file(case.repo, "docs/side.md", "side\n", "side")
        _git(case.repo, "checkout", "-q", "landing")
        _stamp_at(gt.stamps_root(case.repo, gt.STORE), case.t0, side)
        found = self._carry(case)
        assert found.stamp is None
        assert found.reason == f"base {side[:12]} is not behind {case.b1[:12]}"

    def test_a_candidate_with_an_unknown_base_is_skipped_and_the_next_carries(self, tmp_path):
        case = _rebased_landing(tmp_path, main_change=_docs_change)
        root = gt.stamps_root(case.repo, gt.STORE)
        _stamp_at(root, _live_tree(case.repo, "unknown base"), "f" * 40, written_at=_when(2))
        _stamp_at(root, case.t0, case.b0, written_at=_when(1))
        found = self._carry(case)
        assert found.stamp is not None
        assert found.stamp.tree == case.t0

    @pytest.mark.parametrize("exc", [gs.GateScopeError, gt.UnknownGoalError, gt.GateError])
    def test_an_oracle_failure_counts_as_a_bed_lane_and_the_scan_continues(
        self, tmp_path, monkeypatch, exc
    ):
        case = _rebased_landing(tmp_path, main_change=_docs_change)
        b2, t2 = _rebase_again(case, _second_docs_change)
        root = gt.stamps_root(case.repo, gt.STORE)
        _stamp_at(root, case.t1, case.b1, written_at=_when(2))
        _stamp_at(root, case.t0, case.b0, written_at=_when(1))
        real = gs.plan
        failed: "list[str]" = []

        def plan(repo, base, tip, env=None):
            if base == case.b1:
                failed.append(base)
                raise exc("the oracle failed")
            return real(repo, base, tip, env)

        monkeypatch.setattr(gs, "plan", plan)
        found = self._carry(case, tree=t2, base=b2)
        assert failed == [case.b1], "the newest candidate's oracle ran, and failed"
        assert found.stamp is not None
        assert found.stamp.tree == case.t0
        assert found.promotion == "carried"

    def test_a_red_on_the_same_patch_after_a_rebase_blocks_the_carry(self, tmp_path):
        case = _rebased_landing(tmp_path, main_change=_docs_change)
        b2, t2 = _rebase_again(case, _second_docs_change)
        root = gt.stamps_root(case.repo, gt.STORE)
        _stamp_at(root, case.t0, case.b0)
        assert self._carry(case, tree=t2, base=b2).stamp is not None, "the control carries"
        _red_at(root, case.t1, case.b1)
        found = self._carry(case, tree=t2, base=b2)
        assert found.stamp is None
        assert found.reason == (
            f"the lane went red on this patch at tree {case.t1[:12]} after a rebase"
        )

    def test_the_ninth_red_marker_still_blocks(self, tmp_path, monkeypatch):
        case = _rebased_landing(tmp_path, main_change=_docs_change)
        b2, t2 = _rebase_again(case, _second_docs_change)
        root = gt.stamps_root(case.repo, gt.STORE)
        _stamp_at(root, case.t0, case.b0)
        _red_at(root, case.t1, case.b1, written_at=_when(0))  # the matching one, oldest
        others = _other_patch_trees(case.repo, case.b0, 8)
        for n, other in enumerate(others, start=1):
            _red_at(root, other, case.b0, written_at=_when(n))
        calls = _recorded_replays(monkeypatch)
        found = self._carry(case, tree=t2, base=b2)
        assert {(case.b0, other, b2) for other in others} <= set(calls), "every red was replayed"
        assert (case.b1, case.t1, b2) in calls, "the matching marker was visited"
        assert found.stamp is None
        assert found.reason == (
            f"the lane went red on this patch at tree {case.t1[:12]} after a rebase"
        )

    def test_a_red_marker_whose_base_is_unknown_refuses(self, tmp_path):
        case = _rebased_landing(tmp_path, main_change=_docs_change)
        root = gt.stamps_root(case.repo, gt.STORE)
        _stamp_at(root, case.t0, case.b0)
        _red_at(root, _live_tree(case.repo, "red"), "f" * 40)
        found = self._carry(case)
        assert found.stamp is None
        assert found.reason == f"a red marker's base {'f' * 12} cannot be placed; no carry"

    def test_a_red_marker_whose_tree_is_unknown_refuses(self, tmp_path):
        case = _rebased_landing(tmp_path, main_change=_docs_change)
        root = gt.stamps_root(case.repo, gt.STORE)
        _stamp_at(root, case.t0, case.b0)
        _red_at(root, "f" * 40, case.b0)
        found = self._carry(case)
        assert found.stamp is None
        assert found.reason == (
            f"a red marker for this key cannot be replayed (tree {'f' * 12}); no carry"
        )

    def test_a_red_marker_without_a_base_does_not_refuse(self, tmp_path):
        case = _rebased_landing(tmp_path, main_change=_docs_change)
        root = gt.stamps_root(case.repo, gt.STORE)
        _stamp_at(root, case.t0, case.b0)
        _red_at(root, _live_tree(case.repo, "old red"), None)
        found = self._carry(case)
        assert found.stamp is not None
        assert found.stamp.tree == case.t0

    def test_only_the_newest_eight_candidates_are_tried(self, tmp_path, monkeypatch):
        assert gt.CARRY_CAP == 8
        case = _rebased_landing(tmp_path, main_change=_docs_change)
        root = gt.stamps_root(case.repo, gt.STORE)
        _stamp_at(root, case.t0, case.b0, written_at=_when(0))  # the only one that replays
        others = _other_patch_trees(case.repo, case.b0, 8)
        for n, other in enumerate(others, start=1):
            _stamp_at(root, other, case.b0, written_at=_when(n))
        calls = _recorded_replays(monkeypatch)
        found = self._carry(case)
        assert found.stamp is None
        assert found.reason == f"the patch differs on {case.b1[:12]}"
        assert {tree0 for _b0, tree0, _b1 in calls} == set(others)

    def test_the_newest_eight_include_the_carrying_one(self, tmp_path):
        """The control: eight candidates in all, the carrying one the oldest, carry."""
        case = _rebased_landing(tmp_path, main_change=_docs_change)
        root = gt.stamps_root(case.repo, gt.STORE)
        _stamp_at(root, case.t0, case.b0, written_at=_when(0))
        for n, other in enumerate(_other_patch_trees(case.repo, case.b0, 7), start=1):
            _stamp_at(root, other, case.b0, written_at=_when(n))
        found = self._carry(case)
        assert found.stamp is not None
        assert found.stamp.tree == case.t0

    def test_the_plan_for_one_base_pair_is_computed_once(self, tmp_path, monkeypatch):
        case = _rebased_landing(tmp_path, main_change=_docs_change)
        root = gt.stamps_root(case.repo, gt.STORE)
        [other] = _other_patch_trees(case.repo, case.b0, 1)
        _stamp_at(root, case.t0, case.b0, written_at=_when(1))
        _stamp_at(root, other, case.b0, written_at=_when(2))
        replayed: "list[str]" = []

        def replays(repo, base0, tree0, base1):
            replayed.append(tree0)
            return case.t1

        planned: "list[tuple[str, str]]" = []
        bed_plan = gs.Plan(
            base_sha=case.b0,
            tip_sha=case.b1,
            tree="e" * 40,
            live=[],
            stamped=[K],
            bed=True,
            classes=["src-leaf"],
        )

        def plan(repo, base, tip, env=None):
            planned.append((base, tip))
            return bed_plan

        monkeypatch.setattr(gt, "replays_to", replays)
        monkeypatch.setattr(gs, "plan", plan)
        found = self._carry(case)
        assert replayed == [other, case.t0], "both candidates replayed, the newest first"
        assert planned == [(case.b0, case.b1)]
        assert found.stamp is None
        assert found.reason == (
            f"main's delta {case.b0[:12]}..{case.b1[:12]} reaches the bed: src-leaf"
        )

    def test_a_witnessed_promotion_across_bases_wins_before_the_carry(self, tmp_path, monkeypatch):
        case = _rebased_landing(tmp_path, main_change=_docs_change)
        root = gt.stamps_root(case.repo, gt.STORE)
        blob = _measure(case.repo, '{"as_of": "2026-10-20", "rows": [1]}\n')
        gt.write_stamp(
            root, tree=case.t1, key=K, base=case.b0, measured={SUPPORT: blob}, checkout="/x"
        )
        _git(case.repo, "commit", "-qam", "measured")
        measured_tree = tree_hash(case.repo)

        def never(*_args, **_kw):
            raise AssertionError("the carry ran")

        monkeypatch.setattr(gt, "replays_to", never)
        monkeypatch.setattr(gs, "plan", never)
        found = self._carry(case, tree=measured_tree)
        assert found.stamp is not None
        assert found.stamp.tree == case.t1
        assert found.promotion == "witnessed"


def _carried_run(tmp_path, monkeypatch=None, runner=None) -> "tuple[_Rebased, Path, FakeRunner]":
    """Build a landing rebased over a docs commit with K stamped at (T0, B0); return the parts."""
    case = _rebased_landing(tmp_path, main_change=_docs_change)
    root = gt.stamps_root(case.repo, gt.STORE)
    _stamp_at(root, case.t0, case.b0)
    return case, root, runner or FakeRunner()


def _carried_line(case: _Rebased) -> str:
    return (
        f"{K} … already stamped (promoted from {case.t0[:12]}: the patch replays onto "
        f"{case.b1[:12]}, main's delta {case.b0[:12]}..{case.b1[:12]} has no bed lane)"
    )


class TestCarryInTheRunner:
    """The runner scans for a carry with no lock held, then writes it after re-validating."""

    def test_gate_tip_after_a_rebase_carries_the_bed_stamp_and_prints_why(self, tmp_path):
        case, root, runner = _carried_run(tmp_path)
        opts = _options(case.repo, runner)
        assert gt.run(opts) == 0
        assert _carried_line(case) in opts.out.getvalue()
        assert K not in runner.keys, "the carried lane never ran"
        stamp = gt.exact_stamp(root, case.t1, K)
        assert stamp is not None
        assert stamp.promoted_from == case.t0
        assert stamp.base == case.b1

    def test_the_push_check_then_finds_the_carried_stamp_without_loading_the_store(
        self, tmp_path, monkeypatch
    ):
        case, _root, runner = _carried_run(tmp_path)
        assert gt.run(_options(case.repo, runner)) == 0

        def never(_root):
            raise AssertionError("the push check loaded the store")

        monkeypatch.setattr(gt, "load_store", never)
        head = _git(case.repo, "rev-parse", "HEAD").strip()
        opts = gt.Options(repo=case.repo, runner=FakeRunner(), out=io.StringIO())
        assert gt.pre_push(opts, _branch("refs/heads/landing", head)) == 0

    def test_a_marker_changed_between_the_scan_and_the_lock_abandons_the_carry(
        self, tmp_path, monkeypatch
    ):
        case, root, runner = _carried_run(tmp_path)
        other = _live_tree(case.repo, "other")
        marker = _red_at(root, other, None, written_at=_when(1))

        def rewrite():
            record = json.loads(marker.read_text())
            record["base"] = case.b0
            gate_stamp_common.write_atomically(marker, json.dumps(record))

        injected = _inject_on_promotion(monkeypatch, K, rewrite)
        opts = _options(case.repo, runner)
        assert gt.run(opts) == 0
        out = opts.out.getvalue()
        assert injected == [K]
        assert f"{K} … carry abandoned (a red marker changed during the scan)" in out
        assert _carried_line(case) not in out
        assert K in runner.keys, "the lane ran"
        stamp = gt.exact_stamp(root, case.t1, K)
        assert stamp is not None
        assert stamp.promoted_from is None

    def test_a_marker_added_between_the_scan_and_the_lock_abandons_the_carry(
        self, tmp_path, monkeypatch
    ):
        case, root, runner = _carried_run(tmp_path)
        other = _live_tree(case.repo, "other")
        injected = _inject_on_promotion(monkeypatch, K, lambda: _red_at(root, other, None))
        opts = _options(case.repo, runner)
        assert gt.run(opts) == 0
        assert injected == [K]
        assert f"{K} … carry abandoned (a red marker changed during the scan)" in (
            opts.out.getvalue()
        )
        assert K in runner.keys, "the lane ran"
        assert isinstance(gt.read_entry(gt.stamp_path(root, other, K)), gt.Red), "kept"

    def test_a_red_at_the_exact_path_under_the_lock_runs_the_lane(self, tmp_path, monkeypatch):
        case, root, _runner = _carried_run(tmp_path)
        runner = FakeRunner(statuses={K: 1})
        injected = _inject_on_promotion(monkeypatch, K, lambda: _red_at(root, case.t1, case.b1))
        opts = _options(case.repo, runner)
        assert gt.run(opts) == 1
        out = opts.out.getvalue()
        assert injected == [K]
        assert K in runner.keys, "the lane ran"
        assert "carry abandoned" not in out
        assert _carried_line(case) not in out
        assert isinstance(gt.read_entry(gt.stamp_path(root, case.t1, K)), gt.Red)

    def test_a_carried_stamp_lists_as_promoted_in_the_timings(self, tmp_path):
        case, root, runner = _carried_run(tmp_path)
        assert gt.run(_options(case.repo, runner)) == 0
        rows = [line for line in gt.timings(case.repo, root, "HEAD") if line.endswith(K)]
        assert len(rows) == 1
        assert "promoted" in rows[0]

    def test_an_unknown_promotion_line_raises(self, tmp_path):
        stamp = gt.read_stamp(_stamp_at(tmp_path / "store", "a" * 40, "b" * 40))
        assert stamp is not None
        with pytest.raises(ValueError, match="unknown promotion"):
            gt._promoted_line(stamp, "", "c" * 40)

    def test_the_witnessed_promotion_line_names_the_measured_outputs(self, tmp_path, monkeypatch):
        repo = _repo(tmp_path)
        s_tree = tree_hash(repo)
        blob = _measure(repo, '{"as_of": "2026-10-20"}\n')
        _stamp(repo, LANE, tree=s_tree, measured={SUPPORT: blob})
        _git(repo, "commit", "-qam", "measured")
        status, out, _ = _go(monkeypatch, repo, [LANE], FakeRunner())
        assert status == 0
        assert (
            f"{LANE} … already stamped (promoted from {s_tree[:12]}: measured outputs witnessed)"
            in out
        )

    def test_no_replay_or_plan_runs_under_any_lock(self, tmp_path, monkeypatch):
        case, root, runner = _carried_run(tmp_path)
        held: "list[bool]" = []
        asked: "list[str]" = []
        real_lock, real_replays, real_plan = gt.store_lock, gt.replays_to, gs.plan

        @contextlib.contextmanager
        def lock(at, **kw):
            with real_lock(at, **kw):
                held.append(True)
                try:
                    yield
                finally:
                    held.pop()

        def replays(*args):
            assert not held, "replays_to ran under the store lock"
            asked.append("replay")
            return real_replays(*args)

        def plan(*args):
            assert not held, "the oracle ran under the store lock"
            asked.append("plan")
            return real_plan(*args)

        monkeypatch.setattr(gt, "store_lock", lock)
        monkeypatch.setattr(gt, "replays_to", replays)
        monkeypatch.setattr(gs, "plan", plan)
        assert gt.run(_options(case.repo, runner)) == 0
        assert "replay" in asked, "the carry was asked"
        stamp = gt.exact_stamp(root, case.t1, K)
        assert stamp is not None
        assert stamp.promoted_from == case.t0


def _prune(repo: Path) -> None:
    """Expire every reflog of *repo* and prune what nothing else reaches, now."""
    _git(repo, "reflog", "expire", "--expire=now", "--all")
    _git(repo, "prune", "--expire=now")


def _red_with_patch(root: Path, repo: Path, tree: str, base: str, written_at: str = "") -> Path:
    """Write a red marker for (tree, K) against *base*, its patch sidecar beside it."""
    path = gt.write_red(root, tree=tree, key=K, checkout="/x", base=base, repo=repo)
    return _at(path, written_at) if written_at else path


def _pruned_red(tmp_path: Path, *, sidecar: bool) -> "tuple[_Rebased, Path, Path, str, str]":
    """A landing stamped at (T0, B0), red at (T1, B1), rebased again to (T2, B2), T1 pruned.

    T0 is kept by a scratch ref. Returns (case, root, the marker, B2, T2).
    """
    case = _rebased_landing(tmp_path, main_change=_docs_change)
    root = gt.stamps_root(case.repo, gt.STORE)
    _git(case.repo, "update-ref", "refs/keep/t0", case.plan0.tip_sha)
    _stamp_at(root, case.t0, case.b0)
    if sidecar:
        marker = _red_with_patch(root, case.repo, case.t1, case.b1)
        assert gt.sidecar(marker).is_file()
    else:
        marker = _red_at(root, case.t1, case.b1)
        assert not gt.sidecar(marker).exists()
    b2, t2 = _rebase_again(case, _second_docs_change)
    _prune(case.repo)
    assert gt._pruned(case.repo, case.t1), "the red tree is pruned"
    assert not gt._pruned(case.repo, case.t0), "the stamped tree is kept"
    return case, root, marker, b2, t2


class TestRedMarkerAfterItsTreeIsPruned:
    """A red marker with a base stays decidable, and on disk, after its tree is pruned."""

    @staticmethod
    def _carry(case, root, tree, base):
        return gt.lookup(
            case.repo, root, tree, K, base=base, carry=gt.CarryContext(case.repo, base)
        )

    def test_the_sidecar_decides_and_invalidate_keeps_the_marker(self, tmp_path):
        case, root, marker, b2, t2 = _pruned_red(tmp_path, sidecar=True)
        pruned_stamp = _stamp_at(root, "f" * 40, case.b0)
        refused = f"the lane went red on this patch at tree {case.t1[:12]} after a rebase"
        assert self._carry(case, root, t2, b2).reason == refused
        assert gt.invalidate(case.repo, root, K, tree=t2) == [pruned_stamp]
        assert gt.invalidate(case.repo, root, K, tree=_live_tree(case.repo, "other")) == []
        assert marker.is_file()
        assert gt.sidecar(marker).is_file()
        found = self._carry(case, root, t2, b2)
        assert found.stamp is None
        assert found.reason == refused

    def test_a_marker_without_a_sidecar_on_a_pruned_tree_cannot_be_replayed(self, tmp_path):
        case, root, marker, b2, t2 = _pruned_red(tmp_path, sidecar=False)
        refused = f"a red marker for this key cannot be replayed (tree {case.t1[:12]}); no carry"
        assert self._carry(case, root, t2, b2).reason == refused
        assert gt.invalidate(case.repo, root, K, tree=t2) == []
        assert marker.is_file()
        assert self._carry(case, root, t2, b2).reason == refused

    def test_a_marker_without_a_base_on_a_pruned_tree_is_deleted(self, tmp_path):
        root = _store(tmp_path)
        repo = _fresh_repo(tmp_path)
        marker = gt.write_red(root, tree="f" * 40, key=K, checkout="/x")
        assert gt.invalidate(repo, root, K, tree=tree_hash(repo)) == [marker]
        assert not marker.exists()

    def test_an_unrelated_patchs_marker_on_a_pruned_tree_does_not_refuse(self, tmp_path):
        case = _rebased_landing(tmp_path, main_change=_docs_change)
        root = gt.stamps_root(case.repo, gt.STORE)
        _git(case.repo, "update-ref", "refs/keep/t0", case.plan0.tip_sha)
        _stamp_at(root, case.t0, case.b0)
        (other,) = _other_patch_trees(case.repo, case.b0, 1)
        marker = _red_with_patch(root, case.repo, other, case.b0)
        _git(case.repo, "branch", "-q", "-D", "other0")
        _prune(case.repo)
        assert gt._pruned(case.repo, other)
        found = self._carry(case, root, case.t1, case.b1)
        assert found.stamp is not None
        assert found.stamp.tree == case.t0
        gt.sidecar(marker).unlink()
        assert self._carry(case, root, case.t1, case.b1).reason == (
            f"a red marker for this key cannot be replayed (tree {other[:12]}); no carry"
        ), "the control: without its sidecar the marker refuses"

    def test_a_green_lane_on_the_markers_tree_removes_the_marker_and_its_sidecar(self, tmp_path):
        case = _rebased_landing(tmp_path, main_change=_docs_change)
        root = gt.stamps_root(case.repo, gt.STORE)
        marker = _red_with_patch(root, case.repo, case.t1, case.b1)
        runner = FakeRunner()
        assert gt.run(_options(case.repo, runner)) == 0
        assert K in runner.keys, "the lane ran"
        assert isinstance(gt.read_entry(marker), gt.Stamp)
        assert not gt.sidecar(marker).exists()

    def test_the_sidecar_never_surfaces_in_the_store(self, tmp_path):
        case = _rebased_landing(tmp_path, main_change=_docs_change)
        root = gt.stamps_root(case.repo, gt.STORE)
        marker = _red_with_patch(root, case.repo, case.t1, case.b1)
        store = gt.load_store(root)
        assert [r.path for r in store.red] == [marker]
        assert store.unreadable == []
        assert list(gt.marker_snapshot(store, K)) == [marker]

    def test_a_red_then_pruned_then_interrupted_rerun_still_runs_the_lane(self, tmp_path):
        case = _rebased_landing(tmp_path, main_change=_docs_change)
        root = gt.stamps_root(case.repo, gt.STORE)
        _git(case.repo, "update-ref", "refs/keep/t0", case.plan0.tip_sha)
        _stamp_at(root, case.t0, case.b0)
        red = FakeRunner(statuses={K: 1})
        assert gt.run(_options(case.repo, red, force=True)) == 1
        marker = gt.stamp_path(root, case.t1, K)
        assert isinstance(gt.read_entry(marker), gt.Red)
        assert gt.sidecar(marker).is_file(), "the run wrote the patch beside the marker"
        _b2, t2 = _rebase_again(case, _second_docs_change)
        _prune(case.repo)
        assert gt._pruned(case.repo, case.t1)
        gt.invalidate(case.repo, root, K, tree=t2)  # an interrupted rerun: no result
        runner = FakeRunner()
        opts = _options(case.repo, runner)
        assert gt.run(opts) == 0
        assert K in runner.keys, "the lane ran: nothing carried"
        assert (
            f"{K} … no carry: the lane went red on this patch at tree {case.t1[:12]} after a rebase"
            in opts.out.getvalue()
        )
        assert isinstance(gt.read_entry(marker), gt.Red), "the old marker is still on disk"
        assert gt.sidecar(marker).is_file()
        assert gt.exact_stamp(root, t2, K) is not None


class TestCarryReasons:
    """A miss names the newest candidate's first failed condition; the runner says it."""

    def test_the_reason_is_the_newest_candidates(self, tmp_path):
        case = _rebased_landing(
            tmp_path,
            main_change=_docs_change,
            amend=lambda repo: _write(repo, "src/otto/host/unix.py", "VALUE = 3\n"),
        )
        root = gt.stamps_root(case.repo, gt.STORE)
        _git(case.repo, "checkout", "-q", "-b", "side", case.b0)
        side = _commit_file(case.repo, "docs/side.md", "side\n", "side")
        _git(case.repo, "checkout", "-q", "landing")
        _stamp_at(root, case.t0, case.b0, written_at=_when(1))  # older: the patch differs
        _stamp_at(root, _live_tree(case.repo, "x"), side, written_at=_when(2))  # newest
        ctx = gt.CarryContext(case.repo, case.b1)
        found = gt.lookup(case.repo, root, case.t1, K, base=case.b1, carry=ctx)
        assert found.stamp is None
        assert found.reason == f"base {side[:12]} is not behind {case.b1[:12]}"
        assert (case.b0, case.t0, case.b1) in ctx.replays, "the older candidate was tried too"

    def test_the_runner_says_why_nothing_carried_before_the_lane_runs(self, tmp_path):
        case = _rebased_landing(
            tmp_path,
            main_change=_docs_change,
            amend=lambda repo: _write(repo, "src/otto/host/unix.py", "VALUE = 3\n"),
        )
        _stamp_at(gt.stamps_root(case.repo, gt.STORE), case.t0, case.b0)
        runner = FakeRunner()
        opts = _options(case.repo, runner)
        assert gt.run(opts) == 0
        out = opts.out.getvalue()
        said = f"{K} … no carry: the patch differs on {case.b1[:12]}"
        assert said in out
        assert out.index(said) < out.index(f"{K} …\n"), "said before the lane runs"
        assert K in runner.keys


class TestRunnerReads:
    """What the runner reads per lane: an exact hit never loads the store; a dry run carries."""

    def test_an_exact_hit_never_loads_the_store(self, tmp_path, monkeypatch):
        case, _root, runner = _carried_run(tmp_path)
        assert gt.run(_options(case.repo, runner)) == 0

        def never(_root):
            raise AssertionError("the runner loaded the store for an exact hit")

        monkeypatch.setattr(gt, "load_store", never)
        rerun = FakeRunner()
        opts = _options(case.repo, rerun)
        assert gt.run(opts) == 0
        assert f"{K} … already stamped\n" in opts.out.getvalue()
        assert rerun.keys == []

    def test_a_dry_run_previews_a_carry_without_writing_it(self, tmp_path):
        case, root, runner = _carried_run(tmp_path)
        entries = lambda: {p: b for p, b in _snapshot(root).items() if p.name != ".lock"}  # noqa: E731
        before = entries()
        opts = _options(case.repo, runner, dry_run=True)
        gt.run(opts)
        assert f"{K} … would carry (promoted from {case.t0[:12]})" in opts.out.getvalue()
        assert entries() == before, "nothing written"
        assert runner.keys == []
