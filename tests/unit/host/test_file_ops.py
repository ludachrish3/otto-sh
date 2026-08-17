"""Unit tests for posix remote file operations (run against a real LocalHost)."""

import base64
from unittest.mock import AsyncMock

import pytest

from otto.host.local_host import LocalHost
from otto.logger.mode import LogMode
from otto.result import CommandResult
from otto.utils import Status


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
async def test_glob_expands_on_the_hosts_own_shell(tmp_path):
    """The device expands the pattern; otto only reads back concrete paths.

    Run against a REAL shell rather than a scripted `exec`, because the one
    mutation worth catching is invisible to a string check: quoting the
    pattern (`self._q(pattern)`, the spelling every other verb in this module
    uses) stops expansion dead — the shell then iterates the literal, the
    `[ -e ]` guard drops it, and `glob` answers `[]` for a directory that
    plainly has matches. Only a real expansion tells those apart.
    """
    host = LocalHost()
    (tmp_path / "messages").write_text("x")
    (tmp_path / "messages.1").write_text("y")
    (tmp_path / "syslog").write_text("z")  # non-matching: the pattern must select
    matched = await host.glob(str(tmp_path / "messages*"))
    assert sorted(matched) == [str(tmp_path / "messages"), str(tmp_path / "messages.1")]


@pytest.mark.asyncio
async def test_glob_sends_the_pattern_unquoted():
    """The command carries the RAW pattern — quoting it is the whole failure.

    The companion to the test above, at the other end: that one proves the
    expansion happened, this one proves WHY it can, so a future edit that
    reaches for `self._q` here fails with a message about quoting rather than
    about an empty list.
    """
    host = LocalHost()
    host.exec = AsyncMock(  # type: ignore[method-assign]
        return_value=CommandResult(status=Status.Success, value="", command="", retcode=0)
    )
    await host.glob("/var/log/messages*")
    sent = host.exec.await_args.args[0]
    assert "/var/log/messages*" in sent, f"the pattern never reached the device: {sent!r}"
    assert "'/var/log/messages*'" not in sent, (
        f"the pattern was shell-quoted, which is exactly what stops it expanding: {sent!r}"
    )


@pytest.mark.asyncio
async def test_glob_of_an_unmatched_pattern_is_an_empty_list(tmp_path):
    # POSIX sh leaves an unmatched pattern LITERAL — the `[ -e ]` guard is what
    # keeps that literal from being handed back as a path that exists. Zero
    # matches is success (no logs is a legitimate answer), not an error.
    assert await LocalHost().glob(str(tmp_path / "nothing-here-*.log")) == []


@pytest.mark.asyncio
async def test_glob_keeps_its_matches_when_the_last_expanded_entry_fails_the_guard(tmp_path):
    """A failing FINAL iteration must not discard the matches already found.

    THE HOSTILE CONDITION IS INJECTED, not waited for: a broken symlink whose
    name sorts LAST among the pattern's matches. `[ -e ]` is false for it, and
    with the payload written as `[ -e "$p" ] && printf ...` that false test
    became the LOOP's exit status — the command came back non-ok and the
    `if not result.status.is_ok: return []` branch threw away two real paths
    the device had already printed. Measured `[]`, deterministically, for a
    directory with two matching files in it.

    That is a fabricated absence arriving from a REAL run rather than a dry
    one, and the silent-and-total kind: a log collector reads it as "this host
    has no logs". `if`/`fi` is the fix, because a POSIX `if` with no `else`
    exits 0 when its condition is false.
    """
    (tmp_path / "messages").write_text("x")
    (tmp_path / "messages.1").write_text("y")
    (tmp_path / "messages.zz").symlink_to(tmp_path / "nonexistent-target")

    # SETUP CHECKS: this test measures nothing unless the failing iteration is
    # genuinely the LAST one, which is a fact about sort order and about the
    # link being broken. A rename that quietly moved it earlier would leave the
    # test green against the `&&` construct it exists to red.
    assert max(p.name for p in tmp_path.iterdir()) == "messages.zz", (
        "the dangling entry must sort last, or the loop's final iteration is a "
        "successful one and the discard this test is about never triggers"
    )
    assert not (tmp_path / "messages.zz").exists(), (
        "the symlink must be BROKEN — `[ -e ]` is what has to come back false"
    )

    matched = await LocalHost().glob(str(tmp_path / "messages*"))
    assert matched == [str(tmp_path / "messages"), str(tmp_path / "messages.1")], (
        f"the two real matches were discarded because the last expanded entry "
        f"failed the `-e` guard: {matched!r}"
    )


@pytest.mark.asyncio
async def test_glob_of_a_metacharacter_free_pattern_returns_the_concrete_path(tmp_path):
    # A caller may configure a plain path where another configures a pattern;
    # the plain one must come back as itself, and a missing one as nothing.
    present = tmp_path / "otto.log"
    present.write_text("x")
    assert await LocalHost().glob(str(present)) == [str(present)]
    assert await LocalHost().glob(str(tmp_path / "absent.log")) == []


@pytest.mark.asyncio
async def test_mkdir_creates_nested(tmp_path):
    host = LocalHost()
    target = tmp_path / "a" / "b" / "c"
    result = await host.mkdir(target)
    assert result.status is Status.Success
    assert target.is_dir()


@pytest.mark.asyncio
async def test_rm_removes_file(tmp_path):
    host = LocalHost()
    f = tmp_path / "gone.txt"
    f.write_text("x")
    result = await host.rm(f)
    assert result.status is Status.Success
    assert not f.exists()


@pytest.mark.asyncio
async def test_rm_recursive_removes_tree(tmp_path):
    host = LocalHost()
    d = tmp_path / "tree"
    (d / "sub").mkdir(parents=True)
    (d / "sub" / "f").write_text("x")
    result = await host.rm(d, recursive=True)
    assert result.status is Status.Success
    assert not d.exists()


@pytest.mark.asyncio
async def test_rm_missing_without_force_fails(tmp_path):
    host = LocalHost()
    result = await host.rm(tmp_path / "nope")
    assert result.status is not Status.Success


@pytest.mark.asyncio
async def test_rm_missing_with_force_succeeds(tmp_path):
    host = LocalHost()
    result = await host.rm(tmp_path / "nope", force=True)
    assert result.status is Status.Success


@pytest.mark.asyncio
async def test_cp_copies_file(tmp_path):
    host = LocalHost()
    src = tmp_path / "src.txt"
    src.write_text("data")
    dst = tmp_path / "dst.txt"
    result = await host.cp(src, dst)
    assert result.status is Status.Success
    assert dst.read_text() == "data"
    assert src.exists()  # copy, not move


@pytest.mark.asyncio
async def test_cp_recursive_copies_tree(tmp_path):
    host = LocalHost()
    d = tmp_path / "d"
    (d).mkdir()
    (d / "f").write_text("x")
    result = await host.cp(d, tmp_path / "d2", recursive=True)
    assert result.status is Status.Success
    assert (tmp_path / "d2" / "f").read_text() == "x"


@pytest.mark.asyncio
async def test_mv_moves_file(tmp_path):
    host = LocalHost()
    src = tmp_path / "a.txt"
    src.write_text("data")
    dst = tmp_path / "b.txt"
    result = await host.mv(src, dst)
    assert result.status is Status.Success
    assert dst.read_text() == "data"
    assert not src.exists()  # moved


@pytest.mark.asyncio
async def test_write_then_read_round_trip(tmp_path):
    host = LocalHost()
    f = tmp_path / "note.txt"
    result = await host.write_file(f, "hello\nworld\n")
    assert result.status is Status.Success
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


@pytest.mark.asyncio
async def test_read_file_round_trips_content_whose_base64_wraps_multiple_lines(tmp_path):
    """The device's ``base64`` wraps its output, and that must not corrupt the read.

    MEASURED: this machine's coreutils ``base64`` wraps at 76 columns by
    default -- encoding 200 bytes of payload here produces more than one
    line (confirmed directly: ``base64 <file>`` on a 100-byte input already
    produces 2 lines). ``read_file`` must flatten that wrapping before
    decoding with ``validate=True``, or the bare newlines a real device's
    ``base64`` always emits would make every read fail, not just a
    corrupted one.
    """
    host = LocalHost()
    f = tmp_path / "wrapped.bin"
    payload = "y" * 200
    await host.write_file(f, payload)
    assert await host.read_file(f) == payload


@pytest.mark.asyncio
async def test_read_file_raises_on_a_stray_non_alphabet_byte_mid_stream(tmp_path):
    """A transport glitch -- one stray byte inside an otherwise-valid stream -- must be loud.

    MEASURED (reproduced in this test): decoding this exact corrupted
    string with the stdlib DEFAULT (``base64.b64decode(x)``, i.e.
    ``validate=False``) silently discards the stray ``@`` and recovers the
    ORIGINAL payload byte-for-byte -- the corruption is invisible, not
    wrong-looking. ``read_file`` must not rely on that luck: decoding with
    ``validate=True`` on the same input must raise instead of silently
    returning a plausible string.

    A corrupt decode is also a different condition than a failed read, and
    must be reported as one: ``read_file`` already raises
    :class:`FileNotFoundError` when the remote command itself fails (see
    ``test_read_file_missing_raises``), while here the command succeeded and
    something came back -- it just was not valid base64. The raised error
    must name the path it failed on so a caller chasing the failure is not
    left guessing which read broke.
    """
    host = LocalHost()
    f = tmp_path / "corrupt.bin"
    payload = b"the quick brown fox jumps over the lazy dog, more text for length"
    encoded = base64.b64encode(payload).decode("ascii")
    corrupted = encoded[:10] + "@" + encoded[10:]
    assert base64.b64decode(corrupted) == payload, (
        "setup check: the default decode must silently recover the original bytes "
        "here, or this test is not exercising the discard this task is about"
    )
    host.exec = AsyncMock(  # type: ignore[method-assign]
        return_value=CommandResult(
            status=Status.Success, value=corrupted, command="base64 ...", retcode=0
        )
    )
    with pytest.raises(ValueError, match="not valid base64") as exc_info:
        await host.read_file(f)
    assert str(f) in str(exc_info.value), (
        f"error should name the path it failed on: {exc_info.value}"
    )


# ---------------------------------------------------------------------------
#  Embedded host file operations
# ---------------------------------------------------------------------------


def _zephyr_with_fs():
    """Build a ZephyrHost whose filesystem supports transfer (FAT/RAM)."""
    from otto.host.embedded_filesystem import build_filesystem
    from otto.host.embedded_host import ZephyrHost

    return ZephyrHost(
        ip="192.0.2.1", element="sprout", log=LogMode.QUIET, filesystem=build_filesystem("fat-ram")
    )


@pytest.mark.asyncio
async def test_embedded_rm_uses_filesystem_rm_command():
    host = _zephyr_with_fs()
    host._run_one = AsyncMock(  # type: ignore[method-assign]
        return_value=CommandResult(
            status=Status.Success, value="", command="fs rm /RAM:/f", retcode=0
        )
    )
    result = await host.rm("/RAM:/f")
    assert result.status is Status.Success
    issued = host._run_one.await_args.args[0]
    assert issued == host.filesystem.rm_command("/RAM:/f")


@pytest.mark.asyncio
async def test_embedded_ls_uses_filesystem_ls_command():
    host = _zephyr_with_fs()
    host._run_one = AsyncMock(  # type: ignore[method-assign]
        return_value=CommandResult(
            status=Status.Success, value="a.bin\nb.bin", command="fs ls /RAM:", retcode=0
        )
    )
    names = await host.ls("/RAM:")
    assert names == ["a.bin", "b.bin"]
    assert host._run_one.await_args.args[0] == host.filesystem.ls_command("/RAM:")


@pytest.mark.asyncio
async def test_embedded_exists_true_via_ls():
    host = _zephyr_with_fs()
    host._run_one = AsyncMock(  # type: ignore[method-assign]
        return_value=CommandResult(
            status=Status.Success, value="a.bin", command="fs ls /RAM:/a.bin", retcode=0
        )
    )
    assert await host.exists("/RAM:/a.bin") is True


@pytest.mark.asyncio
async def test_embedded_exists_false_when_fs_ls_fails():
    host = _zephyr_with_fs()
    host._run_one = AsyncMock(  # type: ignore[method-assign]
        return_value=CommandResult(
            status=Status.Error, value="", command="fs ls /RAM:/nope", retcode=1
        )
    )
    assert await host.exists("/RAM:/nope") is False


@pytest.mark.asyncio
async def test_embedded_unsupported_ops_fail_loud():
    host = _zephyr_with_fs()
    for coro in (
        host.mkdir("/RAM:/d"),
        host.cp("/a", "/b"),
        host.mv("/a", "/b"),
        host.read_file("/a"),
        host.write_file("/a", "x"),
    ):
        with pytest.raises(NotImplementedError):
            await coro
