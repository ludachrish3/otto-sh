"""The dependency preflight through the real binary: the spec's acceptance pair.

Neither half is the acceptance test on its own. A gate that refuses everything
passes the first; a gate that refuses nothing passes the second. Together they
say the refusal tracks the ENVIRONMENT rather than a hard-wired verdict:

1. ``otto-fixture-beetroot`` absent -> ``otto run use-beetroot`` refuses,
   exit 1, naming ``otto env sync``, and the instruction body never runs.
2. after ``otto env sync -- --find-links tests/_fixtures/wheels`` -> the SAME
   instruction, run from the environment that sync built, succeeds.

repo4 is COPIED into tmp_path rather than used in place -- see
``tests._fixtures.sutrepo.copy_sample_repo`` for why, and
``tests/unit/test_sample_repo_hygiene.py`` for the gate that catches a
regression. The consequence HERE is that the requirement string is
deterministic: read from the pyproject (spaced, ``otto-fixture-beetroot >=
0.1``) rather than from a leftover ``.egg-info``'s ``Requires-Dist``
(normalized, ``otto-fixture-beetroot>=0.1``), so the assertions below mean the
same thing on a developer's machine and on a clean clone.

``OTTO_HOME`` is pinned at tmp_path throughout: without it these build into
the developer's real ``~/.otto``.
"""

import subprocess

import pytest

from tests._fixtures.paths import PROJECT_ROOT, WHEELS_DIR
from tests._fixtures.sutrepo import copy_sample_repo
from tests.e2e._otto_subprocess import OTTO_BIN, REPO1, otto_subprocess_env

pytestmark = pytest.mark.hostless

REPO4 = PROJECT_ROOT / "tests" / "repo4"

BUILD_TIMEOUT = 300
"""A real installer run. Measured at ~1.5s warm; a cold wheel cache on CI is
much slower, and a timeout that fires there reads as a otto bug."""

REQUIREMENT = "otto-fixture-beetroot >= 0.1"
"""As repo4's pyproject spells it -- which is what the copied repo guarantees."""

BODY_MARKER = "the fixture package says"
"""The instruction body's own log line. Its ABSENCE is how a test says the run
stopped before the body, and the body is the only thing in this fixture that
could reach a host."""


def _repo4(tmp_path, *, eager: bool = False):
    """A private copy of repo4, free of any build artifact from a prior run.

    *eager* moves ``repo4_lib`` -- the module that imports the dependency at
    module scope -- into ``init``, which is the shape bootstrap loads.
    """
    dst = copy_sample_repo(REPO4, tmp_path / "repo4")
    if eager:
        settings = dst / ".otto" / "settings.toml"
        eager_init = settings.read_text().replace("init = [", "init = [\n    'repo4_lib',")
        # An in-place edit of a COPIED sample repo's own settings, not a
        # hand-rolled scaffold: the point of the fixture is that this is
        # repo4's real file with one init entry added.
        settings.write_text(eager_init)  # sutrepo-exempt: in-place edit of a copied sample repo
    return dst


def _run(argv, *, home, sut_dirs, binary=None, timeout=60):
    """Run *binary* (default: the dev checkout's otto) with OTTO_HOME pinned."""
    return subprocess.run(
        [str(binary or OTTO_BIN), "-R", *argv],
        env=otto_subprocess_env(
            sut_dirs=None,
            extra_env={"OTTO_SUT_DIRS": str(sut_dirs), "OTTO_HOME": str(home)},
        ),
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        timeout=timeout,
        check=False,
    )


def _env_otto(home):
    """The ``otto`` the built environment provides, or None if it built none."""
    found = [p for p in home.rglob("env/bin/otto") if p.is_file()]
    return found[0] if found else None


class TestTheAcceptancePair:
    """Both halves, in the order an operator meets them."""

    def test_an_absent_dependency_refuses_before_the_instruction_body_runs(self, tmp_path):
        """Half one. ``-R`` skips the reservation gate, which the refusal
        precedes anyway -- so the only thing between the lab loading and the
        refusal is the preflight."""
        result = _run(
            ["--lab", "unix", "run", "use-beetroot"],
            home=tmp_path / "home",
            sut_dirs=f"{REPO1},{_repo4(tmp_path)}",
        )
        assert result.returncode == 1, result.stdout + result.stderr
        blob = result.stdout + result.stderr
        assert f"error: repo 'repo4' requires '{REQUIREMENT}'" in blob
        assert "not satisfied in this environment (found: none)" in blob
        assert "fix: otto env sync" in blob
        assert f"or:  uv pip install '{REQUIREMENT}'" in blob
        assert BODY_MARKER not in blob

    def test_after_env_sync_the_same_instruction_runs(self, tmp_path):
        """Half two, and the reason half one is not vacuous.

        The environment ``sync`` builds is a DIFFERENT interpreter from the one
        that was just refused, so the second leg runs the otto that environment
        provides. That is the workflow the refusal is pointing at: sync, then
        work from the environment it built.
        """
        home = tmp_path / "home"
        sut_dirs = f"{REPO1},{_repo4(tmp_path)}"
        sync = _run(
            ["env", "sync", "--", "--find-links", str(WHEELS_DIR)],
            home=home,
            sut_dirs=sut_dirs,
            timeout=BUILD_TIMEOUT,
        )
        assert sync.returncode == 0, sync.stdout + sync.stderr

        built = _env_otto(home)
        assert built is not None, f"env sync built no otto: {sync.stdout}"
        result = _run(
            ["--lab", "unix", "run", "use-beetroot"],
            home=home,
            sut_dirs=sut_dirs,
            binary=built,
            timeout=BUILD_TIMEOUT,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        blob = result.stdout + result.stderr
        assert BODY_MARKER in blob
        assert "fix: otto env sync" not in blob

    def test_env_sync_itself_is_never_refused_by_the_gate(self, tmp_path):
        """The verb the refusal names has to RUN while the condition holds.

        ``otto env`` is registered ``lab_free``, so it never reaches the
        preamble branch the gate lives in. Structural rather than an exemption
        list -- and this is the cell that notices if the gate is ever hoisted
        out of that branch.
        """
        result = _run(
            ["env", "show"],
            home=tmp_path / "home",
            sut_dirs=f"{REPO1},{_repo4(tmp_path)}",
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "error: repo 'repo4'" not in result.stdout + result.stderr


def test_an_eager_import_fails_at_bootstrap_before_the_preflight_can_speak(tmp_path):
    """F7's cost, pinned as known behaviour rather than left to be discovered.

    The preflight runs after the lab session, and ``bootstrap()``'s per-repo
    init loop runs long before that -- so a repo whose init module imports a
    missing package at MODULE scope is already a ``BootstrapError`` by the time
    the gate would speak. The operator gets "failed to load", not "run otto env
    sync". Moving the gate earlier is what would fix this, and moving it
    earlier is what makes ``active()`` unavailable; that trade is the whole of
    F7.

    Same repo, same missing package, same command as the acceptance pair above
    -- only the failure SHAPE differs, which is what makes the pair of tests a
    comparison rather than two unrelated cases.
    """
    result = _run(
        ["--lab", "unix", "run", "use-beetroot"],
        home=tmp_path / "home",
        sut_dirs=f"{REPO1},{_repo4(tmp_path, eager=True)}",
    )
    assert result.returncode == 1
    blob = result.stdout + result.stderr
    assert "failed to load repo4_lib" in blob
    assert "No module named 'otto_fixture_beetroot'" in blob
    assert "Cannot run commands while a repo fails to load" in blob
    assert "fix: otto env sync" not in blob
