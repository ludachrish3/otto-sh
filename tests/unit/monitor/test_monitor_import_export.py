"""
Integration tests for the monitor JSON export/import round-trip.

Covers the full pipeline:
  - Build a "live-like" collector with rich data (multi-series charts, events, spans)
  - Export to JSON
  - Import from JSON and verify the backend state is identical to the original
  - Re-export and verify the second JSON matches the first (idempotent export)

Multi-series parsers (LoadParser, TopCpuParser) are represented so that
chart_map preservation — the fix that makes historical grouping work — is
exercised directly.
"""

import json
from collections import deque
from datetime import datetime
from pathlib import Path

import pytest

from otto.monitor.collector import MetricCollector
from otto.monitor.parsers import TopCpuParser, LoadParser

pytestmark = pytest.mark.asyncio


# ── Shared timestamps ─────────────────────────────────────────────────────────

T0 = datetime(2024, 6, 1, 12, 0, 0)
T1 = datetime(2024, 6, 1, 12, 0, 5)
T2 = datetime(2024, 6, 1, 12, 0, 10)

EV_TS       = datetime(2024, 6, 1, 12, 0, 2)
SPAN_START  = datetime(2024, 6, 1, 12, 0, 3)
SPAN_END    = datetime(2024, 6, 1, 12, 0, 8)

HOST = 'host1'


# ── Builder ───────────────────────────────────────────────────────────────────

async def _make_live_collector() -> MetricCollector:
    """
    Build a MetricCollector pre-populated as if a live collection run had produced:
      - Single-series CPU chart
      - Multi-series Load chart (1m / 5m / 15m on one plot)
      - Per-process CPU series with rich metadata (proc/python)
      - One instantaneous event
      - One span event (start + end timestamp)
    """
    collector = MetricCollector(
        hosts=[],
        parsers=[TopCpuParser(), LoadParser()],
    )

    def _add(label: str, chart: str, values: list[tuple[datetime, float]],
             meta: object = None) -> None:
        key = f'{HOST}/{label}'
        if key not in collector._series:
            collector._series[key] = deque()
        for ts, val in values:
            collector._series[key].append((ts, val, meta))
        collector._chart_map[label] = chart

    # Single-series: overall CPU usage
    _add('Overall CPU', 'CPU', [(T0, 10.5), (T1, 12.3), (T2, 9.8)])

    # Multi-series: all three load averages share the 'Load' chart group
    _add('Load (1m)',  'Load', [(T0, 0.52), (T1, 0.61), (T2, 0.48)])
    _add('Load (5m)',  'Load', [(T0, 0.58), (T1, 0.57), (T2, 0.55)])
    _add('Load (15m)', 'Load', [(T0, 0.59), (T1, 0.60), (T2, 0.58)])

    # Per-process: proc/1234 with hover metadata
    proc_meta = {
        'Command': 'python3 script.py', 'User': 'root', 'Mem': '1.5%',
        'RSS': '4.0 KB', 'Stat': 'S', 'CPU Time': '0:01.00',
    }
    _add('proc/1234', 'CPU',
         [(T0, 5.2), (T1, 4.8), (T2, 6.1)], meta=proc_meta)

    # Instantaneous event
    await collector.add_event(
        label='test start',
        timestamp=EV_TS,
        color='#888888',
        source='auto',
        dash='dash',
    )

    # Span event (start → end)
    await collector.add_event(
        label='test window',
        timestamp=SPAN_START,
        color='#2ca02c',
        source='user_code',
        dash='solid',
        end_timestamp=SPAN_END,
    )

    return collector


# ── Helpers ───────────────────────────────────────────────────────────────────

def _sorted_metrics(data: dict) -> list[dict]:
    """Return the metrics list sorted by (host, label, timestamp) for stable comparison."""
    return sorted(data['metrics'], key=lambda p: (p.get('host', ''), p['label'], p['timestamp']))


def _assert_collectors_equal(original: MetricCollector, loaded: MetricCollector) -> None:
    """Assert that two collectors hold logically identical state."""
    orig_series  = original.get_series()
    loaded_series = loaded.get_series()

    assert set(loaded_series.keys()) == set(orig_series.keys()), \
        f'Series keys differ.\nExpected: {sorted(orig_series)}\nGot:      {sorted(loaded_series)}'

    for key in orig_series:
        orig_pts   = orig_series[key]
        loaded_pts = loaded_series[key]
        assert len(loaded_pts) == len(orig_pts), \
            f'Point count mismatch for series {key!r}: {len(orig_pts)} → {len(loaded_pts)}'
        for (ots, oval, ometa), (lts, lval, lmeta) in zip(orig_pts, loaded_pts):
            assert lts   == ots,                     f'Timestamp mismatch in {key!r}'
            assert lval  == pytest.approx(oval),     f'Value mismatch in {key!r}'
            assert lmeta == ometa,                   f'Meta mismatch in {key!r}'

    assert loaded.get_chart_map() == original.get_chart_map(), \
        'chart_map mismatch after import'

    orig_events   = original.get_events()
    loaded_events = loaded.get_events()
    assert len(loaded_events) == len(orig_events), \
        f'Event count mismatch: {len(orig_events)} → {len(loaded_events)}'
    for oe, le in zip(orig_events, loaded_events):
        assert le.label         == oe.label
        assert le.color         == oe.color
        assert le.dash          == oe.dash
        assert le.source        == oe.source
        assert le.timestamp     == oe.timestamp
        assert le.end_timestamp == oe.end_timestamp


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestExportImportRoundTrip:
    """Export a rich live-like collector, import it, verify state is identical."""

    async def test_series_keys_preserved(self, tmp_path):
        original = await _make_live_collector()
        path = str(tmp_path / 'export.json')
        original.export_json(path)
        loaded = MetricCollector.from_json(path)

        expected_keys = {
            f'{HOST}/Overall CPU',
            f'{HOST}/Load (1m)', f'{HOST}/Load (5m)', f'{HOST}/Load (15m)',
            f'{HOST}/proc/1234',
        }
        assert set(loaded.get_series().keys()) == expected_keys

    async def test_single_series_values_preserved(self, tmp_path):
        original = await _make_live_collector()
        path = str(tmp_path / 'export.json')
        original.export_json(path)
        loaded = MetricCollector.from_json(path)

        pts = loaded.get_series()[f'{HOST}/Overall CPU']
        assert [v for _, v, _ in pts] == pytest.approx([10.5, 12.3, 9.8])

    async def test_multi_series_load_values_preserved(self, tmp_path):
        original = await _make_live_collector()
        path = str(tmp_path / 'export.json')
        original.export_json(path)
        loaded = MetricCollector.from_json(path)

        series = loaded.get_series()
        assert [v for _, v, _ in series[f'{HOST}/Load (1m)']]  == pytest.approx([0.52, 0.61, 0.48])
        assert [v for _, v, _ in series[f'{HOST}/Load (5m)']]  == pytest.approx([0.58, 0.57, 0.55])
        assert [v for _, v, _ in series[f'{HOST}/Load (15m)']] == pytest.approx([0.59, 0.60, 0.58])

    async def test_timestamps_preserved(self, tmp_path):
        original = await _make_live_collector()
        path = str(tmp_path / 'export.json')
        original.export_json(path)
        loaded = MetricCollector.from_json(path)

        pts = loaded.get_series()[f'{HOST}/Overall CPU']
        assert [ts for ts, _, _ in pts] == [T0, T1, T2]

    async def test_proc_series_meta_preserved(self, tmp_path):
        original = await _make_live_collector()
        path = str(tmp_path / 'export.json')
        original.export_json(path)
        loaded = MetricCollector.from_json(path)

        pts = loaded.get_series()[f'{HOST}/proc/1234']
        _, _, meta = pts[0]
        assert meta is not None
        assert meta['User']    == 'root'
        assert meta['Command'] == 'python3 script.py'
        assert meta['CPU Time'] == '0:01.00'

    async def test_chart_map_preserved(self, tmp_path):
        original = await _make_live_collector()
        path = str(tmp_path / 'export.json')
        original.export_json(path)
        loaded = MetricCollector.from_json(path)

        chart_map = loaded.get_chart_map()
        assert chart_map['Load (1m)']  == 'Load'
        assert chart_map['Load (5m)']  == 'Load'
        assert chart_map['Load (15m)'] == 'Load'
        assert chart_map['proc/1234'] == 'CPU'
        assert chart_map['Overall CPU'] == 'CPU'

    async def test_instantaneous_event_preserved(self, tmp_path):
        original = await _make_live_collector()
        path = str(tmp_path / 'export.json')
        original.export_json(path)
        loaded = MetricCollector.from_json(path)

        events = loaded.get_events()
        ev = next(e for e in events if e.label == 'test start')
        assert ev.timestamp     == EV_TS
        assert ev.color         == '#888888'
        assert ev.source        == 'auto'
        assert ev.dash          == 'dash'
        assert ev.end_timestamp is None

    async def test_span_event_preserved(self, tmp_path):
        original = await _make_live_collector()
        path = str(tmp_path / 'export.json')
        original.export_json(path)
        loaded = MetricCollector.from_json(path)

        events = loaded.get_events()
        span = next(e for e in events if e.label == 'test window')
        assert span.timestamp     == SPAN_START
        assert span.end_timestamp == SPAN_END
        assert span.color         == '#2ca02c'
        assert span.source        == 'user_code'
        assert span.dash          == 'solid'

    async def test_event_count_preserved(self, tmp_path):
        original = await _make_live_collector()
        path = str(tmp_path / 'export.json')
        original.export_json(path)
        loaded = MetricCollector.from_json(path)

        assert len(loaded.get_events()) == len(original.get_events()) == 2

    async def test_full_state_identical(self, tmp_path):
        """Convenience: assert all series, chart_map, and events match in one call."""
        original = await _make_live_collector()
        path = str(tmp_path / 'export.json')
        original.export_json(path)
        loaded = MetricCollector.from_json(path)
        _assert_collectors_equal(original, loaded)


class TestDoubleExportIdempotency:
    """Export → import → re-export should produce an identical JSON document."""

    async def test_second_export_matches_first(self, tmp_path):
        original = await _make_live_collector()
        path1 = str(tmp_path / 'first.json')
        path2 = str(tmp_path / 'second.json')

        original.export_json(path1)
        loaded = MetricCollector.from_json(path1)
        loaded.export_json(path2)

        data1 = json.loads(Path(path1).read_text())
        data2 = json.loads(Path(path2).read_text())

        # chart_map and events must be identical
        assert data1['chart_map'] == data2['chart_map']
        assert data1['events']    == data2['events']

        # metrics: same content regardless of serialisation order
        assert _sorted_metrics(data1) == _sorted_metrics(data2)

    async def test_chart_map_present_in_exported_json(self, tmp_path):
        original = await _make_live_collector()
        path = str(tmp_path / 'export.json')
        original.export_json(path)
        data = json.loads(Path(path).read_text())

        assert 'chart_map' in data
        assert data['chart_map'] != {}

    async def test_three_export_import_cycles_stable(self, tmp_path):
        """State should not drift across repeated export/import cycles."""
        collector = await _make_live_collector()
        for i in range(3):
            path = str(tmp_path / f'cycle{i}.json')
            collector.export_json(path)
            collector = MetricCollector.from_json(path)

        # After three cycles all data must still be intact
        _assert_collectors_equal(await _make_live_collector(), collector)
