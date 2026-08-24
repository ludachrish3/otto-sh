"""The bed openers, exercised against a real lab VM. One committed witness.

WHY THIS FILE EXISTS, plainly: an opener nothing ever opens is not code that
works, it is code that compiles. The sibling item in this workstream shipped
its openers checked once by a throwaway probe that was then deleted, so the
verification did not survive the commit and the first real exercise came a
whole task later. This is that check, committed, so it runs again every time
anyone asks for it.

WHAT IT COVERS AND WHAT IT DOES NOT, because a witness that overstates itself
is worse than none:

- ``bed-unix`` over ``ssh`` IS exercised here, end to end: the factory builds
  the host from the committed lab entry with the cell's pair pinned, the
  connect probe answers, and a real command runs over the real transport.
- ``bed-busybox`` IS exercised here too, and it covers something ``bed-unix``
  structurally cannot: a HOPPED host. ``bb1350`` is reached over ``telnet``
  THROUGH ``test1``, so opening it exercises the lab context
  ``tests/conformance/_lab_context.py`` installs -- the thing whose absence
  made all 17 hopped cells fail before any transport existed (Task 4b). A
  ``bed-unix`` cell hops through nothing, so no amount of it would have caught
  that.
- ``bed-zephyr`` is NOT, and deliberately. Its opener takes the identical path
  -- one factory call, one ``verify_connection``, both inside that same lab
  context -- and that path is asserted hostlessly over all 49 cells in
  ``tests/unit/test_conformance_bed.py``. One ``bed-zephyr`` cell
  (``zephyr37_fat``) HAS been opened against hardware, by hand, during Task
  4b; nothing committed repeats it, and that is the right trade rather than an
  omission. The Zephyr console serves exactly ONE client, and this tree's
  serialization for it (``tests/conformance/_console_safety.py``) protects
  the items the SAMPLER parametrizes, keyed off the ``resolved_cell`` param.
  A witness names its own cell and so carries no ``resolved_cell``: measured
  against ``tests/conformance/conftest.py``'s own ``_cell_under_test``, a
  callspec of ``{'transfer': 'scp'}`` answers ``None``, and both the autouse
  hold and the ``pytest_runtest_call`` guard return early on ``None``. So a
  console witness would have to take the hold itself -- and without it,
  opening a console from here can wedge the guest until ``make qemu-restart``
  (issue #260). An unprotected console open is the one thing this item must
  not ship.
- the TRANSFER axis is crossed at the level the opener owns -- two cells that
  differ only in transfer both build and both report the transfer they were
  drawn for -- and NOT at the level of moving a file. ``verify_connection``
  dials the term channel and warms only ``ftp``; whether ``scp`` and ``sftp``
  behave alike is what ``test_transfer_contract.py`` asks once the venue is
  wired.

MARKED ``conformance_bed`` ON TOP OF ``conformance``, and both are
load-bearing. ``conformance`` keeps this out of every default gate, the way it
keeps the whole tree out. ``conformance_bed`` is subtracted by the hermetic
``make conformance`` lane, which CI runs nightly with no lab: the rest of this
tree reaches the bed only under ``OTTO_CONFORMANCE_BED=1``, and this test
reaches it always, so the venue knob cannot be what excludes it.

Takes no ``resolved_cell``: this is a witness for the openers themselves, so
it names the cell it means rather than measuring whichever cell the session's
seed happened to draw.
"""

import pytest

from tests.conformance._bed import BED_BUSYBOX, BED_UNIX, bed_space

pytestmark = [pytest.mark.asyncio, pytest.mark.conformance, pytest.mark.conformance_bed]

# The element this witness opens, and it is a choice about the SHARED bed
# rather than about the code. `test1` fronts the five BusyBox QEMU guests and
# `test3` is what `tests/integration/conftest.py`'s session-scoped autouse
# fixture SSHes into to reap docker containers, so `test2` is the peer whose
# concurrent use by another session is least likely to be something this test
# perturbs. Any of the three would exercise the same opener.
WITNESS_ELEMENT = "test2"

# Two cells that differ ONLY in transfer, so the pair each host reports can be
# attributed to the cell it was drawn for rather than to a single default.
WITNESS_TERM = "ssh"
WITNESS_TRANSFERS = ["scp", "sftp"]

# A command with output, not a bare success. A host that answered the connect
# probe and then returned nothing to every command would pass a status-only
# assertion, which is the same "green against a host nobody reached" this
# venue exists to make impossible.
_ECHO_TOKEN = "otto-bed-opener-witness"

# The HOPPED cell this witness opens, and the element is again a choice about
# the shared bed. All five BusyBox guests sit behind `test1` and answered on
# Task 4b's run, so any would do; `bb1350` is the newest pinned userland,
# which is the one whose `echo` is least likely to be the interesting variable
# when this test is what fails. The point of the cell is the HOP, not the
# BusyBox version -- `tests/busybox/` and `tests/integration/busybox_bed/`
# are where userland differences belong.
BUSYBOX_WITNESS_ELEMENT = "bb1350"
BUSYBOX_WITNESS_TERM = "telnet"
BUSYBOX_WITNESS_TRANSFER = "shell"


def _witness_cell(element: str, term: str, transfer: str):
    """One cell, looked up in the REAL space rather than constructed.

    Selected out of ``bed_space()`` so the witness can only ever open a cell
    the venue would actually offer. A cell that stopped existing -- a lab-data
    edit, a menu change -- fails the lookup by name here instead of silently
    testing a cell of this file's invention.
    """
    by_triple = {(r.cell.element, r.cell.term, r.cell.transfer): r for r in bed_space()}
    wanted = (element, term, transfer)
    if wanted not in by_triple:
        raise RuntimeError(
            f"the bed space no longer offers {wanted} -- this witness cannot open a cell "
            f"the venue does not have. Available for {element!r}: "
            f"{sorted(t for t in by_triple if t[0] == element)}"
        )
    return by_triple[wanted]


def _witness_cells():
    """The two ``bed-unix`` cells this witness opens, in ``WITNESS_TRANSFERS`` order."""
    return [
        _witness_cell(WITNESS_ELEMENT, WITNESS_TERM, transfer) for transfer in WITNESS_TRANSFERS
    ]


@pytest.mark.parametrize("transfer", WITNESS_TRANSFERS)
async def test_the_bed_opener_opens_a_real_unix_host_over_ssh(transfer: str) -> None:
    """A real lab VM, opened by the venue's own opener, answering a real command.

    Asserts three things that fail in three different ways:

    - the KIND the space filed this cell under, so a witness that started
      opening something else says so instead of quietly passing;
    - the PAIR the built host reports, which is what the whole crossing rests
      on -- an opener that dropped the pin would still open ``test2`` and
      still run this command, and only this assertion would notice;
    - the OUTPUT of a command that actually crossed the wire, because
      ``verify_connection`` succeeding proves a socket, not a shell.

    ``.only``, never ``.value``: ``run()`` returns ``Results``, whose ``value``
    is the LIST of per-command results. It stringifies to
    ``[CommandResult(...)]``, which CONTAINS the output -- so an ``in`` check
    against the wrong attribute passes while measuring nothing. That has
    already cost this workstream one bug.
    """
    resolved = next(r for r in _witness_cells() if r.cell.transfer == transfer)
    assert resolved.kind == BED_UNIX

    async with resolved.open_host() as host:
        assert (host.term, host.transfer) == (WITNESS_TERM, transfer), (
            f"the opener built {host.term}/{host.transfer} for a cell drawn as "
            f"{WITNESS_TERM}/{transfer}"
        )
        answered = (await host.run(f"echo {_ECHO_TOKEN}")).only
        assert answered.is_ok, answered
        assert str(answered.value).strip() == _ECHO_TOKEN


async def test_the_bed_opener_opens_a_real_hopped_busybox_guest_over_telnet() -> None:
    """A QEMU guest behind a hop, opened by the venue's own opener.

    THE HOP IS WHAT THIS ADDS. ``bb1350`` declares ``hop: "test1"``, so
    opening it drives a path the ``bed-unix`` witness above never touches:
    ``RemoteHost._build_hop_transport`` asks for ``test1`` by id, which needs
    a populated ``OttoContext`` because ``create_host_from_dict`` leaves the
    built host's ``_lab`` back-reference ``None``. That context comes from
    ``tests/conformance/_lab_context.py``, installed inside the opener.
    Before it existed, all 17 hopped cells failed identically and BEFORE any
    transport was created -- ``cannot resolve hop 'test1': the host has no lab
    back-reference and there is no active OttoContext``. Eleven of them were
    then opened by hand and nothing committed repeated it, which is exactly
    the gap this file exists to close.

    Asserts the same three independent things the ``bed-unix`` witness does --
    the KIND the space filed the cell under, the PAIR the built host reports,
    and the OUTPUT of a command that crossed the wire -- because they fail in
    three different ways and a socket is not a shell.

    NOT a transfer. Whether ``shell`` moves a file is
    ``test_transfer_contract.py``'s question, and asking it here would put a
    second copy of that contract outside the sampler.
    """
    resolved = _witness_cell(
        BUSYBOX_WITNESS_ELEMENT, BUSYBOX_WITNESS_TERM, BUSYBOX_WITNESS_TRANSFER
    )
    assert resolved.kind == BED_BUSYBOX

    async with resolved.open_host() as host:
        assert (host.term, host.transfer) == (BUSYBOX_WITNESS_TERM, BUSYBOX_WITNESS_TRANSFER), (
            f"the opener built {host.term}/{host.transfer} for a cell drawn as "
            f"{BUSYBOX_WITNESS_TERM}/{BUSYBOX_WITNESS_TRANSFER}"
        )
        assert host.hop == "test1", (
            f"{BUSYBOX_WITNESS_ELEMENT} is this witness's cell because it is HOPPED; "
            f"it now reports hop {host.hop!r}, so this test no longer covers the "
            f"lab-context path it was written for"
        )
        answered = (await host.run(f"echo {_ECHO_TOKEN}")).only
        assert answered.is_ok, answered
        assert str(answered.value).strip() == _ECHO_TOKEN
