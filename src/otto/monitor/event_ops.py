"""Shared event-mutation semantics for every otto surface.

One home for the rules that must not fork between the live collector path,
the review-archive path, the suite's programmatic marks,
and any future CLI command: how a create resolves an omitted timestamp, how
a partial update merges onto an existing event, and when a span's ordering
is invalid. Callers translate :class:`EventValidationError` into their own
surface's failure shape (HTTP 422; a raised ValueError in library code).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..errors import OttoError
from ..models.monitor import EventCreateBody, EventUpdateBody


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


@dataclass
class ResolvedEventFields:
    """The full field set an update resolves to — what a backend writes."""

    label: str
    color: str
    dash: str
    timestamp: datetime
    end_timestamp: datetime | None


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
    if body.end_timestamp is not None and body.end_timestamp <= timestamp:
        raise EventValidationError("end_timestamp must be after timestamp")
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
    if end_timestamp is not None and end_timestamp <= timestamp:
        raise EventValidationError("end_timestamp must be after timestamp")
    return ResolvedEventFields(
        label=body.label if body.label is not None else existing_label,
        color=body.color if body.color is not None else existing_color,
        dash=body.dash if body.dash is not None else existing_dash,
        timestamp=timestamp,
        end_timestamp=end_timestamp,
    )
