"""Tests for ``ShellFileTransfer`` PUT and GET: chunked base64 over exec.

``shell`` moves files using nothing but command execution -- no ``scp``, no
``nc``, no ``rsync`` -- for a device whose entire toolkit is a POSIX shell.

Two fakes, answering two different questions:

``_RecordingExec``
    Records every command string issued, in order, and never runs anything.
    Cheap, and the only way to inspect ORDER or COUNT (how many chunks, which
    redirect, which command came last) without paying for a real shell. For
    GET it can also be handed a scripted ``outputs`` queue so a caller can
    answer the size probe with a specific number -- it still never *runs*
    ``dd`` or ``base64``, it only says what the size probe returned.
``_ShellExecutingExec``
    Actually runs each command through ``/bin/sh -c``, ``cwd`` SET to the
    test's ``tmp_path`` -- not a sandbox: an absolute path outside ``cwd``
    still gets written there (verified locally), it's just that every path
    this backend ever emits is itself absolute and derived from ``tmp_path``,
    so nothing here ever reaches outside it in practice. Lets a test read
    the REAL file that landed on disk. Complementary to ``_RecordingExec``,
    not a replacement for it: round 3 review found that it does NOT guard
    temp-then-mv (see ``TestShellPutOrdering`` and
    ``TestShellPutContentIntegrity``'s docstrings for exactly which of round
    1's three found holes each fake actually closes).

For GET specifically, note that the LOCAL side (writing the decoded temp,
``Path.replace()``) is never faked by either class -- both fakes only stand in
for the REMOTE exec channel (the size probe and each ``dd | base64``). Real
disk I/O happens in every GET test that reaches ``_get_one`` at all,
regardless of which fake is used -- the difference between the two is only
whether the *remote* command is actually executed by a real shell
(``_ShellExecutingExec``) or answered from a script (``_RecordingExec``).
The exception is ``TestShellGetRefusal``'s two tests: both raise
``UnsupportedOnUserlandError`` out of ``_run_get`` before the per-file loop
(and therefore ``_get_one``) is ever reached, so no local file is created or
written for either.

Guards, and which mutation each backs:

1. Temp-then-mv, not a direct write -- ``TestShellPutOrdering`` is written
   to isolate this property specifically, but it is not the only test that
   reddens under the mutation: measured directly (``temp = dst``, the
   ``mv`` call left in place so it self-moves), 8 of the 50 tests this file
   held at the time, across 5 classes, fail -- ``TestShellPutOrdering``,
   ``TestShellPutSequentialFailure``, ``TestShellPutIntegrityVerification``,
   ``TestShellPutContentIntegrity``, and ``TestShellChunkLineLength`` --
   each for its own reason (a changed transcript, a step never reached, a
   shorter command line once *temp* is no longer a generated name), not
   because it was built to catch this. ``TestShellPutContentIntegrity``'s
   two are themselves an artifact of this dev machine's GNU ``mv`` refusing
   a self-move ("are the same file"), which BusyBox's ``mv`` does not do
   (tolerant, rc 0) -- so even those are not real content-based evidence;
   see that class's own docstring, round 3 finding N1, for why no CONTENT
   check can tell this mutation apart from a correct transfer.
2. Staging happens in the destination's OWN directory, never ``/tmp`` --
   ``TestShellPutOrdering::test_temp_stages_in_the_same_directory_as_the_destination``.
3. The final ``mv`` actually lands the file at the real destination --
   ``TestShellPutFileArrivesAtDest``.
4. ``base64_flag == "absent"`` is a refusal, not a silent success --
   ``TestShellPutRefusal``.
5. Chunk slicing reconstructs the source byte-for-byte -- SPLIT across two
   classes on purpose: ``TestShellPutChunkStructure`` checks the shape the
   encoder produced (count, redirect operator, order) from the transcript;
   ``TestShellPutContentIntegrity`` checks the bytes that actually land on
   disk. Only the second can tell "correct content, different chunk count"
   apart from "wrong content" -- see that class's docstring for the two
   off-by-one variants this distinction matters for.
6. Every path interpolated into a command is quoted, and it matters --
   ``TestShellPutContentIntegrity::test_a_filename_with_shell_metacharacters_is_not_interpreted``.
7. A failed temp CREATION (the empty-file path) cleans up like every other
   failure -- ``TestShellPutSequentialFailure::test_a_failed_empty_file_creation_cleans_up``.

GET's own guards, ``G``-prefixed and numbered independently below -- NOT a
claimed 1:1 correspondence to PUT's numbered list above (``G3``'s refusal
guard is PUT's item 4's analogue; ``G4``'s chunk-slicing guard is PUT's item
5's; see each entry's own note):

G1. Temp-then-``Path.replace()``, not a direct write to the destination --
    ``TestShellGetStaging``, BOTH tests: under ``temp = dst`` (the
    direct-write mutation), measured to redden 2 of 2 in that class (48
    passed elsewhere, measured when this file held 50 tests -- it has grown
    since, both then and again for the codec seam). Content tests
    cannot substitute -- see ``TestShellGetSequentialFailure::test_a_failed_chunk_read_...``'s
    own docstring for the measured reason a content check stays green under
    this exact mutation.
G2. Staging happens in the destination's own directory, never elsewhere --
    ``TestShellGetStaging::test_temp_stages_in_the_same_directory_as_the_destination``.
    This is where the cross-device argument belongs (it is a property of
    WHERE the temp lives, not of temp-then-replace as a mechanism): unlike
    PUT's remote ``mv``, which on some implementations degrades to a copy
    across filesystems rather than failing, ``Path.replace()`` has no such
    fallback -- it raises ``OSError: [Errno 18] Invalid cross-device link``
    outright when source and destination are on different filesystems
    (measured locally: replacing a file from ``/dev/shm`` (tmpfs) onto this
    repo's ext4 checkout fails exactly that way) -- so staging in
    ``dest_dir`` specifically, not just "somewhere before the final
    replace," is load bearing for GET even more directly than it is for
    PUT.
G3. ``base64_flag == "absent"`` and ``stat_size == "absent"`` are both
    refusals, not a silent success or a hang -- ``TestShellGetRefusal``.
    (PUT's analogue is its item 4, not item 3 -- these G-numbers are their
    own independent sequence, not a claimed 1:1 mapping onto PUT's list.)
G4. Chunk slicing (via ``dd bs/skip/count``) reads the source byte-for-byte,
    in order -- split the same way as PUT's item 5: ``TestShellGetChunkStructure``
    (transcript: skip sequence, chunk count) and
    ``TestShellGetContentIntegrity`` (actual bytes on disk, including a
    short final chunk and a payload where chunk order is only provable by
    content).
G5. A failure partway through leaves the real destination untouched and its
    local temp removed -- ``TestShellGetSequentialFailure``.
G6. Size 0 is a real case: zero chunks, an empty file still lands, progress
    still fires once at ``(0, 0)`` -- ``TestShellGetEmptyFile``.
G7. The device's ``base64`` wraps its output (measured, not assumed -- see
    below), and decoding must both tolerate that AND reject genuine garbage
    loudly rather than silently drop it -- ``TestShellGetWrappedAndValidatedDecode``.

Two guards belong to NEITHER list because they belong to both directions at
once.

``TestEmittedCommandLinesArePinned`` is the newer of the two and is a
deliberate change detector, unlike everything else here: it pins the literal
command strings both directions emit, because
:class:`~otto.host.transfer.shell.ShellCodec` moved the chunk loops out of
:class:`~otto.host.transfer.shell.ShellFileTransfer` and a refactor that
claims to change nothing has to be able to show it. Its strings were captured
from the code as it stood before that split.

``TestStagedNameFitsTheDeclaredFilenameLimit`` is the other. PUT and GET stage
under the same generated basename (:func:`~otto.host.transfer.shell.staged_temp_name`),
and ``put_files``/``get_files`` validate the CALLER's basename against
``max_filename_len`` before handing it to a staging step that makes it
longer -- so a name the front door accepted could still be one the target
cannot open. See that class's docstring for why its three tests do not
substitute for one another.
"""

import asyncio
import base64
import hashlib
import itertools
import os
import re
import shlex
import subprocess
from collections.abc import Callable
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from otto.host.connections import ConnectionManager
from otto.host.errors import UnsupportedOnUserlandError
from otto.host.options import UserlandOptions
from otto.host.transfer import shell as shell_module
from otto.host.transfer.base import TransferContext
from otto.host.transfer.shell import (
    _SHELL_CHUNK_BYTES,
    _UU_HEREDOC_DELIMITER,
    Base64Codec,
    ShellFileTransfer,
    UuencodeCodec,
    _uu_frame,
    staged_temp_name,
)
from otto.host.userland import APPLET_ABSENT, APPLET_PRESENT, PROBED_APPLETS, Userland
from otto.result import CommandResult
from otto.utils import Status

# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------


def _ok(output: str = "") -> CommandResult:
    return CommandResult(command="", value=output, status=Status.Success, retcode=0)


def _err(output: str = "boom") -> CommandResult:
    return CommandResult(command="", value=output, status=Status.Error, retcode=1)


async def _never_probes(cmd: str, **_kwargs: object) -> CommandResult:
    """A ``Userland`` runner that must not be called -- every field is declared below."""
    raise AssertionError(f"a fully-declared userland must not probe, but it ran {cmd!r}")


def _declared_userland(
    base64_flag: str,
    stat_size: str = "stat",
    checksum: str = "absent",
    applets: "dict[str, str] | None" = None,
) -> Userland:
    """A resolved ``Userland`` with every capability declared, so ``resolve()`` probes nothing.

    The six named directly (shell_dialect, elevation, base64_flag, stat_size,
    checksum, timeout_style) are ``UserlandOptions``'s original six
    CAPABILITIES; ``version`` is metadata only ("Never gates behaviour") and
    is never probed, so it needs no declaration. The seven ``applet_*`` fields
    are declared too, defaulting to ``APPLET_PRESENT``, and that is not
    padding -- ``ShellFileTransfer._select_codec`` reads
    ``is_settled(applet_capability(...))``, so an UNDECLARED applet is a
    genuinely different input from a declared-present one and a fixture that
    could not express the difference could not test the discipline.

    IT ALSO MAKES THIS HELPER'S OWN NAME TRUE AGAIN. Before the applet fields
    were declared here, ``resolve()`` issued the applet batch against
    ``_never_probes``, whose ``AssertionError`` ``resolve()`` swallows as a
    failed probe -- so "a fully-declared userland must not probe" was quietly
    false, and every test in this module ran against seven UNSETTLED applets
    sitting at their cannot-ask default. Pass ``applets={}`` to get that
    state deliberately (see ``TestCodecSelection``); pass a partial dict to
    declare some and leave the rest unasked.

    *checksum* defaults to ``"absent"``, not ``"md5sum"`` -- every existing
    PUT/GET test in this module, written before integrity verification
    existed, exercises the byte-size fallback by default and stays green
    without scripting an extra remote answer. Tests that specifically cover
    the ``md5sum`` path pass ``checksum="md5sum"`` explicitly.
    """
    declared = {f"applet_{name}": APPLET_PRESENT for name in PROBED_APPLETS}
    if applets is not None:
        declared = {f"applet_{name}": value for name, value in applets.items()}
    return Userland(
        UserlandOptions(
            shell_dialect="ash",
            elevation="none",
            base64_flag=base64_flag,
            stat_size=stat_size,
            checksum=checksum,
            timeout_style="absent",
            **declared,
        ),
        _never_probes,
    )


class _RecordingExec:
    """Fake ``exec_cmd``: records every command issued, in order. Never runs anything.

    *fail_when* optionally marks a command as failing by predicate -- used to
    simulate a chunk-write, create, or ``mv`` failure mid-transfer.

    *outputs*, when given, is a queue of canned ``CommandResult`` values
    (successful, with the given ``value``) handed out ONE PER CALL, in
    order; once exhausted, later calls fall back to a bare success like the
    no-``outputs`` case. This does not make the fake "run" anything -- it
    still never touches a real ``dd`` or ``base64`` -- it just lets a GET
    test script a specific answer for the size probe (a real number
    ``_remote_size`` can parse) without needing a real shell.

    *answer_when*, when given, is a predicate-to-answer function checked
    BEFORE *fail_when* and BEFORE the *outputs* queue: the first call it
    returns a non-``None`` string for gets that string as its output,
    regardless of position in the transcript. Added for PUT's integrity
    verification, whose one stat/wc-shaped command (``_put_one`` has no
    other) has to answer with the REAL local total for a test to reach its
    ``mv`` at all -- a position-dependent ``outputs`` entry would break the
    moment an unrelated chunk count changed, where a predicate keyed on the
    command's own shape does not.
    """

    def __init__(
        self,
        fail_when: "Callable[[str], bool] | None" = None,
        outputs: "list[str] | None" = None,
        answer_when: "Callable[[str], str | None] | None" = None,
    ) -> None:
        self.calls: list[str] = []
        self._fail_when = fail_when
        self._outputs = list(outputs) if outputs is not None else None
        self._answer_when = answer_when

    async def __call__(
        self, cmd: str, timeout: "float | None" = None, **_kwargs: object
    ) -> CommandResult:
        self.calls.append(cmd)
        if self._fail_when is not None and self._fail_when(cmd):
            return _err(f"simulated failure: {cmd}")
        if self._answer_when is not None:
            answer = self._answer_when(cmd)
            if answer is not None:
                return _ok(answer)
        if self._outputs:
            return _ok(self._outputs.pop(0))
        return _ok()


class _ShellExecutingExec:
    """Fake ``exec_cmd``: runs each command via ``/bin/sh -c``, ``cwd`` SET (not contained).

    Unlike ``_RecordingExec``, this fake lets a test read the REAL file that
    landed on disk and compare it to the original payload -- it answers "what
    actually happened," not "what did otto ask for."

    ``cwd`` is passed to ``subprocess.run`` and used for anything the command
    references RELATIVELY; it is not a jail. Verified locally: a command
    given an absolute path outside ``cwd`` writes there without complaint.
    Safe here only because every path this backend ever emits is itself
    absolute and derived from the test's own ``tmp_path``.

    No ``timeout=`` is passed to ``subprocess.run`` -- a command that never
    returns is bounded only by this repo's 180s per-test wall-clock cap
    (``pyproject.toml``'s ``[tool.pytest.ini_options] timeout``), not by
    anything in this fake.

    Using this fake makes ``tests/unit`` depend on the HOST actually having
    ``/bin/sh``, ``base64``, ``mv``, ``rm`` and ``printf`` -- a new
    dependency for this test tree, stated here rather than left as a
    surprise the first time one of them is missing.

    *fail_when* optionally SKIPS real execution of a matching command and
    returns a synthetic failure instead -- simulating a remote exec failure
    (the command never ran on the device) rather than a local one. Every
    command it does not match runs for real.

    *path_prefix*, when given, is prepended to ``PATH`` for the child shell.
    Used only by the uu tests, which need a ``uudecode``/``uuencode`` on
    ``PATH`` and cannot assume one: this dev VM has neither (sharutils is not
    installed), so :func:`_uu_applet_shims` writes stand-ins and points the
    shell at them. What stays REAL under that arrangement is everything the
    emitted command's own shape depends on -- the heredoc, the ``&&``, the
    ``$?`` capture across the ``rm``, the subshell exit, the ``cat``
    redirect -- and only the applet itself is a stand-in. See that function
    for what keeps the stand-in honest.
    """

    def __init__(
        self,
        cwd: Path,
        fail_when: "Callable[[str], bool] | None" = None,
        path_prefix: "Path | None" = None,
    ) -> None:
        self.cwd = cwd
        self.calls: list[str] = []
        self.outputs: list[str] = []
        self._fail_when = fail_when
        self._env: "dict[str, str] | None" = None
        if path_prefix is not None:
            self._env = {**os.environ, "PATH": f"{path_prefix}{os.pathsep}{os.environ['PATH']}"}

    async def __call__(
        self, cmd: str, timeout: "float | None" = None, **_kwargs: object
    ) -> CommandResult:
        self.calls.append(cmd)
        if self._fail_when is not None and self._fail_when(cmd):
            self.outputs.append("")
            return _err(f"simulated failure: {cmd}")
        # A real subprocess fork/exec off the event loop thread -- blocking
        # `subprocess.run` directly here would stall every other coroutine
        # for the (small, local) duration of each command.
        proc = await asyncio.to_thread(
            subprocess.run,
            ["/bin/sh", "-c", cmd],
            cwd=self.cwd,
            capture_output=True,
            text=True,
            check=False,
            env=self._env,
        )
        output = proc.stdout + proc.stderr
        self.outputs.append(output)
        status = Status.Success if proc.returncode == 0 else Status.Error
        return CommandResult(command=cmd, value=output, status=status, retcode=proc.returncode)


def _make_ft(
    exec_cmd: "Callable[..., object]",
    *,
    base64_flag: str = "-d",
    stat_size: str = "stat",
    checksum: str = "absent",
    max_filename_len: int = 255,
    applets: "dict[str, str] | None" = None,
) -> ShellFileTransfer:
    mock_connections = MagicMock(spec=ConnectionManager)
    return ShellFileTransfer(
        connections=mock_connections,
        name="tomato",
        exec_cmd=exec_cmd,
        userland=_declared_userland(base64_flag, stat_size, checksum, applets),
        max_filename_len=max_filename_len,
    )


# `printf '%s' '<b64>' | base64 <flag> <>|>>> <path>` -- the exact shape
# `_put_one` emits (matches what `test_shell_codec_contracts.py` measured
# on-device, not `echo`).
_CHUNK_RE = re.compile(
    r"^printf '%s' '(?P<b64>[^']*)' \| base64 (?P<flag>\S+) (?P<redir>>{1,2}) (?P<path>\S+)$"
)


def _parse_chunk_cmd(cmd: str) -> "re.Match[str]":
    m = _CHUNK_RE.match(cmd)
    assert m, f"not a chunk-write command: {cmd!r}"
    return m


def _payload_of(cmd: str) -> bytes:
    """Extract and base64-decode the payload out of one chunk-write command.

    This decodes the text the ENCODER put into the command string -- it
    proves the encoder's output is right, not that the bytes it produced
    actually landed on a real filesystem in the right place. See
    ``TestShellPutContentIntegrity`` for the guard that checks the latter.
    """
    return base64.b64decode(_parse_chunk_cmd(cmd).group("b64"))


# `stat -c %s -- <path>` or `wc -c < <path>` -- the two spellings
# `_remote_size` (and so PUT's byte-size integrity verification, its only
# caller with no other stat/wc-shaped command in the transcript) can emit.
#
# Round 1 review (finding MINOR 9): `\S+` cannot match a `shlex.quote`d path
# containing a space (quoting wraps it in single quotes, which then
# contains a literal space) -- inert on every fixture used today, since
# none of them puts a space in a path, but silently wrong the day one does.
# `.+` instead, mirroring `_GET_CHUNK_RE`'s same fix for the same reason,
# and named groups so a caller can extract WHICH path was queried, not
# just whether the shape matched.
_SIZE_QUERY_RE = re.compile(r"^(?:stat -c %s -- (?P<stat_path>.+)|wc -c < (?P<wc_path>.+))$")


def _parse_size_query_path(cmd: str) -> str:
    m = _SIZE_QUERY_RE.match(cmd)
    assert m, f"not a size-verification command: {cmd!r}"
    path = m.group("stat_path") or m.group("wc_path")
    assert path is not None
    return path


def _get_chunk_outputs(total: int) -> list[str]:
    """Canned ``dd | base64`` outputs for a *total*-byte GET, chunk by chunk.

    Every byte is ``\\x00`` -- these tests care about the TRANSCRIPT (skip
    sequence, chunk count, ``bs``), not content, so any filler that decodes
    to exactly *total* bytes will do. Real content correctness is
    ``TestShellGetContentIntegrity``'s job, over a real shell.

    This is not decorative padding: with integrity verification always on,
    the empty-string canned outputs these tests used before the check
    existed would decode to 0 bytes against a non-zero announced *total*,
    which the ``checksum == "absent"`` byte-size check (the default in
    ``_make_ft``) would now -- correctly -- treat as a mismatch and fail the
    transfer, breaking every ``Status.Success`` assertion below for a reason
    that has nothing to do with what each test actually names.
    """
    chunks = []
    remaining = total
    while remaining > 0:
        n = min(_SHELL_CHUNK_BYTES, remaining)
        chunks.append(base64.b64encode(b"\x00" * n).decode("ascii"))
        remaining -= n
    return chunks


def _size_answer(total: int) -> "Callable[[str], str | None]":
    """An ``answer_when`` predicate: answer any size-verification query with *total*.

    Used with ``_RecordingExec(answer_when=_size_answer(...))`` in PUT tests
    so the `checksum == "absent"` integrity check -- the default in
    ``_make_ft`` -- finds a real, parseable number and lets the transfer
    reach its ``mv``, without the test having to predict the exact position
    of that one command among a variable number of chunk writes.
    """

    def _answer(cmd: str) -> str | None:
        return str(total) if _SIZE_QUERY_RE.match(cmd) else None

    return _answer


# `dd if=<if_expr> bs=<n> skip=<k> count=1 2>/dev/null | base64` -- the exact
# shape `_get_one` emits for one chunk READ (matches what
# `test_shell_codec_contracts.py::test_dd_reads_a_block_range_with_bs_skip_and_count`
# measured on-device). `if_expr` is `.+` rather than `\S+` because
# `shlex.quote` wraps a path containing shell metacharacters in single
# quotes, which then contains a space -- deliberately permissive here so the
# same regex parses both the plain and quoted forms. No decode flag anywhere
# in this command: the device only ENCODES for GET, so a flag appearing
# after `base64` would fail to match this pattern at all (the trailing `$`
# anchors immediately after the literal `base64`).
_GET_CHUNK_RE = re.compile(
    r"^dd (?P<if_expr>.+) bs=(?P<bs>\d+) skip=(?P<skip>\d+) count=1 2>/dev/null \| base64$"
)


def _parse_get_chunk_cmd(cmd: str) -> "re.Match[str]":
    m = _GET_CHUNK_RE.match(cmd)
    assert m, f"not a GET chunk-read command: {cmd!r}"
    return m


# ---------------------------------------------------------------------------
# Step 1 / mutation "write directly to dest instead of temp-then-mv"
# ---------------------------------------------------------------------------


class TestShellPutOrdering:
    """Chunks land on a temp path first, in sequence; exactly one ``mv`` at the end.

    Catches the mutation "write directly to dest instead of temp-then-mv":
    that mutation would make every chunk-write command reference the real
    destination directly, which the loop below explicitly forbids.

    THE ONLY guard for that mutation. A test in ``TestShellPutContentIntegrity``
    (``test_a_failed_transfer_leaves_the_real_destination_untouched``) looks
    like a second, content-based way to catch it and is not: measured
    directly (round 3 review, finding N1), that test PASSES unmodified under
    this mutation, because with ``temp == dst`` the cleanup step's
    ``rm -f -- <dst>`` deletes the real bytes the first chunk just wrote
    there for real -- "the real path doesn't exist" ends up true, but for
    the inverted reason (deleted, not never-written). See that test's own
    docstring. Nor can the executing fake's mutation-1 numbers in
    task-2-report.md's Round 2 stand as evidence either: they came from a
    local GNU ``mv -- X X`` refusing a self-move ("are the same file"),
    which BusyBox 1.36.1's ``mv`` does not do -- tolerant, rc 0, on exactly
    the device class this backend exists for. This transcript-based
    structural check -- not any content check -- is the one actually
    load-bearing for temp-then-mv.
    """

    @pytest.mark.asyncio
    async def test_writes_temp_first_in_sequence_then_moves_to_dest(self, tmp_path: Path) -> None:
        src = tmp_path / "payload.bin"
        # Two chunks: one full block plus a partial tail.
        payload = b"a" * _SHELL_CHUNK_BYTES + b"b" * 100
        src.write_bytes(payload)
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()
        dest_str = str(dest_dir / "payload.bin")

        # `checksum` defaults to "absent" in `_make_ft`, so the integrity
        # check between the last chunk and the `mv` is a size query; answer
        # it with the real total so the transfer reaches `mv` at all.
        exec_cmd = _RecordingExec(answer_when=_size_answer(len(payload)))
        ft = _make_ft(exec_cmd)

        per_file = await ft._run_put([src], dest_dir, None)

        assert per_file[src].status is Status.Success, per_file[src].msg
        assert per_file[src].value == dest_dir / "payload.bin"

        calls = exec_cmd.calls
        assert len(calls) == 4, calls  # 2 chunk writes + 1 size-verify + 1 mv
        write_calls, (verify_cmd, mv_cmd) = calls[:2], calls[2:]
        assert _SIZE_QUERY_RE.match(verify_cmd), verify_cmd

        m0, m1 = _parse_chunk_cmd(write_calls[0]), _parse_chunk_cmd(write_calls[1])
        # The temp filename embeds the real basename as a PREFIX
        # (`payload.bin.otto-<hex>`), so a plain substring check on
        # `dest_str` would always match — assert on the parsed redirect
        # TARGET being exactly the temp, not the real destination, instead.
        assert m0.group("path") != dest_str, "a pre-mv command wrote directly to the real dest"
        assert m1.group("path") != dest_str, "a pre-mv command wrote directly to the real dest"
        assert m0.group("redir") == ">", f"first chunk must create/truncate: {write_calls[0]!r}"
        assert m1.group("redir") == ">>", f"second chunk must append: {write_calls[1]!r}"
        assert m0.group("path") == m1.group("path"), "both chunks must target the same temp"
        assert ".otto-" in m0.group("path"), m0.group("path")

        assert mv_cmd.startswith("mv -- "), mv_cmd
        assert m0.group("path") in mv_cmd
        assert dest_str in mv_cmd

    @pytest.mark.asyncio
    async def test_temp_stages_in_the_same_directory_as_the_destination(
        self, tmp_path: Path
    ) -> None:
        """The brief's explicitly load-bearing property: staging happens NEXT
        TO the destination, never under ``/tmp`` -- a cross-filesystem ``mv``
        degrades to a copy and loses the atomicity the whole temp-then-mv
        shape exists for. Asserted directly (``temp.parent == dest_dir``)
        rather than inferred from content, because on a single-machine test
        ``/tmp`` and ``dest_dir`` can share a filesystem, so a content check
        alone cannot tell "staged in the right place" from "staged in the
        wrong place but it didn't matter this time."
        """
        src = tmp_path / "payload.bin"
        src.write_bytes(b"hello world")
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()

        exec_cmd = _RecordingExec()
        ft = _make_ft(exec_cmd)

        await ft._run_put([src], dest_dir, None)

        write_calls = [c for c in exec_cmd.calls if c.startswith("printf ")]
        assert write_calls
        temp_path = Path(_parse_chunk_cmd(write_calls[0]).group("path"))
        assert temp_path.parent == dest_dir, f"temp staged at {temp_path}, not inside {dest_dir}"


# ---------------------------------------------------------------------------
# Step 3 / mutation "drop the final mv"
# ---------------------------------------------------------------------------


class TestShellPutFileArrivesAtDest:
    """The file must actually land at the real destination via exactly one ``mv``.

    Catches the mutation "drop the final mv": with the ``mv`` call deleted
    but the method still returning Success, ``mv_calls`` below would be
    empty while the assertion demands exactly one.
    """

    @pytest.mark.asyncio
    async def test_exactly_one_mv_lands_the_file(self, tmp_path: Path) -> None:
        payload = b"hello world"
        src = tmp_path / "f.bin"
        src.write_bytes(payload)
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()

        exec_cmd = _RecordingExec(answer_when=_size_answer(len(payload)))
        ft = _make_ft(exec_cmd)

        per_file = await ft._run_put([src], dest_dir, None)
        result = per_file[src]

        assert result.status is Status.Success, result.msg
        assert result.value == dest_dir / "f.bin"

        mv_calls = [c for c in exec_cmd.calls if c.startswith("mv ")]
        assert len(mv_calls) == 1, exec_cmd.calls
        assert str(dest_dir / "f.bin") in mv_calls[0]


# ---------------------------------------------------------------------------
# Step 2 / mutation "return success when base64_flag == absent"
# ---------------------------------------------------------------------------


class TestShellPutRefusal:
    """A device that can run NEITHER codec must refuse loudly, before any command runs.

    ``base64_flag == "absent"`` alone is no longer the condition, and that is
    the point of the uu codec: a device with no ``base64`` but a working
    ``uudecode`` -- every BusyBox matrix row that lacks base64, which is
    1.16.1 -- now transfers instead of being turned away. The refusal that
    remains is for a device with neither, and it still has to arrive before a
    single command.

    Catches the mutation "return success when no codec is available": that
    mutation would return a Success/Skipped mapping instead of raising, so
    ``pytest.raises`` below would fail. The zero-calls assertion is a
    SEPARATE, stronger check: run against the actual disabled-refusal
    mutation, it issues 2 commands (one chunk write, one ``mv``) before
    (wrongly) returning Success -- so the zero-calls assertion catches that
    concrete case, and would also catch a softer mutation that raises late,
    after already emitting a command.

    The second test is what stops the refusal being "fixed" by widening it
    back: a codec-less refusal that also fired on a uu-capable device would
    make this whole path unreachable and nothing else here would notice.
    """

    @pytest.mark.asyncio
    async def test_neither_codec_raises_before_any_command(self, tmp_path: Path) -> None:
        src = tmp_path / "f.bin"
        src.write_bytes(b"hello")
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()

        exec_cmd = _RecordingExec()
        ft = _make_ft(
            exec_cmd,
            base64_flag="absent",
            applets={"uudecode": APPLET_ABSENT, "uuencode": APPLET_ABSENT},
        )

        with pytest.raises(UnsupportedOnUserlandError) as exc_info:
            await ft._run_put([src], dest_dir, None)

        assert exec_cmd.calls == [], f"refusal must precede every command, got {exec_cmd.calls}"
        assert "tomato" in str(exc_info.value)
        assert "base64" in str(exc_info.value)
        assert "uudecode" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_absent_base64_with_uudecode_present_transfers_instead(
        self, tmp_path: Path
    ) -> None:
        """The behaviour this codec exists to add: no base64, and the PUT still happens."""
        src = tmp_path / "f.bin"
        src.write_bytes(b"hello")
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()

        exec_cmd = _RecordingExec(answer_when=_size_answer(5))
        ft = _make_ft(exec_cmd, base64_flag="absent")

        per_file = await ft._run_put([src], dest_dir, None)

        assert per_file[src].status is Status.Success, per_file[src].msg
        assert any(cmd.startswith("uudecode -o ") for cmd in exec_cmd.calls), (
            f"a base64-less device must reach the uu codec, and no command here is a "
            f"uudecode: {[cmd[:40] for cmd in exec_cmd.calls]}"
        )


# ---------------------------------------------------------------------------
# Step 4 / mutation "off-by-one in chunk slicing" -- SHAPE half (transcript)
# ---------------------------------------------------------------------------


class TestShellPutChunkStructure:
    """The encoder's chunk COUNT, redirect operator and order -- from the transcript.

    Deliberately narrow, and named for what it actually checks after round 1
    of this test's review found the previous name overclaimed: this class
    decodes the base64 text the encoder put INTO each command string and
    checks it against the source, which proves the encoder is right, but it
    is blind to anything that happens after the command leaves otto -- a
    real remote shell could still write those same bytes to the wrong file,
    the wrong directory, or lose them to an unquoted path, and nothing here
    would notice. ``TestShellPutContentIntegrity`` is the class that checks
    actual on-disk bytes.

    A chunk-COUNT change (shrinking or growing the read size, N >= 1) is NOT
    content corruption, and this class's two tests are shaped to keep that
    distinction visible rather than paper over it. This is settled, not a
    hedge: for any N >= 1, ``while True: chunk = f.read(N)`` is a contiguous,
    in-order, non-overlapping partition of the file -- a structural property
    of sequential ``read``, not an empirical guess -- and each piece is
    base64-encoded/decoded independently (Python's encoder pads every call on
    its own), so a pure chunk-count change reconstructs identical bytes; it
    only changes how many pieces they arrived in. Confirmed against real
    on-disk bytes via ``TestShellPutContentIntegrity`` in both round 2 and
    round 3 of this task's review (task-2-report.md).

    N = 0 is the one exception, and it is not a counterexample to the above:
    at N = 0 the loop degenerates (``f.read(0)`` is always empty, so the
    first iteration breaks immediately even for a non-empty file), the
    "empty file" branch runs instead, and the real destination ends up
    genuinely empty regardless of the source's actual content -- a true
    content bug, and confirmed by mutation in round 3: 11 failed, 9 passed,
    all three ``TestShellPutContentIntegrity`` tests among the reds. N = 0
    is not a chunk size any code path here would produce; it is listed for
    completeness, not as a live risk.
    """

    @pytest.mark.asyncio
    async def test_two_full_chunks_plus_a_partial_tail_are_split_and_ordered_right(
        self, tmp_path: Path
    ) -> None:
        payload = bytes((i * 7) % 256 for i in range(_SHELL_CHUNK_BYTES * 2 + 123))
        src = tmp_path / "payload.bin"
        src.write_bytes(payload)
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()

        exec_cmd = _RecordingExec(answer_when=_size_answer(len(payload)))
        ft = _make_ft(exec_cmd)

        per_file = await ft._run_put([src], dest_dir, None)
        assert per_file[src].status is Status.Success, per_file[src].msg

        write_calls = exec_cmd.calls[:-2]  # everything but the size-verify and the final mv
        assert len(write_calls) == 3, write_calls  # 2 full chunks + 1 partial tail

        reconstructed = b"".join(_payload_of(c) for c in write_calls)
        assert reconstructed == payload

    @pytest.mark.asyncio
    async def test_exact_multiple_of_chunk_size_has_no_stray_empty_chunk(
        self, tmp_path: Path
    ) -> None:
        payload = b"\xab" * (_SHELL_CHUNK_BYTES * 2)
        src = tmp_path / "payload.bin"
        src.write_bytes(payload)
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()

        exec_cmd = _RecordingExec(answer_when=_size_answer(len(payload)))
        ft = _make_ft(exec_cmd)

        per_file = await ft._run_put([src], dest_dir, None)
        assert per_file[src].status is Status.Success, per_file[src].msg

        write_calls = exec_cmd.calls[:-2]  # everything but the size-verify and the final mv
        assert len(write_calls) == 2, write_calls
        assert b"".join(_payload_of(c) for c in write_calls) == payload


# ---------------------------------------------------------------------------
# _SHELL_CHUNK_BYTES's own guard: not the byte constant, the wire consequence
# ---------------------------------------------------------------------------


class TestShellChunkLineLength:
    """Pins what ``_SHELL_CHUNK_BYTES`` actually exists to control: how many
    characters cross the wire in one PUT chunk's command line.

    Deliberately does NOT assert ``_SHELL_CHUNK_BYTES == 4096``: the byte
    count is a deliberate, revisable choice (see its own docstring, and
    phase 5's Tier 3, which will re-measure it against a real transport).
    Pinning the literal value would be a change-detector, red for the wrong
    reason the moment that re-measurement moves it. Asserting the LENGTH of
    one full chunk's emitted command line means a future change to the
    constant reddens here with a message that says "you changed how much
    crosses the wire per line -- re-justify it", which is the signal that
    actually matters.

    *dest_dir* is a synthetic path, not ``tmp_path``: ``_put_one`` never
    touches the destination directory on the local filesystem (only *src*
    must be real, for ``.stat().st_size``), and ``tmp_path``'s own directory
    name is pytest-run-dependent -- using it here would make the asserted
    length flake across machines and runs. The staged temp filename embeds
    ``uuid.uuid4().hex[:_STAGING_TOKEN_HEX]``: its VALUE differs every run,
    but the token is always exactly 8 hex characters, so the total command
    length stays exactly reproducible even though the temp name itself is
    not. ``payload.bin`` is far shorter than ``max_filename_len``, so
    :func:`~otto.host.transfer.shell.staged_temp_name`'s budget never
    truncates it here and the length is a pure function of the token.

    The pinned number moved from 5548 to 5524 when that token was cut from
    32 hex characters to 8 -- see :data:`~otto.host.transfer.shell._STAGING_TOKEN_HEX`
    for why it was cut. That is a 24-character REDUCTION in what crosses the
    wire per line, which is the direction that needs no re-justification
    against the transport bound; the guard is re-measured rather than
    recomputed regardless.
    """

    @pytest.mark.asyncio
    async def test_one_full_chunks_command_line_length_is_pinned(self, tmp_path: Path) -> None:
        payload = b"x" * _SHELL_CHUNK_BYTES
        src = tmp_path / "payload.bin"
        src.write_bytes(payload)
        dest_dir = Path("/dest")  # synthetic, deterministic -- see class docstring

        exec_cmd = _RecordingExec(answer_when=_size_answer(len(payload)))
        ft = _make_ft(exec_cmd)

        per_file = await ft._run_put([src], dest_dir, None)
        assert per_file[src].status is Status.Success, per_file[src].msg

        write_calls = [c for c in exec_cmd.calls if c.startswith("printf ")]
        assert len(write_calls) == 1, write_calls
        assert len(write_calls[0]) == 5524, (
            f"one full {_SHELL_CHUNK_BYTES}-byte chunk's command line is "
            f"{len(write_calls[0])} characters, not the 5524 measured when this "
            f"guard was last re-measured -- changing _SHELL_CHUNK_BYTES (or the "
            f"staged temp's name; see _STAGING_TOKEN_HEX) changes how much "
            f"crosses the wire per line; re-justify the new number against the "
            f"transport bound (see the constant's own docstring) before updating "
            f"this assertion"
        )


# ---------------------------------------------------------------------------
# Step 4 / mutation "off-by-one in chunk slicing" -- CONTENT half (real exec)
# ---------------------------------------------------------------------------


class TestShellPutContentIntegrity:
    """Executes every emitted command for real, then reads the ACTUAL file that landed.

    ``TestShellPutChunkStructure`` answers "does the fake see the right
    shape of commands"; this class answers "did the right bytes end up at
    the right path on a real filesystem."

    Round 1's review found three holes in the (then transcript-only) suite
    by mutation: staging moved to ``/tmp`` (15 passed, 0 failed), ``shlex.quote``
    removed from the temp path (15 passed, 0 failed), and every chunk
    truncating instead of appending (1 failed, 14 passed --
    ``TestShellPutOrdering`` already inspected the redirect operator
    directly, so that one was partially caught even then). This class closes
    the SECOND and THIRD with a real on-disk byte comparison.

    It does NOT close the first. Staging under ``/tmp`` instead of next to
    the destination is a question of WHERE a file lands, not what bytes it
    contains, and a real device whose ``mv`` still completes the move (or a
    test machine where ``/tmp`` and the destination happen to share a
    filesystem) leaves the content correct either way -- confirmed by
    mutation in round 2 (task-2-report.md), where this class's tests stayed
    green under that exact mutation and only
    ``TestShellPutOrdering::test_temp_stages_in_the_same_directory_as_the_destination``
    went red. No content check can ever substitute for that one.
    """

    @pytest.mark.asyncio
    async def test_multi_chunk_round_trip_lands_correct_bytes_on_disk(self, tmp_path: Path) -> None:
        payload = bytes((i * 7) % 256 for i in range(_SHELL_CHUNK_BYTES * 2 + 123))
        src = tmp_path / "payload.bin"
        src.write_bytes(payload)
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()

        exec_cmd = _ShellExecutingExec(cwd=tmp_path)
        ft = _make_ft(exec_cmd)

        per_file = await ft._run_put([src], dest_dir, None)

        assert per_file[src].status is Status.Success, per_file[src].msg
        landed = dest_dir / "payload.bin"
        assert landed.exists(), exec_cmd.calls
        assert landed.read_bytes() == payload

    @pytest.mark.asyncio
    async def test_a_filename_with_shell_metacharacters_is_not_interpreted(
        self, tmp_path: Path
    ) -> None:
        """A semicolon in the filename must stay DATA, never become a second command.

        ``;printf INJECTED;`` is a standard, safe injection probe: if
        ``shlex.quote`` is doing its job, the whole filename (and the temp
        path built from it) is one shell word and the payload lands
        untouched; if quoting is dropped, the shell reads ``;`` as a command
        separator and runs ``printf INJECTED`` as a second statement, which
        both shows up in this fake's captured output AND breaks the intended
        command (measured locally: the unquoted form exits 127, "not found",
        because the remainder of the filename is parsed as a bogus command of
        its own) -- so the transfer's own Success/Error status is already a
        strong signal, and the INJECTED check is the second, independent one.
        """
        src = tmp_path / "evil;printf INJECTED;name.bin"
        payload = b"just a normal file"
        src.write_bytes(payload)
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()

        exec_cmd = _ShellExecutingExec(cwd=tmp_path)
        ft = _make_ft(exec_cmd)

        per_file = await ft._run_put([src], dest_dir, None)

        assert per_file[src].status is Status.Success, per_file[src].msg
        assert "INJECTED" not in "".join(exec_cmd.outputs), exec_cmd.outputs
        landed = dest_dir / src.name
        assert landed.exists(), exec_cmd.calls
        assert landed.read_bytes() == payload

    @pytest.mark.asyncio
    async def test_a_failed_transfer_leaves_the_real_destination_untouched(
        self, tmp_path: Path
    ) -> None:
        """Temp-then-mv: a mid-transfer failure, WHEN STAGING GENUINELY USED A
        SEPARATE TEMP, must not leave a partial file at the real destination
        path. The first chunk's write DOES run for real (so a real temp file
        with real bytes briefly exists); only the second chunk's exec is
        intercepted, simulating a remote failure rather than a local one.

        NOT a guard against "write directly to dest instead of temp-then-mv"
        (round 3 review, finding N1) -- despite reading like one. Measured
        directly: this test PASSES unmodified against that mutation. With
        ``temp == dst`` there, the first chunk's write lands for real at the
        REAL destination, and when the second chunk's exec then fails,
        ``_cleanup_temp``'s ``rm -f -- <dst>`` deletes those real bytes --
        so ``not real_dest.exists()`` is true, but because the file was
        created and then deleted, not because it was staged safely away from
        the real path the whole time. See ``TestShellPutOrdering``, the
        actual (and only) guard for that mutation, for why.
        """
        src = tmp_path / "payload.bin"
        src.write_bytes(b"x" * _SHELL_CHUNK_BYTES + b"y" * _SHELL_CHUNK_BYTES)
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()

        write_count = 0

        def fail_second_write(cmd: str) -> bool:
            nonlocal write_count
            if cmd.startswith("printf "):
                write_count += 1
                return write_count == 2
            return False

        exec_cmd = _ShellExecutingExec(cwd=tmp_path, fail_when=fail_second_write)
        ft = _make_ft(exec_cmd)

        per_file = await ft._run_put([src], dest_dir, None)

        assert per_file[src].status is Status.Error
        real_dest = dest_dir / "payload.bin"
        assert not real_dest.exists(), (
            f"a failed transfer left a file at the real destination: {real_dest}"
        )
        leftover_temps = list(dest_dir.glob("payload.bin.otto-*"))
        assert leftover_temps == [], (
            f"the failed attempt's temp was not cleaned up: {leftover_temps}"
        )


# ---------------------------------------------------------------------------
# Decode flag is read verbatim, never hard-coded
# ---------------------------------------------------------------------------


class TestShellPutDecodeFlagVerbatim:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("flag", ["-d", "--decode"])
    async def test_emitted_flag_matches_whatever_userland_resolved(
        self, tmp_path: Path, flag: str
    ) -> None:
        src = tmp_path / "f.bin"
        src.write_bytes(b"x" * 10)
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()

        exec_cmd = _RecordingExec()
        ft = _make_ft(exec_cmd, base64_flag=flag)

        await ft._run_put([src], dest_dir, None)

        write_calls = [c for c in exec_cmd.calls if c.startswith("printf ")]
        assert write_calls
        for c in write_calls:
            assert _parse_chunk_cmd(c).group("flag") == flag


# ---------------------------------------------------------------------------
# Sequential batch semantics: stop on failure, skip the rest, clean up
# ---------------------------------------------------------------------------


class TestShellPutSequentialFailure:
    @pytest.mark.asyncio
    async def test_a_failed_chunk_write_skips_remaining_files_and_cleans_up(
        self, tmp_path: Path
    ) -> None:
        src1 = tmp_path / "a.bin"
        src1.write_bytes(b"x" * 10)
        src2 = tmp_path / "b.bin"
        src2.write_bytes(b"y" * 10)
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()

        # The temp filename always embeds the destination basename
        # (`a.bin.otto-<hex>`), so this reliably targets only src1's writes.
        exec_cmd = _RecordingExec(fail_when=lambda c: "a.bin.otto-" in c and c.startswith("printf"))
        ft = _make_ft(exec_cmd)

        per_file = await ft._run_put([src1, src2], dest_dir, None)

        assert per_file[src1].status is Status.Error
        assert per_file[src2].status is Status.Skipped
        assert not any("b.bin" in c for c in exec_cmd.calls), (
            "the second file must never be attempted after the first fails"
        )
        rm_calls = [c for c in exec_cmd.calls if c.startswith("rm ")]
        assert rm_calls, f"a failed chunk write must clean up its temp: {exec_cmd.calls}"
        assert all("a.bin.otto-" in c for c in rm_calls)

    @pytest.mark.asyncio
    async def test_a_failed_mv_cleans_up_and_does_not_report_success(self, tmp_path: Path) -> None:
        payload = b"hello"
        src = tmp_path / "f.bin"
        src.write_bytes(payload)
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()

        # The integrity check must PASS here -- fed the real size via
        # `answer_when` -- so what actually fails, and is what this test is
        # named for, is genuinely the `mv` and nothing upstream of it.
        exec_cmd = _RecordingExec(
            fail_when=lambda c: c.startswith("mv "), answer_when=_size_answer(len(payload))
        )
        ft = _make_ft(exec_cmd)

        per_file = await ft._run_put([src], dest_dir, None)

        assert per_file[src].status is Status.Error
        assert "moving" in per_file[src].msg, (
            f"expected the mv step itself to be what failed, got: {per_file[src].msg}"
        )
        rm_calls = [c for c in exec_cmd.calls if c.startswith("rm ")]
        assert rm_calls, "a failed mv must still clean up its temp"

    @pytest.mark.asyncio
    async def test_a_failed_empty_file_creation_cleans_up(self, tmp_path: Path) -> None:
        """The empty-file ``: > <temp>`` creation step is a failure path too.

        Round 1 shipped this step WITHOUT a cleanup call despite the
        docstring's claim that every failure -- "a chunk write, the temp's
        creation, or the final mv" -- removes the temp; an empty source file
        whose creation step failed left the docstring's claim false and the
        temp genuinely leaked. Fixed in round 2; this pins it.
        """
        src = tmp_path / "empty.bin"
        src.write_bytes(b"")
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()

        exec_cmd = _RecordingExec(fail_when=lambda c: c.startswith(": >"))
        ft = _make_ft(exec_cmd)

        per_file = await ft._run_put([src], dest_dir, None)

        assert per_file[src].status is Status.Error
        rm_calls = [c for c in exec_cmd.calls if c.startswith("rm ")]
        assert rm_calls, "a failed empty-file temp creation must still clean up"
        assert all("empty.bin.otto-" in c for c in rm_calls)


# ---------------------------------------------------------------------------
# Empty source file
# ---------------------------------------------------------------------------


class TestShellPutEmptyFile:
    @pytest.mark.asyncio
    async def test_empty_source_creates_an_empty_temp_and_moves_it(self, tmp_path: Path) -> None:
        src = tmp_path / "empty.bin"
        src.write_bytes(b"")
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()

        exec_cmd = _RecordingExec(answer_when=_size_answer(0))
        ft = _make_ft(exec_cmd)

        per_file = await ft._run_put([src], dest_dir, None)

        assert per_file[src].status is Status.Success, per_file[src].msg
        assert per_file[src].value == dest_dir / "empty.bin"
        assert not any(c.startswith("printf ") for c in exec_cmd.calls)
        assert any(c.startswith(": >") for c in exec_cmd.calls), exec_cmd.calls
        assert any(c.startswith("mv -- ") for c in exec_cmd.calls)


# ---------------------------------------------------------------------------
# Progress contract
# ---------------------------------------------------------------------------


class TestShellPutProgress:
    @pytest.mark.asyncio
    async def test_handler_reaches_bytes_done_equals_total(self, tmp_path: Path) -> None:
        src = tmp_path / "f.bin"
        payload = b"z" * (_SHELL_CHUNK_BYTES + 50)
        src.write_bytes(payload)
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()

        seen: list[tuple[int, int]] = []

        def handler(_src: str, _dst: str, bytes_done: int, bytes_total: int) -> None:
            seen.append((bytes_done, bytes_total))

        # Answered so the transfer actually reaches Success -- confirming the
        # progress contract holds for a transfer integrity verification let
        # through, not merely for one that fails after progress already
        # reported completion (which `seen` alone cannot tell apart).
        exec_cmd = _RecordingExec(answer_when=_size_answer(len(payload)))
        ft = _make_ft(exec_cmd)

        result = await ft._run_put([src], dest_dir, lambda: handler)

        assert result[src].status is Status.Success, result[src].msg
        assert seen, "progress handler was never invoked"
        assert seen[-1] == (len(payload), len(payload))


# ---------------------------------------------------------------------------
# Task 4 / integrity verification -- both directions
# ---------------------------------------------------------------------------


class TestShellPutIntegrityVerification:
    """PUT's post-chunk, pre-``mv`` integrity check, on the TEMP -- both ``checksum`` arms.

    Uses ``_RecordingExec`` throughout: the property under test is the FORMAT
    of what a device returns and how this backend reacts to it (a matching
    digest, a matching size, a mismatch of either), not what a real
    filesystem ends up holding -- that half is
    ``TestShellPutContentIntegrity``'s job, over a real shell via
    ``_ShellExecutingExec``.

    Round 1 review (finding IMPORTANT 4): this docstring previously claimed
    "every test in those classes reaches this code path" and that
    ``TestShellPutSequentialFailure`` was one of the classes doing so
    through ``_ShellExecutingExec``. Both were false, and measured wrong in
    two different ways. Traced by reading each test's own control flow
    (which tests reach ``mv`` unimpeded, since verification sits directly
    before it): of ``TestShellPutContentIntegrity``'s 3 tests, 2 reach
    verification for real (the two that expect ``Status.Success``) and 1
    does not (its chunk write fails first, aborting before verification is
    ever reached). ``TestShellPutSequentialFailure`` uses ``_RecordingExec``
    exclusively, never ``_ShellExecutingExec``; of its 3 tests, exactly 1
    reaches verification (and must PASS it, by its own docstring's design,
    to isolate a later failure) -- the other 2 fail at an earlier step
    (chunk write, empty-file creation) that verification never gets called
    from. 3 of 6 tests across both classes reach this method; the other 3
    exercise a failure that happens strictly before it.
    """

    @pytest.mark.asyncio
    async def test_matching_md5sum_lets_the_transfer_reach_mv(self, tmp_path: Path) -> None:
        """Also pins WHICH path ``md5sum`` targets -- the temp, never the real destination.

        Round 1 review (finding CRITICAL 2): the original version of this
        test answered any command starting with ``"md5sum"``, so a mutant
        querying the wrong path (``dst`` instead of ``temp``, or an
        unrelated path entirely) still got the right-looking answer and
        this test stayed green. Fixed by cross-checking the ``md5sum``
        command's path against the temp path an EARLIER, independently
        parsed command (the chunk write) actually used -- not a path this
        test computed itself, so it cannot agree by construction.
        """
        payload = b"the quick brown fox jumps over the lazy dog"
        src = tmp_path / "f.bin"
        src.write_bytes(payload)
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()
        dest_str = str(dest_dir / "f.bin")
        digest = hashlib.md5(payload).hexdigest()  # noqa: S324 -- matching a real device's md5sum

        exec_cmd = _RecordingExec(
            answer_when=lambda c: f"{digest}  ignored-filename" if c.startswith("md5sum") else None
        )
        ft = _make_ft(exec_cmd, checksum="md5sum")

        per_file = await ft._run_put([src], dest_dir, None)

        assert per_file[src].status is Status.Success, per_file[src].msg
        write_calls = [c for c in exec_cmd.calls if c.startswith("printf ")]
        assert write_calls
        temp_path = _parse_chunk_cmd(write_calls[0]).group("path")
        md5sum_calls = [c for c in exec_cmd.calls if c.startswith("md5sum")]
        assert md5sum_calls == [f"md5sum -- {temp_path}"], (
            f"md5sum must target the same temp the chunks were written to "
            f"({temp_path!r}), not the real destination or anything else: {exec_cmd.calls}"
        )
        assert temp_path != dest_str, temp_path
        assert any(c.startswith("mv -- ") for c in exec_cmd.calls), exec_cmd.calls

    @pytest.mark.asyncio
    async def test_mismatched_md5sum_fails_before_mv_and_cleans_up(self, tmp_path: Path) -> None:
        """Step 3's named mutation: a corrupted chunk must be caught, and the temp removed.

        The corruption is injected at the point ``_RecordingExec`` can most
        directly script it -- the device's answer to the verification
        query itself is scripted WRONG (a real digest of different bytes),
        standing in for "the device silently wrote/served different bytes
        than what was sent, yet every individual chunk command still
        reported exit 0" -- exactly the gap a pure exit-code check cannot
        see and integrity verification exists to close.
        """
        payload = b"the quick brown fox jumps over the lazy dog"
        src = tmp_path / "f.bin"
        src.write_bytes(payload)
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()
        wrong_digest = hashlib.md5(b"not the same bytes at all").hexdigest()  # noqa: S324

        exec_cmd = _RecordingExec(
            answer_when=lambda c: (
                f"{wrong_digest}  ignored-filename" if c.startswith("md5sum") else None
            )
        )
        ft = _make_ft(exec_cmd, checksum="md5sum")

        per_file = await ft._run_put([src], dest_dir, None)

        assert per_file[src].status is Status.Error
        assert "md5sum mismatch" in per_file[src].msg, per_file[src].msg
        assert not any(c.startswith("mv ") for c in exec_cmd.calls), (
            f"a mismatched digest must never reach mv: {exec_cmd.calls}"
        )
        rm_calls = [c for c in exec_cmd.calls if c.startswith("rm ")]
        assert rm_calls, f"a mismatched digest must still clean up its temp: {exec_cmd.calls}"

    @pytest.mark.asyncio
    async def test_a_differently_cased_but_equal_digest_still_matches(self, tmp_path: Path) -> None:
        """The comparison must not be case-sensitive, even though no real ``md5sum`` needs that.

        Every ``md5sum`` measured on this matrix emits lowercase hex and so
        does :meth:`hashlib.md5.hexdigest`, so this scenario cannot arise
        from a real device -- but the parser choosing to compare
        case-sensitively anyway would be a latent bug with no measured
        counterexample to catch it. This test is that counterexample.
        """
        payload = b"case insensitivity probe"
        src = tmp_path / "f.bin"
        src.write_bytes(payload)
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()
        digest_upper = hashlib.md5(payload).hexdigest().upper()  # noqa: S324

        exec_cmd = _RecordingExec(
            answer_when=lambda c: (
                f"{digest_upper}  ignored-filename" if c.startswith("md5sum") else None
            )
        )
        ft = _make_ft(exec_cmd, checksum="md5sum")

        per_file = await ft._run_put([src], dest_dir, None)

        assert per_file[src].status is Status.Success, per_file[src].msg

    @pytest.mark.asyncio
    async def test_absent_checksum_falls_back_to_a_matching_size_check(
        self, tmp_path: Path
    ) -> None:
        """Also pins WHICH path the size query targets -- the temp, never the real destination.

        The same weakness CRITICAL 2 named for the ``md5sum`` arm applies
        here too: ``_size_answer`` answers any stat/wc-shaped command
        regardless of path, so a mutant querying ``dst`` instead of the
        temp would still get the right-looking answer. Fixed the same way:
        cross-check against the temp path an earlier, independently parsed
        command (the chunk write) actually used.
        """
        payload = b"twelve bytes"
        src = tmp_path / "f.bin"
        src.write_bytes(payload)
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()
        dest_str = str(dest_dir / "f.bin")

        exec_cmd = _RecordingExec(answer_when=_size_answer(len(payload)))
        ft = _make_ft(exec_cmd, checksum="absent")

        per_file = await ft._run_put([src], dest_dir, None)

        assert per_file[src].status is Status.Success, per_file[src].msg
        assert not any(c.startswith("md5sum") for c in exec_cmd.calls), (
            f"checksum='absent' must never emit md5sum: {exec_cmd.calls}"
        )
        write_calls = [c for c in exec_cmd.calls if c.startswith("printf ")]
        assert write_calls
        temp_path = _parse_chunk_cmd(write_calls[0]).group("path")
        size_calls = [c for c in exec_cmd.calls if _SIZE_QUERY_RE.match(c)]
        assert size_calls, exec_cmd.calls
        assert all(_parse_size_query_path(c) == temp_path for c in size_calls), (
            f"the size query must target the same temp the chunks were written to "
            f"({temp_path!r}), not the real destination or anything else: {exec_cmd.calls}"
        )
        assert temp_path != dest_str, temp_path

    @pytest.mark.asyncio
    async def test_absent_checksum_size_mismatch_fails_before_mv_and_cleans_up(
        self, tmp_path: Path
    ) -> None:
        payload = b"twelve bytes"
        src = tmp_path / "f.bin"
        src.write_bytes(payload)
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()

        # Answers with a size one byte short of the real total.
        exec_cmd = _RecordingExec(answer_when=_size_answer(len(payload) - 1))
        ft = _make_ft(exec_cmd, checksum="absent")

        per_file = await ft._run_put([src], dest_dir, None)

        assert per_file[src].status is Status.Error
        assert "size mismatch" in per_file[src].msg, per_file[src].msg
        assert not any(c.startswith("mv ") for c in exec_cmd.calls), exec_cmd.calls
        rm_calls = [c for c in exec_cmd.calls if c.startswith("rm ")]
        assert rm_calls, f"a size mismatch must still clean up its temp: {exec_cmd.calls}"

    @pytest.mark.asyncio
    async def test_checksum_and_stat_size_both_absent_refuses_before_any_command(
        self, tmp_path: Path
    ) -> None:
        """Unhit by the matrix (measured: ``md5sum`` and ``stat``/``wc`` are present on all five
        rows), so this is a unit-only branch, like ``stat_size``'s own ``"absent"`` arm --
        kept and tested, not assumed unreachable.
        """
        src = tmp_path / "f.bin"
        src.write_bytes(b"hello")
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()

        exec_cmd = _RecordingExec()
        ft = _make_ft(exec_cmd, checksum="absent", stat_size="absent")

        with pytest.raises(UnsupportedOnUserlandError) as exc_info:
            await ft._run_put([src], dest_dir, None)

        assert exec_cmd.calls == [], f"refusal must precede every command, got {exec_cmd.calls}"
        assert "tomato" in str(exc_info.value)
        assert "checksum" in str(exc_info.value)


# ===========================================================================
# GET
# ===========================================================================

# ---------------------------------------------------------------------------
# G3 / mutation "return success when stat_size or base64_flag is 'absent'"
# ---------------------------------------------------------------------------


class TestShellGetRefusal:
    """A codec-less device and an unsizeable one must both refuse loudly.

    Two independent refusals, mirroring ``TestShellPutRefusal``'s pair.
    Measured (this task's brief): every row of the BusyBox matrix resolves
    ``stat_size`` to ``"stat"``, so the second branch is never live on that
    matrix today -- it exists for a non-BusyBox unix host that genuinely has
    neither ``stat`` nor a usable ``wc``, and is tested here precisely
    because nothing else would ever exercise it.

    GET's codec refusal reads ``uuencode``, not PUT's ``uudecode``: the device
    only ENCODES here. ``TestCodecSelection::test_put_reads_uudecode_and_get_reads_uuencode``
    is what holds the two apart.
    """

    @pytest.mark.asyncio
    async def test_neither_codec_raises_before_any_command(self, tmp_path: Path) -> None:
        src = tmp_path / "f.bin"
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()

        exec_cmd = _RecordingExec()
        ft = _make_ft(
            exec_cmd,
            base64_flag="absent",
            stat_size="stat",
            applets={"uudecode": APPLET_ABSENT, "uuencode": APPLET_ABSENT},
        )

        with pytest.raises(UnsupportedOnUserlandError) as exc_info:
            await ft._run_get([src], dest_dir, None)

        assert exec_cmd.calls == [], f"refusal must precede every command, got {exec_cmd.calls}"
        assert "tomato" in str(exc_info.value)
        assert "base64" in str(exc_info.value)
        assert "uuencode" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_absent_stat_size_raises_before_any_command(self, tmp_path: Path) -> None:
        src = tmp_path / "f.bin"
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()

        exec_cmd = _RecordingExec()
        ft = _make_ft(exec_cmd, base64_flag="-d", stat_size="absent")

        with pytest.raises(UnsupportedOnUserlandError) as exc_info:
            await ft._run_get([src], dest_dir, None)

        assert exec_cmd.calls == [], f"refusal must precede every command, got {exec_cmd.calls}"
        assert "tomato" in str(exc_info.value)
        assert "stat_size" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Size probe spelling -- shell becomes the first consumer of Userland.stat_size
# ---------------------------------------------------------------------------


class TestShellGetSizeQuery:
    """The size probe is issued once, first, spelled per ``Userland.stat_size``.

    ``shell`` is the first consumer of :attr:`~otto.host.userland.Userland.stat_size`
    (see that module's updated docstring table) -- read verbatim here, never
    hard-coded, exactly as ``_put_one`` reads ``base64_flag``.
    """

    @pytest.mark.asyncio
    async def test_stat_spelling_is_stat_dash_c_percent_s_with_terminator(
        self, tmp_path: Path
    ) -> None:
        src = tmp_path / "f.bin"
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()

        exec_cmd = _RecordingExec(outputs=["0"])
        ft = _make_ft(exec_cmd, stat_size="stat")

        await ft._run_get([src], dest_dir, None)

        assert exec_cmd.calls == [f"stat -c %s -- {src}"], exec_cmd.calls

    @pytest.mark.asyncio
    async def test_wc_spelling_is_wc_dash_c_redirected_from_the_path(self, tmp_path: Path) -> None:
        src = tmp_path / "f.bin"
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()

        exec_cmd = _RecordingExec(outputs=["0"])
        ft = _make_ft(exec_cmd, stat_size="wc")

        await ft._run_get([src], dest_dir, None)

        assert exec_cmd.calls == [f"wc -c < {src}"], exec_cmd.calls


# ---------------------------------------------------------------------------
# G4 (transcript half) / mutation "off-by-one in chunk range reads"
# ---------------------------------------------------------------------------


class TestShellGetChunkStructure:
    """The chunk READ shape -- count, ``bs``, increasing ``skip`` -- from the transcript.

    Uses ``_RecordingExec`` with a scripted ``outputs`` queue: the first
    call (the size probe) is answered with the real total; every dd|base64
    call after it is answered with an empty string, which decodes to
    ``b""`` without error. That is enough to check ORDER and COUNT -- it
    proves nothing about DECODED CONTENT, which is
    ``TestShellGetContentIntegrity``'s job, split the same way PUT's G5 is.

    Catches "fixed skip=0" directly: a chunker that requested the same
    block every time would produce ``skips == [0, 0, 0]`` instead of
    ``[0, 1, 2]``.
    """

    @pytest.mark.asyncio
    async def test_two_full_chunks_plus_a_partial_tail_are_split_and_ordered_by_skip(
        self, tmp_path: Path
    ) -> None:
        src = tmp_path / "payload.bin"
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()
        total = _SHELL_CHUNK_BYTES * 2 + 123

        exec_cmd = _RecordingExec(outputs=[str(total), *_get_chunk_outputs(total)])
        ft = _make_ft(exec_cmd)

        per_file = await ft._run_get([src], dest_dir, None)
        assert per_file[src].status is Status.Success, per_file[src].msg

        size_call, chunk_calls = exec_cmd.calls[0], exec_cmd.calls[1:]
        assert size_call == f"stat -c %s -- {src}"
        assert len(chunk_calls) == 3, chunk_calls  # 2 full chunks + 1 partial tail
        skips = [int(_parse_get_chunk_cmd(c).group("skip")) for c in chunk_calls]
        assert skips == [0, 1, 2], skips
        for c in chunk_calls:
            assert _parse_get_chunk_cmd(c).group("bs") == str(_SHELL_CHUNK_BYTES), c

    @pytest.mark.asyncio
    async def test_exact_multiple_of_chunk_size_has_no_stray_empty_chunk(
        self, tmp_path: Path
    ) -> None:
        src = tmp_path / "payload.bin"
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()
        total = _SHELL_CHUNK_BYTES * 2

        exec_cmd = _RecordingExec(outputs=[str(total), *_get_chunk_outputs(total)])
        ft = _make_ft(exec_cmd)

        per_file = await ft._run_get([src], dest_dir, None)
        assert per_file[src].status is Status.Success, per_file[src].msg

        chunk_calls = exec_cmd.calls[1:]
        assert len(chunk_calls) == 2, chunk_calls
        skips = [int(_parse_get_chunk_cmd(c).group("skip")) for c in chunk_calls]
        assert skips == [0, 1], skips

    @pytest.mark.asyncio
    async def test_short_final_chunk_gets_its_own_read_not_a_third_full_one(
        self, tmp_path: Path
    ) -> None:
        """Step 3's size: ``_SHELL_CHUNK_BYTES + 1`` -- two chunks, not one, not three.

        The transcript half of step 3: proves the RANGE READ shape asks for
        exactly two blocks. ``TestShellGetContentIntegrity`` proves the
        second block's decoded bytes are not padded out to a full chunk.
        """
        src = tmp_path / "payload.bin"
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()
        total = _SHELL_CHUNK_BYTES + 1

        exec_cmd = _RecordingExec(outputs=[str(total), *_get_chunk_outputs(total)])
        ft = _make_ft(exec_cmd)

        per_file = await ft._run_get([src], dest_dir, None)
        assert per_file[src].status is Status.Success, per_file[src].msg

        chunk_calls = exec_cmd.calls[1:]
        assert len(chunk_calls) == 2, chunk_calls
        skips = [int(_parse_get_chunk_cmd(c).group("skip")) for c in chunk_calls]
        assert skips == [0, 1], skips


# ---------------------------------------------------------------------------
# G1 / mutation "drop the local temp-then-replace, write straight to dest"
# G2 / mutation "stage the local temp somewhere other than dest_dir"
# ---------------------------------------------------------------------------


class TestShellGetStaging:
    """Local staging: a temp lands in ``dest_dir`` first; ``dest`` appears only at the end.

    THE ONLY guard for "write straight to the destination instead of
    temp-then-replace": unlike PUT's remote ``mv``, GET's final step is
    Python's own ``Path.replace()``, which raises outright
    (``OSError: [Errno 18] Invalid cross-device link``, measured locally by
    replacing a file from ``/dev/shm`` (tmpfs) onto this repo's checkout)
    when source and destination are on different filesystems -- there is no
    silent degrade-to-copy the way some ``mv`` implementations have. That
    makes staging in ``dest_dir`` a correctness requirement for GET, not
    only an atomicity nicety, and it is why this class checks it directly
    rather than inferring it from content: a content check alone cannot
    distinguish "staged correctly" from "staged elsewhere but same
    filesystem, so it still worked this time" -- the identical reasoning
    ``TestShellPutOrdering`` gives for PUT's temp-directory guard.
    """

    @pytest.mark.asyncio
    async def test_dest_does_not_exist_until_the_final_replace(self, tmp_path: Path) -> None:
        src = tmp_path / "payload.bin"
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()
        dst = dest_dir / "payload.bin"
        total = _SHELL_CHUNK_BYTES * 2 + 123

        seen_calls = 0

        def handler(_src: str, _dst: str, _bytes_done: int, _bytes_total: int) -> None:
            nonlocal seen_calls
            seen_calls += 1
            assert not dst.exists(), (
                f"the real destination existed before the final replace (call {seen_calls})"
            )

        exec_cmd = _RecordingExec(outputs=[str(total), *_get_chunk_outputs(total)])
        ft = _make_ft(exec_cmd)

        per_file = await ft._run_get([src], dest_dir, lambda: handler)

        assert per_file[src].status is Status.Success, per_file[src].msg
        assert seen_calls == 3, seen_calls
        assert dst.exists(), "the destination must exist once the transfer completes"

    @pytest.mark.asyncio
    async def test_temp_stages_in_the_same_directory_as_the_destination(
        self, tmp_path: Path
    ) -> None:
        """Every in-loop assertion is inside the progress handler -- deliberately guarded
        against going vacuous if that handler is ever dropped (fix round 1, I4): a
        missing call means zero assertions ran, not a genuine pass. ``seen_calls``
        and the ``Status.Success`` check below are UNCONDITIONAL, run whether or
        not the handler fired, so "the handler never ran" fails loud here instead
        of reading as agreement. Verified by mutation: commenting out this
        method's own ``handler(...)`` call site in ``_get_one`` (simulating the
        drop) reddens `seen_calls == 1` here directly -- see the fix-round report.
        """
        src = tmp_path / "payload.bin"
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()
        dst = dest_dir / "payload.bin"
        total = 11  # single chunk (< _SHELL_CHUNK_BYTES): handler fires exactly once

        seen_calls = 0

        def handler(_src: str, _dst: str, _bytes_done: int, _bytes_total: int) -> None:
            nonlocal seen_calls
            seen_calls += 1
            matches = list(dest_dir.glob(f"{dst.name}.otto-*"))
            assert len(matches) == 1, (
                f"expected exactly one local temp staged in {dest_dir}, found {matches}"
            )
            assert matches[0].parent == dest_dir

        exec_cmd = _RecordingExec(outputs=[str(total), *_get_chunk_outputs(total)])
        ft = _make_ft(exec_cmd)

        per_file = await ft._run_get([src], dest_dir, lambda: handler)

        assert per_file[src].status is Status.Success, per_file[src].msg
        assert seen_calls == 1, seen_calls


# ---------------------------------------------------------------------------
# G4 (content half) / mutations "reverse chunk write order", "off-by-one"
# ---------------------------------------------------------------------------


class TestShellGetContentIntegrity:
    """Executes every emitted command for real, then reads the ACTUAL local file that landed.

    ``TestShellGetChunkStructure`` answers "did the fake see the right
    shape of dd commands"; this class answers "did the right bytes end up
    at the right local path" -- the only class that can tell "correct
    content, different chunk count" apart from "wrong content", and the
    only one that can catch chunks decoded correctly but WRITTEN in the
    wrong order (an ordering bug in the local write, not in the remote
    read, so ``TestShellGetChunkStructure``'s skip-sequence check is blind
    to it -- the emitted dd commands would still ask for blocks 0, 1, 2 in
    order even if the decoded bytes were then written to the local temp
    out of order).
    """

    @pytest.mark.asyncio
    async def test_multi_chunk_round_trip_lands_correct_bytes_including_nul_and_trailing_newline(
        self, tmp_path: Path
    ) -> None:
        base = bytes((i * 7) % 256 for i in range(_SHELL_CHUNK_BYTES * 2 + 123))
        # Force the LAST two bytes to a NUL followed by a trailing newline,
        # without changing the total length (still spans three chunks: two
        # full plus a partial tail).
        payload = base[:-2] + b"\x00\n"
        src = tmp_path / "payload.bin"
        src.write_bytes(payload)
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()

        exec_cmd = _ShellExecutingExec(cwd=tmp_path)
        ft = _make_ft(exec_cmd)

        per_file = await ft._run_get([src], dest_dir, None)

        assert per_file[src].status is Status.Success, (per_file[src].msg, exec_cmd.calls)
        landed = dest_dir / "payload.bin"
        assert landed.exists(), exec_cmd.calls
        assert landed.read_bytes() == payload

    @pytest.mark.asyncio
    async def test_short_final_chunk_is_not_padded_or_dropped(self, tmp_path: Path) -> None:
        payload = b"x" * _SHELL_CHUNK_BYTES + b"y"
        src = tmp_path / "payload.bin"
        src.write_bytes(payload)
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()

        exec_cmd = _ShellExecutingExec(cwd=tmp_path)
        ft = _make_ft(exec_cmd)

        per_file = await ft._run_get([src], dest_dir, None)

        assert per_file[src].status is Status.Success, (per_file[src].msg, exec_cmd.calls)
        landed = dest_dir / "payload.bin"
        landed_bytes = landed.read_bytes()
        assert len(landed_bytes) == _SHELL_CHUNK_BYTES + 1, len(landed_bytes)
        assert landed_bytes == payload

    @pytest.mark.asyncio
    async def test_a_filename_with_shell_metacharacters_is_not_interpreted(
        self, tmp_path: Path
    ) -> None:
        """A semicolon in the SOURCE filename must stay DATA, never become a second command.

        Mirrors ``TestShellPutContentIntegrity``'s same-named test. GET's
        quoting is new code (the ``dd if=<src>`` argument, and the ``stat``/
        ``wc`` size-probe path), so this pins that it is not skipped just
        because PUT already proved the pattern once.
        """
        src = tmp_path / "evil;printf INJECTED;name.bin"
        payload = b"just a normal file"
        src.write_bytes(payload)
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()

        exec_cmd = _ShellExecutingExec(cwd=tmp_path)
        ft = _make_ft(exec_cmd)

        per_file = await ft._run_get([src], dest_dir, None)

        # The DISCRIMINATING assertion goes first, deliberately. Verified by
        # mutation (dropping shlex.quote on `dd`'s if= argument): the
        # unquoted `;` splits the shell into three statements -- a `dd` on a
        # truncated path, a bare `printf INJECTED`, and a bogus third
        # command -- and the compound command still exits nonzero (the
        # captured stdout+stderr is not valid base64 either way), so
        # `Status.Success` reddens too. But checking `Status.Success` FIRST
        # would surface a generic "chunk 0 was not valid base64" message
        # that says nothing about *why* -- it could just as easily be a
        # transport corruption bug. Checking the INJECTED marker first
        # surfaces the actual, specific, load-bearing signal: `printf
        # INJECTED` really executed (confirmed directly, see this test's own
        # verification in the fix-round report) and its output landed in
        # `exec_cmd.outputs`, proving the unquoted `;` was read as a command
        # separator by a REAL shell, not merely present in the command
        # string.
        assert "INJECTED" not in "".join(exec_cmd.outputs), exec_cmd.outputs
        assert per_file[src].status is Status.Success, (per_file[src].msg, exec_cmd.calls)
        landed = dest_dir / src.name
        assert landed.exists(), exec_cmd.calls
        assert landed.read_bytes() == payload


# ---------------------------------------------------------------------------
# I1 (fix round 1 review) -- the device's base64 wraps; decode must tolerate
# wrapping and must not silently accept garbage
# ---------------------------------------------------------------------------


class TestShellGetWrappedAndValidatedDecode:
    """The remote ``base64`` wraps its output, and a corrupt chunk must fail loudly, not silently.

    Measured directly against real BusyBox rootfs images in this worktree
    (``tests/_fixtures/busybox_rootfs``, all four matrix rows with a
    ``base64`` applet at all): encoding one 4096-byte chunk wraps to 72
    lines of up to 76 columns each. This class does not rely on THIS dev
    box's own ``base64`` happening to wrap the same way -- each test hands
    ``_RecordingExec`` a HAND-BUILT, explicitly wrapped or corrupted chunk,
    so the property under test is pinned by the test data, not by whichever
    ``base64`` happens to be on `$PATH` wherever the suite runs.

    ``base64.b64decode(..., validate=True)`` is why both properties hold at
    once: the flatten step (stripping whitespace before decoding) makes
    wrapped text decode correctly, and ``validate=True`` makes anything
    ELSE outside the base64 alphabet a hard failure instead of the default
    ``validate=False``, which would silently accept a corrupted chunk --
    measured on this class's own fixture (see
    ``test_a_stray_non_alphabet_byte_mid_stream_fails_loudly``): discarding
    the stray ``@`` there happens to leave a valid base64 string that decodes
    to exactly the original 52-byte payload, so the bytes would have been
    RIGHT. What ``validate=True`` actually buys is detecting that the wire
    was corrupt at all -- nothing about ``validate=False`` would have
    flagged that a transport injected a byte, even though this particular
    injection was harmless by luck of where it landed.
    """

    @pytest.mark.asyncio
    async def test_a_wrapped_chunk_decodes_to_the_original_bytes(self, tmp_path: Path) -> None:
        payload = bytes((i * 13 + 7) % 256 for i in range(300))
        encoded = base64.b64encode(payload).decode("ascii")
        wrapped = "\n".join(encoded[i : i + 76] for i in range(0, len(encoded), 76)) + "\n"
        assert wrapped.count("\n") > 1, "the fixture must actually span multiple lines"

        src = tmp_path / "wrapped.bin"
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()

        exec_cmd = _RecordingExec(outputs=[str(len(payload)), wrapped])
        ft = _make_ft(exec_cmd)

        per_file = await ft._run_get([src], dest_dir, None)

        assert per_file[src].status is Status.Success, per_file[src].msg
        landed = dest_dir / "wrapped.bin"
        assert landed.read_bytes() == payload

    @pytest.mark.asyncio
    async def test_a_stray_non_alphabet_byte_mid_stream_fails_loudly(self, tmp_path: Path) -> None:
        """A garbage byte injected into an otherwise-valid chunk must ERROR, not silently drop it.

        ``@`` is not in the base64 alphabet (``[A-Za-z0-9+/=]``) and is not
        whitespace, so the flatten step does not remove it. With
        ``validate=False`` (the default, and this backend's behavior before
        this fix round), ``base64.b64decode`` would silently accept the
        corrupted chunk -- measured directly: discarding the stray ``@`` here
        happens to recover the exact original bytes (52 bytes, equal), so the
        payload would NOT have been wrong. Nothing would have flagged that
        the transport injected a byte, though -- that silent acceptance,
        not a bytes-level corruption, is the failure mode this test exists
        to catch.
        """
        payload = b"hello world, this is more than one base64 group long"
        encoded = base64.b64encode(payload).decode("ascii")
        corrupted = encoded[:10] + "@" + encoded[10:]

        src = tmp_path / "corrupt.bin"
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()

        exec_cmd = _RecordingExec(outputs=[str(len(payload)), corrupted])
        ft = _make_ft(exec_cmd)

        per_file = await ft._run_get([src], dest_dir, None)

        assert per_file[src].status is Status.Error, (
            f"a stray non-alphabet byte mid-chunk must fail the transfer, not silently "
            f"decode wrong bytes: got {per_file[src]}"
        )
        assert not (dest_dir / "corrupt.bin").exists(), (
            "a failed decode must not leave a (wrong) file at the destination"
        )


# ---------------------------------------------------------------------------
# G5 / mutation "fail mid-transfer" -- destination untouched, temp cleaned up
# ---------------------------------------------------------------------------


class TestShellGetSequentialFailure:
    @pytest.mark.asyncio
    async def test_a_failed_size_probe_fails_before_any_chunk_read(self, tmp_path: Path) -> None:
        src = tmp_path / "payload.bin"
        src.write_bytes(b"x" * 10)
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()

        exec_cmd = _ShellExecutingExec(cwd=tmp_path, fail_when=lambda c: c.startswith("stat "))
        ft = _make_ft(exec_cmd)

        per_file = await ft._run_get([src], dest_dir, None)

        assert per_file[src].status is Status.Error
        assert not any(c.startswith("dd ") for c in exec_cmd.calls), exec_cmd.calls
        assert not (dest_dir / "payload.bin").exists()

    @pytest.mark.asyncio
    async def test_a_failed_chunk_read_leaves_the_real_destination_untouched_and_cleans_up(
        self, tmp_path: Path
    ) -> None:
        """Second of three chunk reads fails (a simulated remote/exec failure).

        The first chunk's read DOES run for real (so real, decoded bytes
        are briefly written to the local temp); only the second chunk's
        exec is intercepted.

        NOT a second guard for G1 ("write straight to the destination
        instead of temp-then-replace") despite reading like one -- that is
        ``TestShellGetStaging``'s job, and ONLY its job. Measured directly
        (fix round 1): under ``temp = dst`` (G1's mutation), this test
        still PASSES -- `pytest ...::test_a_failed_chunk_read_...` alone
        reports 1 passed, and the same mutation against the whole GET suite
        reports 2 failed (both in ``TestShellGetStaging``), 17 passed,
        this test among the passes. With ``temp == dst``, chunk 1's real
        bytes land at the REAL destination for real, and when chunk 2 then
        fails, ``_cleanup_local_temp(temp)`` -- where ``temp`` IS ``dst`` --
        deletes them; ``not real_dest.exists()`` ends up true, but for the
        inverted reason (created then deleted, not staged safely away the
        whole time). The ``leftover_temps == []`` half is equally blind to
        that mutation for a different reason: with no ``.otto-`` suffix
        ever used, there is nothing matching ``payload.bin.otto-*`` to find
        either way -- vacuously true, not a measurement.

        What THIS test actually guards, and does catch (fix round 1,
        mutation: dropped the ``self._cleanup_local_temp(temp)`` call on
        the chunk-read failure branch): 1 failed, 18 passed, and this test
        -- specifically its ``leftover_temps == []`` assertion -- is the
        one red. That is the real, on-disk analogue of PUT's own F1 defect
        (a cleanup omitted on one failure path), for GET's chunk-read
        branch.
        """
        payload = b"x" * (_SHELL_CHUNK_BYTES * 3)
        src = tmp_path / "payload.bin"
        src.write_bytes(payload)
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()

        exec_cmd = _ShellExecutingExec(
            cwd=tmp_path, fail_when=lambda c: c.startswith("dd ") and "skip=1 " in c
        )
        ft = _make_ft(exec_cmd)

        per_file = await ft._run_get([src], dest_dir, None)

        assert per_file[src].status is Status.Error
        real_dest = dest_dir / "payload.bin"
        assert not real_dest.exists(), (
            f"a failed GET left a file at the real destination: {real_dest}"
        )
        leftover_temps = list(dest_dir.glob("payload.bin.otto-*"))
        assert leftover_temps == [], (
            f"the failed attempt's local temp was not cleaned up: {leftover_temps}"
        )

    @pytest.mark.asyncio
    async def test_a_failed_file_skips_remaining_files(self, tmp_path: Path) -> None:
        src1 = tmp_path / "a.bin"
        src1.write_bytes(b"x" * 10)
        src2 = tmp_path / "b.bin"
        src2.write_bytes(b"y" * 10)
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()

        # `if=<abs path>` always embeds the full source path, never a bare
        # basename, so match on the trailing `/a.bin` to target only src1.
        exec_cmd = _ShellExecutingExec(
            cwd=tmp_path, fail_when=lambda c: c.startswith("dd ") and "/a.bin" in c
        )
        ft = _make_ft(exec_cmd)

        per_file = await ft._run_get([src1, src2], dest_dir, None)

        assert per_file[src1].status is Status.Error
        assert per_file[src2].status is Status.Skipped
        assert not any("/b.bin" in c for c in exec_cmd.calls if c.startswith("dd ")), (
            "the second file must never be attempted after the first fails"
        )


# ---------------------------------------------------------------------------
# G6 / mutation "drop the zero-chunk progress call / mishandle size 0"
# ---------------------------------------------------------------------------


class TestShellGetEmptyFile:
    @pytest.mark.asyncio
    async def test_empty_source_creates_an_empty_local_file_with_no_chunk_reads(
        self, tmp_path: Path
    ) -> None:
        src = tmp_path / "empty.bin"
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()

        seen: list[tuple[int, int]] = []

        def handler(_src: str, _dst: str, bytes_done: int, bytes_total: int) -> None:
            seen.append((bytes_done, bytes_total))

        exec_cmd = _RecordingExec(outputs=["0"])
        ft = _make_ft(exec_cmd)

        per_file = await ft._run_get([src], dest_dir, lambda: handler)

        assert per_file[src].status is Status.Success, per_file[src].msg
        assert per_file[src].value == dest_dir / "empty.bin"
        assert not any(c.startswith("dd ") for c in exec_cmd.calls), exec_cmd.calls
        landed = dest_dir / "empty.bin"
        assert landed.exists()
        assert landed.read_bytes() == b""
        assert seen == [(0, 0)], seen


# ---------------------------------------------------------------------------
# Progress contract
# ---------------------------------------------------------------------------


class TestShellGetProgress:
    @pytest.mark.asyncio
    async def test_handler_reaches_bytes_done_equals_total(self, tmp_path: Path) -> None:
        src = tmp_path / "f.bin"
        payload = b"z" * (_SHELL_CHUNK_BYTES + 50)
        src.write_bytes(payload)
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()

        seen: list[tuple[int, int]] = []

        def handler(_src: str, _dst: str, bytes_done: int, bytes_total: int) -> None:
            seen.append((bytes_done, bytes_total))

        exec_cmd = _ShellExecutingExec(cwd=tmp_path)
        ft = _make_ft(exec_cmd)

        per_file = await ft._run_get([src], dest_dir, lambda: handler)

        assert per_file[src].status is Status.Success, per_file[src].msg
        assert seen, "progress handler was never invoked"
        assert seen[-1] == (len(payload), len(payload))


# ---------------------------------------------------------------------------
# Task 4 / integrity verification -- GET's own arms, plus the named mutation
# ---------------------------------------------------------------------------


class TestShellGetIntegrityVerification:
    """GET's post-decode, pre-``Path.replace()`` integrity check -- both ``checksum`` arms.

    ``_RecordingExec`` throughout, for the same reason PUT's counterpart
    class gives: the property under test is the FORMAT of what a device
    returns and how this backend reacts to it, and GET's LOCAL temp write is
    real regardless of which fake stands in for the remote side (see the
    module docstring) -- so "the temp is gone" can be checked directly
    against the filesystem even with the remote exec entirely faked.
    """

    @pytest.mark.asyncio
    async def test_matching_md5sum_lets_the_transfer_reach_replace(self, tmp_path: Path) -> None:
        """Also pins WHICH path ``md5sum`` targets -- the real remote ``src``, nothing else.

        Round 1 review (finding CRITICAL 2, the most severe of the two): the
        original version of this test answered any command starting with
        ``"md5sum"``, so a mutant substituting an entirely unrelated path
        (the review's own example: ``Path("/wrong/path/entirely")``) still
        got the right-looking answer and this test stayed green -- 49
        passed, 0 failed under that mutation. Fixed with an exact-equality
        check against *src*, which GET's ``md5sum`` arm must always target
        (there is no remote temp on the GET side to stage into first).
        """
        payload = b"the quick brown fox jumps over the lazy dog"
        src = tmp_path / "f.bin"
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()
        dst = dest_dir / "f.bin"
        digest = hashlib.md5(payload).hexdigest()  # noqa: S324 -- matching a real device's md5sum
        chunk = base64.b64encode(payload).decode("ascii")

        exec_cmd = _RecordingExec(
            outputs=[str(len(payload)), chunk],
            answer_when=lambda c: f"{digest}  ignored-filename" if c.startswith("md5sum") else None,
        )
        ft = _make_ft(exec_cmd, checksum="md5sum")

        per_file = await ft._run_get([src], dest_dir, None)

        assert per_file[src].status is Status.Success, per_file[src].msg
        md5sum_calls = [c for c in exec_cmd.calls if c.startswith("md5sum")]
        assert md5sum_calls == [f"md5sum -- {src}"], (
            f"md5sum must target the real remote source ({src}), not a substituted "
            f"path: {exec_cmd.calls}"
        )
        assert dst.exists()
        assert dst.read_bytes() == payload

    @pytest.mark.asyncio
    async def test_a_corrupted_chunk_is_caught_by_md5sum_and_the_temp_is_removed(
        self, tmp_path: Path
    ) -> None:
        """Step 3's named mutation: corrupt one chunk in the fake; the check must catch it
        AND the temp must be gone afterwards.

        Two chunks, both syntactically valid base64 (so
        ``base64.b64decode(..., validate=True)`` -- item 0's guard -- has
        nothing to object to; decoding succeeds and every chunk-read command
        reports exit 0). The SECOND chunk's canned output is swapped for a
        different, equal-length, still-valid-base64 payload -- bytes that
        decode cleanly but are not what the source actually contains. The
        device's ``md5sum`` answer is scripted as the digest of the
        UNCORRUPTED original payload (what a real device holding the real
        source would actually report), so the mismatch is entirely between
        that answer and what landed locally -- exactly the "exit 0 but wrong
        bytes" gap a pure exit-code or base64-validity check cannot see.

        Second thread, alongside ``test_matching_md5sum_lets_the_transfer_reach_replace``'s:
        that test is the only one pinning WHICH path ``md5sum`` targets, so
        reverting its lone exact-equality assertion back to the old
        ``any(c.startswith("md5sum -- "))`` fully un-guards a wrong-path
        regression again -- MEASURED, reverting it while a
        ``Path("/wrong/path/entirely")`` mutation is applied to
        ``_get_one``'s ``_verify_integrity`` call returns the suite to 49
        passed, 0 failed. ``answer_when`` here is keyed on the exact command
        too (not a bare ``startswith("md5sum")``), and ``exec_cmd.calls`` is
        asserted the same way, so a wrong-path regression reds THIS test as
        well -- two independent assertions instead of one.
        """
        payload = b"a" * _SHELL_CHUNK_BYTES + b"b" * 200
        src = tmp_path / "f.bin"
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()
        real_digest = hashlib.md5(payload).hexdigest()  # noqa: S324
        chunk0 = base64.b64encode(payload[:_SHELL_CHUNK_BYTES]).decode("ascii")
        real_chunk1 = payload[_SHELL_CHUNK_BYTES:]
        corrupted_chunk1 = base64.b64encode(b"X" * len(real_chunk1)).decode("ascii")
        assert corrupted_chunk1 != base64.b64encode(real_chunk1).decode("ascii")

        exec_cmd = _RecordingExec(
            outputs=[str(len(payload)), chunk0, corrupted_chunk1],
            answer_when=lambda c: (
                f"{real_digest}  ignored-filename" if c == f"md5sum -- {src}" else None
            ),
        )
        ft = _make_ft(exec_cmd, checksum="md5sum")

        per_file = await ft._run_get([src], dest_dir, None)

        assert per_file[src].status is Status.Error
        assert "md5sum mismatch" in per_file[src].msg, per_file[src].msg
        md5sum_calls = [c for c in exec_cmd.calls if c.startswith("md5sum")]
        assert md5sum_calls == [f"md5sum -- {src}"], (
            f"md5sum must target the real remote source ({src}), not a substituted "
            f"path: {exec_cmd.calls}"
        )
        dst = dest_dir / "f.bin"
        assert not dst.exists(), "a mismatched digest must never reach the real destination"
        leftover_temps = list(dest_dir.glob("f.bin.otto-*"))
        assert leftover_temps == [], (
            f"the corrupted chunk's local temp was not cleaned up: {leftover_temps}"
        )

    @pytest.mark.asyncio
    async def test_absent_checksum_falls_back_to_the_already_known_total(
        self, tmp_path: Path
    ) -> None:
        """No FRESH remote query on the ``absent`` path -- see ``_verify_integrity``'s
        docstring for why reusing *total* does not weaken what this catches. Asserted
        directly: exactly one ``stat`` call in the whole transcript.
        """
        payload = b"twelve bytes"
        src = tmp_path / "f.bin"
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()
        chunk = base64.b64encode(payload).decode("ascii")

        exec_cmd = _RecordingExec(outputs=[str(len(payload)), chunk])
        ft = _make_ft(exec_cmd, checksum="absent")

        per_file = await ft._run_get([src], dest_dir, None)

        assert per_file[src].status is Status.Success, per_file[src].msg
        stat_calls = [c for c in exec_cmd.calls if _SIZE_QUERY_RE.match(c)]
        assert len(stat_calls) == 1, (
            f"the absent-checksum path must reuse the size already fetched, not "
            f"re-query: {exec_cmd.calls}"
        )

    @pytest.mark.asyncio
    async def test_absent_checksum_short_read_is_a_size_mismatch(self, tmp_path: Path) -> None:
        """A chunk that decodes short of the announced total -- no exec error, no base64
        error -- must still fail as a size mismatch, not report ``Status.Success`` on a
        truncated local file.
        """
        payload = b"twelve bytes"
        src = tmp_path / "f.bin"
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()
        # Decodes to one byte fewer than announced.
        short_chunk = base64.b64encode(payload[:-1]).decode("ascii")

        exec_cmd = _RecordingExec(outputs=[str(len(payload)), short_chunk])
        ft = _make_ft(exec_cmd, checksum="absent")

        per_file = await ft._run_get([src], dest_dir, None)

        assert per_file[src].status is Status.Error
        assert "size mismatch" in per_file[src].msg, per_file[src].msg
        dst = dest_dir / "f.bin"
        assert not dst.exists()
        leftover_temps = list(dest_dir.glob("f.bin.otto-*"))
        assert leftover_temps == [], leftover_temps


# ---------------------------------------------------------------------------
# The staged name's own budget: what `put_files` ACCEPTS, staging must SURVIVE
# ---------------------------------------------------------------------------


class TestStagedNameFitsTheDeclaredFilenameLimit:
    """A basename ``put_files``/``get_files`` let through must be STAGEABLE.

    Both entry points validate the caller's basename against
    ``max_filename_len`` (:func:`~otto.host.transfer.base.validate_filename_lengths`,
    ``len(name) > limit``), and both then hand that same basename to a
    staging step that makes it LONGER -- ``<name>.otto-<token>``. So a name
    at or near the limit passes the up-front check that exists precisely to
    turn "the target cannot open this name" into a clear refusal, and then
    fails anyway, late, from the target instead: ``File name too long``
    after the local file has been read and (for PUT) after chunks have
    already crossed the wire. The whole point of the front-door check is
    that it answers for the WHOLE operation, and it was answering for the
    wrong name.

    Two independent things have to be true, and they are guarded separately
    because they can break separately:

    1. **The declared limit is respected** -- ``test_the_staged_temp_name_...``.
       ``max_filename_len`` models the DEVICE's filesystem, which for PUT is
       not the machine running the test at all: the temp is created remotely,
       by the emitted command. So this one reads the emitted TRANSCRIPT and
       measures the basename otto asked the device to create, against a
       declared limit far tighter than anything this machine enforces. It
       needs no real filesystem and cannot be satisfied by a permissive one.
    2. **A real filesystem at that limit actually accepts it** -- the PUT and
       GET round trips below, which run every command through a real
       ``/bin/sh`` (PUT) and do real local staging (GET). These derive
       ``max_filename_len`` FROM ``os.pathconf(..., "PC_NAME_MAX")`` rather
       than hard-coding 255, so the declared limit and the filesystem that
       enforces it are equal BY CONSTRUCTION -- on a filesystem with a
       different ``NAME_MAX`` the test moves with it instead of going
       vacuous, and it can never pass merely because the bed was more
       permissive than the number under test.

    Guard 1 alone would stay green if the arithmetic were right and the real
    world disagreed; guards 2 and 3 alone would stay green on any host whose
    ``NAME_MAX`` happened to exceed the declared limit. Neither substitutes
    for the other.
    """

    @pytest.mark.asyncio
    async def test_the_staged_temp_name_fits_a_limit_far_tighter_than_this_machines(
        self, tmp_path: Path
    ) -> None:
        """Declared limit 40, source basename exactly 40 -- and the emitted temp is <= 40.

        40 is nothing like this machine's ``NAME_MAX`` (255), deliberately:
        the temp PUT stages is created by the DEVICE, so the only limit that
        can be checked from a transcript is the declared one, and a limit the
        local filesystem would happily exceed proves the arithmetic is doing
        the work rather than the bed.
        """
        limit = 40
        name = "n" * limit
        src = tmp_path / name
        src.write_bytes(b"hello")
        dest_dir = Path("/dest")  # synthetic: nothing local is ever created for PUT's temp

        exec_cmd = _RecordingExec(answer_when=_size_answer(5))
        ft = _make_ft(exec_cmd, max_filename_len=limit)

        result = await ft.put_files([src], dest_dir, show_progress=False)
        assert result.is_ok, result.msg

        staged = Path(_parse_chunk_cmd(exec_cmd.calls[0]).group("path")).name
        assert len(staged) <= limit, (
            f"put_files accepted a {len(name)}-character basename against a "
            f"{limit}-character limit and then staged {staged!r}, which is "
            f"{len(staged)} characters -- the front-door check validated the "
            f"SOURCE name while the device is asked to create the STAGED one, so "
            f"a name inside the limit still fails late, on the device, with `File "
            f"name too long`"
        )
        assert staged.startswith("n"), (
            f"the staged name {staged!r} kept nothing of the destination basename -- "
            f"a temp nobody can trace back to its file is a worse debugging story "
            f"than a long one"
        )
        assert ".otto-" in staged, staged

    @pytest.mark.asyncio
    async def test_a_put_source_at_the_limit_lands_on_a_real_filesystem(
        self, tmp_path: Path
    ) -> None:
        """The real failure, end to end: a real shell, a real ext4 ``NAME_MAX``.

        Every command runs through ``/bin/sh``, so the redirect that creates
        the temp is a real ``open(2)`` against a real filesystem -- the exact
        call that returns ``ENAMETOOLONG`` when the staged basename overruns
        ``NAME_MAX``. The limit handed to the backend IS this filesystem's
        ``NAME_MAX``, so "at exactly ``max_filename_len``" and "at exactly
        what the filesystem enforces" are the same length here by
        construction.
        """
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()
        limit = os.pathconf(dest_dir, "PC_NAME_MAX")
        name = "p" * (limit - len(".bin")) + ".bin"
        assert len(name) == limit
        payload = b"a source name at the limit still has to land"
        src = tmp_path / name
        src.write_bytes(payload)

        exec_cmd = _ShellExecutingExec(cwd=tmp_path)
        ft = _make_ft(exec_cmd, max_filename_len=limit)

        result = await ft.put_files([src], dest_dir, show_progress=False)
        assert result.is_ok, (
            f"a {len(name)}-character source basename passed put_files' own "
            f"{limit}-character check and then failed to transfer: {result.msg}. "
            f"Transcript: {exec_cmd.calls[:1]}; shell said: {exec_cmd.outputs[:1]}"
        )
        assert (dest_dir / name).read_bytes() == payload
        assert list(dest_dir.glob("*.otto-*")) == [], "a staged temp was left behind"

    @pytest.mark.asyncio
    async def test_a_get_source_at_the_limit_stages_locally(self, tmp_path: Path) -> None:
        """GET's temp is LOCAL, and stages under the same naming -- same bug, same fix.

        ``get_files`` validates the remote basename against the same
        ``max_filename_len``, then :meth:`ShellFileTransfer._get_one` stages
        the decoded bytes in a same-directory local temp named exactly the way
        PUT names its remote one. The staging failure surfaces differently
        (an ``OSError`` caught into a failing ``Result``, not a device error),
        but it is the same overrun, so it needs its own guard rather than
        inheriting PUT's.
        """
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()
        limit = os.pathconf(dest_dir, "PC_NAME_MAX")
        name = "g" * (limit - len(".bin")) + ".bin"
        assert len(name) == limit
        payload = b"a remote name at the limit still has to come back"
        src = Path("/remote") / name

        encoded = base64.b64encode(payload).decode("ascii")
        exec_cmd = _RecordingExec(outputs=[str(len(payload)), encoded])
        ft = _make_ft(exec_cmd, max_filename_len=limit)

        result = await ft.get_files([src], dest_dir, show_progress=False)
        assert result.is_ok, (
            f"a {len(name)}-character remote basename passed get_files' own "
            f"{limit}-character check and then failed to stage locally: {result.msg}"
        )
        assert (dest_dir / name).read_bytes() == payload
        assert list(dest_dir.glob("*.otto-*")) == [], "a staged temp was left behind"

    @pytest.mark.parametrize("limit", [1, 6, 13, 14, 15, 32, 255])
    def test_the_budget_holds_at_every_limit_including_below_the_tokens_own_framing(
        self, limit: int
    ) -> None:
        """The documented degenerate case, made real rather than merely asserted in prose.

        ``.otto-`` plus an 8-hex token is 14 characters, so a limit under 14
        leaves nothing for the destination half and the arithmetic
        (``max_filename_len - len(suffix)``) goes negative. Two clauses keep
        the bound holding there, and they need SEPARATE assertions because
        they fail in different currencies:

        * the final ``[:max_filename_len]`` slice is what keeps the LENGTH
          inside the cap. Mutation-verified 2026-08-13: drop it and this test
          reds at limits 1, 6 and 13 (the suffix alone is 14 characters).
        * the ``max(..., 0)`` is what keeps the CONTENT right, and the length
          assertion above cannot see it. Mutation-verified the same way:
          replace it with a bare ``max_filename_len - len(suffix)`` and
          ``dest_name[:negative]`` slices from the RIGHT end, but the final
          slice clamps the result anyway, so the returned LENGTH is identical
          at all seven limits and only the returned VALUE changes -- ``"z"``
          where the real function gives ``"."``. An earlier version of this
          docstring claimed both were detected here when only the first was;
          the ``startswith`` assertion below is what makes the second true.

        The name at each limit is the worst case for that limit: longer than
        the cap to begin with.
        """
        framing = ".otto-"
        staged = staged_temp_name("z" * (limit + 40), limit)
        assert len(staged) <= limit, (
            f"staged_temp_name promised a name inside {limit} characters and "
            f"returned {staged!r} ({len(staged)} characters)"
        )
        if limit < 14:
            # Below the framing there is no room for any of the destination,
            # so what survives must be the SUFFIX. Without `max(..., 0)` the
            # negative slice returns destination characters instead, at the
            # same length -- which is why this is asserted on content.
            assert staged.startswith(framing[:limit]), (
                f"at a {limit}-character limit the token's framing is all that fits, so "
                f"staged_temp_name should have returned a prefix of {framing!r}; it "
                f"returned {staged!r}, which is destination text. That is the signature "
                f"of a negative slice reaching in from the RIGHT end -- the bound still "
                f"holds on length and has stopped holding on content"
            )

    def test_a_limit_of_one_returns_a_dot_which_is_out_of_contract(self) -> None:
        """The one degenerate output that is NOT visible degradation. Pinned as known.

        :func:`~otto.host.transfer.shell.staged_temp_name` promises that the
        bound "degrades visibly rather than silently" below the token's
        framing, and at every limit from 6 up it does: you get a recognisable
        ``.otto-`` prefix with progressively less of the token. At ``limit=1``
        it does not -- the return is ``"."``, which is a real basename that
        resolves to the CONTAINING DIRECTORY rather than to a file in it.

        No unix target in the BusyBox matrix declares such a limit (every one
        is the 255 default), and ``max_filename_len`` is a declared host field
        rather than anything probed, so this is unreachable in production.
        It is pinned rather than fixed because making it visible means raising,
        and that is a behaviour change with no caller asking for it. This test
        exists so the next reader finds the hazard already known and measured
        instead of rediscovering it as a bug.
        """
        assert staged_temp_name("z" * 41, 1) == ".", (
            "staged_temp_name(_, 1) no longer returns '.'. If it now raises or returns "
            "something else, that is a deliberate improvement -- update this test and "
            "the out-of-contract note in staged_temp_name's docstring together"
        )


# ---------------------------------------------------------------------------
# create() -- ctx guards
# ---------------------------------------------------------------------------


class TestShellFileTransferCreate:
    def _ctx(self, **overrides: object) -> TransferContext:
        base: dict[str, object] = {
            "transfer": "shell",
            "host_name": "tomato",
            "connections": MagicMock(spec=ConnectionManager),
            "exec_cmd": AsyncMock(),
            "userland": _declared_userland("-d"),
        }
        base.update(overrides)
        return TransferContext(**base)  # type: ignore[arg-type]

    def test_missing_exec_cmd_raises(self) -> None:
        with pytest.raises(ValueError, match="exec_cmd"):
            ShellFileTransfer.create(self._ctx(exec_cmd=None))

    def test_missing_userland_raises(self) -> None:
        with pytest.raises(ValueError, match="userland"):
            ShellFileTransfer.create(self._ctx(userland=None))

    def test_missing_connections_raises(self) -> None:
        with pytest.raises(ValueError, match="connections"):
            ShellFileTransfer.create(self._ctx(connections=None))

    def test_builds_with_a_full_context(self) -> None:
        ft = ShellFileTransfer.create(self._ctx())
        assert isinstance(ft, ShellFileTransfer)
        assert ft.host_families == frozenset({"unix"})


# ---------------------------------------------------------------------------
# The codec seam's inertness proof: the emitted lines, byte for byte
# ---------------------------------------------------------------------------

_PINNED_TOKEN = "0f1e2d3c"
"""A staged temp's random token, held still so a whole command can be a literal.

Eight lowercase hex characters, exactly the shape
:data:`~otto.host.transfer.shell._STAGING_TOKEN_HEX` produces, so every pinned
length below is the length a real run emits.
"""

# The binary-hostile payload `tests/busybox/test_shell_codec_contracts.py`
# uses -- NUL, newline, CR, 0xFF, single quote, backslash -- chosen here for a
# second reason: 13 bytes encode to 20 base64 characters, so a whole chunk
# command fits in this file as a literal string rather than as a computed one.
_PINNED_PAYLOAD = b"A\x00B\nC\rD\xffE'F\\G"
_PINNED_PAYLOAD_B64 = "QQBCCkMNRP9FJ0ZcRw=="

# The same 13 bytes framed the way `UuencodeCodec` frames them. Written out
# rather than computed so the pinned command below is a literal end to end:
# a bug in `_uu_frame` that a computed expectation would follow silently is
# exactly what this class exists to catch. `-` is the length character for 13
# bytes (chr(0x20 + 13)), the trailing backticks are uu's zero value (see
# `_uu_frame`'s `backtick=True`), and the lone-backtick line is the
# zero-length terminator every frame ends with before `end`.
_PINNED_PAYLOAD_UU = 'begin 600 otto\n-00!""D,-1/]%)T9<1P``\n`\nend'

# The 10253-byte payload `todo/busybox-parity-sweep-2026-08-11.md` measured uu
# against on all five matrix rows -- the hostile 13 bytes followed by a
# repeating 0..255 ramp, md5 cec24026d4cb12df00f2ef9be4222224. Reproduced here
# rather than invented so this module and that measurement are talking about
# the same bytes: three chunks at `_SHELL_CHUNK_BYTES` (4096/4096/2061), the
# last one partial, with every byte value present and the whole thing
# order-sensitive.
_UU_SWEEP_PAYLOAD = (_PINNED_PAYLOAD + bytes(range(256)) * 41)[:10253]

# One well-formed FULL body line: `M` declares 45 bytes and 60 backticks
# encode 45 zero bytes. Built as a literal so the malformed cases below are
# each one deliberate edit away from a line that really does decode -- a
# hand-typed "looks uuencoded" string is how the first version of those cases
# asserted the wrong failure.
_UU_FULL_LINE = "M" + "`" * 60


@pytest.fixture
def pinned_staged_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """Freeze the staged temp's random token so the emitted lines are literals.

    Replaces ``staged_temp_name`` in the shell module's OWN namespace -- not
    ``uuid`` itself, which is process-global -- so nothing outside this
    backend sees the substitution. The replacement drops the real function's
    ``max_filename_len`` truncation, which is safe here only because every
    destination name below is far shorter than the 255-character budget;
    ``TestStagedNameFitsTheDeclaredFilenameLimit`` is what tests the budget,
    and this class deliberately does not.
    """

    def _fixed(dest_name: str, max_filename_len: int) -> str:
        return f"{dest_name}.otto-{_PINNED_TOKEN}"

    monkeypatch.setattr(shell_module, "staged_temp_name", _fixed)


@pytest.mark.usefixtures("pinned_staged_name")
class TestEmittedCommandLinesArePinned:
    """Every command this backend emits, pinned byte for byte.

    THIS CLASS IS A CHANGE DETECTOR ON PURPOSE, which everywhere else in this
    file would be the criticism. It exists because
    :class:`~otto.host.transfer.shell.ShellCodec` split the chunk loops out of
    the staging skeleton, and a pure refactor is only credible if it can be
    shown to have changed nothing: these strings were captured from the code
    as it stood BEFORE that split and are asserted against the code after it.
    A future change that legitimately moves an emitted byte updates these
    literals, with the new bytes re-justified against the transport bound the
    way :class:`TestShellChunkLineLength` describes -- it does not delete
    them.

    Three details are pinned that a careless refactor breaks silently, named
    here so nobody reads them as incidental:

    - the FIRST chunk redirects with ``>`` and every later one with ``>>``,
      which is what makes an appending chunk's line exactly one character
      longer than the first's;
    - the decode flag is spelled ``-d``. ``base64 --decode`` is rejected on
      every BusyBox row tested, so a "clearer" long spelling breaks the
      devices this backend exists for;
    - ``bs=4096`` and the 5524/5525-character chunk lines are what actually
      crosses the wire, against the 9000-character ssh exec ceiling measured
      in ``tests/busybox/test_tier3_shell_transfer.py``. 5535 is the longest
      line Tier 3 itself measured; the numbers here are smaller only because
      ``/dest`` is a shorter destination directory than Tier 3's.

    Mutation-verified rather than assumed -- three mutations, each run and
    counted against this class's seven tests:

    - hard-coding the emitted decode flag to ``--decode`` instead of
      interpolating the resolved one: 3 red, 4 pass
      (``test_a_one_chunk_put_transcript_is_pinned``,
      ``test_an_appending_chunk_redirects_with_double_gt``,
      ``test_a_failed_chunk_reports_the_same_message_and_cleans_up_the_same_way``);
    - GET's ``2>/dev/null`` to ``2>&1``: 3 red, 4 pass -- the three GET
      tests, and none of the PUT ones;
    - making the FIRST chunk redirect with ``>>`` like every later one: 3
      red, 4 pass, the same three as the flag mutation.

    The 4 that stay green under each mutation are exactly the tests whose
    transcript contains no mutated command -- the three GET tests under a PUT
    mutation and vice versa, plus ``test_an_empty_source_put_transcript_is_pinned``,
    which is a PUT that emits no chunk command at all. So the reds are the
    tests that name the mutated line, not a blast radius.
    """

    @pytest.mark.asyncio
    async def test_a_one_chunk_put_transcript_is_pinned(self, tmp_path: Path) -> None:
        src = tmp_path / "stored.bin"
        src.write_bytes(_PINNED_PAYLOAD)

        exec_cmd = _RecordingExec(answer_when=_size_answer(len(_PINNED_PAYLOAD)))
        ft = _make_ft(exec_cmd)

        # A synthetic destination directory, for the reason
        # `TestShellChunkLineLength` gives: `_put_one` never touches it
        # locally, and `tmp_path`'s name is run-dependent, so interpolating it
        # would make these literals unpinnable.
        per_file = await ft._run_put([src], Path("/dest"), None)
        assert per_file[src].status is Status.Success, per_file[src].msg

        assert exec_cmd.calls == [
            f"printf '%s' '{_PINNED_PAYLOAD_B64}' | base64 -d > /dest/stored.bin.otto-0f1e2d3c",
            "stat -c %s -- /dest/stored.bin.otto-0f1e2d3c",
            "mv -- /dest/stored.bin.otto-0f1e2d3c /dest/stored.bin",
        ]

    @pytest.mark.asyncio
    async def test_an_empty_source_put_transcript_is_pinned(self, tmp_path: Path) -> None:
        """The empty-file branch stayed on the SHARED side of the seam.

        No codec produces a chunk for a zero-byte source, so ``: > <temp>`` is
        what creates the temp -- and it is emitted by the staging skeleton,
        not by ``Base64Codec``. If that had moved into the codec, this
        transcript would be unchanged; what would change is that the next
        codec would have to re-emit it. Pinned here so the shared-side
        placement is visible in a transcript rather than only in a docstring.
        """
        src = tmp_path / "empty.bin"
        src.write_bytes(b"")

        exec_cmd = _RecordingExec(answer_when=_size_answer(0))
        ft = _make_ft(exec_cmd)

        per_file = await ft._run_put([src], Path("/dest"), None)
        assert per_file[src].status is Status.Success, per_file[src].msg

        assert exec_cmd.calls == [
            ": > /dest/empty.bin.otto-0f1e2d3c",
            "stat -c %s -- /dest/empty.bin.otto-0f1e2d3c",
            "mv -- /dest/empty.bin.otto-0f1e2d3c /dest/empty.bin",
        ]

    @pytest.mark.asyncio
    async def test_an_appending_chunk_redirects_with_double_gt(self, tmp_path: Path) -> None:
        """Two FULL chunks: ``>`` then ``>>``, and the append is one character longer.

        The encoded blob is 5464 characters, far too long to sit in this file
        as a literal, so the frame around it is pinned literally and the blob
        is recomputed from the source bytes -- which also checks the slicing,
        since a wrong slice produces a blob that does not match.
        """
        payload = b"\xab" * (_SHELL_CHUNK_BYTES * 2)
        src = tmp_path / "payload.bin"
        src.write_bytes(payload)

        exec_cmd = _RecordingExec(answer_when=_size_answer(len(payload)))
        ft = _make_ft(exec_cmd)

        per_file = await ft._run_put([src], Path("/dest"), None)
        assert per_file[src].status is Status.Success, per_file[src].msg

        first_blob = base64.b64encode(payload[:_SHELL_CHUNK_BYTES]).decode("ascii")
        second_blob = base64.b64encode(payload[_SHELL_CHUNK_BYTES:]).decode("ascii")
        assert exec_cmd.calls == [
            f"printf '%s' '{first_blob}' | base64 -d > /dest/payload.bin.otto-0f1e2d3c",
            f"printf '%s' '{second_blob}' | base64 -d >> /dest/payload.bin.otto-0f1e2d3c",
            "stat -c %s -- /dest/payload.bin.otto-0f1e2d3c",
            "mv -- /dest/payload.bin.otto-0f1e2d3c /dest/payload.bin",
        ]
        assert len(exec_cmd.calls[0]) == 5524
        assert len(exec_cmd.calls[1]) == 5525, (
            "an appending chunk's line must be exactly one character longer than the "
            "first's -- that one character is the second `>`, and it is the whole "
            "reason the maximum emitted line belongs to an APPEND rather than to the "
            "chunk that creates the temp"
        )

    @pytest.mark.asyncio
    async def test_a_get_transcript_is_pinned(self, tmp_path: Path) -> None:
        """GET's size probe and chunk reads, byte for byte -- and no decode flag.

        The device only ENCODES for GET, so the trailing ``| base64`` carries
        no flag at all; the decode happens locally in Python. A flag appearing
        there would be a real behaviour change and reddens this assertion.
        """
        remote = Path("/remote/stored.bin")
        outputs = [str(len(_PINNED_PAYLOAD)), _PINNED_PAYLOAD_B64]

        exec_cmd = _RecordingExec(outputs=outputs)
        ft = _make_ft(exec_cmd)

        per_file = await ft._run_get([remote], tmp_path, None)
        assert per_file[remote].status is Status.Success, per_file[remote].msg
        assert (tmp_path / "stored.bin").read_bytes() == _PINNED_PAYLOAD

        assert exec_cmd.calls == [
            "stat -c %s -- /remote/stored.bin",
            "dd if=/remote/stored.bin bs=4096 skip=0 count=1 2>/dev/null | base64",
        ]

    @pytest.mark.asyncio
    async def test_a_get_transcript_with_wc_sizing_and_md5_verification_is_pinned(
        self, tmp_path: Path
    ) -> None:
        """The other two userland spellings, on the same transcript.

        ``wc -c <`` instead of ``stat -c %s --`` (a redirection TARGET, so no
        ``--`` terminator), and a closing ``md5sum --`` instead of reusing the
        size already fetched. Both come from
        :class:`~otto.host.userland.Userland`, and neither moved when the
        chunk loop did.
        """
        remote = Path("/remote/stored.bin")
        digest = hashlib.md5(_PINNED_PAYLOAD).hexdigest()  # noqa: S324
        outputs = [str(len(_PINNED_PAYLOAD)), _PINNED_PAYLOAD_B64, f"{digest}  -"]

        exec_cmd = _RecordingExec(outputs=outputs)
        ft = _make_ft(exec_cmd, stat_size="wc", checksum="md5sum")

        per_file = await ft._run_get([remote], tmp_path, None)
        assert per_file[remote].status is Status.Success, per_file[remote].msg

        assert exec_cmd.calls == [
            "wc -c < /remote/stored.bin",
            "dd if=/remote/stored.bin bs=4096 skip=0 count=1 2>/dev/null | base64",
            "md5sum -- /remote/stored.bin",
        ]

    @pytest.mark.asyncio
    async def test_a_failed_chunk_reports_the_same_message_and_cleans_up_the_same_way(
        self, tmp_path: Path
    ) -> None:
        """The failure text is part of the emitted behaviour, so it is pinned too.

        A codec now composes the reason ("writing a chunk to ... failed") and
        the staging skeleton prefixes the file it belongs to; before the seam
        both halves were one f-string. The joined result has to be the same
        string, and the ``rm -f --`` cleanup has to still follow it.
        """
        src = tmp_path / "stored.bin"
        src.write_bytes(_PINNED_PAYLOAD)

        exec_cmd = _RecordingExec(fail_when=lambda c: c.startswith("printf "))
        ft = _make_ft(exec_cmd)

        per_file = await ft._run_put([src], Path("/dest"), None)

        assert per_file[src].status is Status.Error
        assert per_file[src].msg == (
            f"{src}: writing a chunk to /dest/stored.bin.otto-0f1e2d3c failed "
            f"(exit 1): simulated failure: printf '%s' '{_PINNED_PAYLOAD_B64}' | "
            f"base64 -d > /dest/stored.bin.otto-0f1e2d3c"
        )
        assert exec_cmd.calls[-1] == "rm -f -- /dest/stored.bin.otto-0f1e2d3c"

    @pytest.mark.asyncio
    async def test_a_failed_chunk_read_reports_the_same_message(self, tmp_path: Path) -> None:
        """GET's mirror of the above: the codec names the chunk, the caller names the file."""
        remote = Path("/remote/stored.bin")

        exec_cmd = _RecordingExec(
            fail_when=lambda c: c.startswith("dd "),
            outputs=[str(len(_PINNED_PAYLOAD))],
        )
        ft = _make_ft(exec_cmd)

        per_file = await ft._run_get([remote], tmp_path, None)

        assert per_file[remote].status is Status.Error
        assert per_file[remote].msg == (
            "/remote/stored.bin: reading chunk 0 failed (exit 1): simulated failure: "
            "dd if=/remote/stored.bin bs=4096 skip=0 count=1 2>/dev/null | base64"
        )


# ---------------------------------------------------------------------------
# The uu codec: selection, emitted commands, and the container discipline
# ---------------------------------------------------------------------------

_UUDECODE_SHIM = """\
import binascii
import sys

args = sys.argv[1:]
out = None
infile = None
while args:
    arg = args.pop(0)
    if arg == "-o":
        out = args.pop(0)
    else:
        infile = arg

text = open(infile).read() if infile else sys.stdin.read()

data = bytearray()
started = False
ok = True
name = "uudecode.out"
for raw in text.split("\\n"):
    line = raw.rstrip("\\r")
    if not started:
        if line.startswith("begin "):
            parts = line.split(None, 2)
            if len(parts) == 3:
                name = parts[2]
            started = True
        continue
    if line == "end":
        break
    if not line:
        continue
    try:
        piece = binascii.a2b_uu(line)
    except Exception:
        ok = False
        break
    data += piece
else:
    ok = False

with open(out or name, "wb") as fh:
    if ok:
        fh.write(bytes(data))

if not ok:
    sys.stderr.write("uudecode: short file\\n")
    sys.exit(1)
"""

_UUENCODE_SHIM = """\
import binascii
import sys

args = sys.argv[1:]
if len(args) == 2:
    name = args[1]
    data = open(args[0], "rb").read()
else:
    name = args[0]
    data = sys.stdin.buffer.read()

lines = ["begin 664 " + name]
for start in range(0, len(data), 45):
    lines.append(binascii.b2a_uu(data[start:start + 45], backtick=True).decode().rstrip("\\n"))
lines += ["`", "end"]
sys.stdout.write("\\n".join(lines) + "\\n")
"""


def _uu_applet_shims(tmp_path: Path) -> Path:
    """Write ``uudecode``/``uuencode`` stand-ins and return the directory holding them.

    THIS DEV VM AND CI HAVE NEITHER APPLET -- sharutils is not installed and
    ``command -v uudecode`` answers nothing -- so a unit test that wanted a
    real one would either skip (which is how coverage evaporates) or depend on
    a package nobody declares. What is faked is exactly one thing: the applet.
    The command otto emits is still parsed and run by a real ``/bin/sh``, so
    the heredoc, the ``&&``, the ``$?`` capture across the ``rm``, the
    subshell exit and the ``cat`` redirect are all genuine.

    THE ONE BEHAVIOUR THAT MAKES THESE WORTH ANYTHING IS THE CONTAINER RULE:
    ``uudecode`` stops at the FIRST ``end`` line and exits 0. That is not
    invented for convenience, it is the measurement this whole codec's shape
    comes from -- appending three frames and decoding once returns 4096 of
    10253 bytes at rc=0 on all five matrix rows
    (``todo/busybox-parity-sweep-2026-08-11.md``, re-measured 2026-08-14).
    Two more measured behaviours are reproduced because guards below depend on
    them: with no ``-o`` the plaintext goes to the name in the ``begin``
    header rather than anywhere otto reads, and a frame that fails to parse
    still LEAVES ITS OUTPUT FILE behind (``uudecode: short file``, rc 1, an
    empty scratch on disk).

    WHAT KEEPS THE STAND-IN HONEST is not this docstring: it is
    ``tests/busybox/test_shell_codec_contracts.py``, which runs the same three
    behaviours through five real BusyBox artifacts, and
    ``tests/busybox/test_tier3_shell_transfer.py``, which runs this codec's
    real commands against a real ``uudecode`` over a real ssh channel. A
    stand-in that drifted from the applet would leave those green tests
    disagreeing with these ones.
    """
    shim_dir = tmp_path / "uu-applets"
    shim_dir.mkdir()
    for name, body in (("uudecode", _UUDECODE_SHIM), ("uuencode", _UUENCODE_SHIM)):
        script = shim_dir / name
        script.write_text(f"#!/usr/bin/env python3\n{body}")
        script.chmod(0o755)
    return shim_dir


async def _resolved_ft(exec_cmd: "Callable[..., object]", **kwargs: object) -> ShellFileTransfer:
    """A transfer whose userland has been resolved, as ``_run_put``/``_run_get`` leave it.

    ``_select_codec`` reads capabilities, and ``Userland`` refuses to be read
    before ``resolve()`` -- deliberately, so nothing consumes a value that was
    never asked for. Both callers await it first; a test calling the selector
    directly has to do the same, or it measures that guard instead of this one.
    """
    ft = _make_ft(exec_cmd, **kwargs)  # type: ignore[arg-type]
    await ft._userland.resolve()
    return ft


async def _resolved_uu_ft(exec_cmd: "Callable[..., object]", **kwargs: object) -> ShellFileTransfer:
    """A resolved transfer whose userland forces the uu path: base64 DECLARED absent.

    Declaring ``base64_flag="absent"`` is what a 1.16.1 device measures for
    itself, and a declaration is settled (see ``Userland.is_settled``), which
    is exactly the condition ``_select_codec`` switches codecs on.
    """
    return await _resolved_ft(exec_cmd, base64_flag="absent", **kwargs)


class TestCodecSelection:
    """Which codec a device gets, and what happens when it can run neither.

    ``_select_codec`` is the only place either direction decides, so these
    call it directly rather than inferring the answer from a transcript --
    the transcript is pinned separately by
    ``TestUuencodeEmittedCommands``, and a test that read the codec out of a
    command string would fail for two different reasons at once.

    Mutation-verified, each run and counted against this module's 96 tests:

    - preferring uu wherever it is available reds
      ``test_base64_wins_wherever_it_is_available`` -- inside a blast radius
      of 35, because that mutation moves EVERY host in this module off the
      default codec and most of its transcripts with it. The named test is
      the only one that says why;
    - dropping the ``is_settled`` guard on ``base64_flag``, so the cannot-ask
      default selects uu, reds exactly 1:
      ``test_an_unsettled_base64_absence_refuses_instead_of_selecting_a_codec``;
    - turning the applet refusal into "refuse unless PROVEN present", so the
      cannot-ask default refuses, reds exactly 1:
      ``test_an_unsettled_applet_attempts_uu_rather_than_refusing``;
    - dropping the applet check altogether, so a codec-less device gets uu,
      reds 4 -- this class's neither-codec and direction tests plus both
      ``_run_put``/``_run_get`` refusal tests;
    - having GET read ``uudecode`` reds exactly 1,
      ``TestShellGetRefusal::test_neither_codec_raises_before_any_command``,
      which is the one place a device with only ``uudecode`` is put in front
      of a GET.
    """

    @pytest.mark.asyncio
    async def test_base64_wins_wherever_it_is_available(self) -> None:
        """Both applets present AND base64 present: base64, in both directions.

        The preference is not a tie-break. base64 costs one command per chunk
        against uu's one-plus-a-scratch, its chunk command is shorter on the
        wire, and it is the path whose emitted lines are pinned byte for byte.
        """
        ft = await _resolved_ft(_RecordingExec())

        assert isinstance(ft._select_codec("put", "uudecode"), Base64Codec)
        assert isinstance(ft._select_codec("get", "uuencode"), Base64Codec)

    @pytest.mark.asyncio
    async def test_a_settled_absence_of_base64_selects_uu(self) -> None:
        ft = await _resolved_uu_ft(_RecordingExec())

        assert isinstance(ft._select_codec("put", "uudecode"), UuencodeCodec)
        assert isinstance(ft._select_codec("get", "uuencode"), UuencodeCodec)

    @pytest.mark.asyncio
    async def test_the_uu_codec_carries_the_hosts_own_filename_budget(self) -> None:
        """Not a default: the scratch has to fit the same cap the temp does."""
        ft = await _resolved_uu_ft(_RecordingExec(), max_filename_len=64)

        codec = ft._select_codec("put", "uudecode")
        assert isinstance(codec, UuencodeCodec)
        assert codec._max_filename_len == 64

    @pytest.mark.asyncio
    async def test_put_reads_uudecode_and_get_reads_uuencode(self) -> None:
        """Separate applets, separate directions -- neither may answer for the other.

        otto encodes locally and the device DECODES for PUT; the device
        ENCODES for GET. A device shipping only one of the two therefore
        supports exactly one direction, and gating both on one applet would
        either refuse a direction that works or attempt one that cannot.
        """
        decode_only = await _resolved_ft(
            _RecordingExec(),
            base64_flag="absent",
            applets={"uudecode": APPLET_PRESENT, "uuencode": APPLET_ABSENT},
        )
        assert isinstance(decode_only._select_codec("put", "uudecode"), UuencodeCodec)
        with pytest.raises(UnsupportedOnUserlandError, match="uuencode"):
            decode_only._select_codec("get", "uuencode")

        encode_only = await _resolved_ft(
            _RecordingExec(),
            base64_flag="absent",
            applets={"uudecode": APPLET_ABSENT, "uuencode": APPLET_PRESENT},
        )
        assert isinstance(encode_only._select_codec("get", "uuencode"), UuencodeCodec)
        with pytest.raises(UnsupportedOnUserlandError, match="uudecode"):
            encode_only._select_codec("put", "uudecode")

    @pytest.mark.asyncio
    async def test_a_device_with_neither_codec_is_refused_before_any_command(self) -> None:
        exec_cmd = _RecordingExec()
        ft = await _resolved_ft(
            exec_cmd,
            base64_flag="absent",
            applets={"uudecode": APPLET_ABSENT, "uuencode": APPLET_ABSENT},
        )

        with pytest.raises(UnsupportedOnUserlandError) as exc_info:
            ft._select_codec("put", "uudecode")

        assert exec_cmd.calls == [], f"refusal must precede every command, got {exec_cmd.calls}"
        assert "tomato" in str(exc_info.value)
        assert "base64" in str(exc_info.value)
        assert "uudecode" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_an_unsettled_base64_absence_refuses_instead_of_selecting_a_codec(self) -> None:
        """A probe round that never arrived is not a measurement, and must not pick a codec.

        ``base64_flag`` reads ``"absent"`` for a device that answered "no
        base64 here" AND for one otto could not ask at all -- the
        cannot-ask default. Switching codecs on the second is the expensive
        direction twice over: it would move a host that has always used
        base64 onto uu on the strength of nothing, and it would do so exactly
        when the transport is already refusing channels. So this branch
        refuses, and unlike the message it replaces it does not claim the
        device was found to have no base64.
        """
        userland = Userland(
            UserlandOptions(
                shell_dialect="ash",
                elevation="none",
                stat_size="stat",
                checksum="absent",
                timeout_style="absent",
            ),
            AsyncMock(side_effect=OSError("the device refused the probe channel")),
        )
        await userland.resolve()
        assert userland.base64_flag == "absent"
        assert not userland.is_settled("base64_flag"), (
            "this fixture exists to produce an UNSETTLED absence; a settled one "
            "would exercise the branch below the one under test"
        )

        ft = ShellFileTransfer(
            connections=MagicMock(spec=ConnectionManager),
            name="tomato",
            exec_cmd=_RecordingExec(),
            userland=userland,
        )
        with pytest.raises(UnsupportedOnUserlandError) as exc_info:
            ft._select_codec("put", "uudecode")
        assert "could not be asked" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_an_unsettled_applet_attempts_uu_rather_than_refusing(self) -> None:
        """base64 is measured gone and the applet batch never landed: try uu.

        The mirror image of the test above, and it goes the other way for a
        reason that is not inconsistency. There, an unmeasured value would
        have been used to CHANGE a working path; here base64 is already ruled
        out by a settled measurement, so refusing on an unmeasured applet
        buys nothing -- the alternative to attempting uu is not "keep using
        base64", it is "do not transfer at all". If the applet really is
        missing the device says so on the first chunk, with the staged temp
        cleaned up behind it.
        """
        ft = await _resolved_ft(_RecordingExec(), base64_flag="absent", applets={})

        assert isinstance(ft._select_codec("put", "uudecode"), UuencodeCodec)


@pytest.fixture
def pinned_uu_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """Freeze BOTH staged names uu draws, so its whole command can be a literal.

    uu needs two device-side names per file -- the staged temp and the
    scratch it decodes each chunk into -- and both come from
    ``staged_temp_name``, one draw each. ``pinned_staged_name`` freezes a
    single token and would therefore give the codec a scratch path EQUAL to
    the temp it appends to, which is the one arrangement that could never
    happen in production and would quietly turn every chunk into a
    self-append. This hands out the two tokens in call order instead, CYCLING
    so a test that stages more than one file still gets a distinct pair each
    time rather than a ``StopIteration`` raised inside a coroutine -- which
    arrives as a bare ``RuntimeError`` naming neither this fixture nor the
    draw that ran out.
    """
    tokens = itertools.cycle([_PINNED_TOKEN, _PINNED_SCRATCH_TOKEN])

    def _fixed(dest_name: str, max_filename_len: int) -> str:
        return f"{dest_name}.otto-{next(tokens)}"

    monkeypatch.setattr(shell_module, "staged_temp_name", _fixed)


_PINNED_SCRATCH_TOKEN = "5a6b7c8d"
"""The SECOND token :func:`pinned_uu_names` hands out -- the uu scratch's.

Different from :data:`_PINNED_TOKEN` on purpose and asserted to be: the two
names are drawn independently in production, and a fixture that made them
equal would hide a codec appending a file to itself.
"""


@pytest.mark.usefixtures("pinned_uu_names")
class TestUuencodeEmittedCommands:
    """What the uu codec puts on the wire, pinned byte for byte.

    A change detector, exactly like ``TestEmittedCommandLinesArePinned`` and
    for the same reason: every element of this command shape is a measurement
    (see ``UuencodeCodec``'s docstring), so a change to any of them is a
    change to a measured claim and has to be re-justified rather than
    absorbed.

    Mutation-verified, each run and counted against this module's 96 tests:

    - dropping ``-o`` from the ``uudecode`` reds 6 -- this class's two pinned
      commands and all four tests that run the command through a real shell,
      because the plaintext then goes to a file called ``otto`` in the
      working directory and the temp stays empty;
    - chaining the ``rm -f`` onto the success path with ``&&`` instead of
      running it unconditionally reds 3: the pinned command, and the two
      scratch-lifetime tests in ``TestUuencodeThroughARealShell``;
    - making the FIRST chunk redirect with ``>>`` reds 2, the two pinned
      commands, and nothing that runs a real shell -- which is the argument
      for pinning the transcript at all, since a stale temp is not something
      a green round trip can notice;
    - reusing the temp's own name as the scratch reds 7, including
      ``test_the_scratch_is_a_second_staged_name_beside_the_temp``, which is
      the one that names the fault rather than a symptom of it.
    """

    @pytest.mark.asyncio
    async def test_a_one_chunk_put_command_is_pinned(self, tmp_path: Path) -> None:
        src = tmp_path / "stored.bin"
        src.write_bytes(_PINNED_PAYLOAD)

        exec_cmd = _RecordingExec(answer_when=_size_answer(len(_PINNED_PAYLOAD)))
        ft = _make_ft(exec_cmd, base64_flag="absent")

        per_file = await ft._run_put([src], Path("/dest"), None)
        assert per_file[src].status is Status.Success, per_file[src].msg

        temp = "/dest/stored.bin.otto-0f1e2d3c"
        scratch = f"{temp}.otto-5a6b7c8d"
        assert exec_cmd.calls == [
            (
                f"uudecode -o {scratch} <<'ottoUU' && cat -- {scratch} > {temp}\n"
                f"{_PINNED_PAYLOAD_UU}\n"
                f"ottoUU\n"
                f"otto_rc=$?\n"
                f"rm -f -- {scratch}\n"
                f"(exit $otto_rc)"
            ),
            f"stat -c %s -- {temp}",
            f"mv -- {temp} /dest/stored.bin",
        ]

    @pytest.mark.asyncio
    async def test_the_first_chunk_truncates_and_later_ones_append(self, tmp_path: Path) -> None:
        """``>`` then ``>>``, matching base64's own discipline.

        A stale temp at the generated name is vanishingly unlikely and the
        cost of being wrong is a file with someone else's bytes in front of
        it, which the integrity check would catch and the user would then
        have to explain. One character buys the whole question away.
        """
        src = tmp_path / "payload.bin"
        src.write_bytes(b"\x01" * (_SHELL_CHUNK_BYTES + 1))

        exec_cmd = _RecordingExec(answer_when=_size_answer(_SHELL_CHUNK_BYTES + 1))
        ft = _make_ft(exec_cmd, base64_flag="absent")

        await ft._run_put([src], Path("/dest"), None)

        temp = "/dest/payload.bin.otto-0f1e2d3c"
        scratch = f"{temp}.otto-5a6b7c8d"
        chunk_cmds = [cmd for cmd in exec_cmd.calls if cmd.startswith("uudecode ")]
        assert len(chunk_cmds) == 2, chunk_cmds
        assert chunk_cmds[0].startswith(
            f"uudecode -o {scratch} <<'ottoUU' && cat -- {scratch} > {temp}\n"
        )
        assert chunk_cmds[1].startswith(
            f"uudecode -o {scratch} <<'ottoUU' && cat -- {scratch} >> {temp}\n"
        )

    @pytest.mark.asyncio
    async def test_the_scratch_is_a_second_staged_name_beside_the_temp(
        self, tmp_path: Path
    ) -> None:
        """Two independent names in one directory, neither able to be the other.

        The scratch is where a whole chunk's plaintext sits for the instant
        between ``uudecode`` and ``cat``. If it were the temp, every chunk
        would append the temp to itself; if it were outside the destination's
        directory it would risk a different filesystem and a different
        writability. Both are avoided by drawing it from the same
        :func:`~otto.host.transfer.shell.staged_temp_name` the temp comes
        from, which is also what keeps it inside ``max_filename_len``.
        """
        assert _PINNED_SCRATCH_TOKEN != _PINNED_TOKEN

        codec = UuencodeCodec(255)
        temp = Path("/dest/payload.bin.otto-0f1e2d3c")
        scratch = codec._scratch_for(temp)

        assert scratch != temp
        assert scratch.parent == temp.parent

    @pytest.mark.asyncio
    async def test_the_commands_length_does_not_depend_on_the_payloads_bytes(
        self, tmp_path: Path
    ) -> None:
        """A chunk command is the same length whatever the chunk contains.

        THE PROPERTY THE HEREDOC BUYS, and the reason the obvious one-line
        ``printf`` form was not taken. uu's alphabet contains the single
        quote -- byte value 7 encodes to one -- so a form that quotes each
        frame line grows four characters for every quote in the payload:
        measured against these same paths, 6275 characters for a byte ramp
        and 11330 for a chunk of ``0x07``, which is past the ~9000 the ssh
        exec channel carries. A quoted heredoc needs no escaping, so its
        length is a function of the paths and the chunk SIZE only.

        That matters because of what the alternative's failure would look
        like. The transport does not reject an over-long command, it drops
        the whole connection with no server log line
        (``_MEASURED_EXEC_LINE_LIMIT``), so a payload-dependent length turns
        "this file happens to contain a run of 0x07" into "the link is
        flaky". The Tier 3 guard measures ONE payload and could not see it.

        Mutation-verified: emitting the ``printf`` form instead reds this
        test with the two lengths hundreds of characters apart.
        """
        ramp = bytes((i * 7) % 256 for i in range(_SHELL_CHUNK_BYTES))
        quotes = bytes([0x07]) * _SHELL_CHUNK_BYTES
        lengths = []
        for payload in (ramp, quotes):
            src = tmp_path / "payload.bin"
            src.write_bytes(payload)
            exec_cmd = _RecordingExec(answer_when=_size_answer(len(payload)))
            ft = _make_ft(exec_cmd, base64_flag="absent")
            await ft._run_put([src], Path("/dest"), None)
            # The LONGEST command emitted, not the one whose prefix says
            # "uudecode": a form that escaped the frame would change that
            # prefix too, and a filter keyed on it would fail with an empty
            # sequence instead of with the two lengths this test is about.
            lengths.append(max(len(cmd) for cmd in exec_cmd.calls))

        assert lengths[0] == lengths[1], (
            f"a {_SHELL_CHUNK_BYTES}-byte chunk of ramp bytes emitted a "
            f"{lengths[0]}-character command and one of 0x07 bytes emitted "
            f"{lengths[1]}. uu encodes 0x07 as a single quote, so any form that has "
            f"to escape the frame grows with the payload -- and an over-long command "
            f"does not fail, it drops the connection with no log line"
        )

    @pytest.mark.asyncio
    async def test_a_get_chunk_command_is_pinned(self, tmp_path: Path) -> None:
        """``dd | uuencode <name>`` -- one command, no scratch, no decode flag.

        The name argument is required by ``uuencode`` and discarded by otto:
        every decode is local Python, so what the device calls the stream
        cannot affect where the bytes land.
        """
        remote = Path("/remote/stored.bin")
        frame = _uu_frame(_PINNED_PAYLOAD)

        exec_cmd = _RecordingExec(outputs=[str(len(_PINNED_PAYLOAD)), frame])
        ft = _make_ft(exec_cmd, base64_flag="absent", checksum="absent")

        per_file = await ft._run_get([remote], tmp_path, None)
        assert per_file[remote].status is Status.Success, per_file[remote].msg

        assert exec_cmd.calls == [
            "stat -c %s -- /remote/stored.bin",
            "dd if=/remote/stored.bin bs=4096 skip=0 count=1 2>/dev/null | uuencode otto",
        ]
        assert (tmp_path / "stored.bin").read_bytes() == _PINNED_PAYLOAD

    @pytest.mark.asyncio
    async def test_a_failed_chunk_sweeps_the_scratch_and_names_the_temp(
        self, tmp_path: Path
    ) -> None:
        """The transport dropped the command, so the device never ran its own ``rm``.

        The chunk command removes its scratch on both of ITS paths, which
        covers a decode that failed and a ``cat`` that failed. What it cannot
        cover is a command that never completed -- and that is the case this
        sweep exists for, so it is the case the test injects (``fail_when``
        means the command is never run at all, not that it ran and failed).
        """
        src = tmp_path / "stored.bin"
        src.write_bytes(_PINNED_PAYLOAD)

        exec_cmd = _RecordingExec(fail_when=lambda c: c.startswith("uudecode "))
        ft = _make_ft(exec_cmd, base64_flag="absent")

        per_file = await ft._run_put([src], Path("/dest"), None)

        temp = "/dest/stored.bin.otto-0f1e2d3c"
        scratch = f"{temp}.otto-5a6b7c8d"
        assert per_file[src].status is Status.Error
        assert per_file[src].msg.startswith(f"{src}: writing a chunk to {temp} failed (exit 1):")
        assert exec_cmd.calls[-2] == f"rm -f -- {scratch}", (
            f"the scratch sweep must run before the skeleton's own temp cleanup: {exec_cmd.calls}"
        )
        assert exec_cmd.calls[-1] == f"rm -f -- {temp}"


class TestUuencodeThroughARealShell:
    """The uu codec's commands, parsed and run by a real ``/bin/sh``.

    ``_RecordingExec`` proves what otto ASKED for; this proves what the
    request DOES. Everything but the applet is real here -- see
    :func:`_uu_applet_shims` for what is not, and for what keeps it honest.
    """

    @pytest.mark.asyncio
    async def test_a_multi_chunk_hostile_payload_round_trips(self, tmp_path: Path) -> None:
        """Three chunks including a partial tail, NUL/CR/quote/backslash and all.

        The same 10253-byte payload the Tier 2 sweep measured uu against, so
        this test and the five-row contract are talking about the same bytes.
        """
        payload = _UU_SWEEP_PAYLOAD
        src = tmp_path / "payload.bin"
        src.write_bytes(payload)
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()

        exec_cmd = _ShellExecutingExec(tmp_path, path_prefix=_uu_applet_shims(tmp_path))
        ft = _make_ft(exec_cmd, base64_flag="absent", checksum="absent")

        per_file = await ft._run_put([src], dest_dir, None)

        assert per_file[src].status is Status.Success, per_file[src].msg
        assert (dest_dir / "payload.bin").read_bytes() == payload

    @pytest.mark.asyncio
    async def test_the_codec_alone_appends_plaintext_not_frames(self, tmp_path: Path) -> None:
        """THE HEADLINE GUARD: decode per chunk, append plaintext -- or lose 6157 bytes.

        Driven at the CODEC rather than through ``_put_one``, deliberately.
        The staging skeleton verifies the temp before its ``mv``, so the
        end-to-end test above reddens under the container mutation too -- but
        it reddens as an integrity FAILURE, which is the backstop doing its
        job and says nothing about what the codec produced. Here there is no
        backstop: the loop reports success and the file on disk is short.

        THE MUTATION THIS EXISTS FOR is the naive port of base64's order --
        append every chunk's FRAMED text and let the device decode the
        concatenation once. It is the shape a reader reaches for, it exits
        rc=0 on all five matrix rows, and it returns 4096 of 10253 bytes
        (``todo/busybox-parity-sweep-2026-08-11.md``). Applied to
        ``UuencodeCodec.send_chunks``, this test reds on the LENGTH -- a
        truncated file under a clean outcome -- rather than on a status.
        """
        payload = _UU_SWEEP_PAYLOAD
        src = tmp_path / "payload.bin"
        src.write_bytes(payload)
        temp = tmp_path / "temp.bin"

        exec_cmd = _ShellExecutingExec(tmp_path, path_prefix=_uu_applet_shims(tmp_path))
        codec = UuencodeCodec(255)
        outcome = await codec.send_chunks(
            shell_module.PutChunkLoop(
                exec_cmd=exec_cmd,
                src=src,
                temp=temp,
                quoted_temp=shlex.quote(str(temp)),
                on_sent=lambda _chunk: None,
            )
        )

        assert outcome.error is None, outcome.error
        assert outcome.chunks == 3, outcome.chunks
        assert temp.read_bytes() == payload, (
            f"the codec's loop reported no error and left {len(temp.read_bytes())} of "
            f"{len(payload)} bytes on the temp. uu is a CONTAINER format: appending "
            f"frames and decoding once returns only the first chunk, at rc=0, on every "
            f"matrix row -- which is why this codec decodes per chunk and appends "
            f"PLAINTEXT"
        )

    @pytest.mark.asyncio
    async def test_no_scratch_survives_a_completed_transfer(self, tmp_path: Path) -> None:
        src = tmp_path / "payload.bin"
        src.write_bytes(_UU_SWEEP_PAYLOAD)
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()

        exec_cmd = _ShellExecutingExec(tmp_path, path_prefix=_uu_applet_shims(tmp_path))
        ft = _make_ft(exec_cmd, base64_flag="absent", checksum="absent")

        await ft._run_put([src], dest_dir, None)

        assert sorted(p.name for p in dest_dir.iterdir()) == ["payload.bin"], (
            f"a completed uu PUT left something beside the destination: "
            f"{sorted(p.name for p in dest_dir.iterdir())}"
        )

    @pytest.mark.asyncio
    async def test_a_failed_decode_leaves_no_scratch_behind(self, tmp_path: Path) -> None:
        """The measured leak, and the ``rm -f`` that closes it.

        A ``uudecode`` that cannot parse its frame still CREATES its output
        file before failing -- measured on all five rows, ``uudecode: short
        file`` at rc 1 with an empty scratch on disk. So the removal cannot
        ride the success path. Injected here by corrupting the frame the
        codec emits, which is the only way to reach a device-side decode
        failure without a device.

        Mutation-verified: chaining the ``rm -f`` with ``&&`` instead of
        running it unconditionally reds this test with the scratch still in
        the destination directory.
        """
        src = tmp_path / "payload.bin"
        src.write_bytes(b"hello world")
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()

        shims = _uu_applet_shims(tmp_path)

        class _CorruptingExec(_ShellExecutingExec):
            async def __call__(self, cmd: str, *args: object, **kwargs: object) -> CommandResult:
                if cmd.startswith("uudecode "):
                    cmd = cmd.replace("begin 600 otto", "begin 600 otto\nNOTUUENCODED")
                return await super().__call__(cmd, *args, **kwargs)  # type: ignore[arg-type]

        exec_cmd = _CorruptingExec(tmp_path, path_prefix=shims)
        ft = _make_ft(exec_cmd, base64_flag="absent", checksum="absent")

        per_file = await ft._run_put([src], dest_dir, None)

        assert per_file[src].status is Status.Error
        assert list(dest_dir.iterdir()) == [], (
            f"a failed uu chunk left files in the destination directory: "
            f"{sorted(p.name for p in dest_dir.iterdir())} -- the scratch must be "
            f"removed whether the decode worked or not"
        )

    @pytest.mark.asyncio
    async def test_a_get_round_trips_the_devices_own_uuencode(self, tmp_path: Path) -> None:
        remote_dir = tmp_path / "remote"
        remote_dir.mkdir()
        remote = remote_dir / "payload.bin"
        remote.write_bytes(_UU_SWEEP_PAYLOAD)
        landing = tmp_path / "landing"
        landing.mkdir()

        exec_cmd = _ShellExecutingExec(tmp_path, path_prefix=_uu_applet_shims(tmp_path))
        ft = _make_ft(exec_cmd, base64_flag="absent", checksum="absent")

        per_file = await ft._run_get([remote], landing, None)

        assert per_file[remote].status is Status.Success, per_file[remote].msg
        assert (landing / "payload.bin").read_bytes() == _UU_SWEEP_PAYLOAD


class TestUuFramingIsSafeAndStrict:
    """The two properties the frame itself has to have: unquotable, and undecodable-loud."""

    def test_no_frame_line_can_ever_close_the_heredoc_early(self) -> None:
        """The delimiter is unmatchable BY CONSTRUCTION, not by luck.

        A heredoc body ends at a line equal to the delimiter, so a payload
        able to produce that line could end the command early and let the
        rest of the frame be read as shell. uu's alphabet is 0x20-0x60 --
        every data character is ``chr(0x21..0x60)`` and the length character
        is ``chr(0x20 + n)`` -- which excludes lowercase entirely, so a
        delimiter carrying one cannot be produced by any payload.

        Asserted over every byte value in every position within a line rather
        than over a sample: the alphabet claim is what the safety rests on,
        so the test checks the alphabet, not a payload.
        """
        assert any(c.islower() for c in _UU_HEREDOC_DELIMITER), (
            "the delimiter's safety comes from carrying a character uu cannot "
            "emit; lowercase is that character class"
        )

        every_byte = bytes(range(256))
        alphabet = set()
        for length in (1, 2, 44, 45):
            for start in range(0, len(every_byte) - length, length):
                for line in _uu_frame(every_byte[start : start + length]).splitlines()[1:-2]:
                    alphabet.update(line)

        assert alphabet, "the sweep produced no body lines at all"
        assert all(0x21 <= ord(c) <= 0x60 for c in sorted(alphabet)), (
            f"a uu body line carried a character outside 0x21-0x60: "
            f"{sorted(c for c in alphabet if not 0x21 <= ord(c) <= 0x60)!r}"
        )
        assert not any(c.islower() for c in alphabet)
        assert " " not in alphabet, (
            "a body line carried a SPACE, so a transport that strips trailing "
            "whitespace could shorten a line and silently drop bytes from its decode"
        )

    @pytest.mark.parametrize(
        ("text", "why"),
        [
            ("", "carried no `begin` header"),
            (f"begin 664 otto\n{_UU_FULL_LINE}\n`", "never closed by an `end` line"),
            (f"begin 664 otto\n{_UU_FULL_LINE}\nend", "no zero-length terminator line"),
            (f"begin 664 otto\n`\n{_UU_FULL_LINE}\nend", "carried data after its terminator"),
            ("begin 664 otto\nNOTUU\n`\nend", "declares 46 bytes"),
            (f"begin 664 otto\n{_UU_FULL_LINE[:-1]}\n`\nend", "declares 45 bytes"),
            ("begin 664 otto\ngarbage line here\n`\nend", "declares 7 bytes"),
        ],
    )
    def test_a_malformed_frame_raises_rather_than_returning_what_it_managed(
        self, text: str, why: str
    ) -> None:
        """Strict for the reason base64's decode passes ``validate=True``.

        A lenient parse turns a truncated reply, a leaked stderr line or a
        login banner into fewer bytes with no error at all -- and GET's own
        integrity check compares against a size the DEVICE reported, so a
        short decode is caught, but as a size mismatch naming neither the
        chunk nor the reason. Failing here names both.
        """
        with pytest.raises(ValueError, match=re.escape(why)):
            shell_module._uu_unframe(text)

    def test_a_well_formed_frame_round_trips_every_byte_value(self) -> None:
        payload = bytes(range(256)) * 3
        assert shell_module._uu_unframe(_uu_frame(payload)) == payload

    def test_an_empty_frame_decodes_to_no_bytes(self) -> None:
        """What a zero-length ``dd`` read comes back as, measured over real ssh."""
        assert shell_module._uu_unframe("begin 664 otto\n`\nend\n") == b""
