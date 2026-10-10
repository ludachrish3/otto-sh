"""
OttoPlugin — internal pytest plugin registered when otto invokes pytest.main().

Provides the ``pytest_runtest_makereport`` hook that attaches the per-phase
test report to each item (as ``item.rep_setup``, ``item.rep_call``,
``item.rep_teardown``). This makes pass/fail status available to fixtures
during the teardown phase.

When ``sut_test_dirs`` is supplied, the ``pytest_ignore_collect`` hook
restricts collection to only those directories and their descendants,
ensuring that only tests defined in ``OTTO_SUT_DIRS`` repos are run.

Selecting tests by name happens inside the same session, in four steps:

``pytest_ignore_collect``
    With a candidate set, prunes every file that is not a candidate and every
    directory that holds none, so pytest imports only the files the caller
    named. A candidate directory is let through in full: every file in it,
    and every subdirectory the caller's table does not know, with all it
    holds; pytest decides which of those are test files. The session's
    arguments stay the test directories: an explicit file argument would
    bypass a conftest's ``collect_ignore``.

``pytest_itemcollected``
    Records every item as pytest collects it, in collection order, per file
    (:attr:`OttoPlugin.records`, in the collected-tests table's own
    ``otto.config.collected_tests.FileRecord`` shape): its classes and base
    name, and the other files its test is defined in (its dependencies), and
    notes whether a requested name selects it
    (:func:`~otto.suite.selection.matches_name`).

``pytest_collection_modifyitems``
    Before any plain implementation deselects, keeps the selected items and
    reports the rest as deselected; ``-m`` and ``-k`` apply afterwards. The
    items' markers are read once every implementation has run. Then a
    session told which names it must match stops with a usage error when one
    of them matched no item, and so does a session in which a test file or
    conftest registered something (:attr:`OttoPlugin.refusals`): no test
    runs, and the caller, which has the records, says what went wrong.

``pytest_runtestloop``
    Tells the caller, once, that the session is committed to running tests
    (``before_tests``).

``pytest_collectstart`` / ``pytest_collectreport``
    Map each collector to its file, so a module that fails to collect gets
    its ``error``, and a directory that fails (a broken conftest) keeps the
    candidates under it unrecorded. A module that collected adds the files
    its namespace's classes and functions come from to its dependencies.
    Each file and directory the caller has no stat for is stat'ed here,
    before pytest reads or lists it (:attr:`OttoPlugin.dirs`), and so is the
    conftest of each directory taken in full.

Additional hooks:

``pytest_runtest_protocol``
    Implements stability testing (``--iterations`` / ``--duration``).
    Repeats each test item within a single setup/teardown cycle,
    stopping when the iteration or time limit is reached.

``pytest_runtest_call``
    Implements ``@pytest.mark.retry(n)`` by delegating to the shared
    ``otto.suite._retry`` hookwrapper — per-attempt timeout re-arm,
    JUnit/terminal rerun evidence, and double-registration safety live there.

``pytest_runtest_logreport``
    In stability mode, accumulates per-test pass/fail counts into the
    ``StabilityCollector`` attached to the plugin instance.

``pytest_fixture_setup``
    Names each pytest-asyncio runner's loop and closes the hosts that loop
    owns just before the runner closes it (see :mod:`otto.suite.loops`).

Every test, class method or plain function, also gets a start banner in the
log and, under ``--monitor``, a start and an end event on the monitor
timeline, recorded on the test's own loop (``_otto_test_events``).
"""

import asyncio
import contextvars
import functools
import logging
import os
import time
import types
from collections.abc import AsyncGenerator, Callable, Generator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
import pytest_asyncio
from _pytest.runner import call_and_report, show_test_item

from otto.suite._retry import report_retries, retry_hookwrapper
from otto.suite.loops import RUNNER_FIXTURE, runner_for, runner_label, sweep_runner_loop
from otto.suite.pytest_plugin import (
    iteration_dir,
    otto_iteration_key,
    otto_test_dir_base_key,
)

if TYPE_CHECKING:
    from otto.config.collected_tests import FileRecord, RecordedTest
    from otto.monitor.collector import MetricCollector
    from otto.registry import RegistrationRefused

logger = logging.getLogger(__name__)

otto_plugin_key: pytest.StashKey["OttoPlugin"] = pytest.StashKey()
"""Where ``pytest_configure`` parks the plugin instance for its static fixtures.

A class-scoped fixture must not be an instance method — pytest 9.1 deprecates
that form for ANY bound method whose ``__self__`` is not a type, a plugin
object's included (``_pytest.fixtures.resolve_fixture_function``), and pytest
10 makes it an error. So the class-scoped fixtures on this plugin are
staticmethods that find their plugin here, through ``request.config``.
"""


def _python_file(path: object) -> str | None:
    """Return *path* when it names Python a stat can follow: a ``.py``, or a sourceless ``.pyc``.

    A module imported from a sourceless ``.pyc`` has that ``.pyc`` as its
    ``__file__``; replacing it is how such a library changes.
    """
    if isinstance(path, str) and path.endswith((".py", ".pyc")):
        return str(Path(path))
    return None


def _source_file(inspect: Any, obj: object) -> str | None:
    """Return the Python file *obj* is defined in, or ``None`` (built-ins, C, anything else).

    Any exception counts as "no file": a class's metaclass can make reading
    ``__module__`` raise whatever it likes.
    """
    try:
        path = inspect.getfile(obj)
    except Exception:  # noqa: BLE001 - a metaclass can make any read raise anything
        return None
    return _python_file(path)


def _module_name(obj: object) -> str | None:
    """Return the name of the module *obj* was defined in, or ``None`` when that cannot be read.

    Any exception counts as "not known", as for :func:`_source_file`.
    """
    try:
        name = obj.__module__
    except Exception:  # noqa: BLE001 - a metaclass can make any read raise anything
        return None
    return name if isinstance(name, str) else None


_LIB_DEP_STATS: dict[str, list[int] | None] = {}
"""A library file a test comes from -> its stat when a session in this process first met it.

A module from outside the test directories stays imported for the rest of
the process (:meth:`OttoPlugin.imported_modules`), so an edit made to it
later is one this process never sees. A later session records this stat for
it, never the one it has then: the edit leaves its holders' records stale,
and the next process, which imports the library as it is, collects them.

The stat is taken when a session first records the library as a
dependency, which can be after the process imported it (an init module that
imports it, before any session). An edit made between the two is the one
this cannot see; a library caller that edits one starts a new process.
"""


def _within(key: str, root: str) -> bool:
    """Whether path *key* is *root* or below it (string paths, no ``stat``)."""
    return key == root or key.startswith(root.rstrip(os.sep) + os.sep)


@dataclass(frozen=True)
class SelectedTest:
    """One test a session kept once collection was done: the names and ``-m`` selected it."""

    path: Path
    """The file it was collected from, as the session reached it."""
    classes: list[str]
    """The classes it is nested in, outermost first; ``[]`` for a module-level test."""
    name: str
    """Its name as pytest gives it, a parametrization id included (``test_x[a]``)."""


class StabilityCollector:
    """Accumulates per-test pass/fail counts across multiple stability runs."""

    def __init__(self) -> None:
        # Maps test node id → (passed_count, total_count)
        self.results: dict[str, tuple[int, int]] = {}

    def record(self, nodeid: str, passed: bool) -> None:
        """Increment the pass and total counts for *nodeid* by one."""
        prev_passed, prev_total = self.results.get(nodeid, (0, 0))
        self.results[nodeid] = (
            prev_passed + (1 if passed else 0),
            prev_total + 1,
        )


class OttoPlugin:
    """Internal pytest plugin used by ``otto test`` to instrument test runs.

    Parameters
    ----------
    sut_test_dirs :
        Resolved test directories from all configured ``OTTO_SUT_DIRS`` repos
        (i.e. the union of ``Repo.tests`` for every repo). When provided,
        collection is restricted to these directories. Pass an empty list or
        omit to disable filtering.
    stability_collector :
        When running in stability mode, pass a ``StabilityCollector`` instance
        here to accumulate pass/fail counts across repeated runs.
    candidates :
        The files this session may collect, as absolute paths in the form the
        session reaches them (the test directories as the repo lists them,
        joined with each file's relative path). Every other file under a test
        directory, and every directory holding no candidate, is ignored.
        ``None`` collects the whole tree.
    candidate_dirs :
        Directories this session takes in full, with *candidates*: every file
        pytest collects in them, and every subdirectory *known_dirs* does not list,
        with all it holds.
    known_stats, known_dirs :
        The files and the directories the caller already stat'ed before the
        session, each path to its ``[mtime_ns, size]`` (``None``: not there):
        those stats are the ones that apply. The plugin stats what is not in
        them (or was not there) itself, before pytest reads it. ``None``
        knows nothing.
    known_deps :
        The dependencies the caller stat'ed before the session, likewise: a
        library first met here keeps that stat for the rest of the process
        (:attr:`dep_stats`). ``None`` knows nothing.
    names :
        Test, class or ``Class::test`` names to select. After collection only
        the items one of them selects run; the rest are reported deselected.
        ``None`` or empty selects everything collected.
    must_match :
        Names (from *names*) this session must match before it runs anything.
        When one of them matches no collected item, collection ends in a
        ``pytest.UsageError`` that carries no message (pytest prints none):
        no test runs, the session exits 4, and its records still stand, for
        the caller to say which name is unknown (:attr:`unmatched_names`).
        ``None`` runs whatever matched.
    before_tests :
        Called once the session is committed to running tests: after
        collection, with at least one item left and not stopped by a usage
        or collection error, before the first test. An error it raises
        should end the session through ``pytest.exit``.
    announce_seed :
        Log pytest-randomly's seed when the session starts. A caller running
        several sessions on one seed logs it once itself and passes ``False``.

    One plugin serves one session: :attr:`records`, :attr:`unmatched_names`,
    :attr:`selected`, :attr:`registered_markers`, :attr:`refusals`,
    :attr:`stop_reason` and :attr:`exitstatus` describe the session that ran it.
    """

    def __init__(  # noqa: PLR0913 — one plugin per session: each arg is one thing that session is told
        self,
        sut_test_dirs: list[Path] | None = None,
        stability_collector: StabilityCollector | None = None,
        iterations: int = 0,
        duration: int = 0,
        monitor: bool = False,
        monitor_interval: float = 5.0,
        monitor_output: Path | None = None,
        monitor_hosts: str | None = None,
        *,
        candidates: list[Path] | None = None,
        candidate_dirs: list[Path] | None = None,
        known_stats: dict[str, list[int] | None] | None = None,
        known_dirs: dict[str, list[int] | None] | None = None,
        known_deps: dict[str, list[int] | None] | None = None,
        names: list[str] | None = None,
        must_match: list[str] | None = None,
        before_tests: Callable[[], None] | None = None,
        announce_seed: bool = True,
    ) -> None:
        self._sut_test_dirs = sut_test_dirs or []
        self._candidates: set[str] | None = None
        self._ancestors: set[str] = set()
        """Every directory above a candidate file or directory: entered for what is below."""
        self._listed_in_full: set[str] = set()
        """Candidate directories, and the new directories found in them: taken in full."""
        if candidates is not None:
            self._candidates = {str(path) for path in candidates}
            self._listed_in_full = {str(path) for path in candidate_dirs or []}
            within = [*candidates, *(candidate_dirs or [])]
            self._ancestors = {str(parent) for path in within for parent in path.parents}
        # A path the caller saw gone is not known: if it is back by the time
        # pytest reaches it, this plugin stats it before pytest reads it.
        self._known_files: set[str] = {k for k, v in (known_stats or {}).items() if v is not None}
        self._known_dirs: dict[str, list[int]] = {
            k: v for k, v in (known_dirs or {}).items() if v is not None
        }
        self._known_deps = dict(known_deps or {})
        self._file_stats: dict[str, list[int] | None] = {}
        """The stat this plugin took of a file the caller had none for, before pytest read it."""
        self._dir_stats: dict[str, list[int] | None] = {}
        """Each directory taken in full -> its stat from before pytest listed it."""
        self._conftests: dict[str, list[int]] = {}
        """A conftest found in a directory taken in full -> its stat, taken before it loaded."""
        self._names = list(names or [])
        self._must_match = list(must_match or [])
        self._before_tests = before_tests
        self._announce_seed = announce_seed
        self._module_items: dict[str, list[pytest.Item]] = {}
        """Each module's collected items, kept so their markers are read last."""
        self._class_sources: dict[type, set[str]] = {}
        """A test class -> the files its class hierarchy is defined in."""
        self._module_tests: dict[str, list[RecordedTest]] = {}
        """Each module collection reached, by path, in that order -> the tests recorded from it."""
        self._recorded: set[tuple[str, tuple[str, ...], str]] = set()
        """(module path, classes, base name) already recorded: parametrizations collapse."""
        self._deps: dict[str, set[str]] = {}
        """A module's path -> the other files its items' tests are defined in."""
        self._matched: set[str] = set()
        """The requested names some collected item answers to."""
        self._selected: set[str] = set()
        """The nodeids of the items a requested name selects."""
        self._module_of: dict[str, str] = {}
        """A collector's nodeid -> the path of the module it belongs to."""
        self._modules: dict[str, pytest.Module] = {}
        """A module collector's nodeid -> the collector, until its report says it collected."""
        self._dir_of: dict[str, Path] = {}
        """A directory collector's nodeid -> its path."""
        self._errors: dict[str, str] = {}
        """A module's path -> why it failed to collect."""
        self._imported: dict[str, str] = {}
        """Each module the session met, by name -> its file: collected modules, conftests and
        the modules the tests are defined in (see :meth:`imported_modules`)."""
        self._failed_dirs: list[Path] = []
        """Directories whose collection failed: nothing under them was reached."""
        self.collection_finished = False
        """Whether the session got through collection, every ``modifyitems`` included.

        Only then are the records complete (their markers are read last); a
        later ``modifyitems`` that raises leaves this ``False``, and the run
        writes nothing from that session.
        """
        self.records: dict[str, FileRecord] = {}
        """What each file the session considered holds, keyed by its absolute path.

        Every collected module, empty or not, plus every non-conftest
        candidate pytest itself declined to collect (a conftest's
        ``collect_ignore``, say) as an empty record, plus the conftest of each
        directory taken in full that the caller had no stat for; a candidate a
        failed directory kept out of reach gets none. A record's ``stat`` is
        the one this plugin took before pytest read the file, or ``None`` when
        *known_stats* has it (that stat, taken before the session, applies). Empty
        until collection finishes.
        """
        self.dep_stats: dict[str, list[int] | None] = {}
        """Each library a record depends on that an earlier session in this process met -> the
        stat to record for it: the one it was first met at (see ``_LIB_DEP_STATS``). A library
        met for the first time here is left to the caller's stat. Filled once collection
        finishes."""
        self.dirs: dict[str, list[int] | None] = {}
        """Each directory the session took in full -> the stat it was listed at.

        ``None`` when its listing did not complete (a conftest at or under it
        failed). See ``otto.config.collected_tests.updated_table`` for what the
        table does with each. Filled once collection finishes."""
        self.unmatched_names: list[str] = list(self._names)
        """The requested names no collected item answers to, in request order.

        Matched before ``-m``/``-k`` deselect anything: a name those exclude
        is still a known name. Until collection finishes, every name."""
        self.refusals: list[RegistrationRefused] = []
        """The registrations refused while the session loaded test files and conftests.

        Collecting runs under :func:`otto.registry.loading_test_files`: a test
        file or conftest may register nothing. A refusal is a defect in the
        repo, not a broken file to run past, so the session stops before any
        test (a usage error, as for a required name) and its caller raises
        the first refusal."""
        self.stop_reason: str | None = None
        """Why the session stopped before its collection finished, when pytest said.

        A conftest that failed to load, a ``pytest.exit``, an interrupt or an
        internal error: the first of them. ``None`` for a session that stopped
        for none of these."""
        self.exitstatus: int | None = None
        """The session's exit status, once it finishes (``pytest_sessionfinish``).

        Known before pytest prints its summary, so what reads the session's
        output then (the run's JUnit line) can tell a session that ran tests
        from one that stopped or had none."""
        self.selected: list[SelectedTest] = []
        """The tests the session kept once collection was done, in its order.

        What the requested names, ``-m``, ``-k`` and every plugin and conftest
        left in ``session.items``: in a ``--collect-only`` session, exactly
        what a run would have run. Empty until collection finishes."""
        self.registered_markers: list[str] = []
        """Every marker name pytest knew at the end of collection, sorted.

        Declared in a pytest config file, registered by a plugin or a conftest
        the session loaded, and pytest's own. Read then, not in
        ``pytest_configure``: pytest's built-in plugins and the conftests of
        nested directories register theirs after this plugin is configured."""
        self._stability_collector = stability_collector
        self._iterations = iterations
        self._duration = duration
        self._monitor = monitor
        self._monitor_interval = monitor_interval
        self._monitor_output = monitor_output
        self._monitor_hosts = monitor_hosts
        self._seed: int | None = None
        self.session_monitor_collector: "MetricCollector | None" = None
        """The session-wide collector under ``otto test --monitor``; ``None`` otherwise.

        Set and cleared by ``_otto_session_monitor``. The per-test events and
        the per-class collection task read it here, through
        ``request.config.stash[otto_plugin_key]`` where they have no ``self``."""

    def pytest_configure(self, config: pytest.Config) -> None:
        """Enforce auto asyncio mode for the tests otto runs.

        ``otto test`` sessions always run with ``asyncio_mode=auto`` so that
        async fixtures and tests work without explicit ``@pytest.mark.asyncio``
        markers.  This is distinct from otto's own unit tests which use
        ``asyncio_mode=strict`` (set in ``pyproject.toml``).

        Per-test timeouts are handled by ``pytest-timeout`` (a runtime
        dependency), which honors ``@pytest.mark.timeout(seconds)`` natively.
        """
        config.option.asyncio_mode = "auto"
        config.stash[otto_plugin_key] = self
        # pytest-randomly resolves its seed in ITS pytest_configure (a drawn
        # int, `--randomly-seed=N`, or `last`) and writes the int back onto
        # config.option. This hook runs after it — plugins passed to
        # pytest.main(plugins=...) register after entry-point plugins — so the
        # value is final here. READ here, LOGGED in pytest_sessionstart: see
        # the note there. `default=None` is load-bearing: under `-p no:randomly`
        # the option is unregistered and getoption raises on an undeclared
        # name unless a default is supplied.
        self._seed = config.getoption("randomly_seed", default=None)

    def class_monitor_interval(self) -> float | None:
        """Seconds between monitor samples while a class runs; ``None`` with ``--monitor`` off.

        The one thing ``_otto_class_monitor_task`` needs from its plugin.
        """
        return self._monitor_interval if self._monitor else None

    def pytest_sessionstart(self, session: pytest.Session) -> None:
        r"""Quiet down pytest's terminal reporter output.

        Two adjustments, both because otto streams its own Rich log output
        and pytest's terse terminal chatter just collides with it. Done here
        rather than in ``pytest_configure`` because the terminalreporter
        isn't registered yet at configure time.

        ``showfspath = False``: in non-verbose mode pytest writes the test
        file path with no trailing newline (``write_fspath_result``),
        expecting per-test progress letters to follow. otto suppresses those
        letters (see :meth:`pytest_report_teststatus`), so the bare path
        would collide with the first log line. otto's ``_otto_test_events``
        fixture already logs each test start, making the header redundant.

        ``report_collect``: the "collected N items" line has no granular
        suppression flag — only quiet mode (``verbose < 0``) hides it, which
        would strip other output too. The ``pytest_collection`` hook writes a
        bare, un-terminated ``collecting ...`` prefix that ``report_collect``
        normally rewrites in place into ``collected N items\\n``; simply
        no-oping it would leave that prefix dangling. Instead override it to
        erase the line on the final call and park the cursor at column 0 for
        the next writer. Collection counts are tracked separately and stay
        intact.
        """
        # The seed line. The runner passes --no-header, which hides
        # pytest-randomly's own "Using --randomly-seed=N", so this is the only
        # place a user learns the seed to pass back as `--seed N`. Emitted HERE
        # and not in pytest_configure, where the value was read: pytest's
        # logging plugin arms its capture handlers around sessionstart,
        # collection and the run loop, and nothing earlier — a record emitted
        # at configure time escapes the session and lands in whatever handlers
        # the ENCLOSING process has. Invisible in production; in a pytester
        # in-process run it broke the inner session's output capture outright.
        if self._seed is not None and self._announce_seed:
            logger.info(
                "random test order, seed %s (reproduce with --seed %s)", self._seed, self._seed
            )
        tr = session.config.pluginmanager.get_plugin("terminalreporter")
        if tr is not None:
            tr.showfspath = False

            def _erase_collect_line(final: bool = False) -> None:
                if final and tr.isatty():
                    tr.rewrite("", erase=True)
                    tr.write("\r")

            tr.report_collect = _erase_collect_line

    def _outside_the_test_dirs(self, path: Path) -> bool:
        """Whether *path* is neither in a SUT test dir nor above one; ``False`` with no dirs."""
        if not self._sut_test_dirs:
            return False
        return not any(
            path.is_relative_to(test_dir) or test_dir.is_relative_to(path)
            for test_dir in self._sut_test_dirs
        )

    @pytest.hookimpl(tryfirst=True)
    def pytest_ignore_collect(
        self,
        collection_path: Path,
        config: pytest.Config,  # noqa: ARG002 — required by pytest hook signature
    ) -> bool | None:
        """Ignore any path not under a configured SUT test directory, or not a candidate.

        Returns ``True`` (ignore) for paths outside all SUT test dirs, and,
        with a candidate set, for every path that is neither a candidate, nor
        a directory above one, nor in a directory taken in full (a
        subdirectory there that the table knows is left to its own stat, unless
        it is a candidate or above one). Returns ``None`` (let pytest decide,
        so a conftest's ``collect_ignore`` still applies) otherwise. The
        decision is made from the path alone, without a ``stat``.

        ``tryfirst``: this decision comes before any other plugin's.
        """
        if self._outside_the_test_dirs(collection_path):
            return True
        key = str(collection_path)
        parent = str(collection_path.parent)
        if self._candidates is None:
            return None
        if key in self._candidates or key in self._listed_in_full:
            return None
        if parent in self._listed_in_full and key not in self._known_dirs:
            # A file here, or a directory the table does not know: new, so
            # everything in it is taken too.
            self._listed_in_full.add(key)
            return None
        return None if key in self._ancestors else True

    def pytest_collectstart(self, collector: pytest.Collector) -> None:
        """Note which file (or directory) each collector stands for, before it collects.

        Before a directory's conftest loads and before pytest lists it, and
        before a module is imported: so a stat taken here is from before the
        read, the direction that can only make a record look stale.
        """
        if isinstance(collector, pytest.Directory):
            self._dir_of[collector.nodeid] = collector.path
            self._start_directory(collector.path)
            return
        module = collector.getparent(pytest.Module)
        if module is None:
            return
        key = str(module.path)
        self._module_tests.setdefault(key, [])
        self._module_of[collector.nodeid] = key
        if collector is module:
            self._modules[collector.nodeid] = module
            if key not in self._known_files:
                from ..config.collected_tests import _stat_pair

                self._file_stats[key] = _stat_pair(module.path)

    def _in_the_test_dirs(self, key: str) -> bool:
        return any(_within(key, str(test_dir)) for test_dir in self._sut_test_dirs)

    def _start_directory(self, path: Path) -> None:
        """Stat a directory taken in full, and its conftest unless the caller already did.

        The directory's own stat is taken here even when the caller has one:
        this is the last moment before pytest lists it, so a file saved after
        the caller's stat and before the listing is both collected and inside
        the stat recorded.
        """
        key = str(path)
        whole = self._candidates is None or key in self._listed_in_full
        if not whole or not self._in_the_test_dirs(key):
            return
        from ..config.collected_tests import _stat_pair
        from ..config.completion_cache import CONFTEST_FILENAME

        self._dir_stats[key] = _stat_pair(path)

        conftest = path / CONFTEST_FILENAME
        if str(conftest) in self._known_files:
            return
        stat = _stat_pair(conftest)
        if stat is not None:
            self._conftests[str(conftest)] = stat

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        """Note a collected module's dependencies; keep why a module or directory failed."""
        module = self._modules.pop(report.nodeid, None)
        if module is not None and report.passed:
            key = str(module.path)
            self._note_module(vars(module.obj).get("__name__"), key)
            self._deps.setdefault(key, set()).update(self._namespace_sources(module.obj) - {key})
        if not report.failed:
            return
        from ..config.repo import collect_failure_reason

        module = self._module_of.get(report.nodeid)
        if module is not None:
            self._errors.setdefault(module, collect_failure_reason(report))
        elif report.nodeid in self._dir_of:
            self._failed_dirs.append(self._dir_of[report.nodeid])

    def _note_refusal(self, exc: BaseException) -> None:
        from ..config.repo import registration_refusal

        refusal = registration_refusal(exc)
        if refusal is not None:
            self.refusals.append(refusal)

    @pytest.hookimpl(wrapper=True)
    def pytest_load_initial_conftests(self) -> Generator[None, None, None]:
        """Keep a refusal, and why, from the conftests pytest loads before any collector exists.

        ``pytest.main`` turns their failure into a usage error; this is the
        one place to see what it was.
        """
        try:
            return (yield)
        except Exception as exc:
            self._note_refusal(exc)
            self._stopped(str(exc))
            raise

    def _stopped(self, reason: str) -> None:
        if self.stop_reason is None:
            self.stop_reason = reason

    def pytest_keyboard_interrupt(self, excinfo: pytest.ExceptionInfo[BaseException]) -> None:
        """Keep why a ``pytest.exit`` or an interrupt stopped the session (:attr:`stop_reason`)."""
        self._stopped(f"{excinfo.typename}: {excinfo.value}")

    def pytest_internalerror(self, excinfo: pytest.ExceptionInfo[BaseException]) -> None:
        """Keep the internal error that stopped the session (:attr:`stop_reason`)."""
        self._stopped(excinfo.exconly())

    def pytest_exception_interact(
        self, call: pytest.CallInfo[Any], report: pytest.CollectReport | pytest.TestReport
    ) -> None:
        """Keep a refusal that failed a test module's or a nested conftest's collection."""
        if isinstance(report, pytest.CollectReport) and call.excinfo is not None:
            self._note_refusal(call.excinfo.value)

    def pytest_itemcollected(self, item: pytest.Item) -> None:
        """Record *item* in its file's record, and whether a requested name selects it.

        pytest reports every item here as it collects it, in collection
        order and before any ``pytest_collection_modifyitems`` reorders or
        deselects, so the record never depends on another plugin's hooks.
        """
        from ..config.collected_tests import RecordedTest
        from ..config.repo import classes_from_nodeid
        from .selection import base_test_name, matches_name

        classes = classes_from_nodeid(item.nodeid, item.name)
        module = item.getparent(pytest.Module)
        if module is not None:
            key = str(module.path)
            base = base_test_name(item.name)
            if (key, tuple(classes), base) not in self._recorded:
                self._recorded.add((key, tuple(classes), base))
                self._module_tests.setdefault(key, []).append(
                    RecordedTest(classes=classes, name=base)
                )
            self._module_items.setdefault(key, []).append(item)
            self._deps.setdefault(key, set()).update(self._sources(item) - {key})
        hits = [wanted for wanted in self._names if matches_name(wanted, classes, item.name)]
        if hits:
            self._matched.update(hits)
            self._selected.add(item.nodeid)

    def _sources(self, item: pytest.Item) -> set[str]:
        """Return the Python files *item*'s test is defined in: its function's and its classes'.

        ``inspect.getfile`` reads ``__code__``/``__module__``, so this costs no
        file operation. Built-ins and anything not from a ``.py`` file are skipped.
        """
        import inspect

        found: set[str] = set()
        function = getattr(item, "function", None)
        if function is not None and (path := self._defined_in(inspect, function)):
            found.add(path)
        cls = getattr(item, "cls", None)
        if isinstance(cls, type):
            found |= self._class_files(inspect, cls)
        return found

    def _namespace_sources(self, namespace: object) -> set[str]:
        """Return the Python files the classes, functions and modules in a namespace come from.

        A test can reach a module without being one of its items yet: a class
        that collected nothing gains its first test from a base class, a
        star-import brings in whatever its module later defines, and an
        imported module's constant can decide whether a test is defined at
        all. Each class counts with its whole hierarchy, each module with its
        own file.

        Each value is sorted by ``type(value)`` alone, never ``isinstance``:
        that reads ``__class__``, which runs user code on a proxy (Django's
        lazy ``settings`` sets itself up on first use) and can raise. A value
        that raises anyway is skipped.
        """
        found: set[str] = set()
        for value in list(vars(namespace).values()):
            found |= self._value_sources(value)
        return found

    def _value_sources(self, value: object) -> set[str]:
        """Return the files one namespace value comes from; nothing for a value that raises."""
        import inspect

        try:
            kind = type(value)
            if issubclass(kind, type):
                return self._class_files(inspect, cast("type", value))
            if kind is types.FunctionType:
                path = self._defined_in(inspect, value)
                return {path} if path else set()
            if kind is types.ModuleType:
                # The module's own dict: attribute access could reach a
                # module-level __getattr__.
                path = _python_file(vars(value).get("__file__"))
                if path:
                    self._note_module(vars(value).get("__name__"), path)
                return {path} if path else set()
        except Exception:  # noqa: BLE001 - a hostile global never stops a session
            return set()
        return set()

    def _class_files(self, inspect: Any, cls: type) -> set[str]:
        """Return the files *cls* and its bases are defined in, once per class.

        A metaclass can make hashing *cls* or reading its ``__mro__`` raise;
        for a namespace value, :meth:`_value_sources` catches that. An item's
        class got through pytest's own collection, which read both.
        """
        if cls not in self._class_sources:
            self._class_sources[cls] = {
                path for base in cls.__mro__ if (path := self._defined_in(inspect, base))
            }
        return self._class_sources[cls]

    def _defined_in(self, inspect: Any, obj: object) -> str | None:
        """Return the Python file *obj* is defined in, noting its module.

        A plain function's name and file both come from its ``__globals__``,
        its module's own namespace: ``functools.wraps`` copies ``__module__``
        from the function it wraps, so a wrapper defined in a test directory
        would otherwise name, say, one of otto's modules as imported from
        there. A class's come from its module (:func:`_source_file`), which
        ``__module__`` names for both.
        """
        if type(obj) is types.FunctionType:
            namespace = obj.__globals__
            path = _python_file(namespace.get("__file__"))
            name = namespace.get("__name__")
        else:
            path = _source_file(inspect, obj)
            name = _module_name(obj)
        if path is not None:
            self._note_module(name, path)
        return path

    def _note_module(self, name: object, path: object) -> None:
        """Note that the session met module *name*, loaded from *path*."""
        if isinstance(name, str) and isinstance(path, str):
            self._imported.setdefault(name, path)

    def pytest_plugin_registered(self, plugin: object) -> None:
        """Note each conftest pytest loads for the session: a module named for its file."""
        from ..config.completion_cache import CONFTEST_FILENAME

        if type(plugin) is types.ModuleType:
            path = vars(plugin).get("__file__")
            if isinstance(path, str) and Path(path).name == CONFTEST_FILENAME:
                self._note_module(vars(plugin).get("__name__"), path)

    def imported_modules(self) -> list[str]:
        """Return the modules the session loaded from the test directories, packages included.

        Its own record, never ``sys.modules``: each collected module, each
        conftest, and each module a test is defined in, whose file is under
        a test directory; with every package above one of them whose
        directory is too. pytest serves an imported module again to a later
        session in the process, so these are what the caller drops to have
        the next session read the files as they are then. A module from
        anywhere else stays: a repo's libraries are the bootstrap's, and
        what their init modules registered must stay the class a test gets.
        """
        from ..config.completion_cache import CONFTEST_FILENAME

        names: set[str] = set()
        for name, path in self._imported.items():
            file = Path(path)
            if file.name == CONFTEST_FILENAME or self._in_the_test_dirs(path):
                names.add(name)
            here = file.parent if file.name == "__init__.py" else file.with_suffix("")
            parts = name.split(".")
            for depth in range(len(parts) - 1, 0, -1):
                here = here.parent
                if here.name != parts[depth - 1] or not self._in_the_test_dirs(str(here)):
                    break
                names.add(".".join(parts[:depth]))
        return sorted(names)

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_collection_modifyitems(
        self, config: pytest.Config, items: list[pytest.Item]
    ) -> Generator[None, None, None]:
        """Keep what the requested names select, and publish what the session collected.

        Before the wrapper yields, so before every plain implementation,
        ``tryfirst`` ones included, whatever order they registered in: ``-m``,
        ``-k`` and a conftest deselect among the named tests. The kept items
        keep their order, which is collection order in a ``--no-random`` run.
        Markers are read after the yield, so one a conftest adds while it
        modifies the items is recorded. Items another implementation *adds*
        to the list are neither recorded nor narrowed by the names.

        Once the records are complete, a required name that matched nothing
        or a refused registration stops the session with a message-less
        ``pytest.UsageError``: pytest still finishes the session
        (``pytest_sessionfinish`` fires) and runs no test.
        """
        self._finish_collection(config, items)
        result = yield
        for key, record in self.records.items():
            record.markers = sorted(
                {m.name for item in self._module_items.get(key, []) for m in item.iter_markers()}
            )
        self.collection_finished = True
        if self.refusals or any(name in self.unmatched_names for name in self._must_match):
            # No message: pytest prints a UsageError's args, and the caller,
            # which can suggest names from every repo's table, says it once.
            raise pytest.UsageError
        return result

    def pytest_collection_finish(self, session: pytest.Session) -> None:
        """Keep what the session selected (:attr:`selected`): every deselection is done by now."""
        from ..config.repo import classes_from_nodeid

        self.selected = [
            SelectedTest(
                path=item.path, classes=classes_from_nodeid(item.nodeid, item.name), name=item.name
            )
            for item in session.items
        ]

    def pytest_sessionfinish(self, exitstatus: int) -> None:
        """Keep the session's exit status (:attr:`exitstatus`)."""
        self.exitstatus = int(exitstatus)

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtestloop(self, session: pytest.Session) -> None:
        """Tell the caller the session is committed to running tests (``before_tests``).

        Not when nothing is left to run, or a collection error is about to
        stop it (pytest's own run loop checks the same before the first
        test). A session stopped for a name or a refusal never gets here.
        """
        config = session.config
        if self._before_tests is None or not session.items:
            return
        if config.option.collectonly:
            return
        if session.testsfailed and not config.option.continue_on_collection_errors:
            return
        self._before_tests()

    def _finish_collection(self, config: pytest.Config, items: list[pytest.Item]) -> None:
        """Build the records, then narrow *items* to the selected ones (see the hook above)."""
        from ..config.collected_tests import FileRecord
        from ..config.completion_cache import CONFTEST_FILENAME
        from ..config.repo import marker_name

        now = int(time.time())
        records = {
            key: FileRecord(
                stat=self._file_stats.get(key),
                tests=tests,
                error=self._errors.get(key),
                deps=sorted(self._deps.get(key, set())),
                collected_at=now,
            )
            for key, tests in self._module_tests.items()
        }
        for key in sorted((self._candidates or set()) - set(records)):
            path = Path(key)
            unreached = any(path.is_relative_to(d) for d in self._failed_dirs)
            if path.name != CONFTEST_FILENAME and not unreached:
                records[key] = FileRecord(stat=None, collected_at=now)
        for key, stat in self._conftests.items():
            records.setdefault(key, FileRecord(stat=stat, collected_at=now))

        self.records = records
        self.dep_stats = self._library_stats({d for r in records.values() for d in r.deps})
        failed = [str(d) for d in self._failed_dirs]
        self.dirs = {
            key: None if any(_within(f, key) for f in failed) else stat
            for key, stat in self._dir_stats.items()
        }
        self.unmatched_names = [name for name in self._names if name not in self._matched]
        if self._names:
            kept = [item for item in items if item.nodeid in self._selected]
            deselected = [item for item in items if item.nodeid not in self._selected]
            if deselected:
                config.hook.pytest_deselected(items=deselected)
            items[:] = kept
        self.registered_markers = sorted(
            {name for line in config.getini("markers") if (name := marker_name(str(line)))}
        )

    def _library_stats(self, deps: set[str]) -> dict[str, list[int] | None]:
        """Return the stat to record for each library in *deps* an earlier session met.

        A library is a tracked dependency (not the standard library, not an
        installed distribution) outside the test directories, and not a
        conftest: a module no session evicts. One met for the first time is
        noted at the caller's stat for it, or at one taken now.
        """
        from ..config.collected_tests import _stat_pair, _tracked_dependency
        from ..config.completion_cache import CONFTEST_FILENAME

        found: dict[str, list[int] | None] = {}
        for dep in sorted(deps):
            if (
                not _tracked_dependency(dep)
                or self._in_the_test_dirs(dep)
                or Path(dep).name == CONFTEST_FILENAME
            ):
                continue
            if dep in _LIB_DEP_STATS:
                found[dep] = _LIB_DEP_STATS[dep]
            elif dep in self._known_deps:
                _LIB_DEP_STATS[dep] = self._known_deps[dep]
            else:
                _LIB_DEP_STATS[dep] = _stat_pair(Path(dep))
        return found

    @staticmethod
    def _advance_test_dir(item: pytest.Item, iteration: int) -> None:
        """Re-point a repeating item's ``test_dir`` at ``iteration_<iteration>``.

        A no-op for an item that never requested ``test_dir``: no base path was
        stashed, so there is nothing to move and nothing to create. The stash
        is updated either way, so anything else reading the iteration number
        sees it whether or not this test keeps artifacts.

        Args:
            item: The item whose call phase is about to repeat.
            iteration: The 1-based number of the iteration about to run.
        """
        item.stash[otto_iteration_key] = iteration
        base = item.stash.get(otto_test_dir_base_key, None)
        if base is None:
            return
        path = iteration_dir(base, iteration)
        path.mkdir(parents=True, exist_ok=True)
        cast("Any", item).funcargs["test_dir"] = path

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_protocol(
        self, item: pytest.Item, nextitem: pytest.Item | None
    ) -> bool | None:
        """Repeat each test item when stability mode is active.

        When ``--iterations`` or ``--duration`` (or both) are specified,
        each collected test is executed multiple times within a single
        pytest session.

        Unlike calling ``runtestprotocol`` in a loop (which tears down
        *all* fixtures including class-scoped ones after each call), this
        hook runs setup once, repeats the call phase N times, then runs
        teardown once.  This keeps class-scoped resources (SSH
        connections, deployed artifacts, etc.) alive across iterations.

        The cost of that is that NO fixture re-fires per iteration —
        function-scoped ones included, since they resolve during the single
        setup.  ``test_dir`` still has to move, or every iteration's logs and
        artifacts would overwrite the previous one's, so the loop re-points it
        itself: the iteration number goes in the item's stash (which is what
        the fixture reads for iteration 1) and each later iteration rewrites
        ``funcargs["test_dir"]`` from the base path the fixture parked there.
        A test that never requested ``test_dir`` parks no base, and no
        directory is created for it.

        Returns ``True`` to signal that this hook handled the item,
        or ``None`` to fall through to default behaviour.
        """
        if self._iterations <= 0 and self._duration <= 0:
            return None

        max_iters = self._iterations if self._iterations > 0 else float("inf")
        deadline = (time.monotonic() + self._duration) if self._duration > 0 else float("inf")

        # _request, _initrequest, funcargs live on pytest.Function (private
        # API not surfaced on pytest.Item). Duck-type via hasattr and route
        # all access through an Any-cast alias so ty stays out of the way.
        item_any = cast("Any", item)
        hasrequest = hasattr(item, "_request")
        if hasrequest and not item_any._request:  # noqa: SLF001 — deliberate access to pytest.Function._request (private pytest API, cast to Any)
            item_any._initrequest()  # noqa: SLF001 — deliberate access to pytest.Function._initrequest (private pytest API, cast to Any)

        item.stash[otto_iteration_key] = 1

        # ── Setup (once) ──────────────────────────────────────────────
        setup_report = call_and_report(item, "setup", log=True)
        if not setup_report.passed:
            # Teardown even on setup failure, then exit
            call_and_report(item, "teardown", log=True, nextitem=nextitem)
            if hasrequest:
                item_any._request = False  # noqa: SLF001 — deliberate access to pytest.Function._request (private pytest API, cast to Any)
                item_any.funcargs = None
            return True

        if item.config.getoption("setupshow", False):
            show_test_item(item, add_space=False)

        # ── Call (repeated) ───────────────────────────────────────────
        iteration = 0
        is_stability = self._iterations > 1 or self._duration > 0
        while iteration < max_iters and time.monotonic() < deadline:
            if is_stability:
                logger.info(f"[bold cyan]--- {item.name} iteration {iteration + 1} ---[/bold cyan]")
            if iteration > 0:  # iteration 1's dir came from the fixture at setup
                self._advance_test_dir(item, iteration + 1)
            call_and_report(item, "call", log=True)
            iteration += 1

        # ── Teardown (once) ───────────────────────────────────────────
        call_and_report(item, "teardown", log=True, nextitem=nextitem)
        if hasrequest:
            item_any._request = False  # noqa: SLF001 — deliberate access to pytest.Function._request (private pytest API, cast to Any)
            item_any.funcargs = None

        return True

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_call(self, item: pytest.Item) -> Generator[None, Any, None]:
        """Implement ``@pytest.mark.retry(n)`` via the shared hookwrapper.

        A hookwrapper, not a plain impl: ``pytest_runtest_call`` is not
        ``firstresult``, so a plain impl runs *alongside* pytest's default
        runner — the body executed once more after a successful retry, and
        that extra run decided the outcome. All retry semantics live in
        ``otto.suite._retry.retry_hookwrapper``.
        """
        yield from retry_hookwrapper(item)

    def pytest_terminal_summary(self, terminalreporter: Any) -> None:
        """Name every retried test so a pass-after-retries stays visible."""
        report_retries(terminalreporter)

    # PERMANENT(no-tuple-return): pytest dictates this hook's return shape.
    # ast-grep-ignore: no-tuple-return
    def pytest_report_teststatus(
        self,
        report: pytest.TestReport,
        config: pytest.Config,  # noqa: ARG002 — required by pytest hook signature
    ) -> tuple[str, str, str] | None:
        """Suppress pytest's per-test progress characters.

        otto's RichHandler streams log output to the console in real time,
        so pytest's dot/``F``/``E`` column adds no information and races
        with log records when capture is disabled. Returning an empty
        short-letter keeps the category and verbose word intact (so failure
        summaries and the final pass/fail counts still render) while
        stopping the terminal reporter from writing anything per test.

        The *category* must mirror pytest's own categorisation exactly. This
        hook is ``firstresult``; pluggy runs ``tryfirst`` impls, then the rest
        newest-first, then ``trylast``, so otto's undecorated impl runs after
        ``_pytest.subtests`` (``tryfirst``, which therefore still sees every
        report first) and replaces the three below it outright —
        ``_pytest.skipping`` (xfail/xpass), ``_pytest.runner``
        (setup/teardown) and ``_pytest.terminal`` (the rest). In particular a
        *passing* setup or teardown report carries the **empty** category:
        only the ``call`` phase counts towards "passed". Returning "passed"
        for all three phases counted every test three times — a one-test
        suite reported ``3 passed``.
        """
        # mirrors _pytest.skipping.pytest_report_teststatus
        if hasattr(report, "wasxfail"):
            if report.skipped:
                return ("xfailed", "", "XFAIL")
            if report.passed:
                return ("xpassed", "", "XPASS")
        # mirrors _pytest.runner.pytest_report_teststatus
        if report.when in ("setup", "teardown"):
            if report.failed:
                return ("error", "", "ERROR")
            if report.skipped:
                return ("skipped", "", "SKIPPED")
            return ("", "", "")
        # mirrors _pytest.terminal.pytest_report_teststatus
        outcome: str = report.outcome
        if report.when == "collect" and outcome == "failed":
            outcome = "error"
        return (outcome, "", outcome.upper())

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        """In stability mode, accumulate per-test pass/fail counts."""
        if self._stability_collector is None:
            return
        if report.when != "call":
            return
        self._stability_collector.record(report.nodeid, passed=report.passed)

    @pytest.hookimpl(tryfirst=True, hookwrapper=True)
    def pytest_runtest_makereport(
        self,
        item: pytest.Item,
        call: pytest.CallInfo[None],  # noqa: ARG002 — required by pytest hookwrapper signature
    ) -> Generator[None, None, None]:
        """Attach the phase report to *item* so fixtures can inspect pass/fail during teardown."""
        outcome = yield
        # hookwrapper=True: yield returns a pluggy Result whose
        # get_result() surfaces the TestReport; the pytest stubs type it
        # as None, so cast to access the runtime API.
        rep = cast("Any", outcome).get_result()
        setattr(item, f"rep_{rep.when}", rep)

    @pytest.hookimpl(wrapper=True)
    def pytest_fixture_setup(self, fixturedef: Any, request: pytest.FixtureRequest) -> Any:
        """Name each pytest-asyncio loop, hold its cleanup, and close its hosts just before it ends.

        It names each runner's loop and holds a cleanup boundary on it for the
        runner's scope; the finalizer shuts the loop's registry down, closing
        every host on it whatever the count. A runner fixture's own teardown,
        which closes its loop, was registered on its fixturedef while the
        fixture set up. The sweep is registered after it, and finalizers run
        last-in first-out, so the sweep runs while the loop is still open.
        Every fixture set up on the loop later registers its own teardown
        later still, so those finish before the sweep. A fixture that closes
        its host itself therefore leaves nothing to sweep, and the loop-end
        debug line names only the hosts the sweep closed.
        """
        result = yield
        match = RUNNER_FIXTURE.fullmatch(fixturedef.argname)
        if match is not None:
            from ..host.loop_owner import LOOP_LABELS
            from ..invocation import acquire_boundary, current_policy

            loop = result.get_loop()
            label = runner_label(match.group(1), request)
            LOOP_LABELS[loop] = label
            # Held for the runner's whole loop scope: an open_context inside a test
            # leaves its hosts to this runner, whose shutdown closes them.
            boundary = acquire_boundary(loop, deadline=current_policy().teardown_deadline)
            fixturedef.addfinalizer(functools.partial(sweep_runner_loop, result, label, boundary))
        return result

    @pytest.fixture(autouse=True)
    def _otto_test_events(self, request: pytest.FixtureRequest) -> Generator[None, None, None]:
        """Log a start banner for every test; under ``--monitor``, mark its start and end.

        Sync on purpose: the monitor events run on the runner of the loop the
        test body runs on (:func:`~otto.suite.loops.runner_for`), whatever that
        loop's scope, rather than on a fixture loop of pytest-asyncio's
        choosing. The events are labelled ``<owner>.<test name>: start`` and
        ``...: pass`` or ``...: fail``, where the owner is the test's class, or
        its module's stem for a plain function.
        """
        node = cast("pytest.Item", request.node)
        logger.info(f"[bold cyan]=== {node.name} ===[/bold cyan]")
        collector = self.session_monitor_collector
        if collector is None:
            yield
            return
        cls = getattr(request, "cls", None)
        owner = cls.__name__ if cls is not None else Path(str(node.path)).stem
        runner = runner_for(request)
        runner.run(
            collector.add_event(
                label=f"{owner}.{node.name}: start", color="#888888", dash="dash", source="auto"
            ),
            context=contextvars.copy_context(),
        )
        yield
        rep = getattr(node, "rep_call", None)
        outcome = "fail" if (rep is not None and not rep.passed) else "pass"
        runner.run(
            collector.add_event(
                label=f"{owner}.{node.name}: {outcome}",
                color="#2ca02c" if outcome == "pass" else "#d62728",
                dash="solid",
                source="auto",
            ),
            context=contextvars.copy_context(),
        )

    @pytest_asyncio.fixture(
        scope="session",
        loop_scope="session",
        autouse=True,
    )
    async def _otto_session_monitor(self) -> AsyncGenerator[None, None]:
        """Build the session-scoped :class:`MetricCollector` when ``--monitor`` is set.

        Owns the collector lifecycle: construct the collector over the
        configured hosts, expose it as :attr:`session_monitor_collector` so the
        per-test events (and the per-class collection task below) can reach it,
        export collected data on teardown, then close.

        Refuses, rather than running unmonitored: when the selection holds
        nothing otto can sample (or the lab is empty, or a ``--monitor-hosts``
        pattern matched nothing) the run stops with a usage error, because the
        user asked for a monitored run this lab cannot give.

        Note: this fixture *does not* drive ``collector.run()``.
        ``_otto_class_monitor_task`` does, restarted per class, so collection
        runs while each class's tests do and pauses between classes.
        """
        if not self._monitor:
            yield
            return

        from ..config.fleet import get_lab
        from ..config.scope import EmptySelectionError
        from ..monitor.errors import NoMonitorableHostsError
        from ..monitor.live import select_monitor_hosts
        from ..monitor.session import MonitorSession

        # pytest.exit, because that IS the truthful outcome: the user asked for
        # a monitored run and this lab cannot give one. Unguarded, the refusal
        # would escape a session-scoped fixture as an errored-fixture traceback
        # once per test. RunOptions already refused a malformed regex.
        try:
            hosts = select_monitor_hosts(self._monitor_hosts)
        except EmptySelectionError as exc:
            pytest.exit(f"--monitor-hosts: {exc}", returncode=pytest.ExitCode.USAGE_ERROR)
        except NoMonitorableHostsError as exc:
            pytest.exit(f"--monitor: {exc}", returncode=pytest.ExitCode.USAGE_ERROR)

        output = self._monitor_output
        to_db = output is not None and output.suffix.lower() == ".db"
        session = MonitorSession.build(
            hosts,
            interval=self._monitor_interval,
            db_path=output if to_db else None,
            export_path=None if to_db else output,
            declared=get_lab().links,
            owns_hosts=True,
        )
        # Opened HERE, on the session loop, before any per-class task drives
        # run() (see _otto_class_monitor_task): a class that ends before an
        # in-task open completes would leave a partial archive.
        await session.open()
        self.session_monitor_collector = session.collector
        try:
            yield
        finally:
            try:
                await session.finish()
                if output is not None:
                    logger.info(f"Monitor data written to {output}")
            finally:
                self.session_monitor_collector = None

    @pytest_asyncio.fixture(scope="class", autouse=True)
    @staticmethod
    async def _otto_class_monitor_task(
        request: pytest.FixtureRequest,
    ) -> AsyncGenerator[None, None]:
        """Drive ``collector.run()`` while a test class runs.

        A ``staticmethod`` reading its plugin from ``otto_plugin_key``: see
        that key for why a class-scoped fixture cannot be an instance method.

        No ``loop_scope``, so the task runs on the default fixture loop: under
        ``otto test`` (``ASYNCIO_LOOP_ARGS``) that is the session loop, the
        loop every unpinned test runs on, so ``_collect_one`` executes while
        the tests do. A task only ticks while its loop runs: during tests
        pinned to a narrower loop it waits, and those tests' monitor events
        still record, on their own loop.

        Collected metrics accumulate on the shared session-scoped collector,
        so a single export at session teardown captures every class's data.
        Between classes, collection pauses — gaps are expected.
        """
        plugin = request.config.stash[otto_plugin_key]
        interval = plugin.class_monitor_interval()
        collector = plugin.session_monitor_collector
        if interval is None or collector is None:
            yield
            return

        task = asyncio.create_task(collector.run(interval=timedelta(seconds=interval)))
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
