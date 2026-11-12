"""Two live event mutations racing on one event never lose either (#868).

The collector writes an event change to its DB before it changes memory and
publishes (so a snapshot never sees a change with an earlier stamp). That
await opens a window: a second request that read the event before the first
one's write finished would merge onto stale fields and undo it. Every event
mutation, read and merge included, is therefore serialised through the
collector's one entry point. These tests hold a DB write open, race a second
request into the window, and check memory, the archive and the published
record.

Review mode (#941) writes a cold archive through ``archive_edit`` in
``asyncio.to_thread`` and then patches its served document. The same race
opens there, and the same entry point closes it: the review tests hold an
archive write after it lands (before its document patch), race a second
request into that window, and check the archive (read with sqlite3) and the
served document agree on one serial order.

Two more properties close the module: a request cancelled mid-write still
leaves the store and what is served in agreement (the locked section runs
to its end before the lock is released), and in review mode the archive's
row, not the served document's snapshot, is what an edit resolves against.
"""

import asyncio
import contextlib
import dataclasses
import sqlite3
import threading
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from _archive import _make_archive
from _requests import asgi, settle

from otto.models import LabSnapshot
from otto.models.monitor import MonitorSessionFragment
from otto.monitor import archive_edit
from otto.monitor.collector import MetricCollector
from otto.monitor.events import MonitorEvent
from otto.monitor.export import build_db_export
from otto.monitor.review_source import ReviewSource, SourceSlot
from otto.monitor.server import _build_app
from otto.monitor.session import new_frame


@dataclass
class _Hold:
    """An asyncio gate: ``hold()`` makes the NEXT ``passing()`` wait until ``release()``.

    ``pending`` counts the callers inside ``passing()``, held or not.
    """

    pending: int = 0
    _gate: asyncio.Event | None = None
    _armed: bool = False

    def hold(self) -> None:
        self._gate = asyncio.Event()
        self._armed = True

    def release(self) -> None:
        assert self._gate is not None
        self._gate.set()

    @contextlib.asynccontextmanager
    async def passing(self) -> AsyncIterator[None]:
        gate = self._gate if self._armed else None
        self._armed = False
        self.pending += 1
        try:
            if gate is not None:
                await gate.wait()
            yield
        finally:
            self.pending -= 1


@dataclass
class _ArchiveSpy(_Hold):
    """Quacks MetricDB's event writes, recording each as the archive's row.

    ``hold()`` makes the NEXT write wait until ``release()``; ``fail_next``
    makes the next write raise, as a locked or full disk would.
    """

    rows: dict[int, dict[str, object] | None] = field(default_factory=dict)
    fail_next: bool = False

    async def _write(self, event_id: int, row: dict[str, object] | None) -> None:
        async with self.passing():
            if self.fail_next:
                self.fail_next = False
                raise OSError("disk full")
            self.rows[event_id] = row

    async def write_event(self, event: MonitorEvent) -> int:
        return 0

    async def update_event(self, event: MonitorEvent) -> None:
        await self._write(event.id, event.to_dict())

    async def delete_event(self, event_id: int) -> None:
        await self._write(event_id, None)


def _db_slot(path: str, collector: MetricCollector) -> SourceSlot:
    """The slot a review server of the ``.db`` archive at *path* serves from."""
    archive = Path(path)
    return SourceSlot(
        ReviewSource(
            build_db_export(path),
            name=archive.name,
            archive_path=archive,
            publish=collector.publish,
        )
    )


@dataclass
class _Live:
    app: object
    collector: MetricCollector
    archive: _ArchiveSpy
    queue: "asyncio.Queue[dict[str, object]]"
    base: str
    event_id: int

    def stored(self) -> MonitorEvent | None:
        return next((e for e in self.collector.get_events() if e.id == self.event_id), None)

    def published_events(self) -> list[dict[str, object]]:
        out: list[dict[str, object]] = []
        while not self.queue.empty():
            frag = MonitorSessionFragment.model_validate(self.queue.get_nowait())
            out += [e.model_dump(mode="json") for e in frag.events or []]
            out += [{"deleted": i} for i in frag.deleted_event_ids or []]
        return out


async def _live() -> _Live:
    collector = MetricCollector(hosts=[], parsers=[])
    frame = new_frame(label=None, note=None)
    collector.session_id = frame.id
    started = datetime.now(tz=timezone.utc) - timedelta(minutes=5)
    event = await collector.add_event(label="span", timestamp=started)
    archive = _ArchiveSpy()
    collector._db = archive  # type: ignore[assignment]
    queue = collector.subscribe()
    app = _build_app(collector, key="k", frame=frame, lab=LabSnapshot())
    base = f"/api/session/{frame.id}/event/{event.id}"
    return _Live(app, collector, archive, queue, base, event.id)


@pytest.mark.asyncio
async def test_a_label_patch_racing_a_pending_end_keeps_the_end() -> None:
    live = await _live()
    live.archive.hold()
    end = asyncio.create_task(asgi(live.app, "POST", f"{live.base}/end", {}))
    await settle()
    assert live.archive.pending == 1  # /end now waits on its archive write
    patch = asyncio.create_task(asgi(live.app, "PATCH", live.base, {"label": "renamed"}))
    await settle()
    live.archive.release()
    (end_status, _), (patch_status, patched) = await asyncio.gather(end, patch)

    assert (end_status, patch_status) == (200, 200)
    stored = live.stored()
    assert stored is not None
    assert stored.end_timestamp is not None, "the PATCH undid the end in memory"
    assert stored.label == "renamed"
    assert patched is not None
    assert patched["end_timestamp"] is not None
    row = live.archive.rows[live.event_id]
    assert row is not None
    assert row["end_timestamp"] is not None, "the archive lost the end"
    assert row["label"] == "renamed"
    last = live.published_events()[-1]
    assert last["end_timestamp"] is not None
    assert last["label"] == "renamed"


@pytest.mark.asyncio
async def test_an_end_racing_a_pending_label_patch_keeps_both() -> None:
    live = await _live()
    live.archive.hold()
    patch = asyncio.create_task(asgi(live.app, "PATCH", live.base, {"label": "renamed"}))
    await settle()
    assert live.archive.pending == 1
    end = asyncio.create_task(asgi(live.app, "POST", f"{live.base}/end", {}))
    await settle()
    live.archive.release()
    (patch_status, _), (end_status, _) = await asyncio.gather(patch, end)

    assert (patch_status, end_status) == (200, 200)
    stored = live.stored()
    assert stored is not None
    assert stored.label == "renamed"
    assert stored.end_timestamp is not None
    row = live.archive.rows[live.event_id]
    assert row is not None
    assert row["label"] == "renamed"
    assert row["end_timestamp"] is not None
    last = live.published_events()[-1]
    assert last["label"] == "renamed"
    assert last["end_timestamp"] is not None


@pytest.mark.asyncio
async def test_a_patch_racing_a_pending_delete_does_not_resurrect_it() -> None:
    live = await _live()
    live.archive.hold()
    delete = asyncio.create_task(asgi(live.app, "DELETE", live.base))
    await settle()
    assert live.archive.pending == 1
    patch = asyncio.create_task(asgi(live.app, "PATCH", live.base, {"label": "renamed"}))
    await settle()
    live.archive.release()
    (delete_status, _), (patch_status, _) = await asyncio.gather(delete, patch)

    assert delete_status == 200
    assert patch_status == 404
    assert live.stored() is None
    assert live.archive.rows[live.event_id] is None, "the archive row came back"
    published = live.published_events()
    assert published[-1] == {"deleted": live.event_id}


@pytest.mark.asyncio
async def test_a_failed_archive_write_releases_the_entry_point_and_leaves_memory_alone() -> None:
    live = await _live()
    live.archive.fail_next = True
    with pytest.raises(OSError, match="disk full"):
        await live.collector.update_event(
            event_id=live.event_id,
            label="lost",
            color="#888888",
            dash="dash",
            timestamp=datetime.now(tz=timezone.utc),
        )
    stored = live.stored()
    assert stored is not None
    assert stored.label == "span"
    assert live.published_events() == []
    # The next mutation proceeds: the failure released the serialisation.
    status, body = await asyncio.wait_for(
        asgi(live.app, "PATCH", live.base, {"label": "kept"}), timeout=5
    )
    assert status == 200
    assert body is not None
    assert body["label"] == "kept"


class _YieldingArchive:
    """An archive whose event write yields to the loop, so two marks contend."""

    async def write_event(self, event: MonitorEvent) -> int:
        await asyncio.sleep(0)
        return 0


def test_one_collector_serialises_marks_on_one_loop_then_another() -> None:
    """A suite keeps one session collector while tests mark on narrower loops
    (suite/plugin.py's class monitor task): contention on a second loop must
    not hit a lock bound to the first. All four marks record; nothing raises."""
    collector = MetricCollector(hosts=[], parsers=[])
    collector.session_id = "s1"
    collector._db = _YieldingArchive()  # type: ignore[assignment]

    async def two_marks(prefix: str) -> None:
        await asyncio.gather(
            collector.add_event(label=f"{prefix}-1"), collector.add_event(label=f"{prefix}-2")
        )

    asyncio.run(two_marks("a"))
    asyncio.run(two_marks("b"))
    labels = sorted(e.label for e in collector.get_events())
    assert labels == ["a-1", "a-2", "b-1", "b-2"]


# ----------------------------------------------------------------------
# Review mode (#941): archive writes under the same one entry point
# ----------------------------------------------------------------------


@dataclass
class _Review:
    app: object
    path: str
    session_id: str
    base: str
    event_id: int
    write: _Hold
    collector: MetricCollector
    read: _Hold = field(default_factory=_Hold)

    def row(self, event_id: int | None = None) -> tuple[str, str | None] | None:
        """The archive's ``(label, end_ts)`` for the event, read independently of the server."""
        conn = sqlite3.connect(self.path)
        try:
            return conn.execute(
                "SELECT label, end_ts FROM events WHERE id = ? AND session_id = ?",
                (self.event_id if event_id is None else event_id, self.session_id),
            ).fetchone()
        finally:
            conn.close()

    async def served(self) -> dict[int, dict[str, object]]:
        """Return the session's events by id, as the review document's endpoint serves them."""
        status, doc = await asgi(self.app, "GET", "/api/monitor_sessions")
        assert status == 200
        assert doc is not None
        (session,) = [s for s in doc["sessions"] if s["id"] == self.session_id]
        return {e["id"]: e for e in session["events"]}


async def _review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, inline: bool = True
) -> _Review:
    """A review server over a real archive holding one point event ``span``.

    Every archive call goes through ``asyncio.to_thread``. With ``inline``
    the call runs on the loop instead, so each request's archive write lands
    within a scheduling turn and the race is deterministic; ``write.hold()``
    then holds the NEXT call after it returns, i.e. after the archive write
    landed and before the route patches its document, so a second request
    can race into the window a missing lock would leave open. ``read.hold()``
    likewise holds the NEXT archive read (``archive_edit.read_event``) after
    it returns, before its write. Without ``inline`` the product's real
    worker thread runs it (the flock test).
    """
    path, sid = await asyncio.to_thread(_make_archive, tmp_path)
    event_id = archive_edit.insert_event(
        path,
        sid,
        timestamp=datetime(2026, 7, 18, 12, 1, tzinfo=timezone.utc),
        end_timestamp=None,
        label="span",
        source="manual",
        color="#888888",
        dash="dash",
    )
    write = _Hold()
    read = _Hold()

    async def held_inline(func: Callable[..., object], /, *args: object, **kwargs: object):
        result = func(*args, **kwargs)
        gate = read if func is getattr(archive_edit, "read_event", None) else write
        async with gate.passing():
            return result

    if inline:
        monkeypatch.setattr(asyncio, "to_thread", held_inline)
    collector = MetricCollector(hosts=[])
    app = _build_app(collector, key="k", mode="review", slot=_db_slot(path, collector))
    base = f"/api/session/{sid}/event"
    return _Review(app, path, sid, base, event_id, write, collector, read)


_Request = tuple[str, str, "dict[str, object] | None"]


async def _race(review: _Review, first: _Request, second: _Request):
    """Hold ``first``'s archive write, send ``second`` into the window, release; both answers."""
    review.write.hold()
    a = asyncio.create_task(asgi(review.app, *first))
    await settle()
    assert review.write.pending == 1, "the first request never reached its archive write"
    b = asyncio.create_task(asgi(review.app, *second))
    await settle()
    review.write.release()
    return await asyncio.gather(a, b)


@pytest.mark.asyncio
async def test_review_end_racing_a_label_patch_keeps_the_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    review = await _review(tmp_path, monkeypatch)
    one = f"{review.base}/{review.event_id}"
    (end_status, _), (patch_status, patched) = await _race(
        review, ("POST", f"{one}/end", {}), ("PATCH", one, {"label": "renamed"})
    )

    assert (end_status, patch_status) == (200, 200)
    assert patched is not None
    assert patched.get("end_timestamp") is not None, "the PATCH answered without the end"
    row = review.row()
    assert row is not None
    assert row[0] == "renamed"
    assert row[1] is not None, "the archive lost the end"
    served = (await review.served())[review.event_id]
    assert served["label"] == "renamed"
    assert served.get("end_timestamp") is not None, "the document lost the end"


@pytest.mark.asyncio
async def test_review_patch_racing_delete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    review = await _review(tmp_path, monkeypatch)
    one = f"{review.base}/{review.event_id}"
    (patch_status, _), (delete_status, deleted) = await _race(
        review, ("PATCH", one, {"label": "renamed"}), ("DELETE", one, None)
    )

    assert review.row() is None, "the archive row survived the delete"
    assert review.event_id not in await review.served(), "the document resurrected the event"
    # The PATCH held the entry point, so the one serial order is PATCH then
    # DELETE: the DELETE answers the patched record.
    assert (patch_status, delete_status) == (200, 200)
    assert deleted is not None
    assert deleted["label"] == "renamed"


@pytest.mark.asyncio
async def test_review_delete_answers_the_archive_row_not_the_loaded_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The document is a snapshot from load; the undo restores what DELETE answers."""
    review = await _review(tmp_path, monkeypatch)
    conn = sqlite3.connect(review.path)
    try:
        conn.execute(
            "UPDATE events SET label = 'edited-elsewhere' WHERE id = ?", (review.event_id,)
        )
        conn.commit()
    finally:
        conn.close()
    status, deleted = await asgi(review.app, "DELETE", f"{review.base}/{review.event_id}")

    assert status == 200
    assert deleted is not None
    assert deleted["label"] == "edited-elsewhere"
    assert review.row() is None
    assert review.event_id not in await review.served()


@pytest.mark.asyncio
async def test_review_create_racing_a_patch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    review = await _review(tmp_path, monkeypatch)
    created_id = review.event_id + 1  # the archive's AUTOINCREMENT hands out the next id
    (create_status, created), (patch_status, _) = await _race(
        review,
        ("POST", review.base, {"label": "new"}),
        ("PATCH", f"{review.base}/{created_id}", {"label": "renamed"}),
    )

    assert create_status == 201
    assert created is not None
    assert created["id"] == created_id
    # The create held the entry point, so the one serial order is create
    # then PATCH: the PATCH finds the new event and renames it.
    assert patch_status == 200, "the PATCH ran before the create's document patch"
    row = review.row(created_id)
    assert row is not None
    assert row[0] == "renamed"
    assert (await review.served())[created_id]["label"] == "renamed"


@pytest.mark.asyncio
async def test_review_revert_racing_a_patch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    review = await _review(tmp_path, monkeypatch)
    one = f"{review.base}/{review.event_id}"
    expect = (await review.served())[review.event_id]
    (revert_status, _), (patch_status, _) = await _race(
        review,
        ("POST", f"{one}/revert", {"expect": expect, "write": {**expect, "label": "reverted"}}),
        ("PATCH", one, {"label": "renamed"}),
    )

    assert (revert_status, patch_status) == (200, 200)
    row = review.row()
    assert row is not None
    served = (await review.served())[review.event_id]
    assert row[0] == served["label"], "the archive and the document disagree"
    assert row[0] == "renamed"  # the revert held the entry point, so the PATCH landed last


@pytest.mark.asyncio
async def test_review_concurrent_writes_never_report_archive_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first write HOLDS the archive flock while the second is sent.

    The wrapper enters the real ``_locked_connection`` (so the flock is
    really taken) on the first request's write, its second flock after its
    archive read, tells the loop, then blocks its ``to_thread`` worker on a
    test-owned ``threading.Event`` until the second request has had its
    turn. With the entry point serialising review writes, the second parks
    on the asyncio lock and never reaches the flock; without it, it reaches
    the flock and answers busy.
    """
    review = await _review(tmp_path, monkeypatch, inline=False)
    one = f"{review.base}/{review.event_id}"
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    unblock = threading.Event()
    real = archive_edit._locked_connection
    calls = 0

    @contextlib.contextmanager
    def held(path: str) -> Iterator[sqlite3.Connection]:
        nonlocal calls
        calls += 1
        first_write = calls == 2  # the first PATCH's read is call 1
        with real(path) as conn:
            if first_write:
                loop.call_soon_threadsafe(entered.set)
                assert unblock.wait(timeout=10), "the test never released the archive flock"
            yield conn

    monkeypatch.setattr(archive_edit, "_locked_connection", held)
    a = asyncio.create_task(asgi(review.app, "PATCH", one, {"label": "first"}))
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        b = asyncio.create_task(asgi(review.app, "PATCH", one, {"color": "#112233"}))
        await settle()
        # The second write is parked on the entry point, not at the flock.
        assert not b.done(), "the second write answered while the first held the flock"
        assert review.collector.mutation_lock().locked()
        assert calls == 2, "the second write reached the archive flock"
    finally:
        unblock.set()
    (a_status, a_body), (b_status, b_body) = await asyncio.gather(a, b)

    for status, body in ((a_status, a_body), (b_status, b_body)):
        assert body is not None
        assert body.get("code") != "busy", "a second write of this server hit its own flock"
        assert 200 <= status < 300
    served = (await review.served())[review.event_id]
    assert (served["label"], served["color"]) == ("first", "#112233")


# ----------------------------------------------------------------------
# Cancellation (#941): a cancelled request never leaves the store and what
# is served apart. Once its write has started, the locked section runs to
# the end (write AND apply) before the lock is released and the caller sees
# its CancelledError; cancelled before its write starts, it writes nothing.
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_end_cancelled_during_its_archive_write_still_patches_the_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    review = await _review(tmp_path, monkeypatch)
    one = f"{review.base}/{review.event_id}"
    review.write.hold()
    end = asyncio.create_task(asgi(review.app, "POST", f"{one}/end", {}))
    await settle()
    assert review.write.pending == 1, "the /end never reached its archive write"
    end.cancel()
    await settle()
    # The write landed in the archive; the lock is still held for its apply.
    assert review.collector.mutation_lock().locked(), "cancellation released the lock early"
    review.write.release()
    with pytest.raises(asyncio.CancelledError):
        await end
    assert not review.collector.mutation_lock().locked()
    row = review.row()
    assert row is not None
    assert row[1] is not None
    assert (await review.served())[review.event_id].get("end_timestamp") is not None, (
        "the document missed the end the archive holds"
    )

    status, _ = await asgi(review.app, "PATCH", one, {"label": "renamed"})
    assert status == 200
    row = review.row()
    assert row is not None
    assert row[0] == "renamed"
    assert row[1] is not None, "a label PATCH erased the persisted end"
    assert (await review.served())[review.event_id].get("end_timestamp") is not None


@dataclass
class _LandedArchiveSpy(_ArchiveSpy):
    """An ``_ArchiveSpy`` whose write lands BEFORE its hold, as a real DB write
    does in its worker thread: cancelling the awaiting request does not undo it."""

    async def _write(self, event_id: int, row: dict[str, object] | None) -> None:
        self.rows[event_id] = row
        async with self.passing():
            pass


@pytest.mark.asyncio
async def test_live_end_cancelled_during_its_db_write_still_changes_memory() -> None:
    live = await _live()
    spy = _LandedArchiveSpy()
    live.collector._db = spy  # type: ignore[assignment]
    spy.hold()
    end = asyncio.create_task(asgi(live.app, "POST", f"{live.base}/end", {}))
    await settle()
    assert spy.pending == 1
    end.cancel()
    await settle()
    assert live.collector.mutation_lock().locked(), "cancellation released the lock early"
    spy.release()
    with pytest.raises(asyncio.CancelledError):
        await end
    stored = live.stored()
    assert stored is not None
    assert stored.end_timestamp is not None, "memory missed the end the DB holds"
    assert live.published_events()[-1]["end_timestamp"] is not None

    status, _ = await asgi(live.app, "PATCH", live.base, {"label": "renamed"})
    assert status == 200
    row = spy.rows[live.event_id]
    assert row is not None
    assert row["label"] == "renamed"
    assert row["end_timestamp"] is not None, "a label PATCH erased the persisted end"


@pytest.mark.asyncio
async def test_review_create_cancelled_during_its_archive_write_still_serves_the_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    review = await _review(tmp_path, monkeypatch)
    created_id = review.event_id + 1
    review.write.hold()
    create = asyncio.create_task(asgi(review.app, "POST", review.base, {"label": "new"}))
    await settle()
    assert review.write.pending == 1
    create.cancel()
    await settle()
    review.write.release()
    with pytest.raises(asyncio.CancelledError):
        await create
    assert review.row(created_id) == ("new", None)
    assert (await review.served())[created_id]["label"] == "new"


@pytest.mark.asyncio
async def test_review_revert_cancelled_during_its_archive_write_still_patches_the_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    review = await _review(tmp_path, monkeypatch)
    one = f"{review.base}/{review.event_id}"
    expect = (await review.served())[review.event_id]
    review.write.hold()
    revert = asyncio.create_task(
        asgi(
            review.app,
            "POST",
            f"{one}/revert",
            {"expect": expect, "write": {**expect, "label": "reverted"}},
        )
    )
    await settle()
    assert review.write.pending == 1
    revert.cancel()
    await settle()
    review.write.release()
    with pytest.raises(asyncio.CancelledError):
        await revert
    assert review.row() == ("reverted", None)
    assert (await review.served())[review.event_id]["label"] == "reverted"


@pytest.mark.asyncio
async def test_review_end_cancelled_while_waiting_for_the_lock_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    review = await _review(tmp_path, monkeypatch)
    one = f"{review.base}/{review.event_id}"
    review.write.hold()
    patch = asyncio.create_task(asgi(review.app, "PATCH", one, {"label": "first"}))
    await settle()
    assert review.write.pending == 1
    end = asyncio.create_task(asgi(review.app, "POST", f"{one}/end", {}))
    await settle()
    end.cancel()
    await settle()
    review.write.release()
    with pytest.raises(asyncio.CancelledError):
        await end
    status, _ = await patch
    assert status == 200
    await settle()
    assert review.row() == ("first", None), "a cancelled /end still wrote its end"
    assert (await review.served())[review.event_id].get("end_timestamp") is None
    assert not review.collector.mutation_lock().locked()


@pytest.mark.asyncio
async def test_live_end_cancelled_while_waiting_for_the_lock_writes_nothing() -> None:
    live = await _live()
    live.archive.hold()
    patch = asyncio.create_task(asgi(live.app, "PATCH", live.base, {"label": "first"}))
    await settle()
    assert live.archive.pending == 1
    end = asyncio.create_task(asgi(live.app, "POST", f"{live.base}/end", {}))
    await settle()
    end.cancel()
    await settle()
    live.archive.release()
    with pytest.raises(asyncio.CancelledError):
        await end
    status, _ = await patch
    assert status == 200
    await settle()
    stored = live.stored()
    assert stored is not None
    assert (stored.label, stored.end_timestamp) == ("first", None)
    row = live.archive.rows[live.event_id]
    assert row is not None
    assert row["end_timestamp"] is None, "a cancelled /end still wrote its end"


# ----------------------------------------------------------------------
# The archive is the truth in review mode (#941): a PATCH, an /end and a
# DELETE resolve against the archive's row, never the served document's
# snapshot, and the document is repaired to that row.
# ----------------------------------------------------------------------


def _second_server(review: _Review) -> _Review:
    """A second review server opened on the SAME archive, with its own document."""
    collector = MetricCollector(hosts=[])
    app = _build_app(collector, key="k", mode="review", slot=_db_slot(review.path, collector))
    return _Review(
        app,
        review.path,
        review.session_id,
        review.base,
        review.event_id,
        review.write,
        collector,
        review.read,
    )


def _edit_elsewhere(review: _Review, label: str) -> None:
    """Change the event's label through a second connection, as another writer does."""
    conn = sqlite3.connect(review.path)
    try:
        conn.execute("UPDATE events SET label = ? WHERE id = ?", (label, review.event_id))
        conn.commit()
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_two_review_servers_a_colour_patch_keeps_the_other_servers_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = await _review(tmp_path, monkeypatch)
    b = _second_server(a)
    one = f"{a.base}/{a.event_id}"
    status, _ = await asgi(a.app, "PATCH", one, {"label": "renamed-by-a"})
    assert status == 200
    status, patched = await asgi(b.app, "PATCH", one, {"color": "#112233"})

    assert status == 200
    assert patched is not None
    assert (patched["label"], patched["color"]) == ("renamed-by-a", "#112233")
    assert a.row() == ("renamed-by-a", None), "B wrote back its stale label"
    served = (await b.served())[a.event_id]
    assert (served["label"], served["color"]) == ("renamed-by-a", "#112233")


@pytest.mark.asyncio
async def test_two_review_servers_an_end_keeps_the_other_servers_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = await _review(tmp_path, monkeypatch)
    b = _second_server(a)
    one = f"{a.base}/{a.event_id}"
    status, _ = await asgi(a.app, "PATCH", one, {"label": "renamed-by-a"})
    assert status == 200
    status, ended = await asgi(b.app, "POST", f"{one}/end", {})

    assert status == 200
    assert ended is not None
    assert ended["label"] == "renamed-by-a"
    row = a.row()
    assert row is not None
    assert row[0] == "renamed-by-a", "B's /end wrote back its stale label"
    assert row[1] is not None
    served = (await b.served())[a.event_id]
    assert served["label"] == "renamed-by-a"
    assert served.get("end_timestamp") is not None


@pytest.mark.asyncio
async def test_a_changed_revert_repairs_the_document_so_a_colour_patch_keeps_the_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    review = await _review(tmp_path, monkeypatch)
    one = f"{review.base}/{review.event_id}"
    expect = (await review.served())[review.event_id]
    _edit_elsewhere(review, "edited-elsewhere")
    status, body = await asgi(
        review.app,
        "POST",
        f"{one}/revert",
        {"expect": expect, "write": {**expect, "label": "undone"}},
    )
    assert status == 409
    assert body is not None
    assert body["code"] == "changed"
    assert body["current"]["label"] == "edited-elsewhere"
    assert (await review.served())[review.event_id]["label"] == "edited-elsewhere", (
        "the 409 left the document's stale copy"
    )

    status, _ = await asgi(review.app, "PATCH", one, {"color": "#112233"})
    assert status == 200
    assert review.row() == ("edited-elsewhere", None), "the colour PATCH restored a stale label"


@pytest.mark.asyncio
async def test_a_change_between_read_and_write_answers_changed_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    review = await _review(tmp_path, monkeypatch)
    one = f"{review.base}/{review.event_id}"
    review.read.hold()
    patch = asyncio.create_task(asgi(review.app, "PATCH", one, {"color": "#112233"}))
    await settle()
    assert review.read.pending == 1, "the PATCH never read the archive row"
    _edit_elsewhere(review, "edited-between")
    review.read.release()
    status, body = await patch

    assert status == 409
    assert body is not None
    assert body["code"] == "changed"
    assert body["current"]["label"] == "edited-between"
    assert body["current"]["color"] == "#888888"
    assert review.row() == ("edited-between", None)
    conn = sqlite3.connect(review.path)
    try:
        (color,) = conn.execute(
            "SELECT color FROM events WHERE id = ?", (review.event_id,)
        ).fetchone()
    finally:
        conn.close()
    assert color == "#888888", "the PATCH wrote over a row it never read"
    assert (await review.served())[review.event_id]["label"] == "edited-between"


@pytest.mark.asyncio
async def test_a_row_gone_from_the_archive_answers_404_and_drops_it_from_the_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    review = await _review(tmp_path, monkeypatch)
    conn = sqlite3.connect(review.path)
    try:
        conn.execute("DELETE FROM events WHERE id = ?", (review.event_id,))
        conn.commit()
    finally:
        conn.close()
    status, _ = await asgi(review.app, "PATCH", f"{review.base}/{review.event_id}", {"label": "x"})

    assert status == 404
    assert review.event_id not in await review.served()


@pytest.mark.asyncio
async def test_review_end_cancelled_during_its_archive_read_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    review = await _review(tmp_path, monkeypatch)
    one = f"{review.base}/{review.event_id}"
    review.read.hold()
    end = asyncio.create_task(asgi(review.app, "POST", f"{one}/end", {}))
    await settle()
    assert review.read.pending == 1
    end.cancel()
    await settle()
    review.read.release()
    with pytest.raises(asyncio.CancelledError):
        await end
    assert review.row() == ("span", None), "a /end cancelled before its write wrote"
    assert (await review.served())[review.event_id].get("end_timestamp") is None
    assert not review.collector.mutation_lock().locked()


@pytest.mark.asyncio
async def test_a_mutation_returns_before_the_next_one_runs() -> None:
    """The caller, not the inner section, releases the lock (event_ops.run_locked_in_phases).

    So no other mutation runs, and publishes, between a mutation's section and
    its caller's return: a route's response stamp then follows its own publish
    and no later one. With no DB the next mutation would publish in its first
    step, so a lock released by the inner task would show here as two
    fragments when the first caller resumes.
    """
    collector = MetricCollector(hosts=[], parsers=[])
    collector.session_id = "s1"
    event = await collector.add_event(label="span")
    queue = collector.subscribe()
    published_on_return: list[int] = []

    async def rename(label: str) -> None:
        await collector.mutate_event(
            event_id=event.id, resolve=lambda current: dataclasses.replace(current, label=label)
        )
        published_on_return.append(queue.qsize())

    await asyncio.gather(rename("a"), rename("b"))
    assert published_on_return == [1, 2]
