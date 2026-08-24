"""``otto env create`` / ``sync`` through the real binary, building real venvs.

Parametrized over both backends only where the backend is actually the thing
under test. ``--backend pip`` forces the stdlib fallback on a uv-equipped host,
which is the only way that arm is ever exercised here; measured cost is ~1s per
uv build and ~9.5s per pip build, so the cases that exercise otto's own control
flow (refusal, delegation, the dry-run seam) deliberately do NOT build twice --
and the refusal case does not build at all, because "an environment already
exists" is a question about a path, not about a venv.

Every case pins OTTO_HOME at a tmp_path: without it these would build into the
developer's real ~/.otto.
"""

import json
import os
import subprocess

import pytest

from otto.config.home import workspace_key
from tests._fixtures.paths import PROJECT_ROOT, WHEELS_DIR
from tests._fixtures.sutrepo import copy_sample_repo
from tests.e2e._otto_subprocess import REPO1, run_otto

pytestmark = pytest.mark.hostless

REPO4 = PROJECT_ROOT / "tests" / "repo4"

# A real installer run, not otto's own logic: pip needs room (measured 9.5s
# warm, and a cold wheel cache on CI is slower).
BUILD_TIMEOUT = 300

BACKENDS = ("uv", "pip")


def _run(argv, *, home, sut_dirs=REPO1, timeout=60):
    """Run otto with OTTO_HOME pinned, so nothing touches the real ~/.otto."""
    return run_otto(
        argv,
        xdir=None,
        sut_dirs=None,
        extra_argv_prefix=["-R"],
        # COLUMNS pins rich's width: a wrapped cell would make the table
        # unreadable to _cell for reasons that have nothing to do with otto.
        extra_env={
            "OTTO_SUT_DIRS": str(sut_dirs),
            "OTTO_HOME": str(home),
            "COLUMNS": "200",
        },
        timeout=timeout,
    )


def _cell(stdout, row_marker, index):
    """Return one cell of the rounded table row containing *row_marker*.

    Reading the rendered table is deliberate: the column an operator sees is
    the thing under test, and a status object asserted in-process would not
    catch a presentation that reports the wrong field.
    """
    for line in stdout.splitlines():
        if row_marker in line and "\u2502" in line:
            cells = [c.strip() for c in line.split("\u2502") if c.strip()]
            return cells[index]
    return None


def _the_env(home):
    """The single ``env`` directory under *home*, or None if nothing was built."""
    found = [p for p in home.rglob("env") if p.is_dir()]
    return found[0] if found else None


@pytest.fixture
def repo4(tmp_path):
    """A PRIVATE copy of repo4, because these tests install it editable.

    Installing the checked-in tree would write an ``.egg-info`` into
    ``tests/repo4/pylib/`` — which repo4 puts on ``sys.path`` via its ``libs``,
    where ``importlib.metadata`` reads it as an installed distribution and the
    dependency preflight answers differently than it would on a clean clone.
    ``tests/unit/test_sample_repo_hygiene.py`` is the gate that catches a
    regression here.
    """
    return copy_sample_repo(REPO4, tmp_path / "repo4")


class TestCreate:
    @pytest.mark.parametrize("backend", BACKENDS)
    def test_it_builds_an_env_holding_otto_and_the_installable_repo(self, tmp_path, backend, repo4):
        """repo4's dependency lives on no index, so the find-links is required.

        That is the fixture working as intended, not a workaround: repo4 exists
        to model a repo whose Python requirements are not simply there.
        """
        home = tmp_path / "home"
        result = _run(
            ["env", "create", "--backend", backend, "--", "--find-links", str(WHEELS_DIR)],
            home=home,
            sut_dirs=f"{REPO1},{repo4}",
            timeout=BUILD_TIMEOUT,
        )
        assert result.returncode == 0, result.stdout + result.stderr

        env = _the_env(home)
        assert env is not None, f"no env built under {home}"
        assert (env / "bin" / "python").is_file(), sorted(q.name for q in env.iterdir())
        # repo4 has a pyproject and is installed; repo1 has none and is skipped.
        assert "installed (editable): repo4" in result.stdout, result.stdout
        assert "skipped, no pyproject.toml: repo1" in result.stdout, result.stdout
        assert f"source {env / 'bin' / 'activate'}" in result.stdout, result.stdout

    def test_a_second_create_refuses_and_names_force(self, tmp_path):
        """NO VENV IS BUILT HERE: the refusal is about a path existing.

        Standing an empty directory where the env goes reproduces exactly the
        condition `create` meets after a real build, and it is the condition
        the guard is about -- so this case earns its coverage without a 10s
        installer run. The path is derived through otto's own ``workspace_key``
        rather than spelled out, so the test cannot drift from the product's
        keying.
        """
        home = tmp_path / "home"
        (home / workspace_key([REPO1]) / "env").mkdir(parents=True)

        result = _run(["env", "create"], home=home)
        assert result.returncode != 0, result.stdout
        assert "--force" in (result.stdout + result.stderr)

    @pytest.mark.parametrize("backend", BACKENDS)
    def test_force_rebuilds_and_the_metadata_records_the_backend(self, tmp_path, backend):
        home = tmp_path / "home"
        first = _run(["env", "create", "--backend", backend], home=home, timeout=BUILD_TIMEOUT)
        assert first.returncode == 0, first.stderr

        env = _the_env(home)
        scar = env / "left-behind.txt"
        scar.write_text("from the first build")

        again = _run(
            ["env", "create", "--force", "--backend", backend], home=home, timeout=BUILD_TIMEOUT
        )
        assert again.returncode == 0, again.stderr
        assert not scar.exists(), "--force must remove the old env, not merge into it"

        meta = json.loads((env / ".otto-env.json").read_text())
        assert meta["backend"] == backend, meta

    def test_dry_run_creates_nothing(self, tmp_path):
        """F5: the lab-free dry-run seam stops env before the body and exits 0.

        `otto env create -n` must scaffold no venv, exactly as `otto init -n`
        scaffolds no repo. Passing dry_run_preview=True at registration would
        opt out of that seam and let -n build.
        """
        home = tmp_path / "home"
        result = _run(["-n", "env", "create"], home=home, timeout=BUILD_TIMEOUT)
        assert result.returncode == 0, result.stderr
        assert _the_env(home) is None, f"-n built something: {list(home.rglob('*'))}"


class TestSync:
    def test_sync_with_no_env_behaves_as_create(self, tmp_path):
        """Backend-agnostic delegation, so it runs on the cheap backend only."""
        home = tmp_path / "home"
        result = _run(["env", "sync"], home=home, timeout=BUILD_TIMEOUT)
        assert result.returncode == 0, result.stderr

        env = _the_env(home)
        assert env is not None, f"sync built nothing under {home}"
        assert (env / ".otto-env.json").is_file()

    @pytest.mark.parametrize("backend", BACKENDS)
    def test_passthrough_after_a_double_dash_reaches_the_installer(self, tmp_path, backend, repo4):
        """The contrast IS the assertion: same command, only the passthrough differs.

        repo4 requires ``otto-fixture-beetroot``, which exists on no index (that
        was checked against the live one). So the first create must FAIL, and
        the same create with ``-- --find-links`` must succeed -- which is proof
        the arguments after ``--`` travelled all the way to the resolver, not
        merely that otto parsed them.
        """
        home = tmp_path / "home"
        without = _run(
            ["env", "create", "--backend", backend],
            home=home,
            sut_dirs=f"{REPO1},{repo4}",
            timeout=BUILD_TIMEOUT,
        )
        assert without.returncode != 0, (
            "repo4's requirement is supposed to be unsatisfiable from an index — "
            f"if this passed, the fixture name is resolvable again:\n{without.stdout}"
        )

        with_links = _run(
            [
                "env",
                "create",
                "--force",
                "--backend",
                backend,
                "--",
                "--find-links",
                str(WHEELS_DIR),
            ],
            home=home,
            sut_dirs=f"{REPO1},{repo4}",
            timeout=BUILD_TIMEOUT,
        )
        assert with_links.returncode == 0, with_links.stdout + with_links.stderr

        env = _the_env(home)
        probe = subprocess.run(
            [
                str(env / "bin" / "python"),
                "-c",
                "import otto_fixture_beetroot as b; print(b.beet())",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert probe.returncode == 0, probe.stderr
        assert "otto-fixture-beetroot" in probe.stdout


class TestWorkspaceWithoutInstallableRepos:
    def test_it_still_builds_and_names_what_it_skipped(self, tmp_path):
        """Every sample repo but repo4 lacks a pyproject; that must still work.

        A repo without one is not an error and never has been -- its ``libs``
        ride sys.path at bootstrap. The env is still worth building, because
        otto itself goes into it.
        """
        home = tmp_path / "home"
        result = _run(["env", "create"], home=home, sut_dirs=REPO1, timeout=BUILD_TIMEOUT)
        assert result.returncode == 0, result.stderr

        env = _the_env(home)
        assert env is not None
        assert (env / "bin" / "otto").is_file(), "otto itself must land in the env"
        assert "skipped, no pyproject.toml: repo1" in result.stdout, result.stdout
        assert "installed (editable)" not in result.stdout, result.stdout


class TestShow:
    def test_a_missing_env_is_described_not_an_error(self, tmp_path):
        """`show` is the DIAGNOSTIC verb.

        A diagnostic that fails when things are broken is the one you needed
        most, so "there is no environment" is a described state with the verb
        that fixes it -- not a non-zero exit.
        """
        home = tmp_path / "home"
        result = _run(["env", "show"], home=home)
        assert result.returncode == 0, result.stderr
        assert "no environment for this workspace" in result.stdout
        assert "otto env create" in result.stdout

    def test_it_reports_the_backend_the_version_and_each_repo(self, tmp_path, repo4):
        """Both arms of the installed column, because one arm cannot fail.

        Asserting only that an installed repo reads "yes" passes just as well
        against a column hard-wired to "yes". So the repo is UNINSTALLED from
        the env underneath otto and the same query asked again -- the answer
        has to move.
        """
        home = tmp_path / "home"
        built = _run(
            ["env", "create", "--backend", "uv", "--", "--find-links", str(WHEELS_DIR)],
            home=home,
            sut_dirs=f"{REPO1},{repo4}",
            timeout=BUILD_TIMEOUT,
        )
        assert built.returncode == 0, built.stdout + built.stderr

        shown = _run(["env", "show"], home=home, sut_dirs=f"{REPO1},{repo4}", timeout=BUILD_TIMEOUT)
        assert shown.returncode == 0, shown.stderr
        assert "backend:     uv" in shown.stdout, shown.stdout
        # repo1 has no pyproject at all, so it is not installable and says so.
        assert "no pyproject" in shown.stdout, shown.stdout
        assert _cell(shown.stdout, "otto-sample-repo4", 2) == "yes", shown.stdout

        env = _the_env(home)
        removed = subprocess.run(
            [
                "uv",
                "pip",
                "uninstall",
                "--python",
                str(env / "bin" / "python"),
                "otto-sample-repo4",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert removed.returncode == 0, removed.stderr

        after = _run(["env", "show"], home=home, sut_dirs=f"{REPO1},{repo4}", timeout=BUILD_TIMEOUT)
        assert after.returncode == 0, after.stderr
        assert _cell(after.stdout, "otto-sample-repo4", 2) == "no", after.stdout

    def test_a_repo_touched_after_the_build_reads_as_stale(self, tmp_path, repo4):
        """Staleness is an mtime comparison, so it needs no import and no installer.

        The hostile condition is INJECTED -- the pyproject is touched after the
        build -- rather than inherited from whatever the tree happened to look
        like.
        """
        home = tmp_path / "home"
        built = _run(
            ["env", "create", "--backend", "uv", "--", "--find-links", str(WHEELS_DIR)],
            home=home,
            sut_dirs=f"{REPO1},{repo4}",
            timeout=BUILD_TIMEOUT,
        )
        assert built.returncode == 0, built.stdout + built.stderr

        fresh = _run(["env", "show"], home=home, sut_dirs=f"{REPO1},{repo4}")
        assert "stale" not in fresh.stdout, fresh.stdout

        # Backdate the env stamp to just BEFORE repo4's pyproject rather than by
        # some fixed amount: a fixed offset only makes it stale if the repo
        # happens to have been touched inside that window, which is a property
        # of the checkout, not of the code under test.
        env = _the_env(home)
        stamp = env / ".otto-env.json"
        older = (repo4 / "pyproject.toml").stat().st_mtime - 1
        os.utime(stamp, (older, older))

        stale = _run(["env", "show"], home=home, sut_dirs=f"{REPO1},{repo4}")
        assert "stale" in stale.stdout, stale.stdout
        assert "otto env sync" in stale.stdout, stale.stdout
