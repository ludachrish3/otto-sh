"""The review source holder: one owned copy of the document, its ids, its state and its frames."""

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from starlette.requests import Request

from otto.models.monitor import EventRecord, MonitorExport, SessionRecord
from otto.monitor import review_source
from otto.monitor.collector import MetricCollector
from otto.monitor.review import parse_review_json
from otto.monitor.review_source import (
    ReviewSource,
    SourceChanged,
    SourceSlot,
    UnknownSessionError,
    UploadTooLarge,
    read_upload,
)
from tests._fixtures.paths import CHECKOUT_ROOT

_T0 = datetime(2026, 7, 1, 8, 1, tzinfo=timezone.utc)
_STAMP = "2026-07-01T09:00:00+00:00"


class _Tab:
    """A real collector and one subscriber: what a source publishes, as a tab receives it."""

    def __init__(self) -> None:
        self.collector = MetricCollector(hosts=[], parsers=[])
        self._queue: asyncio.Queue[dict[str, Any]] = self.collector.subscribe()

    @property
    def frames(self) -> list[dict[str, Any]]:
        """Every frame published since the last read, in order."""
        frames: list[dict[str, Any]] = []
        while not self._queue.empty():
            frames.append(self._queue.get_nowait())
        return frames


def _event(event_id: int, label: str) -> EventRecord:
    return EventRecord(id=event_id, timestamp=_T0, label=label)


def _one_session_document() -> MonitorExport:
    return MonitorExport(
        format=1, sessions=[SessionRecord(id="a", start=_T0, events=[_event(1, "first")])]
    )


def _source(
    document: MonitorExport | None = None,
    *,
    name: str = "x.json",
    archive_path: Path | None = None,
    publish: _Tab | None = None,
) -> ReviewSource:
    return ReviewSource(
        _one_session_document() if document is None else document,
        name=name,
        archive_path=archive_path,
        publish=(_Tab() if publish is None else publish).collector.publish,
    )


def test_body_is_rebuilt_only_after_a_change(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[MonitorExport] = []
    real = review_source.document_json

    def counting(export: MonitorExport) -> str:
        calls.append(export)
        return real(export)

    monkeypatch.setattr(review_source, "document_json", counting)
    source = _source()
    assert calls == [], "the body is built when first requested, not at construction"
    first = source.body()
    assert source.body() == first
    assert len(calls) == 1

    source.patch_event("a", _event(2, "second"), counted=True)
    source.patch_event("a", _event(2, "renamed"), counted=True)
    assert len(calls) == 1, "a change invalidates the body without rebuilding it"
    changed = source.body()
    assert changed != first
    assert "renamed" in changed
    source.body()
    assert len(calls) == 2

    source.drop_event("a", 2, counted=True)
    assert source.body() == first
    assert len(calls) == 3


def test_id_counter_seeds_above_repeated_ids() -> None:
    path = CHECKOUT_ROOT / "web" / "fixtures" / "repeated-ids.json"
    document = parse_review_json(path.read_bytes(), origin=str(path))
    source = _source(document, name=path.name)
    sid = "2026-07-01T08-00-00-repeated"
    assert source.view_only[sid] == frozenset({3})
    assert source.ids[sid].take() == 10


def test_holder_owns_a_deep_copy() -> None:
    """Two sessions that share one events list (``model_copy`` is shallow) stay apart."""
    first = SessionRecord(id="a", start=_T0, events=[_event(1, "first")])
    second = first.model_copy(update={"id": "b", "label": "second"})
    assert first.events is second.events, "the setup must share the list"
    caller = MonitorExport(format=1, sessions=[first, second])

    source = _source(caller)
    source.patch_event("a", _event(2, "added"), counted=True)
    source.patch_event("a", _event(1, "replaced"), counted=True)

    held = MonitorExport.model_validate_json(source.body())
    assert [e.label for e in held.sessions[0].events] == ["replaced", "added"]
    assert [e.label for e in held.sessions[1].events] == ["first"]
    assert [e.label for e in caller.sessions[0].events] == ["first"]
    assert [e.label for e in caller.sessions[1].events] == ["first"]


def test_a_found_event_is_a_copy() -> None:
    """Changing what ``find_event`` returns never changes the held document: the one
    change seam is ``patch_event``/``drop_event``, which keeps the body in step."""
    source = _source()
    before = source.body()
    found = source.find_event("a", 1)
    assert found is not None
    found.label = "changed behind the seam"
    assert source.body() == before
    again = source.find_event("a", 1)
    assert again is not None
    assert again.label == "first"


def test_revision_counts_only_counted_changes() -> None:
    source = _source()
    source.patch_event("a", _event(2, "counted"), counted=True)
    source.patch_event("a", _event(2, "repair"), counted=False)
    source.drop_event("a", 2, counted=True)
    state = source.state(_STAMP)
    assert (state.revision, state.unsaved) == (2, 2)


def test_a_counted_change_publishes_its_fragment_then_the_source_frame() -> None:
    tab = _Tab()
    source = _source(publish=tab)
    source.patch_event("a", _event(2, "added"), counted=True)
    source.drop_event("a", 1, counted=True)
    added, added_state, dropped, dropped_state = tab.frames
    assert added["session"] == "a"
    assert [e["label"] for e in added["events"]] == ["added"]
    assert dropped["session"] == "a"
    assert dropped["deleted_event_ids"] == [1]
    for frame, revision in ((added_state, 1), (dropped_state, 2)):
        assert set(frame) == {"format", "source_state", "observed_at"}
        assert frame["source_state"]["revision"] == revision
        assert frame["source_state"]["token"] == source.token
        assert frame["observed_at"] == frame["source_state"]["observed_at"]


def test_an_uncounted_change_publishes_only_its_fragment() -> None:
    tab = _Tab()
    source = _source(publish=tab)
    source.patch_event("a", _event(1, "repaired"), counted=False)
    [fragment] = tab.frames
    assert [e["label"] for e in fragment["events"]] == ["repaired"]


def test_acknowledge_clamps_to_the_revision_and_never_lowers_saved() -> None:
    source = _source()
    for label in ("one", "two", "three"):
        source.patch_event("a", _event(2, label), counted=True)
    source.acknowledge(2)
    assert source.unsaved == 1
    source.acknowledge(1)
    assert source.unsaved == 1, "an older acknowledgement never raises the unsaved count"
    source.acknowledge(99)
    assert source.unsaved == 0, "an acknowledgement past the revision covers only the revision"


def test_state_names_the_source() -> None:
    document = _one_session_document()
    json_source = _source(document, name="x.json")
    db_source = _source(document, name="x.db", archive_path=CHECKOUT_ROOT / "x.db")
    json_state = json_source.state(_STAMP)
    db_state = db_source.state(_STAMP)
    assert (json_state.mode, json_state.kind, json_state.source) == ("review", "json", "x.json")
    assert json_state.editable is True
    assert (db_state.kind, db_state.editable) == ("db", True)
    assert json_state.token == json_source.token
    assert json_source.token != db_source.token
    assert json_state.observed_at == datetime.fromisoformat(_STAMP)


def test_lookups() -> None:
    source = _source()
    found = source.find_event("a", 1)
    assert found is not None
    assert found.label == "first"
    assert source.find_event("a", 2) is None
    assert source.find_event("missing", 1) is None
    assert source.require_session("a").id == "a"
    with pytest.raises(UnknownSessionError, match="unknown session 'missing'"):
        source.require_session("missing")


def test_slot_replace() -> None:
    slot = SourceSlot(None)
    assert slot.current is None
    source = _source()
    slot.replace(source)
    assert slot.current is source


def test_slot_require_answers_the_served_source_or_refuses_with_its_state() -> None:
    source = _source()
    slot = SourceSlot(source)
    assert slot.require(source.token) is source
    for token in (None, "wrong"):
        with pytest.raises(SourceChanged) as caught:
            slot.require(token)
        assert caught.value.state.token == source.token
        assert caught.value.state.kind == "json"


_LIMIT = 10
"""The small limit the exact-limit tests read under (``read_upload``'s ``limit``).

That the route reads under the real ``MAX_SOURCE_UPLOAD_BYTES`` is proven at
the route, without allocating it (test_server.py's
``test_the_route_reads_under_the_real_upload_limit``)."""


def _upload_request(chunks: list[bytes], *, content_length: int | None) -> Request:
    """A POST whose body arrives as *chunks*, declaring *content_length* (``None``: chunked)."""
    messages: list[dict[str, Any]] = [
        {"type": "http.request", "body": chunk, "more_body": True} for chunk in chunks
    ]
    messages.append({"type": "http.request", "body": b"", "more_body": False})

    async def receive() -> dict[str, Any]:
        return messages.pop(0)

    headers = [] if content_length is None else [(b"content-length", str(content_length).encode())]
    return Request({"type": "http", "method": "POST", "path": "/", "headers": headers}, receive)


def _at_limit() -> list[bytes]:
    """Exactly ``_LIMIT`` bytes, in more than one chunk."""
    return [b"x" * 4, b"x" * 4, b"x" * (_LIMIT - 8)]


@pytest.mark.asyncio
@pytest.mark.parametrize("declared", [True, False], ids=["content-length", "chunked"])
async def test_an_upload_of_exactly_the_limit_is_read(declared: bool) -> None:
    chunks = _at_limit()
    request = _upload_request(chunks, content_length=_LIMIT if declared else None)
    body = await read_upload(request, limit=_LIMIT)
    assert body == b"x" * _LIMIT


@pytest.mark.asyncio
@pytest.mark.parametrize("declared", [True, False], ids=["content-length", "chunked"])
async def test_an_upload_one_byte_past_the_limit_is_refused(declared: bool) -> None:
    chunks = [*_at_limit(), b"x"]
    request = _upload_request(chunks, content_length=_LIMIT + 1 if declared else None)
    with pytest.raises(UploadTooLarge) as refused:
        await read_upload(request, limit=_LIMIT)
    assert refused.value.limit == _LIMIT
