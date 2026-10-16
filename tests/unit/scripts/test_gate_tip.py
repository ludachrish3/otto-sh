"""Pins for scripts/gate_tip.py — the landing's gate: lane stamps, the runner and the push check."""

import fnmatch
import functools
import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import gate_fresh, gate_stamp_common
from scripts import gate_scope as gs
from scripts import gate_tip as gt
from scripts.gate_stamp_common import tree_hash
from tests._fixtures.gitrepo import git_env
from tests._fixtures.paths import PROJECT_ROOT
from tests.unit.scripts.test_gate_fresh import _published, _stamp_tip
from tests.unit.scripts.test_gate_scope import _head_paths
from tests.unit.test_tier_marker_invariants import _makefile_recipes

pytestmark = pytest.mark.interpreter_agnostic

MAKEFILE = (PROJECT_ROOT / "Makefile").read_text()


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True, env=git_env(repo)
    ).stdout


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
    return repo


def _store(tmp_path: Path) -> Path:
    return tmp_path / "store"


def _measure(repo: Path, text: str) -> str:
    """Rewrite the support matrix as a lane would and return its blob."""
    (repo / "schemas" / "support_matrix.json").write_text(text)
    return _git(repo, "hash-object", "schemas/support_matrix.json").strip()


class TestGoals:
    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            ("make coverage", ["coverage"]),
            ("make check-ts coverage-ts", ["check-ts", "coverage-ts"]),
            ('make quick PATHS="tests/unit/x.py tests/unit/y.py"', ["quick"]),
            ("make check-breaking RANGE=abc..HEAD", ["check-breaking"]),
            ("nox -s tests_hostless-3.14", ["tests_hostless-3.14"]),
            ("uv run ruff check tests/unit/x.py", ["ruff"]),
        ],
    )
    def test_goals_of_each_shape(self, line, expected):
        assert gt.goals(line) == expected

    def test_a_commit_only_goal_line_is_an_unknown_goal(self):
        with pytest.raises(gt.UnknownGoalError, match="ty check"):
            gt.goals("uv run ty check --extra-search-path . scripts/x.py")

    def test_an_unknown_shape_or_goal_is_refused(self):
        with pytest.raises(gt.UnknownGoalError):
            gt.goals("python3 -m something")
        with pytest.raises(gt.UnknownGoalError):
            gt.is_live("make frobnicate")

    def test_live_lanes_are_range_targets_plus_docs(self):
        assert set(gt.LIVE_GOALS) == set(gs.RANGE_TARGETS) | {"docs"}
        assert not set(gt.LIVE_GOALS) & set(gt.STAMPED_GOALS)
        assert gt.is_live("make check-python RANGE=abc..HEAD")
        assert not gt.is_live("make coverage")


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

    def test_a_malformed_stamp_is_unreadable_not_evidence(self, tmp_path):
        root = _store(tmp_path)
        path = gt.stamp_path(root, "t" * 40, "make coverage")
        path.parent.mkdir(parents=True)
        good = {
            "format": 1,
            "tree": "t" * 40,
            "command": "make coverage",
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
        assert gt.lookup(repo, root, "t" * 40, "make coverage").reason == "unreadable stamp"
        store = gt.load_store(root)
        assert store.stamps == []
        assert store.unreadable == [path]

    def test_an_unreadable_file_under_another_tree_is_named_not_no_stamp(self, tmp_path):
        repo, root = _repo(tmp_path), _store(tmp_path)
        other = gt.stamp_path(root, "o" * 40, "make coverage")
        other.parent.mkdir(parents=True)
        other.write_text("{not json")
        assert gt.lookup(repo, root, tree_hash(repo), "make coverage").reason == "unreadable stamp"

    def test_a_corrupt_exact_file_does_not_hide_valid_promoted_evidence(self, tmp_path):
        repo, root = _repo(tmp_path), _store(tmp_path)
        s_tree = tree_hash(repo)
        gt.write_stamp(
            root, tree=s_tree, key="make coverage", base="b", measured={}, checkout=str(repo)
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
        exact = gt.stamp_path(root, t_tree, "make coverage")
        exact.parent.mkdir(parents=True)
        exact.write_text("{crashed mid-write")
        found = gt.lookup(repo, root, t_tree, "make coverage")
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
            root, tree=t_tree, key="make coverage", base="b", measured={}, checkout="/x"
        )
        data = json.loads(path.read_text())
        data["tree"] = s_tree
        path.write_text(json.dumps(data))
        assert gt.find_stamp(repo, root, t_tree, "make coverage") is None
        assert path in gt.load_store(root).unreadable

    def test_a_stamp_whose_command_is_not_the_key_is_a_mismatch(self, tmp_path):
        repo, root = _repo(tmp_path), _store(tmp_path)
        path = gt.write_stamp(
            root, tree="t" * 40, key="make coverage", base="b", measured={}, checkout="/x"
        )
        data = json.loads(path.read_text())
        data["command"] = "make kmodcov"  # a digest collision, or a hand edit
        path.write_text(json.dumps(data))
        found = gt.lookup(repo, root, "t" * 40, "make coverage")
        assert found.stamp is None
        assert found.reason.startswith("command mismatch")
        store = gt.load_store(root)
        assert store.stamps == []
        assert store.mismatched == [gt.Mismatch(path, "make kmodcov")]
        assert gt.find_stamp(repo, root, "t" * 40, "make kmodcov") is None, (
            "never evidence for the claimed key either"
        )
        assert gt.invalidate(root, "make coverage") == [path]
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
            root, tree=s_tree, key="make coverage", base="b", measured={}, checkout=str(repo)
        )
        _git(repo, "commit", "-qam", "measured")
        t_tree = tree_hash(repo)
        assert gt.find_stamp(repo, root, t_tree, "make kmodcov") is None
        assert gt.find_stamp(repo, root, t_tree, "make coverage") is None, (
            "the misnamed file's blob is no witness"
        )

    def test_exact_evidence_wins_without_a_store_scan(self, tmp_path, monkeypatch):
        repo, root = _repo(tmp_path), _store(tmp_path)
        s_tree = tree_hash(repo)
        # a REAL promotable candidate: S's coverage stamp plus S's measurement witness for T
        gt.write_stamp(
            root, tree=s_tree, key="make coverage", base="b", measured={}, checkout="/elsewhere"
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
        assert gt.find_stamp(repo, root, t_tree, "make coverage").checkout == "/elsewhere", (
            "promotable on its own"
        )
        gt.write_stamp(
            root, tree=t_tree, key="make coverage", base="b", measured={}, checkout=str(repo)
        )
        reads = []
        real = gt.read_stamp
        monkeypatch.setattr(gt, "read_stamp", lambda p: (reads.append(p), real(p))[1])
        found = gt.lookup(repo, root, t_tree, "make coverage")
        assert found.stamp is not None
        assert found.stamp.tree == t_tree
        assert found.stamp.checkout == str(repo)
        assert found.store is None, "no store was loaded"
        assert reads == [gt.stamp_path(root, t_tree, "make coverage")], "one read, no scan"

    def test_a_genuine_file_name_collision_is_a_command_mismatch(self, tmp_path, monkeypatch):
        repo, root = _repo(tmp_path), _store(tmp_path)
        monkeypatch.setattr(gt, "slug", lambda key: "collide")  # two known keys, one file name
        tree = tree_hash(repo)
        gt.write_stamp(
            root, tree=tree, key="make kmodcov", base="b", measured={}, checkout=str(repo)
        )
        found = gt.lookup(repo, root, tree, "make coverage")
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
            root, tree="t" * 40, key="make coverage", base="b", measured={}, checkout="/x"
        )
        data = json.loads(path.read_text())
        data["tree"] = "u" * 40
        path.write_text(json.dumps(data))
        assert gt.find_stamp(repo, root, "t" * 40, "make coverage") is None


class TestSatisfies:
    def test_the_exact_tree(self, tmp_path):
        repo, root = _repo(tmp_path), _store(tmp_path)
        tree = tree_hash(repo)
        gt.write_stamp(
            root, tree=tree, key="make coverage", base="b", measured={}, checkout=str(repo)
        )
        assert gt.find_stamp(repo, root, tree, "make coverage") is not None

    def test_a_tree_that_differs_only_by_a_recorded_measured_blob(self, tmp_path):
        repo, root = _repo(tmp_path), _store(tmp_path)
        s_tree = tree_hash(repo)
        gt.write_stamp(
            root, tree=s_tree, key="make coverage", base="b", measured={}, checkout=str(repo)
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
        coverage = gt.find_stamp(repo, root, t_tree, "make coverage")
        assert coverage is not None, "promoted through the measuring stamp"
        assert coverage.tree == s_tree
        assert gt.find_stamp(repo, root, t_tree, "make conformance-bed") is not None

    def test_a_recorded_blob_followed_by_different_bytes_inherits_nothing(self, tmp_path):
        repo, root = _repo(tmp_path), _store(tmp_path)
        s_tree = tree_hash(repo)
        gt.write_stamp(
            root, tree=s_tree, key="make coverage", base="b", measured={}, checkout=str(repo)
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
        found = gt.lookup(repo, root, tree_hash(repo), "make coverage")
        assert found.stamp is None
        assert "beyond the measured outputs" in found.reason
        assert s_tree[:12] in found.reason

    def test_a_matrix_only_commit_with_no_run_behind_it_inherits_nothing(self, tmp_path):
        repo, root = _repo(tmp_path), _store(tmp_path)
        gt.write_stamp(
            root,
            tree=tree_hash(repo),
            key="make coverage",
            base="b",
            measured={},
            checkout=str(repo),
        )
        _measure(repo, '{"as_of": "hand-edited"}\n')
        _git(repo, "commit", "-qam", "edit")
        assert gt.find_stamp(repo, root, tree_hash(repo), "make coverage") is None

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
            root, tree=s_tree, key="make coverage", base="b", measured={}, checkout=str(repo)
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
        assert gt.find_stamp(repo, root, tree_hash(repo), "make coverage") is None

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
            root, tree="e" * 40, key="make coverage", base="b", measured={}, checkout=str(repo)
        )
        assert gt.lookup(repo, root, tree_hash(repo), "make coverage").reason == "unreadable stamp"

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
        found = gt.lookup(repo, root, tree_hash(repo), "make coverage", store=given)
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
                root, tree=tree, key="make coverage", base="b", measured={}, checkout="/x"
            )
        reads = []
        real = gt.read_stamp
        monkeypatch.setattr(gt, "read_stamp", lambda p: (reads.append(p), real(p))[1])
        gt.lookup(repo, root, tree_hash(repo), "make coverage")
        assert len(reads) == 1 + 6, "the exact probe, then one read per stamp file, no re-reads"


class TestInvalidate:
    def test_an_unreadable_file_at_the_keys_name_goes_too(self, tmp_path):
        root = _store(tmp_path)
        path = gt.stamp_path(root, "z" * 40, "make coverage")
        path.parent.mkdir(parents=True)
        path.write_text("{bad")
        assert gt.invalidate(root, "make coverage") == [path]
        assert not path.exists()

    def test_every_stamp_for_the_key_under_every_tree_goes(self, tmp_path):
        root = _store(tmp_path)
        for tree in ("a" * 40, "b" * 40):
            gt.write_stamp(
                root, tree=tree, key="make coverage", base="b", measured={}, checkout="/x"
            )
            gt.write_stamp(
                root, tree=tree, key="make kmodcov", base="b", measured={}, checkout="/x"
            )
        gone = gt.invalidate(root, "make coverage")
        assert len(gone) == 2
        assert not any(p.exists() for p in gone)
        assert gt.read_stamp(gt.stamp_path(root, "a" * 40, "make kmodcov")) is not None


class FakeRunner:
    """Records every (cmd, cwd, env); answers from a table; runs side effects for a lane."""

    def __init__(self, statuses=None, outputs=None, effects=None, checker_statuses=None):
        self.calls = []  # (cmd as given, cwd, env)
        self.statuses = dict(statuses or {})  # str key -> status; default 0
        self.outputs = dict(outputs or {})  # str key -> output text (argv calls)
        self.effects = dict(effects or {})  # str key -> callable() run before answering
        self.checker_statuses = list(checker_statuses or [])  # queue for the coverage checker

    @staticmethod
    def key(cmd):
        return cmd if isinstance(cmd, str) else " ".join(cmd)

    def __call__(self, cmd, cwd, env):
        key = self.key(cmd)
        self.calls.append((cmd, cwd, dict(env)))
        if key in self.effects:
            self.effects[key]()
        if (
            key.startswith("uv run python scripts/coverage_stamp.py check")
            and self.checker_statuses
        ):
            return gt.RunOutcome(self.checker_statuses.pop(0), self.outputs.get("checker", ""))
        return gt.RunOutcome(self.statuses.get(key, 0), self.outputs.get(key, ""))

    @property
    def keys(self):
        return [self.key(c) for c, _, _ in self.calls]


GATE_LINES = (
    "--threshold\n95.5\n--selection\nnot stability and not browser\n"
    "--browsers\nchromium\n--primary\n3.10\n"
)
CHECKER = [
    "uv",
    "run",
    "python",
    "scripts/coverage_stamp.py",
    "check",
    "--threshold",
    "95.5",
    "--selection",
    "not stability and not browser",
    "--browsers",
    "chromium",
    "--primary",
    "3.10",
]
GATE_ARGS = "make -s coverage-gate-args"
NOX = "nox -s tests_hostless-3.14"
SUPPORT = "schemas/support_matrix.json"


def _options(repo, runner, **kw):
    return gt.Options(repo=repo, runner=runner, out=io.StringIO(), **kw)


def _oracle(monkeypatch, repo, lines):
    base = _git(repo, "rev-parse", "HEAD").strip()
    monkeypatch.setattr(gt, "_tip_list", lambda _repo, _base: (base, list(lines)))
    return base


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


class TestRunner:
    def test_the_runner_executes_the_stamped_lines_in_order_and_never_a_live_one(
        self, tmp_path, monkeypatch
    ):
        repo = _repo(tmp_path)
        for name in ("MAKEFLAGS", "MFLAGS", "PYTEST_ADDOPTS", "BASE"):
            monkeypatch.setenv(name, "x")
        lines = [
            "make check-breaking RANGE=x..HEAD",
            "make coverage",
            NOX,
            "make docs",
            "make gate-fresh RANGE=x..HEAD",
        ]
        runner = FakeRunner(outputs={GATE_ARGS: GATE_LINES}, checker_statuses=[1, 0])
        status, out, _ = _go(monkeypatch, repo, lines, runner)
        assert runner.keys == [
            GATE_ARGS,
            " ".join(CHECKER),
            "make coverage",
            " ".join(CHECKER),
            NOX,
        ]
        assert runner.calls[1][0] == CHECKER, "the argv as a list, no --overridden"
        for _cmd, _cwd, env in runner.calls:
            assert not {"MAKEFLAGS", "MFLAGS", "PYTEST_ADDOPTS", "BASE"} & set(env)
        root, tree = gt.stamps_root(repo, gt.STORE), tree_hash(repo)
        assert gt.exact_stamp(root, tree, "make coverage") is not None
        assert gt.exact_stamp(root, tree, NOX) is not None
        assert out.rstrip().endswith(
            "gate-tip: 2 stamped, 0 already stamped, 3 live at push, 0 red"
        )
        assert status == 0

    def test_an_overridden_variable_refuses_before_any_lane(self, tmp_path, monkeypatch):
        repo, runner = _repo(tmp_path), FakeRunner()
        status, out, _ = _go(monkeypatch, repo, [NOX], runner, overridden=["BUMP"])
        assert status == 2
        assert runner.calls == []
        assert "came from the command line" in out

    def test_dirt_refuses_before_any_lane(self, tmp_path, monkeypatch):
        repo, runner = _repo(tmp_path), FakeRunner()
        test_a = repo / "tests" / "unit" / "test_a.py"
        test_a.write_text("def test_a():\n    assert True\n")
        status, out, _ = _go(monkeypatch, repo, [NOX], runner)
        assert (status, runner.calls) == (2, [])
        assert "modified or staged" in out
        _git(repo, "checkout", "--", "tests/unit/test_a.py")
        (repo / "tests" / "unit" / "test_new.py").write_text("def test_n():\n    pass\n")
        status, out, _ = _go(monkeypatch, repo, [NOX], runner)
        assert (status, runner.calls) == (2, [])
        (repo / "tests" / "unit" / "test_new.py").unlink()
        (repo / "note.md").write_text("scratch\n")
        status, _out, _ = _go(monkeypatch, repo, [NOX], runner)
        assert status == 0
        assert runner.keys == [NOX]

    def test_dirt_is_rechecked_before_every_lane(self, tmp_path, monkeypatch):
        repo = _repo(tmp_path)
        lines = [NOX, "make quick-guards"]

        def untracked():
            (repo / "tests" / "unit" / "test_new.py").write_text("def test_n():\n    pass\n")

        runner = FakeRunner(effects={NOX: untracked})
        status, out, _ = _go(monkeypatch, repo, lines, runner)
        assert status == 1
        assert runner.keys == [NOX]
        assert "NOT STAMPED" in out
        (repo / "tests" / "unit" / "test_new.py").unlink()

        def tracked():
            (repo / "tests" / "unit" / "test_a.py").write_text("def test_a():\n    assert True\n")

        runner = FakeRunner(effects={NOX: tracked})
        status, out, _ = _go(monkeypatch, repo, lines, runner)
        assert status == 1
        assert runner.keys == [NOX]
        assert "NOT STAMPED" in out

    def test_the_first_red_lane_stops_the_run_and_leaves_no_evidence_for_its_key(
        self, tmp_path, monkeypatch
    ):
        repo = _repo(tmp_path)
        root = gt.stamps_root(repo, gt.STORE)
        _stamp(repo, "make coverage", tree="a" * 40)
        _stamp(repo, "make coverage", tree="b" * 40, promoted_from="a" * 40)
        lines = [NOX, "make coverage", "make quick-guards"]
        runner = FakeRunner(
            outputs={GATE_ARGS: GATE_LINES}, checker_statuses=[0], statuses={"make coverage": 1}
        )
        status, out, _ = _go(monkeypatch, repo, lines, runner, force=True)
        assert status == 1
        assert not [s for s in gt.load_store(root).stamps if s.command == "make coverage"]
        assert "make quick-guards" not in runner.keys
        assert gt.exact_stamp(root, tree_hash(repo), NOX) is not None
        assert "RED (exit 1" in out
        assert out.rstrip().endswith("1 stamped, 0 already stamped, 0 live at push, 1 red")

        def interrupt():
            raise KeyboardInterrupt

        runner = FakeRunner(effects={NOX: interrupt})
        _oracle(monkeypatch, repo, [NOX])
        with pytest.raises(KeyboardInterrupt):
            gt.run(_options(repo, runner, force=True))
        assert gt.exact_stamp(root, tree_hash(repo), NOX) is None

    def test_the_lineage_sequence(self, tmp_path, monkeypatch):
        repo = _repo(tmp_path)
        root = gt.stamps_root(repo, gt.STORE)
        s_tree = tree_hash(repo)
        b1 = _measure(repo, '{"as_of": "2026-10-16"}\n')
        _stamp(repo, NOX)
        _stamp(repo, "make kmodcov", measured={SUPPORT: b1})
        _git(repo, "commit", "-qam", "measured")
        t_sha = _git(repo, "rev-parse", "HEAD").strip()
        t_tree = tree_hash(repo)
        bed = "make conformance-bed"
        runner = FakeRunner(
            effects={bed: lambda: _measure(repo, '{"as_of": "2026-10-17"}\n')},
        )
        status, _out, _ = _go(monkeypatch, repo, [NOX, bed], runner)
        assert status == 0
        assert runner.keys == [bed], "nox is satisfied through the measuring stamp's witness"
        promoted = gt.exact_stamp(root, t_tree, NOX)
        assert promoted is not None
        assert promoted.promoted_from == s_tree
        bed_stamp = gt.exact_stamp(root, t_tree, bed)
        assert bed_stamp is not None
        assert list(bed_stamp.measured) == [SUPPORT]
        assert bed_stamp.measured[SUPPORT] != b1
        _git(repo, "commit", "-qam", "measured again")
        u_tree = tree_hash(repo)

        runner = FakeRunner(statuses={NOX: 1})
        status, _out, _ = _go(monkeypatch, repo, [NOX], runner, force=True)
        assert status == 1
        assert not [s for s in gt.load_store(root).stamps if s.command == NOX]

        _git(repo, "checkout", "-q", t_sha)
        runner = FakeRunner(statuses={NOX: 1})
        status, _out, _ = _go(monkeypatch, repo, [NOX], runner)
        assert status == 1
        assert runner.keys == [NOX], "no skip: the red run left no evidence"
        assert gt.find_stamp(repo, root, u_tree, NOX) is None

        runner = FakeRunner()
        status, _out, _ = _go(monkeypatch, repo, [NOX], runner)
        assert status == 0
        assert gt.find_stamp(repo, root, u_tree, NOX) is not None, "through the bed's witness"

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

    def test_force_runs_coverage_even_with_a_valid_lane_stamp_and_a_valid_coverage_stamp(
        self, tmp_path, monkeypatch
    ):
        repo = _repo(tmp_path)
        root = gt.stamps_root(repo, gt.STORE)
        _stamp(repo, "make coverage", checkout="/old")
        runner = FakeRunner(outputs={GATE_ARGS: GATE_LINES}, checker_statuses=[0, 0])
        status, _out, _ = _go(monkeypatch, repo, ["make coverage"], runner, force=True)
        assert status == 0
        assert runner.keys == [GATE_ARGS, " ".join(CHECKER), "make coverage", " ".join(CHECKER)]
        stamp = gt.exact_stamp(root, tree_hash(repo), "make coverage")
        assert stamp is not None
        assert stamp.checkout == str(repo)
        runner = FakeRunner()
        status, out, _ = _go(monkeypatch, repo, ["make coverage"], runner)
        assert (status, runner.calls) == (0, [])
        assert "already stamped" in out

    def test_a_valid_coverage_stamp_stamps_the_lane_without_running_coverage(
        self, tmp_path, monkeypatch
    ):
        repo = _repo(tmp_path)
        runner = FakeRunner(outputs={GATE_ARGS: GATE_LINES}, checker_statuses=[0])
        status, out, _ = _go(monkeypatch, repo, ["make coverage"], runner)
        assert runner.keys == [GATE_ARGS, " ".join(CHECKER)]
        root = gt.stamps_root(repo, gt.STORE)
        assert gt.exact_stamp(root, tree_hash(repo), "make coverage") is not None
        assert "1 stamped," in out
        assert status == 0

    def test_a_checker_2_is_red_before_coverage_runs(self, tmp_path, monkeypatch):
        repo = _repo(tmp_path)
        runner = FakeRunner(outputs={GATE_ARGS: GATE_LINES}, checker_statuses=[2])
        status, out, _ = _go(monkeypatch, repo, ["make coverage"], runner)
        assert "make coverage" not in runner.keys
        assert "RED (exit 2" in out
        assert status == 1

    def test_a_green_coverage_with_a_failing_check_is_red_with_the_output_shown(
        self, tmp_path, monkeypatch
    ):
        repo = _repo(tmp_path)
        runner = FakeRunner(
            outputs={GATE_ARGS: GATE_LINES, "checker": "coverage-stamp: no stamp for tree ...\n"},
            checker_statuses=[1, 1],
        )
        status, out, _ = _go(monkeypatch, repo, ["make coverage"], runner)
        assert status == 1
        assert "coverage-stamp: no stamp for tree" in out
        assert runner.keys[-1] == " ".join(CHECKER)

    def test_the_real_runner_returns_merged_stderr(self, tmp_path):
        code = "import sys; sys.stderr.write('only stderr')"
        result = gt._default_runner([sys.executable, "-c", code], tmp_path, dict(os.environ))
        assert result == gt.RunOutcome(0, "only stderr")
        assert gt._default_runner("exit 3", tmp_path, dict(os.environ)) == gt.RunOutcome(3, "")

    @pytest.mark.parametrize(
        "text",
        [
            (
                "--threshold\n95.5\n--selection\nnot stability\nand not browser\n"
                "--browsers\nchromium\n--primary\n3.10\n"
            ),
            (
                "--threshold\n95.5\n--selection\nnot stability\n--threshold\n0\n"
                "--browsers\nchromium\n--primary\n3.10\n"
            ),
            "",
            "--selection\nx\n--threshold\n1\n--browsers\nc\n--primary\n3\n",
        ],
    )
    def test_coverage_gate_framing_rejects_what_is_not_the_declared_sequence(
        self, tmp_path, monkeypatch, text
    ):
        repo = _repo(tmp_path)
        runner = FakeRunner(outputs={GATE_ARGS: text}, checker_statuses=[0])
        status, out, _ = _go(monkeypatch, repo, ["make coverage"], runner)
        assert status == 1
        assert "cannot frame" in out
        assert runner.keys == [GATE_ARGS]

    def test_a_failing_gate_args_query_counts_red_and_the_summary_prints(
        self, tmp_path, monkeypatch
    ):
        repo = _repo(tmp_path)
        runner = FakeRunner(statuses={GATE_ARGS: 1}, outputs={GATE_ARGS: "boom"})
        status, out, _ = _go(monkeypatch, repo, ["make coverage"], runner)
        assert status == 1
        assert "make -s coverage-gate-args failed" in out
        assert "0 stamped, 0 already stamped, 0 live at push, 1 red" in out
        assert runner.keys == [GATE_ARGS]

    def test_an_empty_value_frames_fine(self, tmp_path, monkeypatch):
        repo = _repo(tmp_path)
        text = "--threshold\n95.5\n--selection\nsel\n--browsers\n\n--primary\n3.10\n"
        runner = FakeRunner(outputs={GATE_ARGS: text}, checker_statuses=[0])
        status, _out, _ = _go(monkeypatch, repo, ["make coverage"], runner)
        assert status == 0
        assert runner.calls[1][0][-4:] == ["--browsers", "", "--primary", "3.10"]

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

        runner = FakeRunner(effects={NOX: commit})
        status, out, _ = _go(monkeypatch, repo, [NOX], runner)
        assert status == 1
        assert "NOT STAMPED" in out
        assert out.rstrip().endswith("1 red")

    def test_dry_run_lists_runs_nothing_and_writes_nothing(self, tmp_path, monkeypatch):
        repo = _repo(tmp_path)
        root = gt.stamps_root(repo, gt.STORE)
        b1 = _measure(repo, '{"as_of": "2026-10-16"}\n')
        _stamp(repo, NOX)
        _stamp(repo, "make kmodcov", measured={SUPPORT: b1})
        _git(repo, "commit", "-qam", "measured")
        before = _snapshot(root)
        runner = FakeRunner()
        lines = [NOX, "make quick-guards"]
        status, out, _ = _go(monkeypatch, repo, lines, runner, dry_run=True)
        assert status == 1
        assert runner.calls == []
        assert f"[1/2] {NOX} … already stamped" in out
        assert "[2/2] make quick-guards … missing" in out
        assert _snapshot(root) == before
        status, _out, _ = _go(monkeypatch, repo, [NOX], FakeRunner(), dry_run=True)
        assert status == 0
        assert _snapshot(root) == before

    def test_an_unknown_goal_refuses_the_whole_run(self, tmp_path, monkeypatch):
        repo, runner = _repo(tmp_path), FakeRunner()
        base = _git(repo, "rev-parse", "HEAD").strip()
        monkeypatch.setattr(gt, "_tip_list", lambda _r, _b: (base, [NOX, "make frobnicate"]))
        opts = _options(repo, runner)
        assert gt.run(opts) == 2
        assert runner.calls == []

    def test_a_stamp_written_in_one_worktree_is_honoured_from_another(self, tmp_path):
        repo = _repo(tmp_path)
        other = tmp_path / "other"
        _git(repo, "worktree", "add", "-q", "--detach", str(other), "HEAD")
        _stamp(repo, NOX)
        root = gt.stamps_root(other, gt.STORE)
        assert root == gt.stamps_root(repo, gt.STORE)
        assert gt.find_stamp(other, root, tree_hash(other), NOX) is not None

    def test_the_cli_runs_main(self, tmp_path):
        repo = _repo(tmp_path)
        _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        completed = subprocess.run(
            [
                sys.executable,
                str(PROJECT_ROOT / "scripts/gate_tip.py"),
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
        file = PROJECT_ROOT / path
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
        (f"repeat {glob}", scope(next(p for p in sorted(head) if fnmatch.fnmatch(p, glob))))
        for glob in gs.REPEAT_TRIGGERS
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
                for goal in gt.goals(line):
                    assert (goal in gt.LIVE_GOALS) != (goal in gt.STAMPED_GOALS), (name, line)
                    seen.add(goal)
                gt.is_live(line)
    return seen


class TestCensus:
    def test_every_tip_line_the_oracle_can_emit_has_exactly_one_kind(self):
        seen = _census()
        assert {"coverage", "conformance-bed", "kmodcov", "dashboard", "quick-guards"} <= seen
        assert "check-breaking" in seen
        assert seen - set(gt.LIVE_GOALS) == set(gt.STAMPED_GOALS)

    @pytest.mark.parametrize("goal", ["conformance-bed", "kmodcov", "dashboard", "quick-guards"])
    def test_a_stamped_goal_dropped_from_the_set_fails_the_census(self, monkeypatch, goal):
        monkeypatch.setattr(gt, "STAMPED_GOALS", [g for g in gt.STAMPED_GOALS if g != goal])
        with pytest.raises(gt.UnknownGoalError):
            _census()

    def test_a_live_goal_dropped_from_the_set_fails_the_census(self, monkeypatch):
        monkeypatch.setattr(gt, "LIVE_GOALS", [g for g in gt.LIVE_GOALS if g != "check-breaking"])
        with pytest.raises(gt.UnknownGoalError):
            _census()

    def test_every_live_goal_is_reachable_from_gate_fresh(self):
        prerequisites: "dict[str, list[str]]" = {}
        for match in re.finditer(r"^([a-z][a-z0-9-]*):\s*([^#\n]*)", MAKEFILE, re.MULTILINE):
            if "=" in match.group(2):
                continue
            prerequisites.setdefault(match.group(1), []).extend(
                w for w in match.group(2).split() if re.fullmatch(r"[a-z][a-z0-9-]*", w)
            )
        assert "check-breaking" in _makefile_recipes(), "the reader must see the recipes"
        gated = {"gate-fresh", *gate_fresh.GATED_TARGETS}

        def below(target, seen=None):
            seen = set() if seen is None else seen
            for dep in prerequisites.get(target, []):
                if dep not in seen:
                    seen.add(dep)
                    below(dep, seen)
            return seen

        def covered(goal):
            if goal in gated or any(goal in below(g) for g in gated):
                return True
            deps = prerequisites.get(goal, [])
            return bool(deps) and all(covered(dep) for dep in deps)

        unreachable = [g for g in gt.LIVE_GOALS if not covered(g)]
        assert not unreachable, f"live goals the push does not run: {unreachable}"


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
        real = gt.read_stamp

        def counting(path):
            reads.append(path)
            return real(path)

        monkeypatch.setattr(gt, "read_stamp", counting)
        assert gt.missing_stamps(repo, "origin", remote_sha, tip).missing == []
        assert sorted(reads) == sorted(gt.stamp_path(root, tree, key) for key in lanes)

        reads.clear()
        for key in (lanes[0], lanes[-1]):
            gt.stamp_path(root, tree, key).unlink()
        assert gt.missing_stamps(repo, "origin", remote_sha, tip).missing
        in_store = len(list(root.glob("*/*.json")))
        assert len(reads) == len(lanes) + in_store
        assert max(reads.count(path) for path in set(reads)) <= 2

    def test_the_check_cli_prints_the_hooks_text(self, tmp_path, capsys):
        repo, _bare_repo, remote_sha, tip = _published(tmp_path)
        argv = ["--repo", str(repo), "--check", remote_sha, tip, "--remote", "origin"]
        assert gt.main(argv) == 1
        verdict = gt.missing_stamps(repo, "origin", remote_sha, tip)
        assert capsys.readouterr().err == "\n".join(verdict.lines) + "\n"
        _stamp_tip(repo, remote_sha, tip)
        assert gt.main(argv) == 0
        assert (
            capsys.readouterr().out == f"gate-tip: every stamped lane for {tip[:12]} is stamped\n"
        )
