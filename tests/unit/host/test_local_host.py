import asyncio
import logging
from pathlib import Path

import pytest

from otto.host.host import (
    HostFilter,
    SuppressCommandOutput,
    get_logging_command_output_enabled,
)
from otto.host.local_host import LocalHost
from otto.logger.mode import LogMode
from otto.utils import Status
from tests.conftest import active_context

# ---------------------------------------------------------------------------
# Basic construction
# ---------------------------------------------------------------------------


def test_localhost_name():
    host = LocalHost()
    assert host.name == "localhost"


# ---------------------------------------------------------------------------
# run (session-based)  # noqa: ERA001 — section divider comment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_string_command():
    host = LocalHost()
    try:
        result = (await host.run("echo hello")).only
        assert result.status == Status.Success
        assert result.command == "echo hello"
        assert "hello" in result.output
        assert result.retcode == 0
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_run_command_with_failure():
    host = LocalHost()
    try:
        result = (await host.run("asdf_nonexistent_cmd_12345")).only
        assert result.status == Status.Failed
        assert result.retcode != 0
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_run_commands_with_success_and_failure():
    host = LocalHost()
    try:
        cmds = ["echo ok", "asdf_nonexistent_cmd_12345"]
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
        async with await host.open_session("ctx") as sess:
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
    status, _msg = await host.get(
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
    status, _msg = await host.put(src, dest_dir)
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
        with active_context(log_command_output=True):
            with SuppressCommandOutput():
                await host.run(["echo hello world"])
                assert get_logging_command_output_enabled() is False
                for log in caplog.records:
                    assert HostFilter().filter(log) is False
                assert host.log is LogMode.NORMAL
            assert get_logging_command_output_enabled() is True
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_run_command_with_local_suppression():
    host = LocalHost()
    host.log = LogMode.QUIET

    # Capture via a handler on the otto logger directly — robust to the otto
    # logger's ``propagate`` toggling between tests (caplog uses the root).
    captured: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    handler = _Capture()
    handler.setLevel(logging.DEBUG)
    otto_logger = logging.getLogger("otto")
    prev_level = otto_logger.level
    otto_logger.setLevel(logging.DEBUG)
    otto_logger.addHandler(handler)
    try:
        # A QUIET host folds its standing mode into each record's ``log_mode``
        # at the emit seam (``_effective_log``), so the records it emits carry
        # ``log_mode=LogMode.QUIET`` and HostFilter drops them — the filter no
        # longer reads ``host.log`` directly.
        assert get_logging_command_output_enabled() is True
        host._log_command("echo hello world", host._effective_log(LogMode.NORMAL))
        host._log_output("hello world", host._effective_log(LogMode.NORMAL))
        cmd_records = [r for r in captured if getattr(r, "host", None) is host]
        assert cmd_records
        for record in cmd_records:
            assert getattr(record, "log_mode", None) is LogMode.QUIET
            assert HostFilter().filter(record) is False
    finally:
        otto_logger.removeHandler(handler)
        otto_logger.setLevel(prev_level)
        await host.close()


def test_per_host_suppression_restores_prior_state():
    host = LocalHost()
    host.log = LogMode.NORMAL
    with SuppressCommandOutput(host=host):
        assert host.log is LogMode.QUIET
    assert host.log is LogMode.NORMAL


def test_global_suppression_restores_prior_state():
    # Snapshot True → suppress → restore True
    with active_context(log_command_output=True):
        assert get_logging_command_output_enabled() is True
        with SuppressCommandOutput():
            assert get_logging_command_output_enabled() is False
        assert get_logging_command_output_enabled() is True

    # Snapshot False → suppress → restore False (the pre-fix bug path)
    with active_context(log_command_output=False):
        assert get_logging_command_output_enabled() is False
        with SuppressCommandOutput():
            assert get_logging_command_output_enabled() is False
        assert get_logging_command_output_enabled() is False


def test_per_host_suppression_does_not_touch_global():
    host = LocalHost()
    host.log = LogMode.NORMAL
    with active_context(log_command_output=True):
        assert get_logging_command_output_enabled() is True
        with SuppressCommandOutput(host=host):
            assert get_logging_command_output_enabled() is True
        assert get_logging_command_output_enabled() is True

    # And in the reverse: pre-fix bug flipped the global flag to True on
    # exit of a per-host context; verify it doesn't any more.
    with active_context(log_command_output=False):
        host.log = LogMode.NORMAL
        assert get_logging_command_output_enabled() is False
        with SuppressCommandOutput(host=host):
            assert get_logging_command_output_enabled() is False
        assert get_logging_command_output_enabled() is False


def test_nested_global_then_host():
    host = LocalHost()
    host.log = LogMode.NORMAL
    with active_context(log_command_output=True):
        with SuppressCommandOutput():
            assert get_logging_command_output_enabled() is False
            with SuppressCommandOutput(host=host):
                assert host.log is LogMode.QUIET
                assert get_logging_command_output_enabled() is False
            # Inner exit must leave the outer's global suppression intact.
            assert get_logging_command_output_enabled() is False
            assert host.log is LogMode.NORMAL
        assert get_logging_command_output_enabled() is True


def test_nested_host_then_global():
    host = LocalHost()
    host.log = LogMode.NORMAL
    with active_context(log_command_output=True):
        with SuppressCommandOutput(host=host):
            assert host.log is LogMode.QUIET
            with SuppressCommandOutput():
                assert get_logging_command_output_enabled() is False
                assert host.log is LogMode.QUIET
            # Inner exit must leave the outer's per-host suppression intact.
            assert host.log is LogMode.QUIET
            assert get_logging_command_output_enabled() is True
        assert host.log is LogMode.NORMAL


def test_nested_global_then_global():
    with active_context(log_command_output=True):
        assert get_logging_command_output_enabled() is True
        with SuppressCommandOutput():
            assert get_logging_command_output_enabled() is False
            with SuppressCommandOutput():
                assert get_logging_command_output_enabled() is False
            # Inner must restore the outer's prior value (still False), not True.
            assert get_logging_command_output_enabled() is False
        assert get_logging_command_output_enabled() is True


@pytest.mark.asyncio
async def test_concurrent_per_host_suppression_does_not_conflict_globally():
    host_a = LocalHost()
    host_b = LocalHost()
    host_a.log = LogMode.NORMAL
    host_b.log = LogMode.NORMAL

    # The per-host standing mode is folded into the record's ``log_mode`` at the
    # emit seam (``_effective_log``), so a record's disposition is whatever the
    # host's mode was when it was emitted — captured here at make-time.
    def make_record(host: LocalHost) -> logging.LogRecord:
        record = logging.LogRecord(
            name="otto",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="cmd",
            args=(),
            exc_info=None,
        )
        record.host = host  # type: ignore[attr-defined]
        record.log_mode = host._effective_log(LogMode.NORMAL)  # type: ignore[attr-defined]
        return record

    both_inside = asyncio.Event()
    pending = {"count": 2}
    lock = asyncio.Lock()

    async def run_with_suppression(host: LocalHost) -> None:
        with SuppressCommandOutput(host=host):
            async with lock:
                pending["count"] -= 1
                if pending["count"] == 0:
                    both_inside.set()
            # Wait until both tasks are inside their per-host contexts, so
            # we can assert the two contexts really do overlap.
            await asyncio.wait_for(both_inside.wait(), timeout=2.0)
            assert host.log is LogMode.QUIET
            assert get_logging_command_output_enabled() is True
            # Under overlap: each host's own records (emitted now, while QUIET)
            # are filtered out, and the global flag is unaffected for other hosts.
            assert HostFilter().filter(make_record(host)) is False

    try:
        await asyncio.gather(
            run_with_suppression(host_a),
            run_with_suppression(host_b),
        )
        # Global flag untouched throughout.
        assert get_logging_command_output_enabled() is True
        # Both hosts restored to their prior values.
        assert host_a.log is LogMode.NORMAL
        assert host_b.log is LogMode.NORMAL
        # Post-restoration, records emitted now are NORMAL and would be allowed.
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
