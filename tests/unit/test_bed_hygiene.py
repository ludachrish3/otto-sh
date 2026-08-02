"""Contract tests for the BedHygiene diff — canned probe outputs, no bed.

The oracle's one hard rule: PRE-EXISTING DIRT IS SNAPSHOTTED OUT. A leftover
is a line present after that was absent before; something dirty going in must
never be blamed on the scenario (2026-07-21 misattribution class), and must
never mask a NEW leftover of the same kind either.
"""

from tests._fixtures.bed_hygiene import (
    HygieneSnapshot,
    diff_snapshots,
    format_hygiene_report,
)


def _snap(**over):
    base = {
        "tunnel_procs": frozenset(),
        "impair_timers": frozenset(),
        "nc_listeners": frozenset(),
        "qdiscs": {"eth2": "qdisc noqueue 0: root refcnt 2"},
        "staging": frozenset(),
        "history_digest": "abc  -",
    }
    base.update(over)
    return HygieneSnapshot(**base)


def test_identical_snapshots_diff_empty():
    assert diff_snapshots(_snap(), _snap()) == []


def test_new_listener_named_old_listener_ignored():
    before = _snap(nc_listeners=frozenset({"111 nc -l -w 30 9000"}))
    after = _snap(nc_listeners=frozenset({"111 nc -l -w 30 9000", "222 nc -l -w 30 9001"}))
    leftovers = diff_snapshots(before, after)
    assert len(leftovers) == 1
    assert "222" in leftovers[0]
    assert "111" not in leftovers[0]


def test_qdisc_change_reported_with_device_and_both_states():
    before = _snap()
    after = _snap(qdiscs={"eth2": "qdisc prio 1: root refcnt 2 bands 11"})
    leftovers = diff_snapshots(before, after)
    assert len(leftovers) == 1
    assert "eth2" in leftovers[0]
    assert "prio" in leftovers[0]


def test_history_digest_change_reported():
    leftovers = diff_snapshots(_snap(), _snap(history_digest="def  -"))
    assert len(leftovers) == 1
    assert "history" in leftovers[0]


def test_history_digest_empty_both_sides_no_crash_and_clean():
    # "" only happens when the probe itself returned nothing (timed-out or
    # failed exec) — with nothing on either side there is nothing to compare,
    # so this must not raise and must not be reported as a leftover.
    assert diff_snapshots(_snap(history_digest=""), _snap(history_digest="")) == []


def test_history_digest_empty_after_side_reported_as_probe_failure_not_change():
    # after-probe returned nothing (e.g. host unreachable post-scenario): this
    # is a probe/read failure, not evidence the history changed — must not
    # crash on the old `.split()[0]` and must not say "digest changed".
    leftovers = diff_snapshots(_snap(history_digest="abc  -"), _snap(history_digest=""))
    assert len(leftovers) == 1
    assert "probe" in leftovers[0]
    assert "suppression leak" not in leftovers[0]


def test_history_digest_empty_before_side_reported_as_probe_failure_not_change():
    # Symmetric case: before-probe returned nothing, after has a real digest.
    leftovers = diff_snapshots(_snap(history_digest=""), _snap(history_digest="def  -"))
    assert len(leftovers) == 1
    assert "probe" in leftovers[0]
    assert "suppression leak" not in leftovers[0]


def test_report_names_host_and_lists_leftovers():
    report = format_hygiene_report("tomato", ["eth2: qdisc changed", "new nc listener"])
    assert "tomato" in report
    assert "eth2: qdisc changed" in report
    assert "new nc listener" in report
