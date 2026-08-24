"""What every host owes a caller of ``put()`` and ``get()``: the bytes, and the mode.

A roundtrip test written on ASCII text is not a contract. It passes unchanged
against a backend that rewrites line endings, that stops at the first NUL, or
that drops a trailing newline -- three real failure modes of shell- and
console-framed transfers, and none of them visible in ``"hello world"``. The
payload below carries all three tripwires plus bytes that are not valid UTF-8,
so a backend that round-trips it has round-tripped a file rather than a string.

THE REMOTE DIRECTORY IS A RUNNER PATH, and that is a property of the HERMETIC
venue rather than of these contracts: every cell this venue builds -- the
runner's own userland, a loopback ``sshd`` running as the same user, a
``LocalHost`` with BusyBox applets on ``PATH`` -- shares one filesystem, so
``tmp_path`` is reachable from both sides. The bed venue will need a remote
scratch directory instead; that is item 4's, and it changes where the paths
come from, not what is asserted about them.
"""

from pathlib import Path

import pytest

from otto.host.host import BaseHost
from otto.host.transfer import BaseFileTransfer
from tests._fixtures.profiles import Cell
from tests.conformance._cells import ResolvedCell

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


async def test_put_get_roundtrip_preserves_content(
    resolved_cell: ResolvedCell, tmp_path: Path
) -> None:
    """Byte-for-byte, including a trailing newline and a NUL.

    Three directories, not two. A roundtrip into the source directory would
    have the ``get`` overwrite the very file it is being compared against, so a
    backend that transferred nothing at all would still "round-trip"
    perfectly.

    The aggregate results are asserted before the bytes because they answer a
    different question: a transfer can land the right bytes and still report
    failure (and a caller acting on the report would then delete or retry), and
    it can report success having written nothing.
    """
    source_dir = tmp_path / "source"
    remote_dir = tmp_path / "remote"
    retrieved_dir = tmp_path / "retrieved"
    for directory in (source_dir, remote_dir, retrieved_dir):
        directory.mkdir()
    source = source_dir / "payload.bin"
    source.write_bytes(_PAYLOAD)

    cell = resolved_cell.cell
    async with resolved_cell.open_host() as host:
        put = await host.put(source, remote_dir)
        assert put.is_ok, f"{cell}: put reported {put.status!r} -- {put.msg!r}"
        got = await host.get(remote_dir / source.name, retrieved_dir)
        assert got.is_ok, f"{cell}: get reported {got.status!r} -- {got.msg!r}"

    retrieved = retrieved_dir / source.name
    assert retrieved.exists(), f"{cell}: get reported success but wrote no file at {retrieved}"
    assert retrieved.read_bytes() == _PAYLOAD, (
        f"{cell}: the roundtripped file is not the payload -- "
        f"sent {len(_PAYLOAD)} bytes, got back {len(retrieved.read_bytes())}"
    )


async def test_put_lands_the_documented_mode_on_the_host(
    resolved_cell: ResolvedCell, tmp_path: Path
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
    remote_dir = tmp_path / "remote"
    for directory in (source_dir, remote_dir):
        directory.mkdir()
    source = source_dir / "payload.bin"
    source.write_bytes(_PAYLOAD)
    landed = remote_dir / source.name

    cell = resolved_cell.cell
    async with resolved_cell.open_host() as host:
        backend = _transfer_backend(host, cell)
        put = await host.put(source, remote_dir, mode=_MODE)

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
