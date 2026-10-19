"""Project-root anchoring for the path fields of ``.otto/settings.toml``.

Every path is ``expanduser()``-expanded and, when still relative, resolved
against the project root — never the process CWD. See
``docs/superpowers/specs/2026-08-01-settings-path-anchoring-design.md``.
"""

import textwrap
from pathlib import Path

import pytest

from otto.config.project import Project
from otto.labs.sources import prepared_lab_sources
from tests._fixtures.project_scaffold import make_project


def _write_project(project_root: Path, settings_body: str) -> Path:
    """Materialize a minimal project at *project_root* with *settings_body* appended."""
    return make_project(
        project_root, name="tmp_project", extra=textwrap.dedent(settings_body).strip()
    )


def test_relative_paths_anchor_to_project_root_not_cwd(tmp_path, monkeypatch):
    """The core bug: a bare relative path must not depend on where otto was run."""
    root = _write_project(
        tmp_path / "proj",
        """
        libs  = ["pylib"]
        tests = ["tests"]

        [[lab.sources]]
        backend = "json"
        paths = ["lab_data"]
        """,
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    project = Project(root=root)

    (lab_source,) = prepared_lab_sources(project)
    assert lab_source.prepared is not None
    assert lab_source.prepared.facts.file_inputs == (root / "lab_data",)
    assert project.libs == [root / "pylib"]
    assert project.tests == [root / "tests"]


def test_absolute_paths_pass_through_unchanged(tmp_path):
    """Pins the documented contract: an absolute path in ``settings.toml`` is unchanged.

    This does NOT guard the ``is_absolute()`` early return inside
    ``anchor_to_project_root`` — under POSIX join semantics
    ``Path('/a/b') / Path('/tmp/x') == Path('/tmp/x')``, so deleting that
    early return is behavior-identical here (and for the ``~``-rooted case).
    The early return is documentation of intent, not something any test can
    prove necessary; this test exists to pin the *contract* (absolute paths
    pass through unchanged), not the implementation detail.
    """
    shared = tmp_path / "shared" / "pylib"
    root = _write_project(tmp_path / "proj", f'libs = ["{shared}"]')

    project = Project(root=root)

    assert project.libs == [shared]


def test_parent_relative_escapes_the_project_root_unresolved(tmp_path):
    """``..`` works, and the join is NOT ``resolve()``d — symlinks must survive."""
    root = _write_project(tmp_path / "proj", 'libs = ["../shared/pylib"]')

    project = Project(root=root)

    assert project.libs == [root / ".." / "shared" / "pylib"]


def test_each_project_anchors_to_its_own_root(tmp_path):
    """Multi-project (OTTOPATH): identical text, per-project resolution."""
    a = _write_project(tmp_path / "a", 'libs = ["pylib"]')
    b = _write_project(tmp_path / "b", 'libs = ["pylib"]')

    assert Project(root=a).libs == [a / "pylib"]
    assert Project(root=b).libs == [b / "pylib"]


def test_tilde_path_expands_against_home_not_project_root(tmp_path, monkeypatch):
    """Guards the ``~`` branch end-to-end through ``Project``/``SettingsModel``.

    A future edit that drops ``expanduser()``, or moves it to run after the
    ``is_absolute()`` check, would silently stop expanding ``~`` and anchor
    it under the project root instead of the user's home -- this test fails in
    either case.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    root = _write_project(tmp_path / "proj", 'libs = ["~/pylib"]')

    project = Project(root=root)

    assert project.libs == [home / "pylib"]
    assert not str(project.libs[0]).startswith(str(root))


def test_model_without_context_leaves_relative_paths_unchanged():
    """SettingsModel stays independently validatable with no project attached."""
    from otto.models.settings import SettingsModel

    model = SettingsModel.model_validate({"name": "x", "version": "1.0.0", "libs": ["pylib"]})

    assert model.libs == [Path("pylib")]


def test_docker_paths_anchor_to_project_root(tmp_path, monkeypatch):
    """Dockerfile/context/compose paths are documented absolute; enforce it."""
    root = _write_project(
        tmp_path / "proj",
        """
        [[docker.images]]
        name = "api"
        dockerfile = "docker/api.Dockerfile"
        context = "docker"

        [[docker.composes]]
        path = "docker/compose.yml"
        """,
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    project = Project(root=root)

    image = project.docker_settings.images[0]
    assert image.dockerfile == root / "docker" / "api.Dockerfile"
    assert image.context == root / "docker"
    assert project.docker_settings.composes[0].path == root / "docker" / "compose.yml"


def test_monitor_tls_home_convention_survives(tmp_path, monkeypatch):
    """``~`` is the opt-out: it must NOT be swallowed by project anchoring."""
    home = tmp_path / "home"
    (home / ".otto" / "tls").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))

    root = _write_project(
        tmp_path / "proj",
        """
        [monitor]
        tls_cert = "~/.otto/tls/cert.pem"
        tls_key = "~/.otto/tls/key.pem"
        """,
    )

    project = Project(root=root)

    assert project.monitor_settings.tls_cert == home / ".otto" / "tls" / "cert.pem"
    assert project.monitor_settings.tls_key == home / ".otto" / "tls" / "key.pem"


def test_monitor_tls_relative_anchors_to_project_root(tmp_path, monkeypatch):
    """A bare relative TLS path resolves under the project, not the CWD."""
    root = _write_project(
        tmp_path / "proj",
        """
        [monitor]
        tls_cert = "certs/bundle.pem"
        """,
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    project = Project(root=root)

    assert project.monitor_settings.tls_cert == root / "certs" / "bundle.pem"
    assert project.monitor_settings.tls_key is None


def test_the_removed_dir_variable_is_no_longer_substituted(tmp_path):
    """``${sut_dir}`` is gone: it survives parsing as a literal path segment."""
    root = _write_project(tmp_path / "proj", 'libs = ["${sut_dir}/pylib"]')

    project = Project(root=root)

    assert project.libs == [root / "${sut_dir}" / "pylib"]


def test_anchor_path_direct(tmp_path, monkeypatch):
    """Direct test of the anchor_path helper covering relative, absolute, and ~."""
    from otto.utils import anchor_path

    root = tmp_path / "proj"
    root.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    # Relative paths anchor to root
    assert anchor_path(Path("lab_data"), root) == root / "lab_data"
    assert anchor_path(Path("a/b/c"), root) == root / "a" / "b" / "c"

    # Absolute paths pass through
    assert anchor_path(Path("/abs/path"), root) == Path("/abs/path")

    # Tilde-rooted paths expand to home, not anchored to root
    assert anchor_path(Path("~/pylib"), root) == home / "pylib"
    assert not str(anchor_path(Path("~/pylib"), root)).startswith(str(root))


def test_anchor_path_refuses_an_unknown_user_as_a_value_error(tmp_path):
    """``~nosuchuser/...`` cannot expand: a ValueError naming the path, so pydantic reports it."""
    from otto.utils import anchor_path

    with pytest.raises(ValueError, match="~nosuchuser_xyz/lib"):
        anchor_path(Path("~nosuchuser_xyz/lib"), tmp_path)
