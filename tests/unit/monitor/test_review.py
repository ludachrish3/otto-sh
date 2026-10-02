"""Review mode in the library: load a saved export, serve it."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from otto.config.repo import MonitorSettings
from otto.monitor.errors import MonitorTlsError, ReviewSourceError
from otto.monitor.review import load_review_document, serve_review


def test_unsupported_suffix_is_refused_naming_the_suffix(tmp_path):
    src = tmp_path / "x.csv"
    src.write_text("a,b")
    with pytest.raises(ReviewSourceError, match=r"suffix '\.csv'") as e:
        load_review_document(src)
    assert e.value.field == "source"


def test_invalid_json_export_is_refused(tmp_path):
    src = tmp_path / "x.json"
    src.write_text('{"format": 99}')
    with pytest.raises(ReviewSourceError, match="not a valid format:1 monitor export"):
        load_review_document(src)


def test_valid_json_export_loads(tmp_path):
    src = tmp_path / "x.json"
    src.write_text('{"format": 1, "sessions": []}')
    assert load_review_document(src).sessions == []


def test_unsupported_db_is_refused(tmp_path):
    src = tmp_path / "x.db"
    src.write_bytes(b"garbage")
    with pytest.raises(ReviewSourceError, match="not a monitor database"):
        load_review_document(src)


@pytest.mark.asyncio
async def test_serve_review_resolves_tls_from_the_given_repos_before_serving(tmp_path):
    src = tmp_path / "x.json"
    src.write_text('{"format": 1, "sessions": []}')
    bad = SimpleNamespace(
        name="r", monitor_settings=MonitorSettings(tls_cert=tmp_path / "missing.pem")
    )
    with (
        patch("otto.monitor.server.MonitorServer") as server_cls,
        pytest.raises(MonitorTlsError),
    ):
        await serve_review(src, repos=[bad])
    server_cls.assert_not_called()


@pytest.mark.asyncio
async def test_serve_review_serves_a_db_with_its_archive_path(tmp_path):
    src = tmp_path / "x.db"
    server = MagicMock(serve=AsyncMock())
    with (
        patch("otto.monitor.review.load_review_document", return_value=MagicMock()),
        patch("otto.monitor.server.MonitorServer", return_value=server) as server_cls,
    ):
        await serve_review(src, repos=[])
    kwargs = server_cls.call_args.kwargs
    assert (kwargs["mode"], kwargs["source_name"], kwargs["archive_path"]) == (
        "review",
        "x.db",
        src,
    )
    assert kwargs["tls_cert"] is None
    server.serve.assert_awaited_once()


@pytest.mark.asyncio
async def test_serve_review_keeps_a_json_source_read_only(tmp_path):
    src = tmp_path / "x.json"
    src.write_text('{"format": 1, "sessions": []}')
    server = MagicMock(serve=AsyncMock())
    with patch("otto.monitor.server.MonitorServer", return_value=server) as server_cls:
        await serve_review(src, repos=[])
    assert server_cls.call_args.kwargs["archive_path"] is None


@pytest.mark.asyncio
async def test_serve_review_hands_the_server_the_declared_cert_and_key(tmp_path, tls_pair):
    cert, key = tls_pair
    src = tmp_path / "x.json"
    src.write_text('{"format": 1, "sessions": []}')
    repo = SimpleNamespace(name="r", monitor_settings=MonitorSettings(tls_cert=cert, tls_key=key))
    server = MagicMock(serve=AsyncMock())
    with patch("otto.monitor.server.MonitorServer", return_value=server) as server_cls:
        await serve_review(src, repos=[repo])
    kwargs = server_cls.call_args.kwargs
    assert (kwargs["tls_cert"], kwargs["tls_key"]) == (cert, key)
