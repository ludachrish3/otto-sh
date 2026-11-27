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

import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from scripts.local_gate_status import REQUIRED_CONTEXTS_FILE, required_contexts
from tests._fixtures.gha_matrix import expand_names, matrix_rows, render
from tests._fixtures.paths import CHECKOUT_ROOT
from tests._fixtures.root_conftest_pytester import outer_session_free_env

pytestmark = pytest.mark.interpreter_agnostic

_COVERAGE_FLAGS = ("--cov-append", "--cov-fail-under=", "--no-cov")
_ENGINES = ("chromium", "firefox", "webkit")
_CI = yaml.safe_load((CHECKOUT_ROOT / ".github" / "workflows" / "ci.yml").read_text())
_REPEAT_STEP = "Repeat the"
_ON_A_PULL_REQUEST = "github.event_name == 'pull_request'"
# lane job -> the lane session each row runs, as a template over the row.
_CI_JOBS = {
    "tests-primary": "uv run nox -s ${{ matrix.session }}-3.10",
    "tests-canary": "uv run nox -s ${{ matrix.session }}-3.14",
    "tests": "uv run nox -s tests_hostless_unit-${{ matrix.python }}",
    "dashboard": "uv run nox -k ${{ matrix.browser }}",
}
# hostless family's repeat job -> its lane job (#1252: a job of its own, not a step).
_REPEAT_JOBS = {
    "repeat-primary": "tests-primary",
    "repeat-canary": "tests-canary",
    "repeat-interior": "tests",
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


class _ErroredError(Exception):
    pass


class _RecordingSession:
    """Answers the file listing with *files* and the collection with what each ``-m`` selects.

    *selects* maps a run's ``-m`` expression to the files it selects; a run it
    does not name selects every file. Every other ``pytest`` call is a run.
    """

    name = "recorded"

    def __init__(self, files: "list[str]", selects: "dict[str, list[str]] | None" = None) -> None:
        self._files = files
        self._selects = selects or {}
        self.posargs: "list[str]" = []
        self.listings: "list[tuple[str, ...]]" = []
        self.collections: "list[tuple[tuple[str, ...], dict]]" = []
        self.pytests: "list[tuple[tuple[str, ...], dict]]" = []
        self.logs: "list[str]" = []
        # When set, the collection prints this instead of the `path: count` lines.
        self.collection_listing: "str | None" = None

    def run(self, *args: str, **kwargs: object) -> "str | None":
        if args[:2] == ("python", "scripts/changed_tests.py"):
            self.listings.append(args)
            self.listing_kwargs = kwargs
            return "".join(f"{path}\n" for path in self._files)
        assert args[:1] == ("pytest",), args
        if "--collect-only" in args:
            self.collections.append((args, kwargs))
            if self.collection_listing is not None:
                return self.collection_listing
            expr = args[args.index("-m") + 1]
            kept = [a for a in args if a in self._selects.get(expr, self._files)]
            return "".join(f"{path}: 3\n" for path in kept)
        self.pytests.append((args, kwargs))
        return None

    def skip(self, *reason: str) -> None:
        raise _SkippedError(*reason)

    def log(self, message: str) -> None:
        self.logs.append(message)

    def error(self, *reason: str) -> None:
        raise _ErroredError(*reason)


def _drive(
    name: str, files: "list[str]", selects: "dict[str, list[str]] | None" = None
) -> _RecordingSession:
    session = _RecordingSession(files, selects)
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
        # A run starts only once its collection found a test, so exit 5 there is red.
        assert kwargs["success_codes"] == [0]


def _expr(bundle: "tuple[str, ...]") -> str:
    return bundle[bundle.index("-m") + 1]


def test_a_file_whose_tests_the_runs_marker_deselects_is_not_repeated() -> None:
    """The repeat plugin multiplies items before ``-m`` deselects any (#1394)."""
    noxfile = _noxfile()
    kept, dropped = "tests/unit/test_kept.py", "tests/unit/test_stability_only.py"
    bundle = noxfile.CHANGED_REPEAT_GUARDS_ARGS
    session = _drive("tests_changed_repeat_guards", [kept, dropped], {_expr(bundle): [kept]})
    [(argv, _)] = session.pytests
    assert kept in argv
    assert dropped not in argv


def test_a_run_whose_marker_selects_no_changed_file_is_not_started() -> None:
    noxfile = _noxfile()
    files = ["tests/unit/test_one.py"]
    serial = _expr(noxfile.CHANGED_REPEAT_UNIT_SERIAL_ARGS)
    session = _drive("tests_changed_repeat_unit", files, {serial: []})
    assert len(session.collections) == 2
    [(argv, _)] = session.pytests
    assert _expr(noxfile.CHANGED_REPEAT_UNIT_TEST_ARGS) in argv
    assert serial not in argv
    assert not [a for a, _ in session.pytests if not [t for t in a[1:] if t.endswith(".py")]], (
        "a pytest call with no path would run the whole suite"
    )


@pytest.mark.parametrize("name", list(_SESSIONS))
def test_the_collection_selects_as_its_run_does(name: str) -> None:
    roots, bundles = _SESSIONS[name]
    files = [f"{roots[0]}/test_one.py"]
    noxfile = _noxfile()
    session = _RecordingSession(files)
    session.posargs = ["--browser", "chromium"]
    getattr(noxfile, name).func.__wrapped__(session)
    assert len(session.collections) == len(bundles)
    for (argv, kwargs), bundle in zip(session.collections, bundles, strict=True):
        tokens = [t for t in getattr(noxfile, bundle) if not t.startswith("--count=")]
        assert list(argv) == [
            "pytest",
            "--collect-only",
            "-qq",
            "-n0",
            *files,
            *tokens,
            "--browser",
            "chromium",
        ]
        assert kwargs["success_codes"] == [0, 5]
        assert kwargs["stderr"] is None
    # The collection runs under the env of its run, and so selects as it does.
    for (_, collection), (r_argv, run), bundle in zip(
        session.collections, session.pytests, bundles, strict=True
    ):
        assert collection["env"] == run["env"]
        # The run spells the same bundle as its collection (which drops only the
        # count), so the two names a session spells cannot drift apart.
        full = list(getattr(noxfile, bundle))
        assert list(r_argv[1 + len(files) : 1 + len(files) + len(full)]) == full
    assert session.collections[0][1]["env"] == (
        None if name == "tests_changed_repeat_browser" else noxfile._LEAK_ENV
    )


# Runaway guard, not a measurement: each child takes a few seconds.
_CHILD_TIMEOUT_S = 300


def _collect_listing(cwd: Path, *args: str) -> "subprocess.CompletedProcess[str]":
    """Run a real ``pytest --collect-only -qq`` in a child and return it.

    The parent run must not steer the child: its env is the outer-session-free
    one (an ambient PYTEST_ADDOPTS ``-v`` would turn ``-qq`` into node ids).
    """
    env = outer_session_free_env()
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-qq",
            "-n0",
            "-p",
            "no:cacheprovider",
            "--no-cov",
            *args,
        ],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=_CHILD_TIMEOUT_S,
    )


def test_a_listing_the_parser_cannot_read_fails_the_session_and_starts_no_run() -> None:
    session = _RecordingSession(["tests/unit/x.py"])
    session.collection_listing = "tests/unit/x.py::test_a\ntests/unit/x.py::test_b\n"
    with pytest.raises(_ErroredError, match=r"(?s)could not be read.*tests/unit/x\.py::test_a"):
        _noxfile().tests_changed_repeat_guards.func.__wrapped__(session)
    assert session.pytests == []


def test_a_blank_listing_starts_no_run_and_is_no_error() -> None:
    session = _RecordingSession(["tests/unit/x.py"])
    session.collection_listing = "\n\n"
    _noxfile().tests_changed_repeat_guards.func.__wrapped__(session)
    assert session.pytests == []
    assert session.logs == ["repeat keeps no file; drops ['tests/unit/x.py']"]


def test_each_run_logs_the_files_it_keeps_and_drops() -> None:
    kept, dropped = "tests/unit/a.py", "tests/unit/b.py"
    noxfile = _noxfile()
    expr = _expr(noxfile.CHANGED_REPEAT_GUARDS_ARGS)
    session = _drive("tests_changed_repeat_guards", [kept, dropped], {expr: [kept]})
    assert session.logs == [f"repeat keeps ['{kept}']; drops ['{dropped}']"]


def test_the_parser_reads_the_listing_under_the_checkouts_own_configuration() -> None:
    """The checkout's ini and conftests apply (no private ini), as in the real lane."""
    this = "tests/unit/test_changed_repeat_lanes.py"
    done = _collect_listing(CHECKOUT_ROOT, "-m", "not stability", this)
    assert done.returncode == 0, done.stdout + done.stderr
    assert _noxfile()._selected_files(done.stdout, [this]) == [this]


def test_the_parser_reads_pytests_own_collect_only_listing(tmp_path: Path) -> None:
    """Pins pytest's ``-qq`` format: a ``path: count`` line per file with a selected item."""
    (tmp_path / "pytest.ini").write_text("[pytest]\nmarkers =\n    stability: soak\n")
    (tmp_path / "test_sweep.py").write_text(
        "import pytest\npytestmark = pytest.mark.stability\n\n\n"
        "def test_a():\n    pass\n\n\ndef test_b():\n    pass\n"
    )
    (tmp_path / "test_plain.py").write_text(
        "def test_a():\n    pass\n\n\ndef test_b():\n    pass\n"
    )
    (tmp_path / "test_mixed.py").write_text(
        "import pytest\n\n\n@pytest.mark.stability\ndef test_a():\n    pass\n\n\n"
        "def test_b():\n    pass\n"
    )
    files = ["test_sweep.py", "test_plain.py", "test_mixed.py"]
    done = _collect_listing(tmp_path, "-c", "pytest.ini", "-m", "not stability", *files)
    assert done.returncode == 0, done.stdout + done.stderr
    assert _noxfile()._selected_files(done.stdout, files) == ["test_plain.py", "test_mixed.py"]


def test_the_parser_ignores_every_line_that_is_not_a_positive_count_for_a_given_file() -> None:
    listing = (
        "a.py: 2\nb.py: 0\nother.py: 4\n\nwarning: something: odd\n"
        "3 tests collected in 0.01s\nc.py: x\n"
    )
    assert _noxfile()._selected_files(listing, ["c.py", "a.py", "b.py"]) == ["a.py"]


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


def _repeat_runs() -> "list[str]":
    """Every rendered repeat command CI runs: each hostless repeat job's rows and the browser's."""
    return [
        render(_repeat_step(_CI["jobs"][job_id])["run"], row)
        for job_id in [*_REPEAT_JOBS, "dashboard"]
        for row in matrix_rows(_CI["jobs"][job_id])
    ]


@pytest.mark.parametrize("job_id", list(_REPEAT_JOBS))
def test_a_hostless_repeat_is_a_job_of_its_own_that_runs_only_on_a_pull_request(
    job_id: str,
) -> None:
    """On a push to ``main`` the first parent is the previous landing, not a pull request's base.

    A job, not a step after the lane, so the lane and the repeat each have a full
    budget (#1252): as a step, a slow changed file timed out a green lane.
    """
    job, lane = _CI["jobs"][job_id], _CI["jobs"][_REPEAT_JOBS[job_id]]
    assert job["if"] == _ON_A_PULL_REQUEST
    assert "if" not in _repeat_step(job)
    assert job["needs"] == ["gate-admission"], "it runs beside its lane, not after it"
    assert job["timeout-minutes"] == lane["timeout-minutes"]
    assert not [s for s in lane["steps"] if s.get("name", "").startswith(_REPEAT_STEP)]


@pytest.mark.parametrize("job_id", list(_REPEAT_JOBS))
def test_a_hostless_repeat_runs_on_its_lanes_python_with_its_lanes_setup_and_env(
    job_id: str,
) -> None:
    """The repeat needs what its lane's step had: the setup action on the Python, and the env."""
    job, lane = _CI["jobs"][job_id], _CI["jobs"][_REPEAT_JOBS[job_id]]
    [setup] = [s for s in job["steps"] if s.get("uses") == "./.github/actions/hostless-setup"]
    [lane_setup] = [s for s in lane["steps"] if s.get("uses") == "./.github/actions/hostless-setup"]
    assert setup["with"] == lane_setup["with"]
    assert job.get("env") == lane.get("env")


def test_the_browser_repeat_is_a_step_after_each_engines_run_only_on_a_pull_request() -> None:
    job = _CI["jobs"]["dashboard"]
    step = _repeat_step(job)
    assert step["if"] == _ON_A_PULL_REQUEST
    runs = [s.get("run") for s in job["steps"]]
    assert runs.index(_CI_JOBS["dashboard"]) < runs.index(step["run"])


@pytest.mark.parametrize("job_id", [*_REPEAT_JOBS, "dashboard"])
def test_ci_checks_out_the_parent_the_repeat_diffs_against(job_id: str) -> None:
    """A depth-1 checkout has no ``HEAD^1``; the session would fail on every pull request."""
    [checkout] = [
        s for s in _CI["jobs"][job_id]["steps"] if s.get("uses", "").startswith("actions/checkout@")
    ]
    assert checkout["with"]["fetch-depth"] >= 2


@pytest.mark.parametrize("job_id", list(_REPEAT_JOBS))
def test_each_hostless_lane_row_has_one_repeat_row_that_repeats_it_on_its_python(
    job_id: str,
) -> None:
    """The repeat job's rows are the lane job's rows, each paired with its lane's repeat."""
    lane_id = _REPEAT_JOBS[job_id]
    job = _CI["jobs"][job_id]
    step = _repeat_step(job)
    lane_runs = []
    for row in matrix_rows(job):
        lane_run = render(_CI_JOBS[lane_id], row)
        lane, python = lane_run.removeprefix("uv run nox -s ").rsplit("-", 1)
        assert render(step["run"], row) == f"uv run nox -s {_LANE_OF[lane]}-{python}"
        lane_runs.append(lane_run)
    assert lane_runs == [
        render(_CI_JOBS[lane_id], row) for row in matrix_rows(_CI["jobs"][lane_id])
    ]


def test_every_repeat_session_ci_names_exists_for_that_python() -> None:
    """nox exits non-zero on a session it does not know, but only once a pull request runs it."""
    noxfile = _noxfile()
    runs = _repeat_runs()
    assert runs
    for command in runs:
        name, python = command.split(" -- ")[0].removeprefix("uv run nox -s ").rsplit("-", 1)
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


_HOSTLESS_CONTEXT = "tests_hostless-"


def _job_sessions(job_id: str) -> "set[str]":
    """The ``nox -s`` sessions (``<name>-<python>``) every row of *job_id* runs."""
    job = _CI["jobs"][job_id]
    rendered = [
        render(str(step["run"]), row)
        for row in matrix_rows(job) or [{}]
        for step in job["steps"]
        if "run" in step
    ]
    return {m for run in rendered for m in re.findall(r"nox -s (\S+-3\.\d+)", run)}


def test_every_required_hostless_context_certifies_its_pythons_lanes_and_repeats() -> None:
    """The required names stay the same, and each still covers both the lane and its repeat.

    For every required ``tests_hostless-<python>`` context: the job that produces it
    needs every job that runs a hostless lane on that Python and every job that
    repeats one there, and between them they run every lane and every repeat.
    """
    required = required_contexts(CHECKOUT_ROOT / REQUIRED_CONTEXTS_FILE)
    contexts = [c for c in required if c.startswith(_HOSTLESS_CONTEXT)]
    assert contexts
    producers = {
        name: job_id for job_id, job in _CI["jobs"].items() for name in expand_names(job_id, job)
    }
    for context in contexts:
        python = context.removeprefix(_HOSTLESS_CONTEXT)
        needs = _CI["jobs"][producers[context]]["needs"]
        sessions = {s for job_id in needs for s in _job_sessions(job_id)}
        here = {s.rsplit("-", 1)[0] for s in sessions if s.endswith(f"-{python}")}
        lanes = {s for s in here if s.startswith("tests_hostless_")}
        assert lanes, context
        assert {_LANE_OF[lane] for lane in lanes} <= here, context
        everywhere = {
            s.rsplit("-", 1)[0]
            for job_id in _CI["jobs"]
            for s in _job_sessions(job_id)
            if s.endswith(f"-{python}")
            and s.startswith(("tests_hostless_", "tests_changed_repeat_"))
            and not s.startswith("tests_changed_repeat_browser")
        }
        assert everywhere <= here, (context, everywhere - here)
