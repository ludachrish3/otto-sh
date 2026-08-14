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

**THE PATHS ARE THE EXCEPTION, and that is why they exist.** A ``measured_on``
string is a claim about a DEVICE and unfalsifiable here; a
:class:`~otto.host.userland.GapPath` is a claim about OTTO'S OWN SOURCE, and
every part of it is checked below — the site resolves, a ``WIRED`` site really
calls the guard it names, that guard really reaches ``refuse_if_gapped`` with
this record's surface, a ``PROTECTED`` path's protector exists, a named test
exists. So a hole cannot be papered over by prose: the classes under "The
paths" carry their own list of what those checks can and cannot see, and the
``OPEN`` paths are separately pinned to the docs page by
``tests/unit/test_docs_gap_sync.py``, so a hole is visible to a reader too.
"""

import ast
import importlib
import inspect
import re
import textwrap
from types import ModuleType

import pytest

from otto.host.errors import UnsupportedOnUserlandError
from otto.host.product import FileProduct, Product
from otto.host.userland import (
    GAP_DOCS_PAGE,
    GAPS,
    MEASURED_BROKEN,
    PATH_OPEN,
    PATH_PROBE_REFUSED,
    PATH_PROTECTED,
    PATH_WIRED,
    UNTESTED,
    Gap,
    GapPath,
    gap_for,
    gap_path_totals,
    refuse_if_gapped,
    wired_guards,
)
from tests._fixtures.paths import PROJECT_ROOT

_BROKEN = [g for g in GAPS if g.status == MEASURED_BROKEN]
_UNTESTED = [g for g in GAPS if g.status == UNTESTED]

_BROKEN_ROWS = [pytest.param(g, id=g.surface) for g in _BROKEN]
_UNTESTED_ROWS = [pytest.param(g, id=g.surface) for g in _UNTESTED]
_ALL_ROWS = [pytest.param(g, id=g.surface) for g in GAPS]

_ALL_PATHS = [
    pytest.param(gap, path, id=f"{gap.surface}:{path.site.rsplit('.', 1)[-1]}")
    for gap in GAPS
    for path in gap.paths
]
"""Every declared path, tagged with the record that owns it.

The parametrization that makes a stale claim loud. It is a filter of the live
table, so it is vacuous if the table ever empties -- which is what
``TestEveryPathStateIsRepresented`` is for, and why that class comes first.
"""


def _paths_in(state: str) -> list:
    return [p for p in _ALL_PATHS if p.values[1].state == state]


def _valid_gap(**overrides) -> dict:
    """A record that passes every check, so a test can break exactly one thing."""
    fields = {
        "surface": "a-valid-surface",
        "status": MEASURED_BROKEN,
        "reason": "it does not work",
        "measured_on": "ran it on 2026-08-13, it said no",
        "queued_for": "nothing yet",
        "paths": [_valid_path()],
    }
    fields.update(overrides)
    return fields


def _valid_path(**overrides) -> GapPath:
    """A path that passes every check, so a test can break exactly one thing."""
    fields = {
        "site": "otto.host.userland.refuse_if_gapped",
        "state": PATH_OPEN,
        "detail": "nothing reads the record here",
    }
    fields.update(overrides)
    return GapPath(**fields)


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
        gap = Gap(**_valid_gap(status=UNTESTED, measured_on="", paths=[]))
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

    def test_a_record_is_frozen_but_not_hashable_and_says_what_to_use_instead(self) -> None:
        """The one inference ``frozen=True`` invites and this class does not support.

        ``@dataclass(frozen=True)`` generates a ``__hash__`` over the fields, and
        :attr:`~otto.host.userland.Gap.paths` is a list, so the generated one
        raises when it is finally called — naming the list, not the field. Nobody
        hashes a record today, so this pins a documented LIMITATION rather than a
        behaviour: a future ``set(GAPS)`` fails here, in a test that names the
        cause, instead of at the call site as a puzzle.

        Both alternatives the class docstring offers are asserted, because a
        limitation recorded without a way round it is just an obstacle:
        :class:`~otto.host.userland.GapPath` is hashable, and
        :attr:`~otto.host.userland.Gap.surface` is a unique key
        (``test_every_surface_is_unique`` above). Making ``Gap`` hashable reddens
        this — deliberately: the docstring is then wrong and has to move with it.
        """
        with pytest.raises(TypeError, match="unhashable type: 'list'"):
            hash(GAPS[0])
        with pytest.raises(TypeError, match="unhashable type: 'list'"):
            set(GAPS)

        declared_paths = [path for gap in GAPS for path in gap.paths]
        assert len(set(declared_paths)) == len(declared_paths), (
            "GapPath is hashable and every declared path is distinct, so a caller "
            "wanting a set of call sites already has one"
        )
        assert len({gap.surface for gap in GAPS}) == len(GAPS)

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
        """One missing applet, two surfaces, and each refuses through its own route.

        Two records rather than one because the SURFACE differs and so does the
        route to the refusal: ``ShellFileTransfer`` reads
        ``Userland.base64_flag`` and raises its own probe-driven message, while
        ``read_file``/``write_file`` cannot adapt at all — they hard-code the
        applet — and are refused from THIS table by
        ``otto.host.file_ops.refuse_if_base64_is_absent``. A fix is one change
        for both (a second codec), which is what ``queued_for`` says; the
        records stay separate because a caller hits one or the other.
        """
        transfer = gap_for("shell-transfer-base64")
        file_ops = gap_for("file-ops-base64")
        assert transfer is not None, "the shell transfer's base64 gap is gone from GAPS"
        assert file_ops is not None, "the file_ops base64 gap is gone from GAPS"
        assert transfer.refuses
        assert file_ops.refuses
        assert "1.16.1" in transfer.measured_on
        # The file_ops record names its call sites through `paths`, as dotted
        # names the tests below RESOLVE, and no longer as `file_ops.py:<line>`
        # in prose the operator's error message prints. It carried two such
        # numbers and both had drifted two lines; see `Gap.measured_on`.
        assert not re.search(r"file_ops\.py:\d+", file_ops.measured_on), (
            "a source line number is back in the one field that renders into the "
            "operator's error message and that nothing can check"
        )
        assert [path.site for path in file_ops.wired_paths] == [
            "otto.host.file_ops.PosixFileOps.read_file",
            "otto.host.file_ops.PosixFileOps.write_file",
        ], "the two call sites the measurement was taken against, resolvable rather than cited"
        assert "base64_flag" in file_ops.reason, (
            "the file_ops record has to name the capability whose spelling it ignores — "
            "that is the difference between it and the transfer's record"
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

    def test_the_product_lifecycle_record_is_untested_and_blocks_nothing(self) -> None:
        """The survey's fourth candidate, moved out of the *rejected* comment block.

        ``pgrep``, ``sudo`` and ``reboot`` sit in that block because a
        measurement contradicted the prediction. ``install``/``stage`` sat
        there on reasoning alone, which is the one thing a table whose whole
        authority is "measured, not predicted" cannot carry -- so it is a
        record, and the honest status is the one that blocks nothing.
        """
        gap = gap_for("product-lifecycle")
        assert gap is not None, "the product lifecycle record is gone from GAPS"
        assert gap.status == UNTESTED
        assert gap.refuses is False
        assert refuse_if_gapped("product-lifecycle") is None

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


# ===========================================================================
# The one record whose claim IS checkable, because it is about otto's own code
# ===========================================================================


_LIFECYCLE_VERBS = frozenset({"stage", "install", "uninstall", "is_installed"})
"""The four methods :class:`~otto.host.product.Product` declares."""


def _host_attributes_reached_for_by(source: str) -> set[str]:
    """Every ``host.<name>`` *source* reaches for, read off its AST.

    Source-level rather than behavioural on purpose. The claim under test is
    what otto's one concrete product body *can possibly* emit; a behavioural
    test would need a fake host, and a fake answers whatever it was written to
    answer -- it would pass just as well if the body had grown a second call
    the fake happened to stub.
    """
    tree = ast.parse(textwrap.dedent(source))
    return {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "host"
    }


class TestProductLifecycleIsUntestedBecauseOttoShipsNoImplementation:
    """Why ``product-lifecycle`` cannot be measured, pinned as structure.

    The module header above says no assertion can check that a ``measured_on``
    string is true. This record is the exception that proves the shape of the
    rule: its claim is not about what a BusyBox device answered, it is about
    **otto's own source**, and that is checkable here.

    The claim: ``Host.stage``/``install``/``uninstall``/``is_installed`` emit
    no command of their own. They iterate ``Host.products`` and delegate, and
    :class:`~otto.host.product.Product` declares all four of its methods
    abstract. otto ships exactly ONE concrete body,
    :meth:`~otto.host.product.FileProduct.stage`, and it is a single
    ``host.put`` -- a surface already in this table. So the half a measurement
    could reach is recorded elsewhere, and the half that decides whether
    ``install`` works on a BusyBox device is project code otto does not own.

    WHY THAT MAKES A MEASUREMENT MEANINGLESS RATHER THAN MERELY EXPENSIVE, and
    why this guard is on the structure rather than on a Tier 3 run: a test that
    staged a ``Product`` against a real BusyBox root would exercise the
    subclass the test itself wrote, plus a ``for`` loop. It could not fail for
    a BusyBox reason, which is this repo's recurring defect, not evidence.

    THE DAY THIS RECORD STOPS BEING TRUE is the day otto ships a concrete
    ``Product.install`` -- one emitting ``opkg``, ``dpkg``, ``tar``, anything --
    or grows a second device call in ``FileProduct.stage``. Then the surface
    becomes otto's, and measurable, and these assertions are what say so.
    """

    def test_product_declares_every_lifecycle_verb_abstract(self) -> None:
        assert set(Product.__abstractmethods__) == _LIFECYCLE_VERBS, (
            f"`Product` declares {sorted(Product.__abstractmethods__)} abstract, not "
            f"{sorted(_LIFECYCLE_VERBS)}. The `product-lifecycle` gap record says otto "
            f"ships no implementation of these verbs, which is why it is `untested` and "
            f"not cleared. A concrete body here makes that record false -- and makes the "
            f"surface otto's own, and measurable. Measure it and update the record."
        )

    def test_fileproduct_leaves_every_verb_but_stage_abstract(self) -> None:
        assert set(FileProduct.__abstractmethods__) == _LIFECYCLE_VERBS - {"stage"}, (
            f"`FileProduct` leaves {sorted(FileProduct.__abstractmethods__)} abstract. "
            f"The `product-lifecycle` record rests on `stage` being the ONLY verb otto "
            f"gives a body to; if `install`, `uninstall` or `is_installed` now has one, "
            f"otto emits its own commands during install and the record needs measuring "
            f"rather than rewording."
        )

    def test_the_one_concrete_body_reaches_the_device_only_through_put(self) -> None:
        """``FileProduct.stage`` is one ``host.put`` — an already-recorded surface.

        That single call is the whole reason ``product-lifecycle`` adds no new
        device contact: whatever BusyBox does to it is already answered by
        ``shell-transfer-base64``, ``sftp-transfer``, ``scp-transfer`` and
        ``nc-transfer``, and measured over real ssh in Tier 3.
        """
        reached = _host_attributes_reached_for_by(inspect.getsource(FileProduct.stage))
        assert reached == {"put"}, (
            f"`FileProduct.stage` reaches for {sorted(reached)} on the host, not just "
            f"`put`. The `product-lifecycle` record says otto's one concrete product "
            f"body adds no device contact beyond a surface this table already covers; a "
            f"second call breaks that, and whatever it emits has never been run on a "
            f"BusyBox userland."
        )


# ===========================================================================
# The paths: a surface is a fact about the device, a path is a place otto
# touches it -- and a path's claim must be CHECKED, never trusted
# ===========================================================================
#
# WHAT THESE CHECKS CAN SEE. Structure, statically: that a named site exists and
# is callable, that a `WIRED` site's source really calls the guard it names, that
# the guard really reaches `refuse_if_gapped` with THIS record's surface, that a
# `PROTECTED` path's protector exists, and that a named test exists. Every one of
# those goes stale silently under an ordinary rename or deletion, which is why
# they are read off the AST rather than described in prose.
#
# WHAT THEY CANNOT SEE, stated because a guard whose blind spots are unstated
# gets trusted past them:
#
# * REACHABILITY. `_calls_made_by` finds a call in the source; it cannot tell
#   whether control reaches it. A guard call moved below an early `return`, or
#   under a condition that is never true, still reads as WIRED here. Only the
#   per-surface behavioural suites (`test_run_line_length.py`,
#   `test_daemon_launch_refusal.py`, `test_file_ops_base64_refusal.py`, each of
#   which flips its record to `untested` and watches the refusal stop) can say
#   the guard actually fires.
# * THE PREDICATE. A guard that returned before its `refuse_if_gapped` on every
#   real host would pass every assertion below. What the predicate keys on, and
#   whether that is the right fact, is those same suites' job and review's.
# * INDIRECTION. A site that reached its guard through a variable, a `getattr`,
#   a decorator or a dynamically-built dispatch table would read as unwired.
#   Every wired site today calls its guard by name; the day one does not, this
#   check reds and the fix is to make the call visible, not to loosen the check.
# * COMPLETENESS. Nothing here can know that the declared `OPEN` paths are ALL
#   the open paths. A hole nobody has found is a hole nobody has recorded; these
#   checks only keep the recorded ones honest.


def _resolve_dotted(dotted: str) -> object:
    """Resolve a dotted name to the live object, importing submodules as needed.

    Walks left to right rather than guessing how many trailing components are
    attributes, because both shapes occur (``module.function`` and
    ``module.Class.method``) and a wrong guess would report a real name as
    missing.
    """
    parts = dotted.split(".")
    obj: object = importlib.import_module(parts[0])
    walked = parts[0]
    for part in parts[1:]:
        walked = f"{walked}.{part}"
        if isinstance(obj, ModuleType) and not hasattr(obj, part):
            obj = importlib.import_module(walked)
        else:
            obj = getattr(obj, part)
    return obj


def _calls_made_by(obj: object) -> set[str]:
    """Every name *obj*'s source calls, bare (``f()``) or attributed (``m.f()``).

    Source-level, for the same reason
    ``TestProductLifecycleIsUntestedBecauseOttoShipsNoImplementation`` reads
    source: a behavioural check needs a fake host, and a fake answers whatever
    it was written to answer -- it would pass just as well against a site that
    had stopped calling the guard, if the fake happened not to exercise the
    refusing branch.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(obj)))  # ty: ignore[no-matching-overload]
    called: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            called.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            called.add(node.func.attr)
    return called


def _surfaces_refused_by(obj: object) -> set[str]:
    """Every surface *obj*'s source passes to ``refuse_if_gapped`` as a literal.

    A literal is the only form that can be read here, and requiring one is
    deliberate: a guard that computed its surface at runtime could not be tied
    to a record by any static check, and this table's whole value is that the
    tie is checkable.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(obj)))  # ty: ignore[no-matching-overload]
    surfaces: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.id if isinstance(node.func, ast.Name) else None
        if name != "refuse_if_gapped" or not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            surfaces.add(first.value)
    return surfaces


class TestEveryPathStateIsRepresented:
    """Non-vacuity, first, exactly as ``TestTheTableCanAnswerBothWays`` is.

    Every class below is parametrized over a FILTER of the live paths, and
    ``parametrize`` over an empty sequence produces no failures. A table that
    lost every ``WIRED`` path would report "a wired claim is checked" as green
    while checking nothing -- and a table that lost every ``OPEN`` path would
    report the docs page as in sync with a hole list of zero.
    """

    @pytest.mark.parametrize("state", [PATH_WIRED, PATH_PROBE_REFUSED, PATH_PROTECTED, PATH_OPEN])
    def test_the_table_declares_at_least_one_path_in_this_state(self, state: str) -> None:
        assert _paths_in(state), (
            f"no path in GAPS is {state}, so every assertion parametrized over that state "
            f"is vacuous. If the state genuinely no longer occurs, delete it from "
            f"`_PATH_STATES` and from the docs page's legend deliberately -- do not leave "
            f"a state nothing exercises, because a later record declaring one would then "
            f"be checked by nothing"
        )

    def test_the_totals_account_for_every_declared_path(self) -> None:
        """The derived count is over the same data the parametrization walks."""
        assert sum(gap_path_totals().values()) == sum(len(gap.paths) for gap in GAPS)
        assert set(gap_path_totals()) == {
            PATH_WIRED,
            PATH_PROBE_REFUSED,
            PATH_PROTECTED,
            PATH_OPEN,
        }, "gap_path_totals() must key every state, at zero if need be"


class TestEveryPathNamesRealCode:
    """A site is a dotted name, so a rename must red here rather than rot quietly."""

    @pytest.mark.parametrize(("gap", "path"), _ALL_PATHS)
    def test_the_site_resolves_to_something_callable(self, gap: Gap, path: GapPath) -> None:
        try:
            obj = _resolve_dotted(path.site)
        except (ImportError, AttributeError) as exc:
            pytest.fail(
                f"{gap.surface}: path site {path.site!r} does not resolve ({exc}). The site "
                f"is the whole value of recording a path -- a renamed method or moved "
                f"function has to red here, not sit in the table as a claim about code that "
                f"no longer exists. Update the record, or the name."
            )
        assert callable(obj), (
            f"{gap.surface}: path site {path.site!r} resolves to {obj!r}, which is not "
            f"callable. A path is a place otto EXECUTES something; a constant or a module "
            f"is not one."
        )

    @pytest.mark.parametrize(("gap", "path"), _paths_in(PATH_PROTECTED))
    def test_a_protected_path_names_a_protector_that_exists(self, gap: Gap, path: GapPath) -> None:
        """``PROTECTED`` is the state that stops being true in someone else's file.

        The protector is upstream code this record does not own, so nothing here
        would notice it being deleted or renamed -- and the failure mode is the
        expensive one: the path silently becomes reachable and unguarded while
        the table still reads as covered.
        """
        try:
            obj = _resolve_dotted(path.checked_by)
        except (ImportError, AttributeError) as exc:
            pytest.fail(
                f"{gap.surface}: path {path.site!r} is {PATH_PROTECTED} by "
                f"{path.checked_by!r}, which does not resolve ({exc}). If the upstream "
                f"refusal is gone, this path is no longer protected -- it is OPEN, and "
                f"recording it as protected hides a live hole."
            )
        assert callable(obj), f"{path.checked_by!r} is not callable"

    @pytest.mark.parametrize(("gap", "path"), [p for p in _ALL_PATHS if p.values[1].pinned_by])
    def test_a_named_test_exists(self, gap: Gap, path: GapPath) -> None:
        """``pinned_by`` is a pytest node id; the file and the test have to be there.

        Checked by reading the file rather than by asking pytest, so this stays a
        plain unit test with no nested session -- and so it reds on a rename even
        when the renamed test itself is passing.
        """
        file_part, *names = path.pinned_by.split("::")
        test_file = PROJECT_ROOT / file_part
        assert test_file.is_file(), (
            f"{gap.surface}: path {path.site!r} names {path.pinned_by!r}, but "
            f"{file_part} does not exist"
        )
        tree = ast.parse(test_file.read_text())
        scope: list[ast.stmt] = list(tree.body)
        for name in names:
            match = next(
                (
                    node
                    for node in scope
                    if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name == name
                ),
                None,
            )
            assert match is not None, (
                f"{gap.surface}: path {path.site!r} names {path.pinned_by!r}, but "
                f"{file_part} has no `{name}` at that level. A record pointing at a "
                f"deleted or renamed test claims a guarantee nothing provides."
            )
            scope = list(match.body) if isinstance(match, ast.ClassDef) else []


class TestAWiredClaimIsCheckedNotTrusted:
    """``WIRED`` says "this site reads this record and refuses from it". Prove it.

    Three separate claims, each with its own way of going stale, so each is its
    own assertion: the guard exists, the site calls it, and the guard reaches
    :func:`~otto.host.userland.refuse_if_gapped` with THIS record's surface. A
    deleted guard, a site that stopped calling it, and a guard pointed at a
    different record are three different defects and all three would leave a
    ``WIRED`` claim looking correct.
    """

    @pytest.mark.parametrize(("gap", "path"), _paths_in(PATH_WIRED))
    def test_the_named_guard_exists(self, gap: Gap, path: GapPath) -> None:
        try:
            obj = _resolve_dotted(path.checked_by)
        except (ImportError, AttributeError) as exc:
            pytest.fail(
                f"{gap.surface}: path {path.site!r} claims to be {PATH_WIRED} through "
                f"{path.checked_by!r}, which does not resolve ({exc}). Nothing consults the "
                f"record at that site, so the surface is open there and the table says "
                f"otherwise."
            )
        assert callable(obj), f"{path.checked_by!r} is not callable"

    @pytest.mark.parametrize(("gap", "path"), _paths_in(PATH_WIRED))
    def test_the_site_calls_the_guard_it_names(self, gap: Gap, path: GapPath) -> None:
        guard_name = path.checked_by.rsplit(".", 1)[-1]
        called = _calls_made_by(_resolve_dotted(path.site))
        calls_it = guard_name in called
        assert calls_it, (
            f"{gap.surface}: {path.site} does not call {guard_name}. It is declared "
            f"{PATH_WIRED} on the strength of that call, so without it this path is "
            f"{PATH_OPEN} -- the record and the code disagree, and the record is the one "
            f"a reader trusts. Either restore the call or change the path's state."
        )

    @pytest.mark.parametrize(("gap", "path"), _paths_in(PATH_WIRED))
    def test_the_guard_refuses_from_this_record(self, gap: Gap, path: GapPath) -> None:
        """The claim that makes the table the AUTHORITY rather than decoration.

        A guard that raised on its own would satisfy the two assertions above
        and leave the record unread -- which is exactly what
        :data:`~otto.host.userland.PATH_PROBE_REFUSED` is for, and is not what
        ``WIRED`` says.
        """
        refused = _surfaces_refused_by(_resolve_dotted(path.checked_by))
        refuses_this_record = gap.surface in refused
        assert refuses_this_record, (
            f"{gap.surface}: {path.checked_by} does not pass {gap.surface!r} to "
            f"refuse_if_gapped (it passes {sorted(refused) or 'nothing'}). A guard that "
            f"raises on its own authority is {PATH_PROBE_REFUSED}, not {PATH_WIRED}: "
            f"downgrading this record would not stop it, and its message carries none of "
            f"the record's evidence."
        )

    def test_the_wired_guards_are_derived_and_all_resolve(self) -> None:
        """``wired_guards()`` is the "how many guard functions" count, never retyped."""
        guards = wired_guards()
        assert guards, "no WIRED path names a guard, so this count is vacuous"
        assert len(guards) == len(set(guards)), "wired_guards() must de-duplicate"
        for guard in guards:
            assert callable(_resolve_dotted(guard))


class TestTheFourthStateIsNotEitherOfTheOtherTwo:
    """``shell-transfer-base64`` is why :data:`PATH_PROBE_REFUSED` exists.

    It refuses TODAY, before it sends anything -- so calling its paths
    ``OPEN`` would tell a reader they are exposed when they are not. And it
    refuses on its own probe, rendering its own message, so calling them
    ``WIRED`` would tell a future implementer that this record decides
    something it does not: downgrading the record would not stop the refusal.

    The record must not imply the table is the authority where it is not, and
    these assertions are what hold that line. The load-bearing one is the last:
    the two derived predicates DISAGREE on this surface, which is the whole
    reason there are two of them rather than one called ``fully_wired``.
    """

    def test_the_shell_transfer_paths_are_probe_refused(self) -> None:
        gap = gap_for("shell-transfer-base64")
        assert gap is not None
        assert gap.paths, "the shell transfer's base64 record names no path"
        assert {p.state for p in gap.paths} == {PATH_PROBE_REFUSED}, (
            "every path of `shell-transfer-base64` refuses on its own probe. If one now "
            "reads the record, make it WIRED and check it fires; if one stopped refusing, "
            "make it OPEN and put it on the docs page."
        )

    def test_no_probe_refused_path_claims_a_guard(self) -> None:
        """The state's own discipline: its check is inline, so there is nothing to name.

        A ``checked_by`` here would be read as "the table is consulted through
        this", which is the one thing this state exists to deny.
        """
        for _gap, path in (p.values for p in _paths_in(PATH_PROBE_REFUSED)):
            assert not path.checked_by, path.site

    def test_the_site_really_does_not_consult_the_table(self) -> None:
        """Executed against the source, not asserted about it.

        The mirror image of ``TestAWiredClaimIsCheckedNotTrusted``: if one of
        these sites grew a ``refuse_if_gapped`` call, the record's state would be
        wrong in the direction that flatters otto, and nothing else would notice.
        """
        for gap, path in (p.values for p in _paths_in(PATH_PROBE_REFUSED)):
            called = _calls_made_by(_resolve_dotted(path.site))
            assert "refuse_if_gapped" not in called, (
                f"{gap.surface}: {path.site} now calls refuse_if_gapped, so it is no longer "
                f"{PATH_PROBE_REFUSED} -- it is {PATH_WIRED}. Name the guard and change the "
                f"state; a probe-refused path that secretly reads the table hides the fact "
                f"that the table became load-bearing."
            )

    def test_the_two_derived_predicates_disagree_here(self) -> None:
        gap = gap_for("shell-transfer-base64")
        assert gap is not None
        assert gap.fully_covered is True, (
            "no path of this surface is OPEN -- otto refuses everywhere it is reachable"
        )
        assert gap.consults_the_table is False, (
            "and none of them reads this record, which is why the derived value is called "
            "`fully_covered` and not `fully_wired`. A single predicate would have to lie "
            "about one of these two facts."
        )


class TestTheLiveTableHonoursThePathInvariants:
    """The invariants, asserted over the DECLARED records rather than only synthetic ones.

    ``__post_init__`` makes a bad record unconstructible, so these look
    redundant -- and are not: they are what catches an invariant being LOOSENED.
    Delete the ``untested``-carries-no-paths check from ``__post_init__`` and the
    unit test for it below reds; delete both and this one still reds.
    """

    @pytest.mark.parametrize("gap", _BROKEN_ROWS)
    def test_every_measured_broken_record_names_at_least_one_path(self, gap: Gap) -> None:
        assert gap.paths, (
            f"{gap.surface} refuses and names no path otto touches it from, so it appears "
            f"in the coverage view as neither wired nor open"
        )

    @pytest.mark.parametrize("gap", _UNTESTED_ROWS)
    def test_no_untested_record_carries_a_path(self, gap: Gap) -> None:
        """Those three are not call sites, and two are test-fidelity gaps.

        Inventing paths for them would be inventing evidence -- the same
        objection that moved ``install``/``stage`` out of the rejected-candidates
        block and into a record.
        """
        assert gap.paths == [], (
            f"{gap.surface} is {UNTESTED} and carries {len(gap.paths)} path(s). An untested "
            f"surface is one nobody has run; a path is a place otto touches it and would be "
            f"a claim no measurement backs."
        )

    @pytest.mark.parametrize(("gap", "path"), _ALL_PATHS)
    def test_every_path_says_what_its_state_means_here(self, gap: Gap, path: GapPath) -> None:
        assert path.detail.strip(), f"{gap.surface}: {path.site} has an empty detail"

    def test_no_site_is_recorded_twice_for_one_surface(self) -> None:
        """Two records for one site would let a reader see whichever came first."""
        for gap in GAPS:
            sites = [p.site for p in gap.paths]
            duplicates = sorted({s for s in sites if sites.count(s) > 1})
            assert not duplicates, f"{gap.surface} records {duplicates} more than once"


class TestGapPathRejectsAClaimItCannotSupport:
    """``GapPath.__post_init__``, the way ``Gap``'s own invariants are tested above.

    Each of these is a record that would LOOK fine in review and claim coverage
    it does not have -- which is the only way a coverage table can lie.
    """

    def test_a_site_that_is_not_a_dotted_otto_name_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="dotted name"):
            _valid_path(site="SessionManager.run_cmd")

    def test_an_unknown_state_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="not one of"):
            _valid_path(state="PROBABLY_FINE")

    def test_a_state_with_no_detail_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="verdict with no argument"):
            _valid_path(detail="")

    @pytest.mark.parametrize("state", [PATH_WIRED, PATH_PROTECTED])
    def test_a_checked_state_that_names_nothing_is_rejected(self, state: str) -> None:
        with pytest.raises(ValueError, match="checked_by"):
            _valid_path(state=state, pinned_by="tests/unit/host/test_gap_registry.py::x")

    @pytest.mark.parametrize("state", [PATH_OPEN, PATH_PROBE_REFUSED])
    def test_an_unchecked_state_that_claims_a_checker_is_rejected(self, state: str) -> None:
        with pytest.raises(ValueError, match="may claim a checker"):
            _valid_path(state=state, checked_by="otto.host.userland.refuse_if_gapped")

    def test_a_checker_that_is_not_a_dotted_otto_name_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="which is not"):
            _valid_path(state=PATH_WIRED, checked_by="refuse_if_gapped")

    def test_a_protected_path_with_no_test_is_rejected(self) -> None:
        """The one state whose truth lives in another package's code."""
        with pytest.raises(ValueError, match="names no test"):
            _valid_path(state=PATH_PROTECTED, checked_by="otto.host.userland.gap_for")

    def test_a_pinned_by_that_is_not_a_node_id_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="pytest node id"):
            _valid_path(pinned_by="test_gap_registry.py::test_x")


class TestGapRejectsARecordWhoseCoverageClaimIsEmpty:
    """The two path invariants on the record itself, each shown to be load-bearing."""

    def test_measured_broken_with_no_paths_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="names no path"):
            Gap(**_valid_gap(paths=[]))

    def test_untested_with_paths_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="path"):
            Gap(**_valid_gap(status=UNTESTED, measured_on=""))

    def test_an_untested_record_with_no_paths_is_accepted(self) -> None:
        """The control: the pair above is only meaningful if this passes."""
        gap = Gap(**_valid_gap(status=UNTESTED, measured_on="", paths=[]))
        assert gap.paths == []
        assert gap.fully_covered is False
        assert gap.consults_the_table is False

    def test_paths_in_state_refuses_a_state_it_has_never_heard_of(self) -> None:
        """A typo'd state would otherwise answer "no paths" forever."""
        with pytest.raises(ValueError, match="not a gap path state"):
            GAPS[0].paths_in_state("WIRED_ISH")
