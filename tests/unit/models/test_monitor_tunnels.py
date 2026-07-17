"""TunnelRecord + the tunnels fields (spec 2026-07-16 §1)."""

import pytest
from pydantic import ValidationError

from otto.models.monitor import (
    LinkSnapshot,
    MonitorSessionFragment,
    SessionRecord,
    TunnelRecord,
)


def _record(**overrides: object) -> TunnelRecord:
    base: dict[str, object] = {
        "id": "tun-abc123def456-15001",
        "protocol": "udp",
        "service_port": 15001,
        "hops": ["edge-gw", "core-01", "db-01"],
        "status": "ok",
        "carriers_present": 6,
        "carriers_expected": 6,
        "age_seconds": 120.0,
    }
    base.update(overrides)
    return TunnelRecord.model_validate(base)


def test_tunnel_record_round_trips() -> None:
    rec = _record()
    assert rec.hops == ["edge-gw", "core-01", "db-01"]
    assert TunnelRecord.model_validate(rec.model_dump(mode="json")) == rec


def test_tunnel_record_rejects_single_hop() -> None:
    with pytest.raises(ValidationError):
        _record(hops=["edge-gw"])


def test_tunnel_record_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        _record(status="down")


def test_session_record_tunnels_default_empty() -> None:
    from datetime import datetime, timezone

    s = SessionRecord(id="s1", start=datetime(2026, 7, 16, tzinfo=timezone.utc))
    assert s.tunnels == []


def test_fragment_tunnels_absent_is_none_and_empty_is_empty() -> None:
    """Replace semantics on the wire: None = no update, [] = now empty."""
    assert MonitorSessionFragment(session="s1").tunnels is None
    frag = MonitorSessionFragment.model_validate({"session": "s1", "tunnels": []})
    assert frag.tunnels == []


def test_link_snapshot_rejects_dynamic_provenance() -> None:
    """'dynamic' left the snapshot contract — tunnels are first-class now."""
    with pytest.raises(ValidationError):
        LinkSnapshot.model_validate(
            {
                "id": "l1",
                "endpoints": [
                    {"host": "a", "ip": "10.0.0.1"},
                    {"host": "b", "ip": "10.0.0.2"},
                ],
                "provenance": "dynamic",
            }
        )
