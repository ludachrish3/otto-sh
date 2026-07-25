"""Same-context re-capture supersedes: newest (tier, label, host) wins (spec §8.5)."""

from otto.coverage.capture.model import Capture
from otto.coverage.capture.supersede import select_manual_captures


def _cap(
    captured_at: str,
    board: str = "bench-1",
    tier: str = "manual",
    display_name: str | None = None,
    ticket: str | None = None,
) -> Capture:
    return Capture(
        tier=tier,
        base_commit="c" * 40,
        captured_at=captured_at,
        board=board,
        display_name=display_name,
        ticket=ticket,
    )


class TestSelectManualCaptures:
    def test_newest_same_context_wins(self):
        old, new = _cap("2026-06-01T00:00:00Z"), _cap("2026-07-01T00:00:00Z")
        assert select_manual_captures([old, new]) == [new]
        assert select_manual_captures([new, old]) == [new]

    def test_different_hosts_both_survive(self):
        a = _cap("2026-06-01T00:00:00Z", board="bench-1")
        b = _cap("2026-06-01T00:00:00Z", board="bench-2")
        assert select_manual_captures([a, b]) == [a, b]

    def test_different_labels_both_survive(self):
        a = _cap("2026-06-01T00:00:00Z", display_name="bring-up")
        b = _cap("2026-06-02T00:00:00Z", display_name="cert-sweep")
        assert select_manual_captures([a, b]) == [a, b]

    def test_blank_captured_at_loses_to_dated(self):
        blank, dated = _cap(""), _cap("2026-01-01T00:00:00Z")
        assert select_manual_captures([dated, blank]) == [dated]

    def test_tie_keeps_later_entry(self):
        a, b = _cap("2026-06-01T00:00:00Z", ticket="A"), _cap("2026-06-01T00:00:00Z", ticket="B")
        assert select_manual_captures([a, b]) == [b]

    def test_superseded_is_logged(self, caplog):
        import logging

        old, new = _cap("2026-06-01T00:00:00Z"), _cap("2026-07-01T00:00:00Z")
        with caplog.at_level(logging.INFO):
            select_manual_captures([old, new])
        assert "supersed" in caplog.text.lower()

    def test_partial_supersession_preserves_survivor_order(self):
        # Middle capture (bench-1, old) is superseded by the LAST entry;
        # survivors must keep their original relative order: [b, c].
        a = _cap("2026-06-01T00:00:00Z", board="bench-1")
        b = _cap("2026-06-15T00:00:00Z", board="bench-2")
        c = _cap("2026-07-01T00:00:00Z", board="bench-1")
        assert select_manual_captures([a, b, c]) == [b, c]
