"""The changed-test repeat's nox sessions run each lane's own selection over the diff's files.

A pull request's CI repeats the test files its diff adds or modifies
(``tests_changed_repeat_*`` in the noxfile; docs/contributing.md, "The
changed-test repeat"). Each session is one lane of the hostless matrix or the
browser lane, narrowed to the diff's files: it must select what its lane
selects, or a test would be repeated under a selection no lane runs it in, or
not repeated at all.

The sessions are driven with a recording double, as
``tests/unit/test_doctest_lane_hermeticity.py`` does: no pytest is spawned.
"""

import sys

import pytest
import yaml

from tests._fixtures.gha_matrix import matrix_rows, render
from tests._fixtures.paths import CHECKOUT_ROOT

pytestmark = pytest.mark.interpreter_agnostic

_COVERAGE_FLAGS = ("--cov-append", "--cov-fail-under=", "--no-cov")
_ENGINES = ("chromium", "firefox", "webkit")
_CI = yaml.safe_load((CHECKOUT_ROOT / ".github" / "workflows" / "ci.yml").read_text())
_REPEAT_STEP = "Repeat the"
# job -> the lane session each row runs, as a template over the row.
_CI_JOBS = {
    "tests-primary": "uv run nox -s ${{ matrix.session }}-3.10",
    "tests-canary": "uv run nox -s ${{ matrix.session }}-3.15",
    "tests": "uv run nox -s tests_hostless_unit-${{ matrix.python }}",
    "dashboard": "uv run nox -k ${{ matrix.browser }}",
}
_LANE_OF = {
    "tests_hostless_unit": "tests_changed_repeat_unit",
    "tests_hostless_e2e": "tests_changed_repeat_e2e",
    "tests_hostless_guards": "tests_changed_repeat_guards",
}


def _noxfile():
    sys.path.insert(0, str(CHECKOUT_ROOT))
    try:
        import noxfile
    finally:
        sys.path.remove(str(CHECKOUT_ROOT))
    return noxfile


class _SkippedError(Exception):
    pass


class _RecordingSession:
    """Answers the file listing with *files* and records every other ``run``."""

    name = "recorded"

    def __init__(self, files: "list[str]") -> None:
        self._files = files
        self.posargs: "list[str]" = []
        self.listings: "list[tuple[str, ...]]" = []
        self.pytests: "list[tuple[tuple[str, ...], dict]]" = []

    def run(self, *args: str, **kwargs: object) -> "str | None":
        if args[:2] == ("python", "scripts/changed_tests.py"):
            self.listings.append(args)
            self.listing_kwargs = kwargs
            return "".join(f"{path}\n" for path in self._files)
        assert args[:1] == ("pytest",), args
        self.pytests.append((args, kwargs))
        return None

    def skip(self, *reason: str) -> None:
        raise _SkippedError(*reason)


def _drive(name: str, files: "list[str]") -> _RecordingSession:
    session = _RecordingSession(files)
    getattr(_noxfile(), name).func.__wrapped__(session)
    return session


def _selection(bundle: "tuple[str, ...]") -> "list[str]":
    """A lane bundle less its root path and its coverage flags: what it selects, and how."""
    return [
        token
        for token in bundle
        if not token.startswith("tests/") and not token.startswith(_COVERAGE_FLAGS)
    ]


def _repeat_selection(bundle: "tuple[str, ...]") -> "list[str]":
    return [token for token in _selection(bundle) if not token.startswith("--count=")]


@pytest.mark.parametrize(
    ("repeat", "lane"),
    [
        ("CHANGED_REPEAT_UNIT_TEST_ARGS", "HOSTLESS_UNIT_TEST_ARGS"),
        ("CHANGED_REPEAT_UNIT_SERIAL_ARGS", "HOSTLESS_UNIT_SERIAL_ARGS"),
        ("CHANGED_REPEAT_GUARDS_ARGS", "HOSTLESS_GUARDS_ARGS"),
        ("CHANGED_REPEAT_E2E_TEST_ARGS", "HOSTLESS_E2E_TEST_ARGS"),
        ("CHANGED_REPEAT_E2E_SERIAL_ARGS", "HOSTLESS_E2E_SERIAL_ARGS"),
    ],
)
def test_a_repeat_bundle_selects_what_its_lane_selects(repeat: str, lane: str) -> None:
    """Same marker expression and same ``-n0``; only the path, coverage and count differ."""
    noxfile = _noxfile()
    assert _repeat_selection(getattr(noxfile, repeat)) == _selection(getattr(noxfile, lane))


def test_the_browser_repeat_selects_what_the_dashboard_session_selects() -> None:
    noxfile = _noxfile()
    assert _repeat_selection(noxfile.CHANGED_REPEAT_BROWSER_ARGS) == [
        "-m",
        noxfile.DASHBOARD_MARKER_EXPR,
    ]


@pytest.mark.parametrize(
    "bundle",
    [
        "CHANGED_REPEAT_UNIT_TEST_ARGS",
        "CHANGED_REPEAT_UNIT_SERIAL_ARGS",
        "CHANGED_REPEAT_GUARDS_ARGS",
        "CHANGED_REPEAT_E2E_TEST_ARGS",
        "CHANGED_REPEAT_E2E_SERIAL_ARGS",
        "CHANGED_REPEAT_BROWSER_ARGS",
    ],
)
def test_a_repeat_bundle_repeats_uninstrumented_and_names_no_path(bundle: str) -> None:
    """A path in the bundle would widen the run from the diff's files to a whole tree."""
    tokens = getattr(_noxfile(), bundle)
    assert "--no-cov" in tokens
    [count] = [token for token in tokens if token.startswith("--count=")]
    assert int(count.removeprefix("--count=")) > 1
    assert not [token for token in tokens if token.startswith("tests/")]


_SESSIONS = {
    "tests_changed_repeat_unit": (
        ["tests/unit"],
        ["CHANGED_REPEAT_UNIT_TEST_ARGS", "CHANGED_REPEAT_UNIT_SERIAL_ARGS"],
    ),
    "tests_changed_repeat_guards": (["tests/unit"], ["CHANGED_REPEAT_GUARDS_ARGS"]),
    "tests_changed_repeat_e2e": (
        ["tests/e2e"],
        ["CHANGED_REPEAT_E2E_TEST_ARGS", "CHANGED_REPEAT_E2E_SERIAL_ARGS"],
    ),
    "tests_changed_repeat_browser": (
        ["tests/e2e/monitor/dashboard", "tests/e2e/cov/report_browser"],
        ["CHANGED_REPEAT_BROWSER_ARGS"],
    ),
}


@pytest.mark.parametrize("name", list(_SESSIONS))
def test_a_session_repeats_exactly_the_files_the_diff_lists_under_its_roots(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CHANGED_TESTS_RANGE", raising=False)
    roots, bundles = _SESSIONS[name]
    files = [f"{roots[0]}/test_one.py", f"{roots[0]}/sub/test_two.py"]

    session = _drive(name, files)

    [listing] = session.listings
    assert list(listing[2:]) == ["--range", "HEAD^1..HEAD", *roots]
    # The captured listing is stdout alone: a warning on stderr is not a path.
    assert session.listing_kwargs == {"silent": True, "stderr": None}
    noxfile = _noxfile()
    assert len(session.pytests) == len(bundles)
    for (argv, kwargs), bundle in zip(session.pytests, bundles, strict=True):
        tokens = list(getattr(noxfile, bundle))
        assert list(argv[1 : 1 + len(files)]) == files
        assert list(argv[1 + len(files) : 1 + len(files) + len(tokens)]) == tokens
        assert not [token for token in argv[1:] if token in ("tests/unit", "tests/e2e", *roots)]
        # 5 is pytest's "no tests collected": every test in the files is another lane's.
        assert kwargs["success_codes"] == [0, 5]


@pytest.mark.parametrize("name", list(_SESSIONS))
def test_a_session_with_no_changed_file_skips_and_runs_no_pytest(name: str) -> None:
    """An empty file list handed to pytest would run the whole suite, repeated."""
    session = _RecordingSession([])
    with pytest.raises(_SkippedError):
        getattr(_noxfile(), name).func.__wrapped__(session)
    assert session.pytests == []


def test_the_range_comes_from_the_environment_when_it_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CHANGED_TESTS_RANGE", "origin/main...HEAD")
    session = _drive("tests_changed_repeat_unit", ["tests/unit/test_one.py"])
    [listing] = session.listings
    assert list(listing[2:4]) == ["--range", "origin/main...HEAD"]


def test_the_hostless_repeats_run_under_the_lanes_leak_environment() -> None:
    noxfile = _noxfile()
    for name in (
        "tests_changed_repeat_unit",
        "tests_changed_repeat_guards",
        "tests_changed_repeat_e2e",
    ):
        session = _drive(name, ["tests/unit/test_one.py"])
        assert all(kwargs["env"] == noxfile._LEAK_ENV for _, kwargs in session.pytests)


def test_the_browser_repeat_keeps_the_dashboard_sessions_failure_artifacts() -> None:
    noxfile = _noxfile()
    session = _drive("tests_changed_repeat_browser", ["tests/e2e/monitor/dashboard/test_one.py"])
    [(argv, _)] = session.pytests
    joined = " ".join(argv)
    assert " ".join(noxfile.DASHBOARD_ARTIFACT_ARGS) in joined


def test_no_repeat_session_is_selected_by_an_engine_keyword() -> None:
    """CI and nightly run ``nox -k <engine>``, which must keep selecting ``dashboard`` alone.

    nox matches ``-k`` against a session's name and its parametrization, so a
    repeat session named or parametrized after an engine would run there too.
    """
    noxfile = _noxfile()
    for name in _SESSIONS:
        func = getattr(noxfile, name)
        assert not [engine for engine in _ENGINES if engine in name]
        assert not getattr(func, "parametrize", None)


def _repeat_step(job: dict) -> dict:
    [step] = [step for step in job["steps"] if step.get("name", "").startswith(_REPEAT_STEP)]
    return step


@pytest.mark.parametrize("job_id", list(_CI_JOBS))
def test_ci_repeats_only_on_a_pull_request_and_after_the_lanes_own_run(job_id: str) -> None:
    """On a push to ``main`` the first parent is the previous landing, not a pull request's base."""
    job = _CI["jobs"][job_id]
    step = _repeat_step(job)
    assert step["if"] == "github.event_name == 'pull_request'"
    runs = [s.get("run") for s in job["steps"]]
    assert runs.index(_CI_JOBS[job_id]) < runs.index(step["run"])


@pytest.mark.parametrize("job_id", list(_CI_JOBS))
def test_ci_checks_out_the_parent_the_repeat_diffs_against(job_id: str) -> None:
    """A depth-1 checkout has no ``HEAD^1``; the session would fail on every pull request."""
    [checkout] = [
        s for s in _CI["jobs"][job_id]["steps"] if s.get("uses", "").startswith("actions/checkout@")
    ]
    assert checkout["with"]["fetch-depth"] >= 2


@pytest.mark.parametrize("job_id", ["tests-primary", "tests-canary", "tests"])
def test_each_hostless_row_repeats_its_own_lane_on_its_own_python(job_id: str) -> None:
    job = _CI["jobs"][job_id]
    step = _repeat_step(job)
    for row in matrix_rows(job):
        lane_run = render(_CI_JOBS[job_id], row)
        lane, python = lane_run.removeprefix("uv run nox -s ").rsplit("-", 1)
        assert render(step["run"], row) == f"uv run nox -s {_LANE_OF[lane]}-{python}"


def test_every_repeat_session_ci_names_exists_for_that_python() -> None:
    """nox exits non-zero on a session it does not know, but only once a pull request runs it."""
    noxfile = _noxfile()
    for job_id in _CI_JOBS:
        job = _CI["jobs"][job_id]
        for row in matrix_rows(job):
            command = render(_repeat_step(job)["run"], row).split(" -- ")[0]
            name, python = command.removeprefix("uv run nox -s ").rsplit("-", 1)
            # The session's own list: a session declared for no Python has no `-<python>` name.
            assert python in getattr(noxfile, name).python, command


def test_the_browser_repeat_runs_on_the_jobs_engine_with_the_lanes_sharding() -> None:
    job = _CI["jobs"]["dashboard"]
    step = _repeat_step(job)
    [lane] = [s for s in job["steps"] if s.get("run") == _CI_JOBS["dashboard"]]
    assert step["env"] == lane["env"]
    assert step["run"].endswith("-- --browser ${{ matrix.browser }}")
    names = [s.get("name", "") for s in job["steps"]]
    [upload] = [n for n in names if n.startswith("Upload failed tests' Playwright traces")]
    assert names.index(step["name"]) < names.index(upload)
