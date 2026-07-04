"""Log-sourced parsers: timestamp parsing, high-water dedup, CSV metrics."""

import calendar
from datetime import datetime, timedelta, timezone

import pytest

from otto.monitor.log_sourced import (
    CsvMetricParser,
    HighWaterMark,
    RegexLogEventParser,
    parse_timestamp,
)
from otto.monitor.parsers import LogEvent, ParseContext

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

    def test_composite_year_directive_returns_none_not_raise(self) -> None:
        # %c carries its own year, colliding with the injected %Y at
        # regex-compile time — must degrade to None, never raise.
        assert parse_timestamp("Sun Jul  4 12:00:00 2026", "%c") is None


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


SYSLOG_PATTERN = r"^(?P<ts>\S+) (?P<loghost>\S+) (?P<proc>[^:\[]+)(?:\[\d+\])?: (?P<message>.*)$"


def _syslog() -> RegexLogEventParser:
    return RegexLogEventParser(
        "tail -n 200 /var/log/syslog",
        SYSLOG_PATTERN,
        tab="syslog",
        tab_label="Syslog",
    )


class TestRegexLogEventParser:
    def test_declares_table_columns_in_pattern_order(self) -> None:
        p = _syslog()
        assert p.table_columns == ["loghost", "proc", "message"]
        assert p.tab == "syslog"
        assert p.chart == "Syslog"
        assert p.parse("anything", ctx=ParseContext()) == {}

    def test_named_groups_become_fields(self) -> None:
        line = "2026-07-04T12:00:00+00:00 vm1 sshd[142]: session opened\n"
        tick = _syslog().parse_tick(line, ctx=ParseContext())
        assert tick.samples == []
        assert tick.events == [
            LogEvent(
                ts=T0,
                fields={"loghost": "vm1", "proc": "sshd", "message": "session opened"},
            )
        ]

    def test_nonmatching_lines_skipped(self) -> None:
        out = "not a syslog line\n2026-07-04T12:00:00Z vm1 cron: job ran\n"
        tick = _syslog().parse_tick(out, ctx=ParseContext())
        assert len(tick.events) == 1
        assert tick.events[0].fields["proc"] == "cron"

    def test_events_sorted_ascending_and_hwm_dedups_rereads(self) -> None:
        p = _syslog()
        out = (
            "2026-07-04T12:00:05Z vm1 a: second\n"
            "2026-07-04T12:00:00Z vm1 a: first\n"
        )
        first = p.parse_tick(out, ctx=ParseContext()).events
        assert [e.fields["message"] for e in first] == ["first", "second"]
        assert p.parse_tick(out, ctx=ParseContext()).events == []
        grown = out + "2026-07-04T12:00:10Z vm1 a: third\n"
        assert [e.fields["message"] for e in p.parse_tick(grown, ctx=ParseContext()).events] == [
            "third"
        ]

    def test_strptime_ts_format(self) -> None:
        p = RegexLogEventParser(
            "tail -n 200 /var/log/messages",
            r"^(?P<ts>\w+ +\d+ [\d:]+) (?P<message>.*)$",
            tab="messages",
            tab_label="Messages",
            ts_format="%b %d %H:%M:%S",
        )
        tick = p.parse_tick("Jul  4 12:00:00 classic syslog body\n", ctx=ParseContext())
        assert len(tick.events) == 1
        assert tick.events[0].ts.year == datetime.now(tz=timezone.utc).year

    def test_unparsable_timestamp_skips_line(self) -> None:
        tick = _syslog().parse_tick("garbage vm1 a: hi\n", ctx=ParseContext())
        assert tick.events == []

    def test_ts_group_must_exist(self) -> None:
        with pytest.raises(ValueError, match="no named group 'ts'"):
            RegexLogEventParser("tail x", r"(?P<message>.*)", tab="t", tab_label="T")

    def test_needs_a_column_besides_the_timestamp(self) -> None:
        with pytest.raises(ValueError, match="at least one named group besides"):
            RegexLogEventParser("tail x", r"(?P<ts>\S+)", tab="t", tab_label="T")


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
