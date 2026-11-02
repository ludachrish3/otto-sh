"""Pins for scripts/coverage_stamp.py — `make coverage`'s committed-tree stamp.

Every repo here is a throwaway ``TmpGitRepo`` under ``tmp_path`` (``--repo``):
a stamp is written into that repo's own ``.git``, never the developer's. The
Makefile side (which target calls which verb, the ``$(MAKEOVERRIDES)``
reading that names every command-line variable) is pinned in
``tests/unit/test_make_release_lanes.py``.

``check``'s three statuses are the release's instructions: 0 trust the
stamp; 1 no (or a mismatched) stamp, run ``make coverage`` and check again;
2 a refusal no stamp can cure (dirt, a foreign interpreter, a command-line
variable), stop now.
"""

import json
import os
import platform
import shutil
import sys
import time
from pathlib import Path

import pytest

from scripts import coverage_stamp, gate_stamp_common
from scripts.coverage_stamp import assets_digest, main, stamp_path, tree_hash
from tests._fixtures.gitrepo import TmpGitRepo

pytestmark = pytest.mark.interpreter_agnostic

PRIMARY = f"{sys.version_info[0]}.{sys.version_info[1]}"
"""The running interpreter plays the primary; a foreign one is simulated by monkeypatching."""

FOREIGN = f"{sys.version_info[0]}.{sys.version_info[1] + 1}.0"
"""One minor above the runner: differs from PRIMARY on every interpreter the suite runs on."""

GATE = [
    "--threshold", "95.5",
    "--selection", "not stability and not browser",
    "--browsers", "chromium",
    "--primary", PRIMARY,
]  # fmt: skip
START = ["start", *GATE, "--workers", "5"]
CHECK = ["check", *GATE]
ASSET = "src/otto/_webassets/monitor/dist/index.html"


def _repo(tmp_path: Path) -> TmpGitRepo:
    repo = TmpGitRepo(tmp_path / "repo")
    repo.write(
        "pyproject.toml", '[tool.pytest.ini_options]\ntestpaths = ["tests/unit", "tests/e2e"]\n'
    )
    repo.write("src/otto/__init__.py", "")
    repo.write("tests/unit/test_x.py", "def test_x():\n    pass\n")
    repo.write("src/otto/_webassets/monitor/dist/index.html", "<html>v1</html>\n")
    repo.write("src/otto/_webassets/covapp/index.html", "<html>cov</html>\n")
    repo.write(".gitignore", "src/otto/_webassets/*/\nreports/\n")
    repo.commit("base")
    return repo


def _run(repo: TmpGitRepo, *argv: str) -> int:
    return main([*argv, "--repo", str(repo.root)])


def _stamp(repo: TmpGitRepo) -> Path:
    return stamp_path(repo.root, tree_hash(repo.root))


def test_a_clean_run_is_stamped_and_check_accepts_it(tmp_path: Path, capsys):
    repo = _repo(tmp_path)
    assert _run(repo, *START) == 0
    assert not _stamp(repo).exists(), "start must not stamp; only finish does"
    assert _run(repo, "finish") == 0
    stamp = json.loads(_stamp(repo).read_text())
    assert stamp["tree"] == tree_hash(repo.root)
    assert stamp["python"] == platform.python_version()
    assert stamp["assets"] == assets_digest(repo.root)
    assert stamp["gate"] == {
        "threshold": "95.5",
        "selection": "not stability and not browser",
        "browsers": "chromium",
        "primary": PRIMARY,
        "workers": "5",
    }
    assert not (repo.root / "reports" / "coverage-stamp.pending.json").exists()
    assert sorted(p.name for p in _stamp(repo).parent.iterdir()) == [_stamp(repo).name], (
        "the write must leave no temp file beside the stamp"
    )
    assert _run(repo, *CHECK) == 0
    assert "passed `make coverage`" in capsys.readouterr().out


def test_start_deletes_the_tree_s_previous_stamp(tmp_path: Path):
    """make aborts at the first red leg, so a failed run can only drop the stamp at its start."""
    repo = _repo(tmp_path)
    _stamp(repo).parent.mkdir(parents=True)
    _stamp(repo).write_text("{}")
    assert _run(repo, *START) == 0
    assert not _stamp(repo).exists()
    assert _run(repo, *CHECK) == 1


@pytest.mark.parametrize(
    ("dirt", "named"),
    [
        (lambda r: r.write("src/otto/__init__.py", "changed\n"), "modified or staged"),
        (
            lambda r: (r.write("src/otto/__init__.py", "x\n"), r.git("add", "src")),
            "modified or staged",
        ),
        (lambda r: r.write("src/otto/new.py", ""), "untracked"),
        (lambda r: r.write("tests/unit/test_new.py", ""), "untracked"),
        (lambda r: r.write("tests/e2e/test_new.py", ""), "untracked"),
    ],
)
def test_a_dirty_tree_runs_but_is_not_stamped_and_check_refuses_it(
    tmp_path: Path, capsys, dirt, named
):
    repo = _repo(tmp_path)
    dirt(repo)
    assert _run(repo, *START) == 0
    assert named in capsys.readouterr().out
    assert _run(repo, "finish") == 0
    assert not _stamp(repo).exists()
    assert named in capsys.readouterr().out
    assert _run(repo, *CHECK) == 2, "dirt is no stamp's to cure: the release must stop at once"
    assert named in capsys.readouterr().out


def test_an_untracked_file_outside_src_and_the_test_roots_is_listed_not_blocking(
    tmp_path: Path, capsys
):
    repo = _repo(tmp_path)
    repo.write("notes.txt", "scratch\n")
    assert _run(repo, *START) == 0
    assert "notes.txt" in capsys.readouterr().out
    assert _run(repo, "finish") == 0
    assert _stamp(repo).exists()
    assert _run(repo, *CHECK) == 0


def test_a_command_line_override_withholds_the_stamp(tmp_path: Path, capsys):
    repo = _repo(tmp_path)
    assert (
        _run(repo, *START, "--overridden", "COVERAGE_THRESHOLD", "--overridden", "COVERAGE_WORKERS")
        == 0
    )
    assert _run(repo, "finish") == 0
    assert not _stamp(repo).exists()
    assert "COVERAGE_THRESHOLD" in capsys.readouterr().out


def test_a_tree_that_changes_under_the_run_is_not_stamped(tmp_path: Path, capsys):
    repo = _repo(tmp_path)
    assert _run(repo, *START) == 0
    repo.write("src/otto/__init__.py", "v2\n")
    repo.commit("during the run")
    assert _run(repo, "finish") == 0
    assert not _stamp(repo).exists()
    assert "changed during the run" in capsys.readouterr().out
    assert _run(repo, *CHECK) == 1


def test_an_ignored_source_planted_under_the_run_is_not_stamped(tmp_path: Path, capsys):
    repo = _repo(tmp_path)
    repo.write(".gitignore", "src/otto/_webassets/*/\nreports/\n*.local.py\n")
    repo.commit("ignore local sources")
    assert _run(repo, *START) == 0
    repo.write("src/otto/x.local.py", "x = 1\n")
    assert _run(repo, "finish") == 0
    assert not _stamp(repo).exists()
    assert "changed during the run" in capsys.readouterr().out


def test_assets_rebuilt_under_the_run_are_not_stamped(tmp_path: Path, capsys):
    """The digest is taken at start (after the dist rules ran) and must hold at finish."""
    repo = _repo(tmp_path)
    assert _run(repo, *START) == 0
    repo.write(ASSET, "<html>v2</html>\n")
    assert _run(repo, "finish") == 0
    assert not _stamp(repo).exists()
    assert "assets" in capsys.readouterr().out


def test_a_foreign_interpreter_at_start_withholds_the_stamp(tmp_path: Path, capsys, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setattr(coverage_stamp.platform, "python_version", lambda: FOREIGN)
    assert _run(repo, *START) == 0
    assert f"python {FOREIGN} is not the primary {PRIMARY}" in capsys.readouterr().out
    assert _run(repo, "finish") == 0
    assert not _stamp(repo).exists()


def test_an_interpreter_that_changes_under_the_run_is_not_stamped(
    tmp_path: Path, capsys, monkeypatch
):
    repo = _repo(tmp_path)
    assert _run(repo, *START) == 0
    monkeypatch.setattr(coverage_stamp.platform, "python_version", lambda: FOREIGN)
    assert _run(repo, "finish") == 0
    assert not _stamp(repo).exists()
    assert "the python changed during the run" in capsys.readouterr().out


def test_finish_without_a_start_stamps_nothing_and_says_so(tmp_path: Path, capsys):
    repo = _repo(tmp_path)
    assert _run(repo, "finish") == 0
    assert not _stamp(repo).exists()
    assert "no pending record" in capsys.readouterr().out


def _stamped(tmp_path: Path) -> TmpGitRepo:
    repo = _repo(tmp_path)
    assert _run(repo, *START) == 0
    assert _run(repo, "finish") == 0
    assert _run(repo, *CHECK) == 0
    return repo


def test_check_refuses_a_stamp_of_another_interpreter(tmp_path: Path, capsys, monkeypatch):
    """A release from a venv rebuilt on another CPython must not ride a stamp this one wrote."""
    repo = _stamped(tmp_path)
    monkeypatch.setattr(coverage_stamp.platform, "python_version", lambda: FOREIGN)
    assert _run(repo, *CHECK) == 2, "a foreign venv cannot make a primary stamp: stop at once"
    out = capsys.readouterr().out
    assert "python: stamped" in out
    assert f"python {FOREIGN} is not the primary {PRIMARY}" in out


def test_check_refuses_a_stamp_written_for_another_tree(tmp_path: Path, capsys):
    """The file name is not the evidence; the stamp's own `tree` field is."""
    repo = _stamped(tmp_path)
    stamp = json.loads(_stamp(repo).read_text())
    stamp["tree"] = "0" * 40
    _stamp(repo).write_text(json.dumps(stamp))
    assert _run(repo, *CHECK) == 1
    assert "tree: stamped 000000000000" in capsys.readouterr().out


def test_check_refuses_a_stamp_that_is_not_json(tmp_path: Path, capsys):
    repo = _stamped(tmp_path)
    _stamp(repo).write_text("not json")
    assert _run(repo, *CHECK) == 1
    assert "unreadable" in capsys.readouterr().out


def test_check_refuses_a_stamp_that_is_not_an_object(tmp_path: Path, capsys):
    repo = _stamped(tmp_path)
    _stamp(repo).write_text("[]")
    assert _run(repo, *CHECK) == 1
    assert "unreadable" in capsys.readouterr().out


def test_check_refuses_an_empty_stamp_object(tmp_path: Path, capsys):
    """`{}` is a readable stamp with every field missing — compared, not skipped as 'no stamp'."""
    repo = _stamped(tmp_path)
    _stamp(repo).write_text("{}")
    assert _run(repo, *CHECK) == 1
    out = capsys.readouterr().out
    assert "stamp format None is not 1" in out
    assert "tree: stamped None" in out


def test_check_refuses_rebuilt_assets(tmp_path: Path, capsys):
    repo = _stamped(tmp_path)
    repo.write(ASSET, "<html>v2</html>\n")
    assert _run(repo, *CHECK) == 1
    assert "assets" in capsys.readouterr().out


def test_check_refuses_absent_assets_that_were_present(tmp_path: Path, capsys):
    repo = _stamped(tmp_path)
    shutil.rmtree(repo.root / "src/otto/_webassets/monitor/dist")
    assert _run(repo, *CHECK) == 1
    assert "assets" in capsys.readouterr().out


def _gate(**changed: str) -> "list[str]":
    fields = {
        "threshold": "95.5",
        "selection": "not stability and not browser",
        "browsers": "chromium",
        "primary": PRIMARY,
    }
    fields.update(changed)
    return [arg for name, value in fields.items() for arg in (f"--{name}", value)]


@pytest.mark.parametrize(
    ("argv", "named", "status"),
    [
        (["check", *_gate(threshold="90")], "threshold", 1),
        (["check", *_gate(selection="not stability")], "selection", 1),
        (["check", *_gate(browsers="firefox")], "browsers", 1),
        # the running venv is not the declared primary: no `make coverage` here can fix that
        (["check", *_gate(primary="9.9")], "is not the primary 9.9", 2),
    ],
)
def test_check_refuses_a_gate_the_makefile_now_declares_differently(
    tmp_path: Path, capsys, argv, named, status
):
    repo = _stamped(tmp_path)
    assert _run(repo, *argv) == status
    assert named in capsys.readouterr().out


def test_check_refuses_an_overridden_release(tmp_path: Path, capsys):
    """A stamp vouches for the declared gate; a release with a command-line variable is not it."""
    repo = _stamped(tmp_path)
    capsys.readouterr()
    assert _run(repo, *CHECK, "--overridden", "PYTEST_ADDOPTS") == 2
    out = capsys.readouterr().out
    assert "came from the command line" in out
    assert "PYTEST_ADDOPTS" in out
    assert _run(repo, *CHECK) == 0


def test_check_exit_statuses_tell_the_release_what_to_do(tmp_path: Path):
    """0 trust it; 1 run `make coverage` and re-check; 2 stop — dirt wins over a missing stamp."""
    repo = _stamped(tmp_path)
    assert _run(repo, *CHECK) == 0
    repo.write("src/otto/__init__.py", "v2\n")
    assert _run(repo, *CHECK) == 2, "dirty, stamp still valid for HEAD's tree"
    repo.commit("more")
    assert _run(repo, *CHECK) == 1, "clean, new tree, no stamp"
    repo.write("src/otto/__init__.py", "v3\n")
    assert _run(repo, *CHECK) == 2, "dirty AND no stamp: dirt wins"


def test_check_refuses_a_stamp_whose_gate_is_not_an_object(tmp_path: Path, capsys):
    repo = _stamped(tmp_path)
    _stamp(repo).write_text(json.dumps({"format": 1, "gate": None}))
    capsys.readouterr()
    assert _run(repo, *CHECK) == 1
    assert "stamp unreadable: gate is not an object" in capsys.readouterr().out


def test_a_missing_git_is_an_error_not_a_pass(tmp_path: Path, capsys, monkeypatch):
    def no_git(*_args, **_kwargs):
        raise FileNotFoundError(2, "No such file or directory", "git")

    monkeypatch.setattr(coverage_stamp.subprocess, "run", no_git)
    assert main([*CHECK, "--repo", str(tmp_path)]) == 1
    assert "git is not available" in capsys.readouterr().err


def test_check_after_a_new_commit_finds_no_stamp(tmp_path: Path, capsys):
    repo = _stamped(tmp_path)
    repo.write("src/otto/__init__.py", "v2\n")
    repo.commit("more")
    assert _run(repo, *CHECK) == 1
    assert "no stamp" in capsys.readouterr().out


def _worktree(repo: TmpGitRepo, root: Path) -> TmpGitRepo:
    """A detached worktree of *repo* at *root*; gitignored assets are not checked out, so copy."""
    repo.git("worktree", "add", "--detach", str(root), "HEAD")
    wt = TmpGitRepo.__new__(TmpGitRepo)
    wt.root, wt.dates = root, None
    for rel in coverage_stamp.ASSET_DIRS:
        shutil.copytree(repo.root / rel, root / rel)
    return wt


def test_the_stamp_is_shared_by_every_worktree_of_the_clone(tmp_path: Path):
    """A tree gated in a worktree releases from the main checkout, and the other way round."""
    repo = _repo(tmp_path)
    wt = _worktree(repo, tmp_path / "wt")
    assert tree_hash(wt.root) == tree_hash(repo.root)
    assert assets_digest(wt.root) == assets_digest(repo.root)
    assert _run(wt, *START) == 0
    assert _run(wt, "finish") == 0
    assert _stamp(wt) == _stamp(repo)
    assert _run(repo, *CHECK) == 0
    _stamp(repo).unlink()
    assert _run(repo, *START) == 0
    assert _run(repo, "finish") == 0
    assert _run(wt, *CHECK) == 0


def test_a_worktree_with_other_assets_does_not_ride_the_main_checkout_s_stamp(
    tmp_path: Path, capsys
):
    repo = _stamped(tmp_path)
    wt = _worktree(repo, tmp_path / "wt")
    (wt.root / ASSET).write_text("<html>stale</html>\n")
    assert _run(wt, *CHECK) == 1
    assert "assets" in capsys.readouterr().out


def test_assets_digest_changes_with_content_and_with_presence(tmp_path: Path):
    repo = _repo(tmp_path)
    before = assets_digest(repo.root)
    repo.write("src/otto/_webassets/covapp/index.html", "<html>cov2</html>\n")
    assert assets_digest(repo.root) != before
    (repo.root / "src/otto/_webassets/covapp/index.html").unlink()
    (repo.root / "src/otto/_webassets/covapp").rmdir()
    assert assets_digest(repo.root) != before


def test_a_missing_gate_option_is_a_usage_error(tmp_path: Path):
    repo = _repo(tmp_path)
    with pytest.raises(SystemExit) as exc:
        main(["start", "--threshold", "95.5", "--repo", str(repo.root)])
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        main(["check", "--repo", str(repo.root)])
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        main(["check", *_gate()[:-2], "--repo", str(repo.root)])  # every gate option but --primary
    assert exc.value.code == 2


def test_repo_is_an_option_of_every_verb(tmp_path: Path, monkeypatch):
    """`--repo` follows the verb (each subparser carries it), not before it."""
    repo = _repo(tmp_path)
    # not the repo: a top-level-only --repo would run in the wrong directory
    monkeypatch.chdir(tmp_path)
    assert main(["start", *GATE, "--workers", "1", "--repo", str(repo.root)]) == 0
    assert main(["finish", "--repo", str(repo.root)]) == 0
    assert main(["check", *GATE, "--repo", str(repo.root)]) == 0


def test_refusals_name_every_reason_at_once():
    from scripts.gate_stamp_common import TreeState

    state = TreeState(
        tracked_dirty=["Makefile"], untracked=["src/otto/new.py", "notes.txt"], ignored_sources=[]
    )
    why = coverage_stamp.refusals(state, ["tests/unit"], ["COVERAGE_THRESHOLD"])
    assert len(why) == 3
    assert "Makefile" in why[0]
    assert "src/otto/new.py" in why[1]
    assert "COVERAGE_THRESHOLD" in why[2]
    assert "notes.txt" not in " ".join(why)
    assert coverage_stamp.refusals(TreeState([], ["notes.txt"], []), ["tests/unit"], []) == []
    assert (
        coverage_stamp.refusals(TreeState([], ["tests/unit/test_y.py"], []), ["tests/unit"], [])
        != []
    )
    assert (
        coverage_stamp.refusals(TreeState([], ["tests/unity.py"], []), ["tests/unit"], []) == []
    ), "a root is a directory prefix, not a string prefix"


# ── Per-leg timing: what each leg of `make coverage` cost, recorded in the stamp ─────────


def _junit(repo: TmpGitRepo, group: str) -> str:
    path = repo.root / "reports" / "junit" / group / f"{group}.xml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('<testsuites><testsuite name="pytest" time="1.0"/></testsuites>\n')
    return f"reports/junit/{group}/{group}.xml"


def test_the_stamp_records_each_legs_elapsed_and_the_reports_the_run_wrote(tmp_path: Path, capsys):
    repo = _repo(tmp_path)
    assert _run(repo, *START) == 0
    assert _run(repo, "mark", "dashboard") == 0
    written = _junit(repo, "coverage-python")
    assert _run(repo, "mark", "coverage-python") == 0
    assert _run(repo, "mark", "coverage-python-serial") == 0
    assert _run(repo, "mark", "coverage-ts") == 0
    assert _run(repo, "finish") == 0
    timing = json.loads(_stamp(repo).read_text())["timing"]
    assert set(timing) == {"started", "finished", "elapsed_s", "nproc", "workers", "legs", "junit"}
    assert timing["started"] <= timing["finished"]
    assert isinstance(timing["elapsed_s"], int)
    assert timing["elapsed_s"] >= 0
    assert timing["workers"] == "5"
    assert timing["nproc"] >= 1
    assert [leg["leg"] for leg in timing["legs"]] == [
        "dashboard",
        "coverage-python",
        "coverage-python-serial",
        "coverage-ts",
    ]
    assert all(isinstance(leg["seconds"], int) and leg["seconds"] >= 0 for leg in timing["legs"])
    assert timing["junit"] == [written]
    assert "stamped tree" in capsys.readouterr().out


def test_a_mark_outside_a_stamped_run_is_silent_and_green(tmp_path: Path, capsys):
    """`make coverage-python` alone (and `make all`) marks legs with no pending record."""
    repo = _repo(tmp_path)
    assert _run(repo, "mark", "coverage-python") == 0
    assert capsys.readouterr().out == ""
    assert not (repo.root / "reports" / "coverage-stamp.pending.json").exists()


def test_a_run_without_marks_still_stamps_with_an_empty_leg_table(tmp_path: Path):
    repo = _repo(tmp_path)
    assert _run(repo, *START) == 0
    assert _run(repo, "finish") == 0
    timing = json.loads(_stamp(repo).read_text())["timing"]
    assert timing["legs"] == []


def test_a_report_older_than_the_run_is_not_the_runs(tmp_path: Path):
    repo = _repo(tmp_path)
    old = _junit(repo, "quick")
    os.utime(repo.root / old, (1, 1))
    assert _run(repo, *START) == 0
    new = _junit(repo, "coverage-python")
    assert _run(repo, "finish") == 0
    assert json.loads(_stamp(repo).read_text())["timing"]["junit"] == [new]


def test_each_leg_is_measured_from_the_previous_mark(tmp_path: Path, monkeypatch):
    """One utc_now() per verb: start, four marks, finish's written_at."""
    repo = _repo(tmp_path)
    clock = iter(
        [
            "2026-01-01T00:00:00+00:00",
            "2026-01-01T00:00:10+00:00",
            "2026-01-01T00:00:25+00:00",
            "2026-01-01T00:00:27+00:00",
            "2026-01-01T00:01:40+00:00",
            "2026-01-01T00:02:10+00:00",
        ]
    )
    monkeypatch.setattr(coverage_stamp, "utc_now", lambda: next(clock))
    assert _run(repo, *START) == 0
    for leg in ("dashboard", "coverage-python", "coverage-python-serial", "coverage-ts"):
        assert _run(repo, "mark", leg) == 0
    assert _run(repo, "finish") == 0
    timing = json.loads(_stamp(repo).read_text())["timing"]
    assert timing["legs"] == [
        {"leg": "dashboard", "seconds": 10},
        {"leg": "coverage-python", "seconds": 15},
        {"leg": "coverage-python-serial", "seconds": 2},
        {"leg": "coverage-ts", "seconds": 73},
    ]
    assert timing["elapsed_s"] == 130
    assert timing["started"] == "2026-01-01T00:00:00+00:00"
    assert timing["finished"] == "2026-01-01T00:02:10+00:00"


def test_a_clock_step_backwards_never_makes_a_negative_leg():
    assert (
        coverage_stamp._seconds_between("2026-01-01T00:00:09+00:00", "2026-01-01T00:00:01+00:00")
        == 0
    )


def test_a_mark_on_a_pending_record_it_cannot_use_is_green_and_changes_nothing(
    tmp_path: Path, capsys
):
    """A failed run never reaches finish, so its record lingers; an older format has no marks."""
    repo = _repo(tmp_path)
    pending = repo.root / "reports" / "coverage-stamp.pending.json"
    pending.parent.mkdir(parents=True, exist_ok=True)
    older = json.dumps({"format": 1, "tree": "t" * 40})
    for bad in (older, "{not json", "[]", '"text"', '{"marks": "x"}'):
        pending.write_text(bad)
        assert _run(repo, "mark", "dashboard") == 0
        assert pending.read_text() == bad
    assert capsys.readouterr().out == ""


def test_a_failed_runs_record_takes_marks_until_the_next_start(tmp_path: Path):
    repo = _repo(tmp_path)
    assert _run(repo, *START) == 0
    assert _run(repo, "mark", "dashboard") == 0  # the run then fails: no finish
    assert _run(repo, "mark", "coverage-python") == 0  # a later bare `make coverage-python`
    pending = repo.root / "reports" / "coverage-stamp.pending.json"
    assert [m["leg"] for m in json.loads(pending.read_text())["marks"]] == [
        "dashboard",
        "coverage-python",
    ]
    assert _run(repo, *START) == 0
    assert json.loads(pending.read_text())["marks"] == []


def test_an_untouched_report_is_never_the_runs_and_a_rewritten_one_is(tmp_path: Path):
    repo = _repo(tmp_path)
    untouched = _junit(repo, "quick")
    future = time.time() + 3600
    os.utime(repo.root / untouched, (future, future))
    rewritten = _junit(repo, "dashboard")
    os.utime(repo.root / rewritten, (1, 1))
    assert _run(repo, *START) == 0
    (repo.root / rewritten).write_text("<testsuites/>\n")
    assert _run(repo, "finish") == 0
    assert json.loads(_stamp(repo).read_text())["timing"]["junit"] == [rewritten]


def _raising_snapshot(_repo):
    raise OSError("reports unreadable")


def test_a_failure_while_collecting_reports_still_stamps_and_attributes_nothing(
    tmp_path: Path, monkeypatch
):
    # at finish
    repo = _repo(tmp_path)
    assert _run(repo, *START) == 0
    monkeypatch.setattr(gate_stamp_common, "junit_snapshot", _raising_snapshot)
    assert _run(repo, "finish") == 0
    assert json.loads(_stamp(repo).read_text())["timing"]["junit"] == []
    # at start: a later readable directory full of other lanes' reports is NOT the run's
    repo = _repo(tmp_path / "second")
    _junit(repo, "quick")
    assert _run(repo, *START) == 0
    assert (
        json.loads((repo.root / "reports" / "coverage-stamp.pending.json").read_text())[
            "junit_before"
        ]
        is None
    )
    monkeypatch.undo()
    _junit(repo, "coverage-python")
    assert _run(repo, "finish") == 0
    assert json.loads(_stamp(repo).read_text())["timing"]["junit"] == []
