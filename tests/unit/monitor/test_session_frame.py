"""SessionFrame — identity and stamping (spec 2026-07-12 §Sessionization)."""

from datetime import datetime, timezone

from otto.monitor.session import SessionFrame, new_frame


def test_new_frame_id_is_utc_timestamp_slug():
    now = datetime(2026, 7, 12, 14, 30, 5, tzinfo=timezone.utc)
    frame: SessionFrame = new_frame(label="fan fix", note=None, now=now)
    assert frame.id == "2026-07-12T14-30-05Z"
    assert frame.start == now
    assert frame.end is None
    assert frame.label == "fan fix"
    assert frame.note is None


def test_new_frame_defaults_to_wall_clock_utc():
    frame = new_frame(label=None, note=None)
    assert frame.start.tzinfo is not None
    assert frame.id.endswith("Z")
