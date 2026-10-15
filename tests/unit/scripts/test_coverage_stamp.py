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
import platform
import shutil
import sys
from pathlib import Path

import pytest

from scripts import coverage_stamp
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
    from scripts.gate_fresh import TreeState

    state = TreeState(tracked_dirty=["Makefile"], untracked=["src/otto/new.py", "notes.txt"])
    why = coverage_stamp.refusals(state, ["tests/unit"], ["COVERAGE_THRESHOLD"])
    assert len(why) == 3
    assert "Makefile" in why[0]
    assert "src/otto/new.py" in why[1]
    assert "COVERAGE_THRESHOLD" in why[2]
    assert "notes.txt" not in " ".join(why)
    assert coverage_stamp.refusals(TreeState([], ["notes.txt"]), ["tests/unit"], []) == []
    assert (
        coverage_stamp.refusals(TreeState([], ["tests/unit/test_y.py"]), ["tests/unit"], []) != []
    )
    assert coverage_stamp.refusals(TreeState([], ["tests/unity.py"]), ["tests/unit"], []) == [], (
        "a root is a directory prefix, not a string prefix"
    )
