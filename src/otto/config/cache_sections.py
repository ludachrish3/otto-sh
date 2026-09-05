"""Section registry for the shell-completion cache (spec 2026-09-01, Fix C).

A *section* is a registration — ``(name, key_paths, collect)`` — over one
shared digest (:func:`section_digest`), one generic reader
(:func:`read_section`) and one generic writer (:func:`write_section`). The
cache file stores each section under its own name with its own digest, so a
reader interested only in the cheap ``names`` key set never has to walk the
full test corpus to locate or validate its entry::

    {"schema": N, "sections": {"<name>": {"fingerprint", "generated_at",
                                          "tainted", "payload"}, ...}}

Still ONE file and one open per read — the network-filesystem optimum,
preserved deliberately. Reserved ``__*__`` namespaces (the collected-test
names, the dynamic tunnel ids) live beside ``"sections"`` at the top level,
unchanged.

Three sections:

- ``names`` — everything whose registration source is bounded: instruction
  and suite names, hosts, backends, third-party CLI commands. Keys on the
  init trees, ``.otto/settings.toml``, the pytest config files, the
  TOP-LEVEL test files (sound because :meth:`Repo.iter_test_files
  <otto.config.repo.Repo.iter_test_files>` is non-recursive — only those can
  register), and the lab files (the hosts payload is served from here, so a
  ``lab.json`` edit must move this digest exactly as it moved the old
  monolithic fingerprint).
- ``tests`` — the static ``--tests`` name floor. Keys on the full corpus
  walk: every file whose edit can change a statically-scanned test name.
- ``shim`` — the self-describing entry the console-script shim answers a
  bash TAB from; keys on both siblings' key sets (``names`` U ``tests``), so
  it is rewritten whenever either is.

Adding a further cached item normally needs just ONE ``Section(...)`` entry
in :data:`SECTIONS` — no new digest function, no reader branch, no schema
bump, and no writer keyword: a plain new section is read and written through
:func:`read_section` / :func:`write_section`, never through
:func:`completion_cache.write_cache <otto.config.completion_cache.write_cache>`.
``shim`` is the one exception so far: ``entry()`` needs it written in the SAME
atomic update as its merged-view siblings, which is why ``write_cache`` grew a
``shim=`` keyword solely for that ordering guarantee — a section that does not
need atomicity with the merged view should not assume it needs one too.

Digest contributions that are not stat-able files — the literal
``unresolved:<name>`` token for an init module that resolves under no
``libs`` entry, and the process inventory's freshness text — are mixed into
EVERY section's digest (the shared tail), exactly as the monolithic
fingerprint mixed them into its single digest: they describe process-wide
state no path list can carry, and a new section inherits them automatically.

Taint is per-section: a section written while bootstrap reported errors is
stored with ``"tainted": true`` and never served — otherwise help would go
silently and permanently partial, because the broken file's stats are stable
until edited and the digest would never move.
"""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import completion_cache as _cc
from .repo import Repo, configured_python_files, pytest_config_paths

SHIM_SECTION = "shim"
"""Storage name of the `shim` section. Defined here, not in :mod:`.completion_tree` (which
re-exports it), because this module sits on the WARM completion-read path (a plain
`read_cache` call lazily imports it) while `completion_tree` is cold-path-only (it resolves the
whole CLI tree); a module-scope import the other way round would drag `completion_tree` — and
everything IT imports — onto every TAB, not just a cache write."""


def _names_key_paths(repos: list[Repo]) -> list[Path]:
    """Every path whose edit can change a REGISTERED name.

    Registration executes init modules and top-level test files only —
    ``Repo.iter_test_files`` is called unbound so the repo stand-ins tests
    use share the exact production glob — steered by ``settings.toml`` and
    the pytest config files; the hosts/labs payloads additionally read the
    lab files. Nested test files deliberately do NOT key this section: they
    cannot register, and skipping them is the point — this key set stays
    O(top-level) while the ``tests`` section pays the corpus walk.

    Every directory the enumeration entered is a key path too — its mtime
    moves when a file appears, is removed or is renamed there, which is how a
    NEW lab file or top-level test file is seen by a reader that never
    re-runs the glob (the completion shim). Those directories are appended,
    sorted, after the files.
    """
    paths: list[Path] = []
    visited: set[Path] = set()
    for repo in repos:
        paths.append(repo.sut_dir / ".otto" / "settings.toml")
        paths.extend(_cc.resolved_init_paths(repo))
        paths.extend(pytest_config_paths(repo.sut_dir))
        paths.extend(Repo.iter_test_files(repo, visited=visited))
        # Direct attribute access throughout, as in compute_fingerprint: this
        # is the digest path, and a malformed repo double missing a pinned
        # attribute must fail by name, not hash "nothing declared".
        for src in repo.lab_sources:
            paths.extend(src.lab_files(visited=visited))
    return [*paths, *sorted(visited)]


def _tests_key_paths(repos: list[Repo]) -> list[Path]:
    """Every path whose edit can change a static ``--tests`` name -- or a ``-m`` one.

    The full corpus walk: the repo's own ``python_files`` patterns (plus
    ``conftest.py``) under every tests dir, the conftests on the path from a
    tests dir up to the SUT root, and the files that decide which patterns
    and directories apply at all (the pytest configs and ``settings.toml``).

    The same set guards the section's OTHER payload, the ``-m`` marker floor:
    :func:`completion_cache.collect_marker_names` reads the markers the same
    scan saw and the ``markers =`` declarations in ``pyproject.toml`` -- both
    already here, the second as one of the pytest configs. Its third source,
    ``otto.suite.markers.OTTO_MARKERS``, is otto's own code and moves with the
    otto version, not with a workspace path.

    Every directory the walk entered is a key path too — see
    :func:`_names_key_paths` for why — appended, sorted, after the files.
    """
    paths: list[Path] = []
    visited: set[Path] = set()
    for repo in repos:
        paths.append(repo.sut_dir / ".otto" / "settings.toml")
        paths.extend(pytest_config_paths(repo.sut_dir))
        patterns = configured_python_files(repo.sut_dir)
        for test_dir in repo.tests:
            if test_dir.is_dir():
                paths.extend(
                    _cc.iter_test_sources(test_dir, repo.sut_dir, patterns, visited=visited)
                )
    return [*paths, *sorted(visited)]


def _collect_names(repos: list[Repo]) -> dict[str, Any]:
    """Assemble the ``names`` payload from the live registries and *repos*.

    Key-for-key the merged view :func:`completion_cache.read_cache` serves,
    minus the ``tests`` floor — the writer's payload split must match this
    key set (pinned by ``tests/unit/config/test_cache_sections.py``).
    """
    instructions, suites = _cc.collect_current_commands()
    backends = _cc.collect_backend_names()
    return {
        "instructions": instructions,
        "suites": suites,
        "hosts": _cc.collect_host_ids(repos),
        "hosts_by_lab": _cc.collect_host_ids_by_lab(repos),
        "host_drops": _cc.collect_host_drops(repos),
        "docker_hosts": _cc.collect_docker_capable_host_ids(repos),
        "docker_use_cases": _cc.collect_docker_use_case_names(repos),
        "term_backends": backends["term_backends"],
        "transfer_backends": backends["transfer_backends"],
        "usernames": _cc.collect_reservation_usernames(repos),
        "commands": _cc.collect_cli_commands(),
        "labs": _cc.collect_lab_names(repos),
        "host_classes_by_id": _cc.collect_host_classes_by_id(repos),
        "projects": _cc.collect_project_names(),
        "links": _cc.collect_links(repos),
    }


def _collect_tests(repos: list[Repo]) -> dict[str, Any]:
    """Assemble the ``tests`` payload: the ast-scanned name and marker floors, one pass."""
    scan = _cc.scan_test_corpus(repos)
    return {"tests": scan.names, "markers": _cc.collect_marker_names(repos, scan=scan)}


def _shim_key_paths(repos: list[Repo]) -> list[Path]:
    """Names U tests: the shim entry must be rewritten whenever EITHER sibling is (spec §3.1)."""
    return [*_names_key_paths(repos), *_tests_key_paths(repos)]


def _collect_shim(repos: list[Repo]) -> dict[str, Any]:
    from .completion_tree import build_shim_payload  # lazy: imports the CLI

    return build_shim_payload(repos)


@dataclass(frozen=True)
class Section:
    """One cached item: a name, its invalidation key set, and its collector."""

    name: str
    """Storage key under ``"sections"`` in the cache file."""

    key_paths: Callable[[list[Repo]], list[Path]]
    """Every path whose edit must move this section's digest. Order and
    duplicates are irrelevant — :func:`section_digest` sorts and dedups."""

    collect: Callable[[list[Repo]], dict[str, Any]]
    """Build this section's payload from live state (slow path only)."""


SECTIONS: list[Section] = [
    Section(name="names", key_paths=_names_key_paths, collect=_collect_names),
    Section(name="tests", key_paths=_tests_key_paths, collect=_collect_tests),
    Section(name=SHIM_SECTION, key_paths=_shim_key_paths, collect=_collect_shim),
]

MERGED_VIEW_SECTIONS: list[str] = ["names", "tests"]
"""Membership of the LEGACY merged view — the sections
:func:`completion_cache.read_cache <otto.config.completion_cache.read_cache>`
merges into its returned payload and :func:`completion_cache.write_cache
<otto.config.completion_cache.write_cache>` writes from its keyword
arguments, kept in lockstep so a freshly written cache always reads back as
a hit. A NEW section does NOT join this list: it is read and written
through :func:`read_section` / :func:`write_section` (or, like ``shim``,
folded into a ``write_cache`` call as an EXTRA keyword that is written but
never merged — see :func:`completion_cache.read_cache`'s *require*), and
widening the completion fast path's MERGED payload is a deliberate change
here, never a side effect of registering a Section."""


def section_by_name(name: str) -> Section:
    """Return the registered section called *name* (``KeyError`` for an unknown one)."""
    for section in SECTIONS:
        if section.name == name:
            return section
    raise KeyError(name)


def _shared_tail(repos: list[Repo]) -> list[str]:
    """Digest lines every section mixes in after its key paths.

    The process-wide contributions no path list can carry: the literal
    ``unresolved:<name>`` token per init module that resolves under no
    ``libs`` entry (constant until the module appears, at which point the
    resolved files join the key set anyway — the short TTL is the staleness
    bound, exactly as for the monolithic fingerprint), and the inventory's
    freshness text (an inventory that cannot report freshness clock-stamps
    this line, so no section of an ephemeral write could ever be served).
    """
    lines: list[str] = []
    for repo in sorted(repos, key=lambda r: str(r.sut_dir)):
        lines.extend(f"unresolved:{name}" for name in _cc.unresolved_init_modules(repo))
    lines.append(f"inventory:{_cc.inventory_digest_text(repos)}")
    return lines


def section_digest(section: Section, repos: list[Repo]) -> str:
    """Stat-based sha256 over *section*'s key paths (plus the shared tail)."""
    return _digest(section, repos, _shared_tail(repos))


def _digest(section: Section, repos: list[Repo], tail: list[str]) -> str:
    """:func:`section_digest` with the shared tail precomputed by the caller."""
    h = hashlib.sha256()
    # Via the module attribute, not a from-import: tests count digest work by
    # monkeypatching ``completion_cache.hash_file``, and a bound name here
    # would let this module hash behind the counter's back.
    for path in sorted(set(section.key_paths(repos))):
        _cc.hash_file(h, path)
    for line in tail:
        h.update(f"{line}\n".encode())
    return h.hexdigest()


def section_digests(
    repos: list[Repo],
    sections: list[Section],
    *,
    known: "dict[str, str] | None" = None,
) -> dict[str, str]:
    """Return the digest per named section, computing the shared tail once.

    *sections* is REQUIRED, and deliberately has no ":data:`SECTIONS`"
    default: digesting a section is what makes a caller pay for that
    section's key set, so which ones are wanted is never an incidental
    choice — and a default would quietly enlist every existing caller into
    each newly registered section, which is exactly what
    :data:`MERGED_VIEW_SECTIONS` exists to prevent on the write side.

    Digests already present in *known* are trusted and carried over instead
    of being recomputed — the seam that lets a read's validity check and the
    write that follows a miss share ONE computation per section
    (:func:`completion_cache.read_sections
    <otto.config.completion_cache.read_sections>` fills the dict,
    :func:`completion_cache.write_sections
    <otto.config.completion_cache.write_sections>` consumes it).
    """
    out: dict[str, str] = {}
    tail: list[str] | None = None
    for section in sections:
        if known is not None and section.name in known:
            out[section.name] = known[section.name]
            continue
        if tail is None:
            tail = _shared_tail(repos)
        out[section.name] = _digest(section, repos, tail)
    return out


def read_section(repos: list[Repo], name: str) -> "dict[str, Any] | None":
    """Return section *name*'s fresh payload, or ``None`` (cold for any reason).

    ``KeyError`` for an unregistered *name*. The single-section view of
    :func:`completion_cache.read_sections
    <otto.config.completion_cache.read_sections>`: one file open, one
    section's digest — a ``names`` reader never pays the ``tests`` corpus
    walk.
    """
    payloads = _cc.read_sections(repos, [name])
    return None if payloads is None else payloads[name]


def write_section(
    repos: list[Repo], name: str, payload: dict[str, Any], *, tainted: bool = False
) -> None:
    """Write (or update) section *name* with *payload*, leaving the others alone.

    ``KeyError`` for an unregistered *name*, raised before any I/O. A
    *tainted* section is stored but never served — write it when the data
    was collected while bootstrap reported errors, so the fix (or the TTL)
    forces a full load instead of serving partial names forever.
    """
    _cc.write_sections(repos, {name: payload}, tainted=tainted)
