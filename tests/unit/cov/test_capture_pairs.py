import shutil
import struct
import subprocess
from collections.abc import AsyncIterator
from pathlib import PurePosixPath

import pytest
import pytest_asyncio

from otto.coverage.errors import (
    CaptureFailedError,
    CoverageDataMismatchError,
    CoverageToolVersionError,
)
from otto.coverage.merge.merger import LcovMerger, short_failure
from otto.coverage.pairing import Pair
from otto.host.local_host import LocalHost
from otto.logger.mode import LogMode
from otto.result import CommandResult
from otto.utils import Status


def test_short_failure_is_first_error_then_exact_tail_then_log_path(tmp_path):
    lines = (
        [f"line {i}" for i in range(5000)]
        + ["geninfo: ERROR: mismatched end line"]
        + [f"t{i}" for i in range(30)]
    )
    s = short_failure("\n".join(lines), tmp_path / "verbose.log").splitlines()
    assert s[0] == "geninfo: ERROR: mismatched end line"
    assert s[1] == "..."
    assert s[2:22] == lines[-20:]
    assert s[22] == f"full lcov output: {tmp_path / 'verbose.log'}"
    assert len(s) == 23


def test_short_failure_without_error_line_or_log():
    assert short_failure("a\nb", None).splitlines() == [
        "a",
        "b",
        "full lcov output: verbose.log of this run",
    ]


class _Local:
    def __init__(self, result=None):
        self.cmds = []
        # Result.__bool__ is is_ok, so `result or ...` would swallow a failure.
        self.result = (
            result if result is not None else CommandResult(Status.Success, value="", retcode=0)
        )

    async def exec(self, cmd, timeout=300, log=None, **kw):
        self.cmds.append((cmd, log))
        return self.result


@pytest_asyncio.fixture
async def local_host() -> AsyncIterator[LocalHost]:
    host = LocalHost()
    try:
        yield host
    finally:
        await host.close()


def _pair(tmp_path):
    gcda = tmp_path / "f/a/main.cpp.gcda"
    gcda.parent.mkdir(parents=True)
    gcda.write_bytes(b"d")
    gcno = tmp_path / "proj/a/main.cpp.gcno"
    gcno.parent.mkdir(parents=True)
    gcno.write_bytes(b"n")
    return Pair(gcda=gcda, rel=PurePosixPath("a/main.cpp.gcda"), gcno=gcno), gcno


@pytest.mark.asyncio
async def test_staging_and_single_quiet_command(tmp_path):
    pair, gcno = _pair(tmp_path)
    local = _Local()
    await LcovMerger(local).capture_pairs([pair], tmp_path / "stage", tmp_path / "out.info")
    staged = tmp_path / "stage/a"
    assert (staged / "main.cpp.gcda").read_bytes() == b"d"
    assert (staged / "main.cpp.gcno").resolve() == gcno.resolve()
    ((cmd, log),) = local.cmds
    assert "--build-directory" not in cmd
    assert "--ignore-errors source" in cmd
    assert f"--directory {tmp_path / 'stage'}" in cmd
    assert log is LogMode.QUIET


@pytest.mark.asyncio
async def test_tmp_notes_are_staged_under_the_object_name(tmp_path):
    gcda = tmp_path / "f/mod/foo.gcda"
    gcda.parent.mkdir(parents=True)
    gcda.write_bytes(b"d")
    gcno = tmp_path / "k/mod/.tmp_foo.gcno"
    gcno.parent.mkdir(parents=True)
    gcno.write_bytes(b"n")
    await LcovMerger(_Local()).capture_pairs(
        [Pair(gcda=gcda, rel=PurePosixPath("mod/foo.gcda"), gcno=gcno)],
        tmp_path / "st",
        tmp_path / "o.info",
    )
    assert (tmp_path / "st/mod/foo.gcno").resolve() == gcno.resolve()


@pytest.mark.asyncio
async def test_failure_is_short_and_typed(tmp_path):
    pair, _ = _pair(tmp_path)
    out = "\n".join(["noise"] * 100 + ["geninfo: ERROR: boom"])
    local = _Local(CommandResult(Status.Failed, value=out, retcode=1))
    with pytest.raises(CaptureFailedError) as e:
        await LcovMerger(local).capture_pairs([pair], tmp_path / "stage", tmp_path / "out.info")
    lines = str(e.value).splitlines()
    assert lines[0] == "lcov --capture failed:"
    assert lines[1] == "geninfo: ERROR: boom"
    assert len(lines) == 24


@pytest.mark.asyncio
async def test_capture_dir_is_in_place_with_includes(tmp_path):
    local = _Local()
    await LcovMerger(local).capture_dir(tmp_path, tmp_path / "u.info", includes=["*/src/a[b].c"])
    ((cmd, _),) = local.cmds
    assert f"--directory {tmp_path}" in cmd
    assert "--ignore-errors unused" in cmd
    assert "--include '*/src/a[b].c'" in cmd


@pytest.mark.asyncio
async def test_capture_dir_refuses_gcda_without_notes(tmp_path):
    (tmp_path / "a.gcda").write_bytes(_header(b"adcg", 2))
    local = _Local()
    with pytest.raises(CoverageDataMismatchError, match="no notes file"):
        await LcovMerger(local).capture_dir(tmp_path, tmp_path / "u.info")
    assert local.cmds == []


@pytest.mark.asyncio
async def test_lcov_stamp_mismatch_output_is_a_data_mismatch(tmp_path):
    pair, _ = _pair(tmp_path)
    local = _Local(CommandResult(Status.Failed, value="geninfo: ERROR: stamp mismatch", retcode=1))
    with pytest.raises(CoverageDataMismatchError):
        await LcovMerger(local).capture_pairs([pair], tmp_path / "s", tmp_path / "o.info")


@pytest.mark.asyncio
async def test_lcov_incompatible_version_output_is_a_tool_version_error(tmp_path):
    pair, _ = _pair(tmp_path)
    out = "geninfo: ERROR: Incompatible GCC/GCOV version"
    local = _Local(CommandResult(Status.Failed, value=out, retcode=1))
    with pytest.raises(CoverageToolVersionError):
        await LcovMerger(local).capture_pairs([pair], tmp_path / "s", tmp_path / "o.info")


@pytest.mark.asyncio
async def test_failure_message_names_the_verbose_log(tmp_path, monkeypatch):
    from otto.logger import management

    log = tmp_path / "verbose.log"
    monkeypatch.setattr(management, "verbose_log_path", lambda: log)
    pair, _ = _pair(tmp_path)
    local = _Local(CommandResult(Status.Failed, value="geninfo: ERROR: boom", retcode=1))
    with pytest.raises(CaptureFailedError) as e:
        await LcovMerger(local).capture_pairs([pair], tmp_path / "s", tmp_path / "o.info")
    assert f"full lcov output: {log}" in str(e.value)


def _header(magic: bytes, stamp: int) -> bytes:
    return magic + b"B33*" + struct.pack("<I", stamp)


@pytest.mark.asyncio
async def test_capture_dir_refuses_stamp_mismatch(tmp_path):
    (tmp_path / "a.gcno").write_bytes(_header(b"oncg", 1))
    (tmp_path / "a.gcda").write_bytes(_header(b"adcg", 2))
    local = _Local()
    with pytest.raises(CoverageDataMismatchError):
        await LcovMerger(local).capture_dir(tmp_path, tmp_path / "u.info")
    assert local.cmds == []


@pytest.mark.asyncio
async def test_capture_dir_refuses_clang_checksum_mismatch(tmp_path):
    def clang_file(magic: bytes, checksum: int) -> bytes:
        # Matching headers; one function record (ident, lineno checksum, cfg checksum).
        return _header(magic, 7) + struct.pack("<IIIII", 0x01000000, 3, 1, 5, checksum)

    (tmp_path / "a.gcno").write_bytes(clang_file(b"oncg", 111))
    (tmp_path / "a.gcda").write_bytes(clang_file(b"adcg", 222))
    local = _Local()
    with pytest.raises(CoverageDataMismatchError):
        await LcovMerger(local).capture_dir(tmp_path, tmp_path / "u.info")
    assert local.cmds == []


def _build_and_run(src):
    subprocess.run(["gcc", "--coverage", "-c", "m.c", "-o", "m.o"], cwd=src, check=True)
    subprocess.run(["gcc", "--coverage", "m.o", "-o", "m"], cwd=src, check=True)
    subprocess.run(["./m"], cwd=src, check=True)


@pytest.mark.skipif(
    shutil.which("lcov") is None or shutil.which("gcc") is None, reason="needs lcov+gcc"
)
@pytest.mark.asyncio
async def test_real_capture_with_sources_absent(tmp_path, local_host):
    src = tmp_path / "src"
    src.mkdir()
    (src / "m.c").write_text("int main(void){return 0;}\n")
    _build_and_run(src)
    moved = tmp_path / "fetched/x/m.gcda"
    moved.parent.mkdir(parents=True)
    shutil.move(src / "m.gcda", moved)
    shutil.move(src / "m.c", tmp_path / "m.c.away")
    out = await LcovMerger(local_host).capture_pairs(
        [Pair(gcda=moved, rel=PurePosixPath("x/m.gcda"), gcno=src / "m.gcno")],
        tmp_path / "st",
        tmp_path / "o.info",
    )
    assert "DA:1,1" in out.read_text()
