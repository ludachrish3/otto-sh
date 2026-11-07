"""Two live event mutations racing on one event never lose either (#868).

The collector writes an event change to its DB before it changes memory and
publishes (so a snapshot never sees a change with an earlier stamp). That
await opens a window: a second request that read the event before the first
one's write finished would merge onto stale fields and undo it. Every event
mutation, read and merge included, is therefore serialised through the
collector's one entry point. These tests hold a DB write open, race a second
request into the window, and check memory, the archive and the published
record.
"""

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest

from otto.models import LabSnapshot
from otto.models.monitor import MonitorSessionFragment
from otto.monitor.collector import MetricCollector
from otto.monitor.events import MonitorEvent
from otto.monitor.server import _build_app
from otto.monitor.session import new_frame


@dataclass
class _ArchiveSpy:
    """Quacks MetricDB's event writes, recording each as the archive's row.

    ``hold()`` makes the NEXT write wait until ``release()``; ``fail_next``
    makes the next write raise, as a locked or full disk would.
    """

    rows: dict[int, dict[str, object] | None] = field(default_factory=dict)
    pending: int = 0
    fail_next: bool = False
    _gate: asyncio.Event | None = None
    _armed: bool = False

    def hold(self) -> None:
        self._gate = asyncio.Event()
        self._armed = True

    def release(self) -> None:
        assert self._gate is not None
        self._gate.set()

    async def _write(self, event_id: int, row: dict[str, object] | None) -> None:
        gate = self._gate if self._armed else None
        self._armed = False
        self.pending += 1
        try:
            if gate is not None:
                await gate.wait()
            if self.fail_next:
                self.fail_next = False
                raise OSError("disk full")
            self.rows[event_id] = row
        finally:
            self.pending -= 1

    async def write_event(self, event: MonitorEvent) -> int:
        return 0

    async def update_event(self, event: MonitorEvent) -> None:
        await self._write(event.id, event.to_dict())

    async def delete_event(self, event_id: int) -> None:
        await self._write(event_id, None)


async def _asgi(app, method: str, path: str, body: dict[str, object] | None = None):
    """One in-loop request against *app*: ``(status, json-or-None)``."""
    payload = json.dumps(body).encode() if body is not None else b""
    status: int | None = None
    chunks: list[bytes] = []

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    async def send(message):
        nonlocal status
        if message["type"] == "http.response.start":
            status = message["status"]
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))

    headers = [(b"content-type", b"application/json")] if body is not None else []
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "path": path,
        "query_string": b"key=k",
        "headers": headers,
        "root_path": "",
        "server": ("127.0.0.1", 8000),
    }
    await app(scope, receive, send)
    raw = b"".join(chunks)
    return status, (json.loads(raw) if raw else None)


async def _settle() -> None:
    """Let every runnable task advance as far as it can (no I/O in play)."""
    for _ in range(50):
        await asyncio.sleep(0)


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
    end = asyncio.create_task(_asgi(live.app, "POST", f"{live.base}/end", {}))
    await _settle()
    assert live.archive.pending == 1  # /end now waits on its archive write
    patch = asyncio.create_task(_asgi(live.app, "PATCH", live.base, {"label": "renamed"}))
    await _settle()
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
    patch = asyncio.create_task(_asgi(live.app, "PATCH", live.base, {"label": "renamed"}))
    await _settle()
    assert live.archive.pending == 1
    end = asyncio.create_task(_asgi(live.app, "POST", f"{live.base}/end", {}))
    await _settle()
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
    delete = asyncio.create_task(_asgi(live.app, "DELETE", live.base))
    await _settle()
    assert live.archive.pending == 1
    patch = asyncio.create_task(_asgi(live.app, "PATCH", live.base, {"label": "renamed"}))
    await _settle()
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
            live.event_id,
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
        _asgi(live.app, "PATCH", live.base, {"label": "kept"}), timeout=5
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
