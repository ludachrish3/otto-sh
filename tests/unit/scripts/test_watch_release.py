"""Pins for scripts/watch_release.sh -- the outside-the-terminal release watcher.

The watcher's job is to end with ONE line that says how the release ended, so a
reader who was not at the terminal (a phone, an agent) never has to infer it
from silence. Both endings are pinned here, against a stand-in process rather
than a real ``make release``: the watcher reads nothing from the release but
its pid, the repo's tags and ``dist/``, and the junit files, so a child that
makes those changes and exits is the whole contract.

MEASURED 2026-09-24, twice, on real releases: when a release failed after
``clean-dist`` had removed ``dist/``, the watcher exited 1 and printed nothing
-- ``dist="$(find dist/ ...)"`` fails under ``set -e`` -- so the one ending
that most needed a line got none.
"""

import re
import shutil
import subprocess
from pathlib import Path

from tests._fixtures.gitrepo import TmpGitRepo, git_env
from tests._fixtures.paths import CHECKOUT_ROOT

_SCRIPT = CHECKOUT_ROOT / "scripts" / "watch_release.sh"

_JUNIT_RED = (
    '<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest" '
    'errors="0" failures="5" skipped="0" tests="12" time="4.5">'
    '<testcase name="t" time="0.1"/></testsuite></testsuites>'
)


def _repo(tmp_path: Path) -> TmpGitRepo:
    """A repo shaped like otto-sh as far as the watcher looks: a script and a commit.

    The watcher finds its repo from its OWN path (``scripts/..``), so it is
    copied in rather than run from the dev tree -- which would read the dev
    tree's tags, ``dist/`` and junit reports instead of this one's.
    """
    repo = TmpGitRepo(tmp_path / "repo")
    (repo.root / "scripts").mkdir()
    shutil.copy2(_SCRIPT, repo.root / "scripts" / "watch_release.sh")
    repo.write("README", "r\n")
    repo.commit("base")
    return repo


def _watch(repo: TmpGitRepo, release_body: str) -> subprocess.CompletedProcess[str]:
    """Run *release_body* (bash) as the "release", and the watcher against its pid.

    THE RELEASE IS ORPHANED, not a child of this process. A direct child that
    exits stays a zombie until pytest reaps it, and ``kill -0`` succeeds on a
    zombie -- so the watcher would wait forever on a release that had ended. A
    real ``make release`` is never the watcher's caller's child either.
    """
    env = {**git_env(repo.root), "POLL_SECONDS": "0.2"}
    pid = subprocess.run(
        ["bash", "-c", f"( {release_body} ) >/dev/null 2>&1 & echo $!"],
        cwd=repo.root,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return subprocess.run(
        ["bash", str(repo.root / "scripts" / "watch_release.sh"), pid],
        cwd=repo.root,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _last_line(result: subprocess.CompletedProcess[str]) -> str:
    lines = result.stdout.strip().splitlines()
    assert lines, f"the watcher printed nothing; stderr: {result.stderr!r}"
    return lines[-1]


def test_a_release_that_dies_untagged_with_no_dist_ends_on_a_failed_line(tmp_path):
    """The measured case: no tag, and ``dist/`` gone because ``clean-dist`` ran."""
    repo = _repo(tmp_path)
    assert not (repo.root / "dist").exists()

    result = _watch(repo, "sleep 1")

    assert result.returncode == 0, result.stderr
    last = _last_line(result)
    assert last.startswith("FAILED:"), result.stdout
    assert "dist: <empty>" in last, last


def test_a_failed_line_names_the_red_lane_it_saw(tmp_path):
    """A RED lane is the likeliest reason; the ending should not make you scroll for it."""
    repo = _repo(tmp_path)
    junit = repo.root / "reports" / "junit" / "nox"
    junit.mkdir(parents=True)

    result = _watch(
        repo,
        f"sleep 0.5; printf '%s' '{_JUNIT_RED}' > {junit / 'tests_all-3.10.xml'}; sleep 1",
    )

    assert "LANE RED: nox/tests_all-3.10.xml" in result.stdout, result.stdout
    last = _last_line(result)
    assert last.startswith("FAILED:"), result.stdout
    assert "nox/tests_all-3.10.xml" in last, last


def test_a_release_that_tags_a_new_commit_ends_on_a_success_line(tmp_path):
    """The bump commit, its tag and the built dist, as the release's tail leaves them."""
    repo = _repo(tmp_path)

    result = _watch(
        repo,
        "sleep 0.5; echo v > VERSION; git add VERSION; git commit -qm bump; "
        "git tag -a v9.9.9 -m v9.9.9; mkdir -p dist; : > dist/pkg-9.9.9.whl; sleep 0.5",
    )

    assert result.returncode == 0, result.stderr
    last = _last_line(result)
    assert last.startswith("SUCCESS:"), result.stdout
    assert "v9.9.9" in last, last
    assert "pkg-9.9.9.whl" in last, last


def test_a_tag_that_was_already_at_head_is_not_a_success(tmp_path):
    """HEAD tagged BEFORE the watch began is an old release, not this one."""
    repo = _repo(tmp_path)
    repo.git("tag", "-a", "v1.0.0", "-m", "v1.0.0")

    result = _watch(repo, "sleep 1")

    last = _last_line(result)
    assert last.startswith("FAILED:"), result.stdout


def _watcher_stages(script: str) -> "list[str]":
    """The words of the script's ``STAGES=( … )`` array, backslash continuations joined."""
    match = re.search(r"^STAGES=\(([^)]*)\)", script.replace("\\\n", " "), re.MULTILINE)
    assert match is not None, "the watcher has no STAGES=( … ) array"
    return match.group(1).split()


def _release_make_goals(makefile: str) -> "list[str]":
    """The ``$(MAKE) <goal>`` words of the ``release:`` recipe, in recipe order."""
    match = re.search(r"^release:[^\n]*\n((?:\t[^\n]*\n)+)", makefile, re.MULTILINE)
    assert match is not None, "the Makefile has no `release:` rule with a recipe"
    return re.findall(r"\$\(MAKE\)\s+([a-zA-Z0-9_.-]+)", match.group(1))


def test_the_watcher_s_stages_are_the_release_recipe_s_make_goals_in_order():
    """``STAGE:`` lines name the release's gates; a stale list names stages that no longer run."""
    red = _release_make_goals("release: ## x\n\t@$(MAKE) a \\\n\t\t&& $(MAKE) b\n")
    assert red == ["a", "b"]
    assert _watcher_stages("STAGES=(b \\\n        a)\n") != red, "order must be compared"
    goals = _release_make_goals((CHECKOUT_ROOT / "Makefile").read_text())
    assert goals.count("coverage") == 1, goals
    assert _watcher_stages(_SCRIPT.read_text()) == goals
