"""Shell-completion cache (Phase B).

Tab completion invokes ``otto`` just far enough to walk the Typer command
tree. The expensive step during that walk is not parsing CLI args — it's the
user code that populates dynamic subcommands:

- bootstrap imports every repo's ``init`` modules so ``@instruction()``
  decorators can register into ``INSTRUCTIONS``, and options classes register
  for the ``run`` and ``test`` verbs.

That executes arbitrary user code. Test files are never imported for
completion's cache: test names come only from pytest's collections, which
write each repo's per-file table (:mod:`otto.config.collected_tests`) apart
from any rebuild.

For completion all we actually need is the *names* those decorators would
register and the *option schemas* the user can tab-complete against. This
module captures both in a small JSON file and, when the cache is valid, lets
the caller skip the user code entirely.

Only completion and the root help screen read this cache, and only they
validate and rebuild it (:func:`otto.cli.main.entry`); an ordinary command
never touches it. So "slow path" in this module means a cache REBUILD — a TAB
or root help that missed — not every real invocation, as it once did.
``docs/architecture/subsystems/completion-cache.md`` is the design page:
sections, freshness, taint, who reads and who rebuilds.

Cache location
--------------

``<workspace home>/completion_cache.json`` -- see :mod:`otto.config.home`.
The workspace home is keyed by the normalized ``OTTO_SUT_DIRS`` set under
``~/.otto`` (relocatable with ``OTTO_HOME``), so caching needs no ``OTTO_XDIR``
and one workspace has exactly one cache however many directories otto is
invoked from.

Cache schema
------------

One top-level ``"schema"`` stamp and one ``"sections"`` map — see
:mod:`otto.config.cache_sections` for the registry defining the sections and
their key sets. Each section carries its OWN stat-digest, the wall-clock time
it was generated, and a taint flag::

    {
        "schema": 23,
        "sections": {
            "names": {
                "fingerprint": "<sha256 hex>",
                "generated_at": 1745000000,
                "tainted": false,
                "payload": {
                    "instructions": [{"name": "install", "options": [...]}, ...],
                    "hosts": ["test1", "test2", ...],
                    "hosts_by_lab": {"unix": ["test1", "test2"], ...},
                    "host_drops": [{"repo": "sut", "where": "...", "reason": "..."}],
                    "docker_hosts": ["test1", ...],
                    "docker_use_cases": ["integration", ...],
                    "term_backends": ["ssh", "telnet", ...],
                    "transfer_backends": [
                        {"name": "scp", "host_families": ["unix"]}, ...
                    ],
                    "usernames": ["alice", ...],
                    "commands": [
                        {"name": "flash", "help": "...", "lab_free": false},
                        ...
                    ],
                    "labs": ["tech1", "tech2", ...]
                }
            },
            "shim": {...}
        }
    }

Each ``options`` entry is a ``{"name", "flags", "kind", "default", "help"}``
dict built by ``_serialize_options``; a third-party GROUP entry under
``commands`` also carries recursive ``"commands"`` child metadata, and a
flattening single-command app carries ``"options"`` instead (both keys
omitted when empty). A tainted section — written while bootstrap reported
errors — is stored but never served, so a broken repo forces the full load
instead of silently serving partial names forever (the broken file's stats
are stable until edited, so no digest would ever move). A file from an older
schema keeps only its reserved ``__*__`` namespaces on the first new write:
its entries can never be served again and are dropped rather than being
parsed by every TAB forever.

Collected test-name namespace
-----------------------------

Alongside the sections, the reserved key ``"__collected_tests__"`` holds each
repo's per-file test table, keyed by its ``sut_dir``
(:mod:`otto.config.collected_tests` documents the shape). Only a pytest
collection writes it — a run, a listing, or the collect child a test-name
TAB starts — never the rebuild, which runs no collection. Keeping it in its
own key means the two writers touch disjoint data and can't clobber. A table
validates itself (one ``stat`` per path it tracks), so no section digest
keys it.

Digests
-------

Each section's digest (:mod:`otto.config.cache_sections`) is a sha256 over
``(path, mtime_ns, size)`` triples for every file in its key set
(:func:`hash_file`): each SUT's ``settings.toml``, every ``.py`` file under
any ``init`` module and every lab file named by a repo's json
``[[lab.sources]]`` entries (directory entries contribute their
``lab.json``; ``.json`` entries are the file themselves), plus the
directories the lab-file enumeration entered. File contents are never read,
so a digest is cheap to compute. No test file keys a section: a test file
cannot register anything.

A stale digest is always safe: the fast path is skipped, the slow path
runs as normal and rewrites the cache afterward.

A *constant* digest is the failure mode worth knowing about. A repo with a
``[[lab.sources]]`` entry on a non-json backend, or which configures a
``[reservations]`` backend, keeps that inventory somewhere no stat can see —
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Union, get_args, get_origin

from ..errors import OttoError, is_containable
from . import corpus_snapshot

if TYPE_CHECKING:
    import threading
    from collections.abc import Callable, Collection, Sequence

    from ..inventory.protocol import Inventory
    from ..labs import HostSummary
    from ..labs.drops import HostDrop
    from .cache_sections import Section
    from .repo import Repo


COMPLETION_ENV_VAR = "_OTTO_COMPLETE"
CACHE_FILENAME = "completion_cache.json"

# Bump when the on-disk schema changes in a way older readers can't parse.
# v9: added "labs" and "tests" (sources for --lab / --tests completion).
# v10: added "hosts_by_lab" (lab-scoped `otto host <TAB>` fast path).
# v11: host-ID sources now hash lab.json (renamed from hosts.json), so cached
#      fingerprints reference a different filename.
# v12: lab.json v2 — membership by element pattern, labs declared in the labs
#      table; hosts_by_lab buckets built by the old per-host rule must not be
#      served.
# v13: host summaries may carry an inventory-supplied ip (spec 2026-08-28
#      host-inventory §11); the digest now includes the inventory fingerprint.
# v14: added "docker_use_cases" (source for `otto docker compose
#      build|up|down <TAB>`).
#      The bump is REQUIRED, not cosmetic: `read_cache` defaults the key with
#      `.get("docker_use_cases", [])`, so a surviving v13 entry would validate
#      as an empty list, `_use_case_completer`'s `isinstance(..., list)` guard
#      would take the CACHE branch, and every tab-complete would offer nothing
#      instead of falling back to a live scan of the repos.
# v15: sections layout — the top-level fingerprint key is GONE. Entries live
#      under {"schema": N, "sections": {name: {fingerprint, generated_at,
#      tainted, payload}}} with a PER-SECTION digest, so a names-only reader
#      can locate and validate its entry without the corpus walk
#      (otto.config.cache_sections). Old fingerprint-keyed files have no
#      "schema"/"sections" keys and miss structurally; the first new write
#      drops their dead entries while carrying the reserved __*__ namespaces
#      forward.
# v16: layout unchanged from v15 — the bump corrects CONTENT. v15 writers
#      misattributed @cli_command-decorated third-party leaves to
#      otto.cli.registry (the decorator's frame) and the built-in filter
#      dropped them from the "commands" payload. The section's digest keys on
#      the REPO's files, not on otto's code, so without the bump a surviving
#      v15 entry would keep serving a root help missing those commands for up
#      to the full TTL after the fixed otto ships.
# v17: layout unchanged — CONTENT again. v16 writers enumerated each repo's
#      hosts against its OWN [inventory] (else the user file) instead of the
#      process inventory dispatch resolves, so a workspace declaring the
#      inventory in one repo and referencing it from another cached a hosts
#      payload missing every referenced host. Same reasoning as v16: the
#      digest keys on the repo's files, so without the bump that entry keeps
#      serving the missing hosts for up to the TTL after the fix ships.
# v18: the ``shim`` section — the self-describing entry the console-script
#      shim answers a bash TAB from, keyed on ``names`` U ``tests``, written
#      (at the time) by every real invocation. Also folds in three prior CONTENT-only
#      changes that never got their own bump: the ``tests`` payload gained
#      ``markers`` (a surviving v17 entry would make every ``-m`` TAB fall
#      back to a live corpus scan for a full TTL, because the section would
#      validate but its payload lacks the key the fast path now reads); the
#      marker floor now mixes in otto's OWN ``otto.suite.markers.OTTO_MARKERS``
#      alongside the repo-scanned ones, which the repo-file digest cannot see
#      (a future addition there needs a bump of its own); and the three
#      ``names`` keys ``host_classes_by_id``, ``projects`` and ``links``.
# v19: layout unchanged — CONTENT. Host identity changed under spec
#      2026-09-05 element-object: positional handles (``dut1`` for the first
#      host of element ``dut``) are gone from the ``host_ids`` payload, and an
#      element's ``id`` no longer appears in a host id. The digest keys on the
#      repo's lab files, which did not change, so without the bump a warm v18
#      entry would keep offering handles that no longer dispatch and ids that
#      no host reports, for up to the full TTL after the fix ships.
# v20: names key logins_by_host — the per-host login map ``otto host <id>
#      <verb> --user <TAB>`` reads (login, protocols, proxy off each host's
#      HostSummary, never a password). A surviving v19 entry simply lacks the
#      key; the bump exists so a reader pinned to the schema does not have to
#      special-case its absence on an old entry.
# v21: otto test is a leaf; names payload drops "suites". Test names complete
#      as ``otto test``'s variadic ``NAMES`` from the ``tests`` section, and a
#      rebuild never imports a test file.
# v22: names payload gains "test_options", the test verb's serialised flags,
#      so the Typer fast path offers them on ``otto test --<TAB>``.
# v23: the ``tests`` section is gone, and with it the static name and marker
#      floors: a test-name TAB answers from the per-file tables pytest's
#      collections write (``__collected_tests__``). ``shim`` is derived from
#      ``names`` alone, its payload drops ``tests_digest`` and ``keys`` becomes
#      the one ``names`` key list, and it gains ``tables`` (the repos whose
#      tables a tests site reads). A v22 shim would read a digest-keyed
#      name blob no writer updates any more.
SCHEMA_VERSION = 23

# A conftest holds no test of its own, but pytest loads it before collecting
# anything under its directory, so the per-file tables watch every one.
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
# Deliberately NOT a backend-supplied revision token: the section digests
# run on the completion fast path, and querying a possibly-networked backend
# there is exactly the cost this cache exists to avoid — it would make every
# TAB keystroke depend on the inventory service being reachable.
UNFINGERPRINTED_CACHE_TTL_SECONDS = 5 * 60


# --- Collected (pytest-accurate) test names, for NAMES completion -----------
#
# The reserved ``__collected_tests__`` namespace holds each repo's per-file
# table (``otto.config.collected_tests``), keyed by the repo's ``sut_dir``:
# what pytest collected from every test file, validated by one stat per path.
# Only a pytest collection writes it (a run, a listing, the collect child a
# completer spawns); the cache rebuild never does, and never runs a collection.
# Completion reads it directly: the console-script shim for a bash TAB, the
# completers on the full path.
COLLECTED_TESTS_KEY = "__collected_tests__"

# Env var that flips ``otto`` into the collect child: the process a completer
# spawns to seed or refresh the per-file test tables
# (:func:`otto.config.collected_tests.collect_child_main`). Handled as an early
# exit in :func:`otto.cli.main.entry`, before the normal CLI runs. The child
# prints nothing: its parent, if it waits at all, reads the tables it wrote.
DUMP_TESTS_ENV_VAR = "_OTTO_DUMP_TEST_NAMES"

# Hard cap on the collect child: a cold ``otto test`` TAB blocks at most this
# long (the child stops itself; the waiting parent allows a little more before
# it kills it). "Slow on the first attempt is better than no completion" — but
# bounded, never a wedged shell.
COLLECT_TIMEOUT_SECONDS = 15
_COLLECT_KILL_GRACE_SECONDS = 2

# After a failed / timed-out collect child, no TAB waits on or spawns another
# for this long. Keeps a repo that can't collect within the timeout from
# costing a slow TAB on *every* keystroke — at most one per cooldown window.
COLLECT_COOLDOWN_SECONDS = 60
COLLECT_COOLDOWN_FILENAME = ".completion_collect.failed"
"""Stamped (mtime = when, content = why) by a collect child that failed or timed out."""

COLLECT_LOCK_FILENAME = ".completion_collect.lock"
# Who holds the lock: the token a waiting TAB hands its child, written into
# the lock, so the TAB frees the lock after killing its child only when that
# child took it (never another process's).
COLLECT_OWNER_ENV_VAR = "_OTTO_COLLECT_OWNER"
# A lock older than this is treated as orphaned (its holder died) and stolen,
# so a crashed collector can't block warming forever.
COLLECT_LOCK_STALE_SECONDS = COLLECT_TIMEOUT_SECONDS + 30


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
    """Return the cache file path.

    The workspace home (:mod:`otto.config.home`) is a stable per-user location
    derived from the SUT-dir set alone, so this no longer depends on
    ``OTTO_XDIR`` and no longer has a "caching disabled" case. That is a
    behaviour change, not just a move: an operator who never set an xdir used
    to get the slow path on every invocation, silently.

    It also DEDUPLICATES. The cache's content was always a pure function of the
    workspace -- the section digests hash each repo's settings, init modules
    and lab files and nothing else -- so the xdir was a storage location, never
    a semantic key, and invoking otto from N directories against the same repos
    used to maintain N byte-identical caches.

    The return type stays ``Path | None`` because callers and the remote cache
    already branch on None; nothing produces None today.

    The directory is NOT created here -- writers create it, so a read that
    misses leaves nothing behind.
    """
    # Function-local import: this module is loaded early during config
    # bootstrap, so defer the home import to call time (it reads a fresh
    # OttoEnvSettings, which tests monkeypatch).
    from .home import workspace_home

    return workspace_home() / CACHE_FILENAME


def clear_cache() -> bool:
    """Delete the completion cache file if it exists.

    Returns True if a file was removed, False otherwise. Surface for
    ``otto cache clear``.
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


def resolved_init_paths(repo: "Repo") -> list[Path]:
    """Every ``.py`` file one of *repo*'s ``init`` names resolves to under ``libs``.

    THE enumeration of the init trees: the ``names`` section's key set
    (:mod:`otto.config.cache_sections`) hashes exactly this list, and
    :func:`unresolved_init_modules` shares its rule, so the digest can never
    disagree with itself about which files make up a registered
    instruction's source. Resolution rule: a
    package directory contributes its whole ``*.py`` tree, a plain
    ``<mod>.py`` contributes itself; a name may resolve under several
    ``libs`` entries and every match counts.

    Direct attribute access on ``init``/``libs``, deliberately: this is the
    DIGEST path, and a malformed repo double missing a pinned attribute must
    fail by name, not silently hash "nothing declared". The TTL path keeps
    its own ``getattr`` tolerance — see :func:`_has_unresolved_init_module`.
    """
    found: list[Path] = []
    for init_mod in repo.init:
        mod_base = init_mod.split(".")[0]
        for lib in repo.libs:
            mod_dir = lib / mod_base
            mod_file = lib / f"{mod_base}.py"
            if mod_dir.is_dir():
                found.extend(sorted(mod_dir.rglob("*.py")))
            elif mod_file.is_file():
                found.append(mod_file)
    return found


def unresolved_init_modules(repo: "Repo") -> list[str]:
    """*repo*'s ``init`` names that resolve under none of ``libs``, in declaration order.

    The complement of :func:`resolved_init_paths`, sharing its resolution
    rule and its direct-access rationale. Each name contributes the literal
    ``unresolved:<name>`` to the digests instead of file stats — a
    pip-installed plugin whose init module lives outside ``libs`` hashes as
    that constant string, which an upgrade never moves, so any entry here
    also shortens the TTL (:func:`_has_unresolved_init_module`).
    """
    unresolved: list[str] = []
    for init_mod in repo.init:
        mod_base = init_mod.split(".")[0]
        resolved = any(
            (lib / mod_base).is_dir() or (lib / f"{mod_base}.py").is_file() for lib in repo.libs
        )
        if not resolved:
            unresolved.append(init_mod)
    return unresolved


def _has_unresolved_init_module(repo: "Repo") -> bool:
    """Whether any of *repo*'s ``init`` names fails to resolve under its ``libs``.

    The yes/no view of :func:`unresolved_init_modules`' resolution rule, for
    the TTL decision: an unresolvable init module's digest contribution is a
    constant, so the short TTL is the only staleness bound such a repo has.

    NOT a delegation, although the rule is the same: this is the TTL path,
    where ``getattr(..., [])`` tolerance is load-bearing — a test double
    built with ``MagicMock(spec=[...])`` that omits ``init``/``libs`` must
    read as "nothing declared", not raise — while the digest enumerators
    above deliberately fail by name on such a double.
    """
    libs = getattr(repo, "libs", [])
    for init_mod in getattr(repo, "init", []):
        mod_base = init_mod.split(".")[0]
        resolved = any(
            (lib / mod_base).is_dir() or (lib / f"{mod_base}.py").is_file() for lib in libs
        )
        if not resolved:
            return True
    return False


def _has_unfingerprinted_source(repos: list["Repo"]) -> bool:
    """Report whether any repo's completion data comes from outside the digest.

    Three such sources, all read off already-parsed settings — the compiled
    source list, the raw ``[reservations]`` dict, and the ``init`` name list —
    with no pydantic and no backend construction, so this is safe on the
    completion fast path (the third bullet stats candidate init paths via
    ``is_dir()``/``is_file()`` — a handful of stats bounded by the ``init``
    list, never a walk):

    - any ``[[lab.sources]]`` entry with a non-json backend (hosts, lab
      names).
    - any ``[reservations]`` backend (``--holder`` names). The built-in json
      reservation backend does not implement username completion at all, so
      that field is populated *exclusively* by custom, typically networked
      backends — the same constant-digest problem, one field over.
    - any ``init`` name that does not resolve under ``libs``
      (:func:`_has_unresolved_init_module`). Every section digest hashes
      that as the literal string ``"unresolved:<name>"``, which a plugin
      upgrade never moves — the same constant-digest problem the two bullets
      above have, from a source that is not a backend choice at all.

    Switching either backend rewrites ``settings.toml``, whose mtime IS in the
    digest, so a repo can never inherit a cache entry written under a
    different backend choice. That invariant is what makes an entry-wide (not
    per-repo) TTL correct.

    Known limitation: a repo may re-register ``"json"`` with a replacement
    class (``register_lab_repository("json", ..., overwrite=True)``), which
    this cannot see without constructing the backend. ``build_lab_sources``
    hardcodes the ``cls(search_paths=...)`` contract for that name, so a
    replacement is deliberately impersonating the file backend; it inherits
    file-backed invalidation and ``otto cache clear``.
    """
    for repo in repos:
        if any(src.backend != "json" for src in getattr(repo, "lab_sources", [])):
            return True
        # `isinstance(..., dict)`: a test double's auto-attribute is truthy but
        # is not settings. The lab check above needs no such guard — a double
        # with no `lab_sources` reads as the empty list, and a MagicMock's
        # auto-attribute iterates empty.
        reservations = getattr(repo, "reservation_settings", None)
        if isinstance(reservations, dict) and reservations:
            return True
        if _has_unresolved_init_module(repo):
            return True
    return False


def _cache_ttl_seconds(repos: list["Repo"]) -> int:
    """Effective completion-cache TTL for *repos*.

    Shortened when any repo's completion data comes from a source the
    fingerprint cannot see — see :data:`UNFINGERPRINTED_CACHE_TTL_SECONDS`.

    Applies to the sections only. The per-file test tables keep the long
    TTL (:data:`CACHE_TTL_SECONDS`): each validates itself by one ``stat`` per
    path it tracks (:mod:`otto.config.collected_tests`).
    """
    if _has_unfingerprinted_source(repos):
        return UNFINGERPRINTED_CACHE_TTL_SECONDS
    return CACHE_TTL_SECONDS


def ancestor_conftests(test_dir: Path, sut_dir: Path) -> list[Path]:
    """Return the ``conftest.py`` paths ABOVE *test_dir*, up to and including *sut_dir*'s own.

    They count because pytest is run with the tests dirs as arguments and the
    SUT as rootdir, so a conftest anywhere between them is loaded, and a
    ``pytest_generate_tests`` there parametrizes what gets collected. Returned
    whether or not they exist: a watcher that stats a missing one sees it
    appear. A *test_dir* outside *sut_dir* has none.
    """
    found: list[Path] = []
    for ancestor in test_dir.parents:
        if ancestor == sut_dir or sut_dir in ancestor.parents:
            found.append(ancestor / CONFTEST_FILENAME)
        if ancestor == sut_dir:
            break
    return found


def hash_file(h: "hashlib._Hash", path: Path) -> None:
    """Fold *path*'s ``(path, mtime_ns, size)`` stat triple into digest *h*.

    A path that fails to stat folds in as ``missing:<path>`` — deliberately,
    so the digest moves when the file APPEARS. Contents are never read. The
    shared primitive under :func:`otto.config.cache_sections.section_digest`
    and :func:`_tunnel_scope_digest`.
    """
    st = corpus_snapshot.stat(path)
    if st is None:
        h.update(f"missing:{path}\n".encode())
        return
    h.update(f"{path}|{st.st_mtime_ns}|{st.st_size}\n".encode())


def _hash_lab_files(h: "hashlib._Hash", repo: "Repo") -> None:
    """Fold every file a repo's compiled ``[[lab.sources]]`` entries read into *h*.

    What :func:`_tunnel_scope_digest` hashes besides the settings. A non-file
    backend contributes no lab files, so its digest never moves; it falls back
    to a short TTL instead (:data:`UNFINGERPRINTED_CACHE_TTL_SECONDS`).
    """
    for src in repo.lab_sources:
        for lab_file in src.lab_files():
            hash_file(h, lab_file)


def _tunnel_scope_digest(repos: list["Repo"]) -> str:
    """Sha256 of what a tunnel id actually depends on: settings, lab, inventory.

    Tunnel ids are discovered by process/argv inspection against the live
    lab (spec 2026-09-25-dispatch-startup-cost-design.md §4.2) — they do not
    depend on test sources at all, so they were moved off the whole-corpus
    digest they were first keyed by: an ordinary ``otto tunnel
    list``/``remove`` paid a corpus-proportional cost for no reason. This
    mixes in the settings file, the lab files (:func:`_hash_lab_files`) and
    the inventory term (:func:`_inventory_fingerprint`), and nothing else —
    no init modules, no pytest config, no test sources.
    """
    h = hashlib.sha256()
    for repo in sorted(repos, key=lambda r: str(r.sut_dir)):
        hash_file(h, repo.sut_dir / ".otto" / "settings.toml")
        _hash_lab_files(h, repo)
    h.update(f"inventory:{_inventory_fingerprint(repos).text}\n".encode())
    return h.hexdigest()


@dataclass(frozen=True)
class _InventoryDigest:
    """The inventory's contribution to the fingerprint, and whether it may be STORED."""

    text: str
    """What every digest mixes in."""

    cacheable: bool
    """``False`` when *text* is a one-shot value no later read can ever match."""


def _inventory_fingerprint(repos: list["Repo"]) -> "_InventoryDigest":
    """Return the process inventory's freshness signal (spec 2026-08-28 host-inventory §11).

    ``none`` without an inventory; the backend's ``fingerprint()`` (file
    path/mtime/size, or a snapshot hash) when it has one. A backend that
    returns ``None`` is not cacheable: mix in the clock so the entry never
    matches — completion stays correct, by loading (documented) — and report
    ``cacheable=False`` so no writer stores an entry under it
    (:func:`_fingerprint_is_ephemeral`).

    ``except Exception``, and ``fingerprint()`` INSIDE the guard, for the
    reason :func:`_enumerate_host_summaries` gives: completion never crashes
    the shell. ``construct_inventory`` wraps only ``TypeError``/``ValueError``
    from a third-party constructor, so a networked backend's freshness probe
    (§11's own example) can raise anything at all — an HTTP timeout, say — and
    this runs inside ``write_cache``, past ``otto.cli.main``'s
    ``suppress(OSError)``, which would traceback an otherwise-successful
    command AFTER its real work was done.

    A FAILURE IS EPHEMERAL TOO (R18), for the same reason a missing
    fingerprint is: an inventory whose freshness probe failed has no stable
    identity to key an entry on. The text still moves the digest — so a
    broken declaration is never served the working one's entry, and the fix
    moves it back — but nothing is STORED under it. Assuming the text stable
    would have staked the cache's boundedness on a third party's error
    strings: a message carrying a timestamp, a request id or a resolved IP is
    ordinary, and every one of them would append a dead entry per invocation,
    which is the growth :func:`_fingerprint_is_ephemeral` exists to stop.
    """
    from ..inventory import build_inventory

    try:
        inventory = build_inventory(repos)
        if inventory is None:
            return _InventoryDigest(text="none", cacheable=True)
        fp = inventory.fingerprint()
    except Exception as e:  # noqa: BLE001 — completion never crashes the shell
        return _InventoryDigest(text=f"error:{type(e).__name__}:{e}", cacheable=False)
    if fp is None:
        return _InventoryDigest(text=f"uncacheable:{time.time_ns()}", cacheable=False)
    return _InventoryDigest(text=fp, cacheable=True)


def _fingerprint_is_ephemeral(repos: list["Repo"]) -> bool:
    """Whether an entry written NOW could never be RELIABLY read back (spec §11).

    Two cases, both from :func:`_inventory_fingerprint`: an inventory that
    cannot report freshness gets a clock-stamped digest, which is a miss the
    instant it is written; and one whose freshness probe RAISED has no stable
    identity at all, so whether its digest repeats is a third party's error
    string to decide. Storing under either appends one dead entry per otto
    invocation, forever, into a file every TAB parses and every writer
    rewrites whole — an unbounded cache that never serves a hit, which is
    strictly worse than no cache at all.

    Checked by EVERY digest-keyed writer (the sections, the tunnel ids), not
    just the largest: the payloads differ, the unbounded growth does not.

    Costs one extra inventory resolution per write. Writers run at most once
    per invocation (a rebuild, or a reserved-namespace record) and
    construction does no I/O, so the price is the
    backend's own ``fingerprint()`` — which the digest was going to call
    anyway.
    """
    return not _inventory_fingerprint(repos).cacheable


def inventory_digest_text(repos: list["Repo"]) -> str:
    """Return the inventory's digest line — the public seam for the section registry.

    Every section digest ends with this text
    (:mod:`otto.config.cache_sections`): the process has exactly ONE
    inventory, resolved across every active repo plus the user file, and a
    change in that resolution must invalidate what was cached under it.
    """
    return _inventory_fingerprint(repos).text


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
        if get_origin(ann) is Annotated:
            args = get_args(ann)
            base = args[0]
            # OptionInfo lives at module path typer.models.OptionInfo; match on
            # attribute shape to avoid importing typer at module load.
            meta = next(
                (a for a in args[1:] if hasattr(a, "param_decls")),
                None,
            )
        elif param.default is not inspect.Parameter.empty:
            # A bare annotation with a default is what typer makes a plain
            # `--name` option of: an options-class field written without
            # `typer.Option(...)` (`firmware: str = "latest"`).
            base, meta = ann, typer.Option()
        else:
            log.debug(
                f"completion-cache: skipping option {command_name}.{pname!r} — "
                f"annotation {ann!r} is not Annotated[...] and has no default",
            )
            return None
        if meta is None:
            log.debug(
                f"completion-cache: skipping option {command_name}.{pname!r} — "
                f"no typer.Option metadata in annotation",
            )
            return None

        kind = _type_to_kind(base)
        if kind is None and getattr(meta, "click_type", None) is not None:
            # An explicit click_type is how a type typer cannot convert (a
            # pydantic SecretStr) becomes a flag at all: the command line
            # hands it a string, so a string is what completion rebuilds.
            kind = "str"
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
                # A flag whose help hides its default (``show_default=False``,
                # which otto.params.options_params sets on every sensitive
                # field) never has that default written to disk either.
                "default": (
                    None
                    if getattr(meta, "show_default", True) is False
                    else _json_safe_default(param.default)
                ),
                "help": getattr(meta, "help", None) or "",
            }
        )
    return options


# ---------------------------------------------------------------------------
# Read / write
# ---------------------------------------------------------------------------


def _section_payload_if_fresh(
    entry: Any, digest: str, *, ttl: int, now: float
) -> dict[str, Any] | None:
    """Return one section's payload iff its stored entry is servable, else ``None``.

    Servable: a dict, NOT tainted, carrying a numeric ``generated_at``
    inside *ttl* of *now*, a ``fingerprint`` equal to the freshly computed
    *digest*, and a dict ``payload``.
    """
    if not isinstance(entry, dict) or entry.get("tainted"):
        return None
    generated_at = entry.get("generated_at")
    if not isinstance(generated_at, (int, float)) or now - generated_at > ttl:
        return None
    if entry.get("fingerprint") != digest:
        return None
    payload = entry.get("payload")
    return payload if isinstance(payload, dict) else None


def read_sections(
    repos: list["Repo"],
    names: "Collection[str]",
    *,
    digests: dict[str, str] | None = None,
) -> dict[str, dict[str, Any]] | None:
    """Return the validated payload per requested section, or ``None``.

    *names* is REQUIRED, and deliberately has no "all registered sections"
    default: reading every section is what makes a reader pay for corpora it
    does not consume, and an implicit default would silently re-enlist every
    existing caller into each new :class:`~otto.config.cache_sections.Section`.
    An unknown name raises ``KeyError``
    rather than reading as a miss. ``None`` means at least one requested
    section cannot be served — file missing or corrupt, top-level schema
    mismatch, section absent, TAINTED, digest mismatch, or TTL expired — and
    the caller should fall back to loading. All-or-nothing across the
    REQUESTED sections only.

    One file, ONE open per read — the network-filesystem optimum, preserved
    deliberately across the section split.

    *digests*, when given, is both a memo and a return channel: section
    digests already present in it are trusted (not recomputed), and every
    digest computed here is recorded back. Threading that dict from
    :func:`cache_rebuild_is_worthwhile` into :func:`write_cache` is what
    keeps each section's key set stat-hashed AT MOST ONCE per invocation.

    The TTL is one value for the whole file (:func:`_cache_ttl_seconds` —
    the unfingerprinted-source rules are repo-wide, not per-section) but is
    enforced against each section's own ``generated_at``.
    """
    from .cache_sections import section_by_name, section_digests

    # Resolve names FIRST: an unknown section is a caller bug and must raise
    # even when the cache file is absent, not read as a miss.
    chosen = [section_by_name(name) for name in names]
    if not repos:
        return None
    cache_path = _cache_path()
    if cache_path is None or not cache_path.is_file():
        return None
    try:
        data = json.loads(cache_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("schema") != SCHEMA_VERSION:
        return None
    stored = data.get("sections")
    if not isinstance(stored, dict):
        return None

    fresh = section_digests(repos, chosen, known=digests)
    if digests is not None:
        digests.update(fresh)

    ttl = _cache_ttl_seconds(repos)
    now = time.time()
    payloads: dict[str, dict[str, Any]] = {}
    for section in chosen:
        payload = _section_payload_if_fresh(
            stored.get(section.name), fresh[section.name], ttl=ttl, now=now
        )
        if payload is None:
            return None
        payloads[section.name] = payload
    return payloads


def read_cache(
    repos: list["Repo"],
    *,
    digests: dict[str, str] | None = None,
    require: "Collection[str]" = (),
) -> dict[str, Any] | None:
    """Return the ``names`` section's payload, checked key by key, or ``None``.

    Valid iff the ``names`` section validates — present, untainted, its digest
    matching, inside the TTL (:func:`read_sections`). ``None`` also covers:
    empty repos (would produce the empty-tree digests any shell without
    ``OTTO_SUT_DIRS`` computes, poisoning the cache for other shells), cache
    file missing or corrupt, and schema mismatch. In every case the caller
    should fall back to the slow path.

    *require* names further sections that must ALSO validate — read in the
    SAME :func:`read_sections` call (the single open stays single) but never
    merged into the returned view: they widen validation only, never the
    payload. :func:`cache_rebuild_is_worthwhile` passes ``require=("shim",)``
    so a lost or stale ``shim`` section makes a rebuild worthwhile even while
    ``names`` still validates on its own.

    On success returns one flat dict with ``instructions``, ``test_options``,
    ``hosts``, ``hosts_by_lab``, ``docker_hosts``, ``docker_use_cases``,
    ``term_backends``, ``transfer_backends``, ``usernames``, ``commands``,
    ``labs``, ``host_classes_by_id``, ``projects``, ``links`` and
    ``logins_by_host`` keys. ``instructions`` and ``hosts`` are required; the
    rest default to empty when a payload omits them.

    *digests*, when given, collects the per-section digests computed here
    for reuse by a subsequent :func:`write_cache` — see
    :func:`read_sections`.
    """
    payloads = read_sections(repos, ["names", *require], digests=digests)
    if payloads is None:
        return None
    merged = payloads["names"]

    instructions = merged.get("instructions")
    test_options = merged.get("test_options", [])
    hosts = merged.get("hosts")
    hosts_by_lab = merged.get("hosts_by_lab", {})
    docker_hosts = merged.get("docker_hosts", [])
    docker_use_cases = merged.get("docker_use_cases", [])
    term_backends = merged.get("term_backends", [])
    transfer_backends = merged.get("transfer_backends", [])
    usernames = merged.get("usernames", [])
    commands = merged.get("commands", [])
    labs = merged.get("labs", [])
    host_drops = merged.get("host_drops", [])
    host_classes_by_id = merged.get("host_classes_by_id", {})
    projects = merged.get("projects", [])
    links = merged.get("links", [])
    logins_by_host = merged.get("logins_by_host", {})
    if (
        not isinstance(instructions, list)
        or not isinstance(test_options, list)
        or not isinstance(hosts, list)
        or not isinstance(hosts_by_lab, dict)
        or not isinstance(docker_hosts, list)
        or not isinstance(docker_use_cases, list)
        or not isinstance(term_backends, list)
        or not isinstance(transfer_backends, list)
        or not isinstance(usernames, list)
        or not isinstance(commands, list)
        or not isinstance(labs, list)
        or not isinstance(host_drops, list)
        or not isinstance(host_classes_by_id, dict)
        or not isinstance(projects, list)
        or not isinstance(links, list)
        or not isinstance(logins_by_host, dict)
    ):
        return None
    return {
        "instructions": instructions,
        "test_options": test_options,
        "hosts": hosts,
        "hosts_by_lab": hosts_by_lab,
        "host_drops": host_drops,
        "docker_hosts": docker_hosts,
        "docker_use_cases": docker_use_cases,
        "term_backends": term_backends,
        "transfer_backends": transfer_backends,
        "usernames": usernames,
        "commands": commands,
        "labs": labs,
        "host_classes_by_id": host_classes_by_id,
        "projects": projects,
        "links": links,
        "logins_by_host": logins_by_host,
    }


def cache_rebuild_is_worthwhile(
    repos: list["Repo"], *, digests: dict[str, str] | None = None
) -> bool:
    """Whether collecting and writing a fresh entry would do any good.

    False when write_cache would drop the result (empty repos, no cache
    path, ephemeral fingerprint) or when every on-disk section is already
    valid — in every False case the caller should skip the collect, which
    runs the repos' init modules and enumerates their hosts. ``not repos`` is
    checked FIRST: the ephemeral probe resolves the process inventory, which
    may be a networked backend, and an empty-repos caller must not pay that
    for an answer that is always
    False.

    *digests* is filled with the per-section digests the validity check
    computes, for the caller to hand straight to :func:`write_cache` on a
    miss — never compute a digest twice (:func:`read_sections`).

    Also requires the ``shim`` section to validate (:func:`read_cache`'s
    *require*): a lost or stale ``shim`` entry is a miss for the WRITER, so a
    deleted or hand-edited entry is rebuilt by the next TAB or root help that
    checks, instead of handing over forever — without a second open of the cache
    file, since the digest it needs is computed in the same
    :func:`read_sections` call as the ``names`` check above.
    """
    return cache_is_writable(repos) and cache_is_stale(repos, digests=digests)


def cache_is_writable(repos: list["Repo"]) -> bool:
    """Whether :func:`write_cache` would keep an entry for *repos* at all.

    The cheap half of :func:`cache_rebuild_is_worthwhile`: no key-set stats,
    so a caller can ask it before paying for anything a rebuild needs. False
    with no repos, no cache path, or an inventory whose fingerprint is
    ephemeral; ``not repos`` first, for the reason
    :func:`cache_rebuild_is_worthwhile` gives.
    """
    return bool(repos) and _cache_path() is not None and not _fingerprint_is_ephemeral(repos)


def cache_is_stale(repos: list["Repo"], *, digests: dict[str, str] | None = None) -> bool:
    """Whether any on-disk section, ``shim`` included, fails to validate for *repos*.

    The key-set half of :func:`cache_rebuild_is_worthwhile`, for a caller that
    has already established :func:`cache_is_writable`. *digests* is filled as
    :func:`cache_rebuild_is_worthwhile` documents.
    """
    from .cache_sections import SHIM_SECTION

    return read_cache(repos, digests=digests, require=(SHIM_SECTION,)) is None


def write_cache(  # noqa: PLR0913 — one keyword arg per cached name-set, by design
    repos: list["Repo"],
    instructions: list[dict[str, Any]],
    hosts: list[str],
    *,
    test_options: list[dict[str, Any]] | None = None,
    docker_hosts: list[str] | None = None,
    docker_use_cases: list[str] | None = None,
    term_backends: list[str] | None = None,
    transfer_backends: list[dict[str, Any]] | None = None,
    usernames: list[str] | None = None,
    commands: list[dict[str, Any]] | None = None,
    labs: list[str] | None = None,
    hosts_by_lab: dict[str, list[str]] | None = None,
    host_drops: list[dict[str, str]] | None = None,
    host_classes_by_id: dict[str, str] | None = None,
    projects: list[str] | None = None,
    links: list[dict[str, Any]] | None = None,
    logins_by_host: dict[str, list[dict[str, Any]]] | None = None,
    shim: dict[str, Any] | None = None,
    digests: dict[str, str] | None = None,
    tainted: bool = False,
) -> None:
    """Write every section from the slow path's collected name sets.

    The compatibility writer over :func:`write_sections`: one keyword per
    cached name-set, every one of them ``names`` payload. A NEW cached item
    should be a ``Section`` registration written through
    :func:`otto.config.cache_sections.write_section`, not another keyword.

    *shim* is the exception: it is the other registered section, added here
    so ``entry()`` writes it in the same atomic update as ``names``.
    ``entry()`` always passes it, so the section is rewritten with its
    sibling; a caller that omits it leaves the
    stored entry with a stale digest, so every later invocation reads as a
    miss and re-collects — pass ``shim=`` or write it through
    :func:`otto.config.cache_sections.write_section`.

    Skipped silently when repos is empty — an empty-repo digest is what any
    shell without ``OTTO_SUT_DIRS`` would also compute, and that would
    wrongly override a real entry's meaning. Also skipped when the process
    inventory cannot report freshness — see
    :func:`_fingerprint_is_ephemeral`; this is the write with the largest
    payload, so it is where the unbounded growth would have hurt most.

    Atomic via ``tempfile`` + ``os.replace`` so a concurrent otto
    invocation can't observe a half-written file.

    *digests*: hand it the dict a preceding
    :func:`cache_rebuild_is_worthwhile` filled, so the digests its validity
    check computed are stored rather than recomputed
    (:func:`write_sections`).

    *tainted* marks every written section unservable — pass
    ``bool(result.errors)`` from the composition root, so a partial workspace
    is stored (it still records what the digests were) but never served. See
    :func:`write_sections`.
    """
    payloads: dict[str, dict[str, Any]] = {
        "names": {
            "instructions": instructions,
            "test_options": test_options or [],
            "hosts": hosts,
            "hosts_by_lab": hosts_by_lab or {},
            "host_drops": host_drops or [],
            "docker_hosts": docker_hosts or [],
            "docker_use_cases": docker_use_cases or [],
            "term_backends": term_backends or [],
            "transfer_backends": transfer_backends or [],
            "usernames": usernames or [],
            "commands": commands or [],
            "labs": labs or [],
            "host_classes_by_id": host_classes_by_id or {},
            "projects": projects or [],
            "links": links or [],
            "logins_by_host": logins_by_host or {},
        },
    }
    if shim is not None:
        payloads["shim"] = shim
    write_sections(
        repos,
        payloads,
        tainted=tainted,
        digests=digests,
    )


def _tainted_entry_is_already_current(
    stored: dict[str, Any], sections: "list[Section]", fresh: dict[str, str]
) -> bool:
    """Whether re-writing these sections as tainted would change nothing.

    True iff EVERY section about to be written already sits on disk marked
    tainted with the identical digest. A tainted section is never served, so
    such a write is pure cost — and it is not a one-off: the entry it would
    replace is byte-for-byte the entry it just refused to serve, so a broken
    workspace would rewrite the cache on every single invocation, forever.
    That defeats the already-valid-entry write skip, on that skip's own
    rationale (a write on a network filesystem needs a commit and invalidates
    client cache), at exactly the moment a user is TABbing repeatedly to work
    out what broke.

    Both halves are load-bearing. An UNTAINTED stored entry with a matching
    digest must still be overwritten — it would otherwise keep being SERVED
    for a workspace that no longer loads. A MOVED digest must still be
    written: that is the user editing the broken file, and the fix has to be
    able to land.
    """
    for section in sections:
        entry = stored.get(section.name)
        if not isinstance(entry, dict) or not entry.get("tainted"):
            return False
        if entry.get("fingerprint") != fresh[section.name]:
            return False
    return True


def write_sections(
    repos: list["Repo"],
    payloads: dict[str, dict[str, Any]],
    *,
    tainted: bool = False,
    digests: dict[str, str] | None = None,
) -> None:
    """Write (or update) the named sections' payloads in ONE atomic file update.

    ``KeyError`` for a payload keyed on an unregistered section name, raised
    before any I/O. Skipped silently for empty repos and for an ephemeral
    inventory digest, for the reasons :func:`write_cache` documents — this
    is the single writer under both it and
    :func:`otto.config.cache_sections.write_section`.

    Sections not named in *payloads* are left as they are. Reserved
    ``__*__`` namespaces (collected tests, tunnel ids) are carried forward
    untouched. A file from an OLDER schema contributes only those
    namespaces: its entries can never be served again, so they are dropped
    here rather than being parsed by every TAB forever.

    *tainted* marks every section this call writes; a tainted section is
    stored but never served (see :func:`read_sections`). A wholly tainted
    write whose entries are ALREADY on disk, tainted, with matching digests
    is skipped — see :func:`_tainted_entry_is_already_current`.

    *digests*: per-section digests to trust instead of recomputing —
    :func:`cache_rebuild_is_worthwhile` fills it on the miss that triggered
    this write, so no key set is hashed twice. The stored digest then
    describes the tree AS THE VALIDITY CHECK SAW IT; anything user code
    moved in between simply makes the next read a miss, which is the safe
    direction.
    """
    from .cache_sections import section_by_name, section_digests

    chosen = [section_by_name(name) for name in payloads]
    if not repos or _fingerprint_is_ephemeral(repos):
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

    top: dict[str, Any] = {
        key: value
        for key, value in existing.items()
        if isinstance(key, str) and key.startswith("__") and key.endswith("__")
    }
    stored = existing.get("sections")
    sections_map: dict[str, Any] = (
        dict(stored)
        if existing.get("schema") == SCHEMA_VERSION and isinstance(stored, dict)
        else {}
    )
    fresh = section_digests(repos, chosen, known=digests)
    if tainted and _tainted_entry_is_already_current(sections_map, chosen, fresh):
        return
    now = int(time.time())
    for section in chosen:
        sections_map[section.name] = {
            "fingerprint": fresh[section.name],
            "generated_at": now,
            "tainted": bool(tainted),
            "payload": payloads[section.name],
        }
    top["schema"] = SCHEMA_VERSION
    top["sections"] = sections_map
    _atomic_write_json(cache_path, top)


def _atomic_write_json(cache_path: Path, obj: dict[str, Any]) -> None:
    """Write *obj* as JSON to *cache_path* atomically (tempfile + ``os.replace``).

    A concurrent reader always sees either the old file or the complete new
    one, never a half-written mix.

    The parent is created by the CALLERS (four sites in this module already do
    it, and did before the cache moved to the workspace home), so this function
    does not repeat it -- the tempfile below is opened with ``dir=`` set to
    that parent and would fail loudly if it were missing.
    """
    with tempfile.NamedTemporaryFile(
        mode="w",
        dir=cache_path.parent,
        delete=False,
        prefix=".completion_cache_",
        suffix=".tmp",
    ) as tmp:
        tmp_name = tmp.name
        _own_temporary_files.add(tmp_name)
        json.dump(obj, tmp)
    try:
        Path(tmp_name).replace(cache_path)
    except Exception:
        with contextlib.suppress(OSError):
            Path(tmp_name).unlink()
        raise
    finally:
        _own_temporary_files.discard(tmp_name)


_own_temporary_files: set[str] = set()
"""The temporary files this process's cache writes have open, until each is moved into place."""


def discard_own_temporary_files() -> None:
    """Remove this process's unfinished cache writes, before it exits without unwinding."""
    for name in list(_own_temporary_files):
        with contextlib.suppress(OSError):
            Path(name).unlink()
    _own_temporary_files.clear()


# ---------------------------------------------------------------------------
# Live-registry introspection (writer side)
# ---------------------------------------------------------------------------


def collect_current_commands() -> list[dict[str, Any]]:
    """Read the currently-registered instructions with their options.

    Must be called after :func:`otto.bootstrap.bootstrap` has finished
    populating ``otto.instructions.INSTRUCTIONS``. A source that never
    loaded simply has an empty registry (no init modules → no
    ``@instruction()`` ran → no entries). Test files are never read here:
    ``otto test`` has no per-test subcommands, and its flags (the run flags
    and the ``test`` verb's registered options) are served from the command
    tree like any other leaf's.

    Each item is ``{"name": str, "options": list[dict]}``; a command whose
    options can't be fully serialized is cached with ``options: []`` so
    the name still completes even though the per-option flags don't.
    """
    from ..cli.run import build_instruction_app
    from ..instructions import INSTRUCTIONS

    log = logging.getLogger(__name__)
    out: list[dict[str, Any]] = []
    for name, entry in INSTRUCTIONS.items():
        # Building an instruction's app resolves the `run` verb's options
        # classes, which a rebuild pays for so that their flags complete.
        try:
            app = build_instruction_app(entry)
        except OttoError as e:
            # A command that cannot be built (its flags clash with the
            # verb's) still completes by name; `otto run <name>` is where
            # the clash is reported.
            log.debug(f"completion-cache: {name!r} has no options, its command failed: {e}")
            app = None
        callback = None
        if app is not None and app.registered_commands:
            callback = app.registered_commands[0].callback
        options = _serialize_options(callback, command_name=name) if callback else None
        out.append({"name": name, "options": options if options is not None else []})
    return out


def collect_test_verb_options() -> list[dict[str, Any]]:
    """Serialise the ``test`` verb's registered options, the flags ``otto test`` gains.

    The completion fast path builds ``otto test`` without the options
    registry, so it reads these instead
    (:func:`otto.cli.test.build_test_app`). A verb whose flags cannot be
    built (a collision) or serialised caches ``[]``: ``otto test`` is where
    the collision is reported.
    """
    from ..cli.test import test_verb_params

    try:
        params = test_verb_params()
    except OttoError as e:
        logging.getLogger(__name__).debug(f"completion-cache: no otto test options: {e}")
        return []

    def verb_flags() -> None:  # pragma: no cover — only its signature is read
        """Carry the verb's parameters for :func:`_serialize_options`."""

    verb_flags.__signature__ = inspect.Signature(params)  # ty: ignore[unresolved-attribute]
    return _serialize_options(verb_flags, command_name="test") or []


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
        # tracebacks out of every cache rebuild — root `otto --help` included —
        # and into the shell mid-TAB. See `otto.errors.UNCONTAINABLE`.
        except BaseException as e:
            if not is_containable(e):
                raise
            log.debug(f"completion-cache: no child metadata for {spec.name!r}: {e!r}")
        out.append(entry)
    return out


def collect_reservation_usernames(repos: list["Repo"]) -> list[str]:
    """Best-effort usernames for ``--holder`` completion (cached).

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


@dataclass(frozen=True)
class RepoEnumeration:
    """What one repo's host enumeration produced: the summaries, and what it left out.

    ``drops`` is the outlet (:mod:`otto.labs.drops`): every entry, file, lab
    or source the best-effort enumeration skipped, with the reason, plus the
    enumeration's own failure or deadline when it had one. Stored beside the
    summaries in the per-process memo so the writer collects both from ONE
    enumeration, and served by ``otto cache info`` from the ``names`` payload.
    """

    summaries: list["HostSummary"] = field(default_factory=list)
    drops: list["HostDrop"] = field(default_factory=list)


def _bounded(work: "Callable[[threading.Event], RepoEnumeration]", repo: "Repo") -> RepoEnumeration:
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

    from ..labs.drops import HostDrop

    deadline = _host_summary_deadline()
    box: list[RepoEnumeration] = []
    abandoned = threading.Event()

    def _run() -> None:
        with contextlib.suppress(BaseException):  # see the docstring
            box.append(work(abandoned))

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(deadline)
    if thread.is_alive():
        abandoned.set()
        reason = (
            f"host source did not answer within {deadline}s — offering no hosts for it. "
            f"Raise {HOST_SUMMARY_DEADLINE_ENV_VAR} if it is merely slow."
        )
        logging.getLogger(__name__).warning(rf"\[completion] {repo.sut_dir}: {reason}")
        return RepoEnumeration(drops=[HostDrop(where=str(repo.sut_dir), reason=reason)])
    # `or RepoEnumeration()` rather than `box[0]`: a work() returning None
    # would otherwise hand every caller a None to read attributes off.
    return (box[0] if box else None) or RepoEnumeration()


@dataclass(frozen=True)
class InventoryResolution:
    """The process inventory as the host enumeration joins against it (spec 2026-08-28 §8).

    ``inventory`` is what ``build_inventory`` returned over EVERY active repo
    plus the user file — the resolution dispatch runs — or ``None`` when
    nothing declares one. ``error`` is set instead when that resolution
    RAISED (a broken declaration): every repo then enumerates as empty,
    because a broken declaration fails every lab load at dispatch too, and
    an id that cannot dispatch is worse offered than withheld.
    """

    inventory: "Inventory | None" = None
    error: "str | None" = None

    @property
    def label(self) -> str:
        """Identity for memo keys: the backend's label, ``none``, or the error."""
        if self.error is not None:
            return f"error:{self.error}"
        if self.inventory is None:
            return "none"
        # getattr, not the protocol attribute: this runs on the TAB path, and a
        # third-party backend missing `label` must not crash the shell.
        return getattr(self.inventory, "label", type(self.inventory).__name__)


def resolve_process_inventory(repos: "Sequence[Repo]") -> InventoryResolution:
    """Resolve the ONE inventory every repo's host enumeration joins against.

    ``build_inventory(repos)`` over the whole active set — the same call, over
    the same repos, that :func:`otto.context.open_context` and the invoke
    preamble make for dispatch — never per repo. Until 2026-09-03 each repo
    was enumerated against ``build_inventory([repo])``: its OWN declaration,
    else the user file. A workspace whose ``[inventory]`` lives in one repo
    and whose referencing hosts live in another therefore dispatched those
    hosts (``--list-hosts`` showed them) while completion never offered them,
    and cached that answer for a day. Spec §8 is one resolution per process,
    first hit across the active repos; this is the completion side of it.

    Never raises: a broken declaration is logged ONCE here and recorded on the
    result, so :func:`repo_host_summaries` answers empty for every repo without
    each of them repeating the warning.
    """
    from ..inventory import build_inventory

    try:
        return InventoryResolution(inventory=build_inventory(list(repos)))
    except Exception as e:  # noqa: BLE001 — completion never crashes the shell
        logging.getLogger(__name__).warning(
            rf"\[completion] the host inventory could not be resolved, so no host "
            f"completes until it is fixed: {e}"
        )
        return InventoryResolution(error=f"{type(e).__name__}: {e}")


def repo_host_summaries(repo: "Repo", resolution: InventoryResolution) -> list["HostSummary"]:
    """Every host *repo*'s configured host source knows — best-effort.

    *resolution* is the process inventory from :func:`resolve_process_inventory`
    over the WHOLE active repo list, resolved by the caller once per pass and
    handed to every repo: a referenced entry joins against the same inventory
    it dispatches through, whichever repo declared it. A failed resolution
    answers empty here for every repo, warned once where it failed.

    Goes through the repo's own ``[[lab.sources]]`` backends rather than
    reading its ``lab.json`` files directly, so a project with a custom host
    source gets completion from it (previously a custom backend contributed
    nothing here — completion only ever saw ``lab.json``).

    Scoped PER REPO: each repo contributes the hosts of its own sources, and
    the container ids synthesized below pair a repo's ``[docker]`` composes
    with its own docker-capable parents. Dispatch composes the SAME per-repo
    source lists into one backend (``build_lab_sources``), so the two agree on
    which sources exist; they differ only in that completion keeps each repo's
    hosts separate where dispatch merges them (later sources overriding
    earlier ones).

    Never raises, and never hangs: an unregistered backend, malformed
    settings, or a backend that explodes yields an empty list, because every
    caller is a completion path that must not crash the shell — and one that
    STALLS is bounded by :data:`HOST_SUMMARY_DEADLINE_SECONDS` rather than
    left to wedge the user's TAB. Logged at WARNING, not DEBUG: a
    transiently-unreachable custom backend would otherwise write an EMPTY host
    list into a cache that is then served for the next 24 hours, silently.
    """
    if resolution.error is not None:
        return []
    return _repo_enumeration(repo, resolution).summaries


def repo_host_drops(repo: "Repo", resolution: InventoryResolution) -> list["HostDrop"]:
    """Return what *repo*'s enumeration left OUT, with reasons — the outlet's read side.

    The same memoized enumeration :func:`repo_host_summaries` reads, so
    asking costs nothing extra. A failed inventory resolution is NOT a drop:
    nothing is enumerated, and — its digest being ephemeral — nothing is
    written that ``otto cache info`` could read a drop from. That case is
    reported on the ``inventory`` line instead (:func:`describe_inventory`).
    """
    if resolution.error is not None:
        return []
    return list(_repo_enumeration(repo, resolution).drops)


def _repo_enumeration(repo: "Repo", resolution: InventoryResolution) -> RepoEnumeration:
    key = f"{getattr(repo, 'sut_dir', repo)}|{resolution.label}"
    cached = _SUMMARY_MEMO.get(key)
    if cached is None:
        cached = _bounded(
            lambda abandoned: _enumerate_host_summaries(repo, resolution.inventory, abandoned),
            repo,
        )
        _SUMMARY_MEMO[key] = cached
    return cached


#: Per-process memo, keyed by SUT dir and the inventory's label. Three
#: collectors enumerate the same repo on one cache-write pass; without this a
#: stalled backend cost three deadlines and — worse — could time out for one
#: collector and not another, writing a cache where `otto host <TAB>` is full
#: and `otto docker --on <TAB>` is empty. Process-lifetime only, like the cache
#: itself; nothing invalidates it because nothing lives long enough to need to.
_SUMMARY_MEMO: dict[str, RepoEnumeration] = {}


def _enumerate_host_summaries(
    repo: "Repo",
    inventory: "Inventory | None",
    abandoned: "threading.Event | None" = None,
) -> RepoEnumeration:
    from ..labs import build_lab_sources, host_summaries
    from ..labs.drops import HostDrop, collecting_drops

    # The sink is opened HERE, on the enumerating thread (`_bounded` runs this
    # on a worker), so every skip the backends record lands in this list.
    with collecting_drops() as drops:
        try:
            repository = build_lab_sources([repo])
            summaries = host_summaries(repository, inventory=inventory)
        except Exception as e:  # noqa: BLE001 — completion never crashes the shell
            if abandoned is None or not abandoned.is_set():
                logging.getLogger(__name__).warning(
                    rf"\[completion] could not enumerate hosts for {repo.sut_dir}: {e}"
                )
            drops.append(HostDrop(where=str(repo.sut_dir), reason=f"host source failed: {e}"))
            summaries = []
    # One enumeration loads the documents more than once (labs, then
    # summaries), so a malformed file is recorded once per load; the outlet
    # reports each drop once. dict.fromkeys keeps first-seen order.
    return RepoEnumeration(summaries=summaries, drops=list(dict.fromkeys(drops)))


def collect_host_drops(repos: list["Repo"]) -> list[dict[str, str]]:
    """Everything the host enumeration left out, per repo — the ``names`` payload's ``host_drops``.

    ``[{"repo", "where", "reason"}, ...]``, JSON-shaped for the cache. Read
    back by ``otto cache info``, which is the outlet: a TAB stays silent by
    contract, and this is where its silence gets explained. Enumerates
    nothing of its own — it reads the same memo the id collectors filled.
    """
    resolution = resolve_process_inventory(repos)
    out: list[dict[str, str]] = []
    for repo in repos:
        label = getattr(repo, "name", None) or str(repo.sut_dir)
        out.extend(
            {"repo": str(label), "where": drop.where, "reason": drop.reason}
            for drop in repo_host_drops(repo, resolution)
        )
    return out


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
    resolution = resolve_process_inventory(repos)
    for repo in repos:
        for summary in repo_host_summaries(repo, resolution):
            if summary.docker_capable:
                ids.add(summary.id)
    return sorted(ids)


def collect_docker_use_case_names(repos: list["Repo"]) -> list[str]:
    """Enumerate every ``[[docker.use_cases]]`` name the active repos declare.

    The completion source for ``otto docker compose build|up|down <TAB>``. Read
    straight off parsed settings — no lab, no host source, no
    :func:`otto.bootstrap.bootstrap` call — so it is safe in the completion
    fast path, the same property :func:`collect_docker_capable_host_ids` has.

    Deduped across repos on purpose: one use-case name shared by three repos is
    ONE use-case (that sharing is the whole mechanism, spec §3.1), not three
    completions of the same word.
    """
    return sorted({uc.name for repo in repos for uc in repo.docker_settings.use_cases})


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

    Runs without :func:`otto.bootstrap.bootstrap` having been called, so it's
    safe to call from the completion fast path as well as the cache writer
    on the slow path.

    Returns a sorted, de-duplicated list. Malformed files / entries are
    silently skipped — completion must never crash on bad user data.
    """
    from ..host.builtin_hosts import builtin_host_ids

    wanted = set(lab_names) if lab_names is not None else None

    # Seed with the built-in hosts otto injects into every lab (e.g. `local`) so
    # they are tab-completable in every repo, mirroring load_lab's injection.
    ids: set[str] = set(builtin_host_ids())
    resolution = resolve_process_inventory(repos)
    for repo in repos:
        # Docker-capable ids scoped to THIS repo, so the container ids
        # synthesized below pair each repo's composes with its own parents.
        docker_capable_ids: list[str] = []
        for summary in repo_host_summaries(repo, resolution):
            # Lab filter: keep only hosts tagged with a requested lab.
            if wanted is not None and wanted.isdisjoint(summary.labs):
                continue
            ids.add(summary.id)
            if summary.docker_capable:
                docker_capable_ids.append(summary.id)

        docker = getattr(repo, "docker_settings", None)
        if docker is None or not docker.composes:
            continue
        # Mirror `register_declared_container_hosts`' branch, id shape for id
        # shape: a repo declaring `[[docker.use_cases]]` registers
        # `<parent>.<usecase>.<service>` placeholders (spec §9) and takes the
        # use-case branch INSTEAD OF the legacy composes walk, never both.
        # Synthesizing `<parent>.<repo>.<service>` here regardless made
        # completion offer an id nothing registers whenever a use-case is not
        # named after its repo — the exact id the user needs never completes,
        # while `--list-hosts` shows it. (The sample repos name their
        # use-cases after themselves, which is why the divergence hid.)
        #
        # The PLACEMENT half is deliberately not mirrored: this function has
        # no Lab, so it stays pessimistic-but-stable (every docker-capable
        # host in the repo's labs) exactly as the legacy walk below does,
        # while the placeholder walk resolves one host per fragment.
        middles_for: "dict[str, set[str]]" = {}  # service -> id middle segments
        # getattr, like `docker_settings` above: this is the completion fast
        # path, which must not crash on a settings object that predates the
        # field (or on a test double that never grew it).
        use_cases = getattr(docker, "use_cases", ()) or ()
        if use_cases:
            by_handle = {c.name: c for c in docker.composes if c.name}
            for uc in use_cases:
                for handle in uc.composes:
                    compose = by_handle.get(handle)
                    # An unresolvable handle is settings the schema would
                    # reject; completion never crashes on bad data, it just
                    # offers nothing for it.
                    if compose is None:
                        continue
                    for service in compose.services:
                        middles_for.setdefault(service, set()).add(uc.name)
        else:
            for compose in docker.composes:
                for service in compose.services:
                    middles_for.setdefault(service, set()).add(repo.name)

        # No per-compose placement any more (spec §14): enumerate every
        # docker-capable host in this repo's labs (pessimistic but stable;
        # the actual bring-up picks one via the use-case machinery).
        for parent in docker_capable_ids:
            for service, middles in middles_for.items():
                for middle in middles:
                    ids.add(f"{parent}.{middle}.{service}".lower())

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
    return sorted(
        link_id
        for link_id, hosts in _declared_links(repos)
        if loaded_ids is None or any(h in loaded_ids for h in hosts)
    )


def _declared_links(repos: list["Repo"]) -> list[tuple[str, list[str]]]:
    """``(link id, endpoint host ids)`` per declared static link, first-seen order.

    One entry per id: an id declared twice (two sources, two entries) keeps the
    endpoint hosts of EVERY declaration, so a reader's "any endpoint in the lab"
    test equals the per-entry filter (an id passes if any declaration passes).
    """
    from ..link.derive import raw_endpoint_host_ids
    from ..link.model import LinkEndpoint, make_static_link_id

    out: dict[str, list[str]] = {}
    for repo in repos:
        for src in repo.lab_sources:
            for lab_file in src.lab_files():
                for entry in _read_lab_links(lab_file):
                    if not isinstance(entry, dict):
                        continue
                    hosts = [h for h in raw_endpoint_host_ids(entry) if isinstance(h, str) and h]
                    name = entry.get("name")
                    if isinstance(name, str) and name:
                        link_id = name
                    else:
                        raw = raw_endpoint_host_ids(entry)
                        if len(raw) != 2 or not all(h for h in raw):  # noqa: PLR2004
                            continue
                        a, b = (LinkEndpoint(host=h) for h in raw)
                        link_id = make_static_link_id(a, b, None)
                    bucket = out.setdefault(link_id, [])
                    bucket.extend(h for h in hosts if h not in bucket)
    return list(out.items())


def collect_links(repos: list["Repo"]) -> list[dict[str, Any]]:
    """Return declared static links with their endpoint host ids, for lab-scoped cache completion.

    The same enumeration :func:`collect_link_ids` filters live; storing the
    endpoints lets a reader apply that filter (offer a link when ANY endpoint
    host is in the selected lab's host set) without reading a lab file.
    """
    # `_declared_links` merges the endpoints of an id declared twice, so the reader's
    # "any endpoint in the selected lab" test equals collect_link_ids' per-entry filter.
    return [{"id": link_id, "hosts": hosts} for link_id, hosts in sorted(_declared_links(repos))]


def collect_lab_names(repos: list["Repo"]) -> list[str]:
    """Enumerate every lab name each repo's host source can provide.

    A lab is a *declared name* (a ``labs`` table entry, spec §2.1), not a
    directory and not a tag on hosts, so the names come from every repo's
    configured backend via the required
    :meth:`~otto.labs.protocol.LabRepository.list_labs` — not from reading
    ``lab.json``, which would leave a custom host source with no ``--lab``
    completion and, worse, empty ``hosts_by_lab`` buckets on the warm path
    while the cold path offered its hosts.

    Data-only (no host construction, no user code), so it is safe in the
    completion fast path as well as the cache writer. A backend that fails is
    skipped; any unexpected error yields ``[]`` so completion never crashes.
    """
    from ..labs import build_lab_sources

    names: set[str] = set()
    for repo in repos:
        try:
            repository = build_lab_sources([repo])
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

    builtins = set(builtin_host_ids())
    # Seed every known lab so one whose hosts all fail to enumerate still gets
    # an (empty) bucket, keeping this shape identical to the per-lab form.
    by_lab: dict[str, dict[str, "HostSummary"]] = {lab: {} for lab in collect_lab_names(repos)}
    resolution = resolve_process_inventory(repos)
    for repo in repos:
        for summary in repo_host_summaries(repo, resolution):
            if summary.id in builtins:
                continue
            for lab in summary.labs:
                by_lab.setdefault(lab, {})[summary.id] = summary

    return {lab: sorted(summaries) for lab, summaries in by_lab.items()}


def collect_host_classes_by_id(repos: list["Repo"]) -> dict[str, str]:
    """Map every enumerable host id to its registered host-class name.

    ``otto host <id> <TAB>`` scopes the verb menu to the host's class. On the
    dispatch path that class comes from building the host; completion must
    not build hosts, so the class is derived here from the summary's
    ``os_type`` through the same profile lookup the factory uses
    (``get_os_profile(os_type).base``), data-only. A host whose backend does
    not report an ``os_type``, or whose profile is not registered in THIS
    process, is omitted rather than guessed — the completer then offers the
    union menu, exactly as it did before the map existed.
    """
    from ..host.os_profile import get_os_profile

    classes: dict[str, str] = {}
    resolution = resolve_process_inventory(repos)
    for repo in repos:
        for summary in repo_host_summaries(repo, resolution):
            if summary.os_type is None:
                continue
            profile = get_os_profile(summary.os_type)
            if profile is not None:
                classes[summary.id] = profile.base  # the registered class NAME
    return dict(sorted(classes.items()))


def collect_logins_by_host(repos: list["Repo"]) -> dict[str, list[dict[str, Any]]]:
    """Map every enumerable host id to its login identities, data-only.

    ``otto host <id> <verb> --user <TAB>`` reads this map: each entry is
    ``{"login", "protocols", "proxy"}`` straight off the host's
    :class:`~otto.labs.protocol.HostSummary` — never a password. Hosts whose
    summary carries no logins are omitted, so the completer offers nothing for
    them exactly as it does for an unknown id. Sorted by login so the shim and
    the live completer agree without re-sorting.
    """
    by_host: dict[str, list[dict[str, Any]]] = {}
    resolution = resolve_process_inventory(repos)
    for repo in repos:
        for summary in repo_host_summaries(repo, resolution):
            if not summary.logins:
                continue
            by_host[summary.id] = sorted(
                (
                    {"login": ls.login, "protocols": list(ls.protocols), "proxy": bool(ls.proxy)}
                    for ls in summary.logins
                ),
                key=lambda e: e["login"],
            )
    return dict(sorted(by_host.items()))


def collect_project_names() -> list[str]:
    """Return the discovered repo names, in discovery order — what ``-I``/``-E`` complete.

    Phase 1 only (:func:`otto.bootstrap.discover`), for the reason
    ``otto.cli.main._project_completer`` gives: ``get_repos`` is phase 2 and
    runs user code. The payload and the completer read the same list so the
    warm and cold answers cannot differ.
    """
    from ..bootstrap import discover

    return [repo.name for repo in discover().repos]


# ---------------------------------------------------------------------------
# The collect child: the process that seeds and refreshes the test tables
# ---------------------------------------------------------------------------


def _acquire_collect_lock(lock: Path, *, owner: str = "") -> bool:
    """Try to take the collect child's lock (atomic ``O_EXCL`` create), writing *owner* in it.

    Returns ``False`` when another process holds a fresh lock; steals and takes
    a lock older than :data:`COLLECT_LOCK_STALE_SECONDS` (its holder died).
    With no *owner*, the lock holds this process's id.
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
        os.write(fd, (owner or str(os.getpid())).encode())
    os.close(fd)
    return True


def _release_collect_lock_if_owned(lock: Path, owner: str) -> None:
    """Free *lock* when *owner* holds it: a killed child could not free it itself."""
    with contextlib.suppress(OSError):
        if lock.read_text() == owner:
            lock.unlink()


def _collect_file(name: str) -> Path | None:
    """*name* beside the cache file (the lock, the cooldown stamp); ``None`` with no cache."""
    cache_path = _cache_path()
    return None if cache_path is None else cache_path.parent / name


def _younger_than(path: Path | None, seconds: float) -> bool:
    """Whether *path* exists and was stamped less than *seconds* ago (one ``stat``)."""
    if path is None:
        return False
    try:
        return time.time() - path.stat().st_mtime <= seconds
    except OSError:
        return False


def collect_cooldown_active() -> bool:
    """Whether a collect child failed less than :data:`COLLECT_COOLDOWN_SECONDS` ago."""
    return _younger_than(_collect_file(COLLECT_COOLDOWN_FILENAME), COLLECT_COOLDOWN_SECONDS)


def stamp_collect_cooldown(reason: str) -> None:
    """Record that a collect child failed now, and why; never raises."""
    stamp = _collect_file(COLLECT_COOLDOWN_FILENAME)
    if stamp is None:
        return
    with contextlib.suppress(OSError):
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(reason + "\n")


@dataclass(frozen=True)
class CollectChildState:
    """The collect child's lock and cooldown, as ``otto cache info`` reports them.

    A detached child's failures are silent by design (no terminal to print
    to), so these two files are the only trace it leaves.
    """

    lock_age: float | None
    """Seconds since the lock was taken; ``None`` when no child holds it."""
    cooldown_age: float | None
    """Seconds since a child last failed; ``None`` when none has."""
    cooldown_reason: str = ""
    """Why it failed, as the child stamped it."""

    @property
    def lock_is_stale(self) -> bool:
        """Whether the lock outlived any child (the next one takes it over)."""
        return self.lock_age is not None and self.lock_age > COLLECT_LOCK_STALE_SECONDS

    @property
    def cooling_down(self) -> bool:
        """Whether no child starts yet, after a failure."""
        return self.cooldown_age is not None and self.cooldown_age <= COLLECT_COOLDOWN_SECONDS


def collect_child_state() -> CollectChildState:
    """Read the collect child's lock and cooldown stamp (two ``stat`` calls, one read)."""
    now = time.time()

    def age(name: str) -> float | None:
        path = _collect_file(name)
        try:
            return None if path is None else now - path.stat().st_mtime
        except OSError:
            return None

    cooldown_age = age(COLLECT_COOLDOWN_FILENAME)
    reason = ""
    stamp = _collect_file(COLLECT_COOLDOWN_FILENAME)
    if cooldown_age is not None and stamp is not None:
        with contextlib.suppress(OSError):
            reason = stamp.read_text().strip()
    return CollectChildState(
        lock_age=age(COLLECT_LOCK_FILENAME), cooldown_age=cooldown_age, cooldown_reason=reason
    )


@dataclass(frozen=True)
class _ChildCommand:
    """How to start the collect child."""

    argv: list[str]
    env: dict[str, str]
    cwd: Path
    owner: str
    """The token the child writes into the lock it takes."""


def _collect_child_command(home: Path) -> _ChildCommand | None:
    """Return how to start the collect child, or ``None`` with no venv ``otto``.

    The *venv* ``otto`` binary (so ``entry`` runs, unlike ``python -m otto``)
    with the completion variables stripped, so the child collects instead of
    answering another completion. It starts in *home*, the workspace home
    (created here), never in the shell's cwd, whatever a repo holds: its
    ``OTTO_*`` paths are made absolute against this process's cwd first.
    """
    import sys

    from .env import _PATH_LIST_SEP, HOME_ENV_VAR, SUT_DIRS_ENV_VAR, XDIR_ENV_VAR

    otto_bin = Path(sys.executable).parent / "otto"
    if not otto_bin.exists():
        return None
    env = dict(os.environ)
    for var in (COMPLETION_ENV_VAR, "COMP_WORDS", "COMP_CWORD"):
        env.pop(var, None)
    for var in (SUT_DIRS_ENV_VAR, XDIR_ENV_VAR, HOME_ENV_VAR):
        if env.get(var):
            # os.path, not pathlib: the console-script shim (stdlib only, no
            # pathlib) builds the same environment and must agree byte for byte.
            paths = [os.path.abspath(p) for p in _PATH_LIST_SEP.split(env[var]) if p]  # noqa: PTH100
            env[var] = os.pathsep.join(paths)
    env[DUMP_TESTS_ENV_VAR] = "1"
    owner = f"{os.getpid()}-{time.monotonic_ns()}"
    env[COLLECT_OWNER_ENV_VAR] = owner
    with contextlib.suppress(OSError):  # a cwd that is not there fails the start
        home.mkdir(parents=True, exist_ok=True)
    return _ChildCommand(argv=[str(otto_bin)], env=env, cwd=home.absolute(), owner=owner)


def run_collect_child() -> bool:
    """Run the collect child and wait for it, at most the cap; ``True`` when it succeeded.

    For a cold table: the one TAB that blocks. Skipped (``False``) during the
    cooldown after a failure, while another collect child holds the lock, or
    with no cache. A timeout, a spawn failure or a non-zero exit stamps the
    cooldown. The child writes the tables and prints nothing; the caller
    reads them afterwards. Never raises: completion must never traceback into
    the shell.
    """
    import subprocess

    lock = _collect_file(COLLECT_LOCK_FILENAME)
    if lock is None or collect_cooldown_active():
        return False
    if _younger_than(lock, COLLECT_LOCK_STALE_SECONDS):
        return False
    command = _collect_child_command(lock.parent)
    if command is None:
        return False
    try:
        proc = subprocess.run(  # noqa: S603 — venv otto binary, fixed argv, no shell
            command.argv,
            env=command.env,
            cwd=command.cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=COLLECT_TIMEOUT_SECONDS + _COLLECT_KILL_GRACE_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        # The child was killed before it could stamp or unlock for itself.
        stamp_collect_cooldown("timed out")
        _release_collect_lock_if_owned(lock, command.owner)
        return False
    except (subprocess.SubprocessError, OSError) as exc:
        stamp_collect_cooldown(f"{type(exc).__name__}: {exc}")
        return False
    if proc.returncode != 0:
        stamp_collect_cooldown(f"exit {proc.returncode}")
        return False
    return True


def spawn_collect_child() -> bool:
    """Start the collect child detached, and do not wait for it; ``True`` when one started.

    The check behind a TAB whose tables are due one: the child re-reads what
    moved while the shell has its answer. It gets its own session
    (``start_new_session``), ``/dev/null`` for every stream and no inherited
    descriptors, so nothing it does reaches the shell. Skipped when a collect
    child holds a fresh lock (one ``stat``: a burst of TABs starts one child)
    and during the cooldown after a failure. Starting it touches the
    ``tests`` marker, as the console-script shim's start does, so the TABs
    of a burst ask for no other; a marker that cannot be touched starts no
    child at all. Never raises.
    """
    from .collected_tests import mark_tables_checked

    lock = _collect_file(COLLECT_LOCK_FILENAME)
    if lock is None or _younger_than(lock, COLLECT_LOCK_STALE_SECONDS):
        return False
    if collect_cooldown_active():
        return False
    command = _collect_child_command(lock.parent)
    if command is None:
        return False
    if not mark_tables_checked(lock.parent):
        # With no record of this check, every TAB past the window would start one.
        return False
    import subprocess

    try:
        child = subprocess.Popen(  # noqa: S603 — venv otto binary, fixed argv, no shell
            command.argv,
            env=command.env,
            cwd=command.cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    _detached_children[:] = [c for c in _detached_children if c.poll() is None]
    _detached_children.append(child)
    return True


_detached_children: list[Any] = []
"""Every detached collect child this process started, never waited on.

Held so that dropping the handle while the child still runs, which is the
point, is not reported as a leak: the child outlives this process.
"""


_collect_refresh_requested = False
"""Set by a completer that answered from a table with moved files; see below."""


def request_collect_refresh() -> None:
    """Ask for a detached collect child once this completion's answer is written.

    A completer returns its candidates to click, which prints them; the spawn
    waits for that (:func:`spawn_requested_refresh`, called by
    :func:`otto.cli.main.entry` after the command), so the child never delays
    the answer.
    """
    global _collect_refresh_requested  # noqa: PLW0603 — one request per process, read once at exit
    _collect_refresh_requested = True


def discard_collect_refresh_request() -> None:
    """Forget a request made before this completion began; only its own completers count."""
    global _collect_refresh_requested  # noqa: PLW0603 — see request_collect_refresh
    _collect_refresh_requested = False


def spawn_requested_refresh() -> None:
    """Start the detached collect child if a completer asked for one; clear the request."""
    global _collect_refresh_requested  # noqa: PLW0603 — see request_collect_refresh
    if not _collect_refresh_requested:
        return
    _collect_refresh_requested = False
    with contextlib.suppress(Exception):
        spawn_collect_child()


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
DYNAMIC_TUNNELS_SCHEMA_VERSION = 2
"""Bumped 1 → 2 when tunnel ids stopped keying by the whole-corpus digest in
favor of :func:`_tunnel_scope_digest` (spec §4.2): the two digests are
computed differently, so an old-schema entry's key would never match a
new-schema lookup anyway, but the bump makes that explicit rather than
relying on an accidental digest mismatch. Old entries are simply never
matched again — they expire on their own short TTL either way."""
DYNAMIC_TUNNELS_TTL_SECONDS = 120  # tunnel state is volatile; short TTL (spec §11.2)


def record_tunnel_ids(repos: list["Repo"], ids: list[str]) -> None:
    """Cache the freshly-discovered tunnel ids for ``remove <id>`` completion.

    Keyed by :func:`_tunnel_scope_digest`: tunnel ids depend on the
    workspace's lab and inventory, never on test sources, so this must not
    pay for (or invalidate on) a corpus walk.
    Skipped, like every ephemeral-inventory-guarded writer, when the digest is
    ephemeral (:func:`_fingerprint_is_ephemeral`).
    """
    if not repos or _fingerprint_is_ephemeral(repos):
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
    namespace[_tunnel_scope_digest(repos)] = {
        "schema_version": DYNAMIC_TUNNELS_SCHEMA_VERSION,
        "generated_at": int(time.time()),
        "ids": list(ids),
    }
    existing[DYNAMIC_TUNNELS_KEY] = namespace
    _atomic_write_json(cache_path, existing)


def read_tunnel_ids(repos: list["Repo"]) -> list[str] | None:
    """Fresh cached tunnel ids, or ``None`` (cold / expired / malformed).

    Keyed by :func:`_tunnel_scope_digest` — see :func:`record_tunnel_ids`.
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
    namespace = data.get(DYNAMIC_TUNNELS_KEY) if isinstance(data, dict) else None
    entry = namespace.get(_tunnel_scope_digest(repos)) if isinstance(namespace, dict) else None
    if not isinstance(entry, dict) or entry.get("schema_version") != DYNAMIC_TUNNELS_SCHEMA_VERSION:
        return None
    generated_at = entry.get("generated_at")
    if not isinstance(generated_at, (int, float)):
        return None
    if time.time() - generated_at > DYNAMIC_TUNNELS_TTL_SECONDS:
        return None
    ids = entry.get("ids")
    return ids if isinstance(ids, list) else None


# --- `otto cache info`: the outlet's read side -------------------------------


@dataclass(frozen=True)
class SectionStatus:
    """How one cache section stands for THIS workspace — what ``otto cache info`` reports.

    ``state`` is one of ``fresh`` (the fast path serves it), ``stale`` (a key
    file changed since it was written), ``expired`` (older than the TTL),
    ``tainted`` (written while bootstrap reported errors — never served),
    ``outdated`` (an older schema), ``unreadable`` (a corrupt file or entry)
    or ``missing``. ``payload`` is the stored payload whenever the entry has
    one, servable or not: a stale entry still says what the LAST enumeration
    offered and dropped, which is usually the answer being looked for.
    """

    state: str
    generated_at: float | None = None
    ttl: int = CACHE_TTL_SECONDS
    payload: dict[str, Any] | None = None


def inspect_section(repos: list["Repo"], name: str) -> SectionStatus:
    """Return section *name*'s standing for *repos*, judged as :func:`read_sections` judges it.

    Same file, same schema check, same order of tests — taint, then TTL, then
    digest — but a verdict with a reason instead of ``None``, and the payload
    regardless of the verdict. ``KeyError`` for an unregistered *name*.
    """
    from .cache_sections import section_by_name, section_digests

    section = section_by_name(name)
    cache_path = _cache_path()
    if not repos or cache_path is None or not cache_path.is_file():
        return SectionStatus(state="missing")
    try:
        data = json.loads(cache_path.read_text())
    except (OSError, json.JSONDecodeError):
        return SectionStatus(state="unreadable")
    if not isinstance(data, dict) or data.get("schema") != SCHEMA_VERSION:
        return SectionStatus(state="outdated")
    stored = data.get("sections")
    entry = stored.get(name) if isinstance(stored, dict) else None
    if not isinstance(entry, dict):
        return SectionStatus(state="missing")
    raw_payload = entry.get("payload")
    payload = raw_payload if isinstance(raw_payload, dict) else None
    raw_generated = entry.get("generated_at")
    generated_at = float(raw_generated) if isinstance(raw_generated, (int, float)) else None
    ttl = _cache_ttl_seconds(repos)
    if entry.get("tainted"):
        state = "tainted"
    elif generated_at is None or time.time() - generated_at > ttl:
        state = "expired"
    elif entry.get("fingerprint") != section_digests(repos, [section])[name]:
        state = "stale"
    elif payload is None:
        state = "unreadable"
    else:
        state = "fresh"
    return SectionStatus(state=state, generated_at=generated_at, ttl=ttl, payload=payload)


@dataclass(frozen=True)
class InventoryDescription:
    """The process inventory as ``otto cache info`` reports it.

    ``blocker`` is ``None`` when an entry can be keyed on this inventory;
    otherwise the clause saying why nothing is written for the workspace —
    ``"is broken"`` for a declaration that failed to build, ``"cannot report
    freshness"`` for a backend whose ``fingerprint()`` is ``None`` (both make
    the digest ephemeral, R18) — so the section's standing can be worded
    truthfully beside it instead of promising a rebuild that cannot happen.
    """

    text: str
    blocker: str | None = None


def describe_inventory(repos: list["Repo"]) -> InventoryDescription:
    """Return the process inventory for ``otto cache info``, resolved as completion resolves it.

    :func:`resolve_process_inventory` without the warning — this line IS the
    report — then classed the way the digest classes it: a broken declaration
    and a backend that cannot report freshness are both named, because each
    is why nothing is ever written for the workspace and every TAB reloads.
    """
    from ..inventory import build_inventory

    try:
        inventory = build_inventory(list(repos))
        if inventory is None:
            return InventoryDescription(text="none declared")
        label = getattr(inventory, "label", type(inventory).__name__)
        fingerprint = inventory.fingerprint()
    except Exception as e:  # noqa: BLE001 — a broken declaration is the finding, not a crash
        return InventoryDescription(
            text=f"BROKEN — {type(e).__name__}: {e} (no host completes until it is fixed)",
            blocker="is broken",
        )
    if fingerprint is None:
        return InventoryDescription(
            text=f"{label} — cannot report freshness, so completion never caches",
            blocker="cannot report freshness",
        )
    return InventoryDescription(text=label)
