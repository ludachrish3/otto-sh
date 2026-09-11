"""put_tree: a local tree reduced to per-level non-recursive puts, measured on LocalHost."""

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from otto.host.local_host import LocalHost
from otto.host.recursive_transfer import put_tree
from otto.result import Result
from otto.utils import Status
from tests.conftest import active_context


def _tree(root: Path) -> None:
    (root / "a.txt").write_text("a")
    (root / "sub").mkdir()
    (root / "sub" / "b.bin").write_bytes(b"\x00\x01")
    (root / "empty").mkdir()
    (root / "link_dir").symlink_to(root / "sub")
    (root / "link_file").symlink_to(root / "a.txt")


def _chain_tree(root: Path) -> None:
    """A single-child chain (L1 -> L1/L2 -> L1/L2/L3) so the walk order is unambiguous."""
    l1 = root / "L1"
    l2 = l1 / "L2"
    l3 = l2 / "L3"
    l3.mkdir(parents=True)
    (l1 / "f1").write_text("1")
    (l2 / "f2").write_text("2")
    (l3 / "f3").write_text("3")


class _PeakTrackingHost:
    """Fake host recording each ``put`` call's in-flight count and ``concurrent`` kwarg."""

    def __init__(self) -> None:
        self.peak = 0
        self._in_flight = 0
        self.calls: list[tuple[Path, bool]] = []

    async def _mkdir_all(self, paths, *, user=None):
        return Result(Status.Success, value={})

    async def put(self, files, dest_dir, *, mode, user, show_progress, concurrent):
        self.calls.append((dest_dir, concurrent))
        self._in_flight += 1
        self.peak = max(self.peak, self._in_flight)
        await asyncio.sleep(0.01)
        self._in_flight -= 1
        return Result(
            Status.Success,
            value={f: Result(Status.Success, value=dest_dir / f.name) for f in files},
        )


async def _put(host, src, dest, **kw):
    kw.setdefault("mode", None)
    kw.setdefault("user", None)
    kw.setdefault("show_progress", False)
    kw.setdefault("concurrent", True)
    return await put_tree(host, src, dest, **kw)


@pytest.mark.asyncio
async def test_levels_launch_together_when_concurrent(tmp_path: Path):
    tree = tmp_path / "tree"
    tree.mkdir()
    _chain_tree(tree)
    dest = tmp_path / "dest"
    dest.mkdir()
    host = _PeakTrackingHost()

    result = await _put(host, [tree], dest, concurrent=True)

    assert result.status == Status.Success, result.msg
    assert host.peak == 3
    assert all(concurrent is True for _, concurrent in host.calls)


@pytest.mark.asyncio
async def test_levels_run_in_order_when_not_concurrent(tmp_path: Path):
    tree = tmp_path / "tree"
    tree.mkdir()
    _chain_tree(tree)
    dest = tmp_path / "dest"
    dest.mkdir()
    host = _PeakTrackingHost()

    result = await _put(host, [tree], dest, concurrent=False)

    assert result.status == Status.Success, result.msg
    assert host.peak == 1
    assert [dest_dir for dest_dir, _ in host.calls] == [
        dest / "tree" / "L1",
        dest / "tree" / "L1" / "L2",
        dest / "tree" / "L1" / "L2" / "L3",
    ]
    assert all(concurrent is False for _, concurrent in host.calls)


class _CancelObservingHost:
    """Fake host whose ``L2`` level raises while its siblings sleep and record cancellation."""

    def __init__(self) -> None:
        self.cancelled: list[str] = []
        self.finished: list[str] = []

    async def _mkdir_all(self, paths, *, user=None):
        return Result(Status.Success, value={})

    async def put(self, files, dest_dir, *, mode, user, show_progress, concurrent):
        name = dest_dir.name
        if name == "L2":
            await asyncio.sleep(0.01)
            raise RuntimeError("boom")
        try:
            # Long enough that it would still be asleep when L2 raises; a
            # cancellation interrupts the sleep immediately regardless of
            # its length, so this never actually waits it out.
            await asyncio.sleep(100)
            self.finished.append(name)
            return Result(
                Status.Success,
                value={f: Result(Status.Success, value=dest_dir / f.name) for f in files},
            )
        except asyncio.CancelledError:
            self.cancelled.append(name)
            raise


@pytest.mark.asyncio
async def test_a_raise_cancels_and_drains_the_other_levels(tmp_path: Path):
    tree = tmp_path / "tree"
    tree.mkdir()
    _chain_tree(tree)
    dest = tmp_path / "dest"
    dest.mkdir()
    host = _CancelObservingHost()

    with pytest.raises(RuntimeError, match="boom"):
        await _put(host, [tree], dest, concurrent=True)

    assert sorted(host.cancelled) == ["L1", "L3"]
    assert host.finished == []


@pytest.mark.asyncio
async def test_sequential_raise_never_starts_the_remaining_level(tmp_path: Path):
    tree = tmp_path / "tree"
    tree.mkdir()
    _chain_tree(tree)
    dest = tmp_path / "dest"
    dest.mkdir()
    started = {"L3": False}

    class _RaisingHost:
        async def _mkdir_all(self, paths, *, user=None):
            return Result(Status.Success, value={})

        async def put(self, files, dest_dir, *, mode, user, show_progress, concurrent):
            name = dest_dir.name
            if name == "L3":
                started["L3"] = True
                return Result(Status.Success, value={})
            if name == "L2":
                raise RuntimeError("boom")
            return Result(
                Status.Success,
                value={f: Result(Status.Success, value=dest_dir / f.name) for f in files},
            )

    host = _RaisingHost()

    with pytest.raises(RuntimeError, match="boom"):
        await _put(host, [tree], dest, concurrent=False)

    assert started["L3"] is False


@pytest.mark.asyncio
async def test_tree_lands_under_dest_with_its_own_name(tmp_path: Path):
    tree = tmp_path / "tree"
    tree.mkdir()
    _tree(tree)
    dest = tmp_path / "dest"
    dest.mkdir()

    result = await _put(LocalHost(), [tree], dest)

    assert result.status == Status.Success, result.msg
    landed = dest / "tree"
    assert (landed / "a.txt").read_text() == "a"
    assert (landed / "sub" / "b.bin").read_bytes() == b"\x00\x01"
    assert (landed / "empty").is_dir()
    assert (landed / "link_file").read_text() == "a"
    assert not (landed / "link_dir").exists(), "a directory symlink is not descended"


@pytest.mark.asyncio
async def test_result_is_nested_and_keyed_relative_to_the_root(tmp_path: Path):
    tree = tmp_path / "tree"
    tree.mkdir()
    _tree(tree)
    plain = tmp_path / "plain.txt"
    plain.write_text("p")
    dest = tmp_path / "dest"
    dest.mkdir()

    result = await _put(LocalHost(), [plain, tree], dest)

    assert list(result.value) == [plain, tree]
    assert result.value[plain].value == dest / "plain.txt"
    entry = result.value[tree]
    assert entry.status == Status.Success
    assert set(entry.value) == {Path("a.txt"), Path("link_file"), Path("sub/b.bin")}
    assert entry.value[Path("sub/b.bin")].value == dest / "tree" / "sub" / "b.bin"


@pytest.mark.asyncio
async def test_mode_applies_to_files_and_never_to_directories(tmp_path: Path):
    tree = tmp_path / "tree"
    tree.mkdir()
    _tree(tree)
    dest = tmp_path / "dest"
    dest.mkdir()

    result = await _put(LocalHost(), [tree], dest, mode="600")

    assert result.status == Status.Success, result.msg
    assert (dest / "tree" / "sub" / "b.bin").stat().st_mode & 0o777 == 0o600
    assert (dest / "tree" / "sub").stat().st_mode & 0o777 != 0o600


@pytest.mark.asyncio
async def test_a_failed_mkdir_fails_the_tree_before_any_byte_moves(tmp_path: Path):
    tree = tmp_path / "tree"
    tree.mkdir()
    _tree(tree)
    dest = tmp_path / "dest"
    dest.mkdir()
    host = LocalHost()
    host._mkdir_all = AsyncMock(return_value=Result(Status.Error, msg="disk on fire"))
    host.put = AsyncMock(side_effect=AssertionError("no put after a failed mkdir"))

    result = await _put(host, [tree], dest)

    assert result.status == Status.Error
    assert "disk on fire" in result.value[tree].msg
    assert result.value[tree].value == {}


@pytest.mark.asyncio
async def test_a_per_file_failure_is_that_files_entry_and_later_levels_still_run(tmp_path: Path):
    tree = tmp_path / "tree"
    tree.mkdir()
    _tree(tree)
    dest = tmp_path / "dest"
    dest.mkdir()
    host = LocalHost()
    real_put = host.put
    calls: list[dict] = []

    async def flaky(files, dest_dir, **kw):
        calls.append(kw)
        if any(f.name == "a.txt" for f in files):
            return Result(
                Status.Error,
                value={f: Result(Status.Error, msg="boom", value=dest_dir / f.name) for f in files},
                msg="boom",
            )
        return await real_put(files, dest_dir, **kw)

    host.put = flaky

    result = await _put(host, [tree], dest)

    entry = result.value[tree]
    assert entry.status == Status.Error
    assert entry.value[Path("a.txt")].status == Status.Error
    assert entry.value[Path("a.txt")].value == dest / "tree" / "a.txt"
    assert entry.value[Path("sub/b.bin")].status == Status.Success
    assert calls
    assert all(kw["concurrent"] is True for kw in calls)


@pytest.mark.asyncio
async def test_a_missing_plain_source_keeps_the_backends_own_per_file_semantics(
    tmp_path: Path,
):
    """A batch of plain sources keeps the backend's own per-file semantics.

    ``put_tree`` must not change what a plain, non-recursive ``put`` does:
    every plain source rides one ordinary ``host.put`` call, exactly as it
    would without recursion. That call attempts every file, so a missing
    source ahead of a good one is its own error and the good one still
    lands — the backend's existing contract, not something ``put_tree``
    may override.
    """
    dest = tmp_path / "dest"
    dest.mkdir()
    absent = tmp_path / "absent"
    plain = tmp_path / "plain.txt"
    plain.write_text("p")

    result = await _put(LocalHost(), [absent, plain], dest)

    assert result.status == Status.Error
    assert result.value[absent].status == Status.Error
    assert result.value[absent].msg
    assert result.value[plain].is_ok, result.value[plain].msg
    assert (dest / "plain.txt").read_text() == "p"


@pytest.mark.asyncio
async def test_an_unreadable_directory_inside_the_tree_fails_the_tree(tmp_path: Path):
    if os.geteuid() == 0:
        pytest.skip("root reads everything; the hostile condition cannot be injected")
    tree = tmp_path / "tree"
    tree.mkdir()
    _tree(tree)
    locked = tree / "locked"
    locked.mkdir()
    locked.chmod(0o000)
    dest = tmp_path / "dest"
    dest.mkdir()
    try:
        result = await _put(LocalHost(), [tree], dest)
    finally:
        locked.chmod(0o755)
    assert result.value[tree].status == Status.Error
    assert "locked" in result.value[tree].msg


@pytest.mark.asyncio
async def test_dry_run_previews_every_destination_and_issues_no_mkdir(tmp_path: Path):
    tree = tmp_path / "tree"
    tree.mkdir()
    _tree(tree)
    dest = tmp_path / "dest"
    host = LocalHost()
    host._mkdir_all = AsyncMock(side_effect=AssertionError("a dry run must not mkdir"))

    with active_context(dry_run=True):
        result = await _put(host, [tree], dest)

    entry = result.value[tree]
    assert entry.status == Status.NotRun
    assert entry.value[Path("sub/b.bin")].status == Status.NotRun
    assert entry.value[Path("sub/b.bin")].value == dest / "tree" / "sub" / "b.bin"
    assert not dest.exists()


@pytest.mark.asyncio
async def test_a_bad_mode_refuses_before_any_mkdir(tmp_path: Path):
    tree = tmp_path / "tree"
    tree.mkdir()
    _tree(tree)
    dest = tmp_path / "dest"
    host = LocalHost()
    host._mkdir_all = AsyncMock(side_effect=AssertionError("no mkdir on a bad mode"))

    result = await _put(host, [tree], dest, mode="789")

    entry = result.value[tree]
    assert entry.status == Status.Error
    assert "789" in entry.msg
    assert entry.value == {}, "a tree-level refusal carries the empty per-file mapping"
    assert not dest.exists()


@pytest.mark.asyncio
async def test_dry_run_of_a_files_less_tree_is_not_run_not_success(tmp_path: Path):
    """A tree with no files at all asks the host nothing under a dry run — so
    the entry must not read as a transfer that happened."""
    tree = tmp_path / "tree"
    (tree / "sub" / "deeper").mkdir(parents=True)
    dest = tmp_path / "dest"
    host = LocalHost()
    host._mkdir_all = AsyncMock(side_effect=AssertionError("a dry run must not mkdir"))

    with active_context(dry_run=True):
        result = await _put(host, [tree], dest)

    entry = result.value[tree]
    assert entry.status == Status.NotRun
    assert entry.value == {}
    assert "directories only" in entry.msg
    assert not dest.exists()


@pytest.mark.asyncio
async def test_a_files_less_tree_outside_a_dry_run_still_succeeds(tmp_path: Path):
    """Off the dry-run path the directories really are created, so Success is
    the honest answer for a tree that carries no files."""
    tree = tmp_path / "tree"
    (tree / "sub" / "deeper").mkdir(parents=True)
    dest = tmp_path / "dest"
    dest.mkdir()

    result = await _put(LocalHost(), [tree], dest)

    entry = result.value[tree]
    assert entry.status == Status.Success, entry.msg
    assert (dest / "tree" / "sub" / "deeper").is_dir()
