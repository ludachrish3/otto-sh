"""Section registry for the shell-completion cache (spec 2026-09-01, Fix C).

A *section* is a registration — ``(name, key_paths or derived_from, collect)``
— over one shared digest (:func:`section_digest`), one generic reader
(:func:`read_section`) and one generic writer (:func:`write_section`). The
cache file stores each section under its own name with its own digest::

    {"schema": N, "sections": {"<name>": {"fingerprint", "generated_at",
                                          "tainted", "payload",
                                          "ttl_seconds", "lab_key_paths"}, ...}}

Still ONE file and one open per read — the network-filesystem optimum,
preserved deliberately. Reserved ``__*__`` namespaces (the per-file test
tables, the dynamic tunnel ids) live beside ``"sections"`` at the top level,
unchanged.

Two sections:

- ``names`` — everything whose registration source is bounded: instruction
  names, hosts, backends, third-party CLI commands. Keys on the init trees,
  ``.otto/settings.toml`` and the lab files (the hosts payload is served from
  here, so a ``lab.json`` edit must move this digest). No test file keys it:
  a test file cannot register anything, so editing one never changes a
  cached flag.
- ``shim`` — the self-describing entry the console-script shim answers a
  bash TAB from; its digest is DERIVED from ``names``'s, so it moves whenever
  that does, with no walk of its own.

Test names have no section: they live in each repo's per-file table
(:mod:`otto.config.collected_tests`), which only a pytest collection writes
and which validates itself, one ``stat`` per path it tracks. A rebuild reads
no test file.

Adding a further cached item normally needs just ONE ``Section(...)`` entry
in :data:`SECTIONS` — no new digest function, no reader branch, no schema
bump, and no writer keyword: a plain new section is read and written through
:func:`read_section` / :func:`write_section`, never through
:func:`completion_cache.write_cache <otto.config.completion_cache.write_cache>`.
``shim`` is the one exception so far: ``entry()`` needs it written in the SAME
atomic update as ``names``, which is why ``write_cache`` grew a ``shim=``
keyword solely for that ordering guarantee — a section that does not need
atomicity with ``names`` should not assume it needs one too.

Digest contributions that are not stat-able files — the literal
``unresolved:<name>`` token for an init module that resolves under no
``libs`` entry, and the process inventory's freshness text — are mixed into
EVERY section's digest (the shared tail): they describe process-wide state
no path list can carry, and a new section inherits them automatically.

Taint is per-section: a section written while bootstrap reported errors is
stored with ``"tainted": true`` and never served — otherwise help would go
silently and permanently partial, because the broken file's stats are stable
until edited and the digest would never move.
"""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import completion_cache as _cc
from .repo import Repo

if TYPE_CHECKING:
    from ..host.os_profile import ProfileContext

SHIM_SECTION = "shim"
"""Storage name of the `shim` section. Defined here, not in :mod:`.completion_tree` (which
re-exports it), because this module sits on the WARM completion-read path (a plain
`read_cache` call lazily imports it) while `completion_tree` is cold-path-only (it resolves the
whole CLI tree); a module-scope import the other way round would drag `completion_tree` — and
everything IT imports — onto every TAB, not just a cache write."""


def _names_static_key_paths(repos: list[Repo]) -> list[Path]:
    """Every settings and init path whose edit can change a REGISTERED name.

    Registration executes init modules only, steered by ``settings.toml``.
    Test files deliberately do NOT key this section: they cannot register,
    and skipping them is the point — this key set stays small. Nor do the
    pytest configs: they decide which files are tests, and nothing in this
    payload reads a test file. The lab files the hosts/labs payloads read are
    the section's lab key paths (:func:`lab_key_paths`), stored with the
    entry by the writer.
    """
    paths: list[Path] = []
    for repo in repos:
        paths.append(repo.sut_dir / ".otto" / "settings.toml")
        paths.extend(_cc.resolved_init_paths(repo))
    return paths


def lab_key_paths(repos: list[Repo]) -> list[Path]:
    """Every lab file the repos' prepared sources read, then every directory they were found in.

    For the cache WRITER only, after init: it prepares the sources
    (:func:`~otto.labs.sources.prepared_lab_sources`) and expands their
    entries, and the writer stores the result beside the entry so a warm
    reader never does either. A source whose backend is not registered, or
    that is not file-backed, contributes nothing (its TTL class covers it);
    so does a repo whose sources fail to prepare, which no cache write may
    crash over.

    Every directory the enumeration entered is a key path too — its mtime
    moves when a file appears, is removed or is renamed there, which is how a
    NEW lab file is seen by a reader that never re-runs the glob. Those
    directories are appended, sorted, after the files.
    """
    from ..host.os_profile import ProfileContext
    from ..labs.errors import LabRepositoryError
    from ..labs.sources import prepared_lab_sources

    paths: list[Path] = []
    visited: set[Path] = set()
    profiles = ProfileContext.from_repos(repos)
    for repo in repos:
        try:
            states = prepared_lab_sources(repo, profiles=profiles)
        except LabRepositoryError:
            continue
        for state in states:
            if state.is_known():
                paths.extend(state.lab_files(visited=visited))
    return [*paths, *sorted(visited)]


def describe_lab_sources(repo: Repo, *, profiles: "ProfileContext") -> list[tuple[str, str]]:
    """``(source label, what it reads)`` for each of *repo*'s lab sources, for ``otto cache info``.

    Read before any init module runs, so a backend an init module registers
    is not registered yet: such a source is "unprepared", because whether it
    reads files cannot be told. A source that is not file-backed says so, and
    so does a repo whose sources do not prepare. *profiles* are the data
    profiles of every repo the caller holds, so the preparation is the one
    the cache writer shares.
    """
    from ..labs.errors import LabRepositoryError
    from ..labs.sources import prepared_lab_sources

    try:
        states = prepared_lab_sources(repo, profiles=profiles)
    except LabRepositoryError as e:
        return [(source.label, f"cannot prepare — {e}") for source in repo.lab_sources]
    lines: list[tuple[str, str]] = []
    for state in states:
        source = state.pending
        if not state.is_known():
            files = f"unprepared — backend {source.backend!r} is not registered before init"
        elif not state.is_file_backed():
            files = f"not file-backed ({source.backend})"
        else:
            files = ", ".join(str(p) for p in state.lab_files()) or "no lab file found"
        lines.append((source.label, files))
    return lines


def writer_key_paths(section: "Section", repos: list[Repo]) -> list[Path]:
    """Every key path of *section* as the writer computes it: its own, then its lab key paths."""
    assert section.key_paths is not None  # noqa: S101 — a derived section has no key paths
    own = section.key_paths(repos)
    return [*own, *lab_key_paths(repos)] if section.lab_keyed else own


def _collect_names(repos: list[Repo]) -> dict[str, Any]:
    """Assemble the ``names`` payload from the live registries and *repos*.

    Key-for-key the view :func:`completion_cache.read_cache` serves — the
    writer's keywords must match this key set (pinned by
    ``tests/unit/config/test_cache_sections.py``).
    """
    backends = _cc.collect_backend_names()
    return {
        "instructions": _cc.collect_current_commands(),
        "test_options": _cc.collect_test_verb_options(),
        "hosts": _cc.collect_host_ids(repos),
        "hosts_by_lab": _cc.collect_host_ids_by_lab(repos),
        "host_drops": _cc.collect_host_drops(repos),
        "docker_hosts": _cc.collect_docker_capable_host_ids(repos),
        "docker_use_cases": _cc.collect_docker_use_case_names(repos),
        "docker_images": _cc.collect_docker_image_names(repos),
        "docker_services_by_use_case": _cc.collect_docker_services_by_use_case(repos),
        "repos": _cc.collect_repo_names(repos),
        "term_backends": backends["term_backends"],
        "transfer_backends": backends["transfer_backends"],
        "usernames": _cc.collect_reservation_usernames(repos),
        "commands": _cc.collect_cli_commands(),
        "labs": _cc.collect_lab_names(repos),
        "host_classes_by_id": _cc.collect_host_classes_by_id(repos),
        "projects": _cc.collect_project_names(),
        "links": _cc.collect_links(repos),
        "logins_by_host": _cc.collect_logins_by_host(repos),
        "docker_default_parent_by_lab": _cc.collect_docker_default_parent_by_lab(repos),
    }


def _collect_shim(repos: list[Repo]) -> dict[str, Any]:
    from .completion_tree import build_shim_payload  # lazy: imports the CLI

    return build_shim_payload(repos)


@dataclass(frozen=True)
class Section:
    """One cached item: a name, its invalidation key set, and its collector.

    A section is keyed EITHER by its own paths (``key_paths``) OR by other
    sections' digests (``derived_from``), never both. A derived section's key
    set is exactly the union of its children's, so its digest is a hash of
    theirs, and computing it costs no walk and no stat of its own.
    """

    name: str
    """Storage key under ``"sections"`` in the cache file."""

    collect: Callable[[list[Repo]], dict[str, Any]]
    """Build this section's payload from live state (slow path only)."""

    key_paths: Callable[[list[Repo]], list[Path]] | None = None
    """Every path whose edit must move this section's digest, besides its lab
    key paths. Order and duplicates are irrelevant — :func:`section_digest`
    sorts and dedups."""

    lab_keyed: bool = False
    """Whether the lab files also key this section. The writer computes them
    (:func:`lab_key_paths`) and stores them with the entry; a reader takes
    them from the stored entry, so it never prepares a source or globs."""

    derived_from: list[str] = field(default_factory=list)
    """Sections whose digests this one's is composed from, in a fixed order."""

    def __post_init__(self) -> None:
        """Refuse a section with no key source, or with two."""
        if (self.key_paths is None) == (not self.derived_from):
            raise ValueError(
                f"section {self.name!r} must declare exactly one of key_paths / derived_from"
            )
        if self.lab_keyed and self.key_paths is None:
            raise ValueError(f"section {self.name!r}: a derived section cannot be lab-keyed")


SECTIONS: list[Section] = [
    Section(
        name="names", key_paths=_names_static_key_paths, lab_keyed=True, collect=_collect_names
    ),
    Section(name=SHIM_SECTION, derived_from=["names"], collect=_collect_shim),
]


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
    """Stat-based sha256 over *section*'s key paths (plus the shared tail), as the writer sees them.

    A thin wrapper over :func:`section_digests` for a single section — a
    derived section has no key paths of its own to stat, so both shapes go
    through the same memoized resolution rather than forking into two paths.
    """
    return section_digests(repos, [section])[section.name]


class _StoredKeysMissingError(LookupError):
    """A stored entry carries no usable lab key paths, so it cannot be judged."""


def stored_lab_key_paths(entry: object) -> "list[Path] | None":
    """Return the lab key paths a stored entry carries, or ``None`` if it has none usable."""
    paths = entry.get("lab_key_paths") if isinstance(entry, dict) else None
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        return None
    return [Path(p) for p in paths]


def stored_ttl_seconds(entry: object) -> "int | None":
    """Return the TTL class a stored entry carries, or ``None`` if it has none usable."""
    ttl = entry.get("ttl_seconds") if isinstance(entry, dict) else None
    return ttl if isinstance(ttl, int) and not isinstance(ttl, bool) else None


def _digest(section: Section, repos: list[Repo], tail: list[str], lab: list[Path]) -> str:
    """:func:`section_digest` with the shared tail and the lab key paths supplied by the caller."""
    assert section.key_paths is not None  # noqa: S101 — type narrows: a derived section never reaches here
    h = hashlib.sha256()
    # Via the module attribute, not a from-import: tests count digest work by
    # monkeypatching ``completion_cache.hash_file``, and a bound name here
    # would let this module hash behind the counter's back.
    for path in sorted(set(section.key_paths(repos)) | set(lab)):
        _cc.hash_file(h, path)
    for line in tail:
        h.update(f"{line}\n".encode())
    return h.hexdigest()


def section_digests(
    repos: list[Repo],
    sections: list[Section],
    *,
    known: "dict[str, str] | None" = None,
    stored: "dict[str, Any] | None" = None,
) -> dict[str, str]:
    """Return the digest per named section, computing the shared tail once.

    *sections* is REQUIRED, and deliberately has no ":data:`SECTIONS`"
    default: digesting a section is what makes a caller pay for that
    section's key set, so which ones are wanted is never an incidental
    choice — and a default would quietly enlist every existing caller into
    each newly registered section.

    Digests already present in *known* are trusted and carried over instead
    of being recomputed — the seam that lets a read's validity check and the
    write that follows a miss share ONE computation per section
    (:func:`completion_cache.read_sections
    <otto.config.completion_cache.read_sections>` fills the dict,
    :func:`completion_cache.write_sections
    <otto.config.completion_cache.write_sections>` consumes it).

    A lab-keyed section's lab key paths come from *stored* (the stored
    entries, by section name) when it is given — what a reader passes, so it
    never prepares a source or globs — and are computed
    (:func:`lab_key_paths`) when it is not. A stored entry without usable lab
    key paths raises ``_StoredKeysMissingError``, which a reader takes as a
    miss.

    A derived section's digest is ``sha256`` over ``<child>:<digest>`` lines,
    computing (or reusing) the children through the same memo, so asking for
    ``shim`` alone still works and a child is never hashed twice.
    """
    out: dict[str, str] = {}
    memo: dict[str, str] = {} if known is None else dict(known)
    tail: list[str] | None = None
    computed_lab: list[Path] | None = None

    def lab_of(section: Section) -> list[Path]:
        nonlocal computed_lab
        if not section.lab_keyed:
            return []
        if stored is not None:
            paths = stored_lab_key_paths(stored.get(section.name))
            if paths is None:
                raise _StoredKeysMissingError(section.name)
            return paths
        if computed_lab is None:
            computed_lab = lab_key_paths(repos)
        return computed_lab

    def digest_of(section: Section) -> str:
        nonlocal tail
        if section.name in memo:
            return memo[section.name]
        if section.derived_from:
            h = hashlib.sha256()
            for child in section.derived_from:
                h.update(f"{child}:{digest_of(section_by_name(child))}\n".encode())
            value = h.hexdigest()
        else:
            if tail is None:
                tail = _shared_tail(repos)
            value = _digest(section, repos, tail, lab_of(section))
        memo[section.name] = value
        return value

    for section in sections:
        out[section.name] = digest_of(section)
    return out


def needs_lab_keys(section: Section) -> bool:
    """Whether *section*'s digest depends on lab key paths, its own or a child's."""
    if section.lab_keyed:
        return True
    return any(needs_lab_keys(section_by_name(child)) for child in section.derived_from)


def read_section(repos: list[Repo], name: str) -> "dict[str, Any] | None":
    """Return section *name*'s fresh payload, or ``None`` (cold for any reason).

    ``KeyError`` for an unregistered *name*. The single-section view of
    :func:`completion_cache.read_sections
    <otto.config.completion_cache.read_sections>`: one file open, one
    section's digest.
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
