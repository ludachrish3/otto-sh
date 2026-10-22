"""A fetched product keeps each ``.gcda`` at its path relative to ``cov_dir``."""

import asyncio
import io
import logging
import re
import tarfile
from pathlib import Path

import pytest

from otto.coverage.fetcher.remote import _fetch_one_product, safe_extract
from otto.result import CommandResult, Result
from otto.utils import Status


class _Product:
    name = "app"
    cov_dir = "/var/cov/app"

    async def prepare_coverage(self, host):
        return Result(Status.Success)


class _Host:
    """A host whose ``exec``/``get`` play the archive fetch.

    The archive command answers the path its ``mktemp`` would have made.
    *tar_bytes* is what a ``get`` of the archive delivers (``None``: the get
    answers success but writes nothing).
    """

    id = "h"

    def __init__(
        self,
        tar_bytes: bytes | None,
        *,
        get_status: Status = Status.Success,
        rm_raises: bool = False,
    ):
        self.tar_bytes = tar_bytes
        self.get_status = get_status
        self.rm_raises = rm_raises
        self.cmds: list[str] = []
        self.gets: list[tuple[list[str], Path]] = []

    async def exec(self, cmd, timeout=60, **kw):
        cmd = str(cmd)
        self.cmds.append(cmd)
        if cmd.startswith("rm -f") and self.rm_raises:
            raise RuntimeError("session closed")
        token = re.search(r"otto-fetch-[0-9a-f]+", cmd)
        if "tar -cf" in cmd and token is not None:
            # What mktemp would print for the command's own template.
            path = f"/tmp/{token.group(0)}.Ab12Cd"
            return CommandResult(Status.Success, value=f"otto-archive:{path}\n", retcode=0)
        return CommandResult(Status.Success, value="", retcode=0)

    async def get(self, files, dest, show_progress=False):
        self.gets.append(([str(f) for f in files], Path(dest)))
        if self.get_status is not Status.Success:
            return Result(self.get_status, msg="boom")
        for f in files:
            if self.tar_bytes is not None:
                (Path(dest) / Path(f).name).write_bytes(self.tar_bytes)
        return Result(Status.Success)


_RM = re.compile(r'rm -f "\$\{TMPDIR:-/tmp\}"/otto-fetch-[0-9a-f]+\.\*')
"""The removal of a fetch's temp archive, whatever path mktemp gave it."""


def _rm(token: str) -> str:
    return f'rm -f "${{TMPDIR:-/tmp}}"/{token}.*'


def _tar(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.mark.asyncio
async def test_fetch_keeps_paths_relative_to_cov_dir(tmp_path):
    host = _Host(_tar({"./a/main.cpp.gcda": b"1", "./b/main.cpp.gcda": b"2"}))
    dest = await _fetch_one_product(host, _Product(), tmp_path)
    assert dest is not None
    assert (dest / "a/main.cpp.gcda").read_bytes() == b"1"
    assert (dest / "b/main.cpp.gcda").read_bytes() == b"2"
    assert not list(dest.glob("*.tar"))
    (archive,) = [c for c in host.cmds if "tar -cf" in c]
    token = re.search(r"otto-fetch-[0-9a-f]+", archive).group(0)
    assert archive == (
        "! test -d /var/cov/app && echo otto-no-cov-dir || "
        f'(t=$(mktemp "${{TMPDIR:-/tmp}}/{token}.XXXXXX") && echo "otto-archive:$t" '
        '&& cd /var/cov/app && tar -cf "$t" .)'
    )
    assert host.gets[0][0] == [f"/tmp/{token}.Ab12Cd"]
    assert host.cmds[-1] == _rm(token)


@pytest.mark.asyncio
async def test_empty_archive_is_no_data(tmp_path):
    host = _Host(_tar({}))
    assert await _fetch_one_product(host, _Product(), tmp_path) is None
    assert not (tmp_path / "h" / "app").exists()


@pytest.mark.asyncio
async def test_failed_transfer_with_preexisting_dest_is_not_a_fetch(tmp_path, caplog):
    """Stale counters already in the leaf must never be reported as fetched.

    A failed ``get`` is a failed fetch — even when the leaf it would have filled
    already holds ``.gcda`` from an earlier collection, which a report (or a
    clean that trusts "fetched") would otherwise take for this run's counters.
    """
    stale = tmp_path / "h" / "app" / "old" / "main.cpp.gcda"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"stale")
    host = _Host(_tar({"./a/main.cpp.gcda": b"1"}), get_status=Status.Error)

    with caplog.at_level(logging.WARNING, logger="otto.coverage.fetcher.remote"):
        assert await _fetch_one_product(host, _Product(), tmp_path) is None

    assert not (tmp_path / "h" / "app").exists()
    assert "boom" in caplog.text
    assert any(_RM.fullmatch(c) for c in host.cmds)


@pytest.mark.asyncio
async def test_a_successful_fetch_replaces_a_preexisting_dest(tmp_path):
    """Each fetch stages into a fresh leaf: nothing from an earlier one survives."""
    stale = tmp_path / "h" / "app" / "old" / "main.cpp.gcda"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"stale")
    host = _Host(_tar({"./a/main.cpp.gcda": b"1"}))

    dest = await _fetch_one_product(host, _Product(), tmp_path)

    assert dest is not None
    assert sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*.gcda")) == [
        "a/main.cpp.gcda"
    ]


@pytest.mark.parametrize("use_filter", [True, False])
def test_safe_extract_refuses_escapes(tmp_path, monkeypatch, use_filter):
    if not use_filter:
        monkeypatch.delattr(tarfile, "data_filter", raising=False)
    art = tmp_path / "x.tar"
    art.write_bytes(_tar({"../evil.gcda": b"x"}))
    with pytest.raises((tarfile.TarError, ValueError)):
        safe_extract(art, tmp_path / "out")
    assert not (tmp_path / "evil.gcda").exists()


@pytest.mark.parametrize("use_filter", [True, False])
def test_safe_extract_refuses_links(tmp_path, monkeypatch, use_filter):
    """Counters are regular files; a link in the archive is refused outright."""
    if not use_filter:
        monkeypatch.delattr(tarfile, "data_filter", raising=False)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        info = tarfile.TarInfo("./d/a.gcda")
        info.type = tarfile.SYMTYPE
        info.linkname = "b.gcda"
        tf.addfile(info)
    art = tmp_path / "x.tar"
    art.write_bytes(buf.getvalue())
    with pytest.raises((tarfile.TarError, ValueError)):
        safe_extract(art, tmp_path / "out")
    assert not (tmp_path / "out" / "d" / "a.gcda").is_symlink()


@pytest.mark.parametrize("use_filter", [True, False])
def test_safe_extract_extracts_regular_files(tmp_path, monkeypatch, use_filter):
    if not use_filter:
        monkeypatch.delattr(tarfile, "data_filter", raising=False)
    art = tmp_path / "x.tar"
    art.write_bytes(_tar({"./d/a.gcda": b"1"}))
    safe_extract(art, tmp_path / "out")
    assert (tmp_path / "out/d/a.gcda").read_bytes() == b"1"


@pytest.mark.parametrize("use_filter", [True, False])
def test_safe_extract_with_a_suffix_keeps_only_matching_files(tmp_path, monkeypatch, use_filter):
    """Members not named ``*<suffix>`` are skipped before any check, so a
    link or log elsewhere in a cov_dir neither lands nor fails the extract."""
    if not use_filter:
        monkeypatch.delattr(tarfile, "data_filter", raising=False)
    art = tmp_path / "a.tar"
    with tarfile.open(art, "w") as tf:
        for name, data in {
            "./a/x.gcda": b"1",
            "./run.log": b"2",
            "./other.tar": b"",
        }.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo("./latest")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        tf.addfile(link)
    safe_extract(art, tmp_path / "out", suffix=".gcda")
    assert sorted(
        p.relative_to(tmp_path / "out").as_posix()
        for p in (tmp_path / "out").rglob("*")
        if p.is_file()
    ) == ["a/x.gcda"]


@pytest.mark.asyncio
async def test_a_success_that_delivered_no_archive_is_a_failed_fetch(tmp_path, caplog):
    host = _Host(None)
    with caplog.at_level(logging.WARNING, logger="otto.coverage.fetcher.remote"):
        assert await _fetch_one_product(host, _Product(), tmp_path) is None
    assert "delivered no archive" in caplog.text
    assert not (tmp_path / "h").exists()


@pytest.mark.asyncio
async def test_a_failing_remote_rm_does_not_mask_the_transfer_error(tmp_path):
    class _Raising(_Host):
        async def get(self, files, dest, show_progress=False):
            raise ConnectionResetError("transport went away")

    host = _Raising(b"", rm_raises=True)
    with pytest.raises(ConnectionResetError, match="went away"):
        await _fetch_one_product(host, _Product(), tmp_path)
    assert any(_RM.fullmatch(c) for c in host.cmds)


@pytest.mark.asyncio
async def test_a_failing_stage_dir_reset_still_removes_the_remote_archive(tmp_path):
    """The stage dir is set up inside the guarded block: its failure surfaces,
    and the host-side archive is still removed."""
    staging_root = tmp_path / "not-a-dir"
    staging_root.write_text("")
    host = _Host(_tar({"./a/main.cpp.gcda": b"1"}))
    with pytest.raises(NotADirectoryError):
        await _fetch_one_product(host, _Product(), staging_root)
    assert any(_RM.fullmatch(c) for c in host.cmds)
    assert host.gets == []


class _ShellHost:
    """A host whose ``exec`` runs the real command in a local shell and whose ``get`` copies.

    *env* is the remote environment (``TMPDIR``); every archive it hands over
    is recorded with its member names.
    """

    id = "h"

    def __init__(self, env: dict[str, str]):
        self.env = env
        self.cmds: list[str] = []
        self.gets: list[list[str]] = []
        self.members: list[str] = []

    async def exec(self, cmd, timeout=60, **kw):
        import os

        cmd = str(cmd)
        self.cmds.append(cmd)
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, **self.env},
        )
        out, _ = await proc.communicate()
        status = Status.Success if proc.returncode == 0 else Status.Error
        return CommandResult(status, value=out.decode(), retcode=proc.returncode)

    async def get(self, files, dest, show_progress=False):
        import shutil

        self.gets.append([str(f) for f in files])
        for f in files:
            with tarfile.open(f) as tf:
                self.members.extend(tf.getnames())
            shutil.copy(f, Path(dest) / Path(f).name)
        return Result(Status.Success)


def _here(cov_dir: Path) -> _Product:
    """A product whose ``cov_dir`` is *cov_dir* on the (local-shell) host."""

    class _Here(_Product):
        pass

    _Here.cov_dir = str(cov_dir)
    return _Here()


def _files(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


def _counters(tmp_path: Path) -> tuple[Path, Path]:
    """A cov_dir holding one counter and a log, and an empty host temp dir."""
    cov_dir = tmp_path / "my cov"  # a space: the quoting is load-bearing
    (cov_dir / "a").mkdir(parents=True)
    (cov_dir / "a" / "one.gcda").write_bytes(b"1")
    (cov_dir / "a" / "run.log").write_text("x")
    tmpdir = tmp_path / "host tmp"
    tmpdir.mkdir()
    return cov_dir, tmpdir


@pytest.mark.asyncio
async def test_the_archive_is_made_by_mktemp_in_the_hosts_temp_dir(tmp_path):
    """One plain tar of the whole cov_dir into a file mktemp made under the
    host's ``$TMPDIR``; only ``.gcda`` is kept, and the archive is removed."""
    cov_dir, tmpdir = _counters(tmp_path)
    host = _ShellHost({"TMPDIR": str(tmpdir)})

    dest = await _fetch_one_product(host, _here(cov_dir), tmp_path / "staging")

    assert dest is not None
    assert _files(dest) == ["a/one.gcda"]
    ((archive,),) = host.gets
    assert re.fullmatch(rf"{re.escape(str(tmpdir))}/otto-fetch-[0-9a-f]+\.\w{{6}}", archive)
    assert "./a/run.log" in host.members  # the whole cov_dir went into the archive
    assert not any("otto-fetch" in m for m in host.members)
    assert list(tmpdir.iterdir()) == []
    assert sorted(p.name for p in cov_dir.iterdir()) == ["a"]  # nothing written there


@pytest.mark.asyncio
async def test_a_cov_dir_the_fetching_user_cannot_write_archives_fine(tmp_path):
    """A root-owned but readable cov_dir (0755 dirs, 0644 counters): the
    archive goes to the temp dir, so read access is all a fetch needs."""
    import os

    cov_dir, tmpdir = _counters(tmp_path)
    for d in (cov_dir, cov_dir / "a"):
        d.chmod(0o555)
    if os.access(cov_dir, os.W_OK):  # root writes through a read-only mode
        for d in (cov_dir, cov_dir / "a"):
            d.chmod(0o755)
        pytest.skip("running as root: a read-only directory cannot be made")
    host = _ShellHost({"TMPDIR": str(tmpdir)})
    try:
        dest = await _fetch_one_product(host, _here(cov_dir), tmp_path / "staging")
    finally:
        for d in (cov_dir, cov_dir / "a"):
            d.chmod(0o755)

    assert dest is not None
    assert _files(dest) == ["a/one.gcda"]
    assert list(tmpdir.iterdir()) == []


@pytest.mark.asyncio
async def test_an_unreadable_subdir_is_a_failed_fetch_not_a_partial_one(tmp_path, caplog):
    """tar reports what it could not read in its exit status: the product's
    fetch fails, nothing is fetched, and the temp archive is removed."""
    import os

    cov_dir, tmpdir = _counters(tmp_path)
    locked = cov_dir / "z"
    locked.mkdir()
    (locked / "two.gcda").write_bytes(b"2")
    locked.chmod(0)
    if os.access(locked, os.R_OK):  # root reads through a mode of 0
        locked.chmod(0o755)
        pytest.skip("running as root: an unreadable directory cannot be made")

    host = _ShellHost({"TMPDIR": str(tmpdir)})
    try:
        with caplog.at_level(logging.WARNING, logger="otto.coverage.fetcher.remote"):
            dest = await _fetch_one_product(host, _here(cov_dir), tmp_path / "staging")
    finally:
        locked.chmod(0o755)

    assert dest is None
    assert not (tmp_path / "staging" / "h").exists()
    assert "Permission denied" in caplog.text
    assert host.gets == []
    assert list(tmpdir.iterdir()) == []


@pytest.mark.asyncio
async def test_a_temp_dir_mktemp_cannot_use_is_a_failed_fetch(tmp_path, caplog):
    """No temp file, no archive: the fetch fails loudly with mktemp's own
    message, and nothing is transferred."""
    cov_dir, _tmpdir = _counters(tmp_path)
    host = _ShellHost({"TMPDIR": str(tmp_path / "no such dir")})

    with caplog.at_level(logging.WARNING, logger="otto.coverage.fetcher.remote"):
        dest = await _fetch_one_product(host, _here(cov_dir), tmp_path / "staging")

    assert dest is None
    assert host.gets == []
    assert "Failed to archive .gcda" in caplog.text
    assert "mktemp" in caplog.text


class _ArchiveRaises(_Host):
    """The archive command raises (a dropped transport, a cancelled fetch)."""

    def __init__(self, exc: BaseException, **kw):
        super().__init__(None, **kw)
        self.exc = exc

    async def exec(self, cmd, timeout=60, **kw):
        if "tar -cf" in str(cmd):
            self.cmds.append(str(cmd))
            raise self.exc
        return await super().exec(cmd, timeout=timeout, **kw)


@pytest.mark.asyncio
@pytest.mark.parametrize("rm_raises", [False, True])
@pytest.mark.parametrize("exc", [ConnectionResetError("went away"), asyncio.CancelledError()])
async def test_a_raised_archive_command_still_removes_the_archive(tmp_path, exc, rm_raises):
    """The host-side archive is removed, and a failing remove never masks the
    exception being unwound."""
    host = _ArchiveRaises(exc, rm_raises=rm_raises)
    with pytest.raises(type(exc)):
        await _fetch_one_product(host, _Product(), tmp_path)
    (archive,) = [c for c in host.cmds if "tar -cf" in c]
    token = re.search(r"otto-fetch-[0-9a-f]+", archive)
    assert token is not None
    assert _rm(token.group(0)) in host.cmds


@pytest.mark.asyncio
async def test_a_temp_dir_named_like_the_no_cov_dir_sentinel_still_fetches(tmp_path):
    """The sentinel is a whole line, never a substring: a ``$TMPDIR`` whose
    path contains it (echoed on the archive line) is fetched and cleaned up."""
    cov_dir, _ = _counters(tmp_path)
    tmpdir = tmp_path / "otto-no-cov-dir"
    tmpdir.mkdir()
    host = _ShellHost({"TMPDIR": str(tmpdir)})

    dest = await _fetch_one_product(host, _here(cov_dir), tmp_path / "staging")

    assert dest is not None
    assert _files(dest) == ["a/one.gcda"]
    assert list(tmpdir.iterdir()) == []


class _Chatty(_Host):
    """An archive command whose output also mentions the sentinel, mid-line."""

    async def exec(self, cmd, timeout=60, **kw):
        result = await super().exec(cmd, timeout=timeout, **kw)
        if "tar -cf" in str(cmd):
            noise = "motd: see otto-no-cov-dir in the docs\n"
            return CommandResult(Status.Success, value=noise + result.value, retcode=0)
        return result


@pytest.mark.asyncio
async def test_unrelated_output_mentioning_the_sentinel_still_fetches(tmp_path):
    host = _Chatty(_tar({"./a/x.gcda": b"1"}))

    dest = await _fetch_one_product(host, _Product(), tmp_path)

    assert dest is not None
    assert _files(dest) == ["a/x.gcda"]
    assert any(_RM.fullmatch(c) for c in host.cmds)


class _NoCovDir(_Host):
    async def exec(self, cmd, timeout=60, **kw):
        if "tar -cf" in str(cmd):
            self.cmds.append(str(cmd))
            return CommandResult(Status.Success, value="otto-no-cov-dir\n", retcode=0)
        return await super().exec(cmd, timeout=timeout, **kw)


@pytest.mark.asyncio
async def test_a_missing_cov_dir_is_quiet_and_still_cleans_up(tmp_path, caplog):
    """Every executed archive command is followed by the removal, whatever it answered."""
    host = _NoCovDir(None)
    with caplog.at_level(logging.WARNING, logger="otto.coverage.fetcher.remote"):
        assert await _fetch_one_product(host, _Product(), tmp_path) is None
    assert caplog.text == ""
    assert host.gets == []
    assert any(_RM.fullmatch(c) for c in host.cmds)


class _NoArchivePath(_Host):
    """The archive command answers with bracketed text and no archive path."""

    async def exec(self, cmd, timeout=60, **kw):
        if "tar -cf" in str(cmd):
            self.cmds.append(str(cmd))
            return CommandResult(Status.Success, value="[/x] [red]oops\n", retcode=0)
        return await super().exec(cmd, timeout=timeout, **kw)


@pytest.mark.asyncio
async def test_a_no_archive_path_warning_keeps_the_devices_brackets(tmp_path, caplog):
    from tests._fixtures.log_text import shown

    host = _NoArchivePath(None)
    with caplog.at_level(logging.WARNING, logger="otto.coverage.fetcher.remote"):
        assert await _fetch_one_product(host, _Product(), tmp_path) is None
    (record,) = [r for r in caplog.records if "no archive path" in r.getMessage()]
    assert shown(record).endswith("'[/x] [red]oops'")
