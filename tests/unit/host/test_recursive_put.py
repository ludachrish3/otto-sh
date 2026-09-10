"""put_tree: a local tree reduced to per-level non-recursive puts, measured on LocalHost."""

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


async def _put(host, src, dest, **kw):
    kw.setdefault("mode", None)
    kw.setdefault("user", None)
    kw.setdefault("show_progress", False)
    return await put_tree(host, src, dest, **kw)


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

    async def flaky(files, dest_dir, **kw):
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


@pytest.mark.asyncio
async def test_a_missing_plain_source_keeps_the_backends_own_sequential_semantics(
    tmp_path: Path,
):
    """A batch of plain sources keeps the backend's own sequential semantics.

    ``put_tree`` must not change what a plain, non-recursive ``put`` does:
    every plain source rides one ordinary ``host.put`` call, exactly as it
    would without recursion. On LocalHost that call is sequential and stops
    at its first failure, so a missing source ahead of a good one Skips the
    good one rather than running it — that is the backend's existing
    contract, not something ``put_tree`` may override.
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
    assert result.value[plain].status == Status.Skipped


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
