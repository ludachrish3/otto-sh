"""Shared v2-archive builder for tests exercising archive-edit/review-mode routes.

Used by ``test_archive_edit.py`` (Task 4: the ``archive_edit`` functions
themselves) and ``test_server.py`` (Task 5: review-mode server routes) — the
same real, finalized, one-session archive (no degraded ``lab_json="{}"``/
``meta_json="{}"`` scaffold). A bare top-level module, not a package member:
``tests/unit/monitor`` has no ``__init__.py``, so pytest's default
"prepend" import mode puts this directory on ``sys.path`` for its siblings,
letting them import it as ``from _archive import _make_archive``.
"""

import asyncio
from datetime import datetime, timedelta, timezone

from otto.models import LabSnapshot
from otto.monitor.collector import MetricCollector
from otto.monitor.export import build_session_metric_db
from otto.monitor.session import new_frame

_T0 = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)


def _make_archive(tmp_path) -> tuple[str, str]:
    """A real finalized v2 archive with one session; returns (path, session_id)."""
    path = str(tmp_path / "a.db")
    frame = new_frame(label=None, note=None)
    db = build_session_metric_db(
        path, frame, LabSnapshot(), MetricCollector(hosts=[]), interval=5.0
    )

    async def _build() -> None:
        await db.open()
        await db.finalize(_T0 + timedelta(minutes=10))
        await db.close()

    asyncio.run(_build())
    return path, frame.id
