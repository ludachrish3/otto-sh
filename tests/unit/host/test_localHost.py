import asyncio
import logging
from pathlib import Path

import pytest

from otto.host.host import (
    CommandStatus,
    HostFilter,
    SuppressCommandOutput,
    _setLoggingCommandOutputEnabled,
    getLoggingCommandOutputEnabled,
)
from otto.host.localHost import LocalHost
from otto.utils import Status


# ---------------------------------------------------------------------------
# Basic construction
# ---------------------------------------------------------------------------

def test_localhost_name():
    host = LocalHost()
    assert host.name == "localhost"


# ---------------------------------------------------------------------------
# run (session-based)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_string_command():
    host = LocalHost()
    try:
        result = (await host.run('echo hello')).only
        assert result.status == Status.Success
        assert result.command == 'echo hello'
        assert 'hello' in result.output
        assert result.retcode == 0
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_run_command_with_failure():
    host = LocalHost()
    try:
        result = (await host.run('asdf_nonexistent_cmd_12345')).only
        assert result.status == Status.Failed
        assert result.retcode != 0
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_run_commands_with_success_and_failure():
    host = LocalHost()
    try:
        cmds = ['echo ok', 'asdf_nonexistent_cmd_12345']
        result = await host.run(cmds)
        assert result.status == Status.Failed
        assert result.statuses[0].status == Status.Success
        assert result.statuses[1].status == Status.Failed
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_run_one_cmd_with_output():
    host = LocalHost()
    try:
        result = await host.run(["echo hello world"])
        assert result.status == Status.Success
        assert result.statuses[0].output == "hello world"
        assert result.statuses[0].retcode == 0
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_run_multiline_output():
    host = LocalHost()
    try:
        result = (await host.run("echo line1; echo line2; echo line3")).only
        assert result.status == Status.Success
        lines = result.output.strip().splitlines()
        assert lines == ["line1", "line2", "line3"]
    finally:
        await host.close()


# ---------------------------------------------------------------------------
# State persistence (session-based run)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cd_persists_between_commands():
    host = LocalHost()
    try:
        await host.run("cd /tmp")
        result = (await host.run("pwd")).only
        assert result.status == Status.Success
        assert result.output.strip() == "/tmp"
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_env_var_persists():
    host = LocalHost()
    try:
        await host.run("export OTTO_LOCAL_TEST=xyz789")
        result = (await host.run("echo $OTTO_LOCAL_TEST")).only
        assert result.status == Status.Success
        assert "xyz789" in result.output
    finally:
        await host.close()


# ---------------------------------------------------------------------------
# oneshot (stateless subprocess)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_oneshot_basic():
    host = LocalHost()
    result = await host.oneshot("echo oneshot_test")
    assert result.status == Status.Success
    assert "oneshot_test" in result.output


@pytest.mark.asyncio
async def test_oneshot_failure():
    host = LocalHost()
    result = await host.oneshot("asdf_nonexistent_cmd_12345")
    assert result.status == Status.Failed
    assert result.retcode != 0


@pytest.mark.asyncio
async def test_oneshot_is_stateless():
    """oneshot() should NOT persist state between calls."""
    host = LocalHost()
    await host.oneshot("export OTTO_ONESHOT_VAR=nope")
    result = await host.oneshot("echo ${OTTO_ONESHOT_VAR:-empty}")
    assert "empty" in result.output


# ---------------------------------------------------------------------------
# send / expect
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_and_expect():
    host = LocalHost()
    try:
        await host.send("echo otto_send_test\n")
        output = await host.expect(r"otto_send_test", timeout=5.0)
        assert "otto_send_test" in output
    finally:
        await host.close()


# ---------------------------------------------------------------------------
# open_session (named sessions)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_open_session():
    host = LocalHost()
    try:
        session = await host.open_session("test_sess")
        result = (await session.run("echo from_named_session")).only
        assert result.status == Status.Success
        assert "from_named_session" in result.output
        await session.close()
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_open_session_context_manager():
    host = LocalHost()
    try:
        async with (await host.open_session("ctx")) as sess:
            result = (await sess.run("echo ctx_test")).only
            assert result.status == Status.Success
            assert "ctx_test" in result.output
    finally:
        await host.close()


# ---------------------------------------------------------------------------
# File transfer (local copy)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_files(tmp_path: Path):
    host = LocalHost()
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "a.txt").write_text("aaa")
    (src_dir / "b.txt").write_text("bbb")

    dest_dir = tmp_path / "dest"
    status, msg = await host.get(
        [src_dir / "a.txt", src_dir / "b.txt"],
        dest_dir,
    )
    assert status == Status.Success
    assert (dest_dir / "a.txt").read_text() == "aaa"
    assert (dest_dir / "b.txt").read_text() == "bbb"


@pytest.mark.asyncio
async def test_put_files(tmp_path: Path):
    host = LocalHost()
    src = tmp_path / "file.txt"
    src.write_text("data")

    dest_dir = tmp_path / "remote"
    status, msg = await host.put(src, dest_dir)
    assert status == Status.Success
    assert (dest_dir / "file.txt").read_text() == "data"


@pytest.mark.asyncio
async def test_get_files_nonexistent_source(tmp_path: Path):
    host = LocalHost()
    status, msg = await host.get(
        [tmp_path / "no_such_file.txt"],
        tmp_path / "dest",
    )
    assert status == Status.Error
    assert msg  # should have an error message


# ---------------------------------------------------------------------------
# Logging suppression
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_command_with_global_suppression(caplog):
    host = LocalHost()
    try:
        with SuppressCommandOutput():
            await host.run(["echo hello world"])
            assert getLoggingCommandOutputEnabled() is False
            for log in caplog.records:
                assert HostFilter().filter(log) is False
            assert host.log is True
        assert getLoggingCommandOutputEnabled() is True
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_run_command_with_local_suppression(caplog):
    host = LocalHost()
    host.log = False
    try:
        with SuppressCommandOutput(host=host):
            assert getLoggingCommandOutputEnabled() is True
            await host.run(["echo hello world"])
            for log in caplog.records:
                assert HostFilter().filter(log) is False
    finally:
        await host.close()


def test_per_host_suppression_restores_prior_state():
    host = LocalHost()
    host.log = True
    with SuppressCommandOutput(host=host):
        assert host.log is False
    assert host.log is True


def test_global_suppression_restores_prior_state():
    # Snapshot True → suppress → restore True
    assert getLoggingCommandOutputEnabled() is True
    with SuppressCommandOutput():
        assert getLoggingCommandOutputEnabled() is False
    assert getLoggingCommandOutputEnabled() is True

    # Snapshot False → suppress → restore False (the pre-fix bug path)
    _setLoggingCommandOutputEnabled(False)
    try:
        with SuppressCommandOutput():
            assert getLoggingCommandOutputEnabled() is False
        assert getLoggingCommandOutputEnabled() is False
    finally:
        _setLoggingCommandOutputEnabled(True)


def test_per_host_suppression_does_not_touch_global():
    host = LocalHost()
    host.log = True
    assert getLoggingCommandOutputEnabled() is True
    with SuppressCommandOutput(host=host):
        assert getLoggingCommandOutputEnabled() is True
    assert getLoggingCommandOutputEnabled() is True

    # And in the reverse: pre-fix bug flipped the global flag to True on
    # exit of a per-host context; verify it doesn't any more.
    _setLoggingCommandOutputEnabled(False)
    try:
        with SuppressCommandOutput(host=host):
            assert getLoggingCommandOutputEnabled() is False
        assert getLoggingCommandOutputEnabled() is False
    finally:
        _setLoggingCommandOutputEnabled(True)


def test_nested_global_then_host():
    host = LocalHost()
    host.log = True
    with SuppressCommandOutput():
        assert getLoggingCommandOutputEnabled() is False
        with SuppressCommandOutput(host=host):
            assert host.log is False
            assert getLoggingCommandOutputEnabled() is False
        # Inner exit must leave the outer's global suppression intact.
        assert getLoggingCommandOutputEnabled() is False
        assert host.log is True
    assert getLoggingCommandOutputEnabled() is True


def test_nested_host_then_global():
    host = LocalHost()
    host.log = True
    with SuppressCommandOutput(host=host):
        assert host.log is False
        with SuppressCommandOutput():
            assert getLoggingCommandOutputEnabled() is False
            assert host.log is False
        # Inner exit must leave the outer's per-host suppression intact.
        assert host.log is False
        assert getLoggingCommandOutputEnabled() is True
    assert host.log is True


def test_nested_global_then_global():
    assert getLoggingCommandOutputEnabled() is True
    with SuppressCommandOutput():
        assert getLoggingCommandOutputEnabled() is False
        with SuppressCommandOutput():
            assert getLoggingCommandOutputEnabled() is False
        # Inner must restore the outer's prior value (still False), not True.
        assert getLoggingCommandOutputEnabled() is False
    assert getLoggingCommandOutputEnabled() is True


@pytest.mark.asyncio
async def test_concurrent_per_host_suppression_does_not_conflict_globally():
    host_a = LocalHost()
    host_b = LocalHost()
    host_a.log = True
    host_b.log = True

    # Built once and reused — the filter reads host.log at call time, so
    # it reflects the live state regardless of when the record was made.
    def make_record(host: LocalHost) -> logging.LogRecord:
        record = logging.LogRecord(
            name='otto', level=logging.INFO, pathname='', lineno=0,
            msg='cmd', args=(), exc_info=None,
        )
        record.host = host  # type: ignore[attr-defined]
        return record

    both_inside = asyncio.Event()
    pending = {'count': 2}
    lock = asyncio.Lock()

    async def run_with_suppression(host: LocalHost) -> None:
        with SuppressCommandOutput(host=host):
            async with lock:
                pending['count'] -= 1
                if pending['count'] == 0:
                    both_inside.set()
            # Wait until both tasks are inside their per-host contexts, so
            # we can assert the two contexts really do overlap.
            await asyncio.wait_for(both_inside.wait(), timeout=2.0)
            assert host.log is False
            assert getLoggingCommandOutputEnabled() is True
            # Under overlap: each host's own records are filtered out,
            # and the global flag is unaffected for other hosts.
            assert HostFilter().filter(make_record(host)) is False

    try:
        await asyncio.gather(
            run_with_suppression(host_a),
            run_with_suppression(host_b),
        )
        # Global flag untouched throughout.
        assert getLoggingCommandOutputEnabled() is True
        # Both hosts restored to their prior values.
        assert host_a.log is True
        assert host_b.log is True
        # Post-restoration, both hosts' records would be allowed again.
        assert HostFilter().filter(make_record(host_a)) is True
        assert HostFilter().filter(make_record(host_b)) is True
    finally:
        await host_a.close()
        await host_b.close()


# ---------------------------------------------------------------------------
# interact (still not implemented)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_local_host_interact():
    host = LocalHost()
    with pytest.raises(NotImplementedError):
        await host.interact()


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_close():
    host = LocalHost()
    await host.run("echo init")
    await host.close()
    # After close, a new run should still work (session is recreated)
    result = (await host.run("echo after_close")).only
    assert result.status == Status.Success
    assert "after_close" in result.output
    await host.close()
