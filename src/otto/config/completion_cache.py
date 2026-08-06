"""Shell-completion cache (Phase B).

Tab completion invokes ``otto`` just far enough to walk the Typer command
tree. The expensive step during that walk is not parsing CLI args — it's the
side effects in :mod:`otto.config` that populate dynamic subcommands:

- :meth:`Repo.import_init_modules` — imports every user-defined instruction
  module so ``@instruction()`` decorators can register into ``INSTRUCTIONS``.
- :meth:`Repo.import_test_files` — exec's every ``test_*.py`` so
  ``OttoSuite.__init_subclass__`` can auto-register ``Test*``-named classes
  into the ``SUITES`` registry.

Both execute arbitrary user code. For completion all we actually need is the
*names* those decorators would register and the *option schemas* the user can
tab-complete against. This module captures both in a small JSON file and,
when the cache is valid, lets the caller skip the user code entirely.

Cache location
--------------

``$OTTO_XDIR/.otto/completion_cache.json``. If ``OTTO_XDIR`` is not set,
caching is disabled and completion always falls through to the slow path.

Cache schema (version 9)
------------------------

Single flat map, keyed by fingerprint hex digest. Each entry records both the
schema version and the wall-clock time it was generated so a reader can drop
stale entries without trusting the mtimes on-disk::

    {
        "<fingerprint>": {
            "schema_version": 8,
            "generated_at": 1745000000,
            "instructions": [
                {
                    "name": "install",
                    "options": [
                        {
                            "name": "debug",
                            "flags": ["--field/--debug"],
                            "kind": "bool",
                            "default": false,
                            "help": "...",
                        },
                        ...,
                    ],
                },
                ...,
            ],
            "suites": [{"name": "TestDevice", "options": [...]}, ...],
            "hosts": ["carrot_seed", "tomato_seed", ...],
            "hosts_by_lab": {"veggies": ["carrot_seed", "tomato_seed"], ...},
            "docker_hosts": ["carrot_seed", ...],
            "term_backends": ["ssh", "telnet", ...],
            "transfer_backends": [{"name": "scp", "host_families": ["unix"]}, ...],
            "labs": ["tech1", "tech2", ...],
            "tests": ["test_smoke", "TestDevice::test_reachable", ...],
            "commands": [
                {"name": "flash", "help": "...", "lab_free": false},
                # a third-party GROUP also carries recursive child metadata;
                # a flattening single-command app carries "options" instead
                # (both keys omitted when empty):
                {
                    "name": "e2etool",
                    "help": "...",
                    "lab_free": true,
                    "commands": [
                        {"name": "ping", "help": "...", "options": [...]},
                        {"name": "nested", "help": "...", "commands": [...]},
                    ],
                },
                ...,
            ],
        }
    }

Collected test-name namespace
-----------------------------

Alongside the fingerprint entries, a single reserved key
``"__collected_tests__"`` holds the *pytest-collected* ``--tests`` names
(dynamically generated tests included), keyed by the same fingerprint::

    {
        "__collected_tests__": {
            "<fingerprint>": {
                "schema_version": 1,
                "generated_at": 1745000000,
                "names": ["test_x", "TestX::test_x", ...],
            }
        }
    }

It is written only by a deliberate collection (a real ``otto test --list-tests``
run, or the bounded subprocess the ``--tests`` completer spawns at tab time) —
never by the slow-path writer, which must not run a collection pass. Keeping it
in its own key means the two writers touch disjoint data and can't clobber.

Fingerprint
-----------

sha256 over ``(path, mtime_ns, size)`` triples for every file whose change
would alter the registered name sets: each SUT's ``settings.toml``, every
``.py`` file under any ``init`` module, every test file (pytest's default
``python_files`` — or the repo's own override of it — plus ``conftest.py``)
anywhere under a configured ``tests`` directory or on the path from one up to
the SUT root, every ``lab.json`` under a configured ``labs`` search path, and
every file pytest would read settings from (``python_files`` decides which
files count, so it is a source in its own right). File contents are never
read, so the fingerprint is cheap to compute even when SUTs are large.

Hashing a whole ``pyproject.toml`` for one key means a version bump or a lint
tweak also invalidates completion. That is the conservative direction and the
opposite of the trade made for ``tests/`` — accepted here because the file is
one stat, and being wrong about ``python_files`` blinds both readers.

A stale fingerprint is always safe: the fast path is skipped, the slow path
runs as normal and rewrites the cache afterward.

A *constant* fingerprint is the failure mode worth knowing about. A repo whose
``[lab]`` backend is not the built-in json one, or which configures a
``[reservations]`` backend, keeps its inventory somewhere no stat can see —
so edits to it never move the digest, even though the repo may still have a
``lab.json`` on disk for other reasons. Those repos fall back to a short TTL
(``UNFINGERPRINTED_CACHE_TTL_SECONDS``) rather than the usual day, which is the
only staleness bound available without querying the backend on the completion
fast path.
"""

import contextlib
import hashlib
import inspect
import json
import logging
import os
import tempfile
import time
import types
from fnmatch import fnmatchcase
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Union, get_args, get_origin

from ..errors import is_containable
from .repo import configured_python_files, pytest_config_paths

if TYPE_CHECKING:
    import threading
    from collections.abc import Callable, Collection, Sequence

    from ..labs import HostSummary
    from .repo import Repo


COMPLETION_ENV_VAR = "_OTTO_COMPLETE"
CACHE_FILENAME = "completion_cache.json"

# Bump when the on-disk schema changes in a way older readers can't parse.
# v9: added "labs" and "tests" (sources for --lab / --tests completion).
# v10: added "hosts_by_lab" (lab-scoped `otto host <TAB>` fast path).
# v11: host-ID sources now hash lab.json (renamed from hosts.json), so cached
#      fingerprints reference a different filename.
SCHEMA_VERSION = 11

LAB_FILENAME = "lab.json"

# One home, two readers: `collect_test_names` decides which files to PARSE for
# names, and `compute_fingerprint` decides which files to STAT for
# invalidation. Those two sets must be the same set — a file the scan reads but
# the digest ignores is a name the cache can serve forever after it stops being
# true. Both now ask the REPO, because a project that overrides pytest's
# `python_files` collects from filenames the defaults never match.
#
# `Repo.iter_test_files` is a THIRD reader and deliberately not one of these:
# it EXECUTES what it returns, at bootstrap, on every otto command. Its
# narrowness is a contract rather than an oversight — see its docstring for
# the reasoning and for the `tests`-list escape hatch. Do not "fix" it to
# match these two.

# Directories pytest's default `norecursedirs` skips, minus the two patterns
# handled by prefix/suffix in `_is_norecurse_dir`.
_NORECURSE_NAMES = frozenset(
    {
        # pytest's default `norecursedirs`, minus the two entries handled as
        # patterns in `_is_norecurse_dir` (`*.egg` and `.*`).
        "_darcs",
        "build",
        "CVS",
        "dist",
        "node_modules",
        "venv",
        "{arch}",
        # NOT part of norecursedirs — pytest skips it with its own hardcoded
        # check, one `scandir` per package dir that nothing would collect from.
        "__pycache__",
    }
)

# Fingerprint-only: conftest.py holds no collectable test of its own, so the
# static scan never parses it, but `pytest_generate_tests` and parametrizing
# fixtures live there and DO change the collected set stored under
# COLLECTED_TESTS_KEY (which is keyed by this same digest).
CONFTEST_FILENAME = "conftest.py"

# Cache entries older than this (seconds) are treated as a miss. Forces the
# slow path to run periodically so annotation / option changes that don't
# move any tracked file's mtime still eventually refresh.
CACHE_TTL_SECONDS = 24 * 60 * 60

# The TTL that applies when a repo's completion data comes from somewhere the
# fingerprint cannot stat: a custom [lab] host source, or any [reservations]
# backend (the built-in json one supplies no usernames, so that field is
# always custom-backend data). Such a source contributes real completion data
# but NO invalidation signal — the digest never moves however much the
# inventory changes — so the TTL is the only staleness floor, and a day is far
# too long for live inventory.
#
# Deliberately NOT a backend-supplied revision token: `compute_fingerprint`
# runs on the completion fast path, and querying a possibly-networked backend
# there is exactly the cost this cache exists to avoid — it would make every
# TAB keystroke depend on the inventory service being reachable.
UNFINGERPRINTED_CACHE_TTL_SECONDS = 5 * 60


# --- Collected (pytest-accurate) test-name cache, for --tests completion -----
#
# The ``tests`` field above is an ``ast``-only *floor* — every statically
# written ``def test_*`` / ``Test*`` method, discovered without importing a
# thing. The *collected* set below comes from a real pytest collection, so it
# also covers dynamically generated tests (``pytest_generate_tests`` /
# fixture-driven parametrization) and matches the repo's actual pytest config.
# It selects by *base* name — ``otto test --tests`` matches a bare name against
# every parametrization — so per-parametrization ids are deliberately not part
# of it.
#
# It lives under its own reserved top-level key (never a real fingerprint), so
# writing it never disturbs the main fingerprint entries. That separation is
# load-bearing: the slow-path writer rewrites a whole main entry on every real
# command and must NEVER run a collection pass, while this set is warmed only
# by a deliberate collection (a real ``otto test`` run, or a bounded subprocess
# spawned at tab time). The two writers touch disjoint keys and can't clobber.
COLLECTED_TESTS_KEY = "__collected_tests__"
COLLECTED_SCHEMA_VERSION = 1

# Env var that flips ``otto`` into the one-shot "collect and print test names"
# subprocess the completer spawns to warm the collected cache. Handled as an
# early exit in :func:`otto.cli.main.entry`, before the normal CLI runs.
DUMP_TESTS_ENV_VAR = "_OTTO_DUMP_TEST_NAMES"

# Hard cap on the tab-time collection subprocess: a cold ``--tests`` TAB blocks
# at most this long before falling back to the static floor. "Slow on the first
# attempt is better than no completion" — but bounded, never a wedged shell.
COLLECT_TIMEOUT_SECONDS = 15

# After a failed / timed-out tab-time collection, skip re-collecting at tab time
# for this long. Keeps a repo that can't collect within the timeout from costing
# a slow TAB on *every* keystroke — at most one per cooldown window.
COLLECT_COOLDOWN_SECONDS = 60

COLLECT_LOCK_FILENAME = ".completion_collect.lock"
# A lock older than this is treated as orphaned (its holder died) and stolen,
# so a crashed collector can't block warming forever.
COLLECT_LOCK_STALE_SECONDS = COLLECT_TIMEOUT_SECONDS + 30

# Frame the dumped payload so the parent can recover the names even if repo
# discovery emits stray stdout before them.
_DUMP_BEGIN = "__OTTO_TESTS_BEGIN__"
_DUMP_END = "__OTTO_TESTS_END__"


# Python type <-> serialized kind. Kept intentionally small: these are the
# only types whose tab-completion shape (value vs. flag, how many args) we
# need to recreate. Anything not in this map is "unsupported" for caching
# purposes — the option is logged at DEBUG and dropped from the cached
# schema; completion still works on the slow path.
_TYPE_TO_KIND: dict[Any, str] = {
    str: "str",
    int: "int",
    float: "float",
    bool: "bool",
    Path: "path",
}
_KIND_TO_TYPE: dict[str, Any] = {v: k for k, v in _TYPE_TO_KIND.items()}


def is_completion_mode() -> bool:
    """Return True when otto is being invoked by shell completion."""
    return bool(os.environ.get(COMPLETION_ENV_VAR))


def _cache_path() -> Path | None:
    """Return the cache file path, or ``None`` when caching is disabled.

    Caching requires ``OTTO_XDIR`` to be set. Without it we can't pick a
    stable per-user location, so we skip caching entirely and fall back to
    the slow path every time.
    """
    # Function-local import: this module is loaded early during config
    # bootstrap, so defer the models import to call time. A fresh
    # OttoEnvSettings() re-reads OTTO_XDIR each call (tests monkeypatch it).
    from ..models.settings import OttoEnvSettings

    xdir = OttoEnvSettings().xdir  # Path | None ("" normalized to None)
    if xdir is None:
        return None
    return xdir / ".otto" / CACHE_FILENAME


def clear_cache() -> bool:
    """Delete the completion cache file if it exists.

    Returns True if a file was removed, False otherwise. Surface for the
    ``--clear-autocomplete-cache`` CLI escape hatch.
    """
    cache_path = _cache_path()
    if cache_path is None or not cache_path.is_file():
        return False
    try:
        cache_path.unlink()
    except OSError:
        return False
    else:
        return True


def _has_unfingerprinted_source(repos: list["Repo"]) -> bool:
    """Report whether any repo's completion data comes from outside the digest.

    Two such sources, both pure dict reads of already-parsed settings — no
    pydantic, no backend construction, no I/O, so this is safe on the
    completion fast path:

    - a ``[lab] backend`` other than ``"json"`` (hosts, lab names). ``backend``
      defaults to ``"json"`` (:class:`~otto.models.settings.LabConfigSpec`), so
      an absent ``[lab]`` block reads as json.
    - any ``[reservations]`` backend (``--as-user`` names). The built-in json
      reservation backend does not implement username completion at all, so
      that field is populated *exclusively* by custom, typically networked
      backends — the same constant-digest problem, one field over.

    Switching either backend rewrites ``settings.toml``, whose mtime IS in the
    digest, so a repo can never inherit a cache entry written under a
    different backend choice. That invariant is what makes an entry-wide (not
    per-repo) TTL correct.

    Known limitation: a repo may re-register ``"json"`` with a replacement
    class (``register_lab_repository("json", ..., overwrite=True)``), which
    this cannot see without constructing the backend. ``build_lab_repository``
    hardcodes the ``cls(search_paths=...)`` contract for that name, so a
    replacement is deliberately impersonating the file backend; it inherits
    file-backed invalidation and ``--clear-autocomplete-cache``.
    """
    for repo in repos:
        backend = getattr(repo, "lab_settings", {}).get("backend", "json")
        if isinstance(backend, str) and backend != "json":
            return True
        # `isinstance(..., dict)` for the same reason as the `str` check above:
        # a test double's auto-attribute is truthy but is not settings.
        reservations = getattr(repo, "reservation_settings", None)
        if isinstance(reservations, dict) and reservations:
            return True
    return False


def _cache_ttl_seconds(repos: list["Repo"]) -> int:
    """Effective completion-cache TTL for *repos*.

    Shortened when any repo's completion data comes from a source the
    fingerprint cannot see — see :data:`UNFINGERPRINTED_CACHE_TTL_SECONDS`.

    Applies to the main completion entry only. The collected-test-name cache
    keeps the long TTL: its content tracks test files, and the fingerprint
    hashes them — the same ``python_files`` the scan reads (the repo's own,
    where it configures one), recursively, plus every ``conftest.py`` under
    the tests dirs and on the path up to the SUT root, plus the pytest config
    files that decide which of those patterns apply.

    Not total, and the residue is named rather than papered over: pytest
    FOLLOWS symlinked directories and both readers here do not, so a tests
    tree assembled by symlink is neither offered nor hashed; and a repo that
    narrows pytest's ``norecursedirs`` collects from directories these readers
    prune.
    """
    if _has_unfingerprinted_source(repos):
        return UNFINGERPRINTED_CACHE_TTL_SECONDS
    return CACHE_TTL_SECONDS


def _match_py_files(test_dir: Path, patterns: "Sequence[str]") -> set[Path]:
    """FILES under *test_dir* whose name matches *patterns*, in ONE pruned walk.

    Recursive, because a test tree is a tree: otto's own ``tests/`` has 405
    test files and not one of them at the top level, so a non-recursive glob
    contributes nothing at all for a repo laid out that way.

    ``os.walk`` rather than ``rglob``, for three reasons that all matter on
    the completion fast path (this runs twice per ``--tests`` TAB):

    - It PRUNES the directories pytest's own ``norecursedirs`` skips. ``rglob``
      descended into ``.venv`` / ``.tox`` / ``.git``; a venv living under a
      tests dir measured 83 ms warm, for files pytest would never collect.
    - One walk, not one per pattern. Every pattern ends in ``.py``, so a
      single traversal plus a name match is set-identical to N ``rglob``s for
      a fraction of the directory round-trips — which is what costs on a
      network filesystem.
    - It yields files and directories separately, so a DIRECTORY named
      ``test_x.py`` is no longer matched and stat'd as though it were a file.

    ``fnmatchcase`` rather than ``fnmatch``: the latter normalizes case
    per-platform, which would silently widen the match on a case-insensitive
    filesystem.
    """
    found: set[Path] = set()
    for root, dirs, files in os.walk(test_dir):
        dirs[:] = [d for d in dirs if not _is_norecurse_dir(d)]
        base = Path(root)
        found.update(base / name for name in files if any(fnmatchcase(name, q) for q in patterns))
    return found


def _is_norecurse_dir(name: str) -> bool:
    """Mirror of pytest's default ``norecursedirs``.

    ``*.egg .* _darcs build CVS dist node_modules venv {arch}`` — pytest never
    collects from these, so neither reader should walk them.
    """
    return name.startswith(".") or name.endswith(".egg") or name in _NORECURSE_NAMES


def _test_sources(test_dir: Path, sut_dir: Path, patterns: "Sequence[str]") -> set[Path]:
    """Every path whose edit can change a ``--tests`` name under *test_dir*.

    A set, because ``test_a_test.py`` matches both patterns. Paths that do not
    exist are fine and wanted — :func:`_hash_file` records them as ``missing:``,
    so the digest moves when one appears.
    """
    found = _match_py_files(test_dir, [*patterns, CONFTEST_FILENAME])
    # conftest.py ABOVE the tests dir counts too. `Repo.collect_tests` passes
    # the tests dirs as pytest args with the SUT as rootdir, so a conftest
    # anywhere between them is loaded, and a `pytest_generate_tests` there
    # parametrizes what lands in the collected set.
    for ancestor in test_dir.parents:
        if ancestor == sut_dir or sut_dir in ancestor.parents:
            found.add(ancestor / CONFTEST_FILENAME)
        if ancestor == sut_dir:
            break
    return found


def _hash_file(h: "hashlib._Hash", path: Path) -> None:
    try:
        st = path.stat()
    except OSError:
        h.update(f"missing:{path}\n".encode())
        return
    h.update(f"{path}|{st.st_mtime_ns}|{st.st_size}\n".encode())


def compute_fingerprint(repos: list["Repo"]) -> str:
    """Stat-based sha256 of every file that contributes instruction/suite names."""
    h = hashlib.sha256()
    for repo in sorted(repos, key=lambda r: str(r.sut_dir)):
        _hash_file(h, repo.sut_dir / ".otto" / "settings.toml")

        # Init-module files: resolve each `init` name under the configured
        # `libs` directories. Either a package directory or a plain .py file.
        for init_mod in repo.init:
            mod_base = init_mod.split(".")[0]
            resolved = False
            for lib in repo.libs:
                mod_dir = lib / mod_base
                mod_file = lib / f"{mod_base}.py"
                if mod_dir.is_dir():
                    for py in sorted(mod_dir.rglob("*.py")):
                        _hash_file(h, py)
                    resolved = True
                elif mod_file.is_file():
                    _hash_file(h, mod_file)
                    resolved = True
            if not resolved:
                h.update(f"unresolved:{init_mod}\n".encode())

        # The pytest config files themselves: `python_files` decides which
        # files below even count, so an edit to it must move the digest as
        # surely as an edit to a test. Missing ones hash as "missing:", which
        # is what lets ADDING a pytest.ini invalidate the cache.
        for cfg in pytest_config_paths(repo.sut_dir):
            _hash_file(h, cfg)

        patterns = configured_python_files(repo.sut_dir)
        for test_dir in repo.tests:
            if test_dir.is_dir():
                for t in sorted(_test_sources(test_dir, repo.sut_dir, patterns)):
                    _hash_file(h, t)

        # Host-ID sources: lab.json under each configured lab search path.
        # Adding these to the fingerprint lets the cache self-invalidate on
        # edits. A non-file backend has no such signal — its digest never
        # moves — so it falls back to a short TTL instead
        # (_cache_ttl_seconds / UNFINGERPRINTED_CACHE_TTL_SECONDS).
        for lab_path in repo.labs:
            _hash_file(h, lab_path / LAB_FILENAME)

    return h.hexdigest()


# ---------------------------------------------------------------------------
# Option serialization — convert a live Typer command callback's signature
# into a JSON-safe list of {name, flags, kind, default, help} dicts.
# ---------------------------------------------------------------------------


def _unwrap_optional(t: Any) -> Any:
    """Strip a single ``Optional[...]`` wrapper, leaving other types intact."""
    origin = get_origin(t)
    is_union = origin is Union or isinstance(t, types.UnionType)
    if not is_union:
        return t
    non_none = [a for a in get_args(t) if a is not type(None)]
    if len(non_none) == 1:
        return non_none[0]
    return t


def _type_to_kind(base: Any) -> str | None:
    """Map a Python type to the cache's ``kind`` tag, or ``None`` if unsupported."""
    base = _unwrap_optional(base)
    if base in _TYPE_TO_KIND:
        return _TYPE_TO_KIND[base]
    if get_origin(base) is list and get_args(base) == (str,):
        return "str_list"
    return None


def _extract_flags(option_info: Any) -> list[str]:
    """Return the user-authored flag strings from a ``typer.Option`` instance.

    Typer stores the first positional flag as the info's ``default`` attribute
    and the rest in ``param_decls``; concatenate them in declaration order so
    the rebuilder reproduces the original call.
    """
    flags: list[str] = []
    primary = getattr(option_info, "default", None)
    if isinstance(primary, str) and (primary.startswith("-") or "/" in primary):
        flags.append(primary)
    flags.extend(getattr(option_info, "param_decls", ()) or ())
    return flags


def _json_safe_default(default: Any) -> Any:
    """Coerce a parameter default to a JSON-serializable form."""
    if default is inspect.Parameter.empty or default is Ellipsis:
        return None
    if isinstance(default, Path):
        return str(default)
    if isinstance(default, (str, int, float, bool)) or default is None:
        return default
    # Lists of scalars are the only composite we care to round-trip (str_list).
    if isinstance(default, list):
        try:
            json.dumps(default)
        except TypeError:
            return None
        else:
            return default
    return None


def _serialize_options(
    callback: Any,
    *,
    command_name: str,
) -> list[dict[str, Any]] | None:
    """Convert a Typer command callback's signature into cache-shape dicts.

    Returns ``None`` (not an empty list) when any parameter uses an
    annotation form we don't know how to round-trip — that causes the
    command to be skipped entirely rather than cached with a half-signature.
    """
    log = logging.getLogger(__name__)
    try:
        sig = inspect.signature(callback)
    except (TypeError, ValueError) as e:  # pragma: no cover — paranoia
        log.debug(
            f"completion-cache: skipping {command_name!r}, signature inspection failed: {e!r}",
        )
        return None

    import typer  # lazy: this runs at cache-seed time, not at module import

    options: list[dict[str, Any]] = []
    for pname, param in sig.parameters.items():
        ann = param.annotation
        # The suite runner carries a Typer-injected ``ctx: typer.Context``
        # parameter (used to read run options from ``ctx.meta``). It is not a CLI
        # option and has no ``Annotated[...]`` metadata, so skip it rather than
        # treating the whole command as un-cacheable.
        if ann is typer.Context:
            continue
        if get_origin(ann) is not Annotated:
            log.debug(
                f"completion-cache: skipping option {command_name}.{pname!r} — "
                f"annotation {ann!r} is not Annotated[...]",
            )
            return None
        args = get_args(ann)
        base = args[0]
        # OptionInfo lives at module path typer.models.OptionInfo; match on
        # attribute shape to avoid importing typer at module load.
        meta = next(
            (a for a in args[1:] if hasattr(a, "param_decls")),
            None,
        )
        if meta is None:
            log.debug(
                f"completion-cache: skipping option {command_name}.{pname!r} — "
                f"no typer.Option metadata in annotation",
            )
            return None

        kind = _type_to_kind(base)
        if kind is None:
            log.debug(
                f"completion-cache: skipping option {command_name}.{pname!r} — "
                f"unsupported annotation type {base!r}",
            )
            return None

        options.append(
            {
                "name": pname,
                "flags": _extract_flags(meta),
                "kind": kind,
                "default": _json_safe_default(param.default),
                "help": getattr(meta, "help", None) or "",
            }
        )
    return options


# ---------------------------------------------------------------------------
# Read / write
# ---------------------------------------------------------------------------


def read_cache(repos: list["Repo"]) -> dict[str, Any] | None:
    """Return the cached command lists for the current fingerprint, or ``None``.

    ``None`` means any of: caching disabled, empty repos (would produce the
    empty-sha256 fingerprint and poison the cache for other shells), cache
    file missing, cache file corrupt, fingerprint mismatch, schema mismatch,
    or TTL expired. In every case the caller should fall back to the slow
    path.

    On success returns a dict with ``instructions``, ``suites``, ``hosts``,
    ``docker_hosts``, ``term_backends``, ``transfer_backends``, and
    ``commands`` keys. The first two are lists of
    ``{"name": str, "options": [...]}`` dicts; ``hosts`` and ``docker_hosts``
    are plain lists of host-ID strings; ``term_backends`` is a list of
    backend-name strings; ``transfer_backends`` is a list of
    ``{"name": str, "host_families": [str, ...]}`` dicts; ``commands`` is a
    list of ``{"name": str, "help": str | None, "lab_free": bool}`` dicts for
    third-party top-level CLI commands (default ``[]`` when absent).
    """
    if not repos:
        return None

    cache_path = _cache_path()
    if cache_path is None or not cache_path.is_file():
        return None
    try:
        data = json.loads(cache_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None

    fingerprint = compute_fingerprint(repos)
    entry = data.get(fingerprint)
    if not isinstance(entry, dict):
        return None
    if entry.get("schema_version") != SCHEMA_VERSION:
        return None
    generated_at = entry.get("generated_at")
    if not isinstance(generated_at, (int, float)):
        return None
    if time.time() - generated_at > _cache_ttl_seconds(repos):
        return None
    instructions = entry.get("instructions")
    suites = entry.get("suites")
    hosts = entry.get("hosts")
    hosts_by_lab = entry.get("hosts_by_lab", {})
    docker_hosts = entry.get("docker_hosts", [])
    term_backends = entry.get("term_backends", [])
    transfer_backends = entry.get("transfer_backends", [])
    usernames = entry.get("usernames", [])
    commands = entry.get("commands", [])
    labs = entry.get("labs", [])
    tests = entry.get("tests", [])
    if (
        not isinstance(instructions, list)
        or not isinstance(suites, list)
        or not isinstance(hosts, list)
        or not isinstance(hosts_by_lab, dict)
        or not isinstance(docker_hosts, list)
        or not isinstance(term_backends, list)
        or not isinstance(transfer_backends, list)
        or not isinstance(usernames, list)
        or not isinstance(commands, list)
        or not isinstance(labs, list)
        or not isinstance(tests, list)
    ):
        return None
    return {
        "instructions": instructions,
        "suites": suites,
        "hosts": hosts,
        "hosts_by_lab": hosts_by_lab,
        "docker_hosts": docker_hosts,
        "term_backends": term_backends,
        "transfer_backends": transfer_backends,
        "usernames": usernames,
        "commands": commands,
        "labs": labs,
        "tests": tests,
    }


def write_cache(  # noqa: PLR0913 — one keyword arg per cached name-set, by design
    repos: list["Repo"],
    instructions: list[dict[str, Any]],
    suites: list[dict[str, Any]],
    hosts: list[str],
    *,
    docker_hosts: list[str] | None = None,
    term_backends: list[str] | None = None,
    transfer_backends: list[dict[str, Any]] | None = None,
    usernames: list[str] | None = None,
    commands: list[dict[str, Any]] | None = None,
    labs: list[str] | None = None,
    tests: list[str] | None = None,
    hosts_by_lab: dict[str, list[str]] | None = None,
) -> None:
    """Write (or update) the entry for the current fingerprint.

    Skipped silently when repos is empty — an empty-repo fingerprint is the
    empty-string sha256, which any shell without ``OTTO_SUT_DIRS`` would
    also compute, and that would wrongly override a real entry's meaning.

    Atomic via ``tempfile`` + :func:`os.replace` so a concurrent otto
    invocation can't observe a half-written file. Stale entries from past
    SUT_DIRS combinations are left in place and ignored.
    """
    if not repos:
        return

    cache_path = _cache_path()
    if cache_path is None:
        return

    cache_path.parent.mkdir(parents=True, exist_ok=True)

    existing: dict[str, Any] = {}
    if cache_path.is_file():
        try:
            loaded = json.loads(cache_path.read_text())
            if isinstance(loaded, dict):
                existing = loaded
        except (OSError, json.JSONDecodeError):
            pass

    fingerprint = compute_fingerprint(repos)
    existing[fingerprint] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": int(time.time()),
        "instructions": instructions,
        "suites": suites,
        "hosts": hosts,
        "hosts_by_lab": hosts_by_lab or {},
        "docker_hosts": docker_hosts or [],
        "term_backends": term_backends or [],
        "transfer_backends": transfer_backends or [],
        "usernames": usernames or [],
        "commands": commands or [],
        "labs": labs or [],
        "tests": tests or [],
    }

    _atomic_write_json(cache_path, existing)


def _atomic_write_json(cache_path: Path, obj: dict[str, Any]) -> None:
    """Write *obj* as JSON to *cache_path* atomically (tempfile + ``os.replace``).

    A concurrent reader always sees either the old file or the complete new
    one, never a half-written mix.
    """
    with tempfile.NamedTemporaryFile(
        mode="w",
        dir=cache_path.parent,
        delete=False,
        prefix=".completion_cache_",
        suffix=".tmp",
    ) as tmp:
        tmp_name = tmp.name
        json.dump(obj, tmp)
    try:
        Path(tmp_name).replace(cache_path)
    except Exception:
        with contextlib.suppress(OSError):
            Path(tmp_name).unlink()
        raise


# ---------------------------------------------------------------------------
# Live-registry introspection (writer side)
# ---------------------------------------------------------------------------


# DEBT(no-tuple-return): two independent command lists.
# ast-grep-ignore: no-tuple-return
def collect_current_commands() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read the currently-registered instructions and suites with options.

    Must be called after :func:`otto.bootstrap.bootstrap` has finished
    populating ``otto.instructions.INSTRUCTIONS`` and
    ``otto.suite.register.SUITES``. A source that never loaded simply has an
    empty registry (no init modules → no ``@instruction()`` ran → no entries).

    Each item is ``{"name": str, "options": list[dict]}``; a command whose
    options can't be fully serialized is cached with ``options: []`` so
    the name still completes even though the per-option flags don't.
    """

    def _entries_to_dicts(entries: list[tuple[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for name, entry in entries:
            callback = None
            if entry.sub_app.registered_commands:
                callback = entry.sub_app.registered_commands[0].callback
            options = _serialize_options(callback, command_name=name) if callback else None
            out.append({"name": name, "options": options if options is not None else []})
        return out

    from ..instructions import INSTRUCTIONS
    from ..suite.register import SUITES

    instructions: list[dict[str, Any]] = _entries_to_dicts(INSTRUCTIONS.items())

    suites: list[dict[str, Any]] = _entries_to_dicts(SUITES.items())

    return instructions, suites


def collect_backend_names() -> dict[str, Any]:
    """Snapshot the registered term + transfer backend names for completion.

    Call after :func:`otto.bootstrap.bootstrap` (or ``import_init_modules``) so
    custom per-repo backends are present. Built-ins are always present
    (registered at module import). Each transfer backend carries its
    ``host_families`` so the completer can filter by family (e.g. unix-only
    for ``otto host --transfer``).
    """
    from ..host.connections import TERM_BACKENDS
    from ..host.transfer import TRANSFER_BACKENDS

    return {
        "term_backends": sorted(TERM_BACKENDS.names()),
        "transfer_backends": [
            {"name": name, "host_families": sorted(cls.host_families)}
            for name, cls in sorted(TRANSFER_BACKENDS.items())
        ],
    }


def _serialize_cli_children(app: Any) -> list[dict[str, Any]]:
    """Serialize a third-party Typer group's children for the cache.

    Children reuse the instruction/suite option schema (rebuilt by
    :func:`otto.config.completion_stubs.build_stub_command` on the fast
    path). A child whose options don't round-trip degrades to name+help —
    the name still tab-completes, only ``--<TAB>`` falls back. Nested groups
    recurse; a nested single-command app serializes as the flattened leaf it
    would natively become (see ``_typer_app_flattens``).
    """
    from typer.main import get_command_name

    from ..cli.registry import _typer_app_flattens

    children: list[dict[str, Any]] = []
    for cmd_info in app.registered_commands:
        cname = cmd_info.name or get_command_name(cmd_info.callback.__name__)
        children.append(
            {
                "name": cname,
                "help": cmd_info.help or inspect.getdoc(cmd_info.callback) or "",
                "options": _serialize_options(cmd_info.callback, command_name=cname) or [],
            }
        )
    for grp_info in app.registered_groups:
        sub = grp_info.typer_instance
        if sub is None:
            continue
        if _typer_app_flattens(sub):
            children.extend(_serialize_cli_children(sub))
            continue
        gname = next(
            (n for n in (grp_info.name, sub.info.name) if isinstance(n, str) and n),
            None,
        )
        if gname is None:
            continue
        ghelp = next((h for h in (grp_info.help, sub.info.help) if isinstance(h, str)), "")
        children.append({"name": gname, "help": ghelp, "commands": _serialize_cli_children(sub)})
    return children


def collect_cli_commands() -> list[dict[str, Any]]:
    """Snapshot third-party top-level CLI commands for the completion cache.

    Reads the live :data:`otto.cli.registry.CLI_COMMANDS` registry and
    returns one ``{"name", "help", "lab_free"}`` dict per entry whose
    ``origin`` module is *not* under ``otto.`` — built-in commands re-register
    on every real invocation (bootstrap always runs), so caching them would
    be redundant and risks masking a genuine removal. Third-party commands,
    by contrast, only exist in the registry after a plugin's init module has
    executed, which the completion fast path deliberately skips; caching
    their name/help/``lab_free`` here is what lets them still tab-complete.

    A GROUP entry additionally carries ``"commands"`` (recursive child
    metadata) and a flattening single-command app carries ``"options"`` —
    both omitted when empty. Serializing children may import a lazy
    ``"pkg.mod:attr"`` loader's module: a slow-path-only, once-per-cache-
    refresh cost, contained per command (a broken loader degrades that entry
    to name+help and real dispatch still reports the import error loudly).
    """
    import importlib

    import typer

    from ..cli.registry import CLI_COMMANDS, _typer_app_flattens

    log = logging.getLogger(__name__)
    out: list[dict[str, Any]] = []
    for name, spec in CLI_COMMANDS.items():
        if spec.origin.startswith("otto."):
            continue
        entry: dict[str, Any] = {"name": name, "help": spec.help, "lab_free": spec.lab_free}
        try:
            loader = spec.loader
            if isinstance(loader, str):
                mod_name, _, attr = loader.partition(":")
                loader = getattr(importlib.import_module(mod_name), attr)
            if isinstance(loader, typer.Typer):
                if _typer_app_flattens(loader):
                    cmd_info = loader.registered_commands[0]
                    options = _serialize_options(cmd_info.callback, command_name=spec.name)
                    if options:
                        entry["options"] = options
                else:
                    commands = _serialize_cli_children(loader)
                    if commands:
                        entry["commands"] = commands
        # Containment seam: the cache stays name-only, dispatch reports loudly.
        #
        # BaseException, not Exception: this imports a THIRD-PARTY loader module,
        # so a module-level `pytest.importorskip` there raises `Skipped` — not an
        # `Exception` — straight past this seam and out of `entry()`, which
        # reaches `collect_cli_commands()` as a call ARGUMENT, so the
        # `suppress(OSError)` around the cache write never sees it. That
        # tracebacks out of EVERY command, `otto --help` included, and into the
        # shell mid-TAB. See `otto.errors.UNCONTAINABLE`.
        except BaseException as e:
            if not is_containable(e):
                raise
            log.debug(f"completion-cache: no child metadata for {spec.name!r}: {e!r}")
        out.append(entry)
    return out


def collect_reservation_usernames(repos: list["Repo"]) -> list[str]:
    """Best-effort usernames for ``--as-user`` completion (cached).

    Builds the selected reservation backend (first repo with a
    ``[reservations]`` section) and, when it implements
    :class:`~otto.reservations.protocol.SupportsUsernameCompletion`, returns
    ``list_usernames()`` sorted. Runs on the slow path; any failure (no backend
    configured, build error, enumeration error, missing capability) yields
    ``[]`` so completion degrades gracefully and never blocks real work.
    """
    from ..reservations import build_backend
    from ..reservations.protocol import SupportsUsernameCompletion

    for repo in repos:
        settings = getattr(repo, "reservation_settings", None)
        if not settings:
            continue
        try:
            backend = build_backend(settings, repo.sut_dir)
            if isinstance(backend, SupportsUsernameCompletion):
                return sorted(backend.list_usernames())
        except Exception:  # noqa: BLE001 — completion fallback, best-effort username list; return empty on any error
            return []
        return []
    return []


#: Default seconds completion will wait for a host source before giving up.
#: A custom `[lab]` backend is allowed to be a networked CMDB, and the
#: documented reason this cache exists is to keep that off the TAB path — but
#: on a cold cache the enumeration DOES run, and an unreachable service would
#: otherwise wedge the shell with no feedback until the user interrupts it.
#: Failing is already contained (an empty list); stalling was not.
HOST_SUMMARY_DEADLINE_SECONDS = 2.0

#: Escape hatch for a backend that is SLOW rather than broken. Giving up on
#: one of those costs the user all host completion until it gets faster, and
#: a module constant leaves an affected team no recourse. Read straight from
#: the environment rather than through OttoEnvSettings, which pulls
#: pydantic_settings + dotenv (26 modules) onto the fast path.
HOST_SUMMARY_DEADLINE_ENV_VAR = "OTTO_COMPLETION_HOST_TIMEOUT"


def _host_summary_deadline() -> float:
    raw = os.environ.get(HOST_SUMMARY_DEADLINE_ENV_VAR)
    if not raw:
        return HOST_SUMMARY_DEADLINE_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return HOST_SUMMARY_DEADLINE_SECONDS
    return value if value > 0 else HOST_SUMMARY_DEADLINE_SECONDS


def _bounded(
    work: "Callable[[threading.Event], list[HostSummary]]", repo: "Repo"
) -> list["HostSummary"]:
    """Run *work*, giving up after :func:`_host_summary_deadline`.

    A daemon thread, so a backend still blocked at process exit cannot keep
    the completion process alive. Deliberately NOT ``signal.alarm``: that is
    main-thread-only and would trample whatever handler the caller installed.

    *work* is handed an ``abandoned`` event, set when the deadline passes, so
    a probe that finishes LATE can keep quiet — otherwise its own warning
    lands in the middle of whatever command the main thread has moved on to.

    Catches ``BaseException``, not ``Exception``: the containment that keeps
    completion from crashing the shell lives in the callee, and an escape
    here would reach ``threading.excepthook`` and print a full traceback to
    the user's terminal mid-TAB.
    """
    import threading

    deadline = _host_summary_deadline()
    box: list[list[HostSummary]] = []
    abandoned = threading.Event()

    def _run() -> None:
        with contextlib.suppress(BaseException):  # see the docstring
            box.append(work(abandoned))

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(deadline)
    if thread.is_alive():
        abandoned.set()
        logging.getLogger(__name__).warning(
            rf"\[completion] host source for {repo.sut_dir} did not answer within "
            f"{deadline}s — offering no hosts for it. Raise "
            f"{HOST_SUMMARY_DEADLINE_ENV_VAR} if it is merely slow."
        )
        return []
    # `or []` rather than `box[0]`: a work() returning None would otherwise
    # hand every caller a None to iterate.
    return (box[0] if box else None) or []


def repo_host_summaries(repo: "Repo") -> list["HostSummary"]:
    """Every host *repo*'s configured host source knows — best-effort.

    Goes through the repo's own ``[lab]`` backend rather than reading its
    ``lab.json`` files directly, so a project with a custom host source gets
    completion from it (previously a custom backend contributed nothing here
    — completion only ever saw ``lab.json``).

    Scoped PER REPO, which preserves what completion did before: each repo
    contributed the hosts under its own ``labs`` paths, and the container ids
    synthesized below pair a repo's ``[docker]`` composes with its own
    docker-capable parents. Note this is NOT how dispatch selects a backend —
    ``cli/invoke`` builds ONE backend from the FIRST repo declaring ``[lab]``
    — so with two repos both declaring one, completion enumerates both while
    dispatch honours only the first. Reconciling the two selection rules is
    its own change.

    Never raises, and never hangs: an unregistered backend, malformed
    settings, or a backend that explodes yields an empty list, because every
    caller is a completion path that must not crash the shell — and one that
    STALLS is bounded by :data:`HOST_SUMMARY_DEADLINE_SECONDS` rather than
    left to wedge the user's TAB. Logged at WARNING, not DEBUG: a
    transiently-unreachable custom backend would otherwise write an EMPTY host
    list into a cache that is then served for the next 24 hours, silently.
    """
    key = str(getattr(repo, "sut_dir", repo))
    cached = _SUMMARY_MEMO.get(key)
    if cached is None:
        cached = _bounded(lambda abandoned: _enumerate_host_summaries(repo, abandoned), repo)
        _SUMMARY_MEMO[key] = cached
    return cached


#: Per-process memo, keyed by SUT dir. Three collectors enumerate the same
#: repo on one cache-write pass; without this a stalled backend cost three
#: deadlines and — worse — could time out for one collector and not another,
#: writing a cache where `otto host <TAB>` is full and
#: `otto docker --on <TAB>` is empty. Process-lifetime only, like the cache
#: itself; nothing invalidates it because nothing lives long enough to need to.
_SUMMARY_MEMO: dict[str, list["HostSummary"]] = {}


def _enumerate_host_summaries(
    repo: "Repo", abandoned: "threading.Event | None" = None
) -> list["HostSummary"]:
    from ..labs import build_lab_repository, host_summaries

    try:
        repository = build_lab_repository(
            repo.lab_settings, repo.sut_dir, search_paths=list(repo.labs)
        )
        return host_summaries(repository)
    except Exception as e:  # noqa: BLE001 — completion never crashes the shell
        if abandoned is None or not abandoned.is_set():
            logging.getLogger(__name__).warning(
                rf"\[completion] could not enumerate hosts for {repo.sut_dir}: {e}"
            )
        return []


def collect_docker_capable_host_ids(repos: list["Repo"]) -> list[str]:
    """Enumerate host IDs that can host containers (``docker_capable``).

    Used as the completion source for ``otto docker --on <TAB>`` and any
    other surface that should be limited to docker-capable parents.
    Mirrors :func:`collect_host_ids` (no :func:`otto.bootstrap.bootstrap` call
    needed; safe in the completion fast path).

    The flag is read from the resolved host identity, so a host whose
    ``os_profile`` defaults ``docker_capable`` counts here — it always did in
    :func:`collect_host_ids`, which read the constructed host, and the two
    now agree.
    """
    ids: set[str] = set()
    for repo in repos:
        for summary in repo_host_summaries(repo):
            if summary.docker_capable:
                ids.add(summary.id)
    return sorted(ids)


def collect_host_ids(repos: list["Repo"], lab_names: list[str] | None = None) -> list[str]:
    """Enumerate every host ID reachable via the configured lab search paths.

    Enumerates each repo's configured host source (see
    :func:`repo_host_summaries`), whose ids are resolved through the same
    validation the host factory applies, so the resulting IDs match what
    ``get_host`` will look up at runtime. Also
    synthesizes container host IDs of the form ``<parent>.<project>.<service>``
    from each repo's ``[docker]`` settings so declared container hosts
    are tab-completable before they're actually brought up.

    When *lab_names* is given, only hosts whose ``labs`` array names one of
    those labs are enumerated — the completion source for ``otto host <TAB>``
    once a lab is selected via ``-l``/``--lab``/``OTTO_LAB``. Container IDs are
    scoped the same way (only docker-capable parents in the selected lab).
    The built-in hosts are always seeded regardless of the filter, mirroring
    ``load_lab`` injecting ``local`` into every lab.

    Also emits positional logical handles (``<element-slug><N>``, e.g.
    ``server1``) for every host in a repeated-element group, computed via
    :func:`otto.config.lab.logical_indices` — the same single source
    ``Lab._assign_logical_indices`` stamps from — so a completed handle always
    matches what ``Lab.resolve_handle`` resolves at runtime. Added alongside
    canonical ids, never in place of them.

    Runs without :func:`otto.bootstrap.bootstrap` having been called, so it's
    safe to call from the completion fast path as well as the cache writer
    on the slow path.

    Returns a sorted, de-duplicated list. Malformed files / entries are
    silently skipped — completion must never crash on bad user data.
    """
    from ..host.builtin_hosts import builtin_host_ids
    from ..host.remote_host import slug
    from .lab import logical_indices

    wanted = set(lab_names) if lab_names is not None else None

    # Seed with the built-in hosts otto injects into every lab (e.g. `local`) so
    # they are tab-completable in every repo, mirroring load_lab's injection.
    ids: set[str] = set(builtin_host_ids())
    # Every summarized host across all repos, keyed by id (dedup). Logical
    # positions are derived from this combined set (once, below) so a group
    # split across repos' host sources is still numbered as one group —
    # matching how a real Lab merges hosts from multiple sources before
    # stamping.
    summarized: dict[str, "HostSummary"] = {}
    for repo in repos:
        # Docker-capable ids scoped to THIS repo, so the container ids
        # synthesized below pair each repo's composes with its own parents.
        docker_capable_ids: list[str] = []
        for summary in repo_host_summaries(repo):
            # Lab filter: keep only hosts tagged with a requested lab.
            if wanted is not None and wanted.isdisjoint(summary.labs):
                continue
            ids.add(summary.id)
            summarized[summary.id] = summary
            if summary.docker_capable:
                docker_capable_ids.append(summary.id)

        docker = getattr(repo, "docker_settings", None)
        if docker is None or not docker.composes:
            continue
        for compose in docker.composes:
            # Pick parents to enumerate against. Prefer an explicit
            # default_host; otherwise enumerate every docker-capable host
            # in this repo's labs (pessimistic but stable; the actual
            # bring-up picks one). Under a lab filter, an explicit
            # default_host only counts if it survived the filter (i.e. it is a
            # docker-capable host in the selected lab).
            if compose.default_host:
                parents = (
                    [compose.default_host]
                    if wanted is None or compose.default_host in docker_capable_ids
                    else []
                )
            else:
                parents = list(docker_capable_ids)
            for parent in parents:
                for service in compose.services:
                    ids.add(f"{parent}.{repo.name}.{service}".lower())

    # Logical handles (<slug(element)><position>) alongside canonical ids, so
    # `otto host <TAB>` offers exactly what Lab.resolve_handle would resolve at
    # runtime — logical_indices is the single shared source (see lab.py).
    positions = logical_indices(summarized.values())
    for summary in summarized.values():
        pos = positions.get(summary.id)
        if pos is not None:
            ids.add(f"{slug(summary.element)}{pos}")

    return sorted(ids)


def _read_lab_links(lab_file: Path) -> list[dict[str, Any]]:
    """Best-effort read of a lab.json's ``links`` array ([] on any problem).

    Completion must never crash on bad user data, so malformed shapes are
    silently empty here. Links have no repository seam yet — hosts moved to
    ``LabRepository`` enumeration, this stayed a direct read.
    """
    try:
        data = json.loads(lab_file.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    links = data.get("links", [])
    return links if isinstance(links, list) else []


def collect_link_ids(
    repos: list["Repo"], *, loaded_ids: "Collection[str] | None" = None
) -> list[str]:
    """Enumerate DECLARED static link ids/names for ``otto link`` completion.

    Each id is the declared ``name`` if set, else the ``lo--hi`` static id,
    built by :func:`~otto.link.model.make_static_link_id` rather than by
    re-spelling its format here. The two agree today — plain string sort
    matches sorting by ``(host, interface)`` whenever the hosts differ — so
    this is drift insurance, bought at the price of one import, in a module
    whose hand-derived host ids already drifted once (6576a6b4).

    *loaded_ids*, when given, is the selected lab's host-id set, and an entry
    is offered only if an endpoint is in it — the SAME rule, through the same
    :func:`~otto.link.derive.raw_endpoint_host_ids` helper,
    :func:`~otto.link.derive.resolve_declared_links` applies when deciding
    which links a lab loads. Without it a repo's every lab file contributes,
    and under ``-l <lab>`` completion offers links ``find_link`` will refuse.

    IMPLICIT links are deliberately NOT offered, though ``find_link`` accepts
    them: they are unimpairable by construction, so offering them would be
    offering guaranteed errors. ``implicit_links`` builds endpoints with no
    named interface, which ``endpoint_placements`` refuses ("not impairable,
    spec §4"), and a hop-less host's edge is to ``local``, which
    ``ensure_not_local_link`` refuses outright as otto's own path to the bed.
    ``repair_all`` skips them for the same reason, and ``otto link list``
    reports them ``impairable=False``.

    Lab-data only — sync, no live scan, no host construction. Declared links
    are read raw because links have no repository seam yet (hosts moved to
    ``LabRepository`` enumeration; this stayed a direct read), so malformed
    entries are silently skipped: completion must never crash on bad data.
    """
    from ..link.derive import raw_endpoint_host_ids
    from ..link.model import LinkEndpoint, make_static_link_id

    ids: set[str] = set()
    for repo in repos:
        for lab_path in repo.labs:
            for entry in _read_lab_links(lab_path / LAB_FILENAME):
                if not isinstance(entry, dict):
                    continue
                if loaded_ids is not None and not any(
                    host_id in loaded_ids for host_id in raw_endpoint_host_ids(entry)
                ):
                    continue
                name = entry.get("name")
                if isinstance(name, str) and name:
                    ids.add(name)
                    continue
                hosts = raw_endpoint_host_ids(entry)
                if len(hosts) != 2 or not all(h for h in hosts):  # noqa: PLR2004
                    continue
                a, b = (LinkEndpoint(host=h) for h in hosts)
                ids.add(make_static_link_id(a, b, None))
    return sorted(ids)


def collect_lab_names(repos: list["Repo"]) -> list[str]:
    """Enumerate every lab name each repo's host source can provide.

    A lab is a *tag* on hosts (each host's ``labs`` array), not a directory,
    so the names come from every repo's configured backend via the required
    :meth:`~otto.labs.protocol.LabRepository.list_labs` — not from reading
    ``lab.json``, which would leave a custom host source with no ``--lab``
    completion and, worse, empty ``hosts_by_lab`` buckets on the warm path
    while the cold path offered its hosts.

    Data-only (no host construction, no user code), so it is safe in the
    completion fast path as well as the cache writer. A backend that fails is
    skipped; any unexpected error yields ``[]`` so completion never crashes.
    """
    from ..labs import build_lab_repository

    names: set[str] = set()
    for repo in repos:
        try:
            repository = build_lab_repository(
                repo.lab_settings, repo.sut_dir, search_paths=list(repo.labs)
            )
            names.update(repository.list_labs())
        except Exception as e:  # noqa: BLE001, PERF203 — per-repo resilience: one bad backend must not deny the rest
            logging.getLogger(__name__).warning(
                rf"\[completion] could not list labs for {repo.sut_dir}: {e}"
            )
    return sorted(names)


def collect_host_ids_by_lab(repos: list["Repo"]) -> dict[str, list[str]]:
    """Map each lab name to the host IDs that belong to it (pure membership).

    Powers lab-scoped ``otto host <TAB>`` completion from the fast cache path:
    the completer unions the buckets for the selected lab(s) and adds the
    always-present built-in hosts. The buckets therefore deliberately EXCLUDE
    built-ins — the "``local`` is in every lab" policy lives in the completer,
    in one place, shared with the live fallback (:func:`collect_host_ids` with
    ``lab_names``). Keeping buckets to true membership also means a bogus lab
    name resolves to exactly the built-ins on both the warm and cold paths.

    Written by the slow-path cache writer only. Even so it enumerates ONCE and
    groups by membership rather than calling :func:`collect_host_ids` per lab:
    that shape was O(labs²) backend queries for a non-file host source, which
    the short TTL for those sources (see :func:`_cache_ttl_seconds`) would
    have made a recurring cost rather than a once-a-day one.
    """
    from ..host.builtin_hosts import builtin_host_ids
    from ..host.remote_host import slug
    from .lab import logical_indices

    builtins = set(builtin_host_ids())
    # Seed every known lab so one whose hosts all fail to enumerate still gets
    # an (empty) bucket, keeping this shape identical to the per-lab form.
    by_lab: dict[str, dict[str, "HostSummary"]] = {lab: {} for lab in collect_lab_names(repos)}
    for repo in repos:
        for summary in repo_host_summaries(repo):
            if summary.id in builtins:
                continue
            for lab in summary.labs:
                by_lab.setdefault(lab, {})[summary.id] = summary

    buckets: dict[str, list[str]] = {}
    for lab, summaries in by_lab.items():
        ids = set(summaries)
        # Logical handles are scoped to the lab, exactly as the per-lab
        # collect_host_ids(lab_names=[lab]) call computed them: a group is
        # "repeated" relative to the hosts in THIS lab, not the whole fleet.
        positions = logical_indices(summaries.values())
        for summary in summaries.values():
            pos = positions.get(summary.id)
            if pos is not None:
                ids.add(f"{slug(summary.element)}{pos}")
        buckets[lab] = sorted(ids)
    return buckets


def collect_test_names(repos: list["Repo"]) -> list[str]:
    """Statically discover test names for ``otto test --tests`` completion.

    Parses every file matching the repo's pytest ``python_files`` (its own, if
    it configures one — see :func:`~otto.config.repo.configured_python_files`)
    under each repo's test dirs with
    :mod:`ast` — no import, no collection, no user code — and returns the base
    names of top-level ``def test_*`` / ``async def test_*`` functions plus, for
    each ``Test*`` class, its ``test_*`` methods (emitted both bare and as
    ``ClassName::method`` to match ``--tests``'s disambiguation form).

    This is deliberately static: real pytest collection (which ``--tests``
    resolves against, and which ``otto test --list-tests`` runs) expands
    parametrization and honors ``conftest`` / ``pytest_generate_tests``, none
    of which are visible to a source scan. So a *parametrized-only* id or a
    dynamically generated test will not appear here — those still need
    ``--list-tests`` — but every statically-defined test name does, without
    ever executing test code at tab time. ``python_files`` is read from the
    repo's pytest config; ``python_classes`` / ``python_functions`` are still
    assumed to be pytest's defaults.
    """
    import ast

    names: set[str] = set()
    for repo in repos:
        patterns = configured_python_files(repo.sut_dir)
        for test_dir in repo.tests:
            if not test_dir.exists():
                continue
            for path in sorted(_match_py_files(test_dir, patterns)):
                try:
                    tree = ast.parse(path.read_text(), filename=str(path))
                except (OSError, SyntaxError):
                    continue  # unreadable / unparseable file: skip, never crash
                for node in tree.body:
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if node.name.startswith("test"):
                            names.add(node.name)
                    elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
                        for method in node.body:
                            if isinstance(
                                method, (ast.FunctionDef, ast.AsyncFunctionDef)
                            ) and method.name.startswith("test"):
                                names.add(method.name)
                                names.add(f"{node.name}::{method.name}")
    return sorted(names)


# ---------------------------------------------------------------------------
# Collected (pytest-accurate) test names — real collection, cached separately
# ---------------------------------------------------------------------------


def _test_names_from_items(items: list[Any]) -> list[str]:
    """Completion candidates from collected pytest items: base + ``Class::base``.

    Collapses parametrizations to the base name (``test_x[a]`` → ``test_x``):
    ``otto test --tests`` selects by base name (a bare name runs every
    parametrization and per-parametrization ids are rejected), so this mirrors
    :func:`collect_test_names`'s shape — only the *source* differs (real
    collection vs. an ``ast`` scan). Duck-typed on ``.name`` / ``.cls_name`` so
    it needn't import :class:`~otto.config.repo.CollectedTest`.
    """
    names: set[str] = set()
    for item in items:
        base = str(item.name).partition("[")[0]
        names.add(base)
        cls_name = getattr(item, "cls_name", None)
        if cls_name:
            names.add(f"{cls_name}::{base}")
    return sorted(names)


def dump_collected_test_names(repos: list["Repo"]) -> None:
    """Collect every repo's tests and print a framed name list to stdout.

    The child side of the tab-time warm: run by :func:`otto.cli.main.entry`
    when :data:`DUMP_TESTS_ENV_VAR` is set. Collection runs here — in a
    disposable, timeout-bounded subprocess — never inside the completer itself
    (whose stdout is the shell's completion channel). ``Repo.collect_tests``
    already redirects the inner pytest run's stdout/stderr, so only the framed
    payload below reaches the parent.
    """
    import sys

    items: list[Any] = []
    for repo in repos:
        items.extend(repo.collect_tests())
    names = _test_names_from_items(items)
    sys.stdout.write("\n".join([_DUMP_BEGIN, *names, _DUMP_END]) + "\n")
    sys.stdout.flush()


def _parse_dumped_names(stdout: str) -> list[str] | None:
    """Recover the framed name list from the dump subprocess's stdout."""
    lines = stdout.splitlines()
    try:
        start = lines.index(_DUMP_BEGIN)
        end = lines.index(_DUMP_END)
    except ValueError:
        return None
    if end < start:
        return None
    return [ln for ln in lines[start + 1 : end] if ln.strip()]


def _collected_cache_entry(repos: list["Repo"]) -> dict[str, Any] | None:
    """Raw collected-cache entry (names + timestamp) for the current fingerprint."""
    if not repos:
        return None
    cache_path = _cache_path()
    if cache_path is None or not cache_path.is_file():
        return None
    try:
        data = json.loads(cache_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    namespace = data.get(COLLECTED_TESTS_KEY)
    if not isinstance(namespace, dict):
        return None
    entry = namespace.get(compute_fingerprint(repos))
    return entry if isinstance(entry, dict) else None


def read_collected_tests(repos: list["Repo"]) -> list[str] | None:
    """Return the fresh pytest-collected test names for ``--tests``, or ``None``.

    ``None`` means the collected set is cold for the completer: caching
    disabled, no entry for this fingerprint, wrong schema, TTL-expired, a
    recorded *failed* attempt (``names`` is ``null``), or malformed data. The
    completer then falls back to the static floor and may warm the cache via
    :func:`maybe_warm_collected_tests`. Fingerprint keying means any test-file
    edit invalidates this automatically, exactly like the main cache.
    """
    entry = _collected_cache_entry(repos)
    if entry is None:
        return None
    if entry.get("schema_version") != COLLECTED_SCHEMA_VERSION:
        return None
    generated_at = entry.get("generated_at")
    if not isinstance(generated_at, (int, float)):
        return None
    if time.time() - generated_at > CACHE_TTL_SECONDS:
        return None
    names = entry.get("names")
    if not isinstance(names, list):
        return None
    return names


def _record_collected_tests(repos: list["Repo"], names: list[str] | None) -> None:
    """Merge a collected-cache result for the current fingerprint.

    ``names=None`` records a *failed* attempt; its timestamp drives the
    tab-time retry cooldown. Only the reserved :data:`COLLECTED_TESTS_KEY`
    namespace is touched — every main fingerprint entry is preserved — so this
    warmer and the slow-path writer never clobber each other.
    """
    if not repos:
        return
    cache_path = _cache_path()
    if cache_path is None:
        return
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    existing: dict[str, Any] = {}
    if cache_path.is_file():
        try:
            loaded = json.loads(cache_path.read_text())
            if isinstance(loaded, dict):
                existing = loaded
        except (OSError, json.JSONDecodeError):
            pass

    namespace = existing.get(COLLECTED_TESTS_KEY)
    if not isinstance(namespace, dict):
        namespace = {}
    namespace[compute_fingerprint(repos)] = {
        "schema_version": COLLECTED_SCHEMA_VERSION,
        "generated_at": int(time.time()),
        "names": names,  # None => a failed attempt (cooldown marker only)
    }
    existing[COLLECTED_TESTS_KEY] = namespace
    _atomic_write_json(cache_path, existing)


def record_collected_tests_from_items(repos: list["Repo"], items: list[Any]) -> None:
    """Warm the collected cache from an already-run *unfiltered* collection.

    The free "Option B" path: when a real ``otto test --list-tests`` runs with
    no marker/suite narrowing, it has already collected the full test set, so
    cache it here rather than paying a separate collection later. Callers must
    only pass an *unfiltered* item list — a marker/suite-narrowed collection
    would cache an incomplete set.
    """
    _record_collected_tests(repos, _test_names_from_items(items))


def _acquire_collect_lock(lock: Path) -> bool:
    """Try to take the tab-time collection lock (atomic ``O_EXCL`` create).

    Returns ``False`` when another process holds a fresh lock; steals and takes
    a lock older than :data:`COLLECT_LOCK_STALE_SECONDS` (its holder died).
    """
    now = time.time()
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        try:
            age = now - lock.stat().st_mtime
        except OSError:
            return False
        if age <= COLLECT_LOCK_STALE_SECONDS:
            return False
        with contextlib.suppress(OSError):
            lock.unlink()
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except OSError:
            return False
    except OSError:
        return False
    with contextlib.suppress(OSError):
        os.write(fd, str(now).encode())
    os.close(fd)
    return True


def _run_collect_subprocess() -> list[str] | None:
    """Spawn the bounded ``DUMP_TESTS_ENV_VAR`` subprocess and parse its names.

    Returns the collected names, or ``None`` on timeout / non-zero exit / spawn
    failure. Runs the *venv* ``otto`` binary (so ``entry`` runs, unlike ``python
    -m otto``) with the completion env vars stripped, so the child dumps names
    instead of recursing into another completion.
    """
    import subprocess
    import sys

    otto_bin = Path(sys.executable).parent / "otto"
    if not otto_bin.exists():
        return None
    env = dict(os.environ)
    for var in (COMPLETION_ENV_VAR, "COMP_WORDS", "COMP_CWORD"):
        env.pop(var, None)
    env[DUMP_TESTS_ENV_VAR] = "1"
    try:
        proc = subprocess.run(  # noqa: S603 — venv otto binary, fixed argv, no shell
            [str(otto_bin)],
            env=env,
            capture_output=True,
            text=True,
            timeout=COLLECT_TIMEOUT_SECONDS,
            check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if proc.returncode != 0:
        return None
    return _parse_dumped_names(proc.stdout)


def maybe_warm_collected_tests(repos: list["Repo"]) -> list[str] | None:
    """Best-effort: run one bounded collection to warm the collected cache.

    Returns the collected names on success (so the triggering completion is
    already enriched), else ``None`` — when warming is skipped (caching
    disabled, cooldown active after a recent failure, another process already
    collecting) or the collection times out / fails. Never raises: completion
    must degrade to the static floor, never traceback into the shell.
    """
    if not repos:
        return None
    cache_path = _cache_path()
    if cache_path is None:
        return None
    try:
        return _warm_collected_tests(repos, cache_path)
    except Exception:  # noqa: BLE001 — completion must never raise into the shell
        return None


def _warm_collected_tests(repos: list["Repo"], cache_path: Path) -> list[str] | None:
    """Cooldown-gated, lock-guarded body of :func:`maybe_warm_collected_tests`."""
    entry = _collected_cache_entry(repos)
    if entry is not None:
        at = entry.get("generated_at")
        # read_collected_tests already returned a *fresh success* upstream, so a
        # recent timestamp here means a recent failure → cooldown, skip.
        if isinstance(at, (int, float)) and time.time() - at <= COLLECT_COOLDOWN_SECONDS:
            return None
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    lock = cache_path.parent / COLLECT_LOCK_FILENAME
    if not _acquire_collect_lock(lock):
        return None
    try:
        names = _run_collect_subprocess()
    finally:
        with contextlib.suppress(OSError):
            lock.unlink()
    with contextlib.suppress(OSError):
        _record_collected_tests(repos, names)  # names=None stamps the cooldown
    return names


# ---------------------------------------------------------------------------
# Dynamic tunnel-id namespace, for `otto tunnel remove <id>` completion
# ---------------------------------------------------------------------------
#
# Like COLLECTED_TESTS_KEY above, this lives under its own reserved top-level
# key rather than inside a fingerprint entry: live tunnel state is discovered
# by process/argv inspection, not by anything the fingerprint's file-mtime
# hashing tracks, and it must never clobber (or be clobbered by) the main
# fingerprint entries. The TTL is intentionally short — tunnels come and go
# independently of otto invocations, so a stale id list is wrong far sooner
# than the main cache's config-derived data would be.
DYNAMIC_TUNNELS_KEY = "__dynamic_tunnels__"
DYNAMIC_TUNNELS_SCHEMA_VERSION = 1
DYNAMIC_TUNNELS_TTL_SECONDS = 120  # tunnel state is volatile; short TTL (spec §11.2)


def record_tunnel_ids(repos: list["Repo"], ids: list[str]) -> None:
    """Cache the freshly-discovered tunnel ids for ``remove <id>`` completion."""
    if not repos:
        return
    cache_path = _cache_path()
    if cache_path is None:
        return
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, Any] = {}
    if cache_path.is_file():
        try:
            loaded = json.loads(cache_path.read_text())
            if isinstance(loaded, dict):
                existing = loaded
        except (OSError, json.JSONDecodeError):
            pass
    namespace = existing.get(DYNAMIC_TUNNELS_KEY)
    if not isinstance(namespace, dict):
        namespace = {}
    namespace[compute_fingerprint(repos)] = {
        "schema_version": DYNAMIC_TUNNELS_SCHEMA_VERSION,
        "generated_at": int(time.time()),
        "ids": list(ids),
    }
    existing[DYNAMIC_TUNNELS_KEY] = namespace
    _atomic_write_json(cache_path, existing)


def read_tunnel_ids(repos: list["Repo"]) -> list[str] | None:
    """Fresh cached tunnel ids, or ``None`` (cold / expired / malformed)."""
    if not repos:
        return None
    cache_path = _cache_path()
    if cache_path is None or not cache_path.is_file():
        return None
    try:
        data = json.loads(cache_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    namespace = data.get(DYNAMIC_TUNNELS_KEY) if isinstance(data, dict) else None
    entry = namespace.get(compute_fingerprint(repos)) if isinstance(namespace, dict) else None
    if not isinstance(entry, dict) or entry.get("schema_version") != DYNAMIC_TUNNELS_SCHEMA_VERSION:
        return None
    generated_at = entry.get("generated_at")
    if not isinstance(generated_at, (int, float)):
        return None
    if time.time() - generated_at > DYNAMIC_TUNNELS_TTL_SECONDS:
        return None
    ids = entry.get("ids")
    return ids if isinstance(ids, list) else None
