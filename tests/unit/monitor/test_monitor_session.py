"""MonitorSession: one owner for building, opening, finishing and exporting a session."""

import fcntl
import json
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from otto.host.element import Element
from otto.host.factory import create_host_from_dict
from otto.host.login_proxy import Cred
from otto.host.unix_host import UnixHost
from otto.link.model import Link, LinkEndpoint
from otto.logger.mode import LogMode
from otto.monitor.collector import MonitorTarget
from otto.monitor.errors import MonitorInputError
from otto.monitor.export import build_db_export
from otto.monitor.session import MonitorSession


def _unix(name: str = "box") -> UnixHost:
    return UnixHost(
        ip="10.0.0.1",
        element=Element(name),
        creds=[Cred(login="a", password="b")],
        log=LogMode.NORMAL,
    )


def _console(name: str = "z") -> object:
    return create_host_from_dict(
        {"ip": "192.0.2.1", "os_type": "embedded", "command_frame": "zephyr"},
        element=Element(name),
    )


class TestBuildRefuses:
    def test_neither_hosts_nor_targets(self):
        with pytest.raises(MonitorInputError, match="exactly one of hosts or targets") as e:
            MonitorSession.build(interval=5, owns_hosts=True)
        assert e.value.field == "hosts"

    def test_both_hosts_and_targets(self):
        host = _unix()
        with pytest.raises(MonitorInputError, match="exactly one of hosts or targets"):
            MonitorSession.build(
                [host],
                targets=[MonitorTarget(host=host, parsers={})],
                interval=5,
                owns_hosts=True,
            )

    def test_no_hosts(self):
        with pytest.raises(MonitorInputError, match="no hosts to monitor") as e:
            MonitorSession.build([], interval=5, owns_hosts=True)
        assert e.value.field == "hosts"

    def test_interval_below_the_floor(self):
        with pytest.raises(MonitorInputError, match=r"at least 1\.0s") as e:
            MonitorSession.build([_unix()], interval=0.5, owns_hosts=True)
        assert e.value.field == "interval"

    def test_a_host_that_cannot_be_sampled_is_named(self):
        with pytest.raises(MonitorInputError, match="cannot monitor z") as e:
            MonitorSession.build([_unix(), _console("z")], interval=5, owns_hosts=True)
        assert e.value.field == "hosts"

    def test_a_shell_target_on_a_console_host_is_named(self):
        target = MonitorTarget(host=_console("z"), parsers={})
        with pytest.raises(MonitorInputError, match="cannot monitor z") as e:
            MonitorSession.build(targets=[target], interval=5, owns_hosts=False)
        assert e.value.field == "targets"


def test_build_does_no_io(tmp_path):
    db = tmp_path / "m.db"
    MonitorSession.build([_unix()], interval=5, db_path=db, owns_hosts=True)
    assert not db.exists()


def test_build_stamps_identity_and_snapshots_the_hosts():
    session = MonitorSession.build(
        [_unix("a")], interval=timedelta(seconds=2), label="L", note="N", owns_hosts=True
    )
    assert (session.frame.label, session.frame.note) == ("L", "N")
    assert [h.id for h in session.lab.hosts] == ["a"]
    assert session.interval == timedelta(seconds=2)


@pytest.mark.asyncio
async def test_spawn_before_open_fails_loud():
    session = MonitorSession.build([_unix()], interval=5, owns_hosts=True)
    with pytest.raises(RuntimeError, match="before open"):
        session.spawn()


@pytest.mark.asyncio
async def test_owned_hosts_are_closed_borrowed_hosts_are_not():
    owned = MonitorSession.build([_unix("a")], interval=5, owns_hosts=True)
    borrowed = MonitorSession.build([_unix("b")], interval=5, owns_hosts=False)
    for s in (owned, borrowed):
        s.collector.close = AsyncMock()  # type: ignore[method-assign]
        s.collector.close_db = AsyncMock()  # type: ignore[method-assign]
        await s.open()
        await s.finish()
    owned.collector.close.assert_awaited_once()
    owned.collector.close_db.assert_not_awaited()
    borrowed.collector.close_db.assert_awaited_once()
    borrowed.collector.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_finish_is_idempotent_and_safe_without_open():
    session = MonitorSession.build([_unix()], interval=5, owns_hosts=False)
    session.collector.close_db = AsyncMock()  # type: ignore[method-assign]
    await session.finish()
    await session.finish()
    session.collector.close_db.assert_awaited_once()
    assert session.frame.end is not None


@pytest.mark.asyncio
async def test_finish_writes_the_json_export(tmp_path):
    out = tmp_path / "nested" / "monitor.json"
    session = MonitorSession.build([_unix()], interval=5, export_path=out, owns_hosts=False)
    async with session:
        pass
    doc = json.loads(out.read_text())
    assert doc["format"] == 1
    assert doc["sessions"][0]["id"] == session.frame.id
    assert doc["sessions"][0]["end"] is not None


@pytest.mark.asyncio
async def test_archive_carries_the_interval_and_is_stamped_on_finish(tmp_path):
    db = tmp_path / "m.db"
    session = MonitorSession.build([_unix()], interval=3, db_path=db, owns_hosts=False)
    async with session:
        assert db.exists()
    sess = build_db_export(str(db)).sessions[0]
    assert sess.meta.interval == 3.0
    assert sess.end is not None


@pytest.mark.asyncio
async def test_spawned_task_runs_the_collector_at_the_session_interval():
    session = MonitorSession.build([_unix()], interval=2, owns_hosts=False)
    session.collector.run = AsyncMock()  # type: ignore[method-assign]
    async with session:
        task = session.spawn()
        await task
    session.collector.run.assert_awaited_once_with(timedelta(seconds=2))


def test_build_keeps_only_declared_links_inside_the_session():
    a, b = _unix("a"), _unix("b")
    inside = Link(a=LinkEndpoint(host="a"), b=LinkEndpoint(host="b"), name="inside")
    outside = Link(a=LinkEndpoint(host="a"), b=LinkEndpoint(host="ghost"), name="outside")
    session = MonitorSession.build([a, b], interval=5, declared=[inside, outside], owns_hosts=False)
    ids = [link.id for link in session.lab.links]
    assert inside.id in ids
    assert outside.id not in ids


@pytest.mark.asyncio
async def test_open_is_idempotent(tmp_path):
    session = MonitorSession.build(
        [_unix()], interval=5, db_path=tmp_path / "m.db", owns_hosts=False
    )
    async with session:
        await session.open()


@pytest.mark.asyncio
async def test_finish_after_a_failed_open_still_stamps_exports_and_closes(tmp_path):
    db = tmp_path / "m.db"
    out = tmp_path / "monitor.json"
    session = MonitorSession.build(
        [_unix()], interval=5, db_path=db, export_path=out, owns_hosts=False
    )
    fd = Path(f"{db}.lock").open("w")  # noqa: SIM115 — held for the test's duration
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="already writing"):
            await session.open()
        await session.finish()
    finally:
        fd.close()
    assert session.frame.end is not None
    assert json.loads(out.read_text())["sessions"][0]["end"] is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("owns", [True, False])
async def test_a_failing_export_still_closes_and_propagates(tmp_path, owns):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    session = MonitorSession.build(
        [_unix()], interval=5, export_path=blocker / "monitor.json", owns_hosts=owns
    )
    session.collector.close = AsyncMock()  # type: ignore[method-assign]
    session.collector.close_db = AsyncMock()  # type: ignore[method-assign]
    await session.open()
    with pytest.raises(OSError, match=r"Not a directory|exists"):
        await session.finish()
    closer = session.collector.close if owns else session.collector.close_db
    closer.assert_awaited_once()
