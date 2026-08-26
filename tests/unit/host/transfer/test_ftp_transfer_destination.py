"""An ftp transfer must land the file at the path the caller named -- both ways.

``_put_files_ftp`` and ``_get_files_ftp`` each have two arms that reach aioftp
through different calls. With a progress handler they drive
``upload_stream(dst)`` / ``download_stream(src)``, which use the exact path they
are given. Without one they call ``upload(src, dst)`` / ``download(src, dst)``,
whose ``write_into`` parameter decides whether ``dst`` is the FILE or a
CONTAINER to put the file inside. Those are different contracts, and otto
computes ``dst`` once for both arms of both directions.

THE TWO DIRECTIONS FAIL DIFFERENTLY, which is why they are tested differently.
The PUT bug announced itself one step later -- the file was on the server at the
wrong path, so the next `get` 550'd. The GET bug does not: ``_get_files_ftp``
returns ``Result(Status.Success, value=dst)`` naming a path aioftp has just
turned into a LOCAL directory, so a caller that checks ``is_ok`` believes a file
arrived and a caller that reads the value gets ``IsADirectoryError``. A rule
restated is enough for the loud one; the quiet one is observed directly, because
aioftp's mkdir and write are both local and a stand-in can perform them for
real.

WHAT THIS PINS, AND WHY IT IS THE EFFECTIVE PATH RATHER THAN THE ARGUMENT. A
test that asserted ``upload`` was called with ``dest_dir / src.name`` would have
passed against the defect: that IS what it was called with. The bug was that
aioftp then appended the name a second time, because ``write_into`` defaults to
``False`` and that means "write source INTO destination". So these tests apply
aioftp's own rule to the recorded call and assert on the path the file actually
lands at. Two consequences worth stating:

* the pin survives either real fix -- ``upload(src, dst, write_into=True)`` or
  ``upload(src, dest_dir)`` -- because both put the bytes in the right place,
  and pinning a spelling instead would forbid one of them for no reason;
* it cannot be satisfied by a call that merely LOOKS right.

MEASURED, NOT HYPOTHETICAL (2026-08-26, conformance bed, all four GNU hosts).
Before the fix, ``put(..., show_progress=False)`` over ftp created a DIRECTORY
named after the file and wrote the bytes inside it --
``/tmp/gw2_prog-get.bin/gw2_prog-get.bin`` -- so the follow-up ``get`` of the
path the caller asked for failed with ``550 Failed to open file``. The leftover
directory then broke every later cell sharing that scratch path, because the
suite's cleanup issues ``rm``. One defect, twelve failures.

No network: the aioftp client is a recorder. The rule this file applies to its
recording is aioftp's, quoted at :func:`_effective_destination`.
"""

from pathlib import Path, PurePosixPath
from typing import Any
from unittest.mock import AsyncMock

import pytest
from typing_extensions import Self

from otto.host.transfer.ftp import FtpFileTransfer
from otto.utils import Status


class _RecordedUpload:
    """One ``aioftp.Client.upload`` / ``download`` call, as otto made it."""

    def __init__(self, source: str, destination: str, write_into: bool) -> None:
        self.source = source
        self.destination = destination
        self.write_into = write_into


class _RecordingFtpClient:
    """Records ``upload`` / ``upload_stream`` destinations and moves no bytes.

    Deliberately NOT a ``MagicMock``: this stands in for one class with two
    methods, and a mock would answer every other attribute too, so an arm that
    started reaching for something else on the client would go on passing here.
    """

    def __init__(self) -> None:
        self.uploads: list[_RecordedUpload] = []
        self.stream_destinations: list[str] = []

    async def upload(
        self, source: str, destination: str = "", *, write_into: bool = False, **_: Any
    ) -> None:
        # The signature mirrors aioftp's, INCLUDING the `write_into=False`
        # default, so a caller that omits the flag records the same value the
        # real client would apply.
        self.uploads.append(_RecordedUpload(source, destination, write_into))

    def upload_stream(self, destination: str, **_: Any) -> Any:
        self.stream_destinations.append(destination)
        return _NullStream()


class _NullStream:
    """An ``upload_stream`` context manager that accepts writes and keeps none."""

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> bool:
        return False

    async def write(self, _data: bytes) -> None:
        return None


class _FakeConnections:
    """A connection manager whose ``ftp()`` hands back the recorder."""

    def __init__(self, client: _RecordingFtpClient) -> None:
        self._client = client

    async def ftp(self) -> _RecordingFtpClient:
        return self._client


def _backend(client: _RecordingFtpClient) -> FtpFileTransfer:
    """The REAL backend over a recording client. Nothing about otto is faked."""
    return FtpFileTransfer(
        connections=_FakeConnections(client),  # ty: ignore[invalid-argument-type]
        name="dut",
        exec_cmd=AsyncMock(),
    )


def _effective_destination(call: _RecordedUpload) -> PurePosixPath:
    """Where aioftp actually writes, given what otto asked for.

    Both ``Client.upload`` and ``Client.download`` carry the SAME branch
    (``client.py``, and unchanged across the 0.x line this project pins)::

        if not write_into:
            destination = destination / source.name

    ``write_into`` is documented as "write source into destination", i.e. FALSE
    means *destination is a container*. This function is that branch and nothing
    else, so the assertions below are against aioftp's real behaviour rather
    than against a restatement of otto's intent --
    :func:`test_the_hand_mirrored_write_into_rule_still_matches_aioftp` pins the
    mirror against the real signatures.
    """
    destination = PurePosixPath(call.destination)
    if not call.write_into:
        destination = destination / Path(call.source).name
    return destination


@pytest.mark.asyncio
async def test_a_put_without_progress_writes_the_file_the_caller_asked_for(tmp_path: Path) -> None:
    """The defect, stated as the property it broke.

    RED before the fix: otto passed the already-joined ``/dest/payload.bin`` with
    ``write_into`` left at its default, so aioftp resolved
    ``/dest/payload.bin/payload.bin`` and this assertion reported exactly that
    path. GREEN after.
    """
    src = tmp_path / "payload.bin"
    src.write_bytes(b"x" * 64)
    dest_dir = Path("/remote/dest")

    client = _RecordingFtpClient()
    result = await _backend(client)._run_put([src], dest_dir, None)

    assert result[src].status is Status.Success, result[src].msg
    assert len(client.uploads) == 1, client.uploads
    assert not client.stream_destinations, (
        "the no-progress arm must not open an upload_stream; it took the other branch"
    )
    landed = _effective_destination(client.uploads[0])
    assert landed == PurePosixPath("/remote/dest/payload.bin"), (
        f"a put with progress OFF lands the bytes at {landed}, not at the path the "
        f"caller asked for -- aioftp appends source.name to a destination it treats "
        f"as a container, so an already-joined path is joined a second time"
    )


@pytest.mark.asyncio
async def test_both_put_arms_agree_on_where_the_file_lands(tmp_path: Path) -> None:
    """The invariant behind the defect: progress must not move the destination.

    ``show_progress`` selects an implementation, never a location. This is the
    assertion that would have caught the bug from either side, and it is the one
    that keeps the two arms tied if someone edits one of them later.
    """
    src = tmp_path / "payload.bin"
    src.write_bytes(b"y" * 64)
    dest_dir = Path("/remote/dest")

    without = _RecordingFtpClient()
    await _backend(without)._run_put([src], dest_dir, None)

    with_progress = _RecordingFtpClient()
    await _backend(with_progress)._run_put([src], dest_dir, lambda: lambda *_a: None)

    assert len(with_progress.stream_destinations) == 1, with_progress.stream_destinations
    streamed = PurePosixPath(with_progress.stream_destinations[0])
    uploaded = _effective_destination(without.uploads[0])
    assert uploaded == streamed, (
        f"the two put arms disagree about the destination: progress ON writes "
        f"{streamed}, progress OFF writes {uploaded}. show_progress selects an "
        f"implementation, not a location"
    )


@pytest.mark.asyncio
async def test_the_no_progress_arm_never_creates_a_directory_named_after_the_file(
    tmp_path: Path,
) -> None:
    """The user-visible consequence, pinned separately from the path itself.

    aioftp runs ``make_directory(destination.parent)`` before writing, so a
    destination one level too deep does not merely misplace the file -- it
    leaves a directory wearing the file's own name. That is the artefact that
    outlived the bed run's cleanup (``rm`` cannot remove a directory) and
    poisoned every later cell sharing the scratch path, so it is worth its own
    red rather than being folded into the path assertion above.
    """
    src = tmp_path / "payload.bin"
    src.write_bytes(b"z" * 64)
    dest_dir = Path("/remote/dest")

    client = _RecordingFtpClient()
    await _backend(client)._run_put([src], dest_dir, None)

    created = _effective_destination(client.uploads[0]).parent
    assert created == PurePosixPath("/remote/dest"), (
        f"aioftp will mkdir {created} before writing -- a directory named after the "
        f"file. Only the intended destination directory may be created"
    )


# ===========================================================================
# GET -- observed against a real filesystem, not restated
# ===========================================================================


class _LocalWritingFtpClient:
    """A ``download`` that performs aioftp's own two LOCAL operations, for real.

    aioftp's ``Client.download`` resolves the destination, then
    (``client.py:1285-1293``)::

        await self.path_io.mkdir(destination_path.parent, parents=True, exist_ok=True)
        async with self.path_io.open(destination_path, mode="wb") as file_out:
            ...write blocks...

    Both of those touch the LOCAL filesystem and neither needs a server, so this
    stand-in does exactly them against a real ``tmp_path``. That is what lets
    the GET tests assert ``dst.is_file()`` and read the bytes back -- a direct
    observation of what the caller is handed, rather than a second copy of the
    ``write_into`` rule sitting next to the first.

    It records the call too, so the path assertions can stay symmetric with the
    PUT ones.
    """

    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self.downloads: list[_RecordedUpload] = []
        self.stream_sources: list[str] = []

    async def download(
        self, source: str, destination: str = "", *, write_into: bool = False, **_: Any
    ) -> None:
        self.downloads.append(_RecordedUpload(source, destination, write_into))
        destination_path = Path(destination)
        if not write_into:
            destination_path = destination_path / PurePosixPath(source).name
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        destination_path.write_bytes(self._payload)

    def download_stream(self, source: str, **_: Any) -> Any:
        self.stream_sources.append(source)
        raise AssertionError("the no-progress arm must not open a download_stream")


def _get_backend(client: _LocalWritingFtpClient) -> FtpFileTransfer:
    return FtpFileTransfer(
        connections=_FakeConnections(client),  # ty: ignore[invalid-argument-type]
        name="dut",
        exec_cmd=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_a_get_without_progress_leaves_a_readable_file_at_the_result_path(
    tmp_path: Path,
) -> None:
    """The false ``Success``, observed rather than argued.

    RED before the fix, and note WHICH assertion carried it: the status was
    already ``Success`` and stayed ``Success``, because nothing in
    ``_get_files_ftp`` looks at what landed. What reddened is ``dst.is_file()``
    -- aioftp had made a DIRECTORY at exactly the path the ``Result`` names, and
    put the bytes one level inside it.
    """
    payload = b"downloaded bytes" * 8
    dest_dir = tmp_path / "landing"
    dest_dir.mkdir()
    src = PurePosixPath("/remote/src/payload.bin")

    client = _LocalWritingFtpClient(payload)
    result = await _get_backend(client)._run_get([Path(src)], dest_dir, None)

    landed = result[Path(src)]
    assert landed.status is Status.Success, landed.msg
    dst = landed.value
    assert dst.is_file(), (
        f"the result says Success and names {dst}, but that path is a "
        f"{'directory' if dst.is_dir() else 'not a file'} -- aioftp appended "
        f"source.name to a destination it treats as a container, so the bytes are "
        f"one level deeper and every caller that trusts this Result is wrong"
    )
    assert dst.read_bytes() == payload


@pytest.mark.asyncio
async def test_a_get_creates_no_directory_named_after_the_file(tmp_path: Path) -> None:
    """The litter, on the LOCAL filesystem this time.

    The PUT twin of this leaves a directory on the remote host that the
    conformance teardown's ``rm`` cannot remove. This one leaves it on the
    caller's own machine, where nothing is watching at all.
    """
    dest_dir = tmp_path / "landing"
    dest_dir.mkdir()
    src = PurePosixPath("/remote/src/payload.bin")

    client = _LocalWritingFtpClient(b"x" * 32)
    await _get_backend(client)._run_get([Path(src)], dest_dir, None)

    strays = [p for p in dest_dir.iterdir() if p.is_dir()]
    assert strays == [], (
        f"a get with progress OFF created {strays} under {dest_dir} -- directories "
        f"named after the files it was asked to fetch"
    )


@pytest.mark.asyncio
async def test_both_get_arms_agree_on_where_the_file_lands(tmp_path: Path) -> None:
    """The same invariant as the PUT pair: progress selects an implementation,
    never a location.

    The progress arm opens ``dst`` itself (``with dst.open("wb")``), so its
    destination is ``dest_dir / src.name`` by construction; this pins the
    no-progress arm to the same answer.
    """
    dest_dir = tmp_path / "landing"
    dest_dir.mkdir()
    src = PurePosixPath("/remote/src/payload.bin")

    client = _LocalWritingFtpClient(b"y" * 16)
    await _get_backend(client)._run_get([Path(src)], dest_dir, None)

    resolved = _effective_destination(client.downloads[0])
    assert resolved == PurePosixPath(dest_dir / "payload.bin"), (
        f"the no-progress get resolves to {resolved}, but the progress arm writes "
        f"{dest_dir / 'payload.bin'} -- show_progress selects an implementation, "
        f"not a location"
    )


# ===========================================================================
# The mirror itself
# ===========================================================================


def test_the_hand_mirrored_write_into_rule_still_matches_aioftp() -> None:
    """``_effective_destination`` hand-copies aioftp's branch, so pin the premise.

    Every path assertion in this file reads ``write_into``'s default out of a
    stand-in that declares it locally. If aioftp ever flipped that default, the
    stand-ins would keep the old one, the tests would keep passing, and they
    would be measuring a rule the library no longer has. Same shape, and the
    same reason, as
    ``test_transfer_registry.py::test_the_ftp_stride_is_aioftps_own_block_size``.
    """
    import inspect

    import aioftp

    for name in ("upload", "download"):
        parameter = inspect.signature(getattr(aioftp.Client, name)).parameters["write_into"]
        assert parameter.default is False, (
            f"aioftp.Client.{name} now defaults write_into={parameter.default!r}; "
            f"this file's stand-ins and _effective_destination mirror the old "
            f"default and are no longer measuring aioftp's behaviour"
        )
