"""What every host owes a caller watching the progress bar: events that move with the bytes.

THE PAYLOAD IS SIZED FROM THE BACKEND'S OWN PROMISE. Each backend declares a
:class:`~otto.host.transfer.base.ProgressGranularity` (re-exported from
``otto.host.transfer``, which is where callers import it):
the most ``bytes_done`` may advance between two events, per direction, or
``None`` for one event at completion. This contract moves ``3 * stride + 17``
bytes (four events at the declared stride, the last a partial) and requires the
stream to satisfy ``tests/_fixtures/progress.py``'s
:func:`~tests._fixtures.progress.assert_progress_invariants` -- the one
definition of a correct stream, shared with the unit tests. A backend that
silently regressed to one 0 -> 100% jump reds against its own declaration.

THE PROMISE IS READ FROM THE INSTANCE, NEVER FROM THE CLASS, and on one backend
that is the difference between a contract and a fabrication:
:class:`~otto.host.transfer.scp.ScpFileTransfer`'s stride is
``scp_options.block_size`` (and ``scp_options.extra["block_size"]`` behind it),
which lab data sets per host, so the class attribute is only the default. Only
:meth:`~otto.host.transfer.base.BaseFileTransfer.effective_progress_granularity`
answers for the host actually under test, and sizing a payload from the class
declaration would measure a stride asyncssh was never given.

WHAT THIS SURFACE PINS, AND WHAT IT DOES NOT. Every stride clause bounds the
OBSERVED step from above, so a declaration LARGER than the backend's real stride
satisfies all of them -- with a declared ``G' = 2g`` against a real stride ``g``
the payload is ``6g + 17``, the events arrive every ``g``, and every clause
passes. Sizing the payload from the declaration does not rescue that, and
nothing in this plan does; the design's §4 states the limitation and names the
two ways out (make each loop CONSUME its declaration, as sftp and scp now do,
or add a lower-bound clause). What this surface DOES pin, and no unit fake
can, is the declaration against a REAL transfer: a backend that emits one event
at completion while declaring a stride, or one whose events run ahead of the
bytes, or one whose bar never reaches the total, is red here on the very device
a user would watch it on.

WHICH BACKENDS THE DEFAULT GATE REACHES, MEASURED 2026-08-26 rather than
assumed -- because "a real transfer" is not the same claim in both venues. The
hermetic venue draws EIGHT cells (``venue=hermetic space=8 cells drawn=8``):
``local[local:local:local]``, ``loopback-ssh[loopback:ssh:sftp]``,
``loopback-ssh[loopback:ssh:scp]`` and five
``busybox-artifact[busybox-*:local:local]``. So on EVERY default gate ``sftp``
and ``scp`` are measured against real asyncssh streams over a throwaway
``sshd`` (``tests/conformance/_cells.py``'s ``_loopback_opener``), and the other
six cells are all :class:`~otto.host.local_host.LocalFileTransfer` --
``_busybox_cells`` builds a ``LocalHost`` with the artifact's applets on
``PATH``, so a BusyBox row here says nothing about a BusyBox transfer backend.
``shell`` and ``nc`` are declared only by lab hosts and are measured on the BED
alone, which is why a mutation aimed at ``shell`` reds nothing hermetically and
the equivalent hermetic observation has to be taken on ``sftp`` or ``scp``.

THE SPY REPLACES THE RICH FACTORY, NOT THE BACKEND.
:class:`~otto.host.transfer.BaseFileTransfer` reads
``make_rich_progress_factory`` lazily from :mod:`otto.host.transfer.progress`;
patching it there hands the backend a handler that records instead of
rendering, on exactly the code path a user's bar takes -- the same seam
``tests/integration/host/test_host_contract.py`` uses. Nothing about the
transfer itself is faked, which is why this belongs in a venue with real hosts
rather than beside the unit fakes.

Domain, scratch directory, worker namespacing and cleanup are the transfer
contract's (``test_transfer_contract.py``), for the same reasons it gives at
length: the remote directory comes from the venue, the basename is namespaced
by xdist worker because the bed's scratch is a FIXED path on a SHARED guest,
and every landed file is removed and the removal verified.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

import otto.host.transfer.progress as progress_mod
from otto.host.host import BaseHost
from otto.host.transfer import ProgressGranularity
from tests._fixtures.profiles import Cell
from tests._fixtures.progress import (
    ProgressEvent,
    assert_progress_invariants,
    capture_progress,
)
from tests.conformance._bed import BED_BUSYBOX
from tests.conformance._controls import assert_bed_left_clean, remove_landed
from tests.conformance._resolved import ResolvedCell
from tests.conformance._transfer import transfer_backend_of
from tests.conftest import remote_name

pytestmark = [pytest.mark.asyncio, pytest.mark.conformance]

# Short basenames, for the bed's seven Zephyr guests: they declare
# `max_filename_len: 32` and `BaseFileTransfer.put_files` REFUSES an over-long
# one up front. `master_prog-put.bin` is 19 characters, the longest these can
# produce (an xdist worker id is `gwN`, shorter than `master`).
_PUT_NAME = "prog-put.bin"
_GET_NAME = "prog-get.bin"
_CONTROL_NAME = "prog-ctl.bin"

# The partial last event. `3 * stride + _TAIL` is four events -- three whole
# strides and a remainder -- so the stream has intermediate events to check
# steps between AND a final one that advances less than a stride, which is the
# case a payload of exactly `n * stride` would never produce.
_TAIL = 17

# The payload for a `None` (whole-file) promise. Any positive size does; 64
# bytes keeps a console transfer's single `fs write` cheap on the bed.
_WHOLE_FILE_BYTES = 64


def applicable_cell(resolved: ResolvedCell) -> bool:
    """Cells with somewhere to put a file: ``remote_scratch`` is not None.

    Otto's answer, not this suite's: ``remote_scratch`` is ``None`` for exactly
    the hosts whose filesystem reports ``supports_transfer`` False -- the flag
    :class:`~otto.host.transfer.console.ConsoleFileTransfer` short-circuits both
    ``_run_put`` and ``_run_get`` on. A device with no transfer has no transfer
    whose progress could be watched, which is a property of the device rather
    than a defect. See ``test_transfer_contract.applicable_cell`` for the full
    account of why this is a declared domain and not a skip.
    """
    return resolved.remote_scratch is not None


# ==========================================================================
# A REGISTERED, MEASURED-BROKEN CELL -- bb1350's ssh `shell` at full stride
# ==========================================================================
# The whole decision lives here, under its own banner, so that reversing it is
# a single-file change (the convention `tests/conformance/conftest.py`'s
# `_XFAIL_HOOK` docstring asks for).
#
# MEASURED 2026-09-24, on the real guest. dropbear 2012.55 (`bb1350`'s sshd)
# caps any SSH string at `MAX_STRING_LEN` (`sysoptions.h:118` -- 1400 bytes)
# and exits the connection rather than truncating
# (`buffer.c:211` -- ``dropbear_exit("String too long")``). An SSH `exec`
# request carries the whole command as ONE string, and `ShellFileTransfer`
# puts a full chunk on one command line: `_SHELL_CHUNK_BYTES = 4096`
# plaintext becomes a ~5.5 KB base64 line, four times the cap. Only this
# contract's payload forces a full-size chunk (`_size_for` sizes it from the
# backend's declared stride); the exec, transfer and timeout contracts on the
# same cell move far less and pass.
#
# KEYED ON THE RESOLVER'S ANSWER, not on a guest's name -- `resolved.kind` is
# what `tests/conformance/_bed.py`'s `bed_kind` derives from the host's
# declared userland, and `resolved.cell.term`/`.transfer` are the pair the
# venue crossed this cell for. A future BusyBox guest running a newer dropbear
# over ssh would XPASS and RED THE LANE until someone narrows this predicate
# -- that is the strictness working as designed, not a defect in the
# predicate.
#
# MUST ANSWER None FOR EVERY HERMETIC CELL, and does, by construction:
# `tests/unit/test_conformance_bed.py`'s
# `test_no_hermetic_cell_is_declared_a_known_failure` sweeps every contract
# module's hook against the hermetic space, and no hermetic cell is ever kind
# `bed-busybox` (the hermetic venue's BusyBox rows are `busybox-artifact`,
# built as local subprocesses with no sshd at all), so the `kind` check alone
# already excludes the whole hermetic space -- this predicate cannot reach a
# hermetic cell no matter what it checks after that.
#
# WHY IT IS NOT FIXED HERE. `SessionManager.exec_line_budget` answers `None`
# for the bare ssh exec route by design (`session.py:2981-3006`), so
# `ShellFileTransfer._fitted_chunk_bytes` has no budget to fit the chunk to;
# only the pooled-shell (telnet/pty) route reports one. Giving the exec route
# a per-host budget is a product change that needs a new host field, which
# needs its own spec (repo rule) -- out of this branch's spec scope. The
# follow-up issue the lead files is that spec's trigger: #437.
_DROPBEAR_SHELL_AT_FULL_STRIDE = (
    "shell put at full stride over the ssh exec channel: dropbear 2012.55 caps any SSH "
    "string at MAX_STRING_LEN 1400 (sysoptions.h:118, buffer.c:211 'String too long') and "
    "exits the connection, while a full 4096-byte chunk command is ~5.5 KB on one line; "
    "measured 2026-09-24 on bb1350 (guest journal: 'Exit (root): String too long' at each "
    "failure). exec, transfer and timeout contracts pass on this cell (the transfer payload "
    "is 78 bytes, one short command). otto's SessionManager.exec_line_budget answers None "
    "for the bare exec route by design, so ShellFileTransfer has no budget to fit the chunk "
    "to; a per-host exec line budget is the follow-up, #437."
)


def expected_failure(resolved: ResolvedCell) -> "str | None":
    """The reason this cell's progress contract is KNOWN to fail, or None.

    Read by ``tests/conformance/conftest.py``'s ``pytest_generate_tests``, which
    turns a reason into ``xfail(strict=True)`` on that cell's items. Exactly one
    cell today: ``bed-busybox[bb1350:ssh:shell]``. See the banner above this
    function for the measurement and why it is not fixed here.
    """
    if (
        resolved.kind == BED_BUSYBOX
        and resolved.cell.term == "ssh"
        and resolved.cell.transfer == "shell"
    ):
        return _DROPBEAR_SHELL_AT_FULL_STRIDE
    return None


# ==========================================================================


def _payload(size: int) -> bytes:
    """*size* bytes of binary-hostile filler: every byte value, NULs and CRLFs.

    Not ``b"x" * size``. A backend that normalises line endings, stops at the
    first NUL or decodes as UTF-8 moves an ASCII payload perfectly and its
    progress events would be measured against a transfer that never exercised
    the framing this repo has had three real defects in.
    """
    pattern = bytes(range(256)) + b"\r\n\x00\xff\xfe"
    return (pattern * (size // len(pattern) + 1))[:size]


def _size_for(stride: "int | None") -> int:
    """The payload size that produces four events at *stride*, or one at ``None``."""
    return _WHOLE_FILE_BYTES if stride is None else 3 * stride + _TAIL


def _reported_src(events: "list[ProgressEvent]", moved: Path, cell: Cell, arm: str) -> str:
    """The spelling THIS backend uses for *moved* in its progress events.

    MEASURED 2026-08-26 on the hermetic venue, and the difference is asyncssh's
    rather than otto's: ``scp``'s GET reports the BASENAME, because the SCP
    protocol's ``C <mode> <size> <name>`` control line is the only place the
    receiving side learns a name from (``asyncssh/scp.py``'s
    ``_SCPSink._recv_file`` passes that ``srcpath`` straight to the progress
    handler), while ``sftp``, ``scp``'s PUT and the local copy all report the
    full path. Both identify the same file and a reader cannot tell them apart:
    ``make_rich_progress_handler`` renders ``Path(src).name`` and switches task
    on a CHANGE of ``src``, so the bar is identical either way.

    SO THE BASENAME IS TOLERATED ONLY WHERE THAT QUIRK LIVES: ``scp``'s GET arm
    and nowhere else. Every other cell and arm is held to the full path, because
    a backend that SHOULD report it (``sftp``, ``scp``'s PUT, the local copy)
    and regressed to a bare basename must red rather than land inside a blanket
    tolerance.

    THIS HELPER IS WHERE THE NAMING CHECK NOW LIVES, and saying otherwise would
    overstate what the shared invariant still does here. By returning the ONE
    name the stream used, it leaves
    :func:`~tests._fixtures.progress.assert_progress_invariants`'s clause 2 (a
    stranger ``src``) nothing left to catch: clause 2 cannot fire from this
    module. What moved is STRONGER in one dimension than clause 2 -- exactly one
    name, so a second file's events leaking into this arm red here rather than
    being tolerated as long as they matched -- and, before the scoping above,
    was weaker in another. Clause 2 still guards its other callers, the unit
    fakes among them.
    """
    # asyncssh's SCP sink is the whole of the exemption; see above.
    acceptable = {str(moved)}
    if cell.transfer == "scp" and arm == "get":
        acceptable.add(moved.name)
    named = sorted({event.src for event in events})
    if not named:
        # A backend that emitted NOTHING is clause 1's failure, not this
        # helper's, and clause 1 says it far better ("no progress events for
        # ..."). Hand the caller the path so that refusal is the one a reader
        # sees.
        return str(moved)
    assert len(named) == 1, (
        f"{cell}: the {arm} progress stream names {named} -- one arm's events must "
        f"identify ONE file, and a second name is another file's stream leaking in"
    )
    assert named[0] in acceptable, (
        f"{cell}: the {arm} progress stream names {named[0]!r}, but this cell's {arm} arm "
        f"must name {' or '.join(repr(name) for name in sorted(acceptable))} -- the bare "
        f"basename is tolerated only on scp's GET arm, where asyncssh's `_SCPSink` forwards "
        f"the C-line name, and a stream that does not identify the file under test cannot be "
        f"measured against it"
    )
    return named[0]


def _promise_of(host: BaseHost, cell: Cell) -> ProgressGranularity:
    """The granularity THIS host's backend instance promises, or a named failure.

    THE INSTANCE, NEVER THE CLASS, which is this module's opening paragraph and
    the reason this helper exists at all rather than a ``type(host)`` lookup:
    ``ScpFileTransfer``'s stride is configured per host, so
    ``effective_progress_granularity()`` on the object the host actually built
    is the only answer that describes the transfer under test.

    How that object is reached, and why a host it cannot read is a named
    failure rather than a lenient default -- here, a payload sized from a guess
    -- is :func:`tests.conformance._transfer.transfer_backend_of`, shared with
    ``test_transfer_contract``, which asks this same object whether it carries a
    mode. The refusal below is this surface's own half of that failure.
    """
    backend = transfer_backend_of(
        host,
        cell,
        refusal_tail="this cell's backend cannot be asked what it promises the progress bar",
    )
    return backend.effective_progress_granularity()


@pytest.mark.observable(
    "the progress events `{cell.transfer}` emits while put() and then get() move a payload "
    "sized from the backend's own declared stride"
)
async def test_progress_events_track_the_bytes_in_both_directions(
    resolved_cell: ResolvedCell, remote_scratch: Path, tmp_path: Path, worker_id: str
) -> None:
    """One verdict, both directions, each sized from its own arm of the promise.

    PUT and GET are separate promises on every backend that has two loops, and
    a contract that measured only one would publish a verdict for a direction
    it never watched. They share a cell verdict the way ``transfer-roundtrip``
    already does, because a bar that moves in one direction and not the other
    is one broken progress contract rather than half of one.

    THE GET ARM'S FILE IS STAGED WITH ``show_progress=False``, which is not
    tidiness: a staging put under the spy would leave its own stream in the
    list, and the invariant helper REFUSES a foreign ``src`` rather than
    filtering it out, so the GET assertion would fail naming the staging file.
    ``events.clear()`` behind it is the belt to that brace.
    """
    cell = resolved_cell.cell
    words = resolved_cell.vocabulary
    source_dir = tmp_path / "source"
    retrieved_dir = tmp_path / "retrieved"
    for directory in (source_dir, retrieved_dir):
        directory.mkdir()

    async with resolved_cell.open_host() as host:
        promise = _promise_of(host, cell)
        put_src = source_dir / remote_name(worker_id, _PUT_NAME)
        put_src.write_bytes(_payload(_size_for(promise.put)))
        get_src = source_dir / remote_name(worker_id, _GET_NAME)
        get_src.write_bytes(_payload(_size_for(promise.get)))
        put_landed = remote_scratch / put_src.name
        get_landed = remote_scratch / get_src.name

        events, spy = capture_progress()
        try:
            with patch.object(progress_mod, "make_rich_progress_factory", new=spy):
                put = await host.put(put_src, remote_scratch)
            assert put.is_ok, f"{cell}: put reported {put.status!r} -- {put.msg!r}"
            assert_progress_invariants(
                events,
                src=_reported_src(events, put_src, cell, "put"),
                total=put_src.stat().st_size,
                granularity=promise.put,
            )

            staged = await host.put(get_src, remote_scratch, show_progress=False)
            assert staged.is_ok, f"{cell}: staging put reported {staged.status!r} -- {staged.msg!r}"
            events.clear()
            with patch.object(progress_mod, "make_rich_progress_factory", new=spy):
                got = await host.get(get_landed, retrieved_dir)
            assert got.is_ok, f"{cell}: get reported {got.status!r} -- {got.msg!r}"
            assert_progress_invariants(
                events,
                src=_reported_src(events, get_landed, cell, "get"),
                total=get_src.stat().st_size,
                granularity=promise.get,
            )
        finally:
            # Never raises, and runs whatever failed above -- see
            # `tests/conformance/_controls.remove_landed`. Both files are
            # removed even if only the first one landed.
            removed_put = await remove_landed(host, words, put_landed)
            removed_get = await remove_landed(host, words, get_landed)
    assert_bed_left_clean(removed_put, put_landed, cell)
    assert_bed_left_clean(removed_get, get_landed, cell)


# ==========================================================================
# POSITIVE CONTROL -- proof that this cell's OWN stream could have been refused
# ==========================================================================
# See `tests/conformance/_controls.py` for why these exist. It inherits this
# module's `applicable_cell`, which is right: a cell where the contract is not
# asserted needs no evidence that it could go red.


@pytest.mark.positive_control("transfer-progress")
async def test_control_the_instrument_refuses_a_bar_that_jumps(
    resolved_cell: ResolvedCell, remote_scratch: Path, tmp_path: Path, worker_id: str
) -> None:
    """Run the PUT arm for real, then require the instrument to refuse two mutations of ITS stream.

    A control that reds on synthetic input proves the helper in the abstract;
    this one proves the helper reds on THIS CELL'S OWN DATA, which is the
    property that matters -- ``assert_progress_invariants`` is satisfied by any
    stream a backend hands it, and a cell whose backend emits one completion
    event would pass a ``None`` promise's clauses however the transfer behaved.

    TWO MUTATIONS, EACH THE BUG THIS SURFACE EXISTS FOR:

    * the final event dropped -- the bar never finishes (clause 6 on a stride
      promise; on a whole-file promise there is only one event, so dropping it
      leaves nothing and the refusal is clause 1's);
    * the stream collapsed to one ``0 -> total`` event -- the bar starts at
      100% (clause 8a). A whole-file promise cannot express that, so its
      mutation is the converse it DOES forbid: a spurious intermediate event
      ahead of the completion (clause 7).

    EACH REFUSAL IS CHECKED BY THE CLAUSE IT NAMES, not merely by
    ``AssertionError``. A helper that raised on every input would satisfy a
    bare ``pytest.raises`` twice over and prove nothing about which invariant
    caught what.

    The mutated streams are BUILT, never edited: ``ProgressEvent`` is frozen,
    and a control that mutated the captured list in place would be asserting
    against a stream the cell never produced while the contract's own copy
    changed under it.
    """
    cell = resolved_cell.cell
    words = resolved_cell.vocabulary
    source_dir = tmp_path / "source"
    source_dir.mkdir()

    async with resolved_cell.open_host() as host:
        promise = _promise_of(host, cell)
        src = source_dir / remote_name(worker_id, _CONTROL_NAME)
        src.write_bytes(_payload(_size_for(promise.put)))
        landed = remote_scratch / src.name
        events, spy = capture_progress()
        try:
            with patch.object(progress_mod, "make_rich_progress_factory", new=spy):
                put = await host.put(src, remote_scratch)
            assert put.is_ok, f"{cell}: put reported {put.status!r} -- {put.msg!r}"
        finally:
            removed = await remove_landed(host, words, landed)
    assert_bed_left_clean(removed, landed, cell)

    total = src.stat().st_size
    named = _reported_src(events, src, cell, "put")
    # The real stream passes: without this the two refusals below could both
    # come from a stream that was broken to begin with.
    assert_progress_invariants(events, src=named, total=total, granularity=promise.put)

    truncated = events[:-1]
    unfinished = "no progress events" if promise.put is None else "the bar never finishes"
    with pytest.raises(AssertionError) as dropped:
        assert_progress_invariants(truncated, src=named, total=total, granularity=promise.put)
    assert unfinished in str(dropped.value), (
        f"{cell}: dropping the final event of this cell's own stream was refused as "
        f"{str(dropped.value)!r}, which is not the unfinished-bar clause"
    )

    first = events[0]
    if promise.put is None:
        # A whole-file promise forbids any intermediate event at all.
        jumped = [
            ProgressEvent(src=first.src, dst=first.dst, done=total // 2, total=total),
            first,
        ]
        began_past_a_stride = "promised ONE event"
    else:
        # A stride promise forbids beginning past one stride: one 0 -> total jump.
        jumped = [ProgressEvent(src=first.src, dst=first.dst, done=total, total=total)]
        began_past_a_stride = "the bar cannot begin past one stride"
    with pytest.raises(AssertionError) as jump:
        assert_progress_invariants(jumped, src=named, total=total, granularity=promise.put)
    assert began_past_a_stride in str(jump.value), (
        f"{cell}: a bar that jumps was refused as {str(jump.value)!r}, which is not "
        f"the clause this cell's promise breaks"
    )
