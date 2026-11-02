"""``create``/``sync`` acquire otto's uv once, refuse before any change, and use it throughout.

No installer runs: every exec lands in the ``exec_seam`` fake (``conftest.py``),
and ``uv_bin``/``uv_version`` are counting stubs, so the recorded argvs are
exactly the calls the operation would make to uv.
"""

import sys
from pathlib import Path

import pytest

from otto.env import backends, manage
from otto.env.backends import UvNotFoundError
from otto.env.manage import EnvSelfRebuildError, create_env, read_meta, sync_env
from otto.errors import OttoError

_UV = Path("/o/uv")
_VERSION = "0.13.6"
_PASSTHROUGH = ["--find-links", "/w"]


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
    (root / "pyproject.toml").write_text('[project]\nname = "proj"\n')
    return _Project("proj", root)


@pytest.fixture
def acquired(monkeypatch, exec_seam, env, project):
    """Point create/sync at *env* and one installable project, with uv stubbed."""
    stubs = _Acquired()
    monkeypatch.setattr(backends, "uv_bin", stubs.uv_bin)
    monkeypatch.setattr(backends, "uv_version", stubs.uv_version)
    monkeypatch.setattr(manage, "env_path", lambda: env)
    monkeypatch.setattr(manage, "_discovered_projects", lambda: [project])
    monkeypatch.setattr(manage, "_otto_target", lambda: ["otto-sh==0"])

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
