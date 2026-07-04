"""Log-sourced parsers: timestamp parsing, high-water dedup, CSV metrics."""

import calendar
from datetime import datetime, timedelta, timezone

import pytest

from otto.monitor.log_sourced import CsvMetricParser, HighWaterMark, parse_timestamp
from otto.monitor.parsers import ParseContext

T0 = datetime(2026, 7, 4, 12, 0, 0, tzinfo=timezone.utc)


class TestParseTimestamp:
    def test_auto_epoch(self) -> None:
        assert parse_timestamp("1751630400") == datetime.fromtimestamp(
            1751630400, tz=timezone.utc
        )

    def test_auto_iso_aware(self) -> None:
        assert parse_timestamp("2026-07-04T12:00:00+00:00") == T0

    def test_auto_iso_naive_is_utc(self) -> None:
        assert parse_timestamp("2026-07-04 12:00:00") == T0

    def test_iso_z_suffix(self) -> None:
        assert parse_timestamp("2026-07-04T12:00:00Z", "iso") == T0

    def test_epoch_mode_rejects_iso(self) -> None:
        assert parse_timestamp("2026-07-04T12:00:00", "epoch") is None

    def test_strptime_format(self) -> None:
        assert parse_timestamp("2026/07/04 12:00:00", "%Y/%m/%d %H:%M:%S") == T0

    def test_strptime_without_year_gets_current_utc_year(self) -> None:
        parsed = parse_timestamp("Jul  4 12:00:00", "%b %d %H:%M:%S")
        assert parsed is not None
        assert parsed.year == datetime.now(tz=timezone.utc).year
        assert (parsed.month, parsed.day, parsed.hour) == (7, 4, 12)

    def test_leap_day_with_yearless_format(self) -> None:
        parsed = parse_timestamp("Feb 29 06:00:00", "%b %d %H:%M:%S")
        if calendar.isleap(datetime.now(tz=timezone.utc).year):
            assert parsed is not None
            assert (parsed.month, parsed.day) == (2, 29)
        else:
            # Feb 29 doesn't exist this year — the ambiguous row is dropped.
            assert parsed is None

    def test_garbage_is_none(self) -> None:
        assert parse_timestamp("not a time") is None


class TestHighWaterMark:
    def test_first_pass_emits_all_sorted(self) -> None:
        hwm: HighWaterMark = HighWaterMark()
        rows = [(T0 + timedelta(seconds=2), "b"), (T0, "a")]
        assert hwm.advance(rows) == [(T0, "a"), (T0 + timedelta(seconds=2), "b")]

    def test_reread_of_same_window_emits_nothing(self) -> None:
        hwm: HighWaterMark = HighWaterMark()
        rows = [(T0, "a"), (T0 + timedelta(seconds=2), "b")]
        hwm.advance(rows)
        assert hwm.advance(rows) == []

    def test_overlapping_window_emits_only_newer(self) -> None:
        hwm: HighWaterMark = HighWaterMark()
        hwm.advance([(T0, "a"), (T0 + timedelta(seconds=2), "b")])
        third = (T0 + timedelta(seconds=4), "c")
        assert hwm.advance([(T0 + timedelta(seconds=2), "b"), third]) == [third]

    def test_rotation_new_rows_still_newer(self) -> None:
        """Rotation/truncation: the mark keys on timestamps, not offsets."""
        hwm: HighWaterMark = HighWaterMark()
        hwm.advance([(T0, "old")])
        fresh = (T0 + timedelta(seconds=1), "post-rotate")
        assert hwm.advance([fresh]) == [fresh]


def _csv() -> CsvMetricParser:
    return CsvMetricParser(
        "cat /var/log/perf/net.csv",
        columns=["rx_kbps", "tx_kbps"],
        chart="Cron net digest",
        tab="network",
        tab_label="Network",
        unit="kb/s",
        interval=60,
    )


class TestCsvMetricParser:
    def test_declares_its_registry_metadata(self) -> None:
        p = _csv()
        assert p.command == "cat /var/log/perf/net.csv"
        assert p.chart == "Cron net digest"
        assert p.interval == 60
        assert p.table_columns is None  # a CHART parser, not a table parser
        assert p.parse("anything", ctx=ParseContext()) == {}

    def test_epoch_and_iso_rows_become_timed_samples(self) -> None:
        out = "1751630400,10,20\n2026-07-04T12:00:05+00:00,11,21\n"
        tick = _csv().parse_tick(out, ctx=ParseContext())
        assert tick.events == []
        assert [s.ts for s in tick.samples] == [
            datetime.fromtimestamp(1751630400, tz=timezone.utc),
            datetime(2026, 7, 4, 12, 0, 5, tzinfo=timezone.utc),
        ]
        assert tick.samples[0].series["rx_kbps"].value == 10.0
        assert tick.samples[0].series["tx_kbps"].value == 20.0

    def test_samples_sorted_ascending(self) -> None:
        out = "2026-07-04T12:00:05,11,21\n2026-07-04T12:00:00,10,20\n"
        tick = _csv().parse_tick(out, ctx=ParseContext())
        assert [s.ts for s in tick.samples] == sorted(s.ts for s in tick.samples)

    def test_high_water_dedup_across_rereads(self) -> None:
        p = _csv()
        out = "2026-07-04T12:00:00,10,20\n2026-07-04T12:00:05,11,21\n"
        assert len(p.parse_tick(out, ctx=ParseContext()).samples) == 2
        # Same window re-read: nothing new.
        assert p.parse_tick(out, ctx=ParseContext()).samples == []
        # One new line appended: only it emits.
        grown = out + "2026-07-04T12:00:10,12,22\n"
        fresh = p.parse_tick(grown, ctx=ParseContext()).samples
        assert [s.ts for s in fresh] == [datetime(2026, 7, 4, 12, 0, 10, tzinfo=timezone.utc)]

    def test_restart_backfills_full_window(self) -> None:
        """A fresh parser instance (monitor restart) re-emits the whole file."""
        out = "".join(f"2026-07-04T12:00:{s:02d},1,2\n" for s in range(10))
        assert len(_csv().parse_tick(out, ctx=ParseContext()).samples) == 10

    def test_header_torn_and_malformed_lines_skipped(self) -> None:
        out = (
            "timestamp,rx_kbps,tx_kbps\n"        # header: first col not a timestamp
            "2026-07-04T12:00:00,10,20\n"        # good
            "2026-07-04T12:00:05,11\n"           # column mismatch (torn/partial)
            "2026-07-04T12:00:10,eleven,21\n"    # non-numeric value
            "garbage\n"
        )
        tick = _csv().parse_tick(out, ctx=ParseContext())
        assert len(tick.samples) == 1
        assert tick.samples[0].ts == datetime(2026, 7, 4, 12, 0, 0, tzinfo=timezone.utc)

    def test_torn_line_reemits_whole_next_tick(self) -> None:
        """The mark never passes a skipped line, so its completed form emits later."""
        p = _csv()
        p.parse_tick("2026-07-04T12:00:00,10,20\n2026-07-04T12:00:05,11\n", ctx=ParseContext())
        fresh = p.parse_tick(
            "2026-07-04T12:00:00,10,20\n2026-07-04T12:00:05,11,21\n", ctx=ParseContext()
        ).samples
        assert [s.ts for s in fresh] == [datetime(2026, 7, 4, 12, 0, 5, tzinfo=timezone.utc)]

    def test_requires_at_least_one_column(self) -> None:
        with pytest.raises(ValueError, match="at least one value column"):
            CsvMetricParser("cat x.csv", columns=[], chart="X")
