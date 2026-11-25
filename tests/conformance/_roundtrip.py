"""The round trip and the mode check that two contracts of this tree assert.

``tests/conformance/test_transfer_contract.py`` asserts them on every drawn
cell of the support matrix, and ``tests/conformance/test_nc_rung_contract.py``
asserts them once per netcat implementation on every netcat bed host. Both
import them from here, so a change to what "a file survived the transfer"
means reaches both contracts at once.
"""

from pathlib import Path

from otto.host.host import BaseHost
from otto.result import Result

# Every tripwire a text-only payload misses, in one file:
#   \r\n      a backend that normalises line endings drops the \r
#   \x00      a backend that treats the payload as a C string stops here
#   \xff\xfe  bytes that are not valid UTF-8 at all
#   final \n  a backend that strips or adds a trailing newline is visible
PAYLOAD = (
    b"otto-conformance\r\nnul->\x00<-nul\ttab\n\xff\xfe not utf-8\nand the file ends on a newline\n"
)

# Deliberately not 0o644, 0o664, 0o755 or 0o600: a backend that applies no mode
# at all lands one of those from the runner's umask, and an assertion against
# such a value would pass for the wrong reason. 0o615 keeps the owner read bit,
# so `get` can still read the file back.
MODE = 0o615


async def assert_roundtrip(
    host: BaseHost, remote_dir: Path, tmp_path: Path, *, name: str, payload: bytes, label: str
) -> None:
    """Put *payload* to *remote_dir* as *name*, get it back, and compare the bytes.

    Three directories, not two, and on a bed cell only two of them are on
    this machine. A round trip into the source directory would have the
    ``get`` overwrite the very file it is being compared against, so a
    backend that transferred nothing at all would still "round-trip"
    perfectly. *remote_dir* is the third: a directory under ``tmp_path``
    where the far side is the runner, a device path where it is not.

    The results are asserted before the bytes because they answer a
    different question: a transfer can land the right bytes and still report
    failure (and a caller acting on the report would then delete or retry),
    and it can report success having written nothing.

    The landed file is left on the host; a caller that must leave the host
    as it found it removes ``remote_dir / name`` itself.

    Args:
        host: The open host.
        remote_dir: Where the file lands on the host.
        tmp_path: The test's local directory; ``source`` and ``retrieved``
            are made under it, and may already exist.
        name: The file's basename, here and on the host.
        payload: The bytes to send.
        label: What the failure messages name, the cell or its id.
    """
    source_dir = tmp_path / "source"
    retrieved_dir = tmp_path / "retrieved"
    for directory in (source_dir, retrieved_dir):
        directory.mkdir(exist_ok=True)
    source = source_dir / name
    source.write_bytes(payload)

    put = await host.put(source, remote_dir)
    assert put.is_ok, f"{label}: put reported {put.status!r} -- {put.msg!r}"
    got = await host.get(remote_dir / source.name, retrieved_dir)
    assert got.is_ok, f"{label}: get reported {got.status!r} -- {got.msg!r}"

    retrieved = retrieved_dir / source.name
    assert retrieved.exists(), f"{label}: get reported success but wrote no file at {retrieved}"
    assert retrieved.read_bytes() == payload, (
        f"{label}: the roundtripped file is not the payload -- "
        f"sent {len(payload)} bytes, got back {len(retrieved.read_bytes())}"
    )


async def assert_mode_landed(host: BaseHost, put: Result, landed: Path, *, label: str) -> None:
    """*put*, a ``put(mode=MODE)``, succeeded and left *landed* at :data:`MODE` on the host.

    Read back through ``run()`` rather than through ``Path.stat()``, because
    the question is what the mode is on the HOST. ``stat -c %a`` is the same
    spelling otto's own ``nc`` transfer already assumes for ``stat -c %s``,
    and it answers on every bed host including BusyBox 1.16.1.

    Args:
        host: The open host the put ran on.
        put: The put's result.
        landed: The landed file's path on the host.
        label: What the failure messages name, the cell or its id.
    """
    assert put.is_ok, f"{label}: put(mode=0o{MODE:o}) reported {put.status!r} -- {put.msg!r}"
    observed = (await host.run(f"stat -c %a {landed}")).only
    assert observed.is_ok, (
        f"{label}: could not read the landed file's mode back -- "
        f"`stat -c %a` gave {observed.status!r} {observed.value!r}"
    )
    assert observed.value == f"{MODE:o}", (
        f"{label}: put(mode=0o{MODE:o}) left the file at mode {observed.value!r}"
    )
