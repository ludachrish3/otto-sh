"""Coverage-report browser suite fixtures: a rendered fixture report on disk.

The report is static HTML opened via file:// — no server. The suite pins
the REAL rendered page (templates + report.css + the vite-built
covreport.js), so it needs the actual build: the session guard mirrors the
dashboard suite's (same rationale, see that conftest), pointing at
`make web` when the covreport bundle is missing.
"""

from pathlib import Path

import pytest

import otto.coverage.renderer as renderer_pkg
from tests._fixtures._browser_guard import browser_tests_could_run
from tests._fixtures._report_fixture import build_fixture_report
from tests._fixtures._ts_coverage import (
    collect_ts_coverage,
    start_ts_coverage,
    write_ts_coverage,
)

_COVREPORT_BUNDLE = Path(renderer_pkg.__file__).parent / "static" / "dist" / "covreport.js"


def pytest_configure(config: pytest.Config) -> None:
    """Fail fast with one clear message if the covreport bundle is missing."""
    if not browser_tests_could_run(config):
        return
    if not _COVREPORT_BUNDLE.exists():
        pytest.exit(
            f"coverage-report browser tests need the built frontend bundle "
            f"({_COVREPORT_BUNDLE}); run `make web` first.",
            returncode=1,
        )


@pytest.fixture(scope="session")
def report_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One rendered fixture report per session (tests only read/click it)."""
    base = tmp_path_factory.mktemp("cov_report_fixture")
    return build_fixture_report(base)


@pytest.fixture(scope="session")
def _ts_coverage_sink():
    entries: list[dict] = []
    yield entries
    write_ts_coverage(entries)


@pytest.fixture(autouse=True)
def _ts_coverage(request, _ts_coverage_sink):
    """Per-test V8 coverage; suite-wide accumulation. See _ts_coverage.py.

    Guarded on the `browser` marker BEFORE touching any Playwright fixture
    (mirrors the dashboard suite's conftest, kept byte-identical on purpose):
    a bare `page` parameter would force browser parametrization onto every
    test collected here and pull sync Playwright's event loop into any
    shared hostless CI process this conftest's fixtures end up reused from.
    """
    if (
        request.node.get_closest_marker("browser") is None
        or request.node.get_closest_marker("soak") is not None
    ):
        yield
        return
    if request.getfixturevalue("browser_name") != "chromium":
        yield
        return
    client = start_ts_coverage(request.getfixturevalue("page"))
    yield
    collect_ts_coverage(client, _ts_coverage_sink)
