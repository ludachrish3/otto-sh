"""Project fixtures for tests that run ``run_tests`` (or ``otto test``) in-process.

Shared by ``tests/unit/suite`` and ``tests/unit/cli``: each conftest imports
the fixtures it uses from here, so both directories build projects, wire the
composition root (``otto.bootstrap.bootstrap``, through
:func:`~tests._fixtures.bootstrap_seam.patch_bootstrap`) and evict generated
modules one way. The project
double (:func:`project_double`) is for the tests that stub the pytest session
itself and only need a project with a test directory.
"""

from collections.abc import Callable, Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tests._fixtures.bootstrap_seam import patch_bootstrap

DOUBLE_TEST_NAME = "test_alpha"
"""The one test every :func:`project_double` collects: what those runs select by name."""


def project_double(
    tmp_path: Path,
    *,
    name: str = "project",
    root: "Path | None" = None,
    tests: "list[Path] | None" = None,
    settings: "dict | None" = None,
) -> MagicMock:
    """A project double whose test directories hold the one module-level test ``test_alpha``.

    ``run_tests`` runs a session only for a project with a test directory on
    disk, so each of *tests* (default ``tmp_path/tests``) gets a ``t.py``
    defining ``test_alpha``. The session itself is ``pytest.main``, which the
    caller stubs; a stub that calls :func:`collected` stands for a session
    that collected and found every name, so the run takes its exit code as
    the verdict.
    """
    project = MagicMock()
    project.name = name
    project.root = root if root is not None else tmp_path
    project.tests = tests if tests is not None else [tmp_path / "tests"]
    for test_dir in project.tests:
        test_dir.mkdir(parents=True, exist_ok=True)
        (test_dir / "t.py").write_text(f"def {DOUBLE_TEST_NAME}():\n    pass\n")
    # A real Project always has settings; the coverage decision reads them.
    project.settings = settings if settings is not None else {}
    project.inventory_settings = {}
    # No [project] table, as a real Project without one: a context whose projects
    # are this double resolves the whole-lab fallback, not a Mock's scope.
    project.project_scope = None
    return project


def collected(plugins: "list[object] | tuple[object, ...]") -> None:
    """Make a stubbed ``pytest.main`` stand for a session that collected, and found every name.

    A stub collects nothing. Without this, ``run_tests`` takes its session
    for one whose collection did not finish: when that session was to find
    a name no table places (a cold project's first run), the run ends there.
    """
    from otto.suite.plugin import OttoPlugin

    for plugin in plugins:
        if isinstance(plugin, OttoPlugin):
            plugin.collection_finished = True
            plugin.unmatched_names = []


def pytest_main_returning(rc: int = pytest.ExitCode.OK) -> "Callable[..., int]":
    """A ``pytest.main`` stub: a session that collected (:func:`collected`) and returned *rc*."""

    def fake_main(_args: list[str], plugins: "list[object]" = (), **_kwargs: object) -> int:
        collected(plugins)
        return rc

    return fake_main


@pytest.fixture
def one_project_double(tmp_path, monkeypatch) -> MagicMock:
    """Make a :func:`project_double` the lab's only project; return it."""

    project = project_double(tmp_path)
    patch_bootstrap(monkeypatch, [project])
    return project


TMPFS = Path("/dev/shm")
"""Where a test finds a tmpfs: a directory whose ``st_size`` counts its entries."""


def _tmpfs_unusable() -> "str | None":
    """Why :data:`TMPFS` cannot hold a project whose directories shrink on unlink, or ``None``.

    The capability is probed, not read from the mount table: inside a fresh
    directory there, one file is created and unlinked, and the directory's
    ``st_size`` must have shrunk, as tmpfs's per-entry size does.
    """
    import shutil
    import tempfile

    if not TMPFS.is_dir():
        return f"{TMPFS} is absent"
    try:
        probe = Path(tempfile.mkdtemp(dir=TMPFS))
    except OSError as error:
        return f"{TMPFS} is not writable: {error}"
    try:
        entry = probe / "entry"
        entry.touch()
        before = probe.stat().st_size
        entry.unlink()
        after = probe.stat().st_size
    except OSError as error:
        return f"{TMPFS} probe failed: {error}"
    finally:
        shutil.rmtree(probe, ignore_errors=True)
    if after >= before:
        return f"a directory on {TMPFS} does not shrink on unlink (st_size {before} -> {after})"
    return None


@pytest.fixture
def tmpfs_root() -> Iterator[Path]:
    """A fresh directory on tmpfs, removed afterwards; skips where none is usable.

    On tmpfs a directory's ``st_size`` is 20 bytes per entry, so an unlink moves
    its stat even with the mtime restored, as on a CI runner whose ``/tmp`` is
    tmpfs; ext4 (a dev VM's ``/tmp``) hides that. A missing or read-only tmpfs,
    or one that does not shrink, is a missing filesystem capability: the test
    is skipped with that reason.
    """
    import shutil
    import tempfile

    reason = _tmpfs_unusable()
    if reason is not None:
        pytest.skip(reason)
    root = Path(tempfile.mkdtemp(prefix="otto-tmpfs-", dir=TMPFS))
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def project_root(request, tmp_path) -> Path:
    """The directory :func:`one_project` builds its project in: *tmp_path*, unless overridden.

    A test that needs its project on tmpfs selects it indirectly,
    ``@pytest.mark.parametrize("project_root", ["tmp_path", "tmpfs_root"], indirect=True)``:
    the parameter names the fixture that supplies the root. The suite as a
    whole is not parametrized.
    """
    name = getattr(request, "param", "tmp_path")
    return request.getfixturevalue(name)


@pytest.fixture
def _generated_modules_evicted(tmp_path, project_root, monkeypatch):
    """Drop the modules an in-process session imported from this test's roots; restore sys.path.

    ``run_tests`` runs its pytest sessions in THIS interpreter, and pytest's
    default import mode caches a generated ``tests/test_c.py`` as the top-level
    module ``test_c``. A later test that writes its own ``test_c.py`` elsewhere
    would then collide with the stale entry, so every module whose file lives
    under this test's *tmp_path*, or under its :func:`project_root` when that
    is elsewhere, is evicted afterwards. ``PYTEST_ADDOPTS``
    reaches an inner ``pytest.main`` despite its ``addopts=`` override, and
    pytest-playwright refuses a nested session, so the variable is pinned too.
    """
    import sys

    monkeypatch.setenv("PYTEST_ADDOPTS", "-p no:playwright")
    saved_path = list(sys.path)
    yield
    sys.path[:] = saved_path
    roots = (str(tmp_path), str(project_root))
    for name, module in list(sys.modules.items()):
        if (getattr(module, "__file__", None) or "").startswith(roots):
            del sys.modules[name]


def _wire_projects(monkeypatch, roots):
    """Make the composition root answer real ``Project`` objects for *roots*."""
    from otto.config.project import Project

    projects = [Project(root=d) for d in roots]
    patch_bootstrap(monkeypatch, projects)
    return projects


@pytest.fixture
def one_project(project_root, monkeypatch, _generated_modules_evicted):
    """Build one project (``tests/`` is its test root) and make it the lab's only project.

    Called as ``one_project(files={...})``; returns the project's directory,
    ``<project_root>/<name>`` (:func:`project_root`).
    """
    from tests._fixtures.project_scaffold import make_project

    def build(*, files: dict[str, str], name: str = "sut") -> Path:
        sut = make_project(project_root / name, name=name, tests=["tests"], files=files)
        _wire_projects(monkeypatch, [sut])
        return sut

    return build


@pytest.fixture
def two_projects(tmp_path, monkeypatch, _generated_modules_evicted):
    """Build two projects, ``project_a`` and ``project_b``, and make them the lab's projects.

    Called as ``two_projects(a={...}, b={...})``; returns both directories.
    """
    from tests._fixtures.project_scaffold import make_project

    def build(*, a: dict[str, str], b: dict[str, str]) -> list[Path]:
        dirs = [
            make_project(tmp_path / name, name=name, tests=["tests"], files=files)
            for name, files in (("project_a", a), ("project_b", b))
        ]
        _wire_projects(monkeypatch, dirs)
        return dirs

    return build
