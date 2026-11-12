"""``otto env create`` / ``sync`` / ``show`` through the real binary, building real venvs.

Every build runs the uv installed with otto, the only installer ``otto env``
has, so each case builds once, and only where a venv is the thing under test:
the refusal case builds nothing, because "an environment already exists" is a
question about a path, not about a venv. That the tokens after ``--`` reach
uv's argv verbatim and last is pinned offline and in milliseconds by
``tests/unit/env/test_backends.py::TestInstallerArgv``; the contrast case here
buys only what uv owns, that a real resolver honours them (#402).

Every case pins OTTO_HOME at a tmp_path: without it these would build into the
developer's real ~/.otto.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from otto.config.home import workspace_key
from otto.env.backends import uv_bin, uv_version, venv_python
from otto.errors import OttoError
from otto.version import get_version
from tests._fixtures.paths import CHECKOUT_ROOT, WHEELS_DIR
from tests._fixtures.project_scaffold import copy_sample_project, make_project
from tests.e2e._otto_subprocess import PROJECT1, run_otto

pytestmark = pytest.mark.hostless

PROJECT4 = CHECKOUT_ROOT / "tests" / "project4"

# A real installer run, not otto's own logic: it needs room (a cold wheel cache
# on CI is slower than a warm one). MUST STAY UNDER pyproject's
# per-item `timeout = 180`: at 300 the item's own budget always fired first, so
# a stalled installer arrived as pytest-timeout's blunt signal kill instead of
# the subprocess's own TimeoutExpired, which names the command and its output.
BUILD_TIMEOUT = 150


def _run(argv, *, home, roots=PROJECT1, timeout=60):
    """Run otto with OTTO_HOME pinned, so nothing touches the real ~/.otto."""
    return run_otto(
        argv,
        xdir=None,
        roots=None,
        extra_argv_prefix=["-R"],
        # COLUMNS pins rich's width: a wrapped cell would make the table
        # unreadable to _cell for reasons that have nothing to do with otto.
        extra_env={
            "OTTOPATH": str(roots),
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
def project4(tmp_path):
    """A PRIVATE copy of project4, because these tests install it editable.

    Installing the checked-in tree would write an ``.egg-info`` into
    ``tests/project4/pylib/`` — which project4 puts on ``sys.path`` via its ``libs``,
    where ``importlib.metadata`` reads it as an installed distribution and the
    dependency preflight answers differently than it would on a clean clone.
    ``tests/unit/test_sample_projects_hygiene.py`` is the gate that catches a
    regression here.
    """
    return copy_sample_project(PROJECT4, tmp_path / "project4")


def _uninstall(env, dist):
    """Remove *dist* from *env* underneath otto, with the same uv otto runs."""
    removed = subprocess.run(
        [str(uv_bin()), "pip", "uninstall", "--python", str(venv_python(env)), dist],
        capture_output=True,
        text=True,
        check=False,
    )
    assert removed.returncode == 0, removed.stderr


def _otto_uv_version():
    """The version of the uv the child otto runs: same interpreter, same scripts dir."""
    return uv_version(uv_bin())


class TestCreate:
    def test_it_builds_an_env_holding_otto_and_the_installable_project(self, tmp_path, project4):
        """project4's dependency lives on no index, so the find-links is required.

        That is the fixture working as intended, not a workaround: project4 exists
        to model a project whose Python requirements are not simply there.
        """
        home = tmp_path / "home"
        result = _run(
            ["env", "create", "--", "--find-links", str(WHEELS_DIR)],
            home=home,
            roots=f"{PROJECT1}{os.pathsep}{project4}",
            timeout=BUILD_TIMEOUT,
        )
        assert result.returncode == 0, result.stdout + result.stderr

        env = _the_env(home)
        assert env is not None, f"no env built under {home}"
        assert venv_python(env).is_file(), sorted(q.name for q in env.iterdir())
        # project4 has a pyproject and is installed; project1 has none and is skipped.
        assert "installed (editable): project4" in result.stdout, result.stdout
        assert "skipped, no pyproject.toml: project1" in result.stdout, result.stdout
        assert f"source {env / 'bin' / 'activate'}" in result.stdout, result.stdout
        # The metadata names the uv that actually ran, measured from the binary.
        meta = json.loads((env / ".otto-env.json").read_text())
        assert meta["uv_version"] == _otto_uv_version(), meta

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
        (home / workspace_key([PROJECT1]) / "env").mkdir(parents=True)

        result = _run(["env", "create"], home=home)
        assert result.returncode != 0, result.stdout
        assert "--force" in (result.stdout + result.stderr)

    def test_force_rebuilds(self, tmp_path):
        """``--force`` replaces the env rather than merging into it."""
        home = tmp_path / "home"
        first = _run(["env", "create"], home=home, timeout=BUILD_TIMEOUT)
        assert first.returncode == 0, first.stderr

        env = _the_env(home)
        scar = env / "left-behind.txt"
        scar.write_text("from the first build")

        again = _run(["env", "create", "--force"], home=home, timeout=BUILD_TIMEOUT)
        assert again.returncode == 0, again.stderr
        assert not scar.exists(), "--force must remove the old env, not merge into it"

    def test_dry_run_creates_nothing(self, tmp_path):
        """F5: the lab-free dry-run seam stops env before the body and exits 0.

        `otto env create -n` must scaffold no venv, exactly as `otto init -n`
        scaffolds no project. Passing dry_run_preview=True at registration would
        opt out of that seam and let -n build.
        """
        home = tmp_path / "home"
        result = _run(["-n", "env", "create"], home=home, timeout=BUILD_TIMEOUT)
        assert result.returncode == 0, result.stderr
        assert _the_env(home) is None, f"-n built something: {list(home.rglob('*'))}"


class TestSync:
    def test_sync_with_no_env_behaves_as_create(self, tmp_path):
        """A missing env is built, not refused: ``sync`` is the verb every error names."""
        home = tmp_path / "home"
        result = _run(["env", "sync"], home=home, timeout=BUILD_TIMEOUT)
        assert result.returncode == 0, result.stderr

        env = _the_env(home)
        assert env is not None, f"sync built nothing under {home}"
        assert (env / ".otto-env.json").is_file()

    def test_sync_updates_an_existing_env(self, tmp_path, project4):
        """The in-place branch: an existing env is filled again, never rebuilt.

        Each half of the assertion is INJECTED rather than inherited: project4
        is uninstalled underneath otto (so "installed again" cannot pass on a
        sync that did nothing), a marker sits in the env (so a rebuild would
        lose it), and the metadata is overwritten with versions no real run
        records (so the re-stamp has to happen).
        """
        home = tmp_path / "home"
        roots = f"{PROJECT1}{os.pathsep}{project4}"
        built = _run(
            ["env", "create", "--", "--find-links", str(WHEELS_DIR)],
            home=home,
            roots=roots,
            timeout=BUILD_TIMEOUT,
        )
        assert built.returncode == 0, built.stdout + built.stderr

        env = _the_env(home)
        marker = env / "left-by-the-operator.txt"
        marker.write_text("sync must not remove this")
        _uninstall(env, "otto-sample-project4")
        gone = _run(["env", "show"], home=home, roots=roots)
        assert _cell(gone.stdout, "otto-sample-project4", 2) == "no", gone.stdout
        stamp = env / ".otto-env.json"
        stamp.write_text(json.dumps({"uv_version": "0.0.0", "otto_version": "0.0.0"}))

        synced = _run(
            ["env", "sync", "--", "--find-links", str(WHEELS_DIR)],
            home=home,
            roots=roots,
            timeout=BUILD_TIMEOUT,
        )
        assert synced.returncode == 0, synced.stdout + synced.stderr
        assert f"synced {env}" in synced.stdout, synced.stdout
        assert "last sync:   otto " in synced.stdout, synced.stdout
        assert f", uv {_otto_uv_version()}" in synced.stdout, synced.stdout

        assert marker.exists(), "sync must update the env in place, not rebuild it"
        meta = json.loads(stamp.read_text())
        assert meta["uv_version"] == _otto_uv_version(), meta
        assert meta["otto_version"] == get_version(), meta
        shown = _run(["env", "show"], home=home, roots=roots)
        assert _cell(shown.stdout, "otto-sample-project4", 2) == "yes", shown.stdout

    def test_passthrough_after_a_double_dash_reaches_the_installer(self, tmp_path, project4):
        """The contrast IS the assertion: same command, only the passthrough differs.

        project4 requires ``otto-fixture-beetroot == 0.1.0+otto.fixture``, a local
        version no index can serve (PyPI rejects uploads carrying one, and the
        harness env switches installer config off). So the first create must FAIL, and
        the same create with ``-- --find-links`` must succeed -- which is proof
        the arguments after ``--`` travelled all the way to the resolver, not
        merely that otto parsed them.
        """
        home = tmp_path / "home"
        without = _run(
            ["env", "create"],
            home=home,
            roots=f"{PROJECT1}{os.pathsep}{project4}",
            timeout=BUILD_TIMEOUT,
        )
        assert without.returncode != 0, (
            "project4's requirement is supposed to be unsatisfiable from an index — "
            f"if this passed, the fixture version is resolvable again:\n{without.stdout}"
        )
        # Unsatisfiable for the RIGHT reason: the resolver names the pinned version.
        assert "0.1.0+otto.fixture" in without.stdout + without.stderr

        with_links = _run(
            [
                "env",
                "create",
                "--force",
                "--",
                "--find-links",
                str(WHEELS_DIR),
            ],
            home=home,
            roots=f"{PROJECT1}{os.pathsep}{project4}",
            timeout=BUILD_TIMEOUT,
        )
        assert with_links.returncode == 0, with_links.stdout + with_links.stderr

        env = _the_env(home)
        probe = subprocess.run(
            [
                str(venv_python(env)),
                "-c",
                "import otto_fixture_beetroot as b; print(b.__version__)",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert probe.returncode == 0, probe.stderr
        assert probe.stdout.strip() == "0.1.0+otto.fixture"


class TestWorkspaceWithoutInstallableProjects:
    def test_it_still_builds_and_names_what_it_skipped(self, tmp_path):
        """Every sample project but project4 lacks a pyproject; that must still work.

        A project without one is not an error and never has been -- its ``libs``
        ride sys.path at bootstrap. The env is still worth building, because
        otto itself goes into it.
        """
        home = tmp_path / "home"
        result = _run(["env", "create"], home=home, roots=PROJECT1, timeout=BUILD_TIMEOUT)
        assert result.returncode == 0, result.stderr

        env = _the_env(home)
        assert env is not None
        assert (env / "bin" / "otto").is_file(), "otto itself must land in the env"
        assert "skipped, no pyproject.toml: project1" in result.stdout, result.stdout
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

    def test_it_reports_the_last_sync_and_each_project(self, tmp_path, project4):
        """Both arms of the installed column, because one arm cannot fail.

        Asserting only that an installed project reads "yes" passes just as well
        against a column hard-wired to "yes". So the project is UNINSTALLED from
        the env underneath otto and the same query asked again -- the answer
        has to move.
        """
        home = tmp_path / "home"
        built = _run(
            ["env", "create", "--", "--find-links", str(WHEELS_DIR)],
            home=home,
            roots=f"{PROJECT1}{os.pathsep}{project4}",
            timeout=BUILD_TIMEOUT,
        )
        assert built.returncode == 0, built.stdout + built.stderr

        shown = _run(
            ["env", "show"],
            home=home,
            roots=f"{PROJECT1}{os.pathsep}{project4}",
            timeout=BUILD_TIMEOUT,
        )
        assert shown.returncode == 0, shown.stderr
        assert "last sync:   otto " in shown.stdout, shown.stdout
        assert f", uv {_otto_uv_version()}" in shown.stdout, shown.stdout
        # project1 has no pyproject at all, so it is not installable and says so.
        assert "no pyproject" in shown.stdout, shown.stdout
        assert _cell(shown.stdout, "otto-sample-project4", 2) == "yes", shown.stdout

        _uninstall(_the_env(home), "otto-sample-project4")

        after = _run(
            ["env", "show"],
            home=home,
            roots=f"{PROJECT1}{os.pathsep}{project4}",
            timeout=BUILD_TIMEOUT,
        )
        assert after.returncode == 0, after.stderr
        assert _cell(after.stdout, "otto-sample-project4", 2) == "no", after.stdout

    def test_a_project_touched_after_the_build_reads_as_stale(self, tmp_path, project4):
        """Staleness is an mtime comparison, so it needs no import and no installer.

        The hostile condition is INJECTED -- the pyproject is touched after the
        build -- rather than inherited from whatever the tree happened to look
        like.
        """
        home = tmp_path / "home"
        built = _run(
            ["env", "create", "--", "--find-links", str(WHEELS_DIR)],
            home=home,
            roots=f"{PROJECT1}{os.pathsep}{project4}",
            timeout=BUILD_TIMEOUT,
        )
        assert built.returncode == 0, built.stdout + built.stderr

        fresh = _run(["env", "show"], home=home, roots=f"{PROJECT1}{os.pathsep}{project4}")
        assert "stale" not in fresh.stdout, fresh.stdout

        # Backdate the env stamp to just BEFORE project4's pyproject rather than by
        # some fixed amount: a fixed offset only makes it stale if the project
        # happens to have been touched inside that window, which is a property
        # of the checkout, not of the code under test.
        env = _the_env(home)
        stamp = env / ".otto-env.json"
        older = (project4 / "pyproject.toml").stat().st_mtime - 1
        os.utime(stamp, (older, older))

        stale = _run(["env", "show"], home=home, roots=f"{PROJECT1}{os.pathsep}{project4}")
        assert "stale" in stale.stdout, stale.stdout
        assert "otto env sync" in stale.stdout, stale.stdout

    def test_an_old_record_reads_as_unreadable(self, tmp_path):
        """Metadata from before otto ran its own uv has no ``uv_version``.

        ``show`` names it unreadable and names ``sync`` first, since ``sync``
        needs nothing from it and re-stamps it rather than refusing;
        ``create --force`` comes second, for an env that is actually broken.
        """
        home = tmp_path / "home"
        built = _run(["env", "create"], home=home, timeout=BUILD_TIMEOUT)
        assert built.returncode == 0, built.stdout + built.stderr

        stamp = _the_env(home) / ".otto-env.json"
        stamp.write_text(json.dumps({"backend": "uv", "otto_version": "0.0.0"}))

        old = _run(["env", "show"], home=home)
        assert old.returncode == 0, old.stderr
        line = next(ln for ln in old.stdout.splitlines() if "metadata:    unreadable" in ln)
        assert "otto env sync" in line, line
        assert line.index("otto env sync") < line.index("otto env create --force"), line

        synced = _run(["env", "sync"], home=home, timeout=BUILD_TIMEOUT)
        assert synced.returncode == 0, synced.stdout + synced.stderr

        restamped = _run(["env", "show"], home=home)
        assert "last sync:   otto " in restamped.stdout, restamped.stdout


_VIRTUAL_PYPROJECT = """\
[project]
name = "otto-sample-virtual"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = ["otto-fixture-beetroot>=0.1"]
"""
"""The single-project pyproject docs/installation.md recommends: a ``[project]``
table and NO ``[build-system]``. ``uv sync`` inside the project treats it as a
virtual project; ``otto env`` must too (#554)."""


@pytest.fixture
def virtual_project(tmp_path):
    """A project1-shaped project whose pyproject declares no build backend.

    The SHAPE is the hostile condition: several top-level directories, none of
    them excluded, which is exactly what setuptools' flat-layout auto-discovery
    refuses to build. A project that happened to build under the setuptools
    fallback would prove nothing about never building it.
    """
    return make_project(
        tmp_path / "virtual",
        name="virtual",
        extra='libs = ["pylib"]',
        files={
            "pyproject.toml": _VIRTUAL_PYPROJECT,
            "pylib/virtual_instructions.py": "",
            "product/README": "",
            "docker/Dockerfile": "",
            "tests/test_nothing.py": "",
        },
    )


def _has_dist(env, dist):
    """Whether *dist* is installed in *env*, asked of the env's own interpreter."""
    probe = subprocess.run(
        [str(venv_python(env)), "-c", f"import importlib.metadata as m; m.distribution({dist!r})"],
        capture_output=True,
        text=True,
        check=False,
    )
    return probe.returncode == 0


class TestVirtualProject:
    """A project with a pyproject but no build backend is never built (#554)."""

    def test_create_installs_its_dependencies_and_never_builds_it(
        self, tmp_path, virtual_project, project4
    ):
        """project4 rides along so the package arm (built, editable) is exercised beside it."""
        home = tmp_path / "home"
        roots = os.pathsep.join(str(p) for p in (PROJECT1, virtual_project, project4))
        built = _run(
            ["env", "create", "--", "--find-links", str(WHEELS_DIR)],
            home=home,
            roots=roots,
            timeout=BUILD_TIMEOUT,
        )
        assert built.returncode == 0, built.stdout + built.stderr
        assert "installed dependencies (virtual, not built): virtual" in built.stdout, built.stdout
        assert "installed (editable): project4" in built.stdout, built.stdout

        env = _the_env(home)
        assert _has_dist(env, "otto-fixture-beetroot"), "the virtual project's dependency"
        assert _has_dist(env, "otto-sample-project4"), "the package project, editable"
        assert not _has_dist(env, "otto-sample-virtual"), "a virtual project is never built"

    def test_show_reports_its_dependencies_not_a_dist(self, tmp_path, virtual_project):
        """Both arms: the answer has to MOVE when the dependency is removed underneath otto."""
        home = tmp_path / "home"
        built = _run(
            ["env", "create", "--", "--find-links", str(WHEELS_DIR)],
            home=home,
            roots=str(virtual_project),
            timeout=BUILD_TIMEOUT,
        )
        assert built.returncode == 0, built.stdout + built.stderr

        shown = _run(["env", "show"], home=home, roots=str(virtual_project))
        assert shown.returncode == 0, shown.stderr
        assert _cell(shown.stdout, "otto-sample-virtual", 2) == "deps met", shown.stdout

        _uninstall(_the_env(home), "otto-fixture-beetroot")
        after = _run(["env", "show"], home=home, roots=str(virtual_project))
        assert _cell(after.stdout, "otto-sample-virtual", 2) == "deps missing", after.stdout

    def test_the_hermetic_arm_resolves_offline(self, tmp_path, virtual_project, monkeypatch):
        """``--no-index --find-links`` fails DIFFERENTLY when the project is built.

        Building a project with no backend falls back to setuptools, which an
        offline wheel directory does not hold, so the resolver gives up before
        setuptools' discovery is even reached. Not building it leaves only the
        dependency, which the wheel directory does hold.

        IN-PROCESS, and only because of otto's own install: a hermetic
        ``env create`` through the binary would also have to install otto
        offline, and otto's own dependencies are in no wheel directory the
        suite ships. So otto's install is stood in for by a requirement the
        wheel directory satisfies, and everything else -- the project install,
        the real uv, the real venv -- runs as the command runs it.
        """
        env = _fill_hermetically(
            tmp_path, monkeypatch, virtual_project, ["--find-links", str(WHEELS_DIR)]
        )

        assert _has_dist(env, "otto-fixture-beetroot")
        assert not _has_dist(env, "otto-sample-virtual")

    def test_a_uv_source_is_honoured_not_sent_to_an_index(self, tmp_path, monkeypatch):
        """``uv add --editable ../lib`` writes the dependency's location to
        ``[tool.uv.sources]``, not to ``[project] dependencies``.

        Installing the bare requirement string sends it to an index instead,
        where it is "not found" or, worse, a public package of the same name.
        Here the source is a path to a wheel and the run has NO index and no
        find-links, so only the source can satisfy it.
        """
        wheels = tmp_path / "wheels"
        wheels.mkdir()
        wheel = Path(shutil.copy(_BEETROOT_WHEEL, wheels))
        source = f'otto-fixture-beetroot = {{ path = "../wheels/{wheel.name}" }}'
        root = make_project(
            tmp_path / "sourced",
            name="sourced",
            files={
                "pyproject.toml": f"{_VIRTUAL_PYPROJECT}\n[tool.uv.sources]\n{source}\n",
                "pylib/x.py": "",
                "product/README": "",
            },
        )

        env = _fill_hermetically(tmp_path, monkeypatch, root, [])

        assert _has_dist(env, "otto-fixture-beetroot")
        assert not _has_dist(env, "otto-sample-virtual")

    def test_requires_python_is_enforced_before_installing(
        self, tmp_path, monkeypatch, virtual_project
    ):
        """Installing a package checks its ``requires-python``; a requirements file does not.

        So a virtual project's is checked by otto, against the env's own
        interpreter, and refused before its dependencies go in.
        """
        pyproject = virtual_project / "pyproject.toml"
        pyproject.write_text(
            pyproject.read_text().replace('requires-python = ">=3.10"', 'requires-python = "<3.0"')
        )

        with pytest.raises(OttoError, match="requires-python"):
            _fill_hermetically(
                tmp_path, monkeypatch, virtual_project, ["--find-links", str(WHEELS_DIR)]
            )
        assert not _has_dist(tmp_path / "env", "otto-fixture-beetroot")


_BEETROOT_WHEEL = next(WHEELS_DIR.glob("otto_fixture_beetroot-*.whl"))


def _fill_hermetically(tmp_path, monkeypatch, root, passthrough):
    """Fill a real venv with *root*'s project install, offline, with the real uv.

    ``--no-index`` always: everything the install needs must come from
    *passthrough* or from the project's own declarations. otto's own install
    is stood in for by the fixture wheel's path -- otto's own dependencies are
    in no wheel directory the suite ships.
    """
    from otto.env import backends, manage

    env = tmp_path / "env"
    uv, version = manage._acquire_uv(env)
    manage._relay(backends.create_venv(uv, env), [], "creating the venv")
    monkeypatch.setattr(manage, "_otto_target", lambda: [str(_BEETROOT_WHEEL)])
    project = SimpleNamespace(name=root.name, root=root)
    manage._fill(env, uv, version, manage._plan([project]), ["--no-index", *passthrough])
    return env
