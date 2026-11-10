"""Shared event-mutation semantics for every otto surface.

One home for the rules that must not fork between the live collector path,
the review-archive path, the suite's programmatic marks,
and any future CLI command: how a create resolves an omitted timestamp, how
a partial update merges onto an existing event, and when a span's ordering
is invalid. Callers translate :class:`EventValidationError` into their own
surface's failure shape (HTTP 422; a raised ValueError in library code).
"""

from dataclasses import dataclass, fields
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Literal

from ..errors import OttoError
from ..models.monitor import (
    SPAN_ORDER_ERROR,
    EventCreateBody,
    EventRecord,
    EventUpdateBody,
    checked_label,
    span_is_forward,
)

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
    """A revert found the event in a state other than the one it expected.

    ``current`` is the event as it actually stands (``None`` when no event
    has the id), so the caller can show it and the dashboard can drop the
    step that no longer applies.
    """

    def __init__(self, current: EventRecord | None) -> None:
        super().__init__("the event changed since this step was recorded")
        self.current = current


def same_event(a: EventRecord | None, b: EventRecord | None) -> bool:
    """Report whether two event states are the same: the ONE "is this the expected state" test.

    Two ``None`` are the same (both absent); ``None`` and a record are not.
    Timestamps compare as aware datetimes, i.e. as instants, never as text:
    the wire's ``Z``, the store's ``+00:00`` and the browser's ``.000Z`` all
    name one instant. ``color`` compares after ``EventRecord``'s lenient
    normalisation (``#ABC`` is ``#aabbcc``; a legacy ``"red"`` is itself).
    ``id`` is ignored: the caller has already chosen the event by id.
    """
    if a is None or b is None:
        return a is b
    return (
        a.timestamp == b.timestamp
        and a.end_timestamp == b.end_timestamp
        and a.label == b.label
        and a.color == b.color
        and a.dash == b.dash
        and a.source == b.source
    )


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


def check_span(timestamp: datetime, end_timestamp: datetime | None) -> None:
    """Refuse a span that does not end after it starts, as :class:`EventValidationError`.

    Create, a PATCH or /end merge and a revert's ``write``
    (:func:`check_revert_write`) all call it on the
    pair they are about to store. The rule itself is
    ``otto.models.monitor.span_is_forward``, which the create body's own
    validator shares.
    """
    if not span_is_forward(timestamp, end_timestamp):
        raise EventValidationError(SPAN_ORDER_ERROR)


def check_label_write(existing: str | None, label: str) -> None:
    """Refuse a blank label an event write introduces, as :class:`EventValidationError`.

    The ONE place a library, archive or revert write asks "does this label
    need the create/update rule?" (the rule itself is
    ``otto.models.monitor.checked_label``). ``existing`` is the label the
    event already holds, or ``None`` when the write creates or restores one:
    a new label is checked, as on a create. A label that equals ``existing``
    is put back as it is, as a PATCH that omits it would leave it: an older
    archive may hold one that is now refused.
    """
    if existing is None or label != existing:
        try:
            checked_label(label)
        except ValueError as err:
            raise EventValidationError(str(err)) from err


def check_revert_write(expect: EventRecord | None, write: EventRecord | None) -> None:
    """Refuse a revert's ``write`` that a create or an update would refuse.

    The span is checked as :func:`check_span` checks every stored pair, the
    label as :func:`check_label_write` does: on a restore (no ``expect``, so
    the label is new) and on an update that changes it.
    """
    if write is None:
        return
    check_span(write.timestamp, write.end_timestamp)
    check_label_write(None if expect is None else expect.label, write.label)


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
    ordering is re-checked on the RESOLVED pair — the model can only validate
    what it holds, and a body with only ``end_timestamp`` set becomes a full
    pair here.
    """
    timestamp = body.timestamp or utc_now()
    check_span(timestamp, body.end_timestamp)
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
    JSON null ``end_timestamp`` CLEARS the end (span -> point). The span
    ordering is checked on the MERGED pair — the only place it can be.
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
    check_span(timestamp, end_timestamp)
    return ResolvedEventFields(
        label=body.label if body.label is not None else existing_label,
        color=body.color if body.color is not None else existing_color,
        dash=body.dash if body.dash is not None else existing_dash,
        timestamp=timestamp,
        end_timestamp=end_timestamp,
    )
