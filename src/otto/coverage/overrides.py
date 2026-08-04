"""``.otto/coverage-overrides.toml`` — manual-testing coverage overrides.

Two capabilities, one commented TOML file (design spec 2026-07-30): asserted
manual coverage (top-level tables named after ``kind="manual"`` tiers) and
break-glass ticket reattribution (the reserved ``[[reattribute]]`` table).
This module loads and validates the file — every rule loud at load, never
rendered around — and applies asserted entries to a store.

Entry ids are assigned in table first-appearance order, entries within a
table in file order (the TOML parser groups array-of-tables by key, so
cross-table document order is not observable).
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import tomli

from ..utils import anchor_path
from .attribution import NO_TICKET, UNCOMMITTED_TICKET
from .capture import gitio
from .store.model import CoverageStore, OverrideRecord
from .tiers import TierConfig

logger = logging.getLogger(__name__)


class OverrideConfigError(ValueError):
    """The override file (or its settings key) is malformed — raised loud."""


DEFAULT_OVERRIDES_RELPATH = Path(".otto") / "coverage-overrides.toml"

_RESERVED_TABLE = "reattribute"
_RESERVED_IDS = frozenset({NO_TICKET, UNCOMMITTED_TICKET})
_ASSERTED_KEYS = frozenset({"ticket", "commit", "as_of", "reason"})
_REATTRIBUTE_KEYS = frozenset({"commit", "tickets", "reason"})


@dataclass(frozen=True)
class AssertedEntry:
    """One "this was manually tested" declaration, resolved to full shas."""

    id: int
    tier: str
    reason: str
    ticket: str | None = None
    commit: str | None = None
    as_of: str | None = None

    @property
    def key(self) -> str:
        """The stable display key — ``ticket:<id>`` or ``commit:<full sha>``.

        (Deliberately worded without a leading ``name: description`` colon:
        Napoleon's attribute-docstring parsing reads text before a first
        colon as a type annotation, which turned this summary into a bogus
        ``:type:`` field and an unresolvable ``py:class`` nitpicky warning.)
        """
        if self.ticket is not None:
            return f"ticket:{self.ticket}"
        return f"commit:{self.commit}"


@dataclass(frozen=True)
class OverrideConfig:
    """The parsed, validated override file."""

    path: Path
    asserted: list[AssertedEntry]
    reattributions: dict[str, list[str]]


def _resolve_sha(sut_dir: Path, rev: str, *, where: str) -> str:
    try:
        return gitio.rev_parse_commit(sut_dir, rev)
    except gitio.GitUnavailableError as exc:
        raise OverrideConfigError(f"{where}: cannot resolve {rev!r} to a commit: {exc}") from exc


def _load_asserted_entry(
    entry: dict[str, Any], tier: str, entry_id: int, sut_dir: Path, where: str
) -> AssertedEntry:
    unknown = set(entry) - _ASSERTED_KEYS
    if unknown:
        raise OverrideConfigError(f"{where}: unknown key(s) {sorted(unknown)}")

    has_ticket = "ticket" in entry
    has_commit = "commit" in entry
    if has_ticket == has_commit:
        raise OverrideConfigError(f"{where}: exactly one of 'ticket'/'commit' is required")

    reason = entry.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise OverrideConfigError(f"{where}: 'reason' must be a non-empty string")

    if has_ticket:
        ticket = entry["ticket"]
        if not isinstance(ticket, str) or not ticket:
            raise OverrideConfigError(f"{where}: 'ticket' must be a non-empty string")
        if ticket in _RESERVED_IDS:
            raise OverrideConfigError(f"{where}: ticket id {ticket!r} is reserved")
        if "as_of" not in entry:
            raise OverrideConfigError(f"{where}: 'ticket' requires 'as_of'")
        as_of = _resolve_sha(sut_dir, entry["as_of"], where=where)
        return AssertedEntry(id=entry_id, tier=tier, reason=reason, ticket=ticket, as_of=as_of)

    if "as_of" in entry:
        raise OverrideConfigError(f"{where}: 'as_of' is not allowed with 'commit'")
    commit = _resolve_sha(sut_dir, entry["commit"], where=where)
    return AssertedEntry(id=entry_id, tier=tier, reason=reason, commit=commit)


def _load_reattribute_entry(
    entry: dict[str, Any], sut_dir: Path, where: str
) -> tuple[str, list[str]]:
    unknown = set(entry) - _REATTRIBUTE_KEYS
    if unknown:
        raise OverrideConfigError(f"{where}: unknown key(s) {sorted(unknown)}")
    missing = _REATTRIBUTE_KEYS - set(entry)
    if missing:
        raise OverrideConfigError(f"{where}: missing required key(s) {sorted(missing)}")

    reason = entry["reason"]
    if not isinstance(reason, str) or not reason.strip():
        raise OverrideConfigError(f"{where}: 'reason' must be a non-empty string")

    raw_tickets = entry["tickets"]
    if not isinstance(raw_tickets, list):
        raise OverrideConfigError(f"{where}: 'tickets' must be a list of non-empty strings")
    tickets: list[str] = []
    for t in raw_tickets:
        if not isinstance(t, str) or not t:
            raise OverrideConfigError(f"{where}: 'tickets' must be a list of non-empty strings")
        tickets.append(t)
    reserved = [t for t in tickets if t in _RESERVED_IDS]
    if reserved:
        raise OverrideConfigError(f"{where}: ticket id(s) {reserved} are reserved")

    sha = _resolve_sha(sut_dir, entry["commit"], where=where)
    return sha, tickets


def load_override_config(
    cov_config: dict[str, Any], sut_dir: Path, tiers: list[TierConfig]
) -> OverrideConfig | None:
    """Load and validate the override file, or None when the feature is off.

    Raises:
        OverrideConfigError: any spec §2 rule violated. The absent-default
            case (no ``[coverage.overrides]`` key, no file at the default
            path) is the only silent path — an *explicitly configured* path
            that does not exist is an error, not a no-op.
    """
    raw_key = (cov_config.get("overrides") or {}).get("file")
    path = anchor_path(Path(raw_key), sut_dir) if raw_key else sut_dir / DEFAULT_OVERRIDES_RELPATH
    if not path.is_file():
        if raw_key:
            raise OverrideConfigError(f"[coverage.overrides] file does not exist: {path}")
        return None
    if not cov_config.get("tickets"):
        raise OverrideConfigError(
            f"{path.name}: an override file requires [coverage.tickets] to be "
            "configured — both asserted coverage and reattribution operate on "
            "the ticket-attribution walk"
        )
    manual_tiers = {t.name for t in tiers if t.kind == "manual"}
    if _RESERVED_TABLE in manual_tiers:
        raise OverrideConfigError(
            f"a manual tier may not be named {_RESERVED_TABLE!r} (reserved table name)"
        )
    try:
        # Binary read (F5, final review): TOML mandates UTF-8 regardless of
        # locale (https://toml.io/en/v1.0.0#spec — "TOML is designed to map
        # unambiguously to a hash table... must be valid UTF-8"), but
        # `Path.read_text()` decodes with the platform's locale-default
        # encoding when none is given — on a C/POSIX locale that is often
        # ASCII, which would raise (or worse, mis-decode) on a non-ASCII
        # `reason` string a reviewer wrote in the file. `tomli.load` reads
        # bytes and decodes UTF-8 itself, so this always wins regardless of
        # the process locale.
        with path.open("rb") as f:
            doc = tomli.load(f)
    except tomli.TOMLDecodeError as exc:
        raise OverrideConfigError(f"{path}: not valid TOML: {exc}") from exc

    asserted: list[AssertedEntry] = []
    reattributions: dict[str, list[str]] = {}
    next_id = 0
    for table, entries in doc.items():
        if table != _RESERVED_TABLE and table not in manual_tiers:
            raise OverrideConfigError(
                f"{path.name}: unknown table {table!r} — top-level tables must be "
                f"'{_RESERVED_TABLE}' or a declared kind=\"manual\" tier "
                f"(have: {sorted(manual_tiers)})"
            )
        if not isinstance(entries, list):
            raise OverrideConfigError(
                f"{path.name}: [{table}] must be an array of tables ([[{table}]])"
            )
        for i, raw_entry in enumerate(entries):
            where = f"{path.name}: [[{table}]] entry {i + 1}"
            if not isinstance(raw_entry, dict):
                raise OverrideConfigError(f"{where}: must be a table")
            entry = cast("dict[str, Any]", raw_entry)
            if table == _RESERVED_TABLE:
                sha, ids = _load_reattribute_entry(entry, sut_dir, where)
                if sha in reattributions:
                    raise OverrideConfigError(f"{where}: duplicate reattribution for {sha}")
                reattributions[sha] = ids
            else:
                asserted.append(_load_asserted_entry(entry, table, next_id, sut_dir, where))
                next_id += 1
    return OverrideConfig(path=path, asserted=asserted, reattributions=reattributions)


def _entry_shas(
    entry: AssertedEntry, ticket_commits: dict[str, list[str]], fp_index: dict[str, int]
) -> set[str]:
    """Return the commit shas whose lines *entry* covers (spec §3)."""
    if entry.commit is not None:
        return {entry.commit}
    assert entry.as_of is not None  # noqa: S101 — loader invariant: ticket entries carry as_of
    bound = fp_index.get(entry.as_of)
    if bound is None:
        raise OverrideConfigError(
            f"override {entry.key}: as_of {entry.as_of} is not in the first-parent history of HEAD"
        )
    shas = {s for s in ticket_commits.get(entry.ticket or "", []) if fp_index.get(s, -1) >= bound}
    if not shas:
        raise OverrideConfigError(
            f"override {entry.key}: ticket never appears in a commit at/before "
            f"as_of {entry.as_of} — a typo'd id, or the wrong as_of"
        )
    return shas


def apply_asserted_entries(
    store: CoverageStore,
    entries: list[AssertedEntry],
    *,
    repo_root: Path,
    per_line_sha: dict[str, dict[int, str]],
    ticket_commits: dict[str, list[str]],
    fp_index: dict[str, int],
    path: Path,
) -> None:
    """Fold asserted entries into *store*: hits + provenance + prune signals.

    Snapshot-then-apply: every entry's line set and every involved line's
    pre-existing hit state are computed before any mutation, so one entry's
    added hit can never make a later entry read "already covered", and two
    entries asserting the same line both get a provenance ref while the hit
    counter moves exactly once.
    """
    if not entries:
        return
    resolved_root = repo_root.resolve()
    records = {
        rec.path.relative_to(resolved_root).as_posix(): rec
        for rec in store.files()
        if rec.path.is_relative_to(resolved_root)
    }
    # Pass 1: resolve line sets against the immutable attribution products.
    lines_of: dict[int, list[tuple[str, int]]] = {}
    for entry in entries:
        shas = _entry_shas(entry, ticket_commits, fp_index)
        lines_of[entry.id] = [
            (relpath, lineno)
            for relpath, per_line in per_line_sha.items()
            if relpath in records
            for lineno, sha in per_line.items()
            if sha in shas and lineno in records[relpath].lines
        ]
    # Snapshot real hit state before mutating anything.
    already_hit = {
        (relpath, lineno, entry.tier): records[relpath].lines[lineno].hits.is_hit(entry.tier)
        for entry in entries
        for relpath, lineno in lines_of[entry.id]
    }
    # Pass 2: apply, and log the prune signal for inert entries.
    for entry in entries:
        store.register_tier(entry.tier)
        marked = 0
        for relpath, lineno in lines_of[entry.id]:
            if already_hit[(relpath, lineno, entry.tier)]:
                continue
            line = records[relpath].lines[lineno]
            refs = line.asserted.setdefault(entry.tier, [])
            if not refs:
                line.hits.add(entry.tier, 1)
            refs.append(entry.id)
            marked += 1
        if not lines_of[entry.id]:
            logger.info(
                "override %s (tier %r) is fully aged out — no current line is "
                "attributed to it; prune it from %s (reason: %s)",
                entry.key,
                entry.tier,
                path.name,
                entry.reason,
            )
        elif marked == 0:
            logger.info(
                "override %s (tier %r) is fully covered by recorded runs — every "
                "line is proven; prune it from %s (reason: %s)",
                entry.key,
                entry.tier,
                path.name,
                entry.reason,
            )
        store.overrides.append(
            OverrideRecord(
                id=entry.id,
                tier=entry.tier,
                key=entry.key,
                reason=entry.reason,
                as_of=entry.as_of,
            )
        )
