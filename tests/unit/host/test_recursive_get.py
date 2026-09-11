"""get_tree: a remote tree enumerated once, then fetched per level, measured on LocalHost."""

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from otto.host.local_host import LocalHost
from otto.host.recursive_transfer import ListingError, RemoteListing, get_tree
from otto.result import Result
from otto.utils import Status
from tests.conftest import active_context


def _tree(root: Path) -> None:
    (root / "a.txt").write_text("a")
    (root / "sub").mkdir()
    (root / "sub" / "b.bin").write_bytes(b"\x00\x01")
    (root / "empty").mkdir()
    (root / "link_dir").symlink_to(root / "sub")


class _PeakTrackingHost:
    """Fake host recording each ``get`` call's in-flight count and ``concurrent`` kwarg."""

    def __init__(self, listing: RemoteListing) -> None:
        self._listing = listing
        self.peak = 0
        self._in_flight = 0
        self.calls: list[tuple[Path, bool]] = []

    async def _walk_remote(self, path):
        return self._listing

    async def get(self, files, dest_dir, *, user, show_progress, concurrent):
        self.calls.append((dest_dir, concurrent))
        self._in_flight += 1
        self.peak = max(self.peak, self._in_flight)
        await asyncio.sleep(0.01)
        self._in_flight -= 1
        return Result(
            Status.Success,
            value={f: Result(Status.Success, value=dest_dir / f.name) for f in files},
        )


def _chain_listing(root: Path) -> RemoteListing:
    """A single-child chain (L1 -> L1/L2 -> L1/L2/L3) so the level order is unambiguous."""
    l1 = root / "L1"
    l2 = l1 / "L2"
    l3 = l2 / "L3"
    return RemoteListing(
        kind="d",
        dirs=[l1, l2, l3],
        files=[l1 / "f1", l2 / "f2", l3 / "f3"],
    )


async def _get(host, src, dest, **kw):
    kw.setdefault("user", None)
    kw.setdefault("show_progress", False)
    kw.setdefault("concurrent", True)
    return await get_tree(host, src, dest, **kw)


@pytest.mark.asyncio
async def test_levels_launch_together_when_concurrent(tmp_path: Path):
    tree = tmp_path / "tree"
    dest = tmp_path / "dest"
    host = _PeakTrackingHost(_chain_listing(tree))

    result = await _get(host, [tree], dest, concurrent=True)

    assert result.status == Status.Success, result.msg
    assert host.peak == 3
    assert all(concurrent is True for _, concurrent in host.calls)


@pytest.mark.asyncio
async def test_levels_run_in_order_when_not_concurrent(tmp_path: Path):
    tree = tmp_path / "tree"
    dest = tmp_path / "dest"
    host = _PeakTrackingHost(_chain_listing(tree))

    result = await _get(host, [tree], dest, concurrent=False)

    assert result.status == Status.Success, result.msg
    assert host.peak == 1
    assert [dest_dir for dest_dir, _ in host.calls] == [
        dest / "tree" / "L1",
        dest / "tree" / "L1" / "L2",
        dest / "tree" / "L1" / "L2" / "L3",
    ]
    assert all(concurrent is False for _, concurrent in host.calls)


@pytest.mark.asyncio
async def test_tree_lands_under_dest_with_its_own_name_and_empty_dirs(tmp_path: Path):
    tree = tmp_path / "tree"
    tree.mkdir()
    _tree(tree)
    dest = tmp_path / "dest"

    result = await _get(LocalHost(), [tree], dest)

    assert result.status == Status.Success, result.msg
    assert (dest / "tree" / "a.txt").read_text() == "a"
    assert (dest / "tree" / "sub" / "b.bin").read_bytes() == b"\x00\x01"
    assert (dest / "tree" / "empty").is_dir()
    assert not (dest / "tree" / "link_dir").exists()
    entry = result.value[tree]
    assert set(entry.value) == {Path("a.txt"), Path("sub/b.bin")}
    assert entry.value[Path("sub/b.bin")].value == dest / "tree" / "sub" / "b.bin"


@pytest.mark.asyncio
async def test_plain_files_and_trees_mix_and_keep_argument_order(tmp_path: Path):
    tree = tmp_path / "tree"
    tree.mkdir()
    _tree(tree)
    plain = tmp_path / "plain.txt"
    plain.write_text("p")
    dest = tmp_path / "dest"
    dest.mkdir()

    result = await _get(LocalHost(), [tree, plain], dest)

    assert list(result.value) == [tree, plain]
    assert result.value[plain].value == dest / "plain.txt"
    assert (dest / "plain.txt").read_text() == "p"


@pytest.mark.asyncio
async def test_a_missing_remote_source_fails_its_own_entry(tmp_path: Path):
    dest = tmp_path / "dest"
    result = await _get(LocalHost(), [tmp_path / "absent"], dest)
    assert result.status == Status.Error
    entry = result.value[tmp_path / "absent"]
    assert "no such file" in entry.msg
    assert entry.value == {}


@pytest.mark.asyncio
async def test_a_failed_listing_fails_the_tree_with_the_listing_message(tmp_path: Path):
    host = LocalHost()
    good = tmp_path / "good.txt"
    good.write_text("data")
    real_walk = host._walk_remote
    real_get = host.get
    get_calls: list[list[Path]] = []

    async def fake_walk(path):
        if Path(path) == Path("/x"):
            raise ListingError("/x: tree listing failed: sh: boom")
        return await real_walk(path)

    async def recording_get(files, dest_dir, **kw):
        get_calls.append(list(files))
        return await real_get(files, dest_dir, **kw)

    host._walk_remote = AsyncMock(side_effect=fake_walk)
    host.get = recording_get

    result = await _get(host, [Path("/x"), good], tmp_path / "dest")

    assert result.value[Path("/x")].status == Status.Error
    assert "sh: boom" in result.value[Path("/x")].msg
    assert result.value[Path("/x")].value == {}
    # No get was attempted for the failed listing's source.
    assert all(Path("/x") not in files for files in get_calls)
    # A later source still proceeds after an earlier one's ListingError.
    assert result.value[good].status == Status.Success
    assert (tmp_path / "dest" / "good.txt").read_text() == "data"


@pytest.mark.asyncio
async def test_a_per_file_failure_is_that_files_entry_and_other_levels_still_land(tmp_path: Path):
    tree = tmp_path / "tree"
    tree.mkdir()
    _tree(tree)
    dest = tmp_path / "dest"
    host = LocalHost()
    real_get = host.get
    calls: list[dict] = []

    async def flaky(files, dest_dir, **kw):
        calls.append(kw)
        if any(f.name == "a.txt" for f in files):
            return Result(
                Status.Error,
                value={f: Result(Status.Error, msg="boom", value=dest_dir / f.name) for f in files},
                msg="boom",
            )
        return await real_get(files, dest_dir, **kw)

    host.get = flaky

    result = await _get(host, [tree], dest)

    entry = result.value[tree]
    assert entry.status == Status.Error
    assert entry.value[Path("a.txt")].status == Status.Error
    assert entry.value[Path("sub/b.bin")].status == Status.Success
    assert (dest / "tree" / "sub" / "b.bin").exists()
    assert calls
    assert all(kw["concurrent"] is True for kw in calls)


@pytest.mark.asyncio
async def test_an_unreadable_root_is_an_error_entry_not_an_empty_success(tmp_path: Path):
    if os.geteuid() == 0:
        pytest.skip("root reads everything")
    root = tmp_path / "root"
    root.mkdir()
    root.chmod(0)
    dest = tmp_path / "dest"
    try:
        result = await _get(LocalHost(), [root], dest)
    finally:
        root.chmod(0o700)  # so tmp_path cleanup can remove it

    assert result.status == Status.Error
    assert result.value[root].status == Status.Error
    assert "cannot read" in result.value[root].msg


@pytest.mark.asyncio
async def test_dry_run_is_one_not_run_entry_per_source_and_asks_nothing(tmp_path: Path):
    host = LocalHost()
    host._walk_remote = AsyncMock(side_effect=AssertionError("a dry run asks the host nothing"))
    dest = tmp_path / "dest"

    with active_context(dry_run=True):
        result = await _get(host, [Path("/var/log")], dest)

    entry = result.value[Path("/var/log")]
    assert entry.status == Status.NotRun
    assert entry.value == dest / "log"
    assert "not enumerated" in entry.msg
    assert not dest.exists()
