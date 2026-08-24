"""Activation switches end-to-end: refusal, demotion, usage errors (hostless).

Every test drives the real ``otto`` subprocess. None touches a host: the
sample instructions only log, ``--show-lab`` only prints, and the one walk
test excludes EVERY repo so the walk has nobody to dial (the with/without
discriminator for the walks lives in the orchestrator unit bed, which fakes
the actions seam).

The ``[project]``-declaring repos are written into ``tmp_path`` rather than
onto the shared ``tests/repo1`` / ``tests/repo2`` fixtures: a ``[project]``
table on either of those would silently re-scope every other suite that
loads them.
"""

import textwrap

import pytest

from tests._fixtures.sutrepo import make_sut_repo
from tests.e2e._otto_subprocess import REPO1, REPO2, run_otto

pytestmark = pytest.mark.hostless

_BOTH = f"{REPO1},{REPO2}"


def _run(argv, *, sut_dirs=_BOTH, lab=None, xdir=None):
    return run_otto(
        argv,
        xdir=xdir,
        sut_dirs=None,
        lab=lab,
        extra_argv_prefix=["-R"],
        extra_env={"OTTO_SUT_DIRS": str(sut_dirs)},
    )


class TestDispatchRefusal:
    def test_excluded_owner_is_refused_exit_1(self, tmp_path):
        result = _run(["-E", "repo2", "run", "install-repo2"], lab="unix_alt", xdir=tmp_path)
        assert result.returncode == 1
        blob = result.stdout + result.stderr
        assert "belongs to repo 'repo2'" in blob
        assert "--exclude-projects repo2" in blob

    def test_without_the_switch_it_runs(self, tmp_path):
        result = _run(["run", "install-repo2"], lab="unix_alt", xdir=tmp_path)
        assert result.returncode == 0, result.stderr


class TestUsageErrors:
    def test_conflicting_switches_exit_2(self, tmp_path):
        result = _run(
            ["-I", "repo2", "-E", "repo2", "run", "install-repo2"],
            lab="unix_alt",
            xdir=tmp_path,
        )
        assert result.returncode == 2

    def test_unknown_name_exits_2_with_a_suggestion(self, tmp_path):
        result = _run(["-E", "repoo2", "run", "install-repo2"], lab="unix_alt", xdir=tmp_path)
        assert result.returncode == 2
        assert "did you mean 'repo2'" in result.stdout + result.stderr


def _write_repo(root, name, *, lab_patterns=None, init_body=""):
    """A minimal SUT repo: settings + one init package under ``pylib/``.

    Through ``make_sut_repo``, never a hand-rolled ``settings.toml`` write —
    tests/unit/test_sutrepo_scaffold_policy.py enforces the single builder,
    and it caught the first cut of this helper.
    """
    project = (
        ""
        if lab_patterns is None
        else (
            "[project]\nlab_patterns = ["
            + ", ".join(f'"{p}"' for p in lab_patterns)
            + ']\nhost_patterns = [".*"]\n'
        )
    )
    return make_sut_repo(
        root / name,
        name=name,
        version="0.0.1",
        extra=f"libs = [\"pylib\"]\ninit = ['{name}_init']\n{project}",
        files={f"pylib/{name}_init/__init__.py": init_body},
    )


class TestLabInferredRefusal:
    def test_owner_outside_the_loaded_lab_is_refused_and_include_overrides(self, tmp_path):
        instruction = textwrap.dedent("""\
            from otto.cli.run import instruction


            @instruction()
            async def flash_b():
                print("flash-b ran")
        """)
        repo = _write_repo(tmp_path, "sideproj", lab_patterns=["unix_alt"], init_body=instruction)

        refused = _run(
            ["run", "flash-b"], sut_dirs=f"{REPO1},{repo}", lab="unix", xdir=tmp_path / "x1"
        )
        assert refused.returncode == 1
        blob = refused.stdout + refused.stderr
        assert "belongs to repo 'sideproj'" in blob
        assert "inactive for the loaded lab(s) [unix]" in blob
        assert "lab_patterns: unix_alt" in blob

        forced = _run(
            ["-I", "sideproj", "run", "flash-b"],
            sut_dirs=f"{REPO1},{repo}",
            lab="unix",
            xdir=tmp_path / "x2",
        )
        assert forced.returncode == 0, forced.stderr


class TestBootstrapDemotion:
    _BROKEN = "import does_not_exist_anywhere  # noqa: F401\n"

    def test_inactive_broken_repo_demotes_to_a_warning(self, tmp_path):
        repo = _write_repo(
            tmp_path, "brokenproj", lab_patterns=["never_loaded"], init_body=self._BROKEN
        )
        result = _run(["--show-lab"], sut_dirs=f"{REPO1},{repo}", lab="unix", xdir=tmp_path / "x")
        assert result.returncode == 0, result.stderr
        assert "inactive for this run" in result.stdout + result.stderr

    def test_active_broken_repo_stays_fatal(self, tmp_path):
        repo = _write_repo(tmp_path, "brokenproj", init_body=self._BROKEN)
        result = _run(["--show-lab"], sut_dirs=f"{REPO1},{repo}", lab="unix", xdir=tmp_path / "x")
        assert result.returncode == 1
        assert "Cannot run commands while a repo fails to load" in result.stdout + result.stderr


class TestWalkWithEverythingExcluded:
    def test_status_skips_every_switched_off_repo_without_dialing(self, tmp_path):
        """Every repo switched off: both get a line, and nobody is dialed.

        THE EXIT CODE IS PINNED AT 1 AS AN OBSERVATION, not as an endorsement.
        ``status`` documents 1 as "fully uninstalled", and
        ``orchestrator._aggregate`` reaches it here because zero counted repos
        aggregate to UNINSTALLED by its stated rule. That rule was chosen for
        the counted-repo case (a docs-only repo contributes no state); a run
        that switched EVERY repo off asked about nothing, which is a third
        answer the three-member code cannot express. Left as-is deliberately:
        changing the aggregate would move ``is_uninstalled()`` and the
        converges that branch on it, which is a decision about the install-state
        vocabulary, not about activation. If that decision is taken, this
        assertion is the one that must change with it.
        """
        result = _run(["-E", "repo1,repo2", "run", "status"], lab="unix_alt", xdir=tmp_path)
        blob = result.stdout + result.stderr
        assert "repo 'repo1' switched off for this run" in blob
        assert "repo 'repo2' switched off for this run" in blob
        assert result.returncode == 1, blob
