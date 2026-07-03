"""Shared fixtures for tests/unit/monitor."""

from pathlib import Path

import pytest

from otto.monitor import server as server_module


@pytest.fixture(autouse=True)
def _hermetic_static_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point ``_STATIC_DIR`` at a throwaway dist build for every test in this package.

    ``MonitorServer.__init__`` -> ``_build_app`` -> ``_dist_index_path()``
    hard-requires a real ``dist/index.html`` under ``_STATIC_DIR`` (see
    ``otto.monitor.server``) — a deliberate fail-fast for real deployments
    that skipped ``make web``. Most tests in this package construct a
    ``MonitorServer`` to exercise something unrelated (port binding, the
    delete-event route, display-host resolution, ...) and have no interest
    in the dashboard frontend itself, so they shouldn't need a real React
    build in the checkout to pass. This fixture stands in a marker
    ``dist/index.html`` under ``tmp_path`` before each test so construction
    always succeeds, without touching (or depending on) the real
    ``src/otto/monitor/static/``.

    Tests that specifically exercise the *missing*-dist error path (e.g.
    ``TestDashboardRoute.test_raises_when_dist_absent``) call
    ``monkeypatch.setattr(server_module, "_STATIC_DIR", ...)`` again inside
    the test body — that later call on the same ``monkeypatch`` fixture
    simply overrides this one.
    """
    static_dir = tmp_path / "_hermetic_static"
    dist_dir = static_dir / "dist"
    dist_dir.mkdir(parents=True)
    (dist_dir / "index.html").write_text("<html>HERMETIC_TEST_DIST_MARKER</html>")
    monkeypatch.setattr(server_module, "_STATIC_DIR", static_dir)
