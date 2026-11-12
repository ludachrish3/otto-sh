"""Shared event-mutation semantics for every otto surface.

One home for the rules that must not fork between the live collector path,
the review-archive path, the suite's programmatic marks,
and any future CLI command: how a create resolves an omitted timestamp, how
a partial update merges onto an existing event, and when a span's ordering
is invalid. Callers translate :class:`EventValidationError` into their own
surface's failure shape (HTTP 422; a raised ValueError in library code).
"""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, fields
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Generic, Literal, Protocol, TypeVar

from ..errors import OttoError
from ..models.monitor import (
    SPAN_ORDER_ERROR,
    EventCreateBody,
    EventRecord,
    EventUpdateBody,
    checked_label,
    span_is_forward,
)
from ..utils import utc_instant

if TYPE_CHECKING:
    # Annotation only: event_ops sits below events in tach's layering.
    from .events import MonitorEvent


def utc_now() -> datetime:
    """Return the server's current time, the ONE clock every server stamp reads (#866).

    A Mark-now timestamp, a Stop stamp, a metric tick's timestamp and every
    ``observed_at`` the live channels carry
    (:func:`otto.monitor.collector.observed_now`) all read it here, so they
    can never drift apart. Callers reach it through the module
    (``event_ops.utc_now()``), so one patch fixes every stamp in a test.
    Elapsed-time loops do not read it: they measure a duration, not a stamp.

    Strictly increasing, by at least a microsecond (the wire's resolution),
    even if the wall clock steps back: the dashboard orders a mutation
    against a resync snapshot by comparing their stamps (#868), so two
    stamps must never tie or run backwards. The server is one asyncio
    thread, so call order IS execution order and no lock is needed: call it
    on the event loop's thread only (it is not thread-safe, and otto never
    mixes threads with asyncio; the review-mode ``to_thread`` writes never
    read it).
    """
    global _last_stamp  # noqa: PLW0603 — the clock's one piece of state
    now = _wall_clock()
    if _last_stamp is not None and now <= _last_stamp:
        now = _last_stamp + timedelta(microseconds=1)
    _last_stamp = now
    return now


def _wall_clock() -> datetime:
    """Read the wall clock, which :func:`utc_now` raises to strictly increasing."""
    return datetime.now(tz=timezone.utc)


_last_stamp: datetime | None = None
"""The last stamp :func:`utc_now` returned."""


class EventValidationError(OttoError, ValueError):
    """A semantically invalid event mutation (e.g. span end not after start)."""


class EventChanged(OttoError):  # noqa: N818 — name fixed by the #929 spec: a state mismatch, not a caller error
    """A write found the event in a state other than the one it expected.

    Raised for a revert and for an edit, ``/end`` or delete whose row changed
    between its read and its write. ``current`` is the event as it actually
    stands (``None`` when no event has the id), so the dashboard can show it
    and drop an undo step that no longer applies.
    """

    def __init__(self, current: EventRecord | None) -> None:
        super().__init__("the event was changed elsewhere")
        self.current = current


def same_event(a: EventRecord | None, b: EventRecord | None) -> bool:
    """Report whether two event states are the same: the ONE "is this the expected state" test.

    Two ``None`` are the same (both absent); ``None`` and a record are not.
    Timestamps compare as UTC instants (:func:`otto.utils.utc_instant`),
    never as text or wall time: the wire's ``Z``, the store's ``+00:00`` and
    the browser's ``.000Z`` all name one instant, and the two passes of a DST
    fold name two. ``color`` compares after ``EventRecord``'s lenient
    normalisation (``#ABC`` is ``#aabbcc``; a legacy ``"red"`` is itself).
    ``id`` is ignored: the caller has already chosen the event by id.
    """
    if a is None or b is None:
        return a is b
    return (
        utc_instant(a.timestamp) == utc_instant(b.timestamp)
        and utc_instant(a.end_timestamp) == utc_instant(b.end_timestamp)
        and a.label == b.label
        and a.color == b.color
        and a.dash == b.dash
        and a.source == b.source
    )


def require_expected(current: EventRecord | None, expect: EventRecord | None) -> None:
    """Raise :class:`EventChanged` carrying ``current`` unless it is the ``expect`` state.

    The ONE "the event is still what the caller saw" refusal: every backend's
    revert and guarded edit calls it with the event as it stands.
    """
    if not same_event(current, expect):
        raise EventChanged(current)


def revert_leg(
    expect: EventRecord | None, write: EventRecord | None
) -> Literal["update", "delete", "restore"]:
    """Select a revert's leg from which of its two states are present.

    ``expect`` and ``write`` both present put the event back to ``write``
    (``"update"``); only ``expect`` deletes it (``"delete"``); only ``write``
    restores it under its id (``"restore"``). Neither is not a change.
    """
    if expect is None and write is None:
        raise EventValidationError("a revert needs expect, write, or both")
    if expect is None:
        return "restore"
    if write is None:
        return "delete"
    return "update"


FIRST_EVENT_ID = 1
"""The lowest id an event can hold: both backends count from 1 (the
collector's counter, SQLite's AUTOINCREMENT)."""


def id_handed_out(event_id: int, high_water: int) -> bool:
    """Report whether ``event_id`` was ever handed out: the ONE restore-id rule.

    ``high_water`` is the highest id the backend has handed out (``0`` when
    none). A restore may only reuse an id in ``FIRST_EVENT_ID..high_water``:
    one above would collide with a later create, and one below the first was
    never anybody's. The live collector and the review archive both call it.
    """
    return FIRST_EVENT_ID <= event_id <= high_water


MAX_SAFE_EVENT_ID = 2**53 - 1
"""The highest id a browser holds exactly (``Number.MAX_SAFE_INTEGER``): the
validator of an imported document and the id counter both read it."""


class IdsExhausted(EventValidationError):  # noqa: N818 — name fixed by the #958 spec: a state, not a caller error
    """Every id up to :data:`MAX_SAFE_EVENT_ID` has been handed out."""


class EventIdCounter:
    """The ONE event-id allocation rule: the next id stays above every id seen.

    The live store, and every review source that creates events, own one.
    """

    def __init__(self) -> None:
        self._next = FIRST_EVENT_ID

    @property
    def high_water(self) -> int:
        """The highest id handed out or noted (``0`` when none)."""
        return self._next - 1

    def note(self, event_id: int) -> None:
        """Keep the next id above ``event_id``; never lower it."""
        self._next = max(self._next, event_id + 1)

    def take(self) -> int:
        """Return the next id and advance, or raise :class:`IdsExhausted` past the ceiling."""
        if self._next > MAX_SAFE_EVENT_ID:
            raise IdsExhausted(
                f"no event id is left: ids stop at {MAX_SAFE_EVENT_ID}, the browser's limit"
            )
        taken = self._next
        self._next += 1
        return taken

    def allocated(self, event_id: int) -> bool:
        """Report whether ``event_id`` was handed out or noted (:func:`id_handed_out`)."""
        return id_handed_out(event_id, high_water=self.high_water)


def check_span(timestamp: datetime, end_timestamp: datetime | None) -> None:
    """Refuse a span that does not end after it starts, as :class:`EventValidationError`.

    Called only by :func:`check_event_write`, the one write rule every
    stored pair reaches. The rule itself is
    ``otto.models.monitor.span_is_forward``, which the create body's own
    validator shares; it compares the two as UTC instants
    (:func:`otto.utils.utc_instant`).
    """
    if not span_is_forward(timestamp, end_timestamp):
        raise EventValidationError(SPAN_ORDER_ERROR)


class _Stored(Protocol):
    """An event as stored (``MonitorEvent``, ``EventRecord``): what the write rule reads."""

    @property
    def label(self) -> str: ...

    @property
    def timestamp(self) -> datetime: ...

    @property
    def end_timestamp(self) -> datetime | None: ...


def check_event_write(
    existing: _Stored | None,
    *,
    label: str,
    timestamp: datetime,
    end_timestamp: datetime | None,
) -> None:
    """Refuse an event write a create would refuse, as :class:`EventValidationError`.

    The ONE write rule: every library, archive, HTTP and revert write asks it
    on the values it is about to store (aware UTC: after the write's own
    :func:`otto.utils.as_utc`). ``existing`` is the event as stored, or
    ``None`` when the write creates or restores one.

    * The label must not be blank (``otto.models.monitor.checked_label``),
      unless it equals ``existing``'s: put back as it is, as a PATCH that
      omits it would leave it (an older archive may hold a blank one).
    * The span must end after it starts (:func:`check_span`), unless
      ``existing`` holds this very pair (a legacy backward span can still be
      recoloured or relabelled). A create, a restore, or any write that
      changes the pair must be forward, so undoing the delete of a legacy
      backward event is refused.
    """
    if existing is None or label != existing.label:
        try:
            checked_label(label)
        except ValueError as err:
            raise EventValidationError(str(err)) from err
    if (
        existing is None
        or utc_instant(timestamp) != utc_instant(existing.timestamp)
        or utc_instant(end_timestamp) != utc_instant(existing.end_timestamp)
    ):
        check_span(timestamp, end_timestamp)


def check_revert_write(expect: EventRecord | None, write: EventRecord | None) -> None:
    """Refuse a revert's ``write`` that a create or an update would refuse.

    The revert route's pre-validation: :func:`check_event_write` against
    ``expect`` (``None`` on a restore, so label and span are both new).
    """
    if write is None:
        return
    check_event_write(
        expect, label=write.label, timestamp=write.timestamp, end_timestamp=write.end_timestamp
    )


_E = TypeVar("_E", bound=_Stored)
_R = TypeVar("_R")
_P = TypeVar("_P")


async def run_locked_in_phases(
    lock: asyncio.Lock,
    *,
    prepare: "Callable[[], Awaitable[_P]]",
    commit: "Callable[[_P], Awaitable[_R]]",
) -> _R:
    """Run one event write under ``lock``, cancellation-safe: the ONE place that is decided (#941).

    Every event mutation of both backends runs its locked section through
    here (or :func:`run_locked`, its one-phase form). Under ``lock``,
    ``prepare()`` reads and decides; ``commit(prepared)`` writes the store
    and then changes what is served. The section runs as an inner task the
    caller awaits through :func:`asyncio.shield`, so cancelling the caller
    never interrupts a write midway:

    * cancelled while it waits for the lock, or before ``commit`` starts (an
      awaited read in ``prepare``), the section is cancelled with nothing
      written;
    * cancelled once ``commit`` has started, the section runs to its end, the
      store write AND the change to what is served, before the lock is
      released and the caller's :class:`asyncio.CancelledError` is re-raised.
      A store write running in a worker thread finishes whatever the caller
      does, so stopping there would leave the store ahead of what is served,
      and the next mutation would merge onto the stale copy.

    The caller holds the lock and the inner task runs inside it, so the lock
    is released in the caller's own step once the section is done: no other
    mutation can run (and publish) between the section's end and the
    caller's response, whose stamp must follow the publish of its own change
    and no other.

    An exception from ``prepare`` or ``commit`` propagates. A section that
    fails after its caller was cancelled is not reported: the caller sees
    only its cancellation.
    """
    async with lock:
        committing = False

        async def section() -> _R:
            nonlocal committing
            prepared = await prepare()
            committing = True  # no await before commit's first one: its write
            return await commit(prepared)

        task = asyncio.ensure_future(section())
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if not committing:
                task.cancel()
            while not task.done():
                # Cancelled again: the section still finishes first.
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.wait({task})
            if not task.cancelled():
                task.exception()  # retrieved; the caller's cancellation wins
            raise


async def _nothing_to_prepare() -> None:
    """:func:`run_locked`'s ``prepare``: a one-phase section reads nothing first."""


async def run_locked(lock: asyncio.Lock, commit: "Callable[[], Awaitable[_R]]") -> _R:
    """Run ``commit()`` (a store write, then the change to what is served) under ``lock``.

    The one-phase form of :func:`run_locked_in_phases`, with its
    cancellation guarantee: once started, ``commit`` runs to its end before
    the lock is released, and the caller's cancellation is re-raised after.
    For a create or a revert whose whole decision lives in its one write.
    """

    async def commit_prepared(_: None) -> _R:
        return await commit()

    return await run_locked_in_phases(lock, prepare=_nothing_to_prepare, commit=commit_prepared)


async def revert_via(
    expect: EventRecord | None,
    write: EventRecord | None,
    *,
    restore: "Callable[[EventRecord], Awaitable[_R]]",
    mutate: "Callable[[Callable[[EventRecord], EventRecord | None]], Awaitable[_R | None]]",
) -> _R:
    """Move an event from ``expect`` to ``write``: the ONE revert dispatch.

    :func:`revert_leg` picks the leg. A restore calls ``restore(write)``. An
    update or delete calls ``mutate(resolver)``, the backend's own locked
    change, whose resolver compares the event as it stands with ``expect``
    (:func:`require_expected`) and returns ``None`` for a delete, else
    ``write``'s editable fields over the current id and source. ``mutate``
    returning ``None`` means no event has the id: :class:`EventChanged`
    with ``None``.
    """
    if revert_leg(expect, write) == "restore":
        assert write is not None  # noqa: S101 — revert_leg's "restore" means write is set
        return await restore(write)

    def resolve(current: EventRecord) -> EventRecord | None:
        require_expected(current, expect)
        if write is None:
            return None
        return current.model_copy(update=editable_fields(write))

    result = await mutate(resolve)
    if result is None:
        raise EventChanged(None)
    return result


@dataclass
class _Resolution(Generic[_E]):
    """What a mutation's read and resolve decided: the event, and its new record."""

    current: _E
    resolved: _E | None


@dataclass(frozen=True)
class MutationPhases(Generic[_E, _R]):
    """One event change's two phases, as :func:`mutation_phases` builds them.

    ``prepare`` reads and decides; ``commit`` writes the store and changes what
    is served. Run them under a lock with :func:`run_locked_in_phases` (what
    :func:`mutate_under_lock` does), or with :func:`run_phases` inside a
    section that already holds it.
    """

    prepare: "Callable[[], Awaitable[_Resolution[_E] | None]]"
    commit: "Callable[[_Resolution[_E] | None], Awaitable[_R | None]]"


def mutation_phases(
    *,
    read: "Callable[[], Awaitable[_E | None]]",
    resolve: "Callable[[_E], _E | None]",
    write: "Callable[[_E, _E | None], Awaitable[bool]]",
    apply: "Callable[[_E, _E | None], _R]",
) -> "MutationPhases[_E, _R]":
    """Build the "read, resolve, check, write, apply" sequence: the ONE place its steps live (#941).

    Every change and delete of both backends runs these steps, with its own
    callables: the live collector (``MetricCollector.mutate_event``: its
    store, its DB, its publish) and a review source (``ReviewEvents``: its
    archive or its document, and what it serves).

    1. ``await read()`` the event as it stands in the backend's store (the
       live store, the review archive's row, a ``.json`` source's document);
       ``None`` (no such event) returns ``None`` with nothing written.
    2. ``resolve(current)`` it to the new full record, or ``None`` to delete
       it. It is where a caller's merge lives (a PATCH's omitted fields, an
       end's "already ended" refusal), so it always reads the latest state;
       an exception it raises propagates with nothing written.
    3. The resolved record must pass :func:`check_event_write` against the
       current one (a new blank label, a changed span that is not forward),
       checked here for every change.
    4. ``await write(current, resolved)`` to the backend's store. ``False``
       means the store no longer holds the event: ``None`` is returned and
       nothing applied. An exception propagates, likewise.
    5. ``apply(current, resolved)`` to what the backend serves, in one
       synchronous step, and return its result.

    Steps 1-3 are the ``prepare`` phase, steps 4-5 the ``commit`` phase.
    """

    async def prepare() -> "_Resolution[_E] | None":
        current = await read()
        if current is None:
            return None
        resolved = resolve(current)
        if resolved is not None:
            check_event_write(
                current,
                label=resolved.label,
                timestamp=resolved.timestamp,
                end_timestamp=resolved.end_timestamp,
            )
        return _Resolution(current, resolved)

    async def commit(step: "_Resolution[_E] | None") -> "_R | None":
        if step is None or not await write(step.current, step.resolved):
            return None
        return apply(step.current, step.resolved)

    return MutationPhases(prepare=prepare, commit=commit)


async def run_phases(phases: "MutationPhases[_E, _R]") -> "_R | None":
    """Run a mutation's phases with no lock of their own: for a section already under it.

    A review revert holds the mutation lock once for its whole decision
    (``ReviewEvents.revert``), and its update or delete leg runs the shared
    sequence through here; acquiring the lock again would deadlock, as
    ``asyncio.Lock`` is not re-entrant.
    """
    return await phases.commit(await phases.prepare())


async def mutate_under_lock(
    lock: asyncio.Lock,
    *,
    read: "Callable[[], Awaitable[_E | None]]",
    resolve: "Callable[[_E], _E | None]",
    write: "Callable[[_E, _E | None], Awaitable[bool]]",
    apply: "Callable[[_E, _E | None], _R]",
) -> "_R | None":
    """Change or delete one event under ``lock``: :func:`mutation_phases`'s sequence, locked once.

    Under ``lock``, so no two mutations interleave, and through
    :func:`run_locked_in_phases`, so a cancelled request never leaves the
    store and what is served apart: a request cancelled before its write
    starts writes nothing, and one cancelled after runs its write AND its
    apply before the lock is released and the cancellation re-raised.
    ``apply`` follows its own ``write`` in order, and the lock is released in
    the caller's own step, so the caller's response is built before any other
    mutation runs.
    """
    phases = mutation_phases(read=read, resolve=resolve, write=write, apply=apply)
    return await run_locked_in_phases(lock, prepare=phases.prepare, commit=phases.commit)


@dataclass
class ResolvedEventFields:
    """The full field set an update resolves to — what a backend writes."""

    label: str
    color: str
    dash: str
    timestamp: datetime
    end_timestamp: datetime | None


def editable_fields(source: "ResolvedEventFields | EventRecord | MonitorEvent") -> dict[str, Any]:
    """Return ``source``'s editable event fields, by name: the one list of what an edit writes.

    The list is :class:`ResolvedEventFields`'s fields, so a field added there
    reaches every site that overwrites a live event (a PATCH or /end merge, a
    full-set update, a revert, the collector's in-place copy-back) at once.
    Use it as ``dataclasses.replace(event, **editable_fields(source))``.
    """
    return {f.name: getattr(source, f.name) for f in fields(ResolvedEventFields)}


# DEBT(no-tuple-return): (start, end) pair; wants a frozen dataclass.
# ast-grep-ignore: no-tuple-return
def resolve_create(body: EventCreateBody) -> tuple[datetime, datetime | None]:
    """Resolve a create body to concrete ``(timestamp, end_timestamp)``.

    An omitted timestamp means server-now (the Mark-now flow). The span
    ordering of the RESOLVED pair (a body with only ``end_timestamp`` set
    becomes a full pair here) is not checked here: the backend that stores
    it refuses a backward pair through :func:`check_event_write`.
    """
    timestamp = body.timestamp or utc_now()
    return timestamp, body.end_timestamp


def merge_update(
    body: EventUpdateBody,
    *,
    existing_label: str,
    existing_color: str,
    existing_dash: str,
    existing_timestamp: datetime,
    existing_end: datetime | None,
) -> ResolvedEventFields:
    """Merge a partial update onto an existing event's fields.

    ``model_fields_set`` semantics: an absent field is unchanged; an explicit
    JSON null ``end_timestamp`` CLEARS the end (span -> point). The MERGED
    pair's ordering is checked where it is written
    (:func:`check_event_write`, run by :func:`mutation_phases`), not here.
    Existing fields are passed explicitly (not as a model) so both backends —
    a live ``MonitorEvent`` and a review ``EventRecord`` — use this one rule
    without an adapter type.
    """
    provided = body.model_fields_set
    timestamp = (
        body.timestamp
        if "timestamp" in provided and body.timestamp is not None
        else existing_timestamp
    )
    end_timestamp = body.end_timestamp if "end_timestamp" in provided else existing_end
    return ResolvedEventFields(
        label=body.label if body.label is not None else existing_label,
        color=body.color if body.color is not None else existing_color,
        dash=body.dash if body.dash is not None else existing_dash,
        timestamp=timestamp,
        end_timestamp=end_timestamp,
    )
