"""Coverage-report browser suite fixtures: a rendered fixture report on disk.

The report is the covapp SPA, opened via file:// — no server. The suite pins
the REAL rendered page (the built covapp bundle: index.html + dist/), so it
needs the actual build: the session guard mirrors the dashboard suite's
(same rationale, see that conftest — issue #131), pointing at `make web`
when the covapp bundle is missing or stale.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest

from otto import _webassets
from tests._fixtures._browser_guard import browser_tests_could_run
from tests._fixtures._report_fixture import build_fixture_report
from tests._fixtures._ts_coverage import ts_coverage, write_ts_coverage

_COVAPP_INDEX = _webassets.COVAPP / "index.html"

_REPO_ROOT = Path(__file__).resolve().parents[4]
_WEB_SRC = _REPO_ROOT / "web" / "src"


def _stale_dist_reason() -> str | None:
    """Report the newest ``web/src`` file that post-dates the built covapp bundle.

    Mirrors ``tests/e2e/monitor/dashboard/conftest.py``'s ``_stale_dist_reason``
    (see that docstring for the full issue #131 rationale): this suite drives
    the BUILT covapp bundle, not the sources, and ``pytest`` does not build
    it — only ``make web`` does. Editing ``web/src`` and re-running pytest
    directly would otherwise silently re-test the previous bundle.

    Returns ``None`` when the bundle is current (or when there are no web
    sources to compare against, e.g. an installed sdist).
    """
    if not _WEB_SRC.is_dir():
        return None
    if not _COVAPP_INDEX.exists():
        return None  # missing entirely — the existence guard below reports that
    built = _COVAPP_INDEX.stat().st_mtime
    newest, newest_path = 0.0, None
    for path in _WEB_SRC.rglob("*"):
        if not path.is_file():
            continue
        mtime = path.stat().st_mtime
        if mtime > newest:
            newest, newest_path = mtime, path
    if newest_path is None or newest <= built:
        return None
    return (
        f"The built coverage-report (covapp) bundle is STALE: "
        f"{newest_path.relative_to(_REPO_ROOT)} is newer than the bundle these tests "
        f"serve. pytest does not build the web dist — run `make web` first, or you "
        f"will be testing the previous bundle (see issue #131)."
    )


def pytest_configure(config: pytest.Config) -> None:
    """Fail fast with one clear message if the covapp bundle is missing or stale."""
    if not browser_tests_could_run(config):
        return
    if not _COVAPP_INDEX.exists():
        pytest.exit(
            f"coverage-report browser tests need the built covapp bundle "
            f"({_COVAPP_INDEX}); run `make web` first.",
            returncode=1,
        )
    stale = _stale_dist_reason()
    if stale is not None:
        pytest.exit(stale, returncode=1)


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


@pytest.fixture(autouse=True)
def _pageerror_guard(request: pytest.FixtureRequest) -> Iterator[None]:
    """Collect every ``pageerror`` and ``console`` error-type message a test
    triggers, and assert none fired at teardown.

    Every module in this directory (and the CSP lane Task 10 adds to it)
    drives the real built bundle through a real browser — a silent JS
    exception or a swallowed console.error is exactly the kind of failure a
    string-level/unit test can't see; this is the cutover guard for it.
    Scoped to ``browser``-marked tests only (the ``page`` fixture doesn't
    exist otherwise).
    """
    if request.node.get_closest_marker("browser") is None:
        yield
        return
    page = request.getfixturevalue("page")
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
    page.on(
        "console",
        lambda msg: errors.append(f"console.error: {msg.text}") if msg.type == "error" else None,
    )
    yield
    assert not errors, "Unexpected browser error(s):\n" + "\n".join(errors)
