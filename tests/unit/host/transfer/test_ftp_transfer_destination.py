"""An ftp transfer answers the same with a progress bar and without one.

``show_progress`` selects whether a handler is called, never whether a transfer
succeeds. A non-recursive ftp put or get does not create the destination
directory and does not recurse into a directory source -- creating directories is
the job of the recursive transfer -- and that holds in both modes. Every test
here is parametrized over ``show_progress`` so both modes drive the one function
with the one input and must give the one outcome.

The aioftp client is a stand-in over a real directory tree: "remote" paths are
absolute paths under ``tmp_path``, and the stand-in refuses what a real server
refuses (a STOR into a missing directory, a RETR of a directory). It records the
control commands otto sends through ``command``.
"""

import asyncio
from pathlib import Path
from typing import Any, BinaryIO
from unittest.mock import AsyncMock

import aioftp
import pytest

from otto.host.transfer.ftp import _FtpFileTransfer
from otto.utils import Status


class _DataWriter:
    """The data connection's writer: bytes written land in ``sink`` (a STOR target)."""

    def __init__(self) -> None:
        self.sink: BinaryIO | None = None
        self.closed = False

    def write(self, data: bytes) -> None:
        assert self.sink is not None, "bytes written on a data connection no STOR accepted"
        self.sink.write(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True
        if self.sink is not None:
            self.sink.close()

    async def wait_closed(self) -> None:
        return None


class _FsFtpClient:
    """The ``aioftp.Client`` surface the backend uses, over the local filesystem.

    ``upload`` and ``download`` do what aioftp's do -- make the destination's
    parent (and, for a directory source, the tree) -- so a backend that reached
    for them again would create directories here and the "creates nothing"
    tests would catch it. Deliberately NOT a ``MagicMock``.
    """

    def __init__(self, refuse_upload_of: str | None = None) -> None:
        self.commands: list[str] = []
        self.uploaded: list[str] = []
        self._refuse_upload_of = refuse_upload_of
        self.retrs: list[str] = []
        # True while a final 226/426 reply is unread on the control channel.
        self.pending_reply = False
        self.desyncs: list[str] = []
        # Every data connection opened, with its reader; a refused STOR/RETR must
        # not leave its writer open.
        self.data_connections: list[tuple[asyncio.StreamReader, _DataWriter]] = []
        # What aioftp's stream class reads off the client.
        self.throttle = aioftp.StreamThrottle.from_limits()
        self.socket_timeout: float | None = None

    def send(self, verb: str) -> None:
        """Record a command; one sent while a reply is unread is a desync."""
        if self.pending_reply:
            self.desyncs.append(verb)

    async def get_passive_connection(
        self, conn_type: str = "I"
    ) -> tuple[asyncio.StreamReader, _DataWriter]:
        """Open a data connection BEFORE any STOR/RETR, as aioftp does."""
        reader, writer = asyncio.StreamReader(), _DataWriter()
        self.data_connections.append((reader, writer))
        return reader, writer

    async def command(
        self, cmd: str | None, expected: str = "", wait: str = "", censor_after: None = None
    ) -> tuple[str, list[str]]:
        if cmd is None:
            # The final reply, read by the stream's clean exit.
            self.pending_reply = False
            return "226", []
        verb, _, path = cmd.partition(" ")
        if verb in ("STOR", "RETR"):
            return self._transfer_command(verb, Path(path))
        self.send(cmd)
        self.commands.append(cmd)
        assert verb == "SIZE", cmd
        return "213", [str(Path(path).stat().st_size)]

    def _transfer_command(self, verb: str, path: Path) -> tuple[str, list[str]]:
        self.send(verb)
        reader, writer = self.data_connections[-1]
        if verb == "STOR":
            self.uploaded.append(str(path))
            refused = self._refuse_upload_of is not None and path.name == self._refuse_upload_of
            if refused or not path.parent.is_dir():
                raise aioftp.StatusCodeError("150", "550", ["Failed to open file."])
            # A real server has created (or truncated) the file once it accepts STOR.
            writer.sink = path.open("wb")
        else:
            self.retrs.append(str(path))
            if not path.is_file():
                raise aioftp.StatusCodeError("150", "550", ["Failed to open file."])
            reader.feed_data(path.read_bytes())
            reader.feed_eof()
        self.pending_reply = True
        return "150", []

    async def upload(
        self, source: str, destination: str = "", *, write_into: bool = False, **_: Any
    ) -> None:
        target = Path(destination)
        if not write_into:
            target = target / Path(source).name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(Path(source).read_bytes())

    async def download(
        self, source: str, destination: str = "", *, write_into: bool = False, **_: Any
    ) -> None:
        src, target = Path(source), Path(destination)
        if not write_into:
            target = target / src.name
        if src.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            for child in src.rglob("*"):
                if child.is_file():
                    out = target / child.relative_to(src)
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.write_bytes(child.read_bytes())
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(src.read_bytes())


class _FakeConnections:
    def __init__(self, client: _FsFtpClient) -> None:
        self._client = client

    async def ftp(self) -> _FsFtpClient:
        return self._client


def _backend(client: _FsFtpClient) -> _FtpFileTransfer:
    """The REAL backend over the stand-in client. Nothing about otto is faked."""
    return _FtpFileTransfer(
        connections=_FakeConnections(client),  # ty: ignore[invalid-argument-type]
        name="dut",
        exec_cmd=AsyncMock(),
    )


class _Calls:
    """A progress handler that records its calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int, int]] = []

    def factory(self) -> Any:
        return lambda *args: self.calls.append(args)


def _progress(show_progress: bool) -> tuple[Any, _Calls]:
    calls = _Calls()
    return (calls.factory if show_progress else None), calls


show_progress_modes = pytest.mark.parametrize("show_progress", [False, True], ids=["bare", "bar"])


@show_progress_modes
@pytest.mark.asyncio
async def test_a_put_into_an_existing_directory_lands_the_bytes(
    tmp_path: Path, show_progress: bool
) -> None:
    src = tmp_path / "payload.bin"
    src.write_bytes(b"x" * 20)
    dest_dir = tmp_path / "remote"
    dest_dir.mkdir()
    factory, calls = _progress(show_progress)

    result = await _backend(_FsFtpClient()).run_put([src], dest_dir, progress_factory=factory)

    assert result[src].status is Status.Success, result[src].msg
    assert (dest_dir / "payload.bin").read_bytes() == b"x" * 20
    assert bool(calls.calls) is show_progress


@show_progress_modes
@pytest.mark.asyncio
async def test_a_get_into_an_existing_directory_lands_the_bytes(
    tmp_path: Path, show_progress: bool
) -> None:
    src = tmp_path / "remote" / "payload.bin"
    src.parent.mkdir()
    src.write_bytes(b"y" * 20)
    dest_dir = tmp_path / "local"
    dest_dir.mkdir()
    factory, calls = _progress(show_progress)

    result = await _backend(_FsFtpClient()).run_get([src], dest_dir, progress_factory=factory)

    assert result[src].status is Status.Success, result[src].msg
    landed = result[src].value
    assert landed == dest_dir / "payload.bin"
    assert landed.read_bytes() == b"y" * 20
    assert bool(calls.calls) is show_progress


@show_progress_modes
@pytest.mark.asyncio
async def test_a_put_to_a_missing_directory_fails_and_creates_nothing(
    tmp_path: Path, show_progress: bool
) -> None:
    src = tmp_path / "payload.bin"
    src.write_bytes(b"x" * 20)
    dest_dir = tmp_path / "remote" / "absent"
    factory, _ = _progress(show_progress)

    result = await _backend(_FsFtpClient()).run_put([src], dest_dir, progress_factory=factory)

    assert result[src].status is Status.Error, result[src].msg
    assert str(dest_dir / "payload.bin") in (result[src].msg or ""), result[src].msg
    assert not dest_dir.exists(), "a put without -r must not create the destination directory"


@show_progress_modes
@pytest.mark.asyncio
async def test_a_get_to_a_missing_directory_fails_and_creates_nothing(
    tmp_path: Path, show_progress: bool
) -> None:
    src = tmp_path / "remote" / "payload.bin"
    src.parent.mkdir()
    src.write_bytes(b"y" * 20)
    dest_dir = tmp_path / "local" / "absent"
    factory, _ = _progress(show_progress)

    result = await _backend(_FsFtpClient()).run_get([src], dest_dir, progress_factory=factory)

    assert result[src].status is Status.Error, result[src].msg
    assert str(dest_dir) in (result[src].msg or ""), result[src].msg
    assert not dest_dir.exists(), "a get without -r must not create the destination directory"
    assert not dest_dir.parent.exists()


@show_progress_modes
@pytest.mark.asyncio
async def test_a_get_of_a_remote_directory_fails_and_fetches_nothing(
    tmp_path: Path, show_progress: bool
) -> None:
    src = tmp_path / "remote" / "tree"
    src.mkdir(parents=True)
    (src / "inner.txt").write_text("inner")
    dest_dir = tmp_path / "local"
    dest_dir.mkdir()
    factory, _ = _progress(show_progress)

    result = await _backend(_FsFtpClient()).run_get([src], dest_dir, progress_factory=factory)

    assert result[src].status is Status.Error, result[src].msg
    assert list(dest_dir.iterdir()) == [], "a directory source must not be fetched without -r"


@show_progress_modes
@pytest.mark.asyncio
async def test_a_get_into_a_missing_directory_sends_no_retr_and_does_not_desync(
    tmp_path: Path, show_progress: bool
) -> None:
    remote = tmp_path / "remote"
    remote.mkdir()
    files = [remote / "a.bin", remote / "b.bin"]
    for f in files:
        f.write_bytes(b"y" * 20)
    dest_dir = tmp_path / "local" / "absent"
    factory, _ = _progress(show_progress)
    client = _FsFtpClient()

    result = await _backend(client).run_get(
        files, dest_dir, progress_factory=factory, concurrent=False
    )

    for f in files:
        assert result[f].status is Status.Error, result[f].msg
        assert str(dest_dir) in (result[f].msg or ""), result[f].msg
    assert client.retrs == [], "the local side is decided before the server is asked"
    assert client.desyncs == []
    assert not dest_dir.parent.exists()


@show_progress_modes
@pytest.mark.asyncio
async def test_a_get_after_a_failed_get_on_the_same_client_succeeds(
    tmp_path: Path, show_progress: bool
) -> None:
    src = tmp_path / "remote" / "payload.bin"
    src.parent.mkdir()
    src.write_bytes(b"y" * 20)
    missing = tmp_path / "local" / "absent"
    good = tmp_path / "good"
    good.mkdir()
    factory, _ = _progress(show_progress)
    client = _FsFtpClient()
    backend = _backend(client)

    failed = await backend.run_get([src], missing, progress_factory=factory)
    landed = await backend.run_get([src], good, progress_factory=factory)

    assert failed[src].status is Status.Error
    assert landed[src].status is Status.Success, landed[src].msg
    assert (good / "payload.bin").read_bytes() == b"y" * 20
    assert client.desyncs == []


@show_progress_modes
@pytest.mark.asyncio
async def test_a_refused_get_names_the_source_path_once(
    tmp_path: Path, show_progress: bool
) -> None:
    tree = tmp_path / "remote" / "tree"
    tree.mkdir(parents=True)
    absent = tmp_path / "remote" / "absent.bin"
    dest_dir = tmp_path / "local"
    dest_dir.mkdir()
    factory, _ = _progress(show_progress)

    result = await _backend(_FsFtpClient()).run_get(
        [tree, absent], dest_dir, progress_factory=factory, concurrent=False
    )

    for src in (tree, absent):
        assert result[src].status is Status.Error, result[src].msg
        # The dispatcher prints the source; the wrap does not repeat it.
        assert (result[src].msg or "").count(str(src)) == 1, result[src].msg
        assert "the server refused to send it" in (result[src].msg or ""), result[src].msg


@pytest.mark.asyncio
async def test_without_progress_no_size_command_is_sent(tmp_path: Path) -> None:
    src = tmp_path / "remote" / "payload.bin"
    src.parent.mkdir()
    src.write_bytes(b"y" * 20)
    dest_dir = tmp_path / "local"
    dest_dir.mkdir()
    client = _FsFtpClient()

    await _backend(client).run_get([src], dest_dir, progress_factory=None)

    assert client.commands == []


@pytest.mark.asyncio
async def test_with_progress_the_size_is_asked_once_and_reported(tmp_path: Path) -> None:
    src = tmp_path / "remote" / "payload.bin"
    src.parent.mkdir()
    src.write_bytes(b"y" * 20)
    dest_dir = tmp_path / "local"
    dest_dir.mkdir()
    client = _FsFtpClient()
    factory, calls = _progress(True)

    await _backend(client).run_get([src], dest_dir, progress_factory=factory)

    assert client.commands == [f"SIZE {src}"]
    assert calls.calls[-1] == (str(src), str(dest_dir / "payload.bin"), 20, 20)


@show_progress_modes
@pytest.mark.asyncio
async def test_a_refused_upload_is_its_own_error_and_the_next_file_still_lands(
    tmp_path: Path, show_progress: bool
) -> None:
    """One file's refusal must not answer for the batch."""
    first = tmp_path / "a.bin"
    first.write_bytes(b"a" * 32)
    second = tmp_path / "b.bin"
    second.write_bytes(b"b" * 32)
    dest_dir = tmp_path / "remote"
    dest_dir.mkdir()
    factory, _ = _progress(show_progress)

    client = _FsFtpClient(refuse_upload_of="a.bin")
    result = await _backend(client).run_put(
        [first, second], dest_dir, progress_factory=factory, concurrent=False
    )

    assert result[first].status is Status.Error
    assert "550" in (result[first].msg or ""), result[first].msg
    assert result[second].status is Status.Success, result[second].msg
    assert not any(r.status is Status.Skipped for r in result.values())
    assert [Path(p).name for p in client.uploaded] == ["a.bin", "b.bin"]


def test_ftp_transfer_is_one_file_at_a_time() -> None:
    """An FTP transfer keeps exactly one file in flight.

    Inheritance from the base makes it true today; the point of writing it
    down is that a future override fails HERE, beside the promise. The
    families table in ``docs/api/host/transfer.rst`` and both
    ``Host.put``/``Host.get`` docstrings name this backend as one that runs
    one file at a time whatever ``concurrent`` says, and nothing else checks
    that claim against the class.
    """
    assert _backend(_FsFtpClient()).concurrency_limit == 1


@show_progress_modes
@pytest.mark.asyncio
async def test_a_put_of_a_missing_source_never_reaches_the_server(
    tmp_path: Path, show_progress: bool
) -> None:
    missing = tmp_path / "absent.bin"
    good = tmp_path / "good.bin"
    good.write_bytes(b"g" * 20)
    dest_dir = tmp_path / "remote"
    dest_dir.mkdir()
    (dest_dir / "absent.bin").write_bytes(b"keep")
    factory, _ = _progress(show_progress)
    client = _FsFtpClient()

    result = await _backend(client).run_put(
        [missing, good], dest_dir, progress_factory=factory, concurrent=False
    )

    assert result[missing].status is Status.Error, result[missing].msg
    assert str(missing) in (result[missing].msg or ""), result[missing].msg
    assert [Path(p).name for p in client.uploaded] == ["good.bin"], "no STOR for the missing source"
    assert (dest_dir / "absent.bin").read_bytes() == b"keep"
    assert result[good].status is Status.Success, result[good].msg
    assert (dest_dir / "good.bin").read_bytes() == b"g" * 20
    assert client.desyncs == []


@show_progress_modes
@pytest.mark.asyncio
async def test_a_get_onto_an_existing_directory_sends_no_retr_and_does_not_desync(
    tmp_path: Path, show_progress: bool
) -> None:
    remote = tmp_path / "remote"
    remote.mkdir()
    files = [remote / "a.bin", remote / "b.bin"]
    for f in files:
        f.write_bytes(b"y" * 20)
    dest_dir = tmp_path / "local"
    (dest_dir / "a.bin").mkdir(parents=True)
    factory, _ = _progress(show_progress)
    client = _FsFtpClient()

    result = await _backend(client).run_get(
        files, dest_dir, progress_factory=factory, concurrent=False
    )

    assert result[files[0]].status is Status.Error, result[files[0]].msg
    assert "existing directory" in (result[files[0]].msg or ""), result[files[0]].msg
    assert result[files[1]].status is Status.Success, result[files[1]].msg
    assert (dest_dir / "b.bin").read_bytes() == b"y" * 20
    assert client.retrs == [str(files[1])], "no RETR for the file whose destination is a directory"
    assert client.desyncs == []


@show_progress_modes
@pytest.mark.asyncio
async def test_a_get_whose_parent_is_a_file_says_it_is_not_a_directory(
    tmp_path: Path, show_progress: bool
) -> None:
    src = tmp_path / "remote" / "payload.bin"
    src.parent.mkdir()
    src.write_bytes(b"y" * 20)
    not_a_dir = tmp_path / "local"
    not_a_dir.write_bytes(b"file")
    factory, _ = _progress(show_progress)
    client = _FsFtpClient()

    result = await _backend(client).run_get([src], not_a_dir, progress_factory=factory)

    assert result[src].status is Status.Error, result[src].msg
    assert "is not a directory" in (result[src].msg or ""), result[src].msg
    assert client.retrs == []


@show_progress_modes
@pytest.mark.asyncio
async def test_a_refused_get_leaves_no_temp_file_and_keeps_an_existing_destination(
    tmp_path: Path, show_progress: bool
) -> None:
    src = tmp_path / "remote" / "tree"
    src.mkdir(parents=True)
    dest_dir = tmp_path / "local"
    dest_dir.mkdir()
    (dest_dir / "tree").write_bytes(b"old bytes")
    factory, _ = _progress(show_progress)

    result = await _backend(_FsFtpClient()).run_get([src], dest_dir, progress_factory=factory)

    assert result[src].status is Status.Error, result[src].msg
    assert [p.name for p in dest_dir.iterdir()] == ["tree"], "a failed get leaves no temp file"
    assert (dest_dir / "tree").read_bytes() == b"old bytes"


@show_progress_modes
@pytest.mark.asyncio
async def test_a_get_over_an_existing_file_replaces_it_and_leaves_no_temp_file(
    tmp_path: Path, show_progress: bool
) -> None:
    src = tmp_path / "remote" / "payload.bin"
    src.parent.mkdir()
    src.write_bytes(b"new" * 10)
    dest_dir = tmp_path / "local"
    dest_dir.mkdir()
    (dest_dir / "payload.bin").write_bytes(b"old")
    factory, _ = _progress(show_progress)

    result = await _backend(_FsFtpClient()).run_get([src], dest_dir, progress_factory=factory)

    assert result[src].status is Status.Success, result[src].msg
    assert (dest_dir / "payload.bin").read_bytes() == b"new" * 10
    assert [p.name for p in dest_dir.iterdir()] == ["payload.bin"]


@show_progress_modes
@pytest.mark.asyncio
async def test_a_refused_transfer_leaves_no_data_connection_open(
    tmp_path: Path, show_progress: bool
) -> None:
    """A STOR or RETR the server refuses closes the data connection opened for it."""
    src = tmp_path / "payload.bin"
    src.write_bytes(b"x" * 20)
    tree = tmp_path / "remote" / "tree"
    tree.mkdir(parents=True)
    dest = tmp_path / "local"
    dest.mkdir()
    factory, _ = _progress(show_progress)
    client = _FsFtpClient()
    backend = _backend(client)

    put = await backend.run_put([src], tmp_path / "absent", progress_factory=factory)
    get = await backend.run_get([tree], dest, progress_factory=factory)

    assert put[src].status is Status.Error
    assert get[tree].status is Status.Error
    assert len(client.data_connections) == 2
    assert all(writer.closed for _reader, writer in client.data_connections)
    assert client.desyncs == []
