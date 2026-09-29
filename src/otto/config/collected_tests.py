"""What each test file holds, per repo, validated by one ``stat`` per path.

The completion cache's reserved ``__collected_tests__`` namespace holds one
**table** per repo, keyed by the repo's absolute ``sut_dir``::

    {"schema_version": 5,
     "generated_at": 1727400000,
     "env":   {"python": "3.10.12", "prefix": "/path/to/venv",
               "pytest": "9.1.1", "otto": "0.16.1",
               "site_packages": {"<dir>": [mtime_ns, size]},
               "configs": {"<sut>/pyproject.toml": [mtime_ns, size],
                           "<sut>/pytest.ini": null, ...},
               "settings": [mtime_ns, size]},
     "dirs":  {"<abs dir>": [mtime_ns, size], ...},
     "deps":  {"<abs file a record's tests come from>": [mtime_ns, size] | null, ...},
     "files": {"<abs test file or conftest>": {
                  "stat": [mtime_ns, size] | null,
                  "tests": [[["TestTop0"], "test_x"], [[], "test_plain"], ...],
                  "markers": ["slow"],
                  "error": null | "SyntaxError: ...",
                  "deps": ["<abs path>", ...],
                  "collected_at": 1727400000}},
     "registered_markers": ["slow", "hw", "asyncio", ...]}

Only a pytest collection ever writes a record: otto reads no test file
itself. A **file record** says what one test file held the last time pytest
collected it: its tests as ``(classes, base name)`` pairs (parametrizations
collapsed), the markers applied in it, the error that stopped it being
collected, and when it was collected (``collected_at``, per record). The
``stat`` is the file's ``[mtime_ns, size]`` just before it was read. A
conftest is a record with no tests; only its stat matters. A ``null`` stat
means "watched, content not known": a conftest above a tests dir that does not
exist yet, or a file whose change was seen but not yet read (it classifies as
changed until it is).

A record also names its **dependencies**: the other files its tests were
defined in (a base class in another test module or a library, an imported
test function, a module whose flag decides a test). A file gains or loses
tests when one of them changes, with no change to its own stat, so ``deps``
holds each such file's stat once, and a record whose dependency moved is
changed.

``dirs`` holds every directory pytest entered under the tests dirs, each with
its stat from before pytest listed it. A directory whose stat moved has
gained or lost an entry, so a collection enters it again and pytest decides
what in it is a test file: otto applies no ``python_files`` rule of its own.
``env`` holds what decides collection for the whole repo: the pytest config
files, the repo's ``settings.toml``, the stat of the site-packages directory
pytest and otto are installed in (its mtime moves when any distribution is
installed or removed, the cheapest honest proxy for "the plugin set
changed"), and the python, pytest and otto versions. ``generated_at`` is when
a collection of the whole tree last built the table. The console-script shim
reads the same records without this module (``otto._shim_complete``), and
without a ``stat`` of anything they track: it checks only ``env``, and
leaves the :func:`classify` pass to the collect child it starts once a check
is due.

:func:`classify` validates a table with one ``stat`` per stored file and
directory (plus the few ``env`` paths) and no listing, and says which files
are fresh, changed, new or deleted, and which directories a collection must
enter again. Every record comes from one pytest collection path
(``otto.suite.run._run_pytest_session``), which merges what a session read
with :func:`updated_table`. A run collects what its tables cannot vouch
for, and ``otto.suite.run._refresh_tables`` does the same for a reader of
test names, running nothing: a cold table (none, another ``env``, or older
than the TTL) is seeded by a whole-tree collection, a warm one re-reads what
moved, and a table whose only news is deleted files drops their records
without a session. :func:`read_tables`
and :func:`write_tables` move tables in and out of the cache file under the
same rules as every reserved namespace: one read, an atomic replace, and
everything else in the file carried forward.

This module imports neither pytest nor the run module, so a completer can
read and validate a table: every collection is ``otto.suite.run``'s.
"""

import contextlib
import functools
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeGuard

from . import corpus_snapshot
from .completion_cache import (
    CACHE_FILENAME,
    CACHE_TTL_SECONDS,
    COLLECTED_TESTS_KEY,
    CONFTEST_FILENAME,
    _atomic_write_json,
    _cache_path,
    ancestor_conftests,
)
from .repo import TOML_SETTINGS_PATH, pytest_config_paths, selectable_names

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from .repo import Repo

RECORDS_SCHEMA_VERSION = 5
"""The per-repo table's own schema. A table with any other version reads as absent.

1-3 were one whole-corpus entry per digest of the test tree; one may still
sit in the same namespace, under a digest rather than a path, until the next
write prunes it (:func:`write_tables`). 4 told a record pytest wrote from one
a static scan of the source wrote; only pytest writes records now. The
console-script shim mirrors this number (``otto._shim_complete.TABLE_SCHEMA``).
"""

_FRESH = "fresh"
_CHANGED = "changed"
_NEW = "new"
_DELETED = "deleted"
_ABSENT = "absent"
"""A watched path that did not exist and still does not: kept, never listed."""


@dataclass
class RecordedTest:
    """One test a file holds: the classes around it, outermost first, and its base name.

    The pair :func:`~otto.config.repo.selectable_names` and ``otto test``'s
    name matching take. ``name`` never carries a parametrization id.
    """

    classes: list[str]
    name: str


@dataclass
class FileRecord:
    """What one test file (or conftest) held when it was last read."""

    stat: list[int] | None
    """``[mtime_ns, size]`` when it was read; ``None`` for "watched, not known" (see the module)."""
    tests: list[RecordedTest] = field(default_factory=list)
    """The tests it holds; always empty for a conftest."""
    markers: list[str] = field(default_factory=list)
    """Names of the markers applied in it, sorted."""
    error: str | None = None
    """Why it could not be read (``"SyntaxError: ..."``), kept until the file changes."""
    deps: list[str] = field(default_factory=list)
    """The other files its tests are defined in, sorted absolute paths; see the module."""
    collected_at: int = 0
    """When the content was read, in epoch seconds; ``0`` for never.

    Per record, not per table: a table is rewritten whenever any file is
    read, so only this can say how old one file's names are (a reader's TTL
    applies to it).
    """

    def to_json(self) -> dict[str, Any]:
        """Return the record as the cache file stores it."""
        return {
            "stat": self.stat,
            "tests": [[list(t.classes), t.name] for t in self.tests],
            "markers": list(self.markers),
            "error": self.error,
            "deps": list(self.deps),
            "collected_at": self.collected_at,
        }


@dataclass
class RepoTable:
    """One repo's table: its file records, its directory stats and its ``env``."""

    sut_dir: Path
    env: dict[str, Any]
    """What decides collection for the whole repo; see :func:`current_env`."""
    dirs: dict[str, list[int] | None]
    """Every directory the tests-dir walk entered, by absolute path.

    ``None`` is a tests dir that is missing: its appearance is seen by its stat.
    """
    files: dict[str, FileRecord]
    """Every test file and conftest, by absolute path."""
    deps: dict[str, list[int] | None] = field(default_factory=dict)
    """Every file some record depends on, with its stat before the read that found it.

    ``None`` is a dependency first seen by the last collection: its stat
    from before that read is not known, so its holders stay changed until
    the next collection stamps it."""
    registered_markers: list[str] = field(default_factory=list)
    """Every marker pytest registered (``config.getini("markers")``) when it last collected."""
    generated_at: int = 0
    """When a collection of the whole tree last built the table, in epoch seconds.

    A collection of only some files keeps it: the table's TTL bounds how long
    ago pytest last looked at everything, which is what catches what no stat
    follows (tests generated from a data file)."""

    @property
    def names(self) -> list[str]:
        """Every name a recorded test answers to (``selectable_names``), as last recorded, sorted.

        Every record counts as pytest last read it: a file edited or deleted
        since offers what it held until a collection reads it again. What a
        TAB offers, and what a did-you-mean suggests from; a run never
        trusts it for a file it cannot vouch for (``classify``). The
        console-script shim offers the same (``otto._shim_complete.table_view``).
        """
        found: set[str] = set()
        for record in self.files.values():
            for test in record.tests:
                found.update(selectable_names(test.classes, test.name))
        return sorted(found)

    @property
    def markers(self) -> list[str]:
        """Every marker pytest registered at the last whole-tree collection, and each one applied.

        Sorted; every record counts, as for :attr:`names`.
        """
        found = set(self.registered_markers)
        for record in self.files.values():
            found.update(record.markers)
        return sorted(found)

    def to_json(self) -> dict[str, Any]:
        """Return the table as the cache file stores it."""
        return {
            "schema_version": RECORDS_SCHEMA_VERSION,
            "generated_at": self.generated_at,
            "env": self.env,
            "dirs": dict(self.dirs),
            "deps": dict(self.deps),
            "files": {key: record.to_json() for key, record in self.files.items()},
            "registered_markers": list(self.registered_markers),
        }


_STAT_FIELDS = 2
"""A stored stat is ``[mtime_ns, size]``; a stored test is ``[classes, name]``."""


def _is_stat(value: object) -> bool:
    return value is None or (
        isinstance(value, list)
        and len(value) == _STAT_FIELDS
        and all(isinstance(v, int) for v in value)
    )


def _is_str_list(value: object) -> TypeGuard[list[str]]:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _record_from_json(raw: object) -> FileRecord | None:
    """Parse one stored record, or ``None`` when any part of it is malformed."""
    if not isinstance(raw, dict) or not _is_stat(raw.get("stat")):
        return None
    tests, markers, error = (raw.get(k) for k in ("tests", "markers", "error"))
    if not isinstance(tests, list):
        return None
    if not _is_str_list(markers) or not (error is None or isinstance(error, str)):
        return None
    collected_at, deps = raw.get("collected_at"), raw.get("deps")
    if not isinstance(collected_at, int) or not _is_str_list(deps):
        return None
    parsed: list[RecordedTest] = []
    for pair in tests:
        if not (isinstance(pair, list) and len(pair) == _STAT_FIELDS):
            return None
        classes, name = pair
        if not _is_str_list(classes) or not isinstance(name, str):
            return None
        parsed.append(RecordedTest(classes=list(classes), name=name))
    return FileRecord(
        stat=raw["stat"],
        tests=parsed,
        markers=list(markers),
        error=error,
        deps=list(deps),
        collected_at=collected_at,
    )


def table_from_json(sut_dir: Path, raw: object) -> RepoTable | None:
    """Parse one stored table, or ``None`` for another schema or anything malformed.

    ``None`` is "no table": the caller treats the repo as never collected.
    """
    if not isinstance(raw, dict) or raw.get("schema_version") != RECORDS_SCHEMA_VERSION:
        return None
    env, dirs, files, deps = (raw.get(k) for k in ("env", "dirs", "files", "deps"))
    generated_at, registered = raw.get("generated_at"), raw.get("registered_markers")
    if not (
        isinstance(env, dict)
        and isinstance(dirs, dict)
        and isinstance(files, dict)
        and isinstance(deps, dict)
    ):
        return None
    if not isinstance(generated_at, int) or not _is_str_list(registered):
        return None
    if not all(
        isinstance(k, str) and _is_stat(v) for part in (dirs, deps) for k, v in part.items()
    ):
        return None
    records: dict[str, FileRecord] = {}
    for key, value in files.items():
        record = _record_from_json(value)
        if not isinstance(key, str) or record is None:
            return None
        records[key] = record
    return RepoTable(
        sut_dir=sut_dir,
        env=env,
        dirs=dict(dirs),
        files=records,
        deps=dict(deps),
        registered_markers=list(registered),
        generated_at=generated_at,
    )


# --- the cache file ------------------------------------------------------------


def _cache_file(home: Path | None) -> Path | None:
    """Return the cache file: in *home* when the caller has the workspace home, else looked up."""
    return home / CACHE_FILENAME if home is not None else _cache_path()


def _load_cache_file(cache_path: Path | None) -> dict[str, Any]:
    """Read the whole cache file as a dict; ``{}`` when it is missing or unreadable."""
    if cache_path is None:
        return {}
    try:
        loaded = json.loads(cache_path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _namespace(data: dict[str, Any]) -> dict[str, Any]:
    namespace = data.get(COLLECTED_TESTS_KEY)
    return dict(namespace) if isinstance(namespace, dict) else {}


def read_tables(repos: "Iterable[Repo]", *, home: Path | None = None) -> dict[str, RepoTable]:
    """Every repo's stored table, keyed by ``str(sut_dir)``, from one read of the cache file.

    A repo with no table, a table from another schema or a malformed one is
    simply absent from the result. *home* is the workspace home
    (:func:`otto.config.home.workspace_home`) when the caller has it: looking
    it up resolves every SUT dir.
    """
    namespace = _namespace(_load_cache_file(_cache_file(home)))
    tables: dict[str, RepoTable] = {}
    for repo in repos:
        key = str(repo.sut_dir)
        table = table_from_json(repo.sut_dir, namespace.get(key))
        if table is not None:
            tables[key] = table
    return tables


def read_table(repo: "Repo") -> RepoTable | None:
    """*repo*'s stored table, or ``None``; see :func:`read_tables`."""
    return read_tables([repo]).get(str(repo.sut_dir))


def _is_live(key: object) -> bool:
    """Whether a namespace key is the ``sut_dir`` of a repo that is still there."""
    return isinstance(key, str) and Path(key).is_absolute() and Path(key).is_dir()


def write_tables(tables: list[RepoTable], *, home: Path | None = None) -> None:
    """Store *tables* in the cache file, in one atomic replace.

    Only the tables' own keys in ``__collected_tests__`` change: other live
    repos' tables and the rest of the file are carried forward as they are.
    A key that names no directory now is dropped: a repo whose ``sut_dir``
    was removed or renamed, or a whole-corpus entry an older otto stored
    under a digest. The console-script shim parses the whole file on every
    TAB, so a table no repo can use again is pure cost. No inventory gate,
    unlike the sections: a table depends on no inventory, so it is never an
    entry that could not be read back. *home* as for :func:`read_tables`.
    """
    cache_path = _cache_file(home)
    if cache_path is None or not tables:
        return
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    data = _load_cache_file(cache_path)
    namespace = {key: raw for key, raw in _namespace(data).items() if _is_live(key)}
    for table in tables:
        namespace[str(table.sut_dir)] = table.to_json()
    data[COLLECTED_TESTS_KEY] = namespace
    _atomic_write_json(cache_path, data)
    mark_tables_checked(cache_path.parent)


def mark_tables_checked(cache_dir: Path) -> bool:
    """Record that the tables beside *cache_dir*'s cache file were just checked.

    Touches the ``tests`` marker, which starts the check window
    (:data:`~otto.config.cache_maintenance.CHECK_WINDOW_SECONDS`): until it
    lapses, a test-name TAB starts no collect child
    (:func:`tables_check_due`). Every writer of a table classified the whole
    table before it wrote (an ``otto test`` run, an unfiltered listing, the
    collect child), and the child marks a check that found everything
    current and wrote nothing. The table's own ``generated_at``, and so its
    TTL, moves only on a whole-tree collection.

    Returns whether the marker was written. A caller that only records a
    check can ignore it: with no marker, the next TAB checks again, which is
    only early.
    """
    from .cache_maintenance import MARKER_FILENAMES

    marker = cache_dir / MARKER_FILENAMES["tests"]
    # Not Path.touch: it swallows a refused utime and reports success.
    try:
        os.utime(marker, None)
    except FileNotFoundError:
        try:
            marker.open("a").close()
        except OSError:
            return False
    except OSError:
        return False
    return True


def tables_check_due(cache_dir: Path) -> bool:
    """Whether the tables beside *cache_dir*'s cache file are due a check by the collect child.

    Due when the ``tests`` marker is missing, dated after now (the clock
    stepped back), or older than the check window; see
    :func:`mark_tables_checked`.
    """
    from .cache_maintenance import CHECK_WINDOW_SECONDS, MARKER_FILENAMES

    try:
        age = time.time() - (cache_dir / MARKER_FILENAMES["tests"]).stat().st_mtime
    except OSError:
        return True
    return not 0 <= age < CHECK_WINDOW_SECONDS


# --- env ---------------------------------------------------------------------


@dataclass(frozen=True)
class Installation:
    """The interpreter half of ``env``: versions and where pytest and otto are installed."""

    python: str
    prefix: str
    """``sys.prefix``: the venv. Worktree venvs share one Python and one ``OTTO_HOME``."""
    pytest: str | None
    otto: str | None
    site_packages: list[str]
    """The directories holding pytest's and otto's distributions, deduplicated."""


@functools.lru_cache(maxsize=1)
def _installation() -> Installation:
    """Read once per process: two distribution lookups, no import of either package."""
    import sys
    from importlib import metadata

    versions: dict[str, str | None] = {}
    dirs: list[str] = []
    for dist_name in ("pytest", "otto-sh"):
        try:
            dist = metadata.distribution(dist_name)
            version = dist.version
        except (metadata.PackageNotFoundError, OSError):
            versions[dist_name] = None
            continue
        versions[dist_name] = version
        site = str(dist.locate_file(""))
        if site not in dirs:
            dirs.append(site)
    major, minor, micro = sys.version_info[:3]
    return Installation(
        python=f"{major}.{minor}.{micro}",
        prefix=sys.prefix,
        pytest=versions["pytest"],
        otto=versions["otto-sh"],
        site_packages=dirs,
    )


def _stat_pair(path: Path) -> list[int] | None:
    st = corpus_snapshot.stat(path)
    return None if st is None else [st.st_mtime_ns, st.st_size]


def current_env(repo: "Repo") -> dict[str, Any]:
    """*repo*'s ``env`` as it is now: one ``stat`` per path in it, JSON-shaped.

    A table whose stored ``env`` differs is wholly stale: a config, the
    settings, the installed distributions or a version moved.
    """
    install = _installation()
    configs = pytest_config_paths(repo.sut_dir)
    return {
        "python": install.python,
        "prefix": install.prefix,
        "pytest": install.pytest,
        "otto": install.otto,
        "site_packages": {site: _stat_pair(Path(site)) for site in install.site_packages},
        "configs": {str(path): _stat_pair(path) for path in configs},
        "settings": _stat_pair(repo.sut_dir / TOML_SETTINGS_PATH),
    }


# --- classify ------------------------------------------------------------------


@dataclass
class Classification:
    """What :func:`classify` found: each file's state, and the stats it saw.

    The four lists hold absolute paths, sorted, and cover conftests as well as
    test files. A conftest that changed, appeared or went away makes every
    file under its directory ``changed`` too, since pytest's conftest scope is
    its directory and below.
    """

    fresh: list[Path]
    """Stored records whose file and dependencies have the stats the record was taken at."""
    changed: list[Path]
    """Stored records whose file or a dependency moved, or that sit under a changed conftest."""
    new: list[Path]
    """Conftests with no record: one a tests dir or a directory above it gained, or one
    that appeared in a directory whose stat moved."""
    deleted: list[Path]
    """Stored records whose file is gone."""
    whole_tree: bool
    """The table vouches for nothing: none, its ``env`` differs, or it is older than the TTL.

    Nothing is fresh, and only a collection of the whole tree can bring the
    table back."""
    env: dict[str, Any]
    """The ``env`` as it is now."""
    stats: dict[str, list[int] | None]
    """The stat each file path above had here, and each watched conftest's."""
    dirs: dict[str, list[int] | None]
    """The directory table as it is now: every stored directory still there, and the tests dirs."""
    deps: dict[str, list[int] | None] = field(default_factory=dict)
    """The stat each stored dependency had here; one that is also a file is stat'ed once."""
    candidate_dirs: list[Path] = field(default_factory=list)
    """Directories a collection must enter and take in full, sorted.

    Each directory whose stat moved (an entry appeared or went away), and
    every directory at or under a conftest that changed, appeared or went
    away. In full means: every file pytest collects there, and every
    subdirectory the table does not know yet, with all it holds; which files
    are test files is pytest's decision. Empty with :attr:`whole_tree`."""

    @property
    def is_current(self) -> bool:
        """Whether the table describes the tree as it is: nothing to collect."""
        return not (
            self.whole_tree or self.changed or self.new or self.deleted or self.candidate_dirs
        )


def _watched_conftests(repo: "Repo") -> set[str]:
    """Return each conftest pytest loads before listing anything: the tests dirs' own, and above.

    Watched whether or not they exist: creating one is seen by its own stat.
    """
    watched: set[str] = set()
    for test_dir in repo.tests:
        watched.add(str(test_dir / CONFTEST_FILENAME))
        watched.update(str(p) for p in ancestor_conftests(test_dir, repo.sut_dir))
    return watched


def _stored_status(
    stored: dict[str, FileRecord], stats: dict[str, list[int] | None], watchers: set[str]
) -> dict[str, str]:
    """Each stored record's state from its stat alone."""
    status: dict[str, str] = {}
    for key, record in stored.items():
        now = stats[key]
        if now == record.stat:
            status[key] = _FRESH if now is not None else _ABSENT if key in watchers else _DELETED
        elif now is None:
            status[key] = _DELETED
        else:
            status[key] = _CHANGED
    return status


def _under(key: str, root: str) -> bool:
    """Whether path *key* is *root* or below it (string paths, no ``stat``)."""
    return key == root or key.startswith(root.rstrip(os.sep) + os.sep)


def _invalidate_under_conftests(status: dict[str, str]) -> list[str]:
    """Mark ``changed`` every fresh record under a conftest that changed, appeared or went away.

    Return those conftests' directories.
    """
    roots = sorted(
        str(Path(key).parent)
        for key, state in status.items()
        if Path(key).name == CONFTEST_FILENAME and state in (_CHANGED, _NEW, _DELETED)
    )
    for key, state in status.items():
        if state == _FRESH and any(_under(key, root) and key != root for root in roots):
            status[key] = _CHANGED
    return roots


def _invalidate_dependents(
    stored: dict[str, FileRecord],
    status: dict[str, str],
    stored_deps: dict[str, list[int] | None],
    dep_stats: dict[str, list[int] | None],
) -> None:
    """Mark fresh every record changed whose dependency moved, went away, or was never stamped."""
    moved = {k for k, before in stored_deps.items() if before is None or dep_stats[k] != before}
    if not moved:
        return
    for key, record in stored.items():
        if status[key] == _FRESH and not moved.isdisjoint(record.deps):
            status[key] = _CHANGED


def _expired(table: RepoTable) -> bool:
    """Whether the whole tree was last collected longer ago than the cache's TTL."""
    return time.time() - table.generated_at > CACHE_TTL_SECONDS


def classify(repo: "Repo", table: RepoTable | None) -> Classification:
    """Say which of *repo*'s test files *table* still describes, and which it does not.

    One ``stat`` per stored file, directory and dependency, per watched
    conftest and per ``env`` path, and no listing: a file whose stat matches
    its record is fresh, one whose stat moved is changed, one that is gone is
    deleted. A directory whose stat moved gained or lost an entry, so it is a
    candidate directory (:attr:`Classification.candidate_dirs`) for the next
    collection, and a conftest it gained is new (one more ``stat`` per moved
    directory). A conftest that changed, appeared or went away marks
    everything under its directory changed, and makes each directory there a
    candidate.

    With no table, an ``env`` that moved (a pytest config, the settings, the
    installed distributions, a version), or a table whose whole tree was last
    collected longer ago than the TTL, the table vouches for nothing:
    :attr:`Classification.whole_tree` is set and every stored file still there
    is changed.

    An edit that keeps both mtime and size is invisible, and so are tests
    generated from a non-Python data file. A run stays right because pytest
    collects what it runs; a completer can offer such a name until the file
    is next collected, or the TTL sends the tree to a whole collection.
    """
    env = current_env(repo)
    whole_tree = table is None or table.env != env or _expired(table)
    stored_files = table.files if table is not None else {}
    stored_dirs = table.dirs if table is not None else {}
    watched = _watched_conftests(repo)
    test_dirs = {str(t) for t in repo.tests}

    stats = {key: _stat_pair(Path(key)) for key in stored_files}
    status = _stored_status(stored_files, stats, watched)
    for key in sorted(watched - set(stored_files)):
        stats[key] = _stat_pair(Path(key))
        status[key] = _NEW if stats[key] is not None else _ABSENT
    dir_stats = {key: _stat_pair(Path(key)) for key in stored_dirs}
    for key in sorted(test_dirs - set(stored_dirs)):
        dir_stats[key] = _stat_pair(Path(key))

    moved: list[str] = []
    if not whole_tree:
        for key, before in stored_dirs.items():
            now = dir_stats[key]
            if now is None or now == before:
                continue
            moved.append(key)
            conftest = str(Path(key) / CONFTEST_FILENAME)
            if conftest not in stats:
                stats[conftest] = _stat_pair(Path(conftest))
                if stats[conftest] is not None:
                    status[conftest] = _NEW

    stored_deps = table.deps if table is not None else {}
    dep_stats = {k: stats[k] if k in stats else _stat_pair(Path(k)) for k in stored_deps}
    _invalidate_dependents(stored_files, status, stored_deps, dep_stats)

    dirs = {k: v for k, v in dir_stats.items() if v is not None or k in test_dirs}
    candidate_dirs: set[str] = set()
    if whole_tree:
        for key, state in status.items():
            if state == _FRESH:
                status[key] = _CHANGED
    else:
        roots = _invalidate_under_conftests(status)
        candidate_dirs.update(moved)
        candidate_dirs.update(
            key for key in dirs if dirs[key] is not None and any(_under(key, r) for r in roots)
        )
        # A conftest's own directory, even one the table does not hold (its
        # listing never completed while that conftest was broken).
        candidate_dirs.update(r for r in roots if any(_under(r, t) for t in test_dirs))

    def listed(state: str) -> list[Path]:
        return sorted(Path(k) for k, s in status.items() if s == state)

    return Classification(
        fresh=listed(_FRESH),
        changed=listed(_CHANGED),
        new=listed(_NEW),
        deleted=listed(_DELETED),
        whole_tree=whole_tree,
        env=env,
        stats=stats,
        dirs=dirs,
        deps=dep_stats,
        candidate_dirs=sorted(Path(k) for k in candidate_dirs),
    )


# --- writers -------------------------------------------------------------------


_STDLIB = str(Path(os.__file__).parent) + os.sep
"""The standard library's directory (``os`` is always loaded, so this reads no file)."""


def _tracked_dependency(path: str) -> bool:
    """Whether a dependency file gets its own stat: every module ``env`` cannot see move.

    ``env`` stats the site-packages directories, so an installed distribution
    (anything under ``site-packages`` or ``dist-packages``) is covered, and it
    names the interpreter, so the standard library is too. Everything else is
    tracked: the repo's own files, its libs, an editable install's source, a
    ``PYTHONPATH`` directory.
    """
    parts = Path(path).parts
    if "site-packages" in parts or "dist-packages" in parts:
        return False
    return not path.startswith(_STDLIB)


def _missing_dependencies(
    records: dict[str, FileRecord], classification: Classification
) -> set[str]:
    """Return the tracked dependencies *records* name that have no file.

    A sourceless ``.pyc`` library names a source path that is not there. Kept,
    it would read as deleted on every run and re-collect its holders each
    time, and no edit could ever be seen through it. ``classify`` already
    stat'ed every stored dependency and every file; only one first seen now
    costs a ``stat``, once.
    """
    missing: set[str] = set()
    for dep in {d for record in records.values() for d in record.deps}:
        if not _tracked_dependency(dep):
            continue
        if dep in classification.deps:
            stat = classification.deps[dep]
        elif dep in classification.stats:
            stat = classification.stats[dep]
        else:
            stat = _stat_pair(Path(dep))
        if stat is None:
            missing.add(dep)
    return missing


def _merged_dirs(
    repo: "Repo",
    stored: RepoTable | None,
    classification: Classification,
    listed: dict[str, list[int] | None] | None,
    *,
    whole_tree: bool,
) -> dict[str, list[int] | None]:
    """Return the directory table after a collection that listed *listed*; see updated_table."""
    dirs = dict(classification.dirs)
    before = stored.dirs if stored is not None else {}
    if listed is None:
        # Nothing says a candidate was listed: each stays one.
        for key in map(str, classification.candidate_dirs):
            dirs[key] = before.get(key)
        return dirs
    test_dirs = {str(t) for t in repo.tests}
    meant = set(dirs) if whole_tree else {str(d) for d in classification.candidate_dirs}
    for key in meant - set(listed) - test_dirs:
        # Meant to be listed, and pytest did not go there: it is not a test directory now.
        dirs.pop(key, None)
    for key, stat in listed.items():
        if stat is not None:
            dirs[key] = stat
        elif key in before:
            dirs[key] = before[key]
        elif key in test_dirs:
            dirs[key] = None
        else:
            dirs.pop(key, None)
    return dirs


def updated_table(
    repo: "Repo",
    stored: RepoTable | None,
    classification: Classification,
    records: dict[str, FileRecord],
    *,
    registered_markers: list[str] | None = None,
    whole_tree: bool = False,
    dirs: dict[str, list[int] | None] | None = None,
    dep_stats: dict[str, list[int] | None] | None = None,
) -> RepoTable:
    """Merge a collection's *records* into *stored*, per *classification*.

    *records* maps an absolute file path to what was read from it. A record
    given with a stat keeps it (the reader took it just before reading); one
    given with ``None`` is stamped with the stat :func:`classify` saw, which
    also precedes the read. Either way an edit made while the file was read
    leaves the record stale, never falsely fresh. A record with no
    ``collected_at`` is dated now. Pass a record, empty or not, for every
    file the collection considered. Then, per file:

    - a record given: stored as given;
    - fresh: the stored record is kept;
    - deleted: dropped (a watched conftest stays watched);
    - a conftest that is changed or new: re-stamped with its stat;
    - any other changed or new file: kept or added with a ``None`` stat (and
      its old ``collected_at``), so the next :func:`classify` still says
      changed. A changed conftest whose subtree was not read therefore cannot
      leave that subtree looking fresh.

    A record that says what the stored one says is the stored one, date
    included: a collection that learned nothing leaves the table equal to
    *stored*, and its caller writes nothing.

    A record's ``deps`` are kept unless ``env`` already covers them (an
    installed distribution, the standard library) or they have no file (a
    sourceless ``.pyc``), and each is stamped with
    the stat *classification* saw before the read, or ``None`` when it is
    first seen now (see :attr:`RepoTable.deps`). *dep_stats* overrides
    that stat for the dependencies it names: a library the process imported
    before this collection, stamped with the stat it was imported at, so
    an edit the process cannot see leaves its holders stale.

    *dirs* maps each directory the collection set out to list in full to the
    stat it had before pytest listed it, or ``None`` when that listing did
    not complete (a conftest at or under it failed): such a directory keeps
    the stat the table had, so the next collection lists it again. A
    directory the collection meant to list (a candidate directory, or any
    directory of a *whole_tree* collection) that is not in *dirs* is one
    pytest no longer enters, and leaves the table. With *dirs* ``None`` (no
    report), a candidate directory keeps the stat the table had, so it stays
    a candidate.

    *whole_tree* says the collection covered the whole tree (so does a
    whole-tree *classification*): the table is dated now
    (:attr:`RepoTable.generated_at`). *registered_markers* replaces the
    stored list when given; otherwise the stored one is kept unless the
    ``env`` moved.
    """
    whole_tree = whole_tree or classification.whole_tree
    status: dict[str, str] = {}
    for state, paths in (
        (_FRESH, classification.fresh),
        (_CHANGED, classification.changed),
        (_NEW, classification.new),
        (_DELETED, classification.deleted),
    ):
        status.update((str(p), state) for p in paths)
    now = int(time.time())
    missing = _missing_dependencies(records, classification)

    def taken(record: FileRecord, stat: list[int] | None, key: str) -> FileRecord:
        own = record.stat if record.stat is not None else stat
        deps = sorted(
            {d for d in record.deps if d != key and d not in missing and _tracked_dependency(d)}
        )
        return replace(record, stat=own, deps=deps, collected_at=record.collected_at or now)

    stored_files = stored.files if stored is not None else {}
    files: dict[str, FileRecord] = {}
    for key, state in status.items():
        stat = classification.stats.get(key)
        if key in records:
            # What this collection read wins over what classify saw before it:
            # a file gone then, back by the time pytest read it, keeps this
            # record (stamped with the stat taken just before that read).
            files[key] = taken(records[key], stat, key)
        elif state == _DELETED:
            continue
        elif state == _FRESH:
            files[key] = stored_files[key]
        elif Path(key).name == CONFTEST_FILENAME:
            files[key] = FileRecord(stat=stat, collected_at=now)
        else:
            before = stored_files.get(key)
            files[key] = replace(before, stat=None) if before else FileRecord(None)
    for key in sorted(set(records) - set(files) - set(status)):
        files[key] = taken(records[key], _stat_pair(Path(key)), key)
    for key in sorted(_watched_conftests(repo) - set(files)):
        seen = key in classification.stats
        stat = classification.stats[key] if seen else _stat_pair(Path(key))
        files[key] = FileRecord(stat=stat, collected_at=now)
    for key, record in files.items():
        before = stored_files.get(key)
        if before is not None and replace(record, collected_at=before.collected_at) == before:
            files[key] = before  # read again and found as it was: it keeps its date
    if registered_markers is None:
        stale = stored is None or classification.whole_tree
        registered_markers = [] if stale or stored is None else list(stored.registered_markers)
    deps: dict[str, list[int] | None] = {}
    for dep in sorted({d for record in files.values() for d in record.deps}):
        # The stat from before this collection's read: classify's, which
        # stat'ed every stored dependency and every file. A dependency first
        # seen now has none, and its holders stay changed until it has one.
        if dep_stats is not None and dep in dep_stats:
            deps[dep] = dep_stats[dep]
        elif dep in classification.deps:
            deps[dep] = classification.deps[dep]
        else:
            deps[dep] = classification.stats.get(dep)
    merged = _merged_dirs(repo, stored, classification, dirs, whole_tree=whole_tree)
    return RepoTable(
        sut_dir=repo.sut_dir,
        env=classification.env,
        dirs=dict(sorted(merged.items())),
        files=dict(sorted(files.items())),
        deps=deps,
        registered_markers=sorted(registered_markers),
        generated_at=now if whole_tree else (stored.generated_at if stored is not None else 0),
    )


# --- what completion offers -----------------------------------------------------


@dataclass
class CompletionView:
    """What a completer offers for test names and markers, across repos."""

    names: list[str]
    """Every selectable name, sorted, each once."""
    markers: list[str]
    """Every marker name, sorted, each once."""


def completion_view(repos: "list[Repo]") -> CompletionView:
    """Test names and markers for a TAB, answered as the console-script shim answers one.

    Each repo's table is read and its ``env`` compared (:func:`current_env`:
    a handful of ``stat`` calls, whatever the corpus size); nothing a table
    tracks is stat'ed. A repo with no table, another ``env`` or an expired
    table is cold: this waits once for the collect child
    (:func:`otto.config.completion_cache.run_collect_child`, bounded, locked,
    with a cooldown after a failure) and answers from what it wrote, and
    never offers a cold table. Every other table offers what each record
    held when pytest last read it (:attr:`RepoTable.names`), so a file
    added, edited or deleted since shows up one check late. The check is the
    collect child's: when one is due (:func:`tables_check_due`), the child
    is asked for, detached, once the answer is written
    (:func:`otto.config.completion_cache.request_collect_refresh`).
    """
    from .completion_cache import request_collect_refresh, run_collect_child

    def warm() -> list[RepoTable | None]:
        """Each repo's table, or ``None`` for a cold one."""
        stored = read_tables(repos)
        tables: list[RepoTable | None] = []
        for repo in repos:
            table = stored.get(str(repo.sut_dir))
            cold = table is None or table.env != current_env(repo) or _expired(table)
            tables.append(None if cold else table)
        return tables

    tables = warm()
    if any(table is None for table in tables) and run_collect_child():
        tables = warm()
    names: set[str] = set()
    markers: set[str] = set()
    for table in tables:
        if table is not None:
            names.update(table.names)
            markers.update(table.markers)
    cache_path = _cache_path()
    if cache_path is not None and tables_check_due(cache_path.parent):
        request_collect_refresh()
    return CompletionView(names=sorted(names), markers=sorted(markers))


# --- the collect child -------------------------------------------------------------


@contextlib.contextmanager
def _deadline(seconds: float, on_expiry: Callable[[], None]) -> "Iterator[None]":
    """Call *on_expiry* from ``SIGALRM`` if the body is still running after *seconds*.

    A timer already armed (an outer caller's) is put back afterwards with
    what it had left. Off the main thread, or with no ``SIGALRM``, the body
    runs unbounded.
    """
    import signal

    alarm = getattr(signal, "SIGALRM", None)
    try:
        previous = signal.signal(alarm, lambda _signum, _frame: on_expiry()) if alarm else None
    except ValueError:  # not the main thread
        previous, alarm = None, None
    if alarm is None:
        yield
        return
    started = time.monotonic()
    outer = signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(alarm, previous)
        if outer[0]:
            left = max(outer[0] - (time.monotonic() - started), 0.001)
            signal.setitimer(signal.ITIMER_REAL, left, outer[1])


def _expire_collect_child(lock: Path) -> None:
    """End a collect child that ran past its cap: stamp the cooldown, free the lock, exit 1.

    ``os._exit``: the child is mid-collection, possibly inside an import, and
    nothing it holds is worth unwinding, but for a cache write it may have
    been in the middle of: its temporary file is removed first.
    """
    from .completion_cache import discard_own_temporary_files, stamp_collect_cooldown

    discard_own_temporary_files()
    stamp_collect_cooldown("timed out")
    lock.unlink(missing_ok=True)
    os._exit(1)


def collect_child_main(
    load_repos: "Callable[[], list[Repo]]",
    refresh: "Callable[[list[Repo]], list[RepoTable | None]]",
) -> int:
    """Bring every repo's table up to date, printing nothing: the collect child's whole job.

    Run by :func:`otto.cli.main.entry` in a process of its own, spawned by a
    completer: waited for when a table is cold, detached when a check of
    the tables is due. It takes the collect lock first (and exits at once
    when another process holds it), then arms its deadline
    (:data:`~otto.config.completion_cache.COLLECT_TIMEOUT_SECONDS`), and only
    then calls *load_repos*, the bootstrap that imports the repos' init
    modules: a burst of TABs starts one child that does the work, and a hung
    init module is stopped like a hung collection. *refresh* is the run's
    own (``otto.suite.run._refresh_tables``): it seeds a cold table, re-reads
    what moved in a warm one and leaves a current one as it is. The check
    is recorded either way (:func:`mark_tables_checked`). A repo whose table
    it could not bring up to date, a directory whose listing did not finish
    included, stamps the cooldown, as does any other failure, so the next
    TAB neither waits nor spawns again for a while. Returns the exit code.
    """
    from .completion_cache import (
        COLLECT_LOCK_FILENAME,
        COLLECT_OWNER_ENV_VAR,
        COLLECT_TIMEOUT_SECONDS,
        _acquire_collect_lock,
        collect_cooldown_active,
        stamp_collect_cooldown,
    )

    cache_path = _cache_path()
    if cache_path is None or collect_cooldown_active():
        return 0
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    lock = cache_path.parent / COLLECT_LOCK_FILENAME
    if not _acquire_collect_lock(lock, owner=os.environ.get(COLLECT_OWNER_ENV_VAR, "")):
        return 0
    try:
        with _deadline(COLLECT_TIMEOUT_SECONDS, lambda: _expire_collect_child(lock)):
            repos = load_repos()
            failed = [r.name for r, t in zip(repos, refresh(repos), strict=True) if t is None]
            if failed:
                stamp_collect_cooldown(f"could not collect repo {failed[0]!r}")
                return 1
    except Exception as exc:  # noqa: BLE001 — a child that failed says so with its exit code
        stamp_collect_cooldown(f"{type(exc).__name__}: {exc}")
        return 1
    finally:
        lock.unlink(missing_ok=True)
    mark_tables_checked(cache_path.parent)
    return 0
