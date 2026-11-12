"""The source a review server serves, and the one sequence every event write to it runs.

A review server serves one source at a time: a ``.db`` archive or a ``.json``
export. :class:`ReviewSource` owns that source's document (a copy, never the
caller's), its cached body, its archive path, its per-session id counters and
view-only ids, its token and its write counts, and publishes every change to
the stream. :class:`SourceSlot` holds the current one, so the server can
replace it, and checks a write's token against it.

:class:`ReviewEvents` is every review event write: create, change or delete,
and revert. Each one runs under the collector's mutation lock, checks the
token first, and stores through the source's backend: :class:`DbBackend`
(the archive is the store, the document a copy kept in step with it) or
:class:`JsonBackend` (the document is the store).

:func:`switch_source` replaces a ``.json`` source with an uploaded one, read
by :func:`read_upload` under the :class:`UploadGate`.
"""

import asyncio
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, TypeVar

from starlette.requests import Request

from ..errors import OttoError
from ..models.monitor import (
    EventRecord,
    MonitorExport,
    MonitorSourceFrame,
    MonitorSourceState,
    SessionRecord,
)
from . import archive_edit, event_ops
from .collector import STREAM_FORMAT, observed_now
from .event_ops import EventChanged, EventIdCounter
from .export import document_json
from .review import parse_review_json, view_only_ids

_T = TypeVar("_T")

MAX_SOURCE_UPLOAD_BYTES = 256 * 1024 * 1024
"""The largest ``.json`` upload a review server reads, in bytes."""


def new_source_token() -> str:
    """Return a fresh source token: the ONE token form, for a review source and a live server.

    A token only tells sources apart, so a short random one is enough.
    """
    return secrets.token_urlsafe(9)


class SourceChanged(OttoError):  # noqa: N818 — named by the spec: a state, not a caller error
    """A write named a source this server no longer serves; ``state`` is the one it serves now."""

    def __init__(self, state: MonitorSourceState) -> None:
        super().__init__("this write was made against a monitor source the server no longer serves")
        self.state = state


class ViewOnly(OttoError):  # noqa: N818 — named by the spec: a state, not a caller error
    """A write named an event id two events of the session share, so it names neither."""


class UnknownSessionError(OttoError, LookupError):
    """A write named a session the served source does not hold: the ONE spelling of that refusal."""

    def __init__(self, session_id: str) -> None:
        super().__init__(f"unknown session '{session_id}'")
        self.session_id = session_id


class NotSwitchable(OttoError):  # noqa: N818 — named by the spec: a state, not a caller error
    """An upload to a server whose source no upload replaces: a live run or a ``.db`` archive."""


class UploadTooLarge(OttoError):  # noqa: N818 — named by the spec: a state, not a caller error
    """An upload larger than the limit :func:`read_upload` was given."""

    def __init__(self, limit: int) -> None:
        super().__init__(f"the upload is larger than the {limit}-byte limit")
        self.limit = limit


class ReviewSource:
    """One review source: its own copy of the document, its ids, its token and its counts.

    The holder copies each session with its own ``model_copy(deep=True)``. One
    deep copy of the whole document would keep a list two sessions share still
    shared (deepcopy's memo preserves aliasing), so an edit to one session
    would show in the other.

    :meth:`patch_event` and :meth:`drop_event` are the ONE seam that changes
    the document: it is private, and :meth:`find_event` answers a copy, so
    nothing else can change it behind the cached body. Each change marks the
    body stale (:meth:`body` rebuilds it when next asked) and publishes the
    session fragment. A ``counted`` change adds one to the revision, the
    count of writes since the load, and then publishes the source frame;
    :attr:`saved` is the highest revision an export acknowledged, and
    :attr:`unsaved` the difference.

    Args:
        document: The validated export to serve. It is copied, never changed.
        name: The human-facing origin (a file name) the dashboard shows.
        archive_path: The ``.db`` archive edits persist to, or ``None`` for a
            ``.json`` source.
        publish: Where each change goes out
            (:meth:`~otto.monitor.collector.MetricCollector.publish`).
    """

    def __init__(
        self,
        document: MonitorExport,
        *,
        name: str,
        archive_path: Path | None,
        publish: Callable[..., str],
    ) -> None:
        self._document = MonitorExport(
            format=document.format,
            sessions=[s.model_copy(deep=True) for s in document.sessions],
        )
        self.name = name
        self.archive_path = archive_path
        self.kind: Literal["db", "json"] = "db" if archive_path is not None else "json"
        self.token = new_source_token()
        self.revision = 0
        self.saved = 0
        self.ids: dict[str, EventIdCounter] = {}
        self.view_only: dict[str, frozenset[int]] = {}
        for session in self._document.sessions:
            counter = EventIdCounter()
            for event in session.events:
                if event.id is not None:
                    counter.note(event.id)
            self.ids[session.id] = counter
            self.view_only[session.id] = view_only_ids(session)
        self._publish = publish
        self._body: str | None = None

    @property
    def unsaved(self) -> int:
        """The writes the last acknowledged export does not hold."""
        return self.revision - self.saved

    def find_event(self, session_id: str, event_id: int) -> EventRecord | None:
        """Return a copy of the held event *event_id* of *session_id*, or ``None``."""
        session = self._session(session_id)
        if session is None:
            return None
        found = next((e for e in session.events if e.id == event_id), None)
        return None if found is None else found.model_copy()

    def body(self) -> str:
        """Return the document's ``format:1`` body, rebuilt only after a change."""
        if self._body is None:
            self._body = document_json(self._document)
        return self._body

    def patch_event(self, session_id: str, record: EventRecord, *, counted: bool) -> None:
        """Upsert *record* into *session_id* (the session must be held) and publish it."""
        session = self.require_session(session_id)
        for i, existing in enumerate(session.events):
            if existing.id == record.id:
                session.events[i] = record
                break
        else:
            session.events.append(record)
        self._changed(session_id, {"events": [record.model_dump(mode="json")]}, counted=counted)

    def drop_event(self, session_id: str, event_id: int, *, counted: bool) -> None:
        """Remove event *event_id* from *session_id* (the session must be held) and publish it."""
        session = self.require_session(session_id)
        session.events = [e for e in session.events if e.id != event_id]
        self._changed(session_id, {"deleted_event_ids": [event_id]}, counted=counted)

    def acknowledge(self, revision: int) -> None:
        """Record that an export holds every write up to *revision* (never past the current one).

        Publishes the source frame when the unsaved count changed, so a
        repeated acknowledgement sends nothing.
        """
        saved = max(self.saved, min(revision, self.revision))
        if saved != self.saved:
            self.saved = saved
            self.publish_state()

    def publish_state(self) -> None:
        """Publish this source's state as one source frame: the ONE source-frame publish.

        The stamp is read once: the frame's own ``observed_at`` is its
        state's, as the state serialises it, so the two are the same text.
        """
        frame = MonitorSourceFrame(source_state=self.state(observed_now()))
        payload = frame.model_dump(mode="json", exclude={"observed_at"})
        self._publish(payload, observed_at=payload["source_state"]["observed_at"])

    def state(self, observed_at: str) -> MonitorSourceState:
        """Return this source's state as ``/api/mode`` reports it, stamped *observed_at*.

        Every review source is editable: a ``.db`` write persists to its
        archive, and a ``.json`` write is held here until an export saves it.
        """
        return MonitorSourceState(
            mode="review",
            kind=self.kind,
            source=self.name,
            token=self.token,
            editable=True,
            unsaved=self.unsaved,
            revision=self.revision,
            observed_at=observed_at,
        )

    def _session(self, session_id: str) -> SessionRecord | None:
        return next((s for s in self._document.sessions if s.id == session_id), None)

    def require_session(self, session_id: str) -> SessionRecord:
        """Return the held session *session_id*.

        Raises :class:`UnknownSessionError` for one the document lacks.
        """
        session = self._session(session_id)
        if session is None:
            raise UnknownSessionError(session_id)
        return session

    def _changed(self, session_id: str, fragment: dict[str, Any], *, counted: bool) -> None:
        """Mark the body stale and publish the change: its fragment, then (counted) the count.

        One synchronous step, so no other frame goes out between the two.
        """
        self._body = None
        if counted:
            self.revision += 1
        self._publish({"format": STREAM_FORMAT, "session": session_id, **fragment})
        if counted:
            self.publish_state()


class SourceSlot:
    """The review source a server currently serves (``None`` for a live server).

    :meth:`replace` runs only under ``collector.mutation_lock()`` (its one
    caller is :func:`switch_source`), and every write checks its token with
    :meth:`require` under the same lock, so a write that queued behind a
    switch is refused, never applied to the new source.
    """

    def __init__(self, current: ReviewSource | None) -> None:
        self.current = current

    def replace(self, new: ReviewSource) -> None:
        """Serve *new* in place of the current source (under the mutation lock only)."""
        self.current = new

    def require(self, token: str | None) -> ReviewSource:
        """Return the served source if *token* names it: the ONE review token check.

        Raises :class:`SourceChanged`, carrying the served source's state, for
        a missing or different token. Call it under the mutation lock, first.
        """
        current = self.current
        if current is None:
            raise RuntimeError("a review server serves a source: its slot is never empty")
        if token != current.token:
            raise SourceChanged(current.state(observed_now()))
        return current


class UploadGate:
    """One upload at a time, per app: taken BEFORE the body is read, held until the switch ends.

    So the server never holds more than one upload body. Use it as
    ``async with gate:``.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> None:
        await self._lock.acquire()

    async def __aexit__(self, *_exc: object) -> None:
        self._lock.release()


async def read_upload(request: Request, *, limit: int = MAX_SOURCE_UPLOAD_BYTES) -> bytes:
    """Read an upload's raw body, refusing one past *limit* bytes (:class:`UploadTooLarge`).

    A ``Content-Length`` past the limit is refused before anything is read;
    a body without one (chunked) is refused as soon as the bytes read pass it.
    """
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise UploadTooLarge(limit)
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise UploadTooLarge(limit)
        chunks.append(chunk)
    return b"".join(chunks)


def switch_source(
    slot: SourceSlot,
    *,
    token: str | None,
    unsaved: str | None,
    data: bytes,
    name: str,
    publish: Callable[..., str],
) -> ReviewSource:
    """Serve the uploaded ``.json`` *data* in place of the current source: the ONE switch.

    Call it under ``collector.mutation_lock()``. The confirmation must name
    the served source (*token*, checked by :meth:`SourceSlot.require`) and
    the unsaved count it was shown (*unsaved*, the decimal header text);
    either differing raises :class:`SourceChanged`. *data* is parsed with
    :func:`~otto.monitor.review.parse_review_json`, on the loop; a refusal
    (:class:`~otto.monitor.errors.ReviewSourceError`) leaves the current
    source, its token and its counts untouched. The new source then
    publishes its state, so every tab moves onto it.

    Returns:
        The source now served.
    """
    current = slot.require(token)
    if unsaved != str(current.unsaved):
        raise SourceChanged(current.state(observed_now()))
    document = parse_review_json(data, origin=name)
    new = ReviewSource(document, name=name, archive_path=None, publish=publish)
    slot.replace(new)
    new.publish_state()
    return new


class ReviewBackend(Protocol):
    """Where a review source's events are stored: its archive, or its own document.

    ``counts_writes`` is whether a write is one an export still has to save:
    a ``.db`` write persists to its archive, a ``.json`` write does not. Every
    method runs under the mutation lock, inside :class:`ReviewEvents`.
    """

    counts_writes: bool

    async def read(
        self, source: ReviewSource, session_id: str, event_id: int
    ) -> EventRecord | None:
        """Return the event as the store holds it, or ``None``."""
        ...

    async def insert(self, source: ReviewSource, session_id: str, new: EventRecord) -> EventRecord:
        """Store *new* under a fresh id and return it with that id."""
        ...

    async def write(
        self,
        source: ReviewSource,
        session_id: str,
        event_id: int,
        current: EventRecord,
        resolved: EventRecord | None,
    ) -> bool:
        """Store *resolved* (``None``: delete) over *current*; ``False`` if the event is gone."""
        ...

    async def restore(
        self, source: ReviewSource, session_id: str, event_id: int, record: EventRecord
    ) -> EventRecord:
        """Put *record* back under *event_id* and return it as stored."""
        ...


def _archive_of(source: ReviewSource) -> str:
    archive = source.archive_path
    if archive is None:
        raise RuntimeError(f"review source '{source.name}' has no archive to write")
    return str(archive)


def _repair(source: ReviewSource, session_id: str, event_id: int, row: EventRecord | None) -> None:
    """Bring the document's copy of one event to the archive's row (#941): never counted.

    The archive is the truth for a ``.db`` source, and the document a copy
    another writer of the archive (a second review server) can have made
    stale. A copy that differs from *row* becomes it; a *row* of ``None``
    drops the copy. A copy that already matches is left alone, so nothing is
    published and the body is not rebuilt.
    """
    copy = source.find_event(session_id, event_id)
    if row is None:
        if copy is not None:
            source.drop_event(session_id, event_id, counted=False)
    elif copy != row:
        source.patch_event(session_id, row, counted=False)


class DbBackend:
    """A ``.db`` source: the archive is the store, written off the loop (``to_thread``).

    A write the archive refuses as changed
    (:class:`~otto.monitor.event_ops.EventChanged`) repairs the document to
    the row it carries before the refusal propagates.
    """

    counts_writes = False

    async def read(
        self, source: ReviewSource, session_id: str, event_id: int
    ) -> EventRecord | None:
        """Read the archive's row, repairing the document's copy to it."""
        row = await asyncio.to_thread(
            archive_edit.read_event, _archive_of(source), session_id, event_id
        )
        _repair(source, session_id, event_id, row)
        return row

    async def insert(self, source: ReviewSource, session_id: str, new: EventRecord) -> EventRecord:
        """Insert the row; the archive's AUTOINCREMENT hands out the id."""
        rowid = await asyncio.to_thread(
            archive_edit.insert_event,
            _archive_of(source),
            session_id,
            timestamp=new.timestamp,
            end_timestamp=new.end_timestamp,
            label=new.label,
            source=new.source,
            color=new.color,
            dash=new.dash,
        )
        source.ids[session_id].note(rowid)
        return new.model_copy(update={"id": rowid})

    async def write(
        self,
        source: ReviewSource,
        session_id: str,
        event_id: int,
        current: EventRecord,
        resolved: EventRecord | None,
    ) -> bool:
        """Write the row only if it is still *current*."""
        path = _archive_of(source)
        if resolved is None:
            removed = await self._guarded(
                source,
                session_id,
                event_id,
                lambda: archive_edit.delete_event(path, session_id, event_id, expect=current),
            )
            return removed is not None
        return await self._guarded(
            source,
            session_id,
            event_id,
            lambda: archive_edit.update_event(
                path, session_id, event_id, expect=current, **event_ops.editable_fields(resolved)
            ),
        )

    async def restore(
        self, source: ReviewSource, session_id: str, event_id: int, record: EventRecord
    ) -> EventRecord:
        """Put the row back under its id, if the archive handed it out and nothing holds it."""
        path = _archive_of(source)
        return await self._guarded(
            source,
            session_id,
            event_id,
            lambda: archive_edit.restore_event(path, session_id, event_id, record),
        )

    @staticmethod
    async def _guarded(
        source: ReviewSource,
        session_id: str,
        event_id: int,
        write: Callable[[], _T],
    ) -> _T:
        try:
            return await asyncio.to_thread(write)
        except EventChanged as err:
            _repair(source, session_id, event_id, err.current)
            raise


class JsonBackend:
    """A ``.json`` source: its own document is the store, so a write is held until an export."""

    counts_writes = True

    async def read(
        self, source: ReviewSource, session_id: str, event_id: int
    ) -> EventRecord | None:
        """Return the document's event; an id two events share raises :class:`ViewOnly`."""
        if event_id in source.view_only[session_id]:
            raise ViewOnly(f"event {event_id} shares its id with another event, so it is view-only")
        return source.find_event(session_id, event_id)

    async def insert(self, source: ReviewSource, session_id: str, new: EventRecord) -> EventRecord:
        """Give *new* the session's next id (past the last: ``IdsExhausted``, a 422)."""
        return new.model_copy(update={"id": source.ids[session_id].take()})

    async def write(
        self,
        source: ReviewSource,
        session_id: str,
        event_id: int,
        current: EventRecord,
        resolved: EventRecord | None,
    ) -> bool:
        """Store nothing: the document is the store, and the apply step changes it."""
        del source, session_id, event_id, current, resolved
        return True

    async def restore(
        self, source: ReviewSource, session_id: str, event_id: int, record: EventRecord
    ) -> EventRecord:
        """Put *record* back under *event_id*, if the session handed it out and nothing holds it."""
        present = source.find_event(session_id, event_id)
        if present is not None:
            raise EventChanged(present)
        if not source.ids[session_id].allocated(event_id):
            raise EventChanged(None)
        return record.model_copy(update={"id": event_id})


@dataclass(frozen=True)
class _Target:
    """One session of the source a write's token named, and the backend that stores it."""

    source: ReviewSource
    backend: ReviewBackend
    session_id: str

    async def read(self, event_id: int) -> EventRecord | None:
        return await self.backend.read(self.source, self.session_id, event_id)

    async def write(
        self, event_id: int, current: EventRecord, resolved: EventRecord | None
    ) -> bool:
        return await self.backend.write(self.source, self.session_id, event_id, current, resolved)

    def apply(
        self, event_id: int, current: EventRecord, resolved: EventRecord | None
    ) -> EventRecord:
        """Change the document after a write; return the stored record, or the removed one."""
        counted = self.backend.counts_writes
        if resolved is None:
            self.source.drop_event(self.session_id, event_id, counted=counted)
            return current
        self.source.patch_event(self.session_id, resolved, counted=counted)
        return resolved

    async def insert(self, new: EventRecord) -> EventRecord:
        created = await self.backend.insert(self.source, self.session_id, new)
        self.source.patch_event(self.session_id, created, counted=self.backend.counts_writes)
        return created

    async def restore(self, event_id: int, record: EventRecord) -> EventRecord:
        restored = await self.backend.restore(self.source, self.session_id, event_id, record)
        self.source.patch_event(self.session_id, restored, counted=self.backend.counts_writes)
        return restored


Resolve = Callable[[EventRecord], "EventRecord | None"]
"""A change's resolver: the event as it stands to its new record, or ``None`` to delete it."""


class ReviewEvents:
    """Every event write to a review source: one sequence over both backends.

    Each write runs under the mutation lock, acquired exactly once per
    request (``event_ops.run_locked`` or ``event_ops.mutate_under_lock``),
    and checks its token with :meth:`SourceSlot.require` first, under that
    lock, before it resolves any session or event. A session the source does
    not hold raises :class:`UnknownSessionError`. The backend is the source's
    ``kind``'s; a change is counted when the backend's writes are.

    Args:
        slot: The served source.
        lock: The collector's ``mutation_lock`` (a lock per running loop).
    """

    def __init__(self, slot: SourceSlot, lock: Callable[[], asyncio.Lock]) -> None:
        self._slot = slot
        self._lock = lock
        self._backends: dict[str, ReviewBackend] = {"db": DbBackend(), "json": JsonBackend()}

    def _target(self, source: ReviewSource, session_id: str) -> _Target:
        source.require_session(session_id)
        return _Target(source, self._backends[source.kind], session_id)

    async def create(self, token: str | None, session_id: str, new: EventRecord) -> EventRecord:
        """Store *new* (its ``id`` unset) under a fresh id; return it as stored."""

        async def commit() -> EventRecord:
            source = self._slot.require(token)
            target = self._target(source, session_id)
            event_ops.check_event_write(
                None, label=new.label, timestamp=new.timestamp, end_timestamp=new.end_timestamp
            )
            return await target.insert(new)

        return await event_ops.run_locked(self._lock(), commit)

    async def mutate(
        self, token: str | None, session_id: str, event_id: int, resolve: Resolve
    ) -> EventRecord | None:
        """Change or delete one event: ``event_ops.mutate_under_lock`` over the backend.

        Returns the stored record after a change, the removed one after a
        delete, or ``None`` when the store holds no such event.
        """
        target: _Target | None = None

        async def read() -> EventRecord | None:
            nonlocal target
            source = self._slot.require(token)
            target = self._target(source, session_id)
            return await target.read(event_id)

        async def write(current: EventRecord, resolved: EventRecord | None) -> bool:
            assert target is not None  # noqa: S101 — read ran first
            return await target.write(event_id, current, resolved)

        def apply(current: EventRecord, resolved: EventRecord | None) -> EventRecord:
            assert target is not None  # noqa: S101 — read ran first
            return target.apply(event_id, current, resolved)

        return await event_ops.mutate_under_lock(
            self._lock(), read=read, resolve=resolve, write=write, apply=apply
        )

    async def revert(
        self,
        token: str | None,
        session_id: str,
        event_id: int,
        expect: EventRecord | None,
        write: EventRecord | None,
    ) -> EventRecord:
        """Move an event from *expect* to *write* (``event_ops.revert_via``), under one lock.

        The update and delete legs run the shared sequence with
        ``event_ops.run_phases``, not :meth:`mutate`, since this section
        already holds the lock.
        """

        async def commit() -> EventRecord:
            source = self._slot.require(token)
            target = self._target(source, session_id)
            # The body after the token and the session, as a live revert
            # checks it after its guard: one refusal order in both modes.
            event_ops.check_revert_write(expect, write)

            async def mutate(resolve: Resolve) -> EventRecord | None:
                phases = event_ops.mutation_phases(
                    read=lambda: target.read(event_id),
                    resolve=resolve,
                    write=lambda current, resolved: target.write(event_id, current, resolved),
                    apply=lambda current, resolved: target.apply(event_id, current, resolved),
                )
                return await event_ops.run_phases(phases)

            return await event_ops.revert_via(
                expect,
                write,
                restore=lambda record: target.restore(event_id, record),
                mutate=mutate,
            )

        return await event_ops.run_locked(self._lock(), commit)
