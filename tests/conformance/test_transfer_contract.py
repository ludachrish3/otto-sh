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

AND NOWHERE THAT IT IS DECLARED NOT TO HOLD -- which is new, and is the
whole point of the note where the declaration used to be, below
:func:`applicable_cell`. Every drawn cell this module is about now asserts
the contract outright.
"""

from collections.abc import Callable
from pathlib import Path

import pytest

from otto.host.host import BaseHost
from otto.host.transfer import BaseFileTransfer
from tests._fixtures.profiles import Cell
from tests.conformance._controls import assert_bed_left_clean, remove_landed
from tests.conformance._resolved import ResolvedCell
from tests.conformance._transfer import transfer_backend_of
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

# The recursive surface's control puts a TREE of its own (and, on a host that
# refuses recursion, a single file of its own), for the same reason and under
# the same 32-character limit: `master_ctree` is 12 and `master_ctree.bin` is
# 16. Distinct from the contract's `tree`, so the control's cleanup can never
# remove a tree another worker's contract is mid-roundtrip on.
_RECURSIVE_CONTROL_NAME = "ctree"
_RECURSIVE_CONTROL_FILE = "ctree.bin"

# The batch surface's basenames, under the same 32-character limit and built
# from an INDEX so the files of one batch are told apart by name as well as by
# bytes. `master_batch-par-00.bin` is 23 and `master_ctl-00.bin` is 17, and
# both stay inside the limit for any index a cap-sized batch can reach. The
# contract's and the control's prefixes differ, so neither one's cleanup can
# remove a file the other -- or another worker -- is mid-roundtrip on.
_BATCH_NAME = "batch-{tag}-{index:02d}.bin"
_BATCH_CONTROL_NAME = "ctl-{index:02d}.bin"

# The corrupted payload: `_PAYLOAD` with ONE byte changed, and nothing else.
# A wholly different payload would also fail the comparison, and would prove
# less -- the question is whether the roundtrip is byte-sensitive, not whether
# it is length-sensitive.
_CORRUPT_AT = 0
_CORRUPTED_PAYLOAD = bytes([_PAYLOAD[0] ^ 0x01]) + _PAYLOAD[1:]


def _indexed_payload(index: int) -> bytes:
    """The batch contract's file *index*: its position, then the tripwire bytes.

    A COUNT IS NOT AN IDENTITY, and this is what makes the difference visible.
    A backend that landed one file's bytes under every name of a batch satisfies
    "every file came back" and is caught only by reading each file, so every
    file of a batch carries its own position as a two-byte prefix -- distinct
    for any batch a cap can produce -- ahead of the payload whose tripwires the
    rest of this module is about.
    """
    return index.to_bytes(2, "big") + _PAYLOAD


def _rotated_payload(index: int) -> bytes:
    """The batch control's file *index*: its position, then a rotation by it.

    THE SAME TWO-BYTE PREFIX :func:`_indexed_payload` CARRIES, and for the same
    reason: a rotation alone repeats every 256 positions, so two files of a
    large enough batch would carry identical bytes and a swap between them would
    satisfy the per-name comparison this control exists to drive. With the
    prefix every position is distinct for any batch a cap can produce.

    THE ROTATION IS THE OTHER HALF. Past the prefix these bytes are never the
    contract's ``_PAYLOAD`` -- a rotation of all 256 values is a different
    SEQUENCE at every position, though it is of course built from the same byte
    values -- so no file this control sends equals any file the contract sends,
    and a host that answers the contract's payload however it came by it fails
    here instead of passing.
    """
    start = index % 256
    return index.to_bytes(2, "big") + bytes(range(start, 256)) + bytes(range(start))


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
# NO CELL IS DECLARED A KNOWN FAILURE HERE, and one was
# ==========================================================================
# From 2026-08-21 to 2026-08-25 this module declared an `expected_failure`
# hook that marked the five `bed-busybox[*:telnet:nc]` cells `xfail(strict=True)`
# -- twenty items once the positive controls landed beside the contracts --
# against otto's registered `nc-transfer` gap: the PUT listener was spelled
# `nc -l -w <secs> <port>`, an OpenBSD-ism the applet does not parse -- it
# reads the lone port as a HOST and dies with `bad address` (1.16.1) or binds
# an ephemeral port in silence (1.35.0), and otto saw both as
# `Remote nc listener on port <port> not ready`. The universal `nc -l -p PORT`
# spelling closed that gap and the declaration was repaid in the same change,
# so those cells assert this contract outright now, like every other drawn
# cell. See
# `docs/superpowers/specs/2026-08-25-nc-universal-spelling-design.md`.
#
# The conftest MECHANISM that read the hook is untouched and still waiting for
# the next declaration (`tests/conformance/conftest.py`'s `_XFAIL_HOOK`, pinned
# by `tests/unit/test_conformance_bed.py`), so re-declaring is one function
# here and nothing else.
#
# WHAT THE CROSSING IS STILL FOR, which outlived the declaration: a contract
# that holds over `shell` and breaks over `nc` ON THE SAME HOST is invisible to
# any per-host suite, and `tests/integration/host/` parametrizes one transport
# per backend. The (term, transfer) crossing is the only place that difference
# can be seen -- and it is the place the fix above is measured.
# ==========================================================================


def _transfer_backend(host: BaseHost, cell: Cell) -> BaseFileTransfer:
    """The host's transfer backend, or a loud failure naming the cell.

    WHY THIS SURFACE NEEDS THE OBJECT AND NOT THE TRANSFER NAME:
    ``supports_mode`` is what decides which observable a cell offers here --
    both the mode contract and its control BRANCH on it, and the marker's
    template cannot express that branch -- and only the backend the host
    actually built can answer it.

    How it is reached, and why a host that cannot be read is a named failure
    rather than a lenient default, is
    :func:`tests.conformance._transfer.transfer_backend_of`, shared with
    ``test_progress_contract``, which asks this same object a different
    question. The refusal below is this surface's own half of that failure.
    """
    return transfer_backend_of(
        host,
        cell,
        refusal_tail="this cell's transfer backend cannot be asked whether it carries a mode",
    )


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
    "what a small tree put and got back over `{cell.transfer}` leaves behind -- which of "
    "the two observables that is depends on whether otto walks a tree for this host at all"
)
async def test_put_get_roundtrip_recursive_tree(
    resolved_cell: ResolvedCell,
    remote_scratch: Path,
    tmp_path: Path,
    worker_id: str,
    note_observable: "Callable[[str], None]",
) -> None:
    """``put -r`` then ``get -r``: a tree with nesting, an empty directory and
    the tripwire payload, on every cell the transfer contract is about. An
    embedded cell refuses rather than degrades -- that refusal IS its contract.

    THE SAME TWO-OBSERVABLE SHAPE THE MODE CONTRACT HAS, and for the same
    reason: which of them a cell offers is decided at run time by otto's own
    answer -- there is a recursive walk for this host or there is not -- and the
    marker's template cannot express it. A cell that watched the refusal must
    never be published as a device you can put a tree on, so the branch is
    RECORDED (``tests/conformance/_observable.py``) rather than aggregated
    away.

    ★ THE REFUSING ARM BELOW HAS NEVER RUN, and that gap is DECLARED rather
    than closed. It is selected by ``isinstance(host, EmbeddedHost)``, and the
    hermetic venue builds no such host -- ``tests/unit/test_support_matrix.py``'s
    ``_FakeHost`` cannot become one either -- so the first execution of that arm
    is the maintainer's bed run against a Zephyr guest with a filesystem.
    **The mode contract does NOT share this gap**, and the difference is the
    kind of question each asks: its arms turn on ``supports_mode``, a
    CAPABILITY ANSWER any object can give, so a fake backend drives its refusal
    hermetically and its positive control is proved both ways without a device.
    Otto exposes no such answer for recursion -- there is no
    ``supports_recursive_transfer`` to ask -- so this branch reads the host's
    TYPE, which nothing hermetic can be. Closing it means giving the product a
    fakeable capability answer, which is a change to ``src/`` and not to this
    suite; until then the arm's prose and its declared
    :data:`~scripts.render_support_matrix.VOICE` arm are reviewed rather than
    executed, and they say only what the assertion below asserts.
    """
    from otto.host.embedded_host import EmbeddedHost

    name = remote_name(worker_id, "tree")
    tree = tmp_path / "source" / name
    (tree / "sub").mkdir(parents=True)
    (tree / "empty").mkdir()
    (tree / "a.bin").write_bytes(_PAYLOAD)
    (tree / "sub" / "b.bin").write_bytes(bytes(range(256)))
    retrieved_dir = tmp_path / "retrieved"
    retrieved_dir.mkdir()

    cell = resolved_cell.cell
    async with resolved_cell.open_host() as host:
        # THE BRANCH THE MARKER'S TEMPLATE CANNOT EXPRESS, decided here by
        # whether otto implements a recursive walk for this host kind -- the
        # same shape as the mode contract's branch on `supports_mode`, but read
        # off the host's TYPE, for the reason the docstring declares.
        #
        # SAYS ONLY WHAT THE ARM ASSERTS. The assertion below is a `raises`, so
        # the observable names the raise and stops: whether anything transferred
        # first, and whether the tree was walked at all, are claims this arm
        # never makes and an observable is read as evidence.
        note_observable(
            f"the NotImplementedError {type(host).__name__} raises for recursive=True"
            if isinstance(host, EmbeddedHost)
            else f"the bytes, nesting and empty directory get(recursive=True) reads back "
            f"over `{cell.transfer}` after put(recursive=True) of a small tree into this "
            f"host's scratch directory",
        )
        if isinstance(host, EmbeddedHost):
            with pytest.raises(NotImplementedError, match="recursive=True"):
                await host.put(tree, remote_scratch, recursive=True)
            return
        try:
            put = await host.put(tree, remote_scratch, recursive=True)
            assert put.is_ok, f"{cell}: put -r reported {put.status!r} -- {put.msg!r}"
            got = await host.get(remote_scratch / name, retrieved_dir, recursive=True)
            assert got.is_ok, f"{cell}: get -r reported {got.status!r} -- {got.msg!r}"
        finally:
            # A bed cell's `remote_scratch` is a real device path that outlives
            # this test (unlike the hermetic venue's `tmp_path`-backed one), and
            # `remote_name` is stable across runs -- so a stale tree left behind
            # by a prior run could satisfy the empty-directory assertion below
            # even if `put -r` regressed. Best-effort and unasserted, matching
            # the integration copy: a cleanup failure here must not mask a real
            # put/get failure already on its way out of this block.
            await host.rm(remote_scratch / name, recursive=True, force=True)

    back = retrieved_dir / name
    assert (back / "a.bin").read_bytes() == _PAYLOAD, f"{cell}: a.bin is not the payload"
    assert (back / "sub" / "b.bin").read_bytes() == bytes(range(256)), f"{cell}: sub/b.bin differs"
    assert (back / "empty").is_dir(), f"{cell}: the empty directory did not round-trip"


@pytest.mark.observable(
    "the bytes of every file in a batch larger than the transfer's concurrency cap, read back "
    "over `{cell.transfer}` after put() of the batch, once concurrent and once one at a time"
)
async def test_put_get_batch_lands_every_file_in_both_modes(
    resolved_cell: ResolvedCell, remote_scratch: Path, tmp_path: Path, worker_id: str
) -> None:
    """Every file of a batch past the cap lands and reads back, in BOTH modes.

    THE BATCH IS SIZED FROM THE BACKEND'S OWN CAP -- ``concurrency_limit + 3``
    -- rather than from a number written here, and that is what makes it a
    batch past the cap on every cell rather than on the ones whose cap happens
    to be small. A bounded fan-out must then QUEUE at least three files rather
    than refuse them, and a cap-of-one backend (shell, console, ftp, the local
    copy, where ``concurrent=True`` is a documented no-op) is still driven on
    more than one file, which is the case a one-file roundtrip never reaches.

    BOTH MODES, ONE CONTRACT, because they are two promises rather than two
    implementations of one. ``concurrent=False`` is what a caller reaches for
    when the far side must see one file at a time, and a backend that fanned
    out anyway -- or that dropped the tail of the batch when told not to --
    would be watched by nothing if only the parallel arm ran.

    EVERY FILE CARRIES ITS OWN BYTES (:func:`_indexed_payload`), so the
    read-back is per NAME and not per count: a backend that landed the first
    file's bytes under every name of the batch passes "the right number of
    files came back" and fails here. That claim is driven from the other side
    by the control beside this, which sends a batch these very comparisons
    must refuse.

    THE AGGREGATES ARE ASSERTED BEFORE THE BYTES, for the reason the roundtrip
    contract gives: a transfer can land the right bytes and still report
    failure, and can report success having written nothing. ``put.value`` is
    additionally required to be keyed by the very sources handed in -- that
    mapping is how a caller of a batch finds WHICH file failed, and a backend
    that answered a bare aggregate would leave them nothing to act on.
    """
    cell = resolved_cell.cell
    words = resolved_cell.vocabulary
    async with resolved_cell.open_host() as host:
        backend = transfer_backend_of(
            host,
            cell,
            refusal_tail=(
                "this cell's transfer backend cannot be asked how many files it may have "
                "in flight, and the batch this contract sends is sized from that answer"
            ),
        )
        count = backend.concurrency_limit + 3
        for concurrent in (True, False):
            tag = "par" if concurrent else "seq"
            source_dir = tmp_path / f"source-{tag}"
            retrieved_dir = tmp_path / f"retrieved-{tag}"
            source_dir.mkdir()
            retrieved_dir.mkdir()
            sources = []
            for i in range(count):
                src = source_dir / remote_name(worker_id, _BATCH_NAME.format(tag=tag, index=i))
                src.write_bytes(_indexed_payload(i))
                sources.append(src)
            landed = [remote_scratch / s.name for s in sources]
            try:
                put = await host.put(sources, remote_scratch, concurrent=concurrent)
                assert put.is_ok, (
                    f"{cell}: put(concurrent={concurrent}) of {count} files reported "
                    f"{put.status!r} -- {put.msg!r}"
                )
                assert set(put.value) == set(sources), (
                    f"{cell}: put(concurrent={concurrent}) keyed {sorted(put.value)} rather "
                    f"than the {count} sources it was handed, so a caller cannot tell which "
                    f"file of the batch a failure belongs to"
                )
                got = await host.get(landed, retrieved_dir, concurrent=concurrent)
                assert got.is_ok, (
                    f"{cell}: get(concurrent={concurrent}) of {count} files reported "
                    f"{got.status!r} -- {got.msg!r}"
                )
            finally:
                # Best-effort and UNASSERTED, the rule the recursive contract's
                # own cleanup states: an assertion raised from a `finally`
                # replaces the failure already on its way out, and would report
                # a cleanup problem for a cell whose real defect was that half
                # the batch never landed. `remove_landed` never raises, and the
                # spelling is the cell's own vocabulary rather than `rm -f`,
                # which an embedded userland refuses.
                for path in landed:
                    await remove_landed(host, words, path)
            for i, src in enumerate(sources):
                back = retrieved_dir / src.name
                assert back.read_bytes() == _indexed_payload(i), (
                    f"{cell}: file {i} of the {tag} batch of {count} is not the bytes that "
                    f"were sent under that name"
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
# THEY INHERIT THIS MODULE'S ONE DECLARATION, and it is load-bearing.
# `applicable_cell` keeps them off the three bed guests with no filesystem,
# which is right: a cell where the contract is not asserted needs no evidence
# that it could go red.
#
# THERE WAS A SECOND until 2026-08-25 (see the note under `applicable_cell`),
# and what it did to the controls is worth recording because the next
# declaration will do the same: `expected_failure` marked the five
# `bed-busybox` `nc` cells xfail(strict=True), and because each control has to
# PUT a file before it can demonstrate anything, it took this module's
# expected-xfail count from the contracts' 10 items to 20. A control is only
# as applicable as the contract it is about -- `tests/conformance/_controls.py`.


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


@pytest.mark.positive_control("transfer-recursive")
async def test_control_the_tree_that_comes_back_is_the_tree_that_was_sent(
    resolved_cell: ResolvedCell, remote_scratch: Path, tmp_path: Path, worker_id: str
) -> None:
    """Round-trip a DIFFERENT tree; every instrument the contract uses must move with it.

    The contract's instruments are three claims about a tree whose shape and
    bytes this module wrote itself -- ``a.bin`` equals ``_PAYLOAD``,
    ``sub/b.bin`` equals ``bytes(range(256))``, and ``empty`` is a directory --
    and each of them is satisfied by a backend that produces that shape however
    it came by it: one whose ``get`` answers a cached or echoed copy of the
    local source rather than what is on the far side, and one that creates a
    directory for every name it is asked about. So this puts a tree those
    claims must REFUSE -- the payload corrupted at one byte, the nested file's
    bytes reversed, and NO empty directory at all -- and requires the reply to
    be the tree that was actually sent: different from the contract's, equal to
    this one, and carrying no directory nobody created.

    THE REFUSING ARM IS CONTROLLED TOO. Where the host refuses recursion the
    contract's instrument is ``pytest.raises(NotImplementedError)``, and that
    assertion is satisfied just as well by a host that refuses EVERYTHING --
    against which the contract would be watching nothing. So on such a host
    this puts a single file the same way the roundtrip contract does and
    requires it to land: the refusal is about recursion, not about transfer.
    That arm inherits the contract's declared gap -- it is selected by the same
    ``isinstance`` and so first executes on the bed; the harness in
    ``tests/unit/test_support_matrix.py`` drives this control both ways on the
    round-trip arm only.

    LEAVES THE BED AS FOUND, by the same route as the contract -- ``rm -r`` on
    the tree, best-effort and unasserted, and
    :func:`~tests.conformance._controls.remove_landed` plus
    :func:`~tests.conformance._controls.assert_bed_left_clean` on the single
    file, whose success also proves the file was there to delete.
    """
    from otto.host.embedded_host import EmbeddedHost

    cell = resolved_cell.cell
    words = resolved_cell.vocabulary
    nested = bytes(range(255, -1, -1))

    name = remote_name(worker_id, _RECURSIVE_CONTROL_NAME)
    tree = tmp_path / "source" / name
    (tree / "sub").mkdir(parents=True)
    (tree / "a.bin").write_bytes(_CORRUPTED_PAYLOAD)
    (tree / "sub" / "b.bin").write_bytes(nested)
    retrieved_dir = tmp_path / "retrieved"
    retrieved_dir.mkdir()

    async with resolved_cell.open_host() as host:
        if isinstance(host, EmbeddedHost):
            source_dir = tmp_path / "flat"
            source_dir.mkdir()
            source = source_dir / remote_name(worker_id, _RECURSIVE_CONTROL_FILE)
            source.write_bytes(_CORRUPTED_PAYLOAD)
            landed = remote_scratch / source.name
            try:
                put = await host.put(source, remote_scratch)
                assert put.is_ok, (
                    f"{cell}: this host refuses a recursive transfer, and it refused a "
                    f"single file too -- put reported {put.status!r}, {put.msg!r}. The "
                    f"contract's `NotImplementedError` here would say nothing about "
                    f"recursion"
                )
            finally:
                removed = await remove_landed(host, words, landed)
            assert_bed_left_clean(removed, landed, cell)
            return

        try:
            put = await host.put(tree, remote_scratch, recursive=True)
            assert put.is_ok, f"{cell}: put -r reported {put.status!r} -- {put.msg!r}"
            got = await host.get(remote_scratch / name, retrieved_dir, recursive=True)
            assert got.is_ok, f"{cell}: get -r reported {got.status!r} -- {got.msg!r}"
        finally:
            # Unasserted, for the reason the contract's own cleanup gives: an
            # assertion raised from a `finally` replaces the failure already on
            # its way out.
            await host.rm(remote_scratch / name, recursive=True, force=True)

    back = retrieved_dir / name
    landed_payload = (back / "a.bin").read_bytes()
    assert landed_payload != _PAYLOAD, (
        f"{cell}: a tree whose a.bin was corrupted at byte {_CORRUPT_AT} came back "
        f"carrying the contract's payload, so `a.bin == _PAYLOAD` is true of this cell "
        f"whatever was sent and the contract's green means nothing here"
    )
    assert landed_payload == _CORRUPTED_PAYLOAD, (
        f"{cell}: a.bin is neither the bytes sent nor the contract's -- sent "
        f"{len(_CORRUPTED_PAYLOAD)} bytes, got back {len(landed_payload)}"
    )
    # `strict=False`: a reply of the wrong LENGTH is a different failure and is
    # already reported by the equality above, so this must not raise first.
    differing = [
        i for i, (a, b) in enumerate(zip(landed_payload, _PAYLOAD, strict=False)) if a != b
    ]
    assert differing == [_CORRUPT_AT], (
        f"{cell}: the reply differs from the contract's payload at {differing}, not at "
        f"the single corrupted byte {_CORRUPT_AT} -- the comparison is reacting to "
        f"something other than the corruption"
    )
    assert (back / "sub" / "b.bin").read_bytes() == nested, (
        f"{cell}: the nested file did not come back reversed, so what the contract "
        f"reads at `sub/b.bin` does not track what was put there"
    )
    assert not (back / "empty").exists(), (
        f"{cell}: an `empty` directory nobody created is in the retrieved tree, so the "
        f"contract's empty-directory assertion is satisfied by this cell whether or not "
        f"an empty directory round-trips"
    )


@pytest.mark.positive_control("transfer-concurrent")
async def test_control_the_batch_that_comes_back_is_the_batch_that_was_sent(
    resolved_cell: ResolvedCell, remote_scratch: Path, tmp_path: Path, worker_id: str
) -> None:
    """Send a batch the contract's read-back must tell apart, and watch the fan-out's width.

    TWO CLAIMS THE CONTRACT LEANS ON, each given a way to fail here.

    (1) EVERY FILE COMES BACK UNDER ITS OWN NAME. The contract's instrument is
    a per-index comparison, and it is satisfied by any host that answers those
    bytes however it came by them -- a cache, an echo of the local sources, a
    get that never reached the far side. So this sends files
    (:func:`_rotated_payload`) that are indexed rotations of one byte
    permutation: no file of this batch is any file the contract sends, and no
    file of this batch is any other file of it. Then it requires the reply to
    match PER NAME -- a host that answers the contract's bytes fails, and so
    does one that answers some other file of this very batch under the wrong
    name.

    (2) THE FAN-OUT IS BOUNDED. The contract sends ``cap + 3`` files and reads
    nothing about how many moved at once, so it is equally green against a
    backend that ignored its cap entirely and against one that never fanned
    out at all. This wraps the backend's own per-file entry point and counts
    what is in flight INSIDE it -- the point where the semaphore has already
    been taken -- then refuses a run whose peak exceeded the cap, and, where
    the cap is above one, a run whose peak never reached two. The probe reads
    ``concurrency_limit`` off the backend rather than assuming a number, so a
    cap-of-one backend is proved SEQUENTIAL by the same instrument that proves
    a cap-of-four backend parallel.

    THE WRAPPER IS THE BACKEND'S, NOT THE HOST'S, and on a container cell that
    is the parent's staging backend -- the leg that actually fans out, which is
    the leg the cap is about.

    ★ THE ``limit > 1`` ARM IS NOT DRIVEN HERMETICALLY, and that gap is
    DECLARED rather than closed, the way the recursive contract's refusing arm
    declares its own. The harness in ``tests/unit/test_support_matrix.py``
    drives this control against a fake backend whose cap is one -- so it proves
    the bound and the per-name read-back, and the ``peak > 1`` assertion is
    inert there because it is inert on every cap-of-one backend by design. It
    first executes against a cell whose transfer really does fan out (scp,
    sftp, nc), which is a bed run or the hermetic loopback-ssh cells.

    LEAVES THE BED AS FOUND, by the route the other controls use: the
    per-cell vocabulary's removal for every file it landed
    (:func:`~tests.conformance._controls.remove_landed`), verified afterwards
    on the path where nothing else went wrong
    (:func:`~tests.conformance._controls.assert_bed_left_clean`), whose success
    also proves each file was there to delete.
    """
    cell = resolved_cell.cell
    words = resolved_cell.vocabulary
    in_flight = 0
    peak = 0
    async with resolved_cell.open_host() as host:
        backend = transfer_backend_of(
            host,
            cell,
            refusal_tail=(
                "this cell's transfer backend cannot be asked how many files it may have "
                "in flight, which is the very number this control is watching"
            ),
        )
        limit = backend.concurrency_limit
        original = backend._dispatch_per_file

        async def _counting_dispatch(src_files, transfer_one, *, concurrent):
            """The backend's own dispatcher, with a counter around each file."""

            async def _probe(src):
                nonlocal in_flight, peak
                # Counted INSIDE the per-file callable rather than around the
                # dispatcher call: the dispatcher takes its semaphore permit
                # around this very callable, so a count taken outside would
                # measure how many files were HANDED OVER (always the whole
                # batch) rather than how many are running.
                in_flight += 1
                peak = max(peak, in_flight)
                try:
                    return await transfer_one(src)
                finally:
                    in_flight -= 1

            return await original(src_files, _probe, concurrent=concurrent)

        backend._dispatch_per_file = _counting_dispatch
        count = limit + 3
        source_dir = tmp_path / "source"
        retrieved_dir = tmp_path / "retrieved"
        source_dir.mkdir()
        retrieved_dir.mkdir()
        sources = []
        for i in range(count):
            src = source_dir / remote_name(worker_id, _BATCH_CONTROL_NAME.format(index=i))
            src.write_bytes(_rotated_payload(i))
            sources.append(src)
        landed = [remote_scratch / s.name for s in sources]
        try:
            put = await host.put(sources, remote_scratch, concurrent=True)
            assert put.is_ok, f"{cell}: put reported {put.status!r} -- {put.msg!r}"
            got = await host.get(landed, retrieved_dir, concurrent=True)
            assert got.is_ok, f"{cell}: get reported {got.status!r} -- {got.msg!r}"
        finally:
            # The instance attribute goes first and the cleanup follows, both
            # before any assertion: deleting it restores the class's own
            # dispatcher, so nothing this control did outlives it even on the
            # failing path. Neither statement asserts, for the reason the other
            # controls' cleanups give.
            del backend._dispatch_per_file
            removed = [await remove_landed(host, words, path) for path in landed]
    for result, path in zip(removed, landed, strict=True):
        assert_bed_left_clean(result, path, cell)

    assert 1 <= peak <= limit, (
        f"{cell}: {peak} of {count} files were in flight at once against a cap of {limit} -- "
        f"a peak of 0 means the backend's per-file dispatcher never ran and this control "
        f"watched nothing, and a peak above the cap means the bound is not enforced"
    )
    if limit > 1:
        assert peak > 1, (
            f"{cell}: a cap of {limit} never had two files in flight, so the batch moved one "
            f"file at a time and the contract's `concurrent=True` arm is watching a "
            f"sequential transfer"
        )
    for i, src in enumerate(sources):
        assert (retrieved_dir / src.name).read_bytes() == _rotated_payload(i), (
            f"{cell}: the file landed as {src.name} came back as some other file's bytes, so "
            f"the contract's per-name read-back does not track what was sent under each name"
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
