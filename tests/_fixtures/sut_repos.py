"""SUT-repo fixtures for tests that run ``run_tests`` (or ``otto test``) in-process.

Shared by ``tests/unit/suite`` and ``tests/unit/cli``: each conftest imports
the fixtures it uses from here, so both directories build repos, wire
``otto.bootstrap.get_repos`` and evict generated modules one way. The repo
double (:func:`repo_double`) is for the tests that stub the pytest session
itself and only need a repo with a test directory.
"""

from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock

import pytest

DOUBLE_TEST_NAME = "test_alpha"
"""The one test every :func:`repo_double` collects: what those runs select by name."""


def repo_double(
    tmp_path: Path,
    *,
    name: str = "repo",
    sut_dir: "Path | None" = None,
    tests: "list[Path] | None" = None,
    settings: "dict | None" = None,
) -> MagicMock:
    """A repo double whose test directories hold the one module-level test ``test_alpha``.

    ``run_tests`` runs a session only for a repo with a test directory on
    disk, so each of *tests* (default ``tmp_path/tests``) gets a ``t.py``
    defining ``test_alpha``. The session itself is ``pytest.main``, which the
    caller stubs; a stub that calls :func:`collected` stands for a session
    that collected and found every name, so the run takes its exit code as
    the verdict.
    """
    repo = MagicMock()
    repo.name = name
    repo.sut_dir = sut_dir if sut_dir is not None else tmp_path
    repo.tests = tests if tests is not None else [tmp_path / "tests"]
    for test_dir in repo.tests:
        test_dir.mkdir(parents=True, exist_ok=True)
        (test_dir / "t.py").write_text(f"def {DOUBLE_TEST_NAME}():\n    pass\n")
    # A real Repo always has settings; the coverage decision reads them.
    repo.settings = settings if settings is not None else {}
    repo.inventory_settings = {}
    return repo


def collected(plugins: "list[object] | tuple[object, ...]") -> None:
    """Make a stubbed ``pytest.main`` stand for a session that collected, and found every name.

    A stub collects nothing. Without this, ``run_tests`` takes its session
    for one whose collection did not finish: when that session was to find
    a name no table places (a cold repo's first run), the run ends there.
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
def one_repo_double(tmp_path, monkeypatch) -> MagicMock:
    """Make a :func:`repo_double` the lab's only repo; return it."""

    repo = repo_double(tmp_path)
    monkeypatch.setattr("otto.bootstrap.get_repos", lambda: [repo])
    return repo


@pytest.fixture
def _generated_modules_evicted(tmp_path, monkeypatch):
    """Drop the modules an in-process pytest session imported from *tmp_path*; restore sys.path.

    ``run_tests`` runs its pytest sessions in THIS interpreter, and pytest's
    default import mode caches a generated ``tests/test_c.py`` as the top-level
    module ``test_c``. A later test that writes its own ``test_c.py`` elsewhere
    would then collide with the stale entry, so every module whose file lives
    under this test's *tmp_path* is evicted afterwards. ``PYTEST_ADDOPTS``
    reaches an inner ``pytest.main`` despite its ``addopts=`` override, and
    pytest-playwright refuses a nested session, so the variable is pinned too.
    """
    import sys

    monkeypatch.setenv("PYTEST_ADDOPTS", "-p no:playwright")
    saved_path = list(sys.path)
    yield
    sys.path[:] = saved_path
    root = str(tmp_path)
    for name, module in list(sys.modules.items()):
        if (getattr(module, "__file__", None) or "").startswith(root):
            del sys.modules[name]


def _wire_repos(monkeypatch, sut_dirs):
    """Make ``otto.bootstrap.get_repos`` answer real ``Repo`` objects for *sut_dirs*."""
    from otto.config.repo import Repo

    repos = [Repo(sut_dir=d) for d in sut_dirs]
    monkeypatch.setattr("otto.bootstrap.get_repos", lambda: repos)
    return repos


@pytest.fixture
def sut_repo(tmp_path, monkeypatch, _generated_modules_evicted):
    """Build one SUT repo (``tests/`` is its test root) and make it the lab's only repo.

    Called as ``sut_repo(files={...})``; returns the repo's directory.
    """
    from tests._fixtures.sutrepo import make_sut_repo

    def build(*, files: dict[str, str], name: str = "sut") -> Path:
        sut = make_sut_repo(tmp_path / name, name=name, tests=["tests"], files=files)
        _wire_repos(monkeypatch, [sut])
        return sut

    return build


@pytest.fixture
def two_sut_repos(tmp_path, monkeypatch, _generated_modules_evicted):
    """Build two SUT repos, ``repo_a`` and ``repo_b``, and make them the lab's repos.

    Called as ``two_sut_repos(a={...}, b={...})``; returns both directories.
    """
    from tests._fixtures.sutrepo import make_sut_repo

    def build(*, a: dict[str, str], b: dict[str, str]) -> list[Path]:
        dirs = [
            make_sut_repo(tmp_path / name, name=name, tests=["tests"], files=files)
            for name, files in (("repo_a", a), ("repo_b", b))
        ]
        _wire_repos(monkeypatch, dirs)
        return dirs

    return build
