"""What the BED conformance venue offers, asserted with no bed.

The bed venue's space is a pure function of the committed lab data under
``tests/_fixtures/lab_data/`` -- which hosts declare which lab, and which
``(term, transfer)`` menus the hosts otto builds from those entries report.
So its size, its order, its de-duplication and its kinds are all assertable
here, in the hostless lane, and nothing in this file contacts a host.

Not to be confused with :mod:`otto.testing.conformance`, which asserts that
pluggable *backend interfaces* conform. This covers the *host contract*
conformance suite's BED cell resolution under ``tests/conformance/``.
"""

import asyncio
import contextlib
import dataclasses
import fcntl
import importlib
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest
from typing_extensions import Self

from otto.context import _active, try_get_context
from otto.host.command_frame import FRAME_CLASSES
from otto.host.embedded_filesystem import build_filesystem
from otto.host.embedded_host import EmbeddedHost
from otto.host.remote_host import RemoteHost
from otto.result import CommandResult, Results, Status
from tests._fixtures._console_lock import RESOURCE_NAME
from tests._fixtures.labdata import host_data, lab_data_path
from tests._fixtures.paths import PROJECT_ROOT
from tests._fixtures.profiles import (
    Cell,
    HostAxes,
    _ensure_custom_frames,
    axes_for,
    axis_space,
)
from tests._fixtures.support_matrix import discover_contracts
from tests.conformance import _bed, _console_safety, _lab_context
from tests.conformance import _vocabulary as _vocabulary_module
from tests.conformance import conftest as _conformance_conftest
from tests.conformance import test_exec_contract as _exec_contract
from tests.conformance import test_timeout_contract as _timeout_contract
from tests.conformance._bed import (
    _POSIX_SCRATCH_DIR,
    BED_BUSYBOX,
    BED_LABS,
    BED_TECH,
    BED_UNIX,
    BED_ZEPHYR,
    bed_kind,
    bed_scratch_dir,
    bed_space,
    build_bed_host,
)
from tests.conformance._cells import hermetic_space
from tests.conformance._console_safety import (
    console_lock_dir,
    opens_a_single_client_console,
    serialized_console,
    unhonored_console_lock,
)
from tests.conformance._controls import discover_controls
from tests.conformance._lab_context import BED_LAB_NAME, bed_lab, hop_targets
from tests.conformance._observation import domain_exclusions
from tests.conformance._resolved import ResolvedCell
from tests.conformance._sample import cell_label
from tests.conformance._vocabulary import (
    OTTO_SENTINEL_PREFIX,
    POSIX,
    ZEPHYR_SHELL,
    Vocabulary,
    vocabulary_for_userland,
)
from tests.conformance.test_timeout_contract import _BUDGET_S
from tests.conformance.test_transfer_contract import _PAYLOAD_NAME, applicable_cell
from tests.conftest import remote_name

# THE RECORDED ORDER. Not derived, not sorted, not regenerated at import: a
# recorded expectation is the only thing that can see a STABLE-BUT-WRONG
# order, which is precisely what comparing two calls of the same
# implementation cannot (measured -- see
# `test_bed_space_order_does_not_move_between_calls` below).
#
# It reads lab-major in `BED_LABS` order, then the lab file's own host order,
# then each host's term-major crossing of the menus IT reported. `test2`
# leading with `telnet` where `test1` and `test3` lead with `ssh` is not a
# transcription slip: it is what the host reports, and a sort here or in the
# resolver would be inventing a value the host did not give.
#
# `test1` appears ONCE, under `unix`, though it declares membership in
# `busybox` as well -- the de-duplication keeps first appearance.
RECORDED_ORDER = [
    ("test1", "ssh", "scp"),
    ("test1", "ssh", "sftp"),
    ("test1", "ssh", "ftp"),
    ("test1", "ssh", "nc"),
    ("test1", "telnet", "scp"),
    ("test1", "telnet", "sftp"),
    ("test1", "telnet", "ftp"),
    ("test1", "telnet", "nc"),
    ("test2", "telnet", "nc"),
    ("test2", "telnet", "scp"),
    ("test2", "telnet", "sftp"),
    ("test2", "telnet", "ftp"),
    ("test2", "ssh", "nc"),
    ("test2", "ssh", "scp"),
    ("test2", "ssh", "sftp"),
    ("test2", "ssh", "ftp"),
    ("test3", "ssh", "scp"),
    ("test3", "ssh", "sftp"),
    ("test3", "ssh", "ftp"),
    ("test3", "ssh", "nc"),
    ("test3", "telnet", "scp"),
    ("test3", "telnet", "sftp"),
    ("test3", "telnet", "ftp"),
    ("test3", "telnet", "nc"),
    ("bb1161", "telnet", "shell"),
    ("bb1161", "telnet", "nc"),
    ("bb1211", "telnet", "shell"),
    ("bb1211", "telnet", "nc"),
    ("bb1281", "telnet", "shell"),
    ("bb1281", "telnet", "nc"),
    ("bb1310", "telnet", "shell"),
    ("bb1310", "telnet", "nc"),
    ("bb1350", "telnet", "shell"),
    ("bb1350", "telnet", "nc"),
    ("test4", "ssh", "scp"),
    ("test4", "ssh", "sftp"),
    ("test4", "ssh", "ftp"),
    ("test4", "ssh", "nc"),
    ("test4", "telnet", "scp"),
    ("test4", "telnet", "sftp"),
    ("test4", "telnet", "ftp"),
    ("test4", "telnet", "nc"),
    ("zephyr37_fat", "telnet", "console"),
    ("zephyr37_lfs", "telnet", "console"),
    ("zephyr37_nofs", "telnet", "console"),
    ("zephyr27_fat", "telnet", "console"),
    ("zephyr44_lfs", "telnet", "console"),
    ("zephyr37_llext", "telnet", "console"),
    ("zephyr44_llext", "telnet", "console"),
]


def _triples(space):
    return [(r.cell.element, r.cell.term, r.cell.transfer) for r in space]


def test_bed_space_is_not_empty():
    """F5: the ``NotImplementedError`` this venue replaces existed for this.

    An empty space satisfies every sampling assertion vacuously, so a run
    that resolved nothing would be indistinguishable from a run that
    certified everything.
    """
    assert bed_space(), "an empty bed space certifies everything and asserts nothing"


def test_bed_space_crosses_every_bed_lab_host():
    """Every family the bed actually has must be reachable in the space."""
    elements = {r.cell.element for r in bed_space()}
    assert "test1" in elements, f"the unix lab is missing from the bed space: {sorted(elements)}"
    assert any(e.startswith("zephyr") for e in elements), sorted(elements)
    assert any(e.startswith("bb") for e in elements), sorted(elements)


def test_every_bed_lab_contributes_at_least_one_host():
    """A lab named in ``BED_LABS`` that contributes nothing is a silent hole.

    ``axis_space`` raises on a lab NO host declares, so a typo cannot survive
    -- but a lab whose cells were all dropped downstream would look exactly
    like a lab that was never listed.
    """
    space = bed_space()
    for lab in BED_LABS:
        members = {c.element for c in axis_space(lab, BED_TECH)}
        offered = {r.cell.element for r in space} & members
        assert offered, f"lab {lab!r} contributed no host to the bed space"


def test_bed_labs_names_every_lab_the_bed_data_declares():
    """A lab added to the bed data and not crossed here is coverage nobody asked for.

    Reads the memberships out of the lab file rather than restating them, so
    the day a new lab appears this fails and names it instead of the venue
    quietly continuing to cross the old three.
    """
    hosts = json.loads(lab_data_path(BED_TECH).read_text())["hosts"]
    declared = {lab for host in hosts for lab in (host.get("labs") or [])}
    assert declared == set(BED_LABS), (
        f"{BED_TECH} lab data declares labs {sorted(declared)} but the bed venue "
        f"crosses {sorted(BED_LABS)}"
    )


def test_bed_space_order_matches_the_recorded_expectation():
    """The order is pinned against a RECORD, because self-comparison cannot see it.

    This is the fix for the ordering test the plan predicted would be blind:
    ``test_bed_space_order_does_not_move_between_calls`` compares two calls of
    one implementation, so any stable order passes it, right or wrong.
    MEASURED: reversing the ``unix`` lab's cells inside ``bed_space`` leaves
    that test green and reddens this one.
    """
    assert _triples(bed_space()) == RECORDED_ORDER


def test_bed_space_order_does_not_move_between_calls():
    """Catches an order that moves WITHIN a process. It cannot catch a wrong one.

    A space ordered by a ``set`` of strings, or a generator consumed once,
    reorders between two calls and is caught here. A stable-but-wrong order
    is invisible to this test by construction -- measured, see
    ``test_bed_space_order_matches_the_recorded_expectation`` above, which is
    the guard that sees it. Kept because the two failure modes are different:
    that one pins WHICH order, this one pins that there is only one.
    """
    assert _triples(bed_space()) == _triples(bed_space())


def test_bed_menus_are_emitted_in_the_hosts_own_order_never_sorted():
    """``test2`` reports its terms as ``['telnet', 'ssh']``; the space must say so.

    The discriminator is real rather than theoretical: ``test1`` and ``test3``
    report ``['ssh', 'telnet']``, so a resolver that sorted, or that imposed
    any canonical menu order, would agree with two hosts out of three and
    disagree with ``test2``. Asserted against what ``axes_for`` reports for
    ``test2`` rather than against the literal ``telnet``, so this pins the
    RULE (host order wins) and not today's lab file.
    """
    terms = axes_for("test2", BED_TECH).terms
    seen = list(dict.fromkeys(r.cell.term for r in bed_space() if r.cell.element == "test2"))
    assert seen == terms, f"test2's terms came out {seen}, but the host reports {terms}"


def test_no_bed_cell_is_offered_twice():
    """A duplicate takes two of the sampler's slots and asserts one contract twice.

    ``draw`` samples without replacement and ranks on the cell's label, so
    two identical cells rank identically and both land in the same draw --
    while the run's ``drew N of M`` line still reads correctly.
    """
    cells = [r.cell for r in bed_space()]
    dupes = [cell for cell, n in Counter(cells).items() if n > 1]
    assert not dupes, f"the bed space offers {len(dupes)} cell(s) twice: {dupes}"


def test_the_bed_labs_overlap_so_the_dedup_is_not_vacuous():
    """The guard above must be able to FAIL. Today it can, and this proves it.

    ``test_no_bed_cell_is_offered_twice`` only means something while the raw
    concatenation actually contains a repeat. It does: ``test1`` declares
    membership in both ``unix`` and ``busybox``, so ``BED_LABS`` concatenated
    without de-duplication is 57 cells with ``test1``'s 8 listed twice. If a
    future lab-data edit ends that overlap, this fails and says so, rather
    than letting the de-duplication quietly become untested code.
    """
    raw = [cell for lab in BED_LABS for cell in axis_space(lab, BED_TECH)]
    repeats = sorted({cell.element for cell, n in Counter(raw).items() if n > 1})
    assert repeats, (
        f"no host in {sorted(BED_LABS)} declares two of those labs any more, so "
        f"test_no_bed_cell_is_offered_twice can no longer fail -- either the "
        f"de-duplication in bed_space() is now dead code, or this data changed by "
        f"accident"
    )


def test_bed_cell_labels_are_unique():
    """The sampler's rank is keyed on the label; a collision reweights the space.

    Distinct cells are not sufficient on their own -- the label also carries
    the kind, so this is what pins that the pair together names one cell.
    """
    labels = [cell_label(r) for r in bed_space()]
    assert len(labels) == len(set(labels))


def test_every_bed_cell_names_how_it_was_built():
    """``kind`` is what the run logs; an unnamed cell is unreproducible."""
    for resolved in bed_space():
        assert resolved.kind in {BED_UNIX, BED_BUSYBOX, BED_ZEPHYR}, (
            f"{resolved.cell.element} resolved to kind {resolved.kind!r}"
        )


def test_the_bed_space_covers_more_than_one_kind():
    """Otherwise every kind assertion here passes for the wrong reason."""
    kinds = {r.kind for r in bed_space()}
    assert kinds == {BED_UNIX, BED_BUSYBOX, BED_ZEPHYR}, kinds


def test_bed_kind_is_not_read_off_the_lab_name():
    """``test4`` declares the ``embedded`` lab and is a plain GNU unix VM.

    A kind derived from the lab a host belongs to calls it a Zephyr guest and
    would then assert Zephyr expectations against a Debian box.
    """
    assert "test4" in {c.element for c in axis_space("embedded", BED_TECH)}
    assert bed_kind("test4") == BED_UNIX


def test_bed_kind_is_not_read_off_os_type():
    """The five BusyBox guests declare ``os_type: "unix"`` in the lab data.

    A kind read off ``os_type`` files them as plain unix hosts, and the
    pinned-userland axis -- the entire reason those guests exist -- vanishes.
    """
    entry = next(
        host
        for host in json.loads(lab_data_path(BED_TECH).read_text())["hosts"]
        if host["element"] == "bb1161"
    )
    assert entry["os_type"] == "unix"
    assert bed_kind("bb1161") == BED_BUSYBOX


def test_bed_kind_is_read_off_the_userland_not_the_element_name(monkeypatch):
    """Every Zephyr guest in this lab is *named* ``zephyr*``; that is a coincidence.

    INJECTS the hostile condition rather than inheriting it: a host whose
    name says nothing and whose userland says ``zephyr-9.9``. An
    ``element.startswith("zephyr")`` sniff passes every real assertion in
    this file and fails here.
    """
    axes = HostAxes(
        os_type="renamed-profile",
        userland="zephyr-9.9",
        terms=["telnet"],
        transfers=["console"],
        hop_depth=1,
        docker_capable=False,
    )
    monkeypatch.setattr(_bed, "axes_for", lambda element, tech: axes)
    assert bed_kind("a-name-that-says-nothing") == BED_ZEPHYR


def test_an_unrecognised_userland_has_no_kind(monkeypatch):
    """A new userland layer must announce itself, not be filed under unix.

    Falling back to a default bucket is the failure that cannot be seen from
    a passing run: the cells would resolve, the contracts would run, and they
    would carry the wrong expectations under a kind that names the wrong
    machinery.
    """
    axes = HostAxes(
        os_type="unix",
        userland="toybox-0.8.11",
        terms=["ssh"],
        transfers=["scp"],
        hop_depth=0,
        docker_capable=False,
    )
    monkeypatch.setattr(_bed, "axes_for", lambda element, tech: axes)
    with pytest.raises(
        ValueError, match=re.escape("no bed venue kind for userland 'toybox-0.8.11'")
    ):
        bed_kind("newcomer")


# --- The openers (Task 2): what a cell actually stands up -------------------
#
# All hostless. Building a host is a pure factory call over committed lab
# data -- measured: `BaseHost.__aenter__` returns `self` and contacts nothing,
# so construction reaches no network -- which is what lets the pin, the
# pair check and the loud-failure path all be asserted here with no bed. The
# one thing that cannot be asserted without a bed is that a real host answers,
# and that has its own witness under `tests/conformance/`.


class _FakeProbeHost:
    """A host stand-in that answers the connect probe however the test says.

    Copies the SEAM'S CALL SHAPE, not a host: `_opener_for` enters the object
    as an async context manager and calls `verify_connection()` on it, so
    those two are what this provides. `closed` records the `__aexit__`, which
    is how the leak assertion below can tell a probe failure that released
    the transports from one that stranded them.
    """

    def __init__(self, answer: "CommandResult | None" = None, hang: bool = False) -> None:
        self.answer = answer
        self.hang = hang
        self.closed = False
        self.probes = 0

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.closed = True

    async def verify_connection(self) -> "CommandResult":
        self.probes += 1
        if self.hang:
            await asyncio.sleep(3600)
        assert self.answer is not None
        return self.answer


def _answer(status: Status, value: str) -> CommandResult:
    return CommandResult(status=status, value=value, command="connect", retcode=0)


# Two real cells of the space, named so the assertions below can say WHICH
# cell they mean instead of indexing into a list whose order is the thing
# under test elsewhere in this file.
#
# `OFF_DEFAULT_CELL` is deliberately neither of `test1`'s first menu entries
# (measured: terms ['ssh', 'telnet'], transfers ['scp', 'sftp', 'ftp', 'nc']),
# because a cell that IS the default cannot tell a pin that was honoured from
# a pin that was dropped.
DEFAULT_CELL = ("test1", "ssh", "scp")
OFF_DEFAULT_CELL = ("test1", "telnet", "nc")


def _resolved(triple):
    """The ResolvedCell the real space offers for *triple*."""
    return next(r for r in bed_space() if (r.cell.element, r.cell.term, r.cell.transfer) == triple)


def test_every_bed_cell_carries_a_callable_opener():
    """The plan's Step 2 check: every cell can be opened at all.

    Weak on its own -- it cannot tell an opener that reaches the right host
    from one that reaches the wrong one -- so it is the floor, and the pair
    assertions below are what make it mean something.
    """
    space = bed_space()
    assert space
    for resolved in space:
        assert callable(resolved.open_host), f"{resolved.cell.element} has no opener"


def test_every_bed_cell_builds_a_host_reporting_that_cells_pair():
    """THE crossing guard: a cell must build the transport it was drawn for.

    The failure this exists for is silent. An opener that dropped the pin
    would build each host on its own default ``(term, transfer)`` -- one row
    per host, which is exactly what
    ``tests/integration/host/test_host_contract.py`` already covers -- while
    every cell id here still claimed the pair it was drawn for. The lane
    would stay green and would have stopped crossing anything.

    Over the WHOLE space rather than a sample: the pin is per cell, and the
    cells that would expose a dropped pin are precisely the ones whose pair
    is not their host's default.
    """
    wrong = []
    for resolved in bed_space():
        host = build_bed_host(resolved.cell)
        if (host.term, host.transfer) != (resolved.cell.term, resolved.cell.transfer):
            wrong.append(f"{cell_label(resolved)} built {host.term}/{host.transfer}")
    assert not wrong, f"these cells built a transport nobody drew: {wrong}"


def test_the_space_contains_pairs_that_are_not_the_hosts_default():
    """The guard above must be able to FAIL, and only these cells can make it.

    A space in which every cell happened to be its host's default pair would
    satisfy the pin check with the pin deleted -- the same shape as
    ``bed_kind`` passing every real-data assertion while sniffing the element
    name. MEASURED: ``test1``'s menus are ``['ssh', 'telnet']`` x
    ``['scp', 'sftp', 'ftp', 'nc']``, so seven of its eight cells are
    off-default. This names the count so a lab-data edit that flattened the
    menus could not quietly make the guard vacuous.
    """
    off_default = []
    for resolved in bed_space():
        axes = axes_for(resolved.cell.element, BED_TECH)
        if (resolved.cell.term, resolved.cell.transfer) != (axes.terms[0], axes.transfers[0]):
            off_default.append(cell_label(resolved))
    assert len(off_default) >= 30, (
        f"only {len(off_default)} of {len(bed_space())} bed cells ask for a pair that is "
        f"not their host's default, so test_every_bed_cell_builds_a_host_reporting_"
        f"that_cells_pair would largely pass with the pin deleted"
    )


def test_a_pair_the_host_does_not_serve_is_refused_by_the_factory():
    """The factory validates the pin; this pins that the venue leans on it.

    Not otto's test -- it is the reason ``build_bed_host`` does not re-check
    menu membership itself. INJECTED: a cell asking ``bb1161`` for ``ssh``,
    which the real space never offers because ``axis_space`` crosses only the
    menus the host reported.
    """
    with pytest.raises(ValueError, match=re.escape("term 'ssh' is not in this host's term menu")):
        build_bed_host(Cell("bb1161", "ssh", "shell"))


def test_a_host_that_reports_a_pair_other_than_the_cells_is_refused(monkeypatch):
    """A pin that is WRONG but IN the menu is invisible to the factory.

    INJECTS that condition rather than inheriting it: the factory is replaced
    by one that ignores the pin and answers with the host's first menu entry
    -- the exact shape a dropped pin would produce, and one every real cell
    would otherwise accept, because ``ssh``/``scp`` IS in ``test1``'s menu.
    """

    def collapsed(entry, *args, **kwargs):
        return SimpleNamespace(term=entry["valid_terms"][0], transfer=entry["valid_transfers"][0])

    monkeypatch.setattr(_bed, "create_host_from_dict", collapsed)
    with pytest.raises(RuntimeError, match="built a host reporting ssh/scp instead"):
        build_bed_host(Cell(*OFF_DEFAULT_CELL))


@pytest.mark.asyncio
async def test_the_opener_probes_the_host_because_entering_it_does_not(monkeypatch):
    """MEASURED: ``BaseHost.__aenter__`` returns ``self`` and contacts nothing.

    So an opener that only entered the host would hand a contract a host it
    had never reached, and a contract asserting a FAILING command's status
    would pass against a host that was switched off. The probe is what makes
    "opened" mean "answered", and this pins that it actually runs.
    """
    fake = _FakeProbeHost(_answer(Status.Success, "Connection successful"))
    monkeypatch.setattr(_bed, "build_bed_host", lambda cell: fake)
    async with _resolved(DEFAULT_CELL).open_host() as host:
        assert host is fake
    assert fake.probes == 1, "the opener yielded a host it never asked to answer"
    assert fake.closed


async def _opening_raises(resolved) -> "BaseException | None":
    """Whatever opening *resolved* raised, as a VALUE, or ``None`` if it opened.

    Deliberately not ``pytest.raises``: this file's never-skip rule is about
    an exception type ``pytest.raises`` cannot judge. A ``pytest.skip`` inside
    the opener is a ``Skipped``, which propagates past a
    ``pytest.raises(RuntimeError)`` and marks the test SKIPPED -- an outcome
    that is not red, and so is invisible in exactly the way the rule exists to
    forbid. Handing the exception back as a value lets the caller assert on
    its type in the test body, where a wrong one is a FAILURE.
    """
    try:
        async with resolved.open_host():
            return None
    except BaseException as exc:  # noqa: BLE001 - capturing the type IS the assertion
        return exc


@pytest.mark.asyncio
async def test_a_bed_host_that_does_not_answer_fails_loudly_and_never_skips(monkeypatch):
    """Spec s4: fails loudly, naming the host -- never a skip.

    INJECTED at the seam that can be hostile without a bed: every other test
    here builds the host with the real factory, and this one replaces it with
    a host that refuses the connect probe.

    Asserts the failure is NOT a ``Skipped``, not merely that something was
    raised -- see :func:`_opening_raises` for why that distinction needs a
    helper. Also asserts the text NAMES the element: the bare
    ``ConnectionRefusedError`` otto raises on the first command carries an
    address and a port and no element at all (measured), which is the one
    thing spec s4 requires.
    """
    refused = _answer(Status.Error, "[Errno 111] Connect call failed ('10.10.200.11', 22)")
    monkeypatch.setattr(_bed, "build_bed_host", lambda cell: _FakeProbeHost(refused))
    raised = await _opening_raises(_resolved(DEFAULT_CELL))
    assert raised is not None, "the opener yielded a host that never answered"
    assert not isinstance(raised, pytest.skip.Exception), (
        f"the bed venue skipped an unreachable host: {raised} -- a skipped cell "
        f"reports success for a contract nobody ran"
    )
    assert isinstance(raised, RuntimeError), f"{type(raised).__name__}: {raised}"
    assert "'test1'" in str(raised), str(raised)
    assert "ssh/scp" in str(raised), str(raised)
    assert "Errno 111" in str(raised), str(raised)


@pytest.mark.asyncio
async def test_a_bed_host_that_never_answers_is_bounded(monkeypatch):
    """A powered-off VM blackholes the SYN; ``verify_connection`` is unbounded.

    Without the bound the lane does not fail, it HANGS -- and a hung lane is
    reported by whatever kills it, naming pytest's timeout rather than the
    host. INJECTS a probe that never returns, against a bound shortened so
    the test does not have to wait out the real one.
    """
    fake = _FakeProbeHost(hang=True)
    monkeypatch.setattr(_bed, "build_bed_host", lambda cell: fake)
    monkeypatch.setattr(_bed, "_ANSWER_TIMEOUT_S", 0.05)
    with pytest.raises(RuntimeError, match="no answer within"):
        async with _resolved(DEFAULT_CELL).open_host():
            pass
    assert fake.closed, "a host that never answered was left open"


@pytest.mark.asyncio
async def test_a_failed_probe_still_closes_the_hosts_transports(monkeypatch):
    """The loud failure must not also be a leak.

    An opener that raised before entering the host would strand whatever
    ``verify_connection`` had already dialled -- and on a Zephyr guest a
    stranded console is the wedge issue #260 records, not merely an untidy
    socket. Injected here rather than inferred from the code shape.
    """
    fake = _FakeProbeHost(_answer(Status.Error, "refused"))
    monkeypatch.setattr(_bed, "build_bed_host", lambda cell: fake)
    with pytest.raises(RuntimeError):
        async with _resolved(DEFAULT_CELL).open_host():
            pass
    assert fake.closed, "the probe failed and the host was never closed"


def test_the_openers_reuse_ottos_factory_rather_than_constructing_a_host(monkeypatch):
    """The bed's own fixtures build hosts with ``create_host_from_dict``; so must this.

    A second construction path -- ``UnixHost(...)`` assembled from raw lab
    fields, say -- would be this suite restating otto's answer and would keep
    passing after the factory stopped agreeing with it (profile defaults, the
    options merge, the product and dev-tool providers). Pinned by INTERCEPT:
    the factory is replaced by a recorder, and a builder that bypassed it
    would record nothing.
    """
    calls = []

    def recorder(entry, *args, **kwargs):
        calls.append(entry)
        return SimpleNamespace(term=entry["term"], transfer=entry["transfer"])

    monkeypatch.setattr(_bed, "create_host_from_dict", recorder)
    build_bed_host(Cell(*OFF_DEFAULT_CELL))
    assert len(calls) == 1, "build_bed_host did not go through otto's factory exactly once"
    assert calls[0]["element"] == "test1"
    assert (calls[0]["term"], calls[0]["transfer"]) == OFF_DEFAULT_CELL[1:]
    assert calls[0]["ip"] == "10.10.200.11", (
        "the entry handed to the factory is not the committed lab entry"
    )


def test_a_cell_builds_even_when_the_out_of_tree_frame_was_evicted():
    """``zephyr27_fat`` needs a command frame that does not live in otto.

    It declares ``command_frame: "zephyr-inline"``, a class under
    ``tests/custom_hosts``, and the root conftest's ``_isolate_registries``
    fixture evicts the registering module per test. Resolution registers it
    once at collection, so a builder that relied on THAT would work under a
    whole-tree run and fail when the opener happened to fire after an
    eviction -- the invocation-shape dependence
    ``tests._fixtures.profiles._ensure_custom_frames`` was written for.

    INJECTS the eviction rather than hoping to inherit it. Whether the frame
    happens to be registered when this test starts depends on what else the
    session collected and on where ``_isolate_registries`` last restored --
    measured: absent under ``pytest tests/unit/test_conformance_bed.py``,
    present under the whole hostless lane. A test that INHERITED the absence
    would be asserting nothing in the second case and would not say so.

    ``Registry.unregister``, not ``monkeypatch.delitem``: ``FRAME_CLASSES`` is
    an :class:`~otto.registry.Registry`, not a dict, and its ``get`` takes no
    default -- so ``delitem`` raised ``TypeError: Registry.get() takes 2
    positional arguments but 3 were given`` in exactly the lane where the key
    was present, i.e. the only lane where the injection was doing anything.
    """
    had_frame = "zephyr-inline" in FRAME_CLASSES
    if had_frame:
        FRAME_CLASSES.unregister("zephyr-inline")
    try:
        host = build_bed_host(Cell("zephyr27_fat", "telnet", "console"))
    finally:
        if had_frame and "zephyr-inline" not in FRAME_CLASSES:
            _ensure_custom_frames()
    assert (host.term, host.transfer) == ("telnet", "console")


# --- The lab context a hopped cell needs (Task 4b) --------------------------
#
# 17 of the 49 bed cells name a host that is only reachable THROUGH another
# one: the five BusyBox guests hop `test1` and the seven Zephyr guests hop
# `test4` (measured, `tests/_fixtures/lab_data/tech1/lab.json`).
# `RemoteHost._build_hop_transport` resolves that hop id against the host's
# own `_lab` back-reference or, failing that, against the active
# `OttoContext` -- and `create_host_from_dict` hands back a host with
# NEITHER. So before Task 4b every one of those 17 cells failed before any
# transport existed:
#
#     RuntimeError: Host 'bb1161 qemu' cannot resolve hop 'test1': the host
#     has no lab back-reference and there is no active OttoContext.
#
# Hostless throughout, and by construction rather than by hope:
# `asyncssh.connect` is replaced with a stub that refuses to dial, so the
# tunnel factory runs its resolution FOR REAL and stops at the wire. What the
# stub records -- the address it was asked to dial -- is the assertion: an
# opener whose context resolves `bb1161`'s hop dials `test1`'s IP, and one
# that cannot resolve it never reaches the stub at all.

HOPPED_BUSYBOX_CELL = ("bb1161", "telnet", "shell")
HOPPED_ZEPHYR_CELL = ("zephyr37_fat", "telnet", "console")

# The IPs the two hop targets declare, from the same committed lab data the
# space is built from. Written out rather than read back through
# `host_data`, so a lab entry that moved would fail HERE, loudly, instead of
# agreeing with itself.
TEST1_IP = "10.10.200.11"
TEST4_IP = "10.10.200.14"


class _DialRefusedError(Exception):
    """What the stubbed ``asyncssh.connect`` raises instead of dialling out."""


def _dial_recorder(monkeypatch) -> "list[str]":
    """Replace ``asyncssh.connect`` with a stub; return the list of dialled IPs.

    ``RemoteHost._build_hop_transport`` imports ``connect`` from ``asyncssh``
    inside the method body, so patching the attribute on the module reaches
    the call the tunnel factory actually makes -- confirmed by the recorder
    filling in at all, which is itself one of the assertions below.
    """
    dialled: "list[str]" = []

    async def _refuse(ip, *args, **kwargs):
        dialled.append(ip)
        raise _DialRefusedError(f"stubbed asyncssh.connect refused to dial {ip}")

    monkeypatch.setattr("asyncssh.connect", _refuse)
    return dialled


async def _always_answers(self) -> CommandResult:
    """A ``verify_connection`` stand-in that answers without a wire."""
    return _answer(Status.Success, "Connection successful")


@contextlib.contextmanager
def _no_otto_context():
    """Run the block with the OttoContext ContextVar forced to ``None``."""
    token = _active.set(None)
    try:
        yield
    finally:
        _active.reset(token)


def _entry(element: str, *, hop: "str | None" = None, board: "str | None" = None) -> dict:
    """A BUILDABLE lab entry of this test's own making, based on a real one.

    Copied from ``test1``'s committed entry rather than assembled field by
    field: :func:`tests.conformance._lab_context.bed_lab` builds every hop
    target through otto's factory, so an entry invented from scratch would
    fail menu validation long before reaching what the test is about.
    """
    entry = dict(host_data("test1", BED_TECH))
    entry["element"] = element
    entry.pop("hop", None)
    entry.pop("board", None)
    if hop is not None:
        entry["hop"] = hop
    if board is not None:
        entry["board"] = board
    return entry


def _fabricate_lab_data(monkeypatch, tmp_path, entries: "list[dict]") -> None:
    """Point ``_lab_context`` at a lab file *this test* wrote.

    Both readers are replaced, because the module reads the file twice by two
    different routes -- the whole roster through ``lab_data_path`` and one
    entry at a time through ``host_data`` -- and a fabrication that moved only
    one of them would have the derivation and the construction disagreeing
    about which lab they are in.
    """
    path = tmp_path / "lab.json"
    path.write_text(json.dumps({"hosts": entries, "links": []}))
    monkeypatch.setattr(_lab_context, "lab_data_path", lambda tech="tech1": path)
    monkeypatch.setattr(
        _lab_context,
        "host_data",
        lambda ne, tech="tech1": next(e for e in entries if e["element"] == ne),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("triple", "hop_ip"),
    [(HOPPED_BUSYBOX_CELL, TEST1_IP), (HOPPED_ZEPHYR_CELL, TEST4_IP)],
    ids=["busybox", "zephyr"],
)
async def test_a_hopped_cell_resolves_its_hop_while_the_venue_holds_it_open(
    monkeypatch, triple, hop_ip
):
    """THE Task 4b contract: a hopped cell's opener can resolve its hop.

    Asserted on the ABSENCE of the hop-resolution error and not on a
    successful command, deliberately: this is about the context the venue
    installs, not about whether a BusyBox guest is powered on today. A test
    that needed a live guest would go red for reasons that have nothing to
    do with what it names.

    Both hopped kinds, because they hop through DIFFERENT hosts -- the
    BusyBox guests through ``test1`` and the Zephyr guests through ``test4``
    -- so a lab that carried only one of them would satisfy a single-cell
    version of this test while leaving seven cells or ten unopenable.

    The probe is replaced by one that resolves the hop instead of dialling
    the host, which is the one thing that can be observed from INSIDE the
    opener's window without a bed. Same call shape as the seam it stands in
    for (async, no arguments, answers with a ``CommandResult``), so
    ``_require_an_answer`` reads it exactly as it reads the real one.
    """
    dialled = _dial_recorder(monkeypatch)
    resolved = _resolved(triple)
    raised: "list[BaseException]" = []

    async def _resolve_the_hop_instead_of_probing(self):
        try:
            await self._build_hop_transport().get_tunnel()
        except BaseException as exc:  # noqa: BLE001 - the exception IS the reading
            raised.append(exc)
        return _answer(Status.Success, "hop resolved")

    monkeypatch.setattr(RemoteHost, "verify_connection", _resolve_the_hop_instead_of_probing)
    async with resolved.open_host():
        pass

    assert raised, "the stand-in probe never ran, so nothing was resolved"
    assert isinstance(raised[0], _DialRefusedError), (
        f"{cell_label(resolved)} did not get as far as dialling its hop: {raised[0]!r}"
    )
    assert dialled == [hop_ip], (
        f"{cell_label(resolved)} resolved its hop to {dialled} rather than {hop_ip}"
    )


@pytest.mark.asyncio
async def test_a_hopped_cell_with_no_context_cannot_resolve_its_hop_at_all(monkeypatch):
    """The failure Task 4b removes, INJECTED so the test above can be seen to fail.

    Without this, every assertion up there could be satisfied by a hop that
    never needed resolving. It builds the same cell's host by the same
    factory call and drives the same transport with the context taken away --
    which is precisely the state ``create_host_from_dict`` leaves a host in,
    and precisely what 17 of the 49 cells hit on the first day anyone opened
    one.

    The absence of a context is INJECTED rather than inherited: whether one
    happens to be installed when this test starts depends on what else the
    session ran (``make coverage`` puts the whole repo in one process, and
    the root conftest's ``_reset_otto_context`` restores rather than clears).
    A test that inherited ``None`` would assert nothing on the run where a
    context WAS installed, and would not say so.
    """
    dialled = _dial_recorder(monkeypatch)
    with _no_otto_context():
        host = build_bed_host(Cell(*HOPPED_BUSYBOX_CELL))
        with pytest.raises(RuntimeError, match="cannot resolve hop 'test1'") as excinfo:
            await host._build_hop_transport().get_tunnel()

    assert "no lab back-reference and there is no active OttoContext" in str(excinfo.value)
    assert dialled == [], "a host that cannot resolve its hop still reached the wire"


@pytest.mark.asyncio
async def test_the_venues_context_does_not_outlive_the_open(monkeypatch):
    """The install is scoped to the open; nothing after it inherits the bed's lab.

    A process-global ``ContextVar`` is what makes the hop resolvable, so what
    keeps that acceptable is that it is handed back. Asserted against the
    value held BEFORE the open rather than against ``None``: the block
    restores the previous context, and a caller already running under one of
    its own must be left where it started.
    """
    monkeypatch.setattr(RemoteHost, "verify_connection", _always_answers)

    before = try_get_context()
    async with _resolved(HOPPED_BUSYBOX_CELL).open_host():
        inside = try_get_context()
    assert inside is not None, "no context was installed while the host was open"
    assert inside.lab.name == BED_LAB_NAME
    assert try_get_context() is before, "the bed venue's context outlived the open"


def test_the_venues_lab_resolves_the_hop_of_every_hopped_cell_in_the_space():
    """Over the WHOLE space, not the two cells the tests above name.

    The opener tests prove the mechanism on one BusyBox guest and one Zephyr
    guest. This proves the DATA: every cell whose host declares a hop names a
    hop the venue's lab can answer to. A guest repointed at a jump host the
    lab does not carry would leave the mechanism working and that cell
    unopenable, which is the exact shape of the gap Task 4b exists to close.

    The count is recorded so the guard cannot go quiet. If a lab-data edit
    left zero hopped cells this would pass over an empty loop, having
    asserted nothing -- the vacuum item 3's conftest refuses at the space
    level and nothing else here refuses at this one.
    """
    lab = bed_lab(BED_TECH)
    hopped = []
    unresolvable = []
    for resolved in bed_space():
        host = build_bed_host(resolved.cell)
        if host.hop is None:
            continue
        hopped.append(cell_label(resolved))
        if host.hop not in lab.hosts:
            unresolvable.append(f"{cell_label(resolved)} hops {host.hop!r}")
    assert not unresolvable, (
        f"the venue's lab {sorted(lab.hosts)} cannot resolve these cells' hops: {unresolvable}"
    )
    assert len(hopped) == 17, (
        f"expected the 17 hopped cells this task was written for (the 5 BusyBox guests "
        f"crossed by 2 transfers, plus the 7 Zephyr guests), got {len(hopped)}: {hopped}"
    )


def test_the_venues_lab_holds_the_hop_targets_and_nothing_else():
    """Hop targets only -- the cell's own host is never a member.

    A lab carrying all sixteen bed hosts would resolve the same hops and
    would quietly change what the contracts run against: ``Lab.add_host``
    sets ``_lab`` and stamps ``source_lab`` on what it is given, so the
    cell's host would stop being the object ``tests/integration/host/`` and
    ``tests/integration/busybox_bed/`` build. Pinned here because the
    difference is invisible from a passing contract.
    """
    assert sorted(bed_lab(BED_TECH).hosts) == ["test1", "test4"]
    # `test4` first: the derivation keeps first appearance among the HOPPERS,
    # and the seven Zephyr guests are declared ahead of the five BusyBox ones
    # in `lab.json`. Recorded rather than sorted, so a derivation that started
    # ordering by something process-local would be visible here.
    assert hop_targets(BED_TECH) == ["test4", "test1"], (
        "the hop targets moved; the recorded expectation above is what makes a "
        "silently narrowed derivation visible"
    )


def test_the_hop_targets_are_read_off_the_lab_data_rather_than_listed(monkeypatch, tmp_path):
    """INJECTED: lab data whose hop target is a host today's data never names.

    Every real hop in this bed points at ``test1`` or ``test4``, so a
    hard-coded pair would satisfy every assertion above. Only lab data that
    names something else can tell a derivation from a list -- the same
    inherit-versus-inject shape ``bed_kind`` hit when every Zephyr element
    happened to be called ``zephyr*``.
    """
    _fabricate_lab_data(monkeypatch, tmp_path, [_entry("relay"), _entry("guest", hop="relay")])
    assert hop_targets("tech1") == ["relay"]
    assert sorted(bed_lab("tech1").hosts) == ["relay"]


def test_every_link_of_a_hop_chain_reaches_the_lab(monkeypatch, tmp_path):
    """INJECTED: today's bed data is flat, so real data cannot exercise this.

    ``_create_tunnel`` builds its hop's own hop transport when the hop host
    declares a ``hop`` of its own, resolving it against the SAME lab, so a
    lab holding only the first link hands otto one that fails a hop further
    in. Measured on the real data: neither ``test1`` nor ``test4`` declares a
    hop, so a derivation that dropped the inner link passes every other test
    in this file.

    Asserted as an OUTCOME -- both links in the lab -- rather than as "the
    derivation recursed". It does not recurse, and the mutation that proved
    it does not is recorded in :func:`~tests.conformance._lab_context.hop_targets`:
    every link of a chain is itself a host that declares a hop, so one pass
    over the entries already collects it.
    """
    _fabricate_lab_data(
        monkeypatch,
        tmp_path,
        [_entry("guest", hop="inner"), _entry("inner", hop="outer"), _entry("outer")],
    )
    assert hop_targets("tech1") == ["inner", "outer"]
    assert sorted(bed_lab("tech1").hosts) == ["inner", "outer"]


def test_a_hop_that_names_an_undeclared_host_fails_where_the_name_is_still_in_hand(
    monkeypatch, tmp_path
):
    """A hop pointing at nothing raises here, not as an otto ``KeyError`` at open time.

    INJECTED, because the committed data declares every host it hops
    through: the guard cannot fire on real data and would otherwise be a
    claim nobody had tested.
    """
    _fabricate_lab_data(monkeypatch, tmp_path, [_entry("guest", hop="ghost")])
    with pytest.raises(KeyError, match="hops through 'ghost'"):
        hop_targets("tech1")


def test_a_hop_target_whose_host_id_is_not_its_element_is_refused(monkeypatch, tmp_path):
    """``hop`` names an ELEMENT; a ``Lab`` is keyed by host ID, and they can differ.

    INJECTED with a jump host that declares a board, which is what makes
    ``make_host_id`` produce ``relay_qemu`` for element ``relay`` -- the same
    derivation that turns ``bb1161`` into ``bb1161_qemu``. Every hop target
    in today's data is boardless, so real data cannot tell a lab that answers
    to the hop from one that does not.
    """
    _fabricate_lab_data(
        monkeypatch, tmp_path, [_entry("relay", board="qemu"), _entry("guest", hop="relay")]
    )
    with pytest.raises(RuntimeError, match=r"cannot answer to \['relay'\]"):
        bed_lab("tech1")


# --- Single-client console safety (Task 3) ----------------------------------
#
# The bed's Zephyr guests serve exactly ONE telnet client, and a second one
# arriving at the wrong moment re-initialises the guest's telnet backend,
# after which it refuses every connection until `make qemu-restart` (issue
# #260). `tests/conformance/_console_safety.py` is this tree's protection and
# its docstring is where the mechanism, the rejected options and the gaps are
# argued. What is asserted here is everything about it that a bed cannot be
# needed for -- which is all of it: the classification is a function of the
# space, and the lock is a function of the kernel.

# PRE-EXISTING G9 VIOLATION, FIXED IN PASSING: this was
# `Path(__file__).resolve().parents[2]`, which `make lint-arch`'s
# `no-parents-arithmetic-in-tests` rule rejects -- a parents[N] count encodes
# the file's own depth, so moving this file re-anchors every subprocess cwd
# below it in silence. `tests/_fixtures/paths.py` is the one sanctioned place
# that derives the anchor.
_PROJECT_ROOT = PROJECT_ROOT

# The hermetic venue's `local` cell, by the substring that selects exactly its
# items and nothing else. `busybox-artifact[busybox-1.16.1:local:local]`
# contains `local:local` but not `local:local:local`, which is why the triple
# is spelled out.
_HERMETIC_LOCAL_CELL = "local:local:local"

# Every contract in `tests/conformance/` is parametrized over every drawn
# cell, and since the support matrix landed so is every POSITIVE CONTROL, so
# one cell is this many items. Asserted rather than assumed by the subprocess
# runs below, which would otherwise pass on zero selected items.
#
# DERIVED, and it used to be the literal `6`. That literal was correct when
# there were six contracts and nothing else took the cell, and it went stale
# the moment their controls joined them -- four guards below turned red on a
# COUNT rather than on the protection they are about, which is the least
# useful place for a suite to fail. The two discovery functions are the same
# ones `tests/_fixtures/support_matrix.py` builds the matrix's rows from, so
# this cannot disagree with the tree about what a cell's items are. (14 today:
# 7 contracts + 7 controls, and `make conformance` collects 112 cell items over
# the hermetic venue's 8 cells.)
_ITEMS_PER_CELL = len(discover_contracts()) + len(discover_controls())

# The plugin the subprocess runs load with `-p`. It INJECTS the hostile
# condition rather than waiting to inherit it: the hermetic venue has no
# console cell at all (that is the point of it), and the bed venue -- which
# COLLECTS since item 4 removed `resolve_space()`'s raise, collecting being a
# very different thing from opening -- could not be RUN here in any case,
# since running it opens the very consoles this protects. Re-labelling the
# `local` cell as a single-client console exercises the real conftest wiring
# against a `LocalHost`, with no lab and no console.
#
# Written to a tmp dir instead of committed under `tests/`: a permanent
# override of the protected-kind set is a switch that could be left on.
_INJECT_CONSOLE_KIND = '''\
"""INJECTED BY tests/unit/test_conformance_bed.py -- not part of the suite."""

from tests.conformance import _console_safety

_console_safety.SINGLE_CLIENT_CONSOLE_KINDS = frozenset({"local"})
'''

# Two ways to have the protection "in place" and not in effect. Both leave
# every local signal looking correct, which is the whole reason the guard
# reads the kernel rather than the fixture's own bookkeeping.
_BREAKAGE = {
    # The fixture requests the lock and gets a context manager that locks
    # nothing and records nothing -- the "the fixture was narrowed away" shape.
    "fixture": """
import contextlib

import tests.conformance.conftest as _conftest


@contextlib.contextmanager
def _no_lock(lock_dir):
    yield


_console_safety.serialized_console = _no_lock
_conftest.serialized_console = _no_lock
""",
    # The fixture runs, `_HELD` is populated, everything this tree can see
    # says "protected" -- and no flock was ever taken.
    "flock": """
import contextlib


@contextlib.contextmanager
def _fake_console_access(lock_dir, *, exclusive):
    yield


_console_safety.console_access = _fake_console_access
""",
    "none": "",
}


def _run_conformance(tmp_path, *, shape: str, breakage: str):
    """Run the conformance tree in a subprocess with the console kind injected.

    A subprocess and not an in-process ``pytest.main``: what is under test is
    plugin/conftest registration and an ``flock`` held across xdist workers,
    and both are properties of a real session's process tree. Two workers, not
    ``-n auto``: enough to make the items land on different workers (measured
    -- one cell's items split gw0/gw1) without putting a full fan-out on a dev
    VM.
    """
    name = f"otto_injected_console_{breakage}"
    (tmp_path / f"{name}.py").write_text(_INJECT_CONSOLE_KIND + _BREAKAGE[breakage])
    args = [
        sys.executable,
        "-m",
        "pytest",
        "-p",
        name,
        "-p",
        "no:cacheprovider",
        "--no-cov",
        "-q",
        "-n2",
        "--dist",
        "loadgroup",
        "-k",
        _HERMETIC_LOCAL_CELL,
    ]
    # `not conformance_bed` rides BOTH arms, and it is load-bearing rather than
    # copied off the Makefile: `tests/conformance/test_bed_opener_witness.py`
    # opens a REAL lab VM over ssh on every run, in either shape. The `-k`
    # above happens to deselect it too, but a hostless test must not be
    # hostless by coincidence of a name filter.
    args += (
        ["-m", "not conformance_bed", "tests/conformance"]
        if shape == "path-named"
        else ["-m", "conformance and not conformance_bed"]
    )
    return subprocess.run(
        args,
        cwd=str(_PROJECT_ROOT),
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join([str(tmp_path), os.environ.get("PYTHONPATH", "")]),
            # The ini addopts still apply; this only drops whatever the OUTER
            # run was given, so the inner run's -n/-k are not fought over.
            "PYTEST_ADDOPTS": "",
            # Draw the whole hermetic space, so the `local` cell is present
            # whatever seed the inner run picks.
            "OTTO_CONFORMANCE_CELLS": "all",
        },
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )


@pytest.mark.parametrize("shape", ["path-named", "path-less"])
def test_the_console_protection_is_in_effect_under_both_invocation_shapes(tmp_path, shape):
    """F3's check: the protection cannot depend on how the run was invoked.

    ``tests/integration/host/conftest.py`` learned this the expensive way --
    its collection hook wins when its directory is reached through a full-tree
    run and loses when the directory is NAMED, measured there as 4 failed, 3
    errors and a guest taken down by a CPU exception. This tree's protection is
    a lock taken at fixture setup, which no registration order can reach, and
    this is what says so out loud rather than reasoning about it.

    The `-m conformance` arm is the shape ``make conformance`` really uses.
    """
    result = _run_conformance(tmp_path, shape=shape, breakage="none")
    assert f"{_ITEMS_PER_CELL} passed" in result.stdout, (
        f"the injected console cell did not run {_ITEMS_PER_CELL} items ({shape}) -- a "
        f"pass on zero selected items would prove nothing:\n{result.stdout}\n{result.stderr}"
    )
    assert result.returncode == 0, f"({shape})\n{result.stdout}\n{result.stderr}"


@pytest.mark.parametrize(
    ("breakage", "expected"),
    [
        ("fixture", "holds NO exclusive console lock"),
        ("flock", "is NOT actually locked"),
    ],
)
def test_a_console_cell_running_unprotected_fails_the_run(tmp_path, breakage, expected):
    """The guard's positive control, in the two shapes the failure really takes.

    ``fixture`` is the protection removed; ``flock`` is the protection PRESENT
    and inert -- the fixture runs, ``_HELD`` is populated, and no lock exists.
    The second is the one that matters: a guard that trusted the suite's own
    bookkeeping would pass it, and the guest would be just as exposed.
    """
    result = _run_conformance(tmp_path, shape="path-named", breakage=breakage)
    assert f"{_ITEMS_PER_CELL} failed" in result.stdout, (
        f"expected every item of the injected console cell to fail:\n"
        f"{result.stdout}\n{result.stderr}"
    )
    assert expected in result.stdout, f"{result.stdout}\n{result.stderr}"
    assert "issue #260" in result.stdout, (
        f"the failure did not name what it costs:\n{result.stdout}"
    )


def _unprotected_console_cells():
    """Cells otto builds as ``EmbeddedHost`` that the venue would NOT serialize.

    otto's OWN answer to "does this host's console serve one client", not this
    suite's: ``EmbeddedHost.__post_init__`` is the only place in ``src/`` that
    sets ``TelnetOptions.single_client_console=True``, so the class the factory
    picks IS the property. Returned as a list rather than asserted here so the
    injected negatives below can call it and expect a non-empty answer.
    """
    return [
        cell_label(resolved)
        for resolved in bed_space()
        if isinstance(build_bed_host(resolved.cell), EmbeddedHost)
        and not opens_a_single_client_console(resolved)
    ]


def test_every_host_otto_builds_as_embedded_is_serialized():
    """The whole space, cross-checked against otto rather than against itself.

    ``SINGLE_CLIENT_CONSOLE_KINDS`` is spelled in this suite's own kind
    vocabulary, so on its own it can only ever agree with itself: rename the
    kind and the protection would switch off with every assertion about it
    still green. This asks otto instead, over all 49 cells, and it is the
    reason the predicate is allowed to stay a cheap string membership.
    """
    assert not _unprotected_console_cells()


def test_the_cross_check_notices_a_protected_kind_that_was_dropped(monkeypatch):
    """INJECTED: empty the protected set and the cross-check must see all seven."""
    monkeypatch.setattr(_console_safety, "SINGLE_CLIENT_CONSOLE_KINDS", frozenset())
    assert len(_unprotected_console_cells()) == 7


def test_the_cross_check_notices_a_renamed_kind_vocabulary(monkeypatch):
    """INJECTED: the drift that actually happens -- the kind is renamed.

    Not a hypothetical: item 1's kind names (``bed-unix`` / ``bed-busybox`` /
    ``bed-zephyr``) are this suite's invention and appear in every
    parametrization id, so they are exactly the sort of string a later change
    tidies. Renaming ``bed-zephyr`` here leaves every kind assertion in this
    file green -- they are written against ``BED_ZEPHYR`` -- while
    ``SINGLE_CLIENT_CONSOLE_KINDS``, frozen at import from the OLD name,
    matches nothing.
    """
    monkeypatch.setattr(
        _bed,
        "_kind_for_userland",
        lambda userland: "bed-rtos" if userland.startswith("zephyr-") else BED_UNIX,
    )
    assert len(_unprotected_console_cells()) == 7


def test_a_cell_that_opens_no_console_is_not_serialized():
    """The predicate has to be able to say no, or the space would serialize whole."""
    assert not opens_a_single_client_console(_resolved(DEFAULT_CELL))
    assert opens_a_single_client_console(_resolved(("zephyr37_fat", "telnet", "console")))


def test_the_serialized_console_holds_a_lock_the_kernel_agrees_with(tmp_path):
    """Inside the block the guard is silent, and an independent probe is refused.

    The probe is the point. ``flock`` locks belong to the open file
    DESCRIPTION rather than to the process, so a second descriptor on the same
    file conflicts with this process's own hold -- which is what lets the
    holder verify itself without a second process (measured on this VM:
    ``BlockingIOError: [Errno 11]``).
    """
    with serialized_console(tmp_path):
        assert unhonored_console_lock() is None
        probe = os.open(str(tmp_path / RESOURCE_NAME), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(probe, fcntl.LOCK_SH | fcntl.LOCK_NB)
        finally:
            os.close(probe)


def test_outside_the_block_the_guard_says_nothing_is_held():
    reason = unhonored_console_lock()
    assert reason is not None
    assert "holds NO exclusive console lock" in reason


def test_the_block_releases_the_lock_on_the_way_out(tmp_path):
    """Otherwise the first console cell of a run would hold the bed to the end."""
    with serialized_console(tmp_path):
        pass
    assert unhonored_console_lock() is not None
    probe = os.open(str(tmp_path / RESOURCE_NAME), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(probe, fcntl.LOCK_SH | fcntl.LOCK_NB)  # would raise if still held
        fcntl.flock(probe, fcntl.LOCK_UN)
    finally:
        os.close(probe)


def test_the_guard_reads_the_kernel_and_not_its_own_bookkeeping(monkeypatch, tmp_path):
    """INJECTED: ``console_access`` replaced by a context manager that locks nothing.

    Every signal this suite owns still says protected -- the block was
    entered, ``_HELD`` names a file. The guard must still refuse, because the
    only thing that keeps a second worker off the console is the flock.
    """

    @contextlib.contextmanager
    def _locks_nothing(lock_dir, *, exclusive):
        yield

    monkeypatch.setattr(_console_safety, "console_access", _locks_nothing)
    with serialized_console(tmp_path):
        reason = unhonored_console_lock()
    assert reason is not None
    assert "is NOT actually locked" in reason


def test_no_other_process_can_take_the_console_while_the_block_holds_it(tmp_path):
    """Cross-PROCESS, because cross-worker is what the suite actually does."""
    probe = (
        "import fcntl, sys, os\n"
        f"fd = os.open({str(tmp_path / RESOURCE_NAME)!r}, os.O_RDWR | os.O_CREAT, 0o644)\n"
        "try:\n"
        "    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "except BlockingIOError:\n"
        "    sys.exit(9)\n"
        "sys.exit(0)\n"
    )
    with serialized_console(tmp_path):
        blocked = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True, timeout=60, check=False
        )
    free = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=60, check=False
    )
    assert blocked.returncode == 9, f"a second process took the console: {blocked.stderr}"
    assert free.returncode == 0, f"the lock outlived its block: {free.stderr}"


def test_console_transports_are_swept_before_the_lock_is_released(monkeypatch, tmp_path):
    """The sweep must run while the lock is still HELD, not after.

    ``pytest-timeout``'s signal can abort a test before its ``close()`` runs,
    leaving the guest's one client slot occupied by a transport nobody owns.
    Releasing first would hand the next holder a console that is still taken --
    and the ordering is invisible from a passing run, because both orders
    sweep.
    """
    seen: "list[str | None]" = []
    monkeypatch.setattr(
        _console_safety, "abort_console_transports", lambda: seen.append(unhonored_console_lock())
    )
    with serialized_console(tmp_path):
        pass
    assert seen == [None], f"the sweep did not run inside the lock: {seen}"


def test_both_trees_take_the_console_lock_in_the_same_directory():
    """One spelling of the lock directory, because two would be two locks.

    ``tests/integration/host/conftest.py`` is the only other holder of this
    lock, and it derives the directory inline. If that derivation moves and
    this one does not, both trees keep passing and neither excludes the other
    -- the failure has no symptom until a guest is wedged. Checked as source
    text for want of anything to import; the constant it pins is one line.
    """
    stub = SimpleNamespace(getbasetemp=lambda: Path("/tmp/pytest-of-x/pytest-3/popen-gw0"))
    assert console_lock_dir(stub) == Path("/tmp/pytest-of-x/pytest-3")
    integration_conftest = (
        _PROJECT_ROOT / "tests" / "integration" / "host" / "conftest.py"
    ).read_text()
    assert "lock_dir = tmp_path_factory.getbasetemp().parent" in integration_conftest, (
        "tests/integration/host/conftest.py no longer derives its console lock directory "
        "as getbasetemp().parent -- tests/conformance/_console_safety.console_lock_dir "
        "must move with it or the two trees will lock different files"
    )


# --- What resolving the bed space drags in (Task 4) --------------------------
#
# `ResolvedCell` used to live in `tests/conformance/_cells.py`, the HERMETIC
# venue, which imports the tier-2 chaos lane's loopback `sshd` fixtures and the
# BusyBox artifact matrix at module level. Taking the shape from there meant
# the BED resolver imported all of it: measured before the split, `import
# tests.conformance._bed` pulled in `tests.integration.chaos._sshd` and
# `tests.integration.chaos._target`. `tests/conformance/_resolved.py` is the
# shape's own module now, and this is what keeps it that way.


def _tests_modules_imported_by(module: str) -> "list[str]":
    """Which ``tests.*`` modules a FRESH interpreter pulls in to import *module*.

    A subprocess, because THIS process's ``sys.modules`` answers a different
    question depending on how the run was invoked -- and a guard that changes
    its verdict with the command line is the trap
    ``tests/integration/host/conftest.py`` documents at length. Measured in
    this worktree, at the moment a test in this file runs:

    - path-LESS (``pytest -k ...``, the shape ``make coverage``'s hostless
      legs use): ``tests.integration.chaos._sshd`` is ALREADY imported, along
      with every other test module in the repo, because a path-less run
      collects them all;
    - path-NAMED at this file alone: nothing under ``tests.integration`` is
      imported at all.

    So an in-process probe would report the outer session's imports in the
    first shape -- passing or failing whatever ``_bed`` does -- and only
    happen to be right in the second.
    """
    probe = (
        "import importlib, sys; importlib.import_module(sys.argv[1]); "
        "print('\\n'.join(sorted(m for m in sys.modules if m.startswith('tests.'))))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe, module],
        cwd=str(_PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, f"the import probe could not import {module}:\n{result.stderr}"
    return result.stdout.split()


def test_resolving_the_bed_space_does_not_import_the_hermetic_venues_fixtures():
    """The bed venue reaches real hardware; it has no use for a loopback ``sshd``.

    The DIRECT regression -- ``_bed`` importing ``_cells`` again -- can no
    longer happen silently, because ``_cells`` now imports ``_bed`` for the
    dispatch and the pair would be a cycle (measured: ``ImportError: cannot
    import name 'ResolvedCell' from partially initialized module
    'tests.conformance._cells'``). What this catches is the INDIRECT one: a
    convenience import of any module that itself reaches the chaos lane, which
    nothing about a cycle prevents and nothing else in this file would notice.
    """
    dragged = _tests_modules_imported_by("tests.conformance._bed")
    assert dragged, "the import probe reported no tests.* modules at all"
    strays = [m for m in dragged if m.startswith("tests.integration")]
    assert not strays, (
        f"importing tests.conformance._bed dragged in {strays} -- the bed resolver is a "
        f"pure function of committed lab data and must not import the hermetic venue's "
        f"loopback sshd machinery to get there"
    )


def test_the_import_probe_can_see_a_module_that_does_import_them():
    """The guard above must be able to FAIL, and this is what says it still can.

    The hermetic venue genuinely imports the chaos lane's fixtures -- that is
    the machinery it stands its cells up with -- so the same probe pointed at
    ``_cells`` must report them. If this goes quiet (a lazy import there, a
    ``sys.path`` change that makes the subprocess import nothing at all), the
    probe has stopped discriminating and the guard above is green for a reason
    that has nothing to do with ``_bed``.
    """
    strays = [
        m
        for m in _tests_modules_imported_by("tests.conformance._cells")
        if m.startswith("tests.integration")
    ]
    assert strays, (
        "tests.conformance._cells no longer imports anything under tests.integration, so "
        "the probe above can no longer tell a module that drags the chaos fixtures in "
        "from one that does not"
    )


# --- The remote scratch directory, and the transfer contract's domain -------
#
# All hostless, for the reason the module header gives: where a file lands on
# a device is a function of committed lab data plus otto's own filesystem
# model, and neither needs a bed to ask. Reaching the device is the transfer
# contract's job, and it has its own bed run.

# The three guests that declare `filesystem: "none"`, recorded rather than
# derived. Deriving this list from the same call `bed_scratch_dir` makes would
# be the implementation agreeing with itself; the point of writing it down is
# that a fourth no-FS guest, or a variant renamed under one of these three,
# has to come through here and be seen.
NO_FILESYSTEM_GUESTS = ["zephyr37_nofs", "zephyr37_llext", "zephyr44_llext"]

# What the other thirteen elements answer. `/RAM:` and `/lfs` are otto's, via
# `build_filesystem`; `/tmp` is the value `tests/conftest.py`'s `_UNIX_KIT`
# and `_BUSYBOX_KIT` already declare for these same hosts.
EXPECTED_SCRATCH = {
    "test1": "/tmp",
    "test2": "/tmp",
    "test3": "/tmp",
    "test4": "/tmp",
    "bb1161": "/tmp",
    "bb1211": "/tmp",
    "bb1281": "/tmp",
    "bb1310": "/tmp",
    "bb1350": "/tmp",
    "zephyr37_fat": "/RAM:",
    "zephyr27_fat": "/RAM:",
    "zephyr37_lfs": "/lfs",
    "zephyr44_lfs": "/lfs",
}


def _outside_the_domain(space):
    return [rc for rc in space if not applicable_cell(rc)]


def _inside_the_domain(space):
    return [rc for rc in space if applicable_cell(rc)]


def test_the_transfer_domain_excludes_exactly_the_filesystem_less_guests():
    """WHICH cells the narrowing drops, pinned by name and by count.

    The guard the controller asked for, and it fails in both directions on
    purpose: a domain that quietly WIDENS (a nofs guest becoming applicable,
    which is what reversing the two branches of `bed_scratch_dir` would do --
    a filesystem-less host reports the same empty `default_dest_dir` a POSIX
    one does) fails the element comparison, and a domain that quietly NARROWS
    (a `fat-ram` guest losing its mount) fails it the other way.

    46 of 49, not "most of them": the count is written down because the set
    comparison alone would still pass if a fourth guest were added with no
    filesystem and this list were updated to match without anyone asking why.
    """
    space = bed_space()
    assert len(space) == 49

    outside = _outside_the_domain(space)
    assert sorted({rc.cell.element for rc in outside}) == sorted(NO_FILESYSTEM_GUESTS)
    assert len(outside) == 3, (
        f"a Zephyr host reports a single (telnet, console) pair, so each excluded guest "
        f"contributes exactly one cell -- got {[cell_label(rc) for rc in outside]}"
    )
    assert len(_inside_the_domain(space)) == 46


def test_the_excluded_guests_are_the_ones_otto_says_have_nowhere_to_put_a_file():
    """The REASON, asked of otto directly rather than of the resolver.

    `bed_scratch_dir` reaches otto's answer through the built host's
    `default_dest_dir`; this reaches it through `build_filesystem`, which is
    the single source of truth `tests/conftest.py`'s `_zephyr_kit` names. Two
    different routes to the same answer, so this is a cross-check rather than
    a restatement of the implementation.
    """
    entries = {
        host["element"]: host for host in json.loads(lab_data_path(BED_TECH).read_text())["hosts"]
    }
    for element in NO_FILESYSTEM_GUESTS:
        assert entries[element]["filesystem"] == "none"
        filesystem = build_filesystem("none")
        assert filesystem.mount is None
        assert filesystem.supports_transfer is False, (
            "`supports_transfer` is the flag ConsoleFileTransfer short-circuits _run_put "
            "and _run_get on; if it stopped being False for `none`, otto would no longer "
            "be treating a no-filesystem target as having nowhere to put a file"
        )


def test_every_bed_cells_scratch_dir_is_ottos_own_answer():
    """Each element's landing directory, against the recorded expectation.

    The four guests with a filesystem are cross-checked against
    `build_filesystem` a second time, so this cannot pass by agreeing with
    `bed_scratch_dir`'s route through the host.
    """
    entries = {
        host["element"]: host for host in json.loads(lab_data_path(BED_TECH).read_text())["hosts"]
    }
    seen: dict[str, str | None] = {}
    for rc in bed_space():
        element = rc.cell.element
        if element in seen:
            continue
        if rc.remote_scratch is None:
            seen[element] = None
            continue
        # The argument is the hermetic venue's local `tmp_path`, and a bed
        # cell must IGNORE it. Passing a path that could not be a legitimate
        # answer means a resolver that started echoing its argument back
        # fails here rather than looking plausible.
        seen[element] = str(rc.remote_scratch(Path("/a-runner-path-that-must-not-be-used")))

    for element, expected in EXPECTED_SCRATCH.items():
        assert seen[element] == expected, f"{element} lands transfers at {seen[element]}"
        declared = entries[element].get("filesystem")
        if declared is not None:
            assert build_filesystem(declared).mount == expected, (
                f"{element}'s {declared!r} filesystem no longer mounts at {expected}"
            )
    assert sorted(seen) == sorted([*EXPECTED_SCRATCH, *NO_FILESYSTEM_GUESTS])


def test_a_resolved_cell_cannot_be_built_without_answering_where_a_file_lands():
    """The field is REQUIRED, which is the whole of its defence.

    A default of ``None`` would file every cell a future venue resolves as
    "has nowhere to put a file": the transfer contract would narrow to
    nothing, its `pytest_generate_tests` raise would fire, and the venue
    would have silently lost a contract. Nothing at runtime can catch that,
    because the construction site is where the answer has to be given -- so
    this pins that omitting it is an error at all.
    """
    with pytest.raises(TypeError, match="remote_scratch"):
        ResolvedCell(cell=Cell("nowhere", "ssh", "scp"), kind="fabricated", open_host=lambda: None)


def test_a_cell_with_nowhere_to_put_a_file_carries_no_scratch_callable():
    """`None`, not a path that would then be transferred into."""
    for rc in _outside_the_domain(bed_space()):
        assert rc.remote_scratch is None
        assert bed_scratch_dir(rc.cell.element) is None


def test_the_domain_is_read_off_the_filesystem_not_the_element_name(monkeypatch):
    """INJECTS the hostile condition: every real no-FS guest is *named* ``*nofs``/``*llext``.

    Two fabricated hosts, each the opposite of what its name suggests. A
    predicate that sniffed the element -- `"nofs" in element`, `"llext" in
    element` -- passes every real-data assertion in this file and fails both
    of these. So does one that read the `filesystem` STRING and listed the
    variants it knew, once a variant it had not heard of appears.
    """
    innocuous = dict(host_data("zephyr37_nofs", BED_TECH))
    innocuous["element"] = "a-name-that-says-nothing"
    telling = dict(host_data("zephyr37_fat", BED_TECH))
    telling["element"] = "zephyr99_nofs_llext"
    entries = {entry["element"]: entry for entry in (innocuous, telling)}

    # Injected at the LAB DATA, which is where a new guest actually comes
    # from -- not at the built host. A resolver that stopped reading lab data
    # and started reading a table would go green here without this.
    monkeypatch.setattr(_bed, "host_data", lambda element, tech=BED_TECH: entries[element])

    assert bed_scratch_dir("a-name-that-says-nothing") is None
    assert str(bed_scratch_dir("zephyr99_nofs_llext")) == "/RAM:"


def test_a_host_that_declares_its_own_landing_directory_is_honoured(monkeypatch):
    """Lab data wins over the ``/tmp`` fallback, with no edit to the resolver.

    The half of "a new guest gets a correct path for free" that the real lab
    data cannot show, because no entry in it declares `default_dest_dir`
    today. Injected rather than inherited for exactly that reason.
    """
    entry = dict(host_data("test1", BED_TECH))
    entry["element"] = "declares-its-own"
    entry["default_dest_dir"] = "/var/tmp/otto-scratch"
    monkeypatch.setattr(_bed, "host_data", lambda element, tech=BED_TECH: entry)

    answer = bed_scratch_dir("declares-its-own")
    assert str(answer) == "/var/tmp/otto-scratch"
    assert answer != _POSIX_SCRATCH_DIR


def test_the_contracts_remote_basename_fits_every_domain_cells_declared_limit():
    """32 characters on every Zephyr guest, and the name has to fit ALL of them.

    Asked of the host's DECLARED `max_filename_len` rather than of the 32 in
    the lab file, because that field is what
    `BaseFileTransfer.put_files`/`get_files` validate the basename against --
    a refusal before anything transfers, which is how an over-long name would
    present.

    `master` is the longest worker id pytest-xdist can hand out here: a real
    worker is `gwN`, and `worker_id` is `master` under `-n0`.
    """
    longest = remote_name("master", _PAYLOAD_NAME)

    limits = {}
    for rc in _inside_the_domain(bed_space()):
        limits.setdefault(rc.cell.element, build_bed_host(rc.cell).max_filename_len)
    assert min(limits.values()) == 32, f"the tightest declared limit moved: {limits}"

    # The budget check FIRST, so a lengthened name reddens on the thing that
    # actually breaks rather than on the recorded length below it.
    for element, limit in limits.items():
        assert len(longest) <= limit, (
            f"{element} declares max_filename_len={limit}, which refuses {longest!r} "
            f"({len(longest)} chars) before any transfer starts"
        )
    assert len(longest) == 18, f"the recorded worst-case length moved: {longest!r}"


def test_a_nodeid_derived_remote_name_would_not_fit_and_this_check_can_see_it():
    """The check above must be able to FAIL, and this is the name that fails it.

    The obvious way to make a remote filename unique is to derive it from the
    pytest nodeid, and the brief for this work flagged it. Measured here
    rather than asserted: a bed cell's parametrization id alone is over the
    32-character limit every Zephyr guest declares, so that scheme would turn
    every Zephyr transfer into a filename refusal.
    """
    nodeid_derived = (
        "test_put_get_roundtrip_preserves_content[bed-zephyr[zephyr37_fat:telnet:console]].bin"
    )
    limits = {build_bed_host(rc.cell).max_filename_len for rc in _inside_the_domain(bed_space())}
    assert len(nodeid_derived) > min(limits)
    assert len(nodeid_derived) > 32


# --- The narrowing itself, exercised through the conftest hook --------------


class _RecordingMetafunc:
    """Just enough ``Metafunc`` for ``pytest_generate_tests`` to act on.

    The hook parametrizes over ``pytest.param(cell, marks=...)`` rather than
    over the cells themselves, because a cell may carry an expected-failure
    declaration. So this records the two halves separately: ``parametrized``
    is the cells, which is what every domain assertion here is about, and
    ``marks`` is the per-cell mark list, which is what the expected-failure
    assertions are about. Recording only the ``ParameterSet``s would make
    every domain assertion read through ``.values[0]``; recording only the
    cells would make the marks unobservable.

    ``config.stash`` and ``definition`` are REAL pytest objects and not
    stand-ins. The hook now also hands the COMPLEMENT of the domain to
    ``tests/conformance/_observation.py``'s ``note_domain_exclusions``, which
    stashes it against the config under the contract's own nodeid; a double
    that answered a bare ``SimpleNamespace`` there would be a double that
    disagrees with pytest about the shape of the seam.
    """

    def __init__(self, module, nodeid="tests/conformance/test_fabricated.py::test_contract"):
        self.fixturenames = ["resolved_cell"]
        self.config = SimpleNamespace(stash=pytest.Stash())
        self.definition = SimpleNamespace(nodeid=nodeid)
        self.module = module
        self.parametrized = None
        self.marks = None

    def parametrize(self, argname, argvalues, ids=None, indirect=False):
        params = list(argvalues)
        self.parametrized = [param.values[0] for param in params]
        self.marks = [list(param.marks) for param in params]


def _module_declaring(applies, expected=None):
    module = SimpleNamespace(__name__="fabricated_contract", applicable_cell=applies)
    if expected is not None:
        module.expected_failure = expected
    return module


def test_a_module_with_no_declared_domain_gets_every_drawn_cell(monkeypatch):
    """The positive control: three of the seven contracts declare no domain.

    Three MODULES declare one, covering the other four contracts --
    `test_transfer_contract` (cells with a remote directory, for both of its),
    `test_progress_contract` (the same predicate, for its one) and
    `test_timeout_contract` (cells whose vocabulary has a long-running
    command, for its one). The three that declare nothing are the exec file's,
    so a hook that narrowed unconditionally would still satisfy the declaring
    modules and only this test would notice.
    """
    drawn = bed_space()[:4]
    monkeypatch.setattr(_conformance_conftest, "_drawn_cells", lambda config: drawn)
    metafunc = _RecordingMetafunc(SimpleNamespace(__name__="no_domain"))

    _conformance_conftest.pytest_generate_tests(metafunc)
    assert metafunc.parametrized == drawn


def test_a_declared_domain_narrows_the_parametrization_to_its_cells(monkeypatch):
    """And the cells it drops are dropped, not drawn-and-skipped."""
    space = bed_space()
    drawn = [_inside_the_domain(space)[0], _outside_the_domain(space)[0]]
    monkeypatch.setattr(_conformance_conftest, "_drawn_cells", lambda config: drawn)
    metafunc = _RecordingMetafunc(_module_declaring(applicable_cell))

    _conformance_conftest.pytest_generate_tests(metafunc)
    assert metafunc.parametrized == [drawn[0]]
    # The dropped cell is REMEMBERED, not merely dropped: it generates no
    # item, so nothing downstream could otherwise learn it was ever drawn --
    # and that is the only source the support matrix's `not_observable` list
    # has (tests/conformance/_observation.py).
    assert domain_exclusions(metafunc.config) == {metafunc.definition.nodeid: [drawn[1]]}


def test_a_domain_that_narrows_the_draw_to_nothing_raises_rather_than_skipping(monkeypatch):
    """INJECTED: pytest's answer to an empty parameter set is a SKIP.

    Unreachable from real data -- 3 of 49 bed cells and 0 of 8 hermetic ones
    are outside the only domain that exists, so no draw of 8 can miss every
    applicable cell. That is exactly why it is injected here instead of being
    left as a check nothing has ever reached: a skipped contract reports
    success for a contract nobody ran, and it would be invisible in a report
    that already carries the run's draw line.
    """
    drawn = _outside_the_domain(bed_space())
    monkeypatch.setattr(_conformance_conftest, "_drawn_cells", lambda config: drawn)
    metafunc = _RecordingMetafunc(_module_declaring(applicable_cell))

    with pytest.raises(RuntimeError, match="declares an applicable domain that none of"):
        _conformance_conftest.pytest_generate_tests(metafunc)
    assert metafunc.parametrized is None


# --- The declared-failure hook: MECHANISM ONLY, no declaration to pin ---
#
# There was one to pin until 2026-08-25: `tests/conformance/test_transfer_contract.py`
# declared an `expected_failure` hook over the five `bed-busybox[*:telnet:nc]`
# cells, and two tests here pinned WHICH cells it claimed and that the hermetic
# venue -- the one CI runs nightly with no lab -- claimed none. The universal
# `nc -l -p PORT` listener spelling closed otto's `nc-transfer` gap, the
# declaration was repaid with it, and the pin naming those cells went with its
# subject (see that module's note under `applicable_cell`, and
# `docs/superpowers/specs/2026-08-25-nc-universal-spelling-design.md`).
# RESTORE IT WITH THE NEXT DECLARATION: a hook nothing pins can widen to cells
# that pass, and a strict xfail on a passing cell is a red lane for the wrong
# reason. What it asserted -- which cells are claimed, by kind and by transfer
# and by count, failing in BOTH directions -- is the shape to bring back, keyed
# on the resolver's answer rather than on a guest's name.
# The nightly's protection did NOT go with it: see
# `test_no_hermetic_cell_is_declared_a_known_failure` below, which sweeps every
# contract module and so covers the next declaration without being edited.
#
# WHAT STAYS is everything about the CONFTEST MECHANISM, and it stays because
# the next declaration will be read by exactly this code: that a declared cell
# gets a STRICT xfail and its neighbour gets nothing, that a module declaring
# no hook is marked nowhere, and that an empty reason is not a reason. Each
# INJECTS its own fabricated module rather than reading the tree's, so none of
# them went vacuous when the last real declaration was deleted -- which is the
# property that let the two pins be deleted honestly rather than left to pass
# over an empty set.
#
# THE SWEEP BELOW IS NOT THAT KIND OF EMPTY SET, and the distinction is the
# whole reason it survived while the pins did not. `_declared_failures(space)
# == []` written as a bare assertion would be a claim about the tree that no
# longer has a subject: nothing declares, so nothing is checked, and a broken
# enumerator reads exactly like a clean tree. What
# `test_no_hermetic_cell_is_declared_a_known_failure` asserts is the same
# emptiness with the ENUMERATOR GUARDED -- it fails if it found no contract
# modules at all -- so its silence is the tree's answer and not its own, and
# it RE-ARMS on the next declaration with no edit here. A guard that cannot
# fail is the defect; a guard whose subject is temporarily absent, that says
# so and proves it looked, is not.


def _contract_modules():
    """Every conformance contract module, IMPORTED, from the tree's own enumerator.

    `discover_contracts` reads the tree by AST and answers nodeids; the conftest
    reads its hook off the imported module. This is the one place the two meet,
    and it derives the module list rather than restating it so a fifth
    contract file is swept the day it lands -- it swept the fourth,
    `test_progress_contract.py`, with no edit here. There are four files today,
    holding seven contracts between them, and it is FILES this enumerates.
    """
    stems = dict.fromkeys(Path(nodeid.split("::")[0]).stem for nodeid in discover_contracts())
    return [importlib.import_module(f"tests.conformance.{stem}") for stem in stems]


def test_no_hermetic_cell_is_declared_a_known_failure():
    """`make conformance` is the lane CI runs nightly, and it must not change.

    A declaration that reached a hermetic cell would turn a passing contract
    into a strict xfail there -- reported as XPASS, so the nightly job goes red
    for a defect that is not in that venue at all, and nobody with a lab is
    watching. The bed lane can afford that conversation; the nightly cannot.

    VACUOUS TODAY, AND IT SAYS SO. Since 2026-08-25 no contract module defines
    `expected_failure` at all, so the inner loop asserts over nothing. That is
    why the sweep is the SHAPE it is: it enumerates every contract module and
    fails if it enumerated none, so the emptiness that makes it quiet today is
    the tree's answer rather than a broken enumerator's. It RE-ARMS by itself
    the day any module declares a hook -- no edit here, which is the property
    the deleted per-cell pin did not have. Proved able to fail by injecting a
    hook onto a real module and watching it red (recorded in the item's task
    report; the injection is not left behind, because a permanent one would be
    asserting against a fabricated module the conftest never reads).
    """
    modules = _contract_modules()
    assert len(modules) >= 3, (
        f"the contract sweep found {len(modules)} modules, so its silence below "
        f"is the enumerator's rather than the tree's"
    )
    space = hermetic_space()
    assert len(space) == 8

    for module in modules:
        declare = getattr(module, _conformance_conftest._XFAIL_HOOK, None)
        if declare is None:
            continue
        declared = [rc for rc in space if declare(rc)]
        assert declared == [], (
            f"{module.__name__} declares a known failure on hermetic cells "
            f"{sorted(cell_label(rc) for rc in declared)} -- on the lane CI runs "
            f"nightly with no lab, a strict xfail there is an XPASS and a red job"
        )


def test_a_declared_failure_becomes_a_strict_xfail_on_that_cell_alone(monkeypatch):
    """INJECTED, and the injection is the point.

    A test that took the real declaration and the real space would pass
    identically whether the hook wired `expected_failure` up or ignored it,
    because it would only ever see cells the real declaration already agrees
    about. So this fabricates a module declaring the FIRST drawn cell a
    failure and the second not, and asserts that the marks land asymmetrically
    -- which no reading of the real data can produce.

    STRICT is asserted, not assumed. A non-strict xfail absorbs both outcomes:
    the lane would be green with the defect present AND green the day it is
    fixed, and nothing would ever force the declaration's removal. That is the
    difference between asserting a known failure and suppressing an unknown
    one, and it is one keyword.
    """
    drawn = _inside_the_domain(bed_space())[:2]
    monkeypatch.setattr(_conformance_conftest, "_drawn_cells", lambda config: drawn)
    metafunc = _RecordingMetafunc(
        _module_declaring(
            applicable_cell,
            expected=lambda rc: "fabricated defect" if rc == drawn[0] else None,
        )
    )

    _conformance_conftest.pytest_generate_tests(metafunc)

    assert metafunc.parametrized == drawn
    marked, unmarked = metafunc.marks
    assert unmarked == [], f"the undeclared cell picked up {unmarked}"
    assert len(marked) == 1, marked
    assert marked[0].name == "xfail"
    assert marked[0].kwargs == {"reason": "fabricated defect", "strict": True}


def test_a_module_declaring_no_expected_failure_marks_nothing(monkeypatch):
    """The positive control: since 2026-08-25 all seven contracts declare nothing.

    Without it, a hook that marked EVERY cell would satisfy the asymmetry
    assertion above only by accident of which list was checked first.
    """
    drawn = _inside_the_domain(bed_space())[:3]
    monkeypatch.setattr(_conformance_conftest, "_drawn_cells", lambda config: drawn)
    metafunc = _RecordingMetafunc(SimpleNamespace(__name__="no_declaration"))

    _conformance_conftest.pytest_generate_tests(metafunc)

    assert metafunc.parametrized == drawn
    assert metafunc.marks == [[], [], []]


def test_a_declaration_returning_an_empty_reason_marks_nothing(monkeypatch):
    """An xfail with no reason is a marker nobody can act on.

    The hook treats an empty string as "no declaration" rather than as a
    reason, so a declaration that fell through to `return ""` cannot silently
    xfail every cell it is asked about with a blank explanation.
    """
    drawn = _inside_the_domain(bed_space())[:2]
    monkeypatch.setattr(_conformance_conftest, "_drawn_cells", lambda config: drawn)
    metafunc = _RecordingMetafunc(_module_declaring(applicable_cell, expected=lambda rc: ""))

    _conformance_conftest.pytest_generate_tests(metafunc)

    assert metafunc.marks == [[], []]


def test_the_scratch_fixture_refuses_a_cell_the_domain_should_have_excluded():
    """The fixture and the domain must not be able to disagree in silence.

    Reached through ``__wrapped__`` because pytest refuses a direct call of a
    fixture function. A lenient fallback here -- ``or tmp_path`` -- would put
    back the original bug (a runner path handed to ``scp`` as a remote
    directory) for any cell whose contract forgot to declare its domain.
    """
    outside = _outside_the_domain(bed_space())[0]
    with pytest.raises(RuntimeError, match="has nowhere on its host to put a file"):
        _conformance_conftest.remote_scratch.__wrapped__(outside, Path("/unused"))


def test_the_hermetic_venue_answers_with_a_runner_path_for_every_cell():
    """The other venue's answer, so the field cannot become bed-only by accident.

    A hermetic cell whose ``remote_scratch`` went ``None`` would silently drop
    the transfer contract out of ``make conformance`` -- the lane CI runs
    nightly -- while every other contract stayed green.
    """
    space = hermetic_space()
    assert len(space) == 8
    for rc in space:
        assert rc.remote_scratch is not None, cell_label(rc)
        assert applicable_cell(rc)

    # And it must be UNDER the tmp_path it was handed, not merely non-None.
    # A hermetic cell answering a device path (`/tmp` on some guest, say)
    # satisfies "not None" while transferring outside the test's own sandbox
    # -- on the runner, into a directory pytest never cleans.
    with tempfile.TemporaryDirectory(prefix="conformance-scratch-check-") as tmp:
        sandbox = Path(tmp)
        for rc in space:
            answer = rc.remote_scratch(sandbox)
            assert answer.is_relative_to(sandbox), (
                f"{cell_label(rc)} answers {answer}, which is outside the tmp_path it "
                f"was given -- the hermetic venue's far side IS this filesystem"
            )
            assert answer.is_dir(), f"{cell_label(rc)} answered a directory that does not exist"


# ---------------------------------------------------------------------------
# The per-cell STIMULUS vocabulary: derived from lab data, and holding the
# asserted property constant while the spelling varies.
# ---------------------------------------------------------------------------

#: Every vocabulary the two venues can hand a cell today, by a short name the
#: parametrization ids read cleanly under. A RECORDED LIST rather than a walk
#: of the module: a vocabulary added and wired into a venue but left out of
#: these guards would otherwise be measured by nothing at all, and the walk
#: would make that invisible. `test_every_vocabulary_in_the_module_is_guarded`
#: pins the two against each other.
VOCABULARIES: "dict[str, Vocabulary]" = {"posix": POSIX, "zephyr": ZEPHYR_SHELL}

#: The real output the measured stimuli answered with on the bed, for the
#: commands whose output the tester cannot choose. Transcribed from a live
#: `zephyr37_fat` open in this worktree (`help` abridged to its first lines
#: plus the two that matter), not invented: a fabricated sample would let the
#: scripted host below satisfy assertions the real guest does not.
#:
#: The `retval` line is kept ON PURPOSE. It is a genuine near-miss for the
#: framing check -- otto's Zephyr frame reads the return code back with a
#: `retval` command, and `help` legitimately LISTS that command -- so a
#: framing check written as `"retval" not in output` would fail on honest
#: output. Only the "a line of its own" spelling survives it.
MEASURED_ANSWERS: dict[str, str] = {
    "help": (
        "Please press the <Tab> button to see all available commands.\n"
        "\n"
        "Available commands:\n"
        "  device       : Device commands\n"
        "  help         : Prints the help message.\n"
        "  kernel       : Kernel commands\n"
        "  retval       : Print return value of most recent command\n"
        "  version      : Show kernel version"
    ),
    "kernel uptime": "Uptime: 42441320 ms",
}

#: The contracts this guard drives, by the short name the case table uses.
#: Named rather than walked for the same reason `VOCABULARIES` is: a contract
#: that stopped being driven here would be a silent hole.
#:
#: Each is called with a NOTES LIST as its second argument, standing in for the
#: `note_observable` fixture `tests/conformance/conftest.py` supplies to a real
#: run. A no-op stub would do to make the call work, and a list is used instead
#: because it lets the honest-host guard below assert the one thing a stub would
#: hide: that the framing contract's observable really does DIFFER between the
#: two vocabularies. A contract that takes no note ignores its argument.
CONTRACTS = {
    "exit code": lambda cell, _notes: _exec_contract.test_exec_reports_the_documented_exit_code(
        cell
    ),
    "framing": lambda cell, notes: _exec_contract.test_exec_frames_output_without_prompt_noise(
        cell, notes.append
    ),
    "sequence": lambda cell, _notes: (
        _exec_contract.test_a_failing_command_is_not_reported_as_success(cell)
    ),
    "timeout": lambda cell, _notes: (
        _timeout_contract.test_a_command_exceeding_its_budget_fails_the_documented_way(cell)
    ),
}

#: Each way a host can lie, and every contract that must catch it. A lie whose
#: contract is outside a vocabulary's applicable domain is dropped from that
#: vocabulary's cases by :func:`_lie_cases`, which records the drop as data
#: rather than letting it vanish into an empty assertion.
LIES: dict[str, set[str]] = {
    # The failure code is flattened to 1 -- the exact backend the exit-code
    # contract's "deliberately neither 0 nor 1 nor 255" comment is about.
    "collapses_failure_codes": {"exit code", "sequence"},
    # The `Status.Success` hardcoder the module docstring names.
    "always_reports_success": {"exit code", "sequence"},
    "leaks_a_frame_sentinel": {"framing", "sequence"},
    "echoes_the_command_line": {"framing", "sequence"},
    "swallows_all_output": {"framing", "sequence"},
    # A shell prompt where the command's answer should be. This is the case
    # the SHAPE assertion exists for: on a vocabulary that chose its output an
    # equality catches it, and on one that could not, only the pattern does.
    "answers_with_a_prompt": {"sequence", "timeout"},
    "stops_after_the_failure": {"sequence"},
    "never_times_out": {"timeout"},
    "wedges_the_session": {"timeout"},
}


def _contracts_for(words: Vocabulary) -> "list[str]":
    """The contracts whose applicable domain *words* is inside."""
    return [
        name for name in CONTRACTS if name != "timeout" or words.long_running_command is not None
    ]


def _lie_cases() -> "list[tuple[str, str, tuple[str, ...]]]":
    """Every (vocabulary, lie) pair that has a contract to catch it, and which."""
    cases = []
    for vocab_name, words in VOCABULARIES.items():
        applicable = _contracts_for(words)
        for lie, caught_by in LIES.items():
            within = tuple(name for name in applicable if name in caught_by)
            if within:
                cases.append((vocab_name, lie, within))
    return cases


LIE_CASES = _lie_cases()


class _ScriptedHost:
    """A host that answers a vocabulary's stimuli, honestly or with one lie.

    THE POINT OF THE WHOLE CLASS: it lets the contracts themselves be run once
    per vocabulary, so "every cell asserts the same property" is proved by
    EXECUTION rather than by inspection. An honest host must satisfy every
    contract under every vocabulary, and each lying host must be caught under
    every vocabulary whose domain includes the contract that catches it. A
    vocabulary that quietly weakened an assertion would let a lie through, and
    that is what turns red here.

    Answers only the commands its vocabulary names, and RAISES on anything
    else. A permissive default would make a contract that drifted onto a
    hard-coded bash spelling pass against this double while failing on the
    bed, which is the failure this task exists to end.
    """

    def __init__(self, words: Vocabulary, lie: "str | None") -> None:
        self._words = words
        self._lie = lie
        self._timed_out = False

    async def run(self, command, timeout=None) -> Results:
        commands = [command] if isinstance(command, str) else list(command)
        entries = [self._answer(one, timeout) for one in commands]
        if self._lie == "stops_after_the_failure":
            entries = self._truncate_at_the_failure(entries)
        return Results.collect(entries)

    @staticmethod
    def _truncate_at_the_failure(entries: "list[CommandResult]") -> "list[CommandResult]":
        kept: list[CommandResult] = []
        for entry in entries:
            kept.append(entry)
            if not entry.is_ok:
                break
        return kept

    def _answer(self, command: str, timeout: "float | None") -> CommandResult:
        words = self._words
        if command == words.long_running_command and timeout is not None:
            if self._lie == "never_times_out":
                return CommandResult(Status.Success, value="", command=command, retcode=0)
            self._timed_out = True
            return CommandResult(
                Status.Error,
                value=f"Command timed out after {timeout}s; partial output discarded",
                command=command,
                retcode=-1,
                timed_out=True,
            )
        if self._lie == "wedges_the_session" and self._timed_out:
            return CommandResult(
                Status.Failed, value="", command=command, retcode=1, msg="session is gone"
            )
        return self._ordinary(command)

    def _ordinary(self, command: str) -> CommandResult:
        words = self._words
        retcode = {
            words.succeeding_command: 0,
            words.failing_command: words.failing_code,
            words.sequence_failing_command: words.sequence_failing_code,
            words.multiline_command: 0,
            words.single_line_command: 0,
        }.get(command, "unknown")
        if retcode == "unknown":
            raise AssertionError(
                f"a contract issued {command!r}, which the {words!r} vocabulary does not "
                f"name -- a hard-coded spelling has crept back into a contract body"
            )
        value = self._output_for(command)
        status = Status.Success if retcode == 0 else Status.Failed
        if self._lie == "always_reports_success":
            return CommandResult(Status.Success, value=value, command=command, retcode=0)
        if self._lie == "collapses_failure_codes" and retcode != 0:
            retcode = 1
        return CommandResult(status, value=value, command=command, retcode=retcode)

    def _output_for(self, command: str) -> str:
        words = self._words
        if command == words.multiline_command:
            value = words.multiline_expected or MEASURED_ANSWERS[command]
        elif command == words.single_line_command:
            if self._lie == "answers_with_a_prompt":
                # Framing-clean, non-empty, and not the command line -- so only
                # an equality or a SHAPE can reject it.
                return "uart:~$"
            value = words.single_line_expected or MEASURED_ANSWERS[command]
        else:
            # Nothing asserts these commands' output, so an honest host may
            # answer with nothing.
            value = ""
        if self._lie == "leaks_a_frame_sentinel":
            return f"__OTTO_deadbeef_BEGIN__\n{value}"
        if self._lie == "echoes_the_command_line":
            return f"{command}\n{value}"
        if self._lie == "swallows_all_output":
            return ""
        return value


def _scripted_cell(words: Vocabulary, lie: "str | None") -> ResolvedCell:
    """A resolved cell whose host is scripted rather than real."""

    @contextlib.asynccontextmanager
    async def opener():
        yield _ScriptedHost(words, lie)

    return ResolvedCell(
        cell=Cell("scripted", "term", "transfer"),
        kind="scripted",
        open_host=opener,
        remote_scratch=None,
        vocabulary=words,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("vocab_name", sorted(VOCABULARIES))
async def test_an_honest_host_satisfies_every_contract_in_every_vocabulary(vocab_name) -> None:
    """The stimulus varies; the contracts still pass. Half one of the property claim.

    Without this half, the lie matrix below would be satisfied by a contract
    that failed on EVERYTHING -- a body of `assert False` catches every lie in
    every vocabulary. So the honest host has to get through first.

    ALSO CAPTURES WHAT EACH CONTRACT SAYS IT WATCHED, which is what
    :func:`test_the_framing_observable_differs_between_the_two_vocabularies`
    below compares across dialects.
    """
    words = VOCABULARIES[vocab_name]
    for name in _contracts_for(words):
        await CONTRACTS[name](_scripted_cell(words, lie=None), [])


async def _noted_observable(vocab_name: str) -> str:
    """What the framing contract says it watched, under *vocab_name*."""
    notes: "list[str]" = []
    await CONTRACTS["framing"](_scripted_cell(VOCABULARIES[vocab_name], lie=None), notes)
    assert len(notes) == 1, f"the framing contract noted {len(notes)} observables, not 1"
    return notes[0]


@pytest.mark.asyncio
async def test_the_framing_observable_differs_between_the_two_vocabularies() -> None:
    """★ THE CLAIM THE `observable` FIELD EXISTS FOR, measured rather than asserted.

    Spec §5 asks a `measured-ok` cell to name its observable BECAUSE a surface's
    observable differs by environment -- shell-history suppression is provable
    on bash and not provable at all on the five BusyBox guests. A field derived
    from the surface id could not express that, which is why
    `tests/conformance/_observable.py` renders a per-cell template and lets a
    contract narrow it further from its own body.

    The framing surface is where this tree's own version of that difference
    lives: a POSIX cell's stimulus is a `printf` whose text the tester CHOSE, so
    exact equality is available; a Zephyr cell's is a stock builtin whose text
    belongs to the firmware, so it is not. Both directions are asserted -- the
    two must differ, AND each must name the comparison it really made -- because
    "they differ" alone is satisfied by two strings that differ for no reason.
    """
    posix = await _noted_observable("posix")
    zephyr = await _noted_observable("zephyr")
    assert posix != zephyr, (
        "the framing observable renders identically on a POSIX and a Zephyr cell, so "
        "the matrix's `observable` field could not disagree with the surface's own name"
    )
    assert "exact equality against the 2 lines the tester chose" in posix, posix
    assert "NO exact comparison" in zephyr, zephyr
    assert "belongs" in zephyr, zephyr
    assert "firmware" in zephyr, zephyr


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("vocab_name", "lie", "caught_by"),
    LIE_CASES,
    ids=[f"{vocab}-{lie}" for vocab, lie, _ in LIE_CASES],
)
async def test_every_lie_is_caught_in_every_vocabulary_that_can_be_asked(
    vocab_name, lie, caught_by
) -> None:
    """Half two: the SAME lies are caught whichever dialect the host is asked in.

    THIS IS THE GUARD THE VOCABULARY LAYER IS BUILT AROUND. Varying the
    stimulus per cell is the point; varying the ASSERTION would turn seven
    contracts into a per-userland suite that can never disagree with the
    product. A vocabulary that quietly narrowed what its cells are asked --
    an early return for a userland, an assertion moved behind an `if`, a
    weakened expectation -- lets one of these lies through, and this reddens.

    Each lie is driven through every contract that must catch it, rather than
    through one, so a contract that stopped catching it cannot be covered for
    by a sibling that still does.
    """
    words = VOCABULARIES[vocab_name]
    for name in caught_by:
        with pytest.raises(AssertionError) as caught:
            await CONTRACTS[name](_scripted_cell(words, lie=lie), [])
        assert "scripted" in str(caught.value), (
            f"the {name!r} contract caught {lie!r} under {vocab_name} but its message "
            f"does not name the cell: {caught.value}"
        )


def test_the_lie_matrix_drops_a_case_only_where_the_contract_does_not_apply() -> None:
    """Which (vocabulary, lie) pairs are NOT measured, recorded rather than implied.

    A pair vanishes from :data:`LIE_CASES` when every contract that would
    catch that lie is outside the vocabulary's applicable domain. That is
    correct -- a Zephyr shell has no command that outlives a budget, so the
    timeout contract is not asked of it -- but it is also exactly how a lie
    could stop being measured by accident. Recorded here so widening or
    narrowing a domain has to come past this line.
    """
    dropped = {
        (vocab_name, lie)
        for vocab_name in VOCABULARIES
        for lie in LIES
        if (vocab_name, lie) not in {(v, ell) for v, ell, _ in LIE_CASES}
    }
    assert dropped == {("zephyr", "never_times_out"), ("zephyr", "wedges_the_session")}, (
        f"the set of unmeasured (vocabulary, lie) pairs moved to {sorted(dropped)}; "
        f"every drop must be a contract outside that vocabulary's domain, not a "
        f"lie that quietly stopped being caught"
    )
    for vocab_name, lie in dropped:
        assert LIES[lie] == {"timeout"}, (
            f"{vocab_name}/{lie} is unmeasured, but it is not a timeout-only lie"
        )
        assert VOCABULARIES[vocab_name].long_running_command is None


def test_every_vocabulary_in_the_module_is_guarded() -> None:
    """A vocabulary the module exports but these guards never drive is unmeasured.

    BY IDENTITY, NOT BY VALUE, and that is a measurement rather than a style
    choice. :class:`Vocabulary` is a frozen dataclass, so a second vocabulary
    whose fields happen to equal an existing one's compares EQUAL and
    de-duplicates inside a set -- measured: a fabricated third export copied
    from POSIX left this test green while nothing drove it. ``is`` is what
    distinguishes "the same dialect" from "a different object that currently
    agrees".
    """
    exported = [
        (name, value)
        for name, value in vars(_vocabulary_module).items()
        if isinstance(value, Vocabulary) and not name.startswith("_")
    ]
    driven = list(VOCABULARIES.values())
    unguarded = [name for name, value in exported if not any(value is one for one in driven)]
    assert not unguarded, (
        f"tests/conformance/_vocabulary.py exports {unguarded}, which "
        f"tests/unit/test_conformance_bed.py never drives; add each to VOCABULARIES "
        f"or a userland's dialect ships with nothing measuring it"
    )
    assert len(exported) == len(driven), (
        f"{len(driven)} vocabularies are driven here but the module exports "
        f"{len(exported)}: {[name for name, _ in exported]}"
    )


def test_a_vocabulary_carries_data_and_never_behaviour() -> None:
    """No callable, no predicate: the field types are the defence.

    A vocabulary that could carry a checker would let a userland supply its
    own assertion, which is the one thing this layer must not allow. Field
    TYPES rather than a naming convention, because a callable named
    ``expected_something`` would read as data.
    """
    allowed = (str, int, float, type(None))
    for vocab_name, words in VOCABULARIES.items():
        for field in dataclasses.fields(words):
            value = getattr(words, field.name)
            assert isinstance(value, allowed), (
                f"{vocab_name}.{field.name} is a {type(value).__name__}; a vocabulary "
                f"carries stimuli and expected values, never behaviour"
            )
            assert not callable(value), (
                f"{vocab_name}.{field.name} is callable, so this userland could answer "
                f"a different question from every other one"
            )


def test_no_vocabulary_pins_ottos_never_ran_sentinel_as_a_failure_code() -> None:
    """``-1`` and ``0`` are both unusable as an expected failure code.

    ``retcode == -1`` is otto's reserved "the command never ran" value
    (``CommandResult.exit_code`` maps it to ssh's 255), and Zephyr's
    signed-errno convention makes it a genuinely reachable answer
    (``-EPERM``) -- so a stimulus that returned it would be reported as a
    connection failure and be indistinguishable from one. ``0`` would make
    the failing half of the exit-code contract assert success.

    NOT MEASURED, and deliberately not asserted as fact: whether any bed guest
    can actually be driven to ``-EPERM``. This pins the expectation.
    """
    for vocab_name, words in VOCABULARIES.items():
        for field_name in ("failing_code", "sequence_failing_code"):
            code = getattr(words, field_name)
            assert code not in (0, -1), (
                f"{vocab_name}.{field_name} is {code}: -1 is otto's 'never ran' sentinel "
                f"and 0 is a success, so neither can stand for a command that failed"
            )


def test_the_two_failing_commands_are_never_the_same_command() -> None:
    """The cross-wired-constant discriminator, in the half every vocabulary keeps.

    On a POSIX shell the two contracts' failing commands differ in their CODE
    as well (5 against 42). A Zephyr shell has exactly one failure code that
    is stable across this lab's LTS releases -- measured, ``kernel uptime
    extra arg`` answers ``-22`` on 3.7 and 4.4 and ``0`` on 2.7 -- so there
    the codes coincide and only the command NAMES differ. What survives is
    ``Results.first_failure.command``; this pins that it survives.
    """
    for vocab_name, words in VOCABULARIES.items():
        assert words.sequence_failing_command != words.failing_command, (
            f"{vocab_name} uses one command for both failing contracts, so a "
            f"cross-wired constant could make one pass on the other's evidence"
        )


def test_every_vocabulary_can_plant_ottos_sentinel_prefix_in_its_own_output() -> None:
    """The framing surface's positive control needs a stimulus that CARRIES the prefix.

    Without it the control could only assert that a clean reply is clean,
    which is the vacuous form of exactly the check it exists to make
    meaningful. Pinned per vocabulary because the two spellings work by
    completely different means -- ``printf`` on a POSIX shell, and on a Zephyr
    shell an UNKNOWN COMMAND whose name is the token, since that shell has no
    way to print an arbitrary string and answers ``<name>: command not
    found``.

    NOT MEASURED HERE, and it cannot be: that the host really echoes it back.
    That is what the control itself asserts, on every cell, which is why its
    first assertion is that the plant landed at all.
    """
    for vocab_name, words in VOCABULARIES.items():
        assert OTTO_SENTINEL_PREFIX in words.sentinel_plant_command, (
            f"{vocab_name}.sentinel_plant_command is {words.sentinel_plant_command!r}, which "
            f"cannot put {OTTO_SENTINEL_PREFIX!r} into any output"
        )


def test_every_vocabulary_can_delete_a_file_and_report_whether_it_did() -> None:
    """The controls' cleanup must be a MEASUREMENT, not a best-effort gesture.

    Two properties, and the second is the one measured on the bed. The
    template must take a path; and it must not be a spelling that succeeds
    whether or not the file was there -- ``rm -f`` answers 0 either way, so an
    assertion on it could never fail, while plain ``rm`` and ``fs rm`` both
    answer non-zero for an absent file. That makes the removal its own
    verification: success means there WAS a file and there is not one now.
    """
    for vocab_name, words in VOCABULARIES.items():
        assert "{path}" in words.remove_file_template, (
            f"{vocab_name}.remove_file_template is {words.remove_file_template!r} and "
            f"names no path to delete"
        )
        assert " -f" not in words.remove_file_template, (
            f"{vocab_name}.remove_file_template forces the removal, so it answers success "
            f"for a file that was never there and the controls' cleanup check becomes a "
            f"guard that cannot fail"
        )


def test_every_long_running_command_outruns_the_budget_by_an_order_of_magnitude() -> None:
    """The MARGIN is a discriminator, not padding.

    At a margin near 1 a backend that quietly ran the command to completion
    and reported success is indistinguishable from one that timed out, on a
    loaded box. The contract's budget lives in the contract; the duration
    lives in the vocabulary; this is the only place the two meet.
    """
    for vocab_name, words in VOCABULARIES.items():
        assert (words.long_running_command is None) == (words.long_running_seconds is None), (
            f"{vocab_name} names one half of its long-running stimulus and not the other"
        )
        if words.long_running_seconds is None:
            continue
        assert words.long_running_seconds >= _BUDGET_S * 10, (
            f"{vocab_name}'s `{words.long_running_command}` runs for "
            f"{words.long_running_seconds}s against a {_BUDGET_S}s budget; below a 10x "
            f"margin a completed command and a timed-out one are not distinguishable"
        )


def test_every_bed_cell_carries_the_vocabulary_its_own_userland_answers_in() -> None:
    """Derived from lab data over the whole space, not spot-checked.

    ``axes_for`` resolves the userland off the host otto BUILDS from
    ``lab.json``, so this also fails the day a guest's declared userland and
    its vocabulary stop agreeing -- which a table keyed by element could not
    do, because a missing entry would simply never be asked for.
    """
    for resolved in bed_space():
        expected = vocabulary_for_userland(axes_for(resolved.cell.element, BED_TECH).userland)
        assert resolved.vocabulary is expected, (
            f"{cell_label(resolved)} carries a vocabulary its userland did not choose"
        )


def test_the_zephyr_guests_are_asked_in_the_zephyr_dialect_and_nobody_else_is() -> None:
    """The split, pinned against the real space rather than against the mapping.

    Both halves matter: a mapping that answered ZEPHYR_SHELL for everything
    would satisfy the first assertion and fail the second.
    """
    zephyr = {rc.cell.element for rc in bed_space() if rc.vocabulary is ZEPHYR_SHELL}
    posix = {rc.cell.element for rc in bed_space() if rc.vocabulary is POSIX}
    assert zephyr == {
        "zephyr27_fat",
        "zephyr37_fat",
        "zephyr37_lfs",
        "zephyr37_llext",
        "zephyr37_nofs",
        "zephyr44_lfs",
        "zephyr44_llext",
    }, f"the Zephyr dialect reached {sorted(zephyr)}"
    assert posix == {
        "test1",
        "test2",
        "test3",
        "test4",
        "bb1161",
        "bb1211",
        "bb1281",
        "bb1310",
        "bb1350",
    }, f"the POSIX dialect reached {sorted(posix)}"
    assert not (zephyr & posix)


def test_a_userland_with_no_vocabulary_raises_rather_than_defaulting_to_bash() -> None:
    """INJECTS the hostile condition: a userland layer nothing here has heard of.

    A fall-through to POSIX is the failure this raise exists to prevent -- a
    new userland would be asked bash's questions and would either go red for
    something that reads as a product bug or, worse, go green having measured
    the wrong dialect. Two shapes, because a prefix test and an equality test
    fail differently.
    """
    for userland in ("plan9-4", "gnu-but-not-really", "zephyr", "busybox"):
        with pytest.raises(ValueError, match="no conformance vocabulary"):
            vocabulary_for_userland(userland)


def test_a_resolved_cell_cannot_be_built_without_naming_its_dialect() -> None:
    """REQUIRED, with no default, for the reason ``remote_scratch`` is.

    A default of POSIX would hand every cell a future venue resolves bash's
    spellings; a userland with none of them would go red for a reason that
    reads as a product bug rather than as a venue that never said what dialect
    it speaks.
    """
    with pytest.raises(TypeError, match="vocabulary"):
        ResolvedCell(
            cell=Cell("nowhere", "ssh", "scp"),
            kind="fabricated",
            open_host=lambda: None,
            remote_scratch=None,
        )


def test_the_timeout_contract_covers_every_cell_with_a_long_running_stimulus() -> None:
    """Which cells the timeout contract's domain includes and excludes, pinned.

    The exclusion is the seven ``bed-zephyr`` cells and nothing else. Both
    directions are asserted: a domain that answered False for everything would
    satisfy "no Zephyr cell is in it" while covering nothing at all, and one
    that answered True for everything would put the wedge shape back on a
    single-client console.
    """
    space = bed_space()
    inside = {cell_label(rc) for rc in space if _timeout_contract.applicable_cell(rc)}
    outside = {cell_label(rc) for rc in space if not _timeout_contract.applicable_cell(rc)}

    assert outside == {cell_label(rc) for rc in space if rc.kind == BED_ZEPHYR}, (
        f"the timeout contract's domain excludes {sorted(outside)}, which is not "
        f"exactly the single-client console cells"
    )
    assert len(outside) == 7, f"expected the 7 Zephyr cells outside the domain, got {len(outside)}"
    assert len(inside) == len(space) - 7
    assert all(rc.vocabulary.long_running_command is None for rc in space if rc.kind == BED_ZEPHYR)

    for resolved in hermetic_space():
        assert _timeout_contract.applicable_cell(resolved), (
            f"{cell_label(resolved)} is a POSIX shell with `sleep`; the timeout "
            f"contract must not have narrowed the hermetic venue"
        )
