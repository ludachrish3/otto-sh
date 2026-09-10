"""The remote tree listing every POSIX family answers, measured against the runner's real shell.

``PosixFileOps._walk_remote`` is called UNBOUND on a ``LocalHost`` so the sh
function itself runs (the ``LocalHost`` override is a filesystem walk with no
shell); both must agree on every tree here.
"""

import os
from pathlib import Path

import pytest

from otto.host.file_ops import PosixFileOps
from otto.host.local_host import LocalHost
from otto.host.recursive_transfer import ListingError, RemoteListing, parse_listing
from otto.result import CommandNotRunError
from otto.utils import Status
from tests.conftest import active_context


def _tree(root: Path) -> None:
    (root / "a.txt").write_text("a")
    (root / ".dot").write_text("hidden")
    (root / "name with space.txt").write_text("s")
    (root / "sub").mkdir()
    (root / "sub" / "b.bin").write_bytes(b"\x00\x01")
    (root / "sub" / "deeper").mkdir()
    (root / "sub" / "deeper" / "c").write_text("c")
    (root / "empty").mkdir()
    (root / "link_dir").symlink_to(root / "sub")
    (root / "link_file").symlink_to(root / "a.txt")
    (root / "dangling").symlink_to(root / "nowhere")


def _norm(listing: RemoteListing, root: Path) -> tuple[str, list[str], list[str]]:
    return (
        listing.kind,
        sorted(str(d.relative_to(root)) for d in listing.dirs),
        sorted(str(f.relative_to(root)) for f in listing.files),
    )


EXPECTED = (
    "d",
    ["empty", "sub", "sub/deeper"],
    [".dot", "a.txt", "link_file", "name with space.txt", "sub/b.bin", "sub/deeper/c"],
)


@pytest.mark.asyncio
async def test_shell_listing_walks_the_tree_without_descending_directory_symlinks(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    _tree(root)
    listing = await PosixFileOps._walk_remote(LocalHost(), root)
    assert _norm(listing, root) == EXPECTED


@pytest.mark.asyncio
async def test_local_listing_matches_the_shell_listing(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    _tree(root)
    assert _norm(await LocalHost()._walk_remote(root), root) == EXPECTED


@pytest.mark.asyncio
@pytest.mark.parametrize("walk", ["shell", "local"])
async def test_listing_classifies_a_file_and_a_missing_path(tmp_path: Path, walk: str):
    f = tmp_path / "plain"
    f.write_text("x")
    host = LocalHost()
    call = (lambda p: PosixFileOps._walk_remote(host, p)) if walk == "shell" else host._walk_remote
    assert (await call(f)).kind == "f"
    assert (await call(tmp_path / "absent")).kind == "-"


@pytest.mark.asyncio
async def test_top_level_symlink_to_a_directory_is_walked(tmp_path: Path):
    real = tmp_path / "real"
    real.mkdir()
    (real / "x").write_text("x")
    link = tmp_path / "link"
    link.symlink_to(real)
    listing = await PosixFileOps._walk_remote(LocalHost(), link)
    assert listing.kind == "d"
    assert [f.name for f in listing.files] == ["x"]


@pytest.mark.asyncio
@pytest.mark.parametrize("walk", ["shell", "local"])
async def test_a_newline_in_a_name_refuses_the_tree_by_name(tmp_path: Path, walk: str):
    root = tmp_path / "root"
    root.mkdir()
    (root / "bad\nname").write_text("x")
    host = LocalHost()
    call = (lambda p: PosixFileOps._walk_remote(host, p)) if walk == "shell" else host._walk_remote
    with pytest.raises(ListingError, match="contains a newline"):
        await call(root)


@pytest.mark.asyncio
async def test_local_walk_remote_refuses_a_root_whose_own_name_contains_a_newline(tmp_path: Path):
    root = tmp_path / "bad\nname"
    root.mkdir()
    with pytest.raises(ListingError, match="contains a newline"):
        await LocalHost()._walk_remote(root)


@pytest.mark.asyncio
@pytest.mark.parametrize("walk", ["shell", "local"])
async def test_an_unreadable_subdirectory_makes_the_listing_raise(tmp_path: Path, walk: str):
    if os.geteuid() == 0:
        pytest.skip("root reads everything")
    root = tmp_path / "root"
    root.mkdir()
    locked = root / "locked"
    locked.mkdir()
    (root / "a.txt").write_text("a")
    host = LocalHost()
    call = (lambda p: PosixFileOps._walk_remote(host, p)) if walk == "shell" else host._walk_remote
    locked.chmod(0)
    try:
        with pytest.raises(ListingError, match="cannot read"):
            await call(root)
    finally:
        locked.chmod(0o700)  # so tmp_path cleanup can remove it


@pytest.mark.asyncio
async def test_an_unreadable_root_makes_the_shell_listing_raise(tmp_path: Path):
    if os.geteuid() == 0:
        pytest.skip("root reads everything")
    root = tmp_path / "root"
    root.mkdir()
    root.chmod(0)
    try:
        with pytest.raises(ListingError, match="cannot read"):
            await PosixFileOps._walk_remote(LocalHost(), root)
    finally:
        root.chmod(0o700)  # so tmp_path cleanup can remove it


@pytest.mark.asyncio
async def test_walk_remote_declines_under_dry_run(tmp_path: Path):
    with active_context(dry_run=True), pytest.raises(CommandNotRunError):
        await PosixFileOps._walk_remote(LocalHost(), tmp_path)


def test_parse_listing_count_mismatch_is_the_newline_signal():
    text = "t d\nf /r/a\nf /r/b\nn 1\n"
    with pytest.raises(ListingError, match="contains a newline"):
        parse_listing(text, Path("/r"))


def test_parse_listing_rejects_a_truncated_stream():
    with pytest.raises(ListingError, match="malformed"):
        parse_listing("t d\nf /r/a\n", Path("/r"))


def test_parse_listing_rejects_a_non_integer_count_line():
    with pytest.raises(ListingError, match="malformed"):
        parse_listing("t d\nn x\n", Path("/r"))


def test_parse_listing_rejects_an_unknown_tag():
    with pytest.raises(ListingError, match="malformed"):
        parse_listing("t d\nz /r/a\nn 1\n", Path("/r"))


@pytest.mark.asyncio
async def test_mkdir_all_creates_every_directory_in_one_command(tmp_path: Path, monkeypatch):
    host = LocalHost()
    seen: list[str] = []
    real_exec = host.exec

    async def spy(cmd, *a, **k):
        seen.append(cmd)
        return await real_exec(cmd, *a, **k)

    monkeypatch.setattr(host, "exec", spy)
    targets = [tmp_path / "x" / "y", tmp_path / "z"]
    result = await PosixFileOps._mkdir_all(host, targets)
    assert result.is_ok
    assert all(t.is_dir() for t in targets)
    assert len(seen) == 1
    assert seen[0].startswith("mkdir -p ")


@pytest.mark.asyncio
async def test_mkdir_all_of_an_empty_list_is_success_without_exec(monkeypatch):
    host = LocalHost()

    async def boom(*a, **k):
        raise AssertionError("PosixFileOps._mkdir_all([]) must not exec")

    monkeypatch.setattr(host, "exec", boom)
    result = await PosixFileOps._mkdir_all(host, [])
    assert result.is_ok


@pytest.mark.asyncio
async def test_local_mkdir_all_uses_no_shell(tmp_path: Path, monkeypatch):
    host = LocalHost()

    async def boom(*a, **k):
        raise AssertionError("LocalHost._mkdir_all must not exec")

    monkeypatch.setattr(host, "exec", boom)
    result = await host._mkdir_all([tmp_path / "p" / "q"])
    assert result.is_ok
    assert (tmp_path / "p" / "q").is_dir()
    assert (await host._mkdir_all([])).is_ok


@pytest.mark.asyncio
async def test_local_mkdir_all_declines_under_dry_run(tmp_path: Path):
    host = LocalHost()
    target = tmp_path / "x" / "y"
    with active_context(dry_run=True):
        result = await host._mkdir_all([target])
    assert result.status is Status.NotRun
    assert not target.exists()
