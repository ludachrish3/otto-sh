"""``create``/``sync`` acquire otto's uv once, refuse before any change, and use it throughout.

No installer runs: every exec lands in the ``exec_seam`` fake (``conftest.py``),
and ``uv_bin``/``uv_version`` are counting stubs, so the recorded argvs are
exactly the calls the operation would make to uv.
"""

import dataclasses
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from otto.env import backends, manage
from otto.env.backends import UvNotFoundError
from otto.env.backends import venv_python as manage_backends_venv_python
from otto.env.manage import (
    EnvSelfRebuildError,
    EnvUninstallableProjectError,
    SkippedProject,
    create_env,
    is_virtual,
    read_meta,
    sync_env,
)
from otto.errors import OttoError

_UV = Path("/o/uv")
_VERSION = "0.13.6"
_PASSTHROUGH = ["--find-links", "/w"]
_REAL_ENV_PATH = manage.env_path
_PACKAGE_PYPROJECT = (
    '[project]\nname = "proj"\n\n'
    '[build-system]\nrequires = ["setuptools"]\nbuild-backend = "setuptools.build_meta"\n'
)


class _Project:
    """Enough of a Project for create/sync: a name and a directory."""

    def __init__(self, name, root):
        self.name = name
        self.root = Path(root)


class _Acquired:
    """Counting stand-ins for ``uv_bin`` and ``uv_version``."""

    def __init__(self):
        self.path = _UV
        self.bin_calls = 0
        self.version_calls = 0
        self.raises: "BaseException | None" = None
        self.version_raises: "BaseException | None" = None

    def uv_bin(self):
        self.bin_calls += 1
        if self.raises is not None:
            raise self.raises
        return self.path

    def uv_version(self, uv):
        self.version_calls += 1
        if self.version_raises is not None:
            raise self.version_raises
        return _VERSION


@pytest.fixture
def env(tmp_path):
    return tmp_path / "env"


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    # A PACKAGE project: the [build-system] is what makes it one (see is_virtual).
    (root / "pyproject.toml").write_text(_PACKAGE_PYPROJECT)
    return _Project("proj", root)


@pytest.fixture
def acquired(monkeypatch, exec_seam, env, project):
    """Point create/sync at *env* and one installable project, with uv stubbed."""
    stubs = _Acquired()
    monkeypatch.setattr(backends, "uv_bin", stubs.uv_bin)
    monkeypatch.setattr(backends, "uv_version", stubs.uv_version)
    monkeypatch.setattr(manage, "env_path", lambda: env)
    monkeypatch.setattr(manage, "_discovered_projects", lambda: [project])
    monkeypatch.setattr(manage, "_otto_target", lambda otto=None: ["otto-sh==0"])

    def _venv_makes_the_env(argv):
        if argv[1] == "venv":
            Path(argv[2]).mkdir(parents=True)

    exec_seam.effect = _venv_makes_the_env
    return stubs


def _create(env):
    return create_env(passthrough=list(_PASSTHROUGH))


def _create_force(env):
    env.mkdir()
    return create_env(force=True, passthrough=list(_PASSTHROUGH))


def _sync_no_env(env):
    return sync_env(passthrough=list(_PASSTHROUGH))


def _sync_existing(env):
    env.mkdir()
    return sync_env(passthrough=list(_PASSTHROUGH))


OPERATIONS = [
    pytest.param(_create, True, id="create"),
    pytest.param(_create_force, True, id="create-force"),
    pytest.param(_sync_no_env, True, id="sync-no-env"),
    pytest.param(_sync_existing, False, id="sync-existing"),
]


def test_create_records_the_probed_uv_version(acquired, env):
    create_env()
    meta = read_meta(env)
    assert meta is not None
    assert meta.uv_version == _VERSION


@pytest.mark.parametrize(("operation", "builds"), OPERATIONS)
def test_every_uv_call_uses_the_acquired_path(acquired, exec_seam, env, project, operation, builds):
    operation(env)

    kinds = [argv[1:3] for argv in exec_seam]
    expected = [["pip", "install"], ["pip", "install"]]
    if builds:
        expected = [["venv", str(env)], *expected]
    assert kinds == expected, exec_seam
    assert all(argv[0] == str(_UV) for argv in exec_seam), exec_seam
    installs = [argv for argv in exec_seam if argv[1] == "pip"]
    assert "-e" in installs[0]
    assert str(project.root) in installs[0]
    assert "otto-sh==0" in installs[1]
    assert all(argv[-2:] == _PASSTHROUGH for argv in installs), installs


@pytest.mark.parametrize(("operation", "builds"), OPERATIONS)
def test_uv_is_acquired_exactly_once(acquired, env, operation, builds):
    operation(env)
    assert (acquired.bin_calls, acquired.version_calls) == (1, 1)


def _env_with_marker(env):
    env.mkdir()
    marker = env / "marker"
    marker.write_text("still here")
    return marker


def test_force_from_inside_the_env_refuses_before_deleting(acquired, exec_seam, env):
    marker = _env_with_marker(env)
    acquired.path = env / "bin" / "uv"

    with pytest.raises(OttoError, match="outside that env"):
        create_env(force=True)
    assert marker.exists()
    assert exec_seam == []


def test_sync_from_inside_the_env_refuses(acquired, exec_seam, env):
    marker = _env_with_marker(env)
    acquired.path = env / "bin" / "uv"

    with pytest.raises(OttoError, match="outside that env"):
        sync_env()
    assert marker.exists()
    assert exec_seam == []


def _force(env):
    return create_env(force=True)


def _sync(env):
    return sync_env()


REPLACING = [pytest.param(_force, id="create-force"), pytest.param(_sync, id="sync-existing")]


@pytest.mark.parametrize("operation", REPLACING)
def test_an_env_uv_symlinked_outward_refuses(acquired, exec_seam, env, tmp_path, operation):
    """``uv pip install --link-mode symlink`` leaves ``env/bin/uv`` pointing into the
    uv cache: the target survives ``rmtree`` but the path otto calls does not."""
    marker = _env_with_marker(env)
    target = tmp_path / "cache" / "uv"
    target.parent.mkdir()
    target.write_text("")
    link = env / "bin" / "uv"
    link.parent.mkdir()
    link.symlink_to(target)
    acquired.path = link

    with pytest.raises(EnvSelfRebuildError):
        operation(env)
    assert marker.exists()
    assert exec_seam == []


@pytest.mark.parametrize("operation", REPLACING)
def test_otto_running_from_the_env_refuses_with_uv_outside(
    acquired, exec_seam, env, monkeypatch, operation
):
    """A half-installed env whose own uv is gone: ``find_uv_bin`` falls back to a uv
    elsewhere, but otto itself still runs from the env it would replace."""
    marker = _env_with_marker(env)
    monkeypatch.setattr(sys, "prefix", str(env))

    with pytest.raises(EnvSelfRebuildError):
        operation(env)
    assert marker.exists()
    assert exec_seam == []


def test_uv_missing_refuses_before_deleting(acquired, exec_seam, env):
    marker = _env_with_marker(env)
    acquired.raises = UvNotFoundError("no uv")

    with pytest.raises(UvNotFoundError):
        create_env(force=True)
    assert marker.exists()
    assert exec_seam == []


@pytest.mark.parametrize("operation", REPLACING)
def test_a_failed_version_probe_refuses_before_changing_anything(
    acquired, exec_seam, env, operation
):
    """The uv is found but does not report a version: the probe runs before the env
    is touched, so the old env stands and no installer runs."""
    marker = _env_with_marker(env)
    acquired.version_raises = UvNotFoundError("`/o/uv --version` exited 2")

    with pytest.raises(UvNotFoundError):
        operation(env)
    assert marker.exists()
    assert exec_seam == []


@pytest.mark.parametrize("operation", REPLACING)
def test_a_uv_spelled_through_the_env_but_outside_it_is_not_refused(
    acquired, exec_seam, env, operation
):
    """``<env>/../outside/uv`` names a uv beside the env, not in it: ``rmtree`` cannot
    reach it, so the guard must judge the normalized spelling, not the raw one."""
    _env_with_marker(env)
    acquired.path = env / ".." / "outside" / "uv"

    operation(env)
    assert exec_seam, "the operation ran its installs"


def test_a_forced_env_rebuild_spares_otto_xdir(acquired, monkeypatch, tmp_path):
    """``otto env create --force`` removes the workspace's ``env/`` and nothing beside it.

    The run tree, ``$OTTO_HOME/otto-xdir/``, is the one thing under the home
    that is not derived state.
    """
    from otto.home_root import default_xdir

    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    real_env = _REAL_ENV_PATH()
    monkeypatch.setattr(manage, "env_path", lambda: real_env)
    (real_env / "bin").mkdir(parents=True)
    (real_env / "bin" / "stale").write_text("old\n")
    run = default_xdir() / "test" / "20200101_000000_000"
    run.mkdir(parents=True)
    (run / "x.log").write_text("kept\n")

    create_env(force=True)

    assert not (real_env / "bin" / "stale").exists()
    assert (run / "x.log").read_text() == "kept\n"


def _write_project(tmp_path, name, pyproject):
    root = tmp_path / name
    root.mkdir()
    if pyproject is not None:
        (root / "pyproject.toml").write_text(pyproject)
    return _Project(name, root)


_VIRTUAL_PYPROJECT = (
    '[project]\nname = "virt"\nrequires-python = ">=3.10"\n'
    'dependencies = ["beetroot>=1", "turnip; python_version < \'3.0\'"]\n'
)
"""``turnip`` is marker-gated off every Python otto runs on, so which marker
environment judges it decides whether it is required."""

_DYNAMIC_PYPROJECT = '[project]\nname = "virt"\ndynamic = ["dependencies"]\n'


class TestIsVirtual:
    """The one decision, uv's: [tool.uv] package if set, else no [build-system] is virtual."""

    def test_no_build_system_is_virtual(self, tmp_path):
        assert is_virtual(_write_project(tmp_path, "virt", _VIRTUAL_PYPROJECT))

    def test_a_build_system_makes_a_package(self, tmp_path):
        assert not is_virtual(_write_project(tmp_path, "pkg", _PACKAGE_PYPROJECT))

    def test_an_explicit_package_false_wins_over_a_build_system(self, tmp_path):
        """docs/installation.md's file sets it, so it stays virtual if a backend is added."""
        body = _PACKAGE_PYPROJECT + "\n[tool.uv]\npackage = false\n"
        assert is_virtual(_write_project(tmp_path, "virt", body))

    def test_an_explicit_package_true_builds_without_a_build_system(self, tmp_path):
        """uv's rule, not otto's: ``package = true`` asks for a build either way."""
        body = _VIRTUAL_PYPROJECT + "\n[tool.uv]\npackage = true\n"
        assert not is_virtual(_write_project(tmp_path, "pkg", body))

    def test_a_non_boolean_package_is_left_to_uv(self, tmp_path):
        """uv refuses ``package = "no"`` itself ("expected a boolean"), so it gets the -e."""
        body = _VIRTUAL_PYPROJECT + '\n[tool.uv]\npackage = "no"\n'
        assert not is_virtual(_write_project(tmp_path, "odd", body))

    @pytest.mark.parametrize("tool", ["tool = 3\n", "tool = { uv = 3 }\n"])
    def test_a_non_table_tool_does_not_raise(self, tmp_path, tool):
        body = tool + _VIRTUAL_PYPROJECT
        assert is_virtual(_write_project(tmp_path, "virt", body))

    def test_no_pyproject_is_not_virtual(self, tmp_path):
        """It is not installable at all, which is a different answer (skipped)."""
        assert not is_virtual(_write_project(tmp_path, "bare", None))

    def test_an_unreadable_pyproject_is_left_to_the_installer(self, tmp_path):
        """Not virtual, so it is installed as a package and uv refuses it in its own words."""
        assert not is_virtual(_write_project(tmp_path, "broken", "[project\n"))


@pytest.mark.parametrize(("operation", "builds"), OPERATIONS)
def test_a_virtual_project_goes_in_as_a_requirements_file_and_is_never_built(
    acquired, exec_seam, env, monkeypatch, tmp_path, operation, builds
):
    """``-r <pyproject>``: uv reads its dependencies, through its own [tool.uv.sources]."""
    virt = _write_project(tmp_path, "virt", _VIRTUAL_PYPROJECT)
    monkeypatch.setattr(manage, "_discovered_projects", lambda: [virt])

    build = operation(env)

    installs = [argv for argv in exec_seam if argv[1] == "pip"]
    assert len(installs) == 2, installs
    assert installs[0][5:7] == ["-r", str(virt.root / "pyproject.toml")], installs[0]
    assert "-e" not in installs[0], installs[0]
    assert (build.virtual, build.installed, build.skipped) == (["virt"], [], [])


def test_packages_and_virtual_projects_share_one_install(
    acquired, exec_seam, env, monkeypatch, tmp_path, project
):
    """One resolver call for every project, so the env is resolved together."""
    virt = _write_project(tmp_path, "virt", _VIRTUAL_PYPROJECT)
    monkeypatch.setattr(manage, "_discovered_projects", lambda: [project, virt])

    build = create_env()

    installs = [argv for argv in exec_seam if argv[1] == "pip"]
    assert installs[0][-4:] == [
        "-e",
        str(project.root),
        "-r",
        str(virt.root / "pyproject.toml"),
    ], installs[0]
    assert (build.installed, build.virtual) == (["proj"], ["virt"])


@pytest.mark.parametrize(
    ("pyproject", "reason"),
    [
        pytest.param(None, "no pyproject.toml", id="no-pyproject"),
        pytest.param("[tool.ruff]\nline-length = 99\n", "no [project] table", id="no-table"),
    ],
)
def test_a_project_with_nothing_to_install_is_skipped_and_says_why(
    acquired, exec_seam, env, monkeypatch, tmp_path, pyproject, reason
):
    """Skipped means no project install call at all, and the reason travels to the report."""
    bare = _write_project(tmp_path, "bare", pyproject)
    monkeypatch.setattr(manage, "_discovered_projects", lambda: [bare])

    build = create_env()

    installs = [argv for argv in exec_seam if argv[1] == "pip"]
    assert installs == [[str(_UV), "pip", "install", "--python", installs[0][4], "otto-sh==0"]]
    assert build.skipped == [SkippedProject(name="bare", reason=reason)]


def test_a_package_without_a_project_table_is_still_installed(
    acquired, exec_seam, env, monkeypatch, tmp_path
):
    """A build backend can build it (``setup.py``), so it is not the no-table skip."""
    body = '[build-system]\nrequires = ["setuptools"]\nbuild-backend = "setuptools.build_meta"\n'
    pkg = _write_project(tmp_path, "pkg", body)
    monkeypatch.setattr(manage, "_discovered_projects", lambda: [pkg])

    build = create_env()

    assert (build.installed, build.skipped) == (["pkg"], [])


@pytest.mark.parametrize("operation", [*REPLACING, pytest.param(_create, id="create")])
def test_dynamic_dependencies_without_a_backend_refuse_before_anything_changes(
    acquired, exec_seam, env, monkeypatch, tmp_path, operation
):
    """Only a build backend can compute them, and a virtual project has none."""
    virt = _write_project(tmp_path, "virt", _DYNAMIC_PYPROJECT)
    monkeypatch.setattr(manage, "_discovered_projects", lambda: [virt])
    marker = None if operation is _create else _env_with_marker(env)

    with pytest.raises(EnvUninstallableProjectError, match="declare a build backend") as exc:
        operation(env)
    assert "virt" in str(exc.value)
    assert exec_seam == []
    assert marker is None or marker.exists()


def _contents(site, *, dists=frozenset(), python="3.12.1", marker_env=None):
    """What the env's probe would report, with its Python and marker values injected."""
    from packaging.markers import default_environment

    return manage._EnvContents(
        dists=set(dists),
        site_dirs=[site],
        python_version=python,
        marker_env=default_environment() if marker_env is None else marker_env,
    )


class TestRequiresPython:
    """uv does not check a requirements file's requires-python, so otto does (#554)."""

    @pytest.mark.parametrize("operation", [_sync_existing, _create])
    def test_an_env_python_outside_it_refuses_before_any_project_install(
        self, acquired, exec_seam, env, monkeypatch, tmp_path, operation
    ):
        virt = _write_project(tmp_path, "virt", _VIRTUAL_PYPROJECT)
        monkeypatch.setattr(manage, "_discovered_projects", lambda: [virt])
        monkeypatch.setattr(
            manage, "_env_contents", lambda env: _contents(tmp_path, python="3.9.18")
        )

        with pytest.raises(EnvUninstallableProjectError, match=r"requires-python = '>=3\.10'"):
            operation(env)
        assert [argv for argv in exec_seam if argv[1] == "pip"] == []

    def test_an_env_python_inside_it_installs(
        self, acquired, exec_seam, env, monkeypatch, tmp_path
    ):
        virt = _write_project(tmp_path, "virt", _VIRTUAL_PYPROJECT)
        monkeypatch.setattr(manage, "_discovered_projects", lambda: [virt])
        monkeypatch.setattr(
            manage, "_env_contents", lambda env: _contents(tmp_path, python="3.10.0")
        )

        build = create_env()

        assert build.virtual == ["virt"]


def _fabricate_dist(site, name, version):
    """An installed distribution is a dist-info directory with a METADATA file."""
    info = site / f"{name}-{version}.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")


class TestShowsAVirtualProjectByItsDependencies:
    """``env show``'s installed column for a project that never has a dist of its own.

    One site directory per arm, never one mutated between asks:
    ``importlib.metadata`` caches a directory's listing keyed by its mtime, so
    a dist written into an already-read directory within the same mtime tick
    is invisible to the second ask.
    """

    def _answer(self, project, contents):
        dist = manage._dist_name(project)
        return manage._installed(project, dist, is_virtual(project), contents)

    def test_met_and_missing_are_both_reachable(self, tmp_path):
        virt = _write_project(tmp_path, "virt", _VIRTUAL_PYPROJECT)
        empty = tmp_path / "empty"
        empty.mkdir()
        assert self._answer(virt, _contents(empty)) is False, "beetroot is missing"

        full = tmp_path / "full"
        _fabricate_dist(full, "beetroot", "1.2")
        assert self._answer(virt, _contents(full)) is True

    def test_markers_are_judged_in_the_envs_environment_not_ottos(self, tmp_path):
        """The env's Python is whatever uv picked: a marker true there is required there."""
        from packaging.markers import default_environment

        virt = _write_project(tmp_path, "virt", _VIRTUAL_PYPROJECT)
        site = tmp_path / "site"
        _fabricate_dist(site, "beetroot", "1.2")
        old_python = {**default_environment(), "python_version": "2.7"}

        assert self._answer(virt, _contents(site)) is True, "turnip is gated off otto's Python"
        assert self._answer(virt, _contents(site, marker_env=old_python)) is False

    def test_no_marker_values_from_the_env_cannot_be_judged(self, tmp_path):
        virt = _write_project(tmp_path, "virt", _VIRTUAL_PYPROJECT)
        site = tmp_path / "site"
        _fabricate_dist(site, "beetroot", "1.2")
        contents = manage._EnvContents(
            dists=set(), site_dirs=[site], python_version="3.12.1", marker_env=None
        )
        assert self._answer(virt, contents) is None

    def test_a_package_project_still_answers_by_its_dist(self, tmp_path, project):
        site = tmp_path / "site"
        site.mkdir()
        assert self._answer(project, _contents(site, dists={"proj"})) is True
        assert self._answer(project, _contents(site)) is False

    def test_dynamic_dependencies_cannot_be_judged(self, tmp_path):
        virt = _write_project(tmp_path, "virt", _DYNAMIC_PYPROJECT)
        site = tmp_path / "site"
        site.mkdir()
        assert self._answer(virt, _contents(site)) is None


@pytest.mark.parametrize(
    ("pyproject", "advice"),
    [
        pytest.param(
            '[project]\nname = "virt"\ndynamic = ["optional-dependencies"]\n',
            "declare a build backend",
            id="optional-dependencies",
        ),
        pytest.param(
            _DYNAMIC_PYPROJECT
            + _PACKAGE_PYPROJECT.replace('[project]\nname = "proj"\n', "")
            + "\n[tool.uv]\npackage = false\n",
            r"drop `package = false` \(and declare a build backend if it has none\)",
            id="virtual-by-package-false",
        ),
        pytest.param(
            _DYNAMIC_PYPROJECT + "\n[tool.uv]\npackage = false\n",
            r"drop `package = false` \(and declare a build backend if it has none\)",
            id="package-false-and-no-build-system",
        ),
    ],
)
def test_every_field_uv_would_build_to_read_is_refused_with_the_right_advice(
    acquired, exec_seam, env, monkeypatch, tmp_path, pyproject, advice
):
    """uv builds to read dynamic optional-dependencies too, not only dependencies.

    And a project that is virtual only because ``package = false`` overrides
    its [build-system] already HAS a backend: the fix is to stop overriding it.
    """
    virt = _write_project(tmp_path, "virt", pyproject)
    monkeypatch.setattr(manage, "_discovered_projects", lambda: [virt])

    with pytest.raises(EnvUninstallableProjectError, match=advice):
        create_env()
    assert exec_seam == []


def test_a_requires_python_check_that_cannot_run_says_so(
    acquired, exec_seam, env, monkeypatch, tmp_path, caplog
):
    """Skipped is not passed: the operator is told the check did not happen."""
    virt = _write_project(tmp_path, "virt", _VIRTUAL_PYPROJECT)
    monkeypatch.setattr(manage, "_discovered_projects", lambda: [virt])
    monkeypatch.setattr(manage, "_env_contents", lambda env: None)

    with caplog.at_level("WARNING", logger="otto.env.manage"):
        create_env()

    assert any("requires-python check for virt was skipped" in r.message for r in caplog.records)


def test_the_probe_reports_a_pre_release_python_as_pip_compares_it(monkeypatch, capsys, tmp_path):
    """``sys.version_info[:3]``, not ``platform.python_version()``.

    ``>=3.15`` rejects ``3.15.0a5`` as a version, yet pip installs a
    ``requires-python = ">=3.15"`` project on a 3.15 alpha: it compares the
    release triple. Run in THIS interpreter with its version faked.
    """
    import json

    monkeypatch.setattr(sys, "version_info", (3, 15, 0, "alpha", 5))
    monkeypatch.setattr(sys, "version", "3.15.0a5 (main) [GCC]")
    exec(compile(manage._PROBE_ENV, "<probe>", "exec"), {})  # noqa: S102 — otto's own probe text

    reported = json.loads(capsys.readouterr().out)["python"]
    assert reported == "3.15.0"
    monkeypatch.undo()
    body = '[project]\nname = "virt"\nrequires-python = ">=3.15"\n'
    manage._refuse_unsupported_python([_write_project(tmp_path, "virt", body)], reported)


def test_a_string_dynamic_is_not_substring_matched(acquired, exec_seam, env, monkeypatch, tmp_path):
    """``dynamic = "optional-dependencies"`` is malformed, not dynamic: uv refuses it itself.

    A string would substring-match ``dependencies`` too, so both readers are
    asked, and the project is handed to uv rather than refused by otto.
    """
    from otto.env.preflight import dynamic_fields_needing_build, static_dependencies

    body = '[project]\nname = "virt"\ndynamic = "optional-dependencies"\n'
    virt = _write_project(tmp_path, "virt", body)
    monkeypatch.setattr(manage, "_discovered_projects", lambda: [virt])
    table = {"dynamic": "optional-dependencies", "dependencies": ["beetroot"]}

    assert dynamic_fields_needing_build(table) == []
    assert static_dependencies(table) == ["beetroot"]
    build = create_env()
    assert build.virtual == ["virt"], "handed to uv, whose message is the diagnosis"


_REAL_OTTO_TARGET = manage._otto_target
"""The real target builder: the ``acquired`` fixture replaces the module's own."""


@pytest.mark.parametrize(
    ("spec", "requirement"),
    [
        ("0.19.0", "otto-sh==0.19.0"),
        (" 0.19.0 ", "otto-sh==0.19.0"),
        ("0.20.1.dev3", "otto-sh==0.20.1.dev3"),
        ("==0.19.0", "otto-sh==0.19.0"),
        (">=0.19,<0.20", "otto-sh>=0.19,<0.20"),
        (">= 0.19 , < 0.20", "otto-sh>= 0.19 , < 0.20"),
        ("~=0.19.2", "otto-sh~=0.19.2"),
        ("==0.19.*", "otto-sh==0.19.*"),
    ],
)
def test_otto_names_a_version_or_a_specifier(spec, requirement):
    """A bare version means exactly that version; a specifier is used as written."""
    assert manage._otto_target(spec) == [requirement]


@pytest.mark.parametrize(
    "spec",
    [
        "",
        "  ",
        "latest",
        "0.19.0 --index-url http://x",
        "../otto",
        "--no-deps",
        "otto-sh==0.19.0",
        "0.19.0 @ https://example.invalid/otto.whl",
        # SpecifierSet skips an empty part, so each of these passed it and
        # reached the installer malformed, after the env was touched.
        ",",
        ">=0.19,,<0.20",
        ">=0.19,",
        # Arbitrary equality: its operand need not be a version at all.
        "===0.19.0",
        "===--no-deps",
        ">=0.19 ,\n<0.20",
    ],
)
def test_an_otto_that_is_no_version_and_no_specifier_is_refused(spec):
    with pytest.raises(manage.EnvOttoSpecError, match="not a version or a version specifier"):
        manage._otto_target(spec)


def _create_otto(env, otto):
    return create_env(otto=otto, passthrough=list(_PASSTHROUGH))


def _create_force_otto(env, otto):
    env.mkdir()
    return create_env(force=True, otto=otto, passthrough=list(_PASSTHROUGH))


def _sync_no_env_otto(env, otto):
    return sync_env(otto=otto, passthrough=list(_PASSTHROUGH))


def _sync_existing_otto(env, otto):
    env.mkdir()
    return sync_env(otto=otto, passthrough=list(_PASSTHROUGH))


OTTO_OPERATIONS = [
    pytest.param(_create_otto, id="create"),
    pytest.param(_create_force_otto, id="create-force"),
    pytest.param(_sync_no_env_otto, id="sync-no-env"),
    pytest.param(_sync_existing_otto, id="sync-existing"),
]
"""``--otto`` on both verbs: create and sync must treat the same spec the same way."""


@pytest.mark.parametrize("operation", OTTO_OPERATIONS)
def test_otto_installs_the_named_release_not_the_running_otto(
    acquired, exec_seam, env, monkeypatch, operation
):
    """``--otto 0.19.0`` installs ``otto-sh==0.19.0`` as the otto step, on create and on sync.

    The projects' install is unchanged, the operator's passthrough still ends
    the otto install, and nothing names the running otto.
    """
    monkeypatch.setattr(manage, "_otto_target", _REAL_OTTO_TARGET)
    monkeypatch.setattr(manage, "_env_contents", lambda env: None)

    operation(env, "0.19.0")

    installs = [argv for argv in exec_seam if argv[1:3] == ["pip", "install"]]
    assert len(installs) == 2
    otto_step = installs[-1]
    assert otto_step[-len(_PASSTHROUGH) - 1 :] == ["otto-sh==0.19.0", *_PASSTHROUGH]
    assert "-e" not in otto_step


def test_sync_without_otto_still_installs_the_running_otto(acquired, exec_seam, env, monkeypatch):
    seen = []
    monkeypatch.setattr(manage, "_otto_target", lambda otto=None: seen.append(otto) or ["x"])
    sync_env(passthrough=[])
    assert seen == [None]


@pytest.mark.parametrize("operation", OTTO_OPERATIONS)
def test_a_malformed_otto_is_refused_before_anything_changes(
    acquired, exec_seam, env, monkeypatch, operation
):
    """Refused before uv is acquired, so ``create --force`` leaves the old env standing."""
    monkeypatch.setattr(manage, "_otto_target", _REAL_OTTO_TARGET)
    with pytest.raises(manage.EnvOttoSpecError):
        operation(env, "latest")
    assert exec_seam == []
    assert acquired.bin_calls == 0
    assert not env.exists() or not any(env.iterdir())


@pytest.mark.parametrize("operation", OTTO_OPERATIONS)
def test_the_record_names_the_otto_the_installer_chose(
    acquired, env, monkeypatch, tmp_path, operation
):
    """With ``--otto`` the env is asked which otto it holds: a specifier is not a version."""
    monkeypatch.setattr(manage, "_otto_target", _REAL_OTTO_TARGET)
    contents = dataclasses.replace(_contents(tmp_path), otto_version="0.19.4")
    monkeypatch.setattr(manage, "_env_contents", lambda env: contents)

    build = operation(env, ">=0.19,<0.20")

    assert build.meta.otto_version == "0.19.4"
    assert manage.read_meta(env).otto_version == "0.19.4"


def test_without_otto_the_record_names_the_running_otto_and_the_env_is_not_asked(
    acquired, env, monkeypatch
):
    def never(env):
        raise AssertionError("the env was probed for a sync that named no otto")

    monkeypatch.setattr(manage, "_env_contents", never)
    build = sync_env(passthrough=[])
    assert build.meta.otto_version == manage.get_version()


@pytest.mark.parametrize(
    "contents",
    [None, "no-otto"],
    ids=["the-probe-fails", "the-env-holds-no-otto"],
)
def test_an_env_that_cannot_say_which_otto_it_holds_is_recorded_as_unknown(
    acquired, env, monkeypatch, caplog, tmp_path, contents
):
    """The running otto is NOT what ``--otto`` installed, so the record never names it."""
    monkeypatch.setattr(manage, "_otto_target", _REAL_OTTO_TARGET)
    answer = None if contents is None else _contents(tmp_path)
    monkeypatch.setattr(manage, "_env_contents", lambda env: answer)
    with caplog.at_level("WARNING", logger="otto.env.manage"):
        build = sync_env(otto="0.19.0", passthrough=[])
    assert build.meta.otto_version == manage.UNKNOWN_OTTO == "unknown"
    assert manage.read_meta(env).otto_version == "unknown"
    assert "could not ask the environment which otto it now holds" in caplog.text


def test_the_probe_runs_isolated_so_the_env_answers_for_itself(monkeypatch, tmp_path):
    """``-I`` keeps the working directory and PYTHONPATH off the probe's ``sys.path``.

    Without it a distribution visible there, another otto's dist-info for
    one, is reported as the env's own and recorded as the otto installed.
    """
    env = tmp_path / "env"
    python = manage_backends_venv_python(env)
    python.parent.mkdir(parents=True)
    python.write_text("")
    seen = []

    def run(argv, **kwargs):
        seen.append(list(argv))
        return SimpleNamespace(returncode=1, stdout="", stderr="")

    monkeypatch.setattr(manage.subprocess, "run", run)
    assert manage._env_contents(env) is None
    assert seen == [[str(python), "-I", "-c", manage._PROBE_ENV]]


def test_the_probe_reports_the_envs_otto_version(tmp_path):
    """The probe script itself, run by this interpreter: it holds otto, so it names a version."""
    import json
    import subprocess
    from importlib import metadata

    out = subprocess.run(
        [sys.executable, "-I", "-c", manage._PROBE_ENV], capture_output=True, text=True, check=True
    )
    assert json.loads(out.stdout)["otto"] == metadata.version("otto-sh")
