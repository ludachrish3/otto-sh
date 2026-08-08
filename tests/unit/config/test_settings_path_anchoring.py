"""Repo-root anchoring for the path fields of ``.otto/settings.toml``.

Every path is ``expanduser()``-expanded and, when still relative, resolved
against the repo root — never the process CWD. See
``docs/superpowers/specs/2026-08-01-settings-path-anchoring-design.md``.
"""

import textwrap
from pathlib import Path

from otto.config.repo import Repo
from tests._fixtures.sutrepo import make_sut_repo


def _write_repo(repo_dir: Path, settings_body: str) -> Path:
    """Materialize a minimal SUT repo at *repo_dir* with *settings_body* appended."""
    return make_sut_repo(repo_dir, name="tmp_repo", extra=textwrap.dedent(settings_body).strip())


def test_relative_paths_anchor_to_repo_root_not_cwd(tmp_path, monkeypatch):
    """The core bug: a bare relative path must not depend on where otto was run."""
    sut = _write_repo(
        tmp_path / "repo",
        """
        labs  = ["lab_data"]
        libs  = ["pylib"]
        tests = ["tests"]
        """,
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    repo = Repo(sut_dir=sut)

    assert repo.labs == [sut / "lab_data"]
    assert repo.libs == [sut / "pylib"]
    assert repo.tests == [sut / "tests"]


def test_absolute_paths_pass_through_unchanged(tmp_path):
    """Pins the documented contract: an absolute path in ``settings.toml`` is unchanged.

    This does NOT guard the ``is_absolute()`` early return inside
    ``anchor_to_repo`` — under POSIX join semantics
    ``Path('/a/b') / Path('/tmp/x') == Path('/tmp/x')``, so deleting that
    early return is behavior-identical here (and for the ``~``-rooted case).
    The early return is documentation of intent, not something any test can
    prove necessary; this test exists to pin the *contract* (absolute paths
    pass through unchanged), not the implementation detail.
    """
    shared = tmp_path / "shared" / "pylib"
    sut = _write_repo(tmp_path / "repo", f'libs = ["{shared}"]')

    repo = Repo(sut_dir=sut)

    assert repo.libs == [shared]


def test_parent_relative_escapes_the_repo_root_unresolved(tmp_path):
    """``..`` works, and the join is NOT ``resolve()``d — symlinks must survive."""
    sut = _write_repo(tmp_path / "repo", 'libs = ["../shared/pylib"]')

    repo = Repo(sut_dir=sut)

    assert repo.libs == [sut / ".." / "shared" / "pylib"]


def test_each_repo_anchors_to_its_own_root(tmp_path):
    """Multi-repo (OTTO_SUT_DIRS): identical text, per-repo resolution."""
    a = _write_repo(tmp_path / "a", 'libs = ["pylib"]')
    b = _write_repo(tmp_path / "b", 'libs = ["pylib"]')

    assert Repo(sut_dir=a).libs == [a / "pylib"]
    assert Repo(sut_dir=b).libs == [b / "pylib"]


def test_tilde_path_expands_against_home_not_repo_root(tmp_path, monkeypatch):
    """Guards the ``~`` branch end-to-end through ``Repo``/``SettingsModel``.

    A future edit that drops ``expanduser()``, or moves it to run after the
    ``is_absolute()`` check, would silently stop expanding ``~`` and anchor
    it under the repo root instead of the user's home -- this test fails in
    either case.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    sut = _write_repo(tmp_path / "repo", 'libs = ["~/pylib"]')

    repo = Repo(sut_dir=sut)

    assert repo.libs == [home / "pylib"]
    assert not str(repo.libs[0]).startswith(str(sut))


def test_model_without_context_leaves_relative_paths_unchanged():
    """SettingsModel stays independently validatable with no repo attached."""
    from otto.models.settings import SettingsModel

    model = SettingsModel.model_validate({"name": "x", "version": "1.0.0", "libs": ["pylib"]})

    assert model.libs == [Path("pylib")]


def test_docker_paths_anchor_to_repo_root(tmp_path, monkeypatch):
    """Dockerfile/context/compose paths are documented absolute; enforce it."""
    sut = _write_repo(
        tmp_path / "repo",
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

    repo = Repo(sut_dir=sut)

    image = repo.docker_settings.images[0]
    assert image.dockerfile == sut / "docker" / "api.Dockerfile"
    assert image.context == sut / "docker"
    assert repo.docker_settings.composes[0].path == sut / "docker" / "compose.yml"


def test_monitor_tls_home_convention_survives(tmp_path, monkeypatch):
    """``~`` is the opt-out: it must NOT be swallowed by repo anchoring."""
    home = tmp_path / "home"
    (home / ".config" / "otto" / "tls").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))

    sut = _write_repo(
        tmp_path / "repo",
        """
        [monitor]
        tls_cert = "~/.config/otto/tls/cert.pem"
        tls_key = "~/.config/otto/tls/key.pem"
        """,
    )

    repo = Repo(sut_dir=sut)

    assert repo.monitor_settings.tls_cert == home / ".config" / "otto" / "tls" / "cert.pem"
    assert repo.monitor_settings.tls_key == home / ".config" / "otto" / "tls" / "key.pem"


def test_monitor_tls_relative_anchors_to_repo_root(tmp_path, monkeypatch):
    """A bare relative TLS path resolves under the repo, not the CWD."""
    sut = _write_repo(
        tmp_path / "repo",
        """
        [monitor]
        tls_cert = "certs/bundle.pem"
        """,
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    repo = Repo(sut_dir=sut)

    assert repo.monitor_settings.tls_cert == sut / "certs" / "bundle.pem"
    assert repo.monitor_settings.tls_key is None


def test_sut_dir_variable_is_no_longer_substituted(tmp_path):
    """``${sut_dir}`` is gone: it survives parsing as a literal path segment."""
    sut = _write_repo(tmp_path / "repo", 'libs = ["${sut_dir}/pylib"]')

    repo = Repo(sut_dir=sut)

    assert repo.libs == [sut / "${sut_dir}" / "pylib"]


def test_anchor_path_direct(tmp_path, monkeypatch):
    """Direct test of the anchor_path helper covering relative, absolute, and ~."""
    from otto.utils import anchor_path

    root = tmp_path / "repo"
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
