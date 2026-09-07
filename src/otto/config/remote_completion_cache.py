"""Sidecar cache for remote-path tab completion.

Separate file from ``completion_cache.json`` because that cache is a
fingerprinted snapshot rewritten only by the slow path, while this one takes
a small write on every live remote listing.  Same directory, same
``otto cache clear`` escape hatch, same "corrupt file = empty cache"
degradation.

Two sections:

* ``listings`` — per ``(host_id, directory)`` results of a remote ``ls``,
  trusted for :data:`LISTING_TTL_SECONDS`.
* ``reservations`` — per username, one row per booking the backend reported,
  each with its ``start``/``end`` bounds (``null`` meaning unbounded).  An
  entry is trusted until ``valid_until = min(fetched_at +
  RESERVATION_TTL_SECONDS, earliest booking edge after fetched_at)`` —
  bookings churn quickly and get extended mid-session, so a crossed
  start/end boundary must force a live refresh immediately.  A booking with
  no edges ahead simply contributes none, leaving the flat TTL.

**Completion-only.** Owner ruling (2026-08-06): the ``reservations`` section
exists purely for TAB latency and only :mod:`otto.cli.remote_completion` may
read it. Command execution always queries the backend live
(:meth:`otto.reservations.check.ReservationGate.evaluate`) — stale reservation
data is acceptable for a deliberate TAB, never for a recalled command. A test
in ``tests/unit/cli/test_remote_completion.py`` enforces the import boundary.

Every read and store takes ``now`` explicitly rather than calling
:func:`datetime.now`: the TTL and window-edge arithmetic is the whole
substance of this module, and a caller-supplied clock is what makes the
boundaries testable to the second.

``now`` must be timezone-aware, and a naive one is *refused at the door*
by every public entry point (:func:`_usable_clock`): reads report a miss
and stores no-op, so the cache switches itself off rather than answering
from arithmetic it cannot do.  Guarding only the stored side would not
be enough — the dangerous case is a naive ``now`` meeting a cache some
ordinary aware caller wrote, where every comparison mixes the two and
raises ``TypeError`` in the middle of a TAB press.
"""

import contextlib
import json
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..reservations.protocol import Reservation

LISTING_TTL_SECONDS = 45
RESERVATION_TTL_SECONDS = 120
MAX_DIRS_PER_HOST = 50
SCHEMA_VERSION = 2
REMOTE_CACHE_FILENAME = "remote_completion_cache.json"


@dataclass(frozen=True)
class ListingEntry:
    """One name in a remote directory listing."""

    name: str
    is_dir: bool


def _path() -> "Path | None":
    """Sidecar path beside the main completion cache, or None when caching is off.

    Derived from :func:`otto.config.completion_cache._cache_path` rather than
    re-deriving ``$OTTO_XDIR/.otto`` here, so the "no xdir means no caching"
    rule stays in one place.
    """
    from .completion_cache import _cache_path

    main = _cache_path()
    if main is None:
        return None
    return main.with_name(REMOTE_CACHE_FILENAME)


def _empty() -> dict[str, Any]:
    return {"schema": SCHEMA_VERSION, "reservations": {}, "listings": {}}


def _load() -> dict[str, Any]:
    """Read the sidecar; a missing, unreadable, corrupt or foreign-schema file reads empty."""
    path = _path()
    if path is None or not path.is_file():
        return _empty()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return _empty()
    if not isinstance(data, dict) or data.get("schema") != SCHEMA_VERSION:
        return _empty()
    for section in ("reservations", "listings"):
        if not isinstance(data.get(section), dict):
            data[section] = {}
    return data


def _save(data: dict[str, Any], now: datetime) -> None:
    """Prune and write *data* atomically; any OSError is swallowed.

    The cache is an optimization — a read-only ``.otto`` directory or a full
    disk must cost completion its speed, never its correctness.
    """
    path = _path()
    if path is None:
        return
    _prune(data, now)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=path.parent,
            delete=False,
            prefix=".remote_completion_cache_",
            suffix=".tmp",
        ) as tmp:
            tmp_name = tmp.name
            json.dump(data, tmp)
        try:
            Path(tmp_name).replace(path)
        except OSError:
            with contextlib.suppress(OSError):
                Path(tmp_name).unlink()
            raise
    except OSError:
        pass  # cache is best-effort; completion still works without it


def _prune(data: dict[str, Any], now: datetime) -> None:
    """Drop expired entries and cap listings per host (oldest evicted).

    Oldest is decided by the stored ``fetched_at`` string: ISO-8601 UTC
    timestamps sort lexicographically in chronological order, so no reparse is
    needed for the ordering.
    """
    listings = data["listings"]
    for host_id in list(listings):
        dirs = listings[host_id]
        fresh: dict[str, Any] = {}
        for directory, entry in dirs.items():
            fetched = _parse(entry.get("fetched_at"))
            if fetched is not None and now - fetched < timedelta(seconds=LISTING_TTL_SECONDS):
                fresh[directory] = entry
        if len(fresh) > MAX_DIRS_PER_HOST:
            by_age = sorted(fresh, key=lambda d: fresh[d]["fetched_at"])
            for d in by_age[: len(fresh) - MAX_DIRS_PER_HOST]:
                del fresh[d]
        if fresh:
            listings[host_id] = fresh
        else:
            del listings[host_id]
    reservations = data["reservations"]
    for user in list(reservations):
        until = _parse(reservations[user].get("valid_until"))
        if until is None or now >= until:
            del reservations[user]


def _usable_clock(now: datetime) -> bool:
    """Whether *now* can be compared against stored timestamps at all.

    A naive clock cannot: the stored side is aware (:func:`_parse` keeps it
    that way), so every subtraction and comparison would mix the two and raise
    ``TypeError`` — a traceback in the user's terminal mid-TAB, out of a cache
    that exists purely as an optimization. Refusing at the entry point turns
    that into a miss, which every caller already handles.

    Refused rather than coerced: assuming UTC for an unanchored clock would
    invent an offset and could serve a listing hours past its TTL.
    """
    return now.tzinfo is not None


def _parse(raw: object) -> "datetime | None":
    """Parse a stored ISO-8601 timestamp; None for anything that isn't an AWARE one.

    Naive is rejected rather than assumed-UTC because every comparison in this
    module is against the caller's ``now``. ``Reservation`` requires aware
    bounds when it has them, but a third-party backend that hands back a bare
    ``datetime.now()`` would otherwise raise ``TypeError`` from a subtraction
    two frames down — a traceback in the user's terminal mid-TAB, for a cache
    that is supposed to be pure optimization. Reading it as "no usable
    timestamp" degrades to a miss instead, which is always safe.
    """
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def cached_listing(host_id: str, directory: str, now: datetime) -> "list[ListingEntry] | None":
    """Return the cached listing for ``(host_id, directory)``, or None when absent/stale.

    A naive *now* reports a miss — see :func:`_usable_clock`.
    """
    if not _usable_clock(now):
        return None
    entry = _load()["listings"].get(host_id, {}).get(directory)
    if not isinstance(entry, dict):
        return None
    fetched = _parse(entry.get("fetched_at"))
    if fetched is None or now - fetched >= timedelta(seconds=LISTING_TTL_SECONDS):
        return None
    raw = entry.get("entries")
    if not isinstance(raw, list):
        return None
    return [
        ListingEntry(name=e["name"], is_dir=e["is_dir"])
        for e in raw
        if isinstance(e, dict)
        and isinstance(e.get("name"), str)
        and isinstance(e.get("is_dir"), bool)
    ]


def store_listing(
    host_id: str, directory: str, entries: "list[ListingEntry]", now: datetime
) -> None:
    """Record a live listing of *directory* on *host_id*, fetched at *now*.

    A naive *now* stores nothing — see :func:`_usable_clock`.
    """
    if not _usable_clock(now):
        return
    data = _load()
    data["listings"].setdefault(host_id, {})[directory] = {
        "fetched_at": now.isoformat(),
        "entries": [{"name": e.name, "is_dir": e.is_dir} for e in entries],
    }
    _save(data, now)


def _valid_until(fetched_at: datetime, edges: "list[datetime | None]") -> datetime:
    """Clamp the flat reservation TTL to the earliest booking edge still ahead.

    ``None`` edges are dropped first, before anything reads ``tzinfo``: a
    :class:`~otto.reservations.protocol.Reservation` bound may legitimately be
    absent (unknown start, open-ended end), and an absent edge is one that
    never arrives, so it can never clamp the TTL.

    Naive edges are dropped for the reason given on :func:`_parse`: comparing
    one against an aware *fetched_at* raises, and a backend that reports them
    should degrade to the flat TTL — precisely the fallback already defined
    for backends that report no edges at all — not crash a TAB. *fetched_at*
    itself is always aware here; :func:`_usable_clock` turned the callers back
    at the door.
    """
    block = fetched_at + timedelta(seconds=RESERVATION_TTL_SECONDS)
    future = [e for e in edges if e is not None and e.tzinfo is not None and e > fetched_at]
    return min([block, *future])


def store_reservations(username: str, reservations: "list[Reservation]", now: datetime) -> None:
    """Record *username*'s reservations, valid until the first edge or the TTL.

    ``None`` bounds are stored as JSON ``null`` and mean unbounded: a booking
    with no ``end`` never expires, and so contributes no edge to clamp the TTL
    against.  Serializing such a bound as a sentinel date instead would put a
    fabricated instant on disk that the reader could not tell from a real one.

    A naive *now* stores nothing — see :func:`_usable_clock`.

    Parameters
    ----------
    username : str
        The identity the backend was queried for.
    reservations : list[Reservation]
        The rows that backend reported, active right now.
    now : datetime
        The timezone-aware instant the query was made at.
    """
    if not _usable_clock(now):
        return
    edges: list[datetime | None] = [t for r in reservations for t in (r.start, r.end)]
    data = _load()
    data["reservations"][username] = {
        "fetched_at": now.isoformat(),
        "valid_until": _valid_until(now, edges).isoformat(),
        "windows": [
            {
                "resource": r.resource,
                "start": r.start.isoformat() if r.start is not None else None,
                "end": r.end.isoformat() if r.end is not None else None,
            }
            for r in reservations
        ],
    }
    _save(data, now)


def cached_reservation_ok(username: str, required: "set[str]", now: datetime) -> "bool | None":
    """Answer the gate from cache: True/False when a valid entry decides it, None when stale.

    ``None`` (not ``False``) past ``valid_until`` is load-bearing: a crossed
    window edge must trigger a live refresh, never a cached refusal.

    A stored bound of ``null`` means unbounded and the row is active on that
    side.  A bound that is present but unparseable means the opposite — we
    cannot tell — and that row is skipped rather than counted, so corrupt
    cache data narrows the answer instead of widening it.

    "Active" itself is decided by
    :meth:`otto.reservations.protocol.Reservation.is_active`, the one home for
    that rule, by rebuilding a :class:`~otto.reservations.protocol.Reservation`
    from each stored row: the live gate in :mod:`otto.cli.remote_completion`
    asks the same object the same question, so a warm cache and a cold one
    cannot answer a TAB differently.  The import is function-scope because
    :mod:`otto.reservations` is a heavy package and this module sits under
    :mod:`otto.cli.remote_completion`'s module-level import, which is on a
    budgeted completion surface; the sole caller has already imported
    ``otto.reservations`` by the time it gets here, so this costs nothing.

    A naive *now* reports a miss — see :func:`_usable_clock`.
    """
    if not _usable_clock(now):
        return None
    entry = _load()["reservations"].get(username)
    if not isinstance(entry, dict):
        return None
    until = _parse(entry.get("valid_until"))
    if until is None or now >= until:
        return None
    if not isinstance(entry.get("windows"), list):
        return None
    from ..reservations.protocol import Reservation

    active: set[str] = set()
    for w in entry["windows"]:
        if not isinstance(w, dict):
            continue
        raw_start, raw_end = w.get("start"), w.get("end")
        start = _parse(raw_start) if raw_start is not None else None
        end = _parse(raw_end) if raw_end is not None else None
        # A stored bound that fails to parse is not the same as an absent one:
        # absent means unbounded, unparseable means we cannot tell, so the row
        # is skipped rather than read as always-active.
        if raw_start is not None and start is None:
            continue
        if raw_end is not None and end is None:
            continue
        row = Reservation(user=username, resource=str(w.get("resource")), start=start, end=end)
        if row.is_active(now=now):
            active.add(row.resource)
    return required <= active


def clear_remote_cache() -> bool:
    """Delete the sidecar cache file; True when a file was removed."""
    path = _path()
    if path is None or not path.is_file():
        return False
    try:
        path.unlink()
    except OSError:
        return False
    return True
