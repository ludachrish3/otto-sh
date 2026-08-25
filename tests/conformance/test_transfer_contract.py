"""What every host owes a caller of ``put()`` and ``get()``: the bytes, and the mode.

A roundtrip test written on ASCII text is not a contract. It passes unchanged
against a backend that rewrites line endings, that stops at the first NUL, or
that drops a trailing newline -- three real failure modes of shell- and
console-framed transfers, and none of them visible in ``"hello world"``. The
payload below carries all three tripwires plus bytes that are not valid UTF-8,
so a backend that round-trips it has round-tripped a file rather than a string.

THE REMOTE DIRECTORY COMES FROM THE VENUE, not from here, and that is the
resolution of what this docstring used to hand forward. It used to say the
remote directory was a runner ``tmp_path`` -- true of the hermetic venue,
whose cells all share one filesystem with the process asserting on them --
and that the bed venue would need a real remote scratch directory instead.
It does, and it now has one: every cell carries its own answer
(:attr:`~tests.conformance._resolved.ResolvedCell.remote_scratch`, reached
through the ``remote_scratch`` fixture), so these assertions are unchanged
and only the path they run against moved. Measured before that landed: every
bed cell failed with ``scp: /tmp/pytest-of-vagrant/.../remote: No such file
or directory``, because the runner path does not exist on the far side.

THE FILENAME IS NAMESPACED BY WORKER, and on the bed that is not decoration.
The hermetic venue's ``tmp_path`` is unique per test, so nothing there can
collide; the bed's scratch directory is a FIXED path on a SHARED guest, so
two workers transferring ``payload.bin`` into ``/tmp`` on ``test1`` would
overwrite each other's file -- one of them reading back bytes, or a mode, that
the other wrote. ``tests/conftest.py``'s :func:`~tests.conftest.remote_name`
is the repo's existing answer to exactly that -- ``tests/integration/host/``
transfers under it into ``/tmp`` on ``test1`` and on all five BusyBox guests
-- and it is reused rather than restated. What it does NOT cover is a second
pytest SESSION on this box, which is the same gap the console lock carries
(``tests/conformance/_console_safety.py``).

WHAT THIS CONTRACT IS ABOUT, declared rather than assumed: cells whose host
has somewhere to put a file. See :func:`applicable_cell`.

AND WHERE OTTO ALREADY KNOWS IT CANNOT HOLD: the five ``bed-busybox`` ``nc``
cells, declared by :func:`expected_failure` and asserted as strict xfails
rather than skipped, excluded or edited out of lab data. otto's own
``nc-transfer`` gap registry carries the PUT path as ``PATH_OPEN`` and
predicts this exact timeout; the whole decision, the measurement of the
gap's GET/PUT asymmetry, and why only five of the ten items can ever XPASS
are under this module's banner.
"""

from collections.abc import Callable
from pathlib import Path

import pytest

from otto.host.host import BaseHost
from otto.host.transfer import BaseFileTransfer
from tests._fixtures.profiles import Cell
from tests.conformance._bed import BED_BUSYBOX
from tests.conformance._controls import assert_bed_left_clean, remove_landed
from tests.conformance._resolved import ResolvedCell
from tests.conftest import remote_name

pytestmark = [pytest.mark.asyncio, pytest.mark.conformance]


# Every tripwire a text-only payload misses, in one file:
#   \r\n      a backend that normalises line endings drops the \r
#   \x00      a backend that treats the payload as a C string stops here
#   \xff\xfe  bytes that are not valid UTF-8 at all
#   final \n  a backend that strips or adds a trailing newline is visible
_PAYLOAD = (
    b"otto-conformance\r\nnul->\x00<-nul\ttab\n\xff\xfe not utf-8\nand the file ends on a newline\n"
)

# Deliberately not 0o644, 0o664, 0o755 or 0o600: a backend that applies no mode
# at all lands one of those from the runner's umask, and an assertion against
# such a value would pass for the wrong reason. 0o615 keeps the owner read bit,
# so `get` can still read the file back.
_MODE = 0o615

# The SECOND mode, for the mode surface's positive control. Same three
# constraints as `_MODE` (not a umask default, owner read kept) plus the one
# that makes it a control: it must differ from `_MODE`, so a read-back that
# tracked the request can be told from one that answers a constant.
_CONTROL_MODE = 0o651

# The basename every cell's payload is transferred under, before
# `remote_name` namespaces it by worker. SHORT ON PURPOSE: the bed's seven
# Zephyr guests declare `max_filename_len: 32` (measured, `lab.json`), and
# `BaseFileTransfer.put_files`/`get_files` REFUSE an over-long basename up
# front -- `filename ... exceeds the 32-character basename limit`. A name
# derived from the pytest nodeid, which is the obvious way to make a remote
# name unique, is 85 characters for a bed cell (measured) and would turn every
# Zephyr transfer into that refusal. `master_payload.bin` is 18, the longest this
# can produce (an xdist worker id is `gwN`, shorter than `master`), and
# `tests/unit/test_conformance_bed.py` pins it against the limit every cell
# in the domain declares rather than against the 32 written here.
_PAYLOAD_NAME = "payload.bin"

# The two controls land files of their own and REMOVE them again, so each
# needs a basename the contracts do not use -- otherwise a control's cleanup
# would delete a file another worker's contract was mid-roundtrip on. Both
# stay short for the Zephyr guests' 32-character basename limit:
# `master_roundtrip.bin` is 20 and `master_mode.bin` is 15.
_ROUNDTRIP_CONTROL_NAME = "roundtrip.bin"
_MODE_CONTROL_NAME = "mode.bin"

# The corrupted payload: `_PAYLOAD` with ONE byte changed, and nothing else.
# A wholly different payload would also fail the comparison, and would prove
# less -- the question is whether the roundtrip is byte-sensitive, not whether
# it is length-sensitive.
_CORRUPT_AT = 0
_CORRUPTED_PAYLOAD = bytes([_PAYLOAD[0] ^ 0x01]) + _PAYLOAD[1:]


def applicable_cell(resolved: ResolvedCell) -> bool:
    """The drawn cells this contract is about: the ones with a remote directory.

    Read by ``tests/conformance/conftest.py``'s ``pytest_generate_tests``,
    which parametrizes ``resolved_cell`` over the drawn cells this answers
    True for. Today it narrows nothing in the hermetic venue (all 8 cells are
    a runner filesystem) and excludes exactly three of the bed's 49:
    ``zephyr37_nofs``, ``zephyr37_llext`` and ``zephyr44_llext``, which
    declare ``filesystem: "none"``.

    OTTO'S ANSWER, NOT A PREDICATE OF THIS SUITE'S. ``remote_scratch`` is
    ``None`` for exactly the hosts whose
    :class:`~otto.host.embedded_filesystem.EmbeddedFileSystem` reports
    ``supports_transfer`` False -- the flag
    ``otto.host.transfer.console.ConsoleFileTransfer`` short-circuits both
    ``_run_put`` and ``_run_get`` on, returning an error for every file. So
    otto already treats "no filesystem" as *nowhere to put a file* rather
    than as a transfer that fails, and this reads that rather than sniffing
    a variant name or an element.

    NOT A SKIP AND NOT AN EXCLUSION FROM THE SPACE, and the difference is the
    point. A skip inside a drawn cell reports success for a contract nobody
    ran. Dropping the cell from the space is worse than it looks: a Zephyr
    host reports a single ``(telnet, console)`` pair, so its one cell IS the
    guest, and dropping it would take that guest's exec and timeout coverage
    with it -- paying for one inapplicable contract with two applicable ones.
    A contract stating what it covers is not a run pretending it covered
    something.

    THE UNCOVERED HALF HAS A HOME, which is why this narrowing costs no
    coverage rather than merely little: what a no-filesystem target owes a
    caller of ``put`` is a CLEAR ERROR, and
    ``tests/integration/host/test_host_contract.py``'s
    ``test_no_filesystem_backend_surfaces_clear_error`` asserts exactly that
    against these same guests. That is the per-backend depth this lane
    deliberately does not duplicate.

    Which cells this includes and excludes is pinned in
    ``tests/unit/test_conformance_bed.py``, so a change that quietly widens
    or narrows the domain fails instead of passing.
    """
    return resolved.remote_scratch is not None


# ==========================================================================
# A REGISTERED, DELIBERATELY-OPEN GAP PATH -- otto's `nc` PUT on a BusyBox guest
# ==========================================================================
# Everything between this banner and the next rule is ONE decision, kept in
# one place so that reversing it is a single-file change: delete
# `expected_failure` below and the ten items go back to being plain failures.
# It is a recommendation the controller flagged to the maintainer and may yet
# be overridden.
#
# CORRECTED 2026-08-24, AFTER READING OTTO'S OWN GAP REGISTRY. An earlier
# version of this banner called it a "KNOWN PRODUCT DEFECT" the cell crossing
# had FOUND. That framing was wrong and the correction matters more than the
# xfail does: otto has this measured, recorded and reasoned about already.
# `src/otto/host/userland.py`'s `nc-transfer` Gap carries a `GapPath` for
# `otto.host.transfer.nc.NcFileTransfer._put_files_nc` with `state=PATH_OPEN`,
# whose detail says, verbatim:
#
#     spawns the device-side listener as `nc -l -w <secs> <port>`, the OpenBSD
#     spelling the applet does not accept (it wants `-l -p PORT`), and reads
#     nothing from this record. So the listener never binds, otto waits for a
#     peer that cannot arrive, and `_cancel_and_reap` ends it -- a timeout
#     rather than the refusal this record describes. STILL OPEN DELIBERATELY
#
# and gives the reason it stays open: the question that would decide the path
# -- whether the device's `nc` accepts a listener spelled `-l PORT` -- "cannot
# be settled without asking a device to BIND, which is a probe with a side
# effect on the host it is asking about". The queued fix is a whole BusyBox
# `nc` variant in `todo/busybox-parity-sweep-2026-08-11.md`, not a spelling
# tweak.
#
# WHAT THIS LANE ACTUALLY CONTRIBUTES, which is narrower and still worth
# having: it is the first thing in this repo to drive that OPEN path against
# real BusyBox hardware and show the user-visible consequence, and it shows
# the ASYMMETRY inside one gap. Measured 2026-08-24 on `bb1161:telnet:nc`,
# both directions, one host build:
#
#     get   RAISES UnsupportedOnUserlandError before the wire, naming the
#           `nc-transfer` gap (path WIRED, guard
#           `otto.host.transfer.nc.refuse_if_nc_rejects_dash_n`)
#     put   RETURNS Status.Error after 5.0s:
#           `Remote nc listener on port 9000 not ready within 5.0s`
#
# So otto knows the backend cannot drive this applet and says so loudly in one
# direction, while the other fails into exactly the timeout the refusal exists
# to prevent. That asymmetry is the reportable observation -- not a
# rediscovery of the spelling, which the registry measured on 2026-08-13.
#
# ONE MEASUREMENT WORTH KEEPING that the registry does NOT record, taken on the
# bed by Task 4c of the item that built this venue: the two BusyBox generations
# fail the listener DIFFERENTLY, and the modern one fails silently.
#
#     bb1161 (1.16.1)  `nc: bad address '9000'`  -- a loud usage error
#     bb1350 (1.35.0)  process ALIVE, binds an EPHEMERAL port -- no error at all
#
# Both surface to otto as the same `Remote nc listener on port 9000 not ready
# within 5.0s`, so a `nc: timeout` reply is NOT evidence that a listener bound
# the requested port; `ps` plus `netstat` at check time is the discriminator.
#
# WHY IT IS NOT FIXED HERE: `src/otto/` is frozen for the item that built this
# venue, and the registry says the fix is a queued workstream rather than a
# one-line change.
#
# WHY xfail(strict=True) AND NOT A SKIP, AN EXCLUSION OR A LAB-DATA EDIT.
# These guests have a `nc` APPLET (the gap is about its option set, not its
# presence); deleting `nc` from their `valid_transfers` would encode a
# capability lie in lab data to buy a green, and the crossing that exercises
# this path is exactly what that edit would blind. A skip reports success for
# a contract nobody ran. A strict xfail is neither: it ASSERTS the failure, so
# the items are still exercised on every bed run and the lane's green still
# means something.
#
# BUT ONLY HALF OF THEM CAN EVER XPASS, and a reader relying on the strict
# marker as the reminder should know which. `test_put_lands_the_documented_
# mode_on_the_host` is a `put` alone, so wiring the PUT path turns those five
# green and the strict marker then reddens the lane until this declaration is
# deleted. `test_put_get_roundtrip_preserves_content` also does a `get`, and
# the GET is REFUSED BY DESIGN on these guests -- so those five keep failing
# after any PUT fix, for a declared incapability rather than a defect, and
# they will then need their own disposition.
#
# TEN ITEMS WHEN THIS BANNER WAS WRITTEN; TWENTY SINCE the support matrix put
# a positive control beside each contract here. The split above applies to the
# controls in exactly the same way -- the mode control is a `put` alone, the
# roundtrip control also does a `get`. The natural one is an
# applicable-domain narrowing keyed on otto's own answer, which is the model
# `applicable_cell` above already uses for `remote_scratch`.
#
# WHAT THE CROSSING IS STILL FOR: a contract that holds over `shell` and
# breaks over `nc` ON THE SAME HOST is invisible to any per-host suite, and
# `tests/integration/host/` parametrizes one transport per backend. The
# (term, transfer) crossing is the only place that difference can be seen.
_NC_ON_BUSYBOX = (
    "otto's `nc-transfer` gap, PUT direction: `NcFileTransfer._put_files_nc` is a "
    "GapPath with state PATH_OPEN in `src/otto/host/userland.py` -- it spells the "
    "device-side listener `nc -l -w <secs> <port>`, which the BusyBox applet does not "
    "accept (it wants `-l -p PORT`), and reads nothing from that record, so the put "
    "fails into `Remote nc listener on port <port> not ready` instead of the refusal "
    "the GET direction raises. A registered open path, not a test or lab-data problem: "
    "`shell` transfer passes on these same five guests, and `nc` passes on every "
    "`bed-unix` cell that uses it (test1-test4 over both ssh and telnet, 8 cells, "
    "16/16 items, measured on the first full `make conformance-bed` run). "
    "See this module's banner."
)


def expected_failure(resolved: ResolvedCell) -> "str | None":
    """The reason this cell's transfer contract is KNOWN to fail, or None.

    Read by ``tests/conformance/conftest.py``'s ``pytest_generate_tests``,
    which turns a reason into ``xfail(strict=True)`` on that cell's items.
    TWENTY items today: every test in this module -- the two contracts AND
    the two positive controls that arrived with the support matrix -- over
    the five ``bed-busybox`` cells whose transfer is ``nc``. It was ten
    before the controls landed. The controls are covered for the same reason
    the contracts are and not by oversight: each of them has to PUT a file to
    say anything, so on a cell where otto's ``nc`` listener never binds there
    is nothing for a control to demonstrate, and asserting the failure is the
    honest record of that.

    KEYED ON THE RESOLVER'S ANSWER, not on a guest's name. ``kind`` is what
    ``tests/conformance/_bed.py``'s ``bed_kind`` derived from the host's
    declared userland, so a new BusyBox guest is covered the day it joins the
    lab and a guest RENAMED to look like one is not. Task 1 of this item
    measured how easily the other reading passes: every real bed element
    happens to be named after its kind, so name-sniffing satisfies every
    assertion made against real data.

    Deliberately narrow in the other direction too. The bug is in the LISTENER
    otto asks a BusyBox userland to start, so this says nothing about ``nc``
    on a GNU userland (all eight ``bed-unix[test2:*]`` cells pass, ``nc``
    included) and nothing about ``shell`` transfer on these same guests (all
    five pass). A wider declaration would xfail cells that work, and a strict
    xfail on a passing cell is a red lane -- which is the right failure, but
    for the wrong reason.
    """
    if resolved.kind == BED_BUSYBOX and resolved.cell.transfer == "nc":
        return _NC_ON_BUSYBOX
    return None


# ==========================================================================


def _transfer_backend(host: BaseHost, cell: Cell) -> BaseFileTransfer:
    """The host's transfer backend, or a loud failure naming the cell.

    Reached through the private attribute both host families happen to agree
    on (``LocalHost`` and ``UnixHost`` each name it ``_file_transfer``), because
    there is no public one: the registry lookup ``build_transfer_backend`` would
    answer for ``sftp`` and ``scp`` but raises for the ``local`` cell, whose
    transfer name deliberately records the ABSENCE of a registered backend
    rather than naming one.

    Deliberately not ``getattr(host, "_file_transfer", None)`` with a lenient
    default. A host this cannot read is a cell whose transfer contract has
    never been measured, and the honest report of that is a named failure, not
    a quietly skipped assertion.
    """
    backend = getattr(host, "_file_transfer", None)
    if backend is None:
        raise AssertionError(
            f"{cell}: {type(host).__name__} exposes no `_file_transfer`, so this "
            f"cell's transfer backend cannot be asked whether it carries a mode"
        )
    return backend


@pytest.mark.observable(
    "the bytes get() reads back over `{cell.transfer}` after put() of a known payload "
    "to this host's own scratch directory"
)
async def test_put_get_roundtrip_preserves_content(
    resolved_cell: ResolvedCell, remote_scratch: Path, tmp_path: Path, worker_id: str
) -> None:
    """Byte-for-byte, including a trailing newline and a NUL.

    Three directories, not two, and on a bed cell only two of them are on
    this machine. A roundtrip into the source directory would have the
    ``get`` overwrite the very file it is being compared against, so a
    backend that transferred nothing at all would still "round-trip"
    perfectly. ``remote_scratch`` is the third, and it is the venue's answer:
    a directory under ``tmp_path`` where the far side is the runner, a
    device path where it is not.

    The aggregate results are asserted before the bytes because they answer a
    different question: a transfer can land the right bytes and still report
    failure (and a caller acting on the report would then delete or retry), and
    it can report success having written nothing.
    """
    source_dir = tmp_path / "source"
    retrieved_dir = tmp_path / "retrieved"
    for directory in (source_dir, retrieved_dir):
        directory.mkdir()
    source = source_dir / remote_name(worker_id, _PAYLOAD_NAME)
    source.write_bytes(_PAYLOAD)

    cell = resolved_cell.cell
    async with resolved_cell.open_host() as host:
        put = await host.put(source, remote_scratch)
        assert put.is_ok, f"{cell}: put reported {put.status!r} -- {put.msg!r}"
        got = await host.get(remote_scratch / source.name, retrieved_dir)
        assert got.is_ok, f"{cell}: get reported {got.status!r} -- {got.msg!r}"

    retrieved = retrieved_dir / source.name
    assert retrieved.exists(), f"{cell}: get reported success but wrote no file at {retrieved}"
    assert retrieved.read_bytes() == _PAYLOAD, (
        f"{cell}: the roundtripped file is not the payload -- "
        f"sent {len(_PAYLOAD)} bytes, got back {len(retrieved.read_bytes())}"
    )


@pytest.mark.observable(
    "what put(mode=...) leaves behind over `{cell.transfer}` -- which of the two "
    "observables that is depends on whether the backend carries a permission model"
)
async def test_put_lands_the_documented_mode_on_the_host(
    resolved_cell: ResolvedCell,
    remote_scratch: Path,
    tmp_path: Path,
    worker_id: str,
    note_observable: "Callable[[str], None]",
) -> None:
    """Mode survives the transfer where the backend claims to carry it.

    NOT A ROUNDTRIP ASSERTION, and the difference is measured rather than
    assumed. ``put(mode=...)`` documents the bits it sets ON THE HOST; nothing
    in otto claims ``get`` carries a mode back, and it does not: retrieving the
    same 0o615 file lands 0o664 through ``sftp`` and ``scp`` (the runner's
    umask) while ``LocalFileTransfer``'s ``shutil.copy2`` preserves it. A
    "roundtrip preserves mode" assertion would therefore be red on two of this
    venue's eight cells for a property otto never promised.

    Read back through ``run()`` rather than through ``Path.stat()``, because
    the question is what the mode is on the HOST. On a hermetic cell the two
    happen to be the same file; on a bed cell they are not, and a contract that
    only holds because the venue shares a filesystem is not a host contract.
    ``stat -c %a`` is the same spelling otto's own ``nc`` transfer already
    assumes for ``stat -c %s`` (``otto.host.userland``'s ``stat_size``
    default), and it answers on every cell here including BusyBox 1.16.1.

    The no-permission-model arm is written from ``BaseFileTransfer.put_files``'
    documented pre-flight refusal and is NOT exercised in the hermetic venue:
    all four backends this venue builds (``LocalFileTransfer``,
    ``SftpFileTransfer``, ``ScpFileTransfer``, and the ``UnixFileTransfer`` base
    they share) declare ``supports_mode = True``, measured. The embedded
    ``console``/``tftp`` backends are the ones that refuse, and they arrive with
    the bed venue.
    """
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    source = source_dir / remote_name(worker_id, _PAYLOAD_NAME)
    source.write_bytes(_PAYLOAD)
    landed = remote_scratch / source.name

    cell = resolved_cell.cell
    async with resolved_cell.open_host() as host:
        backend = _transfer_backend(host, cell)
        # THE BRANCH THE MARKER'S TEMPLATE CANNOT EXPRESS. Which observable
        # this cell offers is decided here, by otto's own `supports_mode`, and
        # only the running test knows the answer -- so the matrix cell records
        # what was actually watched rather than the surface's generic name.
        # See `tests/conformance/_observable.py`.
        note_observable(
            f"the mode `stat -c %a` reads back on the host after put(mode=0o{_MODE:o}) "
            f"over `{cell.transfer}`"
            if backend.supports_mode
            else f"the pre-flight refusal {type(backend).__name__} returns for a non-None "
            f"mode -- aggregate and per file -- because it declares no permission model",
        )
        put = await host.put(source, remote_scratch, mode=_MODE)

        if not backend.supports_mode:
            assert put.is_ok is False, (
                f"{cell}: {type(backend).__name__} declares no permission model, so "
                f"a non-None mode is refused before anything transfers"
            )
            assert all(not entry.is_ok for entry in put.value.values()), (
                f"{cell}: the refusal is per-file as well as aggregate -- {put.value!r}"
            )
            return

        assert put.is_ok, f"{cell}: put(mode=0o{_MODE:o}) reported {put.status!r} -- {put.msg!r}"
        observed = (await host.run(f"stat -c %a {landed}")).only

    assert observed.is_ok, (
        f"{cell}: could not read the landed file's mode back -- "
        f"`stat -c %a` gave {observed.status!r} {observed.value!r}"
    )
    assert observed.value.strip() == f"{_MODE:o}", (
        f"{cell}: put(mode=0o{_MODE:o}) left the file at 0o{observed.value.strip()}"
    )


# ==========================================================================
# POSITIVE CONTROLS -- proof that each observable above CAN GO RED on this cell
# ==========================================================================
# See `tests/conformance/_controls.py` for why these exist and why the marker
# rather than the signature is what tells a control from a contract.
#
# THEY INHERIT THIS MODULE'S TWO DECLARATIONS, and both are load-bearing.
# `applicable_cell` keeps them off the three bed guests with no filesystem,
# which is right: a cell where the contract is not asserted needs no evidence
# that it could go red. `expected_failure` marks them xfail(strict=True) on
# the five `bed-busybox` `nc` cells, which is also right and is worth stating
# plainly -- otto's `nc-transfer` gap breaks the PUT on those guests, so a
# control that has to put a file there cannot demonstrate anything, and
# asserting that it fails is the honest record of that. It takes the bed
# lane's expected xfail count for this module from 10 items to 20.


@pytest.mark.positive_control("transfer-roundtrip")
async def test_control_the_roundtrip_comparison_rejects_a_corrupted_byte(
    resolved_cell: ResolvedCell, remote_scratch: Path, tmp_path: Path, worker_id: str
) -> None:
    """Round-trip a payload one byte away from ``_PAYLOAD``; the difference must survive.

    The contract's instrument is ``retrieved.read_bytes() == _PAYLOAD``, and
    it is satisfied by any backend that answers those exact bytes however it
    got them -- a cache, an echo of the local source, a get that never
    reached the far side. This puts a payload the comparison must REFUSE and
    requires two things of the reply: that it does not equal ``_PAYLOAD``, and
    that it equals what was actually sent, differing at exactly the one index
    that was corrupted.

    So it fails against a backend whose get is content-blind in either
    direction: one that always answers the contract's payload, and one that
    answers something merely different.

    LEAVES THE BED AS FOUND. Its own basename, and
    :func:`~tests.conformance._controls.remove_and_verify` deletes the landed
    file and requires the deletion to have succeeded -- which on both
    userlands also proves the file was there to delete.
    """
    source_dir = tmp_path / "source"
    retrieved_dir = tmp_path / "retrieved"
    for directory in (source_dir, retrieved_dir):
        directory.mkdir()
    source = source_dir / remote_name(worker_id, _ROUNDTRIP_CONTROL_NAME)
    source.write_bytes(_CORRUPTED_PAYLOAD)
    landed = remote_scratch / source.name

    cell = resolved_cell.cell
    words = resolved_cell.vocabulary
    async with resolved_cell.open_host() as host:
        # The removal ALWAYS runs and NEVER raises; the verification happens
        # after, on the path where nothing else went wrong. A cleanup
        # assertion raised from a `finally` replaces the exception already on
        # its way out, which would report "could not remove the file" for a
        # cell whose real failure was that the transfer never happened.
        try:
            put = await host.put(source, remote_scratch)
            assert put.is_ok, f"{cell}: put reported {put.status!r} -- {put.msg!r}"
            got = await host.get(landed, retrieved_dir)
            assert got.is_ok, f"{cell}: get reported {got.status!r} -- {got.msg!r}"
        finally:
            removed = await remove_landed(host, words, landed)
    assert_bed_left_clean(removed, landed, cell)

    retrieved = retrieved_dir / source.name
    assert retrieved.exists(), f"{cell}: get reported success but wrote no file at {retrieved}"
    back = retrieved.read_bytes()
    assert back != _PAYLOAD, (
        f"{cell}: a payload corrupted at byte {_CORRUPT_AT} came back EQUAL to the "
        f"contract's payload, so `retrieved == _PAYLOAD` is true of this cell whatever "
        f"was sent and the contract's green means nothing here"
    )
    assert back == _CORRUPTED_PAYLOAD, (
        f"{cell}: the roundtripped file is neither the payload sent nor the contract's "
        f"-- sent {len(_CORRUPTED_PAYLOAD)} bytes, got back {len(back)}"
    )
    # `strict=False`: a reply of the wrong LENGTH is a different failure and is
    # caught by the equality above, so this must not raise before that reports.
    differing = [i for i, (a, b) in enumerate(zip(back, _PAYLOAD, strict=False)) if a != b]
    assert differing == [_CORRUPT_AT], (
        f"{cell}: the reply differs from the contract's payload at {differing}, not at "
        f"the single corrupted byte {_CORRUPT_AT} -- the comparison is reacting to "
        f"something other than the corruption"
    )


@pytest.mark.positive_control("transfer-mode")
async def test_control_the_landed_mode_follows_the_mode_that_was_asked_for(
    resolved_cell: ResolvedCell, remote_scratch: Path, tmp_path: Path, worker_id: str
) -> None:
    """Ask for a DIFFERENT mode, and require the observable to move with it.

    TWO ARMS, because the contract has two and each needs its own control.

    On a backend that carries a mode, the contract reads ``stat -c %a`` back
    and compares it to ``_MODE`` -- satisfied by a host whose files simply
    happen to sit at 0o615, and by a read-back that answers a constant. So
    this puts the same bytes at ``_CONTROL_MODE`` and requires the read-back
    to report THAT and not ``_MODE``: the channel moves with the request.

    On a backend with ``supports_mode = False`` the observable IS THE REFUSAL,
    and a refusal is the easiest thing in this file to fake -- a backend that
    failed every put would satisfy the contract's arm exactly. So the control
    asks three questions instead of one: two different modes must each be
    refused with their OWN mode named (the refusal is a function of the
    argument, not a constant), and a put with NO mode must SUCCEED (the
    refusal is about the mode, not about putting). MEASURED on
    ``zephyr37_fat`` 2026-08-24 before this was written: ``ConsoleFileTransfer``
    refuses 0o615 and 0o651 with each spelled into its message, and the
    mode-less put of the same file succeeds.

    Leaves the bed as found on both arms: whichever put landed a file, that
    file is removed and the removal verified.
    """
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    source = source_dir / remote_name(worker_id, _MODE_CONTROL_NAME)
    source.write_bytes(_PAYLOAD)
    landed = remote_scratch / source.name

    cell = resolved_cell.cell
    words = resolved_cell.vocabulary
    async with resolved_cell.open_host() as host:
        backend = _transfer_backend(host, cell)

        if not backend.supports_mode:
            refusals = {
                mode: await host.put(source, remote_scratch, mode=mode)
                for mode in (_MODE, _CONTROL_MODE)
            }
            for mode, refused in refusals.items():
                assert refused.is_ok is False, (
                    f"{cell}: {type(backend).__name__} declares no permission model but "
                    f"accepted mode 0o{mode:o}"
                )
                assert f"0o{mode:o}" in str(refused.msg), (
                    f"{cell}: the refusal of mode 0o{mode:o} does not name it -- "
                    f"{refused.msg!r}. A refusal that says the same thing whatever it "
                    f"was asked is not evidence that it read the mode at all"
                )
            assert str(refusals[_MODE].msg) != str(refusals[_CONTROL_MODE].msg), (
                f"{cell}: both modes were refused with the identical message, so the "
                f"contract's refusal assertion holds for every mode this cell can be asked"
            )
            try:
                plain = await host.put(source, remote_scratch)
                assert plain.is_ok, (
                    f"{cell}: a put with NO mode reported {plain.status!r} -- "
                    f"{plain.msg!r}. Every put fails on this cell, so the contract's arm "
                    f"is satisfied by a backend that refuses everything rather than by "
                    f"one that read a mode"
                )
            finally:
                removed = await remove_landed(host, words, landed)
            assert_bed_left_clean(removed, landed, cell)
            return

        try:
            put = await host.put(source, remote_scratch, mode=_CONTROL_MODE)
            assert put.is_ok, (
                f"{cell}: put(mode=0o{_CONTROL_MODE:o}) reported {put.status!r} -- {put.msg!r}"
            )
            observed = (await host.run(f"stat -c %a {landed}")).only
        finally:
            removed = await remove_landed(host, words, landed)
    assert_bed_left_clean(removed, landed, cell)

    assert observed.is_ok, (
        f"{cell}: could not read the landed file's mode back -- "
        f"`stat -c %a` gave {observed.status!r} {observed.value!r}"
    )
    assert observed.value.strip() == f"{_CONTROL_MODE:o}", (
        f"{cell}: put(mode=0o{_CONTROL_MODE:o}) left the file at 0o{observed.value.strip()}"
    )
    assert observed.value.strip() != f"{_MODE:o}", (
        f"{cell}: the read-back answered 0o{_MODE:o} for a put that asked for "
        f"0o{_CONTROL_MODE:o}, so the contract's mode assertion is true of this cell "
        f"whatever mode was requested"
    )
