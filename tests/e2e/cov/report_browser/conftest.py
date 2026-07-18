"""Coverage-report browser suite fixtures: a rendered fixture report on disk.

The report is static HTML opened via file:// — no server. The suite pins
the REAL rendered page (templates + report.css + the vite-built
covreport.js), so it needs the actual build: the session guard mirrors the
dashboard suite's (same rationale, see that conftest), pointing at
`make web` when the covreport bundle is missing.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest

import otto.coverage.renderer as renderer_pkg
from tests._fixtures._browser_guard import browser_tests_could_run
from tests._fixtures._report_fixture import build_fixture_report
from tests._fixtures._ts_coverage import ts_coverage, write_ts_coverage

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
def _ts_coverage_sink() -> Iterator[list[dict]]:
    entries: list[dict] = []
    yield entries
    write_ts_coverage(entries)


@pytest.fixture(autouse=True)
def _ts_coverage(request: pytest.FixtureRequest, _ts_coverage_sink: list[dict]) -> Iterator[None]:
    """Autouse wrapper; shared body lives in tests/_fixtures/_ts_coverage.py."""
    yield from ts_coverage(request, _ts_coverage_sink)
