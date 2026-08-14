"""The codec facts the `shell` transfer backend's design rests on.

The `shell` backend moves files using nothing but command execution: base64
over exec, temp-then-`mv`. Several of its load-bearing assumptions are pinned
here, inside a real BusyBox-only root rather than assumed from Tier 1's
dash-driven probe (`test_which_decode_spelling_base64_accepts` in
`test_applet_contracts.py` found the same decode-spelling fact through a
different mechanism — reproducing it here is what turns "the applet accepted
this flag under dash" into "the backend's own exec path would see the same
thing").

Four facts, one test each:

1. The decode spelling is `-d`, and ONLY `-d`. `base64 --decode` fails on
   every row, including the four that have the applet at all — rc=1 with an
   `unrecognized option` diagnostic on those four, rc=127 (`not found`) on
   1.16.1, which has no `base64` applet whatsoever. Both are LOUD failures,
   not the silent corruption a wrong DECODE VALUE would be — see
   `test_the_decode_spelling_matches_what_the_matrix_records`'s own docstring
   for why the guard still earns its place. `Userland.base64_flag`'s
   GNU-coreutils value is for unix hosts, never a BusyBox fallback.
2. A binary-hostile payload — a NUL (truncates a naive C-string read), an
   embedded LF and CR (breaks a line-oriented transport), a 0xFF byte
   (non-UTF-8), a quote and a backslash (shell metacharacters) — survives
   `base64 -d` unchanged, MD5-compared rather than eyeballed. Only runs on
   the four rows that have `base64` at all.
3. `base64 -d`'s output is safe to build a file from multiple exec calls:
   decoding three chunks separately and appending each with `>>` reassembles
   the original byte-for-byte, in order. This is the property PUT depends on,
   and reversing the append order is the mutation that proves the test
   actually reads the order rather than just the byte set. Also only the four
   `base64`-bearing rows.
4. `dd bs=N skip=S count=C` seeks by BLOCK, the range read Task 3's GET
   depends on. Present on all FIVE rows — `dd` and `base64` are different
   applets, and 1.16.1's missing `base64` says nothing about `dd`.

A fifth test cross-checks this module's `_EXPECTED_BASE64_FLAG` against
`test_applet_resolution.py`'s `_EXPECTED_BASE64` — a hygiene guard, not a new
fact, since the two tables record the same underlying presence/absence and
nothing before it kept them from silently disagreeing.

Three of the five tests above stage an intermediate file under `/tmp` (the
round trip, chunk-append and `dd` tests — the decode-spelling test and the
table cross-check never touch it). `tests/_fixtures/busybox_rootfs.py`'s
`_ROOTFS_DIRS` grew `/tmp` back for exactly this consumer — see that module's
comment for why it disappeared for one phase and came back for this one.
Drop `tmp` from that tuple again and every row of those three tests that
reaches a `/tmp` write fails loudly (a `can't create /tmp/...: nonexistent
directory` setup failure that fails the whole wrapped command, not just the
redirect) — except 1.16.1's round-trip and chunk-append rows, which return
before ever reaching `/tmp` because they take the base64-absent branch
instead.
"""

import asyncio
import base64
import hashlib
import shlex
import tempfile
from pathlib import Path

import pytest

from otto.host.transfer.shell import (
    _SHELL_CHUNK_BYTES,
    GetChunkLoop,
    PutChunkLoop,
    UuencodeCodec,
    _uu_frame,
    _uu_unframe,
)
from otto.result import CommandResult
from otto.utils import Status
from tests._fixtures.busybox import BUSYBOX_MATRIX, require_interpreter
from tests._fixtures.busybox_rootfs import busybox_rootfs, require_userns, run_in_rootfs
from tests.busybox.test_applet_resolution import _EXPECTED_BASE64

pytestmark = [pytest.mark.busybox]

# Measured across all five matrix rows before this task was planned (see the
# task brief) — `None` is not "unknown", it is "no base64 applet in this
# build at all", the gap the `shell` backend declares for 1.16.1 rather than a
# spelling to fall back to. Parametrised over `BUSYBOX_MATRIX` directly, never
# a table-filtered subset: a matrix row with no entry here fails every test
# below with a `KeyError` naming the version, rather than silently vanishing
# from the run. Task 2 reads this table's SHAPE (one entry per matrix row,
# `None` or a flag string) rather than importing these values, so a change
# here that keeps the shape does not require touching Task 2's code.
#
# This duplicates the presence half of `test_applet_resolution.py`'s
# `_EXPECTED_BASE64` (a version has a flag here IFF it has `True` there), and
# nothing enforces that agreement structurally — hence
# `test_the_flag_table_agrees_with_the_presence_table` below, which is the
# thing that would catch the two drifting apart.
_EXPECTED_BASE64_FLAG = {
    "1.16.1": None,
    "1.21.1": "-d",
    "1.28.1": "-d",
    "1.31.0": "-d",
    "1.35.0": "-d",
}

# 13 bytes chosen to be hostile to every layer a naive transport might use: a
# NUL at offset 1 (truncates a C-string read), an embedded LF and CR (breaks
# anything line-oriented), a 0xFF byte (not valid UTF-8), a single quote and a
# backslash (shell metacharacters, relevant because this whole backend moves
# bytes THROUGH a shell). Measured identical across the FOUR rows that have a
# base64 applet at all (1.16.1 has none, so the round trip cannot run there —
# see `test_a_binary_hostile_payload_survives_the_round_trip`): MD5
# a4119bcf623a896e535fc44c74e94d1d. `_HOSTILE_PAYLOAD_MD5` below is computed
# from the same bytes rather than that literal, so a transcription slip in
# either place would show up as this module disagreeing with itself.
_HOSTILE_PAYLOAD = b"A\x00B\nC\rD\xffE'F\\G"
# `usedforsecurity=False`: this MD5 is a round-trip checksum against a
# 13-byte fixture, never a security boundary — the same reason BusyBox's own
# `md5sum` is the tool being measured here, not merely a convenient hasher.
_HOSTILE_PAYLOAD_MD5 = hashlib.md5(_HOSTILE_PAYLOAD, usedforsecurity=False).hexdigest()

# Split into 4, 5 and 4 bytes — chosen so EVERY chunk's LENGTH is a
# non-multiple of 3 (4 mod 3 = 1, 5 mod 3 = 2, 4 mod 3 = 1), which means
# EVERY chunk needs its own base64 padding when encoded alone. Verified:
# encoding each of the three separately gives `QQBCCg==`, `Qw1E/0U=` and
# `J0ZcRw==` — all three end in `=`. That matters because padding mid-stream
# is exactly what makes decode-then-append non-obvious: a split where only
# SOME chunks pad (an earlier version of this constant split 4/6/3, where
# only the first chunk pads) leaves the other chunks as accidentally-whole
# base64 groups and exercises the padding case only partially. A prior
# version of this comment framed the choice as being about numeric split
# POINTS relative to the base64 group size ("not a multiple of 3") — that
# framing was itself the defect: it invited "fixing" the property by moving
# the split points, which is what produced the weaker 4/6/3 split. The
# property that matters is the per-chunk LENGTH, not where the chunk starts.
_HOSTILE_PAYLOAD_CHUNKS = (
    _HOSTILE_PAYLOAD[:4],  # b"A\x00B\n"
    _HOSTILE_PAYLOAD[4:9],  # b"C\rD\xffE"
    _HOSTILE_PAYLOAD[9:],  # b"'F\\G"
)


def _fields(stdout: str, *required: str) -> "dict[str, str]":
    """Parse `KEY=value` lines from a rootfs probe into a dict.

    Split on the FIRST `=` only, so a decoded value that itself contains `=`
    (base64 padding, if it ever leaked into an OUT field) is not truncated.
    Lines without `=` are ignored rather than raising.

    *required* names every key the caller is about to read, and a missing one
    fails HERE rather than at the read site. That distinction is load-bearing,
    not decoration: a caller reading a missing key through `dict.get(...)
    != "0"` sees `None != "0"`, which is `True` — a NEGATIVE assertion PASSING
    because the thing it was supposed to check never printed anything, not
    because it checked and found the value absent. Measured: deleting this
    module's `--decode` probe from the script entirely (so `LONG_RC` is never
    printed) left the old `fields.get("LONG_RC") != "0"` check green — the
    exact silent-pass shape this function now closes, once, for every caller
    instead of at each read site.
    """
    fields: "dict[str, str]" = {}
    for line in stdout.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            fields[key] = value
    missing = [key for key in required if key not in fields]
    assert not missing, (
        f"the probe script did not print {missing} — a missing field must "
        f"fail loud here, not be silently read as a negative result by "
        f"whichever assertion reads it next: {stdout!r}"
    )
    return fields


def _assert_base64_absent(root, release, consequence: str) -> None:
    """Prove absence STRUCTURALLY (`command -v`), and say what breaks for the caller.

    Shared by every test below for the 1.16.1 row, so each one still runs a
    real assertion against a real root instead of skipping — a skipped row
    and a passing one are the same line in a summary.
    """
    result = run_in_rootfs(
        root, "command -v base64 >/dev/null 2>&1 && echo PRESENCE=PRESENT || echo PRESENCE=ABSENT"
    )
    assert result.returncode == 0, f"the probe did not run: {result.stderr}"
    fields = _fields(result.stdout, "PRESENCE")
    assert fields["PRESENCE"] == "ABSENT", (
        f"BusyBox {release.version} was recorded as having no base64 applet "
        f"at all, but command -v found one — {consequence}: {result.stdout!r}"
    )


@pytest.mark.parametrize("release", BUSYBOX_MATRIX, ids=[r.version for r in BUSYBOX_MATRIX])
def test_the_decode_spelling_matches_what_the_matrix_records(release):
    """`base64 -d` decodes; `base64 --decode` must NOT.

    The negative half is the point, not a bonus assertion. `--decode` fails
    LOUD here — rc=1 with `unrecognized option` on every row that has the
    applet — so this is not guarding against silent corruption the way the
    round-trip test is; it is guarding against a REGRESSION that would only
    be noticed the day someone "generalises" the `shell` backend to accept
    the GNU long spelling and ships it to a target where every decode then
    fails outright. A loud break in production is still a break nobody meant
    to ship, and this is the assertion that catches it before that happens.
    """
    require_interpreter(release.arch)
    require_userns()

    flag = _EXPECTED_BASE64_FLAG[release.version]

    with busybox_rootfs(release) as root:
        if flag is None:
            _assert_base64_absent(
                root,
                release,
                "the `shell` backend's declared decode-spelling gap for this row is stale",
            )
            return

        result = run_in_rootfs(
            root,
            f'SHORT_OUT=$(echo aGk= | base64 {flag} 2>/dev/null); echo "SHORT_RC=$?"; '
            'echo "SHORT_OUT=$SHORT_OUT"; '
            'echo aGk= | base64 --decode >/dev/null 2>&1; echo "LONG_RC=$?"',
        )
        assert result.returncode == 0, f"the probe did not run: {result.stderr}"
        fields = _fields(result.stdout, "SHORT_RC", "SHORT_OUT", "LONG_RC")

        assert fields["SHORT_RC"] == "0", (
            f"BusyBox {release.version}: the recorded decode spelling "
            f"`base64 {flag}` exited {fields['SHORT_RC']!r} rather than 0 "
            f"({result.stdout!r}) — the `shell` transfer backend would treat "
            f"every decode on this row as a hard failure"
        )
        assert fields["SHORT_OUT"] == "hi", (
            f"BusyBox {release.version}: the recorded decode spelling "
            f"`base64 {flag}` did not decode `aGk=` to `hi` ({result.stdout!r}) "
            f"— the `shell` transfer backend would corrupt every file it "
            f"decodes on this row"
        )
        assert fields["LONG_RC"] != "0", (
            f"BusyBox {release.version} accepted GNU's `base64 --decode` "
            f"({result.stdout!r}) — this is the row where 'generalising' the "
            f"backend to the long spelling would silently keep working while "
            f"every other row broke loud"
        )


@pytest.mark.parametrize("release", BUSYBOX_MATRIX, ids=[r.version for r in BUSYBOX_MATRIX])
def test_a_binary_hostile_payload_survives_the_round_trip(release):
    """A NUL/LF/CR/0xFF/quote/backslash payload decodes byte-for-byte, MD5-compared.

    The payload is base64-ENCODED ON THE HOST before it ever reaches the
    rootfs script — this test measures whether BusyBox's `base64 -d` corrupts
    the bytes on the way BACK out, not whether Python's own encoder is
    correct. Compared by MD5 rather than by eye, because a payload with an
    embedded NUL cannot be read as a plain string on either side of the
    comparison without risking exactly the truncation this payload is chosen
    to expose.
    """
    require_interpreter(release.arch)
    require_userns()

    flag = _EXPECTED_BASE64_FLAG[release.version]

    with busybox_rootfs(release) as root:
        if flag is None:
            _assert_base64_absent(
                root,
                release,
                "the round trip below cannot run on this row at all",
            )
            return

        encoded = base64.b64encode(_HOSTILE_PAYLOAD).decode("ascii")
        result = run_in_rootfs(
            root,
            f"printf '%s' '{encoded}' | base64 {flag} >/tmp/payload.bin; md5sum /tmp/payload.bin",
        )
        assert result.returncode == 0, f"the probe did not run: {result.stderr}"
        seen_md5 = result.stdout.split()[0] if result.stdout.split() else ""
        assert seen_md5 == _HOSTILE_PAYLOAD_MD5, (
            f"BusyBox {release.version}: the binary-hostile payload "
            f"round-tripped through `base64 {flag}` to md5 {seen_md5!r}, not "
            f"{_HOSTILE_PAYLOAD_MD5!r} ({result.stdout!r}) — the `shell` "
            f"transfer backend would corrupt exactly this class of file"
        )


@pytest.mark.parametrize("release", BUSYBOX_MATRIX, ids=[r.version for r in BUSYBOX_MATRIX])
def test_chunk_append_reassembles_in_order(release):
    """Three chunks, decoded and `>>`-appended separately, reassemble IN ORDER.

    This is the property PUT depends on — a file sent as several exec calls
    rather than one — and the one nobody would notice breaking: a chunker
    that silently transposed two pieces would still produce a file of the
    right length and the right byte SET, and only an order-sensitive
    comparison (MD5, not size) catches it. Each chunk is base64-encoded
    SEPARATELY and decoded through its own `base64 -d` invocation, matching
    how the backend would actually build the file across several exec calls
    rather than one.
    """
    require_interpreter(release.arch)
    require_userns()

    flag = _EXPECTED_BASE64_FLAG[release.version]

    with busybox_rootfs(release) as root:
        if flag is None:
            _assert_base64_absent(
                root,
                release,
                "the chunk-append reassembly below cannot run on this row at all",
            )
            return

        decode_and_append = " ".join(
            f"printf '%s' '{base64.b64encode(chunk).decode('ascii')}' | "
            f"base64 {flag} >>/tmp/reassembled.bin;"
            for chunk in _HOSTILE_PAYLOAD_CHUNKS
        )
        result = run_in_rootfs(root, f"{decode_and_append} md5sum /tmp/reassembled.bin")
        assert result.returncode == 0, f"the probe did not run: {result.stderr}"
        seen_md5 = result.stdout.split()[0] if result.stdout.split() else ""
        assert seen_md5 == _HOSTILE_PAYLOAD_MD5, (
            f"BusyBox {release.version}: three chunks decoded and appended in "
            f"order via successive `base64 {flag} >>` reassembled to md5 "
            f"{seen_md5!r}, not {_HOSTILE_PAYLOAD_MD5!r} ({result.stdout!r}) "
            f"— PUT builds files exactly this way, across multiple exec calls, "
            f"and this is the guard that catches a chunk landing out of order"
        )


# GET's range read (Task 3) depends on `dd` seeking by BLOCK — `bs` sets the
# block size, `skip` the number of blocks to skip on the INPUT side, `count`
# the number of blocks to copy — not on byte offsets. `skip=1 count=1` at
# `bs=4` must land on the file's SECOND four-byte block, not its first: a
# `dd` that silently ignored `skip`/`count` and echoed the file's start (or
# its whole content) would still exit 0, so the expected slice is chosen to
# differ from both of those wrong answers.
_DD_PROBE_PAYLOAD = "0123456789ABCDEF"
_DD_PROBE_BLOCK_SIZE = 4
_DD_PROBE_SKIP = 1
_DD_PROBE_COUNT = 1
_DD_PROBE_EXPECTED = "4567"


@pytest.mark.parametrize("release", BUSYBOX_MATRIX, ids=[r.version for r in BUSYBOX_MATRIX])
def test_dd_reads_a_block_range_with_bs_skip_and_count(release):
    """`dd bs=N skip=S count=C` — the range read Task 3's GET depends on.

    Present on every row, INCLUDING 1.16.1: `dd` and `base64` are different
    applets, and 1.16.1's missing `base64` says nothing about whether `dd` is
    there too. Not branched on `_EXPECTED_BASE64_FLAG` for exactly that
    reason — this fact is independent of the decode-spelling table above, and
    parametrising it separately over the full matrix is what lets 1.16.1
    prove that independence rather than merely asserting it.
    """
    require_interpreter(release.arch)
    require_userns()

    with busybox_rootfs(release) as root:
        result = run_in_rootfs(
            root,
            f"printf '%s' '{_DD_PROBE_PAYLOAD}' >/tmp/dd_probe.bin; "
            f"dd if=/tmp/dd_probe.bin bs={_DD_PROBE_BLOCK_SIZE} skip={_DD_PROBE_SKIP} "
            f"count={_DD_PROBE_COUNT} 2>/dev/null",
        )

    assert result.returncode == 0, f"the probe did not run: {result.stderr}"
    assert result.stdout == _DD_PROBE_EXPECTED, (
        f"BusyBox {release.version}: `dd bs={_DD_PROBE_BLOCK_SIZE} "
        f"skip={_DD_PROBE_SKIP} count={_DD_PROBE_COUNT}` read {result.stdout!r} "
        f"from {_DD_PROBE_PAYLOAD!r}, not {_DD_PROBE_EXPECTED!r} — Task 3's GET "
        f"range read depends on this exact seek-by-block behaviour"
    )


@pytest.mark.parametrize("release", BUSYBOX_MATRIX, ids=[r.version for r in BUSYBOX_MATRIX])
def test_the_flag_table_agrees_with_the_presence_table(release):
    """`_EXPECTED_BASE64_FLAG` (here) and `_EXPECTED_BASE64` (`test_applet_resolution.py`)
    must never disagree about presence.

    Two tables record the same underlying fact — whether a row's BusyBox
    build has a `base64` applet at all — through two different mechanisms
    (a per-row flag string vs. a per-row bool), and nothing before this test
    cross-checked them. An edit to one that forgot the other — a new artifact
    whose build gained or lost the applet, updated in only one file — would
    leave the two modules silently contradicting each other about the same
    version, each internally consistent and neither aware of the other.
    Needs no rootfs: both tables are plain Python dicts, so this is a data
    comparison, not a measurement.
    """
    has_flag = _EXPECTED_BASE64_FLAG[release.version] is not None
    present = _EXPECTED_BASE64[release.version]
    assert has_flag == present, (
        f"BusyBox {release.version}: this module's _EXPECTED_BASE64_FLAG "
        f"records base64 as {'present' if has_flag else 'absent'}, but "
        f"test_applet_resolution._EXPECTED_BASE64 records it as "
        f"{'present' if present else 'absent'} — the two tables record the "
        f"same fact and must agree"
    )


# ---------------------------------------------------------------------------
# The uu codec's contracts -- the same four facts, for the OTHER codec
# ---------------------------------------------------------------------------
#
# These run on all five rows, unbranched, and that is the difference from the
# base64 block above: `uudecode` and `uuencode` are present on every matrix
# row (`test_applet_resolution.py`'s `_EXPECTED_UUDECODE`/`_EXPECTED_UUENCODE`
# both record True five times), so there is no absent-row branch to take. The
# row that matters most is 1.16.1 -- the one with no `base64` applet at all,
# and therefore the one whose only path through the `shell` backend is this
# codec.
#
# WHAT IS EXECUTED HERE IS THE PRODUCT'S OWN COMMAND STRING, not a
# reconstruction of it. Each test drives the real `UuencodeCodec` against a
# recording exec, collects exactly what it would have sent, and runs THOSE
# lines inside the rootfs. That is what makes these tests fail when the codec
# changes shape, rather than when someone forgets to update a transcript
# copied into this file -- the failure mode the base64 block above accepts
# because it predates the codec seam.

_UU_SWEEP_PAYLOAD = (_HOSTILE_PAYLOAD + bytes(range(256)) * 41)[:10253]
"""The 10253 bytes `todo/busybox-parity-sweep-2026-08-11.md` measured uu against.

The hostile 13 followed by a repeating 0..255 ramp: three chunks at
``_SHELL_CHUNK_BYTES`` (4096/4096/2061, the last one partial), every byte
value present, and order-sensitive, so a transposed or short chunk changes the
digest rather than only the length. Reproduced from the sweep's own
construction rather than invented, which is checkable -- its md5 is the
`cec24026d4cb12df00f2ef9be4222224` that document records, asserted below so a
future edit to these bytes cannot quietly stop being the measured payload.
"""

_UU_SWEEP_PAYLOAD_MD5 = hashlib.md5(_UU_SWEEP_PAYLOAD, usedforsecurity=False).hexdigest()
_UU_SWEEP_PAYLOAD_SPEC_MD5 = "cec24026d4cb12df00f2ef9be4222224"


class _Recorder:
    """Collect the commands a codec emits, answering every one with success."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, cmd: str, **_kwargs: object) -> CommandResult:
        self.calls.append(cmd)
        return CommandResult(command=cmd, value="", status=Status.Success, retcode=0)


def _uu_put_commands(temp: str) -> "list[str]":
    """Exactly what ``UuencodeCodec.send_chunks`` would send for the sweep payload."""
    recorder = _Recorder()
    codec = UuencodeCodec(255)
    with tempfile.TemporaryDirectory() as workdir:
        src = Path(workdir) / "payload.bin"
        src.write_bytes(_UU_SWEEP_PAYLOAD)
        outcome = asyncio.run(
            codec.send_chunks(
                PutChunkLoop(
                    exec_cmd=recorder,
                    src=src,
                    temp=Path(temp),
                    quoted_temp=shlex.quote(temp),
                    on_sent=lambda _chunk: None,
                )
            )
        )
    assert outcome.error is None, outcome.error
    assert outcome.chunks == 3, (
        f"the sweep payload must split into three chunks for this measurement to cover a "
        f"partial tail; it produced {outcome.chunks}"
    )
    return recorder.calls


def _uu_get_command(remote: str) -> str:
    """Exactly what ``UuencodeCodec.fetch_chunks`` would send for one chunk."""
    recorder = _Recorder()
    codec = UuencodeCodec(255)
    asyncio.run(
        codec.fetch_chunks(
            GetChunkLoop(exec_cmd=recorder, src=Path(remote), total=1, on_received=lambda _b: None)
        )
    )
    return recorder.calls[0]


def test_the_uu_sweep_payload_is_the_one_the_measurement_names():
    """These bytes still digest to what the parity sweep recorded for them.

    Pure, like ``test_the_flag_table_agrees_with_the_presence_table``: no
    rootfs, no interpreter. Every uu assertion below compares a device's
    answer against ``_UU_SWEEP_PAYLOAD_MD5``, which is computed from the bytes
    themselves and so is self-consistent no matter what those bytes are. This
    is the one place the literal from the measurement appears, so an edit to
    the payload reddens here instead of silently re-pointing five rows'
    worth of contracts at a different measurement.
    """
    assert _UU_SWEEP_PAYLOAD_MD5 == _UU_SWEEP_PAYLOAD_SPEC_MD5, (
        f"the uu sweep payload digests to {_UU_SWEEP_PAYLOAD_MD5!r}, not the "
        f"{_UU_SWEEP_PAYLOAD_SPEC_MD5!r} recorded in "
        f"todo/busybox-parity-sweep-2026-08-11.md for the 10253 bytes uu was measured "
        f"against -- these tests would still pass and would no longer be that measurement"
    )


@pytest.mark.parametrize("release", BUSYBOX_MATRIX, ids=[r.version for r in BUSYBOX_MATRIX])
def test_the_backends_own_uu_put_commands_reassemble_the_payload(release):
    """PUT's real chunk commands, run by this row's own ash, land the right bytes.

    THREE FACTS AT ONCE, and they are one measurement rather than three
    because they cannot be separated without running the same commands three
    times against a per-test rootfs budget that does not allow it (see
    `busybox_rootfs.py`'s coupled-budget block):

    1. ``uudecode -o <scratch>`` WORKS ON THIS ROW. It is the option this
       whole path depends on and the one most likely to be missing on 1.16.1,
       where ``base64 --decode`` and ``busybox --list`` both fail.
    2. Decoding per chunk and appending PLAINTEXT reassembles all 10253 bytes
       in order, MD5-compared rather than eyeballed -- the payload has a NUL
       in it, so no string comparison on either side is safe.
    3. NO SCRATCH SURVIVES. The chunk command removes it whether its decode
       worked or not, which matters because a failed ``uudecode`` leaves the
       file behind (see the failure test below).
    """
    require_interpreter(release.arch)
    require_userns()

    temp = "/tmp/staged.bin"
    script = "\n".join([*_uu_put_commands(temp), f"md5sum {temp}", "ls /tmp | tr '\\n' ' '"])

    with busybox_rootfs(release) as root:
        result = run_in_rootfs(root, script)

    assert result.returncode == 0, f"the probe did not run: {result.stderr}"
    lines = result.stdout.splitlines()
    seen_md5 = lines[0].split()[0] if lines and lines[0].split() else ""
    assert seen_md5 == _UU_SWEEP_PAYLOAD_MD5, (
        f"BusyBox {release.version}: the `shell` backend's own uu chunk commands "
        f"reassembled the {len(_UU_SWEEP_PAYLOAD)}-byte payload to md5 {seen_md5!r}, not "
        f"{_UU_SWEEP_PAYLOAD_MD5!r} ({result.stdout!r}) -- a PUT to this row would land "
        f"the wrong bytes, and `uudecode -o` not being supported here is the first thing "
        f"to check"
    )
    left_behind = sorted(lines[-1].split()) if len(lines) > 1 else []
    assert left_behind == ["staged.bin"], (
        f"BusyBox {release.version}: a completed uu PUT left {left_behind} in /tmp, and "
        f"only the staged temp belongs there -- the per-chunk scratch must be removed by "
        f"the same command that creates it"
    )


@pytest.mark.parametrize("release", BUSYBOX_MATRIX, ids=[r.version for r in BUSYBOX_MATRIX])
def test_appending_frames_and_decoding_once_truncates_at_rc_zero(release):
    """The measured hazard this codec's whole shape exists to avoid, pinned per row.

    ``base64`` is a stream codec and ``uuencode`` is a CONTAINER format, so
    the naive port of PUT's base64 order -- append every chunk's framed text,
    let the device decode the concatenation once at the end -- returns only
    the FIRST chunk. It does not fail while doing it: rc is 0, and 4096 of
    10253 bytes land under a clean exit.

    A NEGATIVE MEASUREMENT, deliberately, and it is the one that would catch
    the rewrite. The positive test above proves the codec's real order works;
    it would go on passing if someone "simplified" the loop back into base64's
    shape ONLY on a device this suite never runs. This one records what that
    device would actually do, on all five rows, so the hazard is a fact in the
    tree rather than a sentence in a docstring.
    """
    require_interpreter(release.arch)
    require_userns()

    chunks = [
        _UU_SWEEP_PAYLOAD[i : i + _SHELL_CHUNK_BYTES]
        for i in range(0, len(_UU_SWEEP_PAYLOAD), _SHELL_CHUNK_BYTES)
    ]
    appended = "\n".join(
        f"cat >> /tmp/all.uu <<'ottoUU'\n{_uu_frame(chunk)}\nottoUU" for chunk in chunks
    )
    script = (
        f"{appended}\n"
        "uudecode -o /tmp/decoded.bin < /tmp/all.uu\n"
        'echo "RC=$?"\n'
        'echo "LEN=$(wc -c < /tmp/decoded.bin)"\n'
    )

    with busybox_rootfs(release) as root:
        result = run_in_rootfs(root, script)

    assert result.returncode == 0, f"the probe did not run: {result.stderr}"
    fields = _fields(result.stdout, "RC", "LEN")
    assert fields["RC"] == "0", (
        f"BusyBox {release.version}: appending three uu frames and decoding once exited "
        f"{fields['RC']!r} rather than 0 ({result.stdout!r}). A LOUD failure here would "
        f"be good news and would change this codec's rationale -- the reason PUT decodes "
        f"per chunk is that this shape is SILENT"
    )
    assert fields["LEN"] == str(len(chunks[0])), (
        f"BusyBox {release.version}: appending three uu frames and decoding once yielded "
        f"{fields['LEN']} bytes, not the first chunk's {len(chunks[0])} "
        f"({result.stdout!r}). The `shell` backend's uu path is shaped around this row "
        f"returning exactly one chunk at rc=0; if that changed, re-read "
        f"`UuencodeCodec`'s docstring before relying on it"
    )


@pytest.mark.parametrize("release", BUSYBOX_MATRIX, ids=[r.version for r in BUSYBOX_MATRIX])
def test_this_rows_uuencode_output_parses_with_the_backends_own_decoder(release):
    """GET's real command, and the frame this row's ``uuencode`` actually emits.

    The device only ENCODES for GET, so what has to survive here is otto's
    LOCAL parse of a real device's output -- ``_uu_unframe``, the same
    function the backend runs, against this row's own ``uuencode`` rather than
    against Python's encoder. That distinction is the whole value: a decoder
    tested only against its own encoder proves the pair self-consistent and
    nothing about the device.

    The source file is staged by the PUT commands rather than by a shell
    one-liner, because a 10253-byte binary-hostile payload cannot be written
    into a BusyBox rootfs any other way that does not itself depend on a
    codec -- and on 1.16.1 there is no ``base64`` to depend on.
    """
    require_interpreter(release.arch)
    require_userns()

    remote = "/tmp/source.bin"
    script = "\n".join([*_uu_put_commands(remote), "echo FRAME", _uu_get_command(remote)])

    with busybox_rootfs(release) as root:
        result = run_in_rootfs(root, script)

    assert result.returncode == 0, f"the probe did not run: {result.stderr}"
    _, _, frame = result.stdout.partition("FRAME\n")
    assert frame.startswith("begin "), (
        f"BusyBox {release.version}: `dd | uuencode` did not start a frame "
        f"({result.stdout[:200]!r})"
    )
    decoded = _uu_unframe(frame)
    seen_md5 = hashlib.md5(decoded, usedforsecurity=False).hexdigest()
    expected = hashlib.md5(
        _UU_SWEEP_PAYLOAD[:_SHELL_CHUNK_BYTES], usedforsecurity=False
    ).hexdigest()
    assert seen_md5 == expected, (
        f"BusyBox {release.version}: otto's own frame parser turned this row's "
        f"`dd | uuencode` output into md5 {seen_md5!r} rather than the first chunk's "
        f"{expected!r} ({len(decoded)} bytes) -- a GET from this row would land the wrong "
        f"bytes"
    )
