"""Unit tests for posix remote file operations (run against a real LocalHost)."""
from unittest.mock import AsyncMock

import pytest

from otto.host.local_host import LocalHost
from otto.utils import CommandStatus, Status


@pytest.mark.asyncio
async def test_exists_true_and_false(tmp_path):
    host = LocalHost()
    f = tmp_path / "present"
    f.write_text("hi")
    assert await host.exists(f) is True
    assert await host.exists(tmp_path / "absent") is False


@pytest.mark.asyncio
async def test_ls_lists_names(tmp_path):
    host = LocalHost()
    (tmp_path / "a.txt").write_text("x")
    (tmp_path / "b.txt").write_text("y")
    names = await host.ls(tmp_path)
    assert sorted(names) == ["a.txt", "b.txt"]


@pytest.mark.asyncio
async def test_ls_all_includes_dotfiles(tmp_path):
    host = LocalHost()
    (tmp_path / ".hidden").write_text("x")
    (tmp_path / "visible").write_text("y")
    assert ".hidden" in await host.ls(tmp_path, all=True)
    assert ".hidden" not in await host.ls(tmp_path, all=False)



@pytest.mark.asyncio
async def test_mkdir_creates_nested(tmp_path):
    host = LocalHost()
    target = tmp_path / "a" / "b" / "c"
    status, _ = await host.mkdir(target)
    assert status is Status.Success
    assert target.is_dir()


@pytest.mark.asyncio
async def test_rm_removes_file(tmp_path):
    host = LocalHost()
    f = tmp_path / "gone.txt"
    f.write_text("x")
    status, _ = await host.rm(f)
    assert status is Status.Success
    assert not f.exists()


@pytest.mark.asyncio
async def test_rm_recursive_removes_tree(tmp_path):
    host = LocalHost()
    d = tmp_path / "tree"
    (d / "sub").mkdir(parents=True)
    (d / "sub" / "f").write_text("x")
    status, _ = await host.rm(d, recursive=True)
    assert status is Status.Success
    assert not d.exists()


@pytest.mark.asyncio
async def test_rm_missing_without_force_fails(tmp_path):
    host = LocalHost()
    status, _ = await host.rm(tmp_path / "nope")
    assert status is not Status.Success


@pytest.mark.asyncio
async def test_rm_missing_with_force_succeeds(tmp_path):
    host = LocalHost()
    status, _ = await host.rm(tmp_path / "nope", force=True)
    assert status is Status.Success


@pytest.mark.asyncio
async def test_cp_copies_file(tmp_path):
    host = LocalHost()
    src = tmp_path / "src.txt"
    src.write_text("data")
    dst = tmp_path / "dst.txt"
    status, _ = await host.cp(src, dst)
    assert status is Status.Success
    assert dst.read_text() == "data"
    assert src.exists()  # copy, not move


@pytest.mark.asyncio
async def test_cp_recursive_copies_tree(tmp_path):
    host = LocalHost()
    d = tmp_path / "d"
    (d).mkdir()
    (d / "f").write_text("x")
    status, _ = await host.cp(d, tmp_path / "d2", recursive=True)
    assert status is Status.Success
    assert (tmp_path / "d2" / "f").read_text() == "x"


@pytest.mark.asyncio
async def test_mv_moves_file(tmp_path):
    host = LocalHost()
    src = tmp_path / "a.txt"
    src.write_text("data")
    dst = tmp_path / "b.txt"
    status, _ = await host.mv(src, dst)
    assert status is Status.Success
    assert dst.read_text() == "data"
    assert not src.exists()  # moved


@pytest.mark.asyncio
async def test_write_then_read_round_trip(tmp_path):
    host = LocalHost()
    f = tmp_path / "note.txt"
    status, _ = await host.write_file(f, "hello\nworld\n")
    assert status is Status.Success
    assert await host.read_file(f) == "hello\nworld\n"


@pytest.mark.asyncio
async def test_write_file_append(tmp_path):
    host = LocalHost()
    f = tmp_path / "log.txt"
    await host.write_file(f, "a\n")
    await host.write_file(f, "b\n", append=True)
    assert await host.read_file(f) == "a\nb\n"


@pytest.mark.asyncio
async def test_write_file_handles_shell_special_chars(tmp_path):
    host = LocalHost()
    f = tmp_path / "tricky.txt"
    payload = "x';rm -rf /;$(echo bad)`echo worse`\n"
    await host.write_file(f, payload)
    assert await host.read_file(f) == payload  # base64 transport is injection-safe


@pytest.mark.asyncio
async def test_read_file_missing_raises(tmp_path):
    host = LocalHost()
    with pytest.raises(FileNotFoundError):
        await host.read_file(tmp_path / "nope")


@pytest.mark.asyncio
async def test_read_file_round_trips_arbitrary_content_exactly(tmp_path):
    # No trailing newline, an embedded would-be sentinel, and trailing spaces on
    # a line — all byte-exact via base64 (no sentinel/rstrip corruption).
    host = LocalHost()
    f = tmp_path / "exact.txt"
    payload = "trailing spaces   \n__OTTO_EOF__ in body\nno final newline"
    await host.write_file(f, payload)
    assert await host.read_file(f) == payload


# ---------------------------------------------------------------------------
#  Embedded host file operations
# ---------------------------------------------------------------------------


def _zephyr_with_fs():
    """Build a ZephyrHost whose filesystem supports transfer (FAT/RAM)."""
    from otto.host.embedded_filesystem import build_filesystem
    from otto.host.embedded_host import ZephyrHost
    return ZephyrHost(ip="192.0.2.1", element="sprout", log=False,
                      filesystem=build_filesystem("fat-ram"))


@pytest.mark.asyncio
async def test_embedded_rm_uses_filesystem_rm_command():
    host = _zephyr_with_fs()
    host._run_one = AsyncMock(  # type: ignore[method-assign]
        return_value=CommandStatus("fs rm /RAM:/f", "", Status.Success, 0))
    status, _ = await host.rm("/RAM:/f")
    assert status is Status.Success
    issued = host._run_one.await_args.args[0]
    assert issued == host.filesystem.rm_command("/RAM:/f")


@pytest.mark.asyncio
async def test_embedded_ls_uses_filesystem_ls_command():
    host = _zephyr_with_fs()
    host._run_one = AsyncMock(  # type: ignore[method-assign]
        return_value=CommandStatus("fs ls /RAM:", "a.bin\nb.bin", Status.Success, 0))
    names = await host.ls("/RAM:")
    assert names == ["a.bin", "b.bin"]
    assert host._run_one.await_args.args[0] == host.filesystem.ls_command("/RAM:")


@pytest.mark.asyncio
async def test_embedded_exists_true_via_ls():
    host = _zephyr_with_fs()
    host._run_one = AsyncMock(  # type: ignore[method-assign]
        return_value=CommandStatus("fs ls /RAM:/a.bin", "a.bin", Status.Success, 0))
    assert await host.exists("/RAM:/a.bin") is True


@pytest.mark.asyncio
async def test_embedded_exists_false_when_fs_ls_fails():
    host = _zephyr_with_fs()
    host._run_one = AsyncMock(  # type: ignore[method-assign]
        return_value=CommandStatus("fs ls /RAM:/nope", "", Status.Error, 1))
    assert await host.exists("/RAM:/nope") is False


@pytest.mark.asyncio
async def test_embedded_unsupported_ops_fail_loud():
    host = _zephyr_with_fs()
    for coro in (host.mkdir("/RAM:/d"), host.cp("/a", "/b"),
                 host.mv("/a", "/b"), host.read_file("/a"), host.write_file("/a", "x")):
        with pytest.raises(NotImplementedError):
            await coro
