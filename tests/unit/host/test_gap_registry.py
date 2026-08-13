"""The userland gap registry, and the one rule that decides what it blocks.

    **Measured-broken refuses up front; unmeasured runs.**

That sentence is
``docs/superpowers/specs/2026-08-11-busybox-host-support-design.md`` §4's, and
this module exists to pin BOTH of its halves. The second half is the one that
needs saying twice, because a registry that blocked EVERYTHING would sail
through a suite that only checked the first: every measured-broken surface
would still refuse, every assertion would still be green, and otto would have
started declining work it has never once seen fail. So
``TestUnmeasuredRuns`` is not the polite sibling of ``TestMeasuredBrokenRefuses``
— it is the half that catches the expensive mistake, and it is
mutation-verified against exactly that mutation (``Gap.refuses -> True``).

BOTH HALVES ARE PARAMETRIZED OVER THE LIVE TABLE, which makes them vacuous if
the table ever loses a status. ``TestTheTableCanAnswerBothWays`` is the guard
for that, and it comes first on purpose: a parametrized test over an empty
list passes, silently, and reports the same green as one that ran.

WHAT IS NOT TESTED HERE, and cannot be. No assertion can check that a
``measured_on`` string is TRUE — that a command really was run and really
answered that. What the suite can do is make the shape of the claim
compulsory: ``Gap.__post_init__`` refuses a measured-broken record with no
measurement and an untested record that carries one, so the only way to get a
refusing record into the table is to write down what earned it. The rest is
review.
"""

import pytest

from otto.host.errors import UnsupportedOnUserlandError
from otto.host.userland import (
    GAP_DOCS_PAGE,
    GAPS,
    MEASURED_BROKEN,
    UNTESTED,
    Gap,
    gap_for,
    refuse_if_gapped,
)

_BROKEN = [g for g in GAPS if g.status == MEASURED_BROKEN]
_UNTESTED = [g for g in GAPS if g.status == UNTESTED]

_BROKEN_ROWS = [pytest.param(g, id=g.surface) for g in _BROKEN]
_UNTESTED_ROWS = [pytest.param(g, id=g.surface) for g in _UNTESTED]
_ALL_ROWS = [pytest.param(g, id=g.surface) for g in GAPS]


def _valid_gap(**overrides) -> dict:
    """A record that passes every check, so a test can break exactly one thing."""
    fields = {
        "surface": "a-valid-surface",
        "status": MEASURED_BROKEN,
        "reason": "it does not work",
        "measured_on": "ran it on 2026-08-13, it said no",
        "queued_for": "nothing yet",
    }
    fields.update(overrides)
    return fields


# ===========================================================================
# Non-vacuity: the table has to be able to answer both ways
# ===========================================================================


class TestTheTableCanAnswerBothWays:
    """Neither half of the firing rule may be tested against an empty list.

    Both classes below are parametrized over a FILTER of the live table, and
    ``pytest.mark.parametrize`` over an empty sequence produces no failures.
    So a table that lost every untested record would report the "unmeasured
    runs" half as green while proving nothing about it — the same shape of
    defect this repo keeps finding in guards that cannot fail.
    """

    def test_the_registry_declares_at_least_one_measured_broken_surface(self) -> None:
        assert _BROKEN, (
            "no gap in GAPS is measured-broken, so TestMeasuredBrokenRefuses is "
            "parametrized over an empty list and proves nothing"
        )

    def test_the_registry_declares_at_least_one_untested_surface(self) -> None:
        assert _UNTESTED, (
            "no gap in GAPS is untested, so TestUnmeasuredRuns is parametrized over an "
            "empty list. That is the dangerous direction: with every record refusing, a "
            "registry that blocks everything would look exactly like a correct one"
        )


# ===========================================================================
# Half one: measured-broken refuses up front
# ===========================================================================


class TestMeasuredBrokenRefuses:
    """Every measured-broken surface raises the named error, rendered from its record."""

    @pytest.mark.parametrize("gap", _BROKEN_ROWS)
    def test_a_measured_broken_surface_refuses(self, gap: Gap) -> None:
        with pytest.raises(UnsupportedOnUserlandError):
            refuse_if_gapped(gap.surface)

    @pytest.mark.parametrize("gap", _BROKEN_ROWS)
    def test_the_message_names_surface_reason_and_docs_anchor(self, gap: Gap) -> None:
        """The spec's three: what, why, where to read more.

        Asserted against the RECORD's own strings rather than against a
        retyped expectation, so a reworded reason travels into the message
        instead of reddening this test — the message is the record, which is
        the entire point of rendering it here rather than at the call site.
        """
        with pytest.raises(UnsupportedOnUserlandError) as exc_info:
            refuse_if_gapped(gap.surface)
        message = str(exc_info.value)

        assert gap.surface in message, f"the message does not name the surface: {message!r}"
        assert gap.reason in message, f"the message does not carry the reason: {message!r}"
        assert gap.docs_anchor in message, f"the message has no docs anchor: {message!r}"
        assert gap.measured_on in message, f"the message cites no measurement: {message!r}"
        assert gap.queued_for in message, f"the message names no queue: {message!r}"

    def test_the_caller_s_own_context_reaches_the_message(self) -> None:
        """*host* and *attempted* are the two things the record cannot know.

        Without them the operator gets a true sentence about a class of
        userland and no idea which of their boxes produced it.
        """
        gap = _BROKEN[0]
        with pytest.raises(UnsupportedOnUserlandError) as exc_info:
            refuse_if_gapped(gap.surface, host="tomato", attempted="shutdown -h now")
        message = str(exc_info.value)

        assert "tomato" in message, message
        assert "shutdown -h now" in message, message

    @pytest.mark.parametrize("gap", _BROKEN_ROWS)
    def test_the_refusal_says_nothing_was_attempted(self, gap: Gap) -> None:
        """The one thing this exception means that the other two host errors do not.

        ``HostUnreachableError`` says the transport failed; ``HostCommandError``
        says the command ran and answered badly. This one says no command was
        sent, so nothing was learned about the system under test — and a
        message that does not say so reads like the third case.
        """
        with pytest.raises(UnsupportedOnUserlandError) as exc_info:
            refuse_if_gapped(gap.surface)
        assert "nothing was attempted" in str(exc_info.value).lower()


# ===========================================================================
# Half two: unmeasured runs
# ===========================================================================


class TestUnmeasuredRuns:
    """Nothing untested is blocked, and nothing undeclared is blocked either.

    THE HALF THAT CATCHES THE EXPENSIVE MISTAKE. Blocking an untested surface
    converts "we do not know" into "does not work" and makes otto refuse
    things that work — and unlike the other direction it fails SILENTLY in
    review, because a registry that refuses everything passes every test about
    refusing.
    """

    @pytest.mark.parametrize("gap", _UNTESTED_ROWS)
    def test_an_untested_surface_is_not_blocked(self, gap: Gap) -> None:
        assert refuse_if_gapped(gap.surface, host="tomato") is None, (
            f"{gap.surface!r} is declared untested and was blocked anyway"
        )

    def test_a_surface_nobody_declared_is_not_blocked(self) -> None:
        """The third outcome, and the commonest one: not in the table at all.

        Every otto surface that has never been near a BusyBox box takes this
        path, so a lookup that raised on a miss would take the whole product
        down rather than one gap's worth of it.
        """
        assert gap_for("no-such-surface-is-declared") is None
        assert refuse_if_gapped("no-such-surface-is-declared") is None

    @pytest.mark.parametrize("gap", _UNTESTED_ROWS)
    def test_an_untested_record_still_says_what_would_close_it(self, gap: Gap) -> None:
        """Not blocking is not the same as not caring.

        An untested record's whole job is to send the reader to the
        workstream that would measure it; without that it is a shrug in a
        table.
        """
        assert gap.queued_for, gap.surface
        assert gap.reason, gap.surface


# ===========================================================================
# The record type's own invariant
# ===========================================================================


class TestGapRejectsARecordThatCannotBeTrue:
    """``Gap.__post_init__`` is the firing rule expressed as data.

    A measured-broken record with no measurement would refuse on nobody's
    authority; an untested record carrying a measurement is a contradiction in
    terms. Both are caught at construction — which means at import, for the
    declared table — rather than at the moment a user is refused.
    """

    def test_measured_broken_without_a_measurement_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="carries no measurement"):
            Gap(**_valid_gap(measured_on=""))

    def test_untested_with_a_measurement_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="not untested"):
            Gap(**_valid_gap(status=UNTESTED))

    def test_an_untested_record_with_no_measurement_is_accepted(self) -> None:
        """The control for the two above — the pair is only meaningful if this passes."""
        gap = Gap(**_valid_gap(status=UNTESTED, measured_on=""))
        assert gap.refuses is False

    def test_an_unknown_status_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="not one of"):
            Gap(**_valid_gap(status="probably-fine"))

    @pytest.mark.parametrize(
        "surface", ["Has Spaces", "UPPER", "`backticks`", "trailing-", "under_score", ""]
    )
    def test_a_surface_that_would_break_its_docs_anchor_is_rejected(self, surface: str) -> None:
        with pytest.raises(ValueError, match="anchor-safe"):
            Gap(**_valid_gap(surface=surface))

    @pytest.mark.parametrize("field", ["reason", "queued_for"])
    def test_a_record_with_no_consequence_or_no_owner_is_rejected(self, field: str) -> None:
        with pytest.raises(ValueError, match="reason and a queued_for"):
            Gap(**_valid_gap(**{field: ""}))


# ===========================================================================
# The docs table's contract (Task 6 renders these records; it adds no facts)
# ===========================================================================


class TestTheTableIsRenderable:
    """What a generated docs page needs from every record, guarded here.

    The docs-sync test lands with the page and asserts the two agree. These
    are the properties that have to hold BEFORE such a page can exist at all:
    a duplicate surface makes two rows compete for one anchor, and an
    unresolvable anchor makes a link that silently goes nowhere.
    """

    def test_every_surface_is_unique(self) -> None:
        surfaces = [g.surface for g in GAPS]
        duplicates = sorted({s for s in surfaces if surfaces.count(s) > 1})
        assert not duplicates, (
            f"{duplicates} appear twice in GAPS. `gap_for` returns the first match, so "
            f"the second record would never fire and its docs row would collide with "
            f"the first one's anchor"
        )

    @pytest.mark.parametrize("gap", _ALL_ROWS)
    def test_every_anchor_hangs_off_the_declared_page(self, gap: Gap) -> None:
        assert gap.docs_anchor == f"{GAP_DOCS_PAGE}#{gap.surface}"

    @pytest.mark.parametrize("gap", _ALL_ROWS)
    def test_gap_for_finds_every_declared_record(self, gap: Gap) -> None:
        assert gap_for(gap.surface) is gap


# ===========================================================================
# The entries phases 4 and 5 measured, pinned by name
# ===========================================================================


class TestThePhase4And5MeasurementsAreRecorded:
    """Three findings the phases paid for, which must not evaporate.

    Pinned BY SURFACE and by the load-bearing fact, not by the whole string:
    the wording is free to improve, but a table that quietly lost one of these
    would have lost the reason those phases were run.
    """

    def test_the_1_16_1_base64_hole_covers_both_surfaces(self) -> None:
        """The transfer refuses on it; ``read_file``/``write_file`` cannot even do that.

        Two records rather than one because the CONSEQUENCE differs:
        ``ShellFileTransfer`` consults ``Userland.base64_flag`` and refuses up
        front, while ``file_ops`` hard-codes the applet and hands the caller
        the device's own "not found".
        """
        transfer = gap_for("shell-transfer-base64")
        file_ops = gap_for("file-ops-base64")
        assert transfer is not None, "the shell transfer's base64 gap is gone from GAPS"
        assert file_ops is not None, "the file_ops base64 gap is gone from GAPS"
        assert transfer.refuses
        assert file_ops.refuses
        assert "1.16.1" in transfer.measured_on
        assert "file_ops.py" in file_ops.measured_on
        assert "base64_flag" in file_ops.reason, (
            "the file_ops record has to name the capability it fails to consult — that "
            "is the difference between it and the transfer's record"
        )

    def test_the_run_pty_truncation_is_recorded_with_both_boundaries(self) -> None:
        """1022 intact / 1023 truncated, and ``exec()`` named as the unaffected path.

        Recorded rather than fixed, deliberately: the phase-5 plan puts the
        fix under "Out of scope". A record that omitted the boundary or the
        escape hatch would leave a reader unable to work around it.
        """
        gap = gap_for("run-command-line-length")
        assert gap is not None, "the run() pty truncation is gone from GAPS"
        assert gap.refuses
        assert "1022" in gap.reason
        assert "1023" in gap.measured_on
        assert "exec()" in gap.reason, (
            "the record must name `exec()` as the pty-free path, since that is the only "
            "thing a caller can actually do about this today"
        )
        assert "term_type" in gap.reason, (
            "the record must name what allocates the pty, or the next reader will look "
            "for the truncation in the transport — where it is not"
        )

    @pytest.mark.parametrize("surface", ["legacy-dropbear-crypto", "busybox-over-a-real-network"])
    def test_the_tier3_fidelity_gaps_are_recorded_as_untested_and_block_nothing(
        self, surface: str
    ) -> None:
        """``todo/busybox-tier3-fidelity-2026-08-13.md`` §D, and its own insistence.

        That queue file says it plainly: these two "must be worded as what is
        *untested* rather than what is broken", because blocking them would
        convert "we do not know" into "does not work". Declaring either
        measured-broken would make otto refuse a connection nobody has ever
        watched fail.
        """
        gap = gap_for(surface)
        assert gap is not None, f"{surface!r} is not in GAPS"
        assert gap.status == UNTESTED
        assert gap.refuses is False
        assert refuse_if_gapped(surface) is None
        assert "busybox-tier3-fidelity-2026-08-13" in gap.queued_for
