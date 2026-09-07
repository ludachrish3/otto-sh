"""Unit tests for deriving a container mount table from docker inspect."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from otto.docker.mounts import inspect_mounts, parse_inspect_output
from otto.result import CommandNotRunError, CommandResult
from otto.utils import Status

CID_A = "a" * 64
CID_B = "b" * 64

BIND = (
    '{"Type":"bind","Source":"/srv/data","Destination":"/var/lib/app",'
    '"Mode":"","RW":true,"Propagation":"rprivate"}'
)
VOLUME = (
    '{"Type":"volume","Name":"appvol",'
    '"Source":"/var/lib/docker/volumes/appvol/_data","Destination":"/data",'
    '"Driver":"local","Mode":"z","RW":true,"Propagation":""}'
)
RO_BIND = (
    '{"Type":"bind","Source":"/srv/ro","Destination":"/ro",'
    '"Mode":"","RW":false,"Propagation":"rprivate"}'
)
TMPFS = '{"Type":"tmpfs","Source":"","Destination":"/scratch","Mode":"","RW":true,"Propagation":""}'


def test_parses_a_bind():
    table = parse_inspect_output(f"{CID_A}\t[{BIND}]")
    (mount,) = table[CID_A]
    assert mount.container_path == Path("/var/lib/app")
    assert mount.parent_path == Path("/srv/data")
    assert mount.kind == "bind"
    assert mount.name is None
    assert mount.read_only is False


def test_parses_a_named_volume():
    table = parse_inspect_output(f"{CID_A}\t[{VOLUME}]")
    (mount,) = table[CID_A]
    assert mount.kind == "volume"
    assert mount.name == "appvol"
    assert mount.parent_path == Path("/var/lib/docker/volumes/appvol/_data")


def test_read_only_is_the_negation_of_rw():
    (mount,) = parse_inspect_output(f"{CID_A}\t[{RO_BIND}]")[CID_A]
    assert mount.read_only is True


def test_drops_entries_with_no_parent_side_path():
    # `--mount type=tmpfs` reports Source "" -- there is no parent path, so
    # the honest answer to "is /scratch shared?" is no.
    table = parse_inspect_output(f"{CID_A}\t[{TMPFS},{BIND}]")
    assert [m.container_path for m in table[CID_A]] == [Path("/var/lib/app")]


def test_empty_array_is_a_container_with_no_mounts():
    assert parse_inspect_output(f"{CID_A}\t[]") == {CID_A: []}


def test_null_mounts_reads_as_no_mounts():
    assert parse_inspect_output(f"{CID_A}\tnull") == {CID_A: []}


def test_multiple_containers_are_keyed_by_id():
    table = parse_inspect_output(f"{CID_A}\t[{BIND}]\n{CID_B}\t[]")
    assert set(table) == {CID_A, CID_B}
    assert table[CID_B] == []


def test_ignores_interleaved_diagnostic_lines():
    # otto merges stderr into stdout, so docker's own error text arrives here.
    text = f"{CID_A}\t[{BIND}]\nerror: no such object: nosuch\n{CID_B}\t[]"
    assert set(parse_inspect_output(text)) == {CID_A, CID_B}


def test_ignores_a_line_whose_payload_is_not_json():
    assert parse_inspect_output(f"{CID_A}\tnot json at all") == {}


@pytest.mark.asyncio
async def test_inspect_mounts_returns_empty_for_no_ids():
    parent = MagicMock()
    parent.exec = AsyncMock()
    assert await inspect_mounts(parent, []) == {}
    parent.exec.assert_not_awaited()


@pytest.mark.asyncio
async def test_inspect_mounts_keys_by_the_ids_the_caller_passed():
    parent = MagicMock()
    parent.id = "test3"
    parent.exec = AsyncMock(
        return_value=CommandResult(
            Status.Success, value=f"{CID_A}\t[{BIND}]", command="", retcode=0
        )
    )
    table = await inspect_mounts(parent, [CID_A])
    assert list(table) == [CID_A]
    assert table[CID_A][0].parent_path == Path("/srv/data")


@pytest.mark.asyncio
async def test_inspect_mounts_pins_the_object_type():
    """``--type container`` is what makes a miss say "no such container".

    Without it, ``docker inspect`` falls back to images on a miss and
    ``{{json .Mounts}}`` against an image errors instead of producing a row --
    a different failure shape for the same input, arriving as interleaved
    stderr the parser silently drops.
    """
    parent = MagicMock()
    parent.id = "test3"
    parent.exec = AsyncMock(
        return_value=CommandResult(
            Status.Success, value=f"{CID_A}\t[{BIND}]", command="", retcode=0
        )
    )
    await inspect_mounts(parent, [CID_A])
    assert "--type container" in parent.exec.await_args.args[0]


@pytest.mark.asyncio
async def test_inspect_mounts_resolves_a_short_id_against_the_full_hex_row():
    # docker ps -q (compose.py's _resolve_container_id, run without
    # --no-trunc) hands every real caller a 12-char short id, while
    # docker inspect's own row is always keyed by the full 64-hex id. This
    # fallback is the path every real container takes, not an edge case.
    short = CID_A[:12]
    parent = MagicMock()
    parent.id = "test3"
    parent.exec = AsyncMock(
        return_value=CommandResult(
            Status.Success, value=f"{CID_A}\t[{BIND}]", command="", retcode=0
        )
    )
    table = await inspect_mounts(parent, [short])
    assert table[short][0].parent_path == Path("/srv/data")


@pytest.mark.asyncio
async def test_inspect_mounts_a_legit_empty_table_is_not_treated_as_unanswered(caplog):
    # An id that maps directly to `[]` genuinely has no mounts. What this
    # pins is the SECOND `if found is None:` (the `unanswered` decision):
    # mutating it to `if not found:` treats the empty list as a miss and
    # logs a spurious "could not read mounts" warning. The FIRST `found is
    # None` (the short-id fallback trigger) is defensive-and-currently-
    # equivalent to `if not found:` here -- sending `[]` through the prefix
    # scan is harmless, since `full.startswith(cid)` matches the container
    # against its own full id and returns the same `[]` right back.
    parent = MagicMock()
    parent.id = "test3"
    parent.exec = AsyncMock(
        return_value=CommandResult(Status.Success, value=f"{CID_A}\t[]", command="", retcode=0)
    )
    table = await inspect_mounts(parent, [CID_A])
    assert table[CID_A] == []
    assert "could not read mounts" not in caplog.text


@pytest.mark.asyncio
async def test_inspect_mounts_keeps_the_good_rows_when_one_container_is_gone(caplog):
    # docker exits 1 for the missing object but still prints the others. Gating
    # on is_ok would discard the whole stack's mapping.
    parent = MagicMock()
    parent.id = "test3"
    parent.exec = AsyncMock(
        return_value=CommandResult(
            Status.Failed,
            value=f"{CID_A}\t[{BIND}]\nerror: no such object: {CID_B}",
            command="",
            retcode=1,
        )
    )
    table = await inspect_mounts(parent, [CID_A, CID_B])
    assert table[CID_A][0].parent_path == Path("/srv/data")
    assert table[CID_B] == []
    assert CID_B[:12] in caplog.text


@pytest.mark.asyncio
async def test_inspect_mounts_refuses_a_dry_run_decline():
    # A dry run's decline is Status.NotRun (see host.py's _dry_run_result),
    # never Status.Skipped -- Skipped.is_ok is True, which would sail past
    # refuse_declined_fact and read a fabricated empty table as fact.
    parent = MagicMock()
    parent.id = "test3"
    parent.exec = AsyncMock(
        return_value=CommandResult(Status.NotRun, value="", command="", retcode=-1)
    )
    with pytest.raises(CommandNotRunError, match="inspect_mounts"):
        await inspect_mounts(parent, [CID_A])
