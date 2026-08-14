"""Exit criterion 3: otto's `shell` transfer moves real files over a real ssh channel.

Phase 4 built :mod:`otto.host.transfer.shell` — PUT and GET with nothing but
command execution, base64 over exec, temp-then-`mv`. Everything that measured
it until now measured half of it:

* `tests/unit/host/transfer/test_shell_transfer.py` drives the backend against
  a recording fake and a local `/bin/sh`. It proves the emitted commands and
  the bytes they produce, and it cannot prove anything about a transport,
  because there isn't one.
* `tests/busybox/test_shell_codec_contracts.py` runs the CODEC — `base64 -d`,
  chunk-append, `dd` range reads — inside a real BusyBox root. Also no
  transport: Tier 2 is a local `chroot`, and the "wire" is a pipe this process
  owns.

That left the one number depending on a transport unmeasured, and
``_SHELL_CHUNK_BYTES``'s own docstring said so — its wire-level headroom was
"UNMEASURED and plausibly negative". This module is where that stops being
true FOR AN EXEC CHANNEL, which is the only channel it measures: the pty
path, where BusyBox ash's line editor truncates at 1023 characters, is a
different limit recorded separately as the ``run-command-line-length`` gap,
and that constant's docstring now carries both. Everything here goes through
the Tier 3 dropbear — a real ssh daemon,
real exec channels, a real chroot into the BusyBox root on the far side — and
through the PRODUCT's own entry points, `UnixHost.put` and `UnixHost.get`,
against a host built by :func:`~otto.host.factory.create_host_from_dict` from
``os_type: "busybox"``. Nothing constructs a `ShellFileTransfer` by hand.

THE TIER IS WHAT MAKES A GREEN RESULT MEAN SOMETHING. `test_tier3_session.py`
proves sftp and scp are BOTH unreachable inside this root, so a transfer that
succeeds here cannot have quietly fallen back to either: the `shell` backend
is not merely the configured one, it is the only one that could have moved a
byte. The three assertions each test makes are then about content, placement
and line length rather than about which code path ran.

WHERE THE BYTES ARE CHECKED, AND WHY IT IS NOT OVER SSH. The device's
filesystem is a directory on this machine (`daemon.rootfs`), so a PUT is
verified by reading `<rootfs>/tmp/<name>` directly — no ssh, no shell, no
base64. That is strictly stronger than asking the device for an `md5sum` over
a second channel: it proves the bytes AND the location, and it shares no
mechanism at all with the transfer under test, so the two cannot fail
together. The location half is a real guard, not a bonus — a session that
never entered the chroot would land the file in the dev VM's own `/tmp`, and
each test asserts that path stays empty.

COST, AND THE WINDOW IT IS SPENT FROM. Measured warm on this VM, 2026-08-13:
the hostile-payload round trip is ~0.1s, the three-chunk PUT ~0.15s, the
three-chunk GET ~0.15s. All of it is CALL-phase work against an
already-running session daemon (pyproject's `timeout = 180`,
`timeout_func_only = true`), and none of it starts a daemon of its own.

The bounds these tests spend under are the PRODUCT's, which is worth naming
because it is the one place this module's diagnostics are weaker than the rest
of the tier's: each exec is bounded by `DEFAULT_COMMAND_TIMEOUT` (30s) and the
first call also pays `Userland`'s `_RESOLVE_BUDGET_S` (30s), so a
worst-case-wedged three-chunk transfer would exceed the 180s per-test SIGALRM
and arrive as a bare `Timeout >180.0s`. Nothing here can shorten those — `put`
and `get` take no timeout — so the honest statement is that a WEDGED device
(as opposed to a dead or refusing one, which fails in milliseconds) is the one
failure this module reports less well than the harness reports its own. See
the coupled-budget block at the top of `busybox_rootfs.py`, amended for it.

NOTHING HERE IS TIMING-BASED. No assertion reads a clock, and none may — a
transfer test is a tempting place for "it should take under N seconds", which
is a discriminator built from a measurement of this machine on this day. See
`docs/architecture/quality-gates.md`, and issue #229 for the most recent time
that was got wrong.
"""

import contextlib
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import asyncssh
import pytest

from otto.host.factory import create_host_from_dict
from otto.host.transfer.shell import _SHELL_CHUNK_BYTES
from otto.host.unix_host import UnixHost
from otto.result import Result
from otto.utils import Status
from tests._fixtures import busybox_dropbear as bbd
from tests._fixtures.busybox_dropbear import run_over_ssh_async
from tests.busybox.conftest import TIER3_GROUP

# The payload and its digest come from the Tier 2 codec module rather than
# being restated here. They are the same 13 bytes for the same reason — a NUL,
# an LF, a CR, a 0xFF, a single quote and a backslash — and a second copy would
# be a second thing to get subtly wrong (the digest there is COMPUTED from the
# bytes there, so the pair cannot drift internally either). What this module
# adds is not a new payload, it is a transport: the same bytes, over ssh,
# through the product. Precedent for the cross-module import in this tree:
# `test_shell_codec_contracts.py` imports `_EXPECTED_BASE64` from
# `test_applet_resolution.py`.
from tests.busybox.test_shell_codec_contracts import _HOSTILE_PAYLOAD, _HOSTILE_PAYLOAD_MD5

# `busybox` is LOAD-BEARING despite this tree's directory stamp (the stamp
# depends on collection-hook order and dies with its own file — see the
# conftest), and `xdist_group` is the ANSWER to dropbear's measured
# MAX_UNAUTH_PER_IP=5: it keeps the whole tier sequential on one worker sharing
# one daemon. Declared rather than stamped because only a declaration is
# independent of the invocation shape; `_unhonored_tier3_group` fails any Tier
# 3 test where xdist did not act on it.
pytestmark = [pytest.mark.busybox, pytest.mark.xdist_group(TIER3_GROUP)]

_DEVICE_TMP = Path("/tmp")
"""The destination directory INSIDE the BusyBox root, as the device names it.

`/tmp` is the only writable directory the Tier 2 root has (see
`_ROOTFS_DIRS`), and it exists there precisely because this backend stages a
temp beside its destination — the two facts are the same fact. The same string
also names a real directory on the dev VM, which is what makes the
"nothing landed outside the root" assertion in each test possible AND
necessary.
"""

_SPEC_PAYLOAD_MD5 = "a4119bcf623a896e535fc44c74e94d1d"
"""The digest the spec and the plan name for the payload Tier 3 must round-trip.

The ONLY literal digest in this tier, and it is a drift guard rather than a
measurement — see
:func:`test_the_payload_this_tier_moves_is_the_one_the_spec_names`. Every other
comparison here is bytes against bytes.
"""

_MEASURED_EXEC_LINE_LIMIT = 9000
"""Longest exec-channel command line dropbear carries INTACT. Measured, twice.

The phase-5 spike measured 9000 characters through and 9001 broken; that was
re-measured in this worktree against this exact tier on 2026-08-13, one
character at a time around the boundary: 8999 and 9000 both answer normally,
and 9001, 9100 and 11008 all raise `asyncssh.misc.ConnectionLost` with the
whole connection gone.

THE FAILURE MODE IS WHY THIS CONSTANT IS WRITTEN DOWN AT ALL. There is no
server log line, no channel-level error and no partial output — the connection
simply drops. A transfer that crossed this limit would therefore not look like
a size problem; it would look like a flaky network, on a device where "the ssh
link is unreliable" is a completely plausible thing to believe. The guard below
exists so that the day something makes otto's chunk line longer, the failure
says `_SHELL_CHUNK_BYTES` instead.

Not a bound on anything this module WAITS for, and not a clock reading: it is a
measured property of the transport, and the assertion compares one character
count against another.
"""


@contextlib.asynccontextmanager
async def busybox_host(
    daemon: bbd.LoopbackDropbear, userland_options: "dict[str, str] | None" = None
) -> "AsyncIterator[UnixHost]":
    """Yield a real `UnixHost` pointed at *daemon*, built the way a lab builds one.

    THROUGH :func:`~otto.host.factory.create_host_from_dict`, from a host dict
    whose only unusual key is `os_type: "busybox"` — the same path
    `lab.json` takes. That is the difference between proving "the `shell`
    backend works over ssh" and proving "a BusyBox host declared the ordinary
    way ENDS UP with the `shell` backend and it works over ssh". The profile is
    what supplies `transfer="shell"`, `has_bash=False` and
    `command_frame="ash"`; nothing here pins any of them, so a profile that
    stopped defaulting the transfer would red at the precondition below rather
    than pass with scp.

    The credential's password is never used — dropbear runs with `-s` (no
    password auth) and the tier authenticates with the client key in
    `ssh_options` — but a unix host spec requires at least one cred, so it is
    spelled to say so.

    Closes the host on the way out, in a `finally`. otto holds an asyncssh
    connection and its transport open behind `_connections`; a test that left
    one open would be reported by the suite's own leak detectors as an
    unraisable at some later test's teardown, naming neither this module nor
    the connection.

    The precondition is mutation-verified by changing the profile's `transfer`
    default to `"scp"`: all three transfer tests then error here with `the
    `busybox` os_profile built a host whose transfer is 'scp'` instead of
    quietly measuring a different backend.

    *userland_options* is passed through to the host dict, and it is the only
    way this tier can reach the `shell` backend's uu codec at all. See
    :func:`test_the_uu_codec_moves_a_payload_over_real_ssh` for why that is
    needed and what it costs.
    """
    host = create_host_from_dict(
        {
            "os_type": "busybox",
            "ip": "127.0.0.1",
            "element": "bbtier3",
            "is_virtual": True,
            "creds": [{"login": daemon.username, "password": "unused-pubkey-auth"}],
            "term": "ssh",
            "ssh_options": {"port": daemon.port, "client_keys": [str(daemon.keys.client)]},
            **({"userland_options": userland_options} if userland_options else {}),
        }
    )
    assert host.transfer == "shell", (
        f"the `busybox` os_profile built a host whose transfer is {host.transfer!r}, so this "
        f"module would measure that backend instead of the one exit criterion 3 is about. "
        f"The profile's `transfer` default is what puts `shell` here — see "
        f"`_register_builtin_os_profiles`"
    )
    try:
        yield host
    finally:
        await host.close()


def device_path(daemon: bbd.LoopbackDropbear, name: str) -> Path:
    """Where `<_DEVICE_TMP>/<name>` really is, on the machine running the tests.

    The device's filesystem is `daemon.rootfs`, so this is the oracle every
    PUT here is checked against — a read that involves no ssh, no shell and no
    base64, and therefore cannot fail in the same way the transfer under test
    fails.
    """
    return daemon.rootfs / _DEVICE_TMP.relative_to("/") / name


def unique_name(stem: str) -> str:
    """A basename nothing else can own, so a stale file cannot fake a pass.

    The session root and the dev VM's real `/tmp` both outlive one test — the
    root across the whole Tier 3 session, `/tmp` across everything — and both
    are asserted against here (one must hold the file, the other must not).
    A fixed name would let a leftover from an earlier run satisfy either.
    """
    return f"{stem}-{uuid.uuid4().hex[:8]}.bin"


def assert_landed_in_the_root(daemon: bbd.LoopbackDropbear, name: str, payload: bytes) -> None:
    """A PUT put *payload* at `<root>/tmp/<name>` and put NOTHING on the dev VM.

    THREE STATEMENTS, IN THIS ORDER, BECAUSE THE FIRST ONE EXPLAINS THE OTHER
    TWO. `_DEVICE_TMP` is `/tmp`, which names a directory on this machine as
    well as one inside the root, and the two are one `chroot` apart. A session
    that never entered the chroot — the exact condition `test_tier3_session.py`
    injects by dropping the `-c` wrapper — writes the dev VM's own `/tmp/<name>`
    and reports success, and a test that only compared bytes would then either
    blow up on a missing file or, worse, compare a file the transfer never
    touched. So the stray is checked FIRST: it is the assertion that names the
    real fault, and it is the one that fires under that mutation.

    Existence is checked before content so a file that landed in some third
    place arrives as a sentence rather than as a bare `FileNotFoundError` from
    inside a test.

    :func:`unique_name` is what makes any of this checkable — neither path can
    be occupied by anything but this transfer.

    Mutation-verified, one assertion each. Dropping `-c <wrapper>` from the
    harness argv reds the STRAY check (`the transfer wrote /tmp/hostile-….bin
    on the DEV VM`) — the injected hostile condition this exists for. Making
    `_run_put` report success without emitting a command reds the EXISTENCE
    check (`the PUT reported success and nothing is at …`). Reversing each
    chunk symmetrically reds the CONTENT check.
    """
    stray = _DEVICE_TMP / name
    assert not stray.exists(), (
        f"the transfer wrote {stray} on the DEV VM. `/tmp` inside the BusyBox root and `/tmp` "
        f"on this machine are different directories one chroot apart, so this file landing "
        f"here means the session never entered the root — and every content assertion in "
        f"this module would then be measuring the wrong filesystem"
    )
    landed = device_path(daemon, name)
    assert landed.exists(), (
        f"the PUT reported success and nothing is at {landed}. Nothing is at {stray} either, "
        f"so the bytes went to a third place — read the emitted `mv` destination"
    )
    assert landed.read_bytes() == payload, (
        f"{landed} holds {len(landed.read_bytes())} bytes after a {len(payload)}-byte PUT and "
        f"they are not the bytes that were sent. For a multi-chunk transfer a transposed or "
        f"short chunk looks exactly like this; it starts {landed.read_bytes()[:24]!r} and "
        f"should start {payload[:24]!r}"
    )


def multi_chunk_payload() -> bytes:
    """Two full chunks plus a remainder, with the hostile bytes across a boundary.

    ``_SHELL_CHUNK_BYTES * 2 + 123`` so the transfer emits two FULL-length
    command lines (the thing the ceiling guard is about) and one short one (the
    final-chunk arithmetic, which an exact multiple would never exercise). The
    123 is not a multiple of 3 either, so the last chunk's base64 carries
    padding.

    The body is a ``(i * 7) % 256`` ramp — every byte value appears, and it is
    ORDER-SENSITIVE, so a transfer that reassembled two chunks the wrong way
    round produces the right length and the wrong bytes. Onto that,
    :data:`_HOSTILE_PAYLOAD` is spliced so it STRADDLES the first chunk
    boundary: six bytes before it, seven after. Chunking is byte-aligned and
    each chunk is base64-encoded on its own, so those thirteen bytes are split
    across two independent command lines and two independent `base64 -d`
    invocations on the device — a case a single-chunk payload structurally
    cannot produce.
    """
    body = bytearray((i * 7) % 256 for i in range(_SHELL_CHUNK_BYTES * 2 + 123))
    straddle = _SHELL_CHUNK_BYTES - 6
    body[straddle : straddle + len(_HOSTILE_PAYLOAD)] = _HOSTILE_PAYLOAD
    return bytes(body)


def test_the_payload_this_tier_moves_is_the_one_the_spec_names():
    """The imported payload still digests to the value exit criterion 3 names.

    Every other assertion in this module compares bytes to bytes, which is
    self-consistent by construction and says nothing about WHICH bytes. The
    payload is imported from `test_shell_codec_contracts.py` — deliberately, so
    Tier 2 and Tier 3 measure the same thing — and that import is also the way
    this module could silently stop measuring the hostile case at all: edit
    those 13 bytes to something friendlier and every test here goes on passing
    while the criterion the spec worded ("verified in Tier 3 over real ssh",
    against THESE bytes) is no longer met by anything.

    So the literal digest is pinned once, here, and nowhere else. The imported
    `_HOSTILE_PAYLOAD_MD5` is COMPUTED from `_HOSTILE_PAYLOAD` at its own site,
    so comparing it to the literal checks the payload, not a second transcription
    of the hash.

    Pure — no daemon, no root, no interpreter. It still speaks on a machine
    where nothing else in this module can run.
    """
    assert _HOSTILE_PAYLOAD_MD5 == _SPEC_PAYLOAD_MD5, (
        f"the payload imported from `test_shell_codec_contracts.py` digests to "
        f"{_HOSTILE_PAYLOAD_MD5!r}, not the {_SPEC_PAYLOAD_MD5!r} the busybox design spec and "
        f"the phase-5 plan name for exit criterion 3. Either those bytes changed — in which "
        f"case this tier is no longer round-tripping the NUL/LF/CR/0xFF/quote/backslash case "
        f"the criterion is about — or the spec moved and this constant has to move with it. "
        f"Payload: {_HOSTILE_PAYLOAD!r}"
    )


@pytest.mark.asyncio
async def test_the_binary_hostile_payload_round_trips_over_real_ssh(tier3_dropbear, tmp_path):
    """The spec's criterion, end to end: PUT it, GET it back, byte for byte.

    THIRTEEN BYTES CHOSEN TO BREAK EVERY LAYER THIS BACKEND CROSSES, and they
    are the Tier 2 codec module's bytes rather than new ones: a NUL (truncates
    a C-string read), an LF and a CR (break anything line-oriented — and the
    transport under test is a line-oriented protocol carrying commands), a 0xFF
    (not valid UTF-8, and asyncssh decodes channel output as text), a single
    quote and a backslash (the shell metacharacters that matter to a backend
    which moves bytes THROUGH a shell). Tier 2 proved BusyBox's `base64 -d`
    does not corrupt them. What could not be measured there is the ssh channel
    in between, which is all this test adds.

    TWO CHECKS, IN THE ORDER THEY DISCRIMINATE. First the bytes on the
    device's own filesystem, read directly — that is the PUT, proved without
    ssh (see :func:`device_path`), and it is where a symmetric corruption would
    be caught: a PUT that mangled the payload and a GET that unmangled it would
    round-trip perfectly and fail here. Then the GET, compared byte for byte
    against the original — `==` on bytes, never a decoded string, because a
    payload with an embedded NUL cannot be compared as text without risking the
    truncation it was chosen to expose. No digest is compared anywhere in
    between: these are 13 bytes, `==` on them says everything an md5 would and
    says it about the bytes rather than about a hash of them.

    Mutation-verified with a SYMMETRIC corruption in each direction, since an
    asymmetric one is caught by the backend's own `md5sum` check and never
    reaches an assertion here. Reversing each chunk in `_put_one` (the local
    digest follows, so integrity passes) reds with `... holds 13 bytes after a
    13-byte PUT and they are not the bytes that were sent`; reversing only what
    `_get_one` WRITES (leaving its digest honest) reds with `the GET landed
    b"G\\\\F'E\\xffD\\rC\\nB\\x00A", which is not the payload that was sent`.
    """
    name = unique_name("hostile")
    src = tmp_path / name
    src.write_bytes(_HOSTILE_PAYLOAD)
    landing = tmp_path / "landing"
    landing.mkdir()

    async with busybox_host(tier3_dropbear) as host:
        put = await host.put(src, _DEVICE_TMP, show_progress=False)
        assert put.is_ok, f"PUT failed: {put.msg} {put.value}"
        assert_landed_in_the_root(tier3_dropbear, name, _HOSTILE_PAYLOAD)

        got = await host.get(_DEVICE_TMP / name, landing, show_progress=False)
        assert got.is_ok, f"GET failed: {got.msg} {got.value}"

    back = landing / name
    assert back.read_bytes() == _HOSTILE_PAYLOAD, (
        f"the GET landed {back.read_bytes()!r}, which is not the payload that was sent "
        f"({_HOSTILE_PAYLOAD!r}) — a round trip that returns different bytes than it started "
        f"with is exactly what this payload's NUL, CR and 0xFF were chosen to expose"
    )


@pytest.mark.asyncio
async def test_a_full_chunks_command_line_crosses_the_channel_inside_the_measured_ceiling(
    tier3_dropbear, tmp_path
):
    """A FULL chunk, on the wire, and the headroom pinned as a RELATIONSHIP.

    The hostile payload above is 13 bytes: one chunk, a 20-character base64
    blob, a command line nothing could object to. The number this backend
    actually risks its transport on is the FULL one — 4096 plaintext bytes,
    5464 base64 characters, a command line of over five thousand — and no test
    anywhere had ever put one on a wire. This one sends three chunk commands,
    two of them full length, and measures what it sent.

    THE GUARD IS THE RELATIONSHIP, NOT EITHER NUMBER. `_SHELL_CHUNK_BYTES` is a
    revisable choice and `TestShellChunkLineLength` already pins its emitted
    consequence, for ITS destination path, at 5524 characters; re-pinning
    either here would be a change-detector with two copies. What has no owner
    anywhere else is the inequality: the longest line otto emits must stay under
    what this transport carries (:data:`_MEASURED_EXEC_LINE_LIMIT`). Both numbers
    are named in the message because a reader who reds this needs to know which
    of the two moved.

    THE UNIT PIN IS NOT THIS TIER'S NUMBER, and cannot be. Every chunk command
    carries the staged temp's full path, so its length moves with the
    destination: the unit guard measures `/dest/payload.bin` (17 characters),
    this tier writes `/tmp/fullchunk-<8 hex>.bin` inside the root (27), so its
    lines run ten longer for that reason alone. Measured here on 2026-08-13,
    after `_STAGING_TOKEN_HEX` cut the staged temp's token from 32 hex
    characters to 8: the emitted lines are **5534, 5535 and 235**. Chunk one
    creates the temp with `>` and chunk two appends with `>>`, one character
    more; chunk three carries the payload's 123-byte tail rather than a full
    4096, which is the whole of why it is so much shorter. (The same three were
    5558, 5559 and 259 before that token was shortened — 24 characters of pure
    headroom, recovered.) So no full-line number anywhere else in the tree is
    this test's, and the observed maximum is what it asserts.

    THE LENGTH IS CHECKED BEFORE THE TRANSFER'S OWN OUTCOME, and that ordering
    is the entire point of the guard rather than a stylistic preference. When
    the line does get too long the connection is dropped with no server log
    (see :data:`_MEASURED_EXEC_LINE_LIMIT`), so `put` raises `ConnectionLost`
    and a test that asserted success first would report the transport, which is
    the diagnosis that costs an afternoon. Mutation-verified with
    `_SHELL_CHUNK_BYTES = 8192`, re-run on 2026-08-13 against the shortened
    staging token: one 10994-character line is emitted, the connection dies,
    and this ordering still reds with `otto emitted a 10994-character chunk
    command line, and this transport carries 9000 intact` (10994 rather than
    the 11018 recorded when the guard was written, for the same 24 characters
    the token gave back). The chunk-count check below is verified separately,
    by halving the read size (`a 8315-byte payload at 4096 bytes per chunk is
    3 chunk commands, and 5 were emitted`).

    The payload is a byte ramp with :data:`_HOSTILE_PAYLOAD` spliced across the
    FIRST CHUNK BOUNDARY. Chunking is byte-aligned and base64-encodes each
    chunk independently, so a NUL/CR/quote sequence straddling a boundary is
    split across two separate command lines and reassembled on the device by
    two separate `base64 -d >>` appends — the one place those bytes can go
    wrong that a single-chunk transfer cannot reach.
    """
    name = unique_name("fullchunk")
    payload = multi_chunk_payload()
    src = tmp_path / name
    src.write_bytes(payload)

    async with busybox_host(tier3_dropbear) as host:
        emitted: list[str] = []
        real_exec = host._file_transfer._exec_cmd

        async def recording_exec(cmd, *args, **kwargs):
            """Record what the backend hands the exec channel, then hand it over.

            This is the exact string `SSHClientConnection.create_process` is
            called with (`session.py`'s ssh arm passes it through untouched),
            so it is the line the measurement in
            :data:`_MEASURED_EXEC_LINE_LIMIT` was made against — not a
            reconstruction of it.
            """
            emitted.append(cmd)
            return await real_exec(cmd, *args, **kwargs)

        host._file_transfer._exec_cmd = recording_exec

        # Caught, not propagated: over-long lines kill the CONNECTION, and the
        # guard below has to speak before that does. `asyncssh.Error` and
        # `OSError` cover both shapes the transport fails in; anything else is
        # a bug in the backend and still propagates.
        put_error: "Exception | None" = None
        put = Result(Status.Error, msg="the PUT never returned a result")
        try:
            put = await host.put(src, _DEVICE_TMP, show_progress=False)
        except (asyncssh.Error, OSError) as e:
            put_error = e

        chunk_lines = [cmd for cmd in emitted if cmd.startswith("printf ")]
        assert chunk_lines, (
            f"the transfer emitted no chunk command at all, so the line-length guard below "
            f"measured nothing. Commands seen: {[cmd[:60] for cmd in emitted]}"
        )
        longest = max(len(cmd) for cmd in chunk_lines)
        assert longest <= _MEASURED_EXEC_LINE_LIMIT, (
            f"otto emitted a {longest}-character chunk command line, and this transport "
            f"carries {_MEASURED_EXEC_LINE_LIMIT} intact — 9001 is the first length measured "
            f"broken, and it breaks by dropping the whole connection with NO server log line, "
            f"so a regression past this point reads as a flaky link rather than as a size "
            f"problem. `_SHELL_CHUNK_BYTES` is {_SHELL_CHUNK_BYTES} bytes of plaintext here; "
            f"the emitted line is that base64-expanded plus the `printf ... | base64 -d >> "
            f"<temp>` framing and the destination path, so a longer path spends the same "
            f"headroom. Re-measure the ceiling against the real transport before raising "
            f"either"
        )
        expected_chunks = -(-len(payload) // _SHELL_CHUNK_BYTES)
        assert len(chunk_lines) == expected_chunks, (
            f"a {len(payload)}-byte payload at {_SHELL_CHUNK_BYTES} bytes per chunk is "
            f"{expected_chunks} chunk commands, and {len(chunk_lines)} were emitted — the "
            f"guard above measured a different transfer than the one it was written for"
        )

        if put_error is not None:
            raise AssertionError(
                f"the chunk lines were inside the measured ceiling, but the PUT still failed "
                f"on the transport: {type(put_error).__name__}: {put_error}"
            ) from put_error
        assert put.is_ok, f"PUT failed: {put.msg} {put.value}"
        assert_landed_in_the_root(tier3_dropbear, name, payload)


@pytest.mark.asyncio
async def test_a_multi_chunk_get_reassembles_the_devices_wrapped_base64(tier3_dropbear, tmp_path):
    """GET's chunk loop, over the channel, against output the device really wraps.

    THE SOURCE IS STAGED WITHOUT THE TRANSPORT UNDER TEST. It is written
    straight into `daemon.rootfs` on this machine, so nothing otto's PUT does
    can influence what GET is asked to read — a corruption that PUT and GET
    shared would survive a round trip and die here.

    THE PRECONDITION IS THE POINT OF THE TEST. `_get_one` flattens ALL
    whitespace out of each chunk's reply before decoding it with
    `validate=True`, and that step is load-bearing only if the reply is
    genuinely multi-line: BusyBox's `base64` wraps at 76 columns, so one
    4096-byte chunk comes back as 72 lines. That was measured in Tier 2, where
    there is no channel; here it is asserted through a SEPARATE ssh connection
    (`run_over_ssh_async`, not otto's exec) before the GET runs, because if the reply
    ever arrived as one line this test would pass with the flatten step never
    exercised — green, and blind to the thing it exists to watch.

    What the channel adds on top of Tier 2's measurement is real: the wrapped
    text crosses an exec channel, is split by asyncssh into lines and rejoined
    by `session.exec` with `\\n`, and only then reaches the decoder. Nothing
    before this measured that path.

    Byte-for-byte on the result, never a length or a digest of a decoded
    string: the payload's hostile bytes straddle a chunk boundary and the whole
    question is whether they survive being reassembled from three separate
    replies.

    Mutation-verified, both halves, and NEITHER mutation touches the other two
    tests in this module — which is the argument for this being its own test.
    `_SHELL_CHUNK_BYTES = 48` makes one chunk encode to a single line and reds
    the precondition (`the device encoded one 48-byte chunk to a SINGLE line`)
    while the round trip and the ceiling guard stay green; reversing what
    `_get_one` writes reds the comparison (`GET reassembled 8315 bytes that are
    not the 8315 on the device`) while the PUT-only test stays green.
    """
    name = unique_name("getchunks")
    payload = multi_chunk_payload()
    source = device_path(tier3_dropbear, name)
    source.write_bytes(payload)
    landing = tmp_path / "landing"
    landing.mkdir()

    wrapped = await run_over_ssh_async(
        tier3_dropbear,
        f"dd if={_DEVICE_TMP / name} bs={_SHELL_CHUNK_BYTES} skip=0 count=1 2>/dev/null "
        f"| base64 | wc -l",
    )
    assert wrapped.exit_status == 0, wrapped.stderr
    assert int(wrapped.stdout.strip()) > 1, (
        f"the device encoded one {_SHELL_CHUNK_BYTES}-byte chunk to a SINGLE line "
        f"({wrapped.stdout!r}). GET's whitespace-flatten step exists for wrapped output, and "
        f"with unwrapped output this test would exercise the decode path that needs no "
        f"flattening at all and report success for it"
    )

    async with busybox_host(tier3_dropbear) as host:
        got = await host.get(_DEVICE_TMP / name, landing, show_progress=False)
        assert got.is_ok, f"GET failed: {got.msg} {got.value}"

    back = landing / name
    assert back.read_bytes() == payload, (
        f"GET reassembled {len(back.read_bytes())} bytes that are not the "
        f"{len(payload)} on the device. Chunks are decoded and appended in order, so a "
        f"dropped line from a wrapped reply, a transposed chunk or a short `dd` read all "
        f"land here"
    )


@pytest.mark.asyncio
async def test_the_uu_codec_moves_a_payload_over_real_ssh(tier3_dropbear, tmp_path):
    """The `shell` backend's OTHER codec, over the same real channel, both directions.

    THE COVERAGE PROBLEM THIS SOLVES, stated plainly because the obvious test
    plan does not solve it. The uu codec exists for BusyBox 1.16.1, the one
    matrix row with no `base64` applet, and `TIER3_RELEASE` runs 1.35.0 --
    one row, on cost grounds. So a uu test that waited for its own device
    would never run here, and uu would ship with chroot-only coverage: exactly
    the gap phase 4 left for base64 and phase 5 closed.

    WHAT MAKES THE WAY OUT LEGITIMATE is that the codec is chosen from a
    DECLARABLE fact. `userland_options` sets `base64_flag` to `absent`, which
    `Userland` treats as settled -- a declaration is as authoritative as a
    measurement, by design -- and `ShellFileTransfer._select_codec` then picks
    uu exactly as it would on a device that answered the same way. Nothing is
    monkeypatched and no test-only switch exists; the selector runs its real
    branch on its real input.

    BE HONEST ABOUT THE ONE THING THIS DECLARATION IS: a lie about THIS
    device. 1.35.0 has `base64`, and this host says it does not. What that
    costs is precision about which row was proved -- the transport, the
    channel, the daemon, the chroot and every command are real, and the
    applet the commands actually run is this row's own `uudecode`, which is
    present on all five. What it does not cost is the codec's per-row
    correctness, because that is
    `tests/busybox/test_shell_codec_contracts.py`'s job and it covers 1.16.1
    itself.

    THE LINE-LENGTH GUARD IS THE RELATIONSHIP AGAIN, not a number, matching
    the base64 test above -- but the quantity is different and the difference
    is measured rather than assumed. uu's chunk command is MULTI-LINE: 100
    lines, of which 95 are the frame at up to 61 characters each and the
    longest is the 186-character first line that carries the scratch path
    twice and the temp path once. The ceiling is on the WHOLE command string
    rather than on its longest line -- measured on this tier, a 400-line
    command of 18-character lines (8991 characters) crosses intact and 500
    such lines (9009) drops the connection, the same boundary
    :data:`_MEASURED_EXEC_LINE_LIMIT` records for a single line. So the guard
    compares the total, and the multi-line shape claims no extra room: for a
    26-character destination the whole command measures 5952 characters
    against base64's 5533.
    """
    name = unique_name("uuchunks")
    payload = multi_chunk_payload()
    src = tmp_path / name
    src.write_bytes(payload)
    landing = tmp_path / "landing"
    landing.mkdir()

    async with busybox_host(tier3_dropbear, {"base64_flag": "absent"}) as host:
        emitted: list[str] = []
        real_exec = host._file_transfer._exec_cmd

        async def recording_exec(cmd, *args, **kwargs):
            emitted.append(cmd)
            return await real_exec(cmd, *args, **kwargs)

        host._file_transfer._exec_cmd = recording_exec

        put_error: "Exception | None" = None
        put = Result(Status.Error, msg="the PUT never returned a result")
        try:
            put = await host.put(src, _DEVICE_TMP, show_progress=False)
        except (asyncssh.Error, OSError) as e:
            put_error = e

        chunk_cmds = [cmd for cmd in emitted if cmd.startswith("uudecode ")]
        assert chunk_cmds, (
            f"the transfer emitted no uu chunk command, so it did not take the codec this "
            f"test exists for -- the declaration that forces it is the first thing to "
            f"check. Commands seen: {[cmd[:40] for cmd in emitted]}"
        )
        assert not any(cmd.startswith("printf ") for cmd in emitted), (
            f"a base64 chunk command was emitted on a host that declared base64_flag="
            f"'absent': {[cmd[:40] for cmd in emitted if cmd.startswith('printf ')]}"
        )
        longest = max(len(cmd) for cmd in chunk_cmds)
        assert longest <= _MEASURED_EXEC_LINE_LIMIT, (
            f"otto emitted a {longest}-character uu chunk command, and this transport "
            f"carries {_MEASURED_EXEC_LINE_LIMIT} intact before it drops the whole "
            f"connection with no server log line. The command is a heredoc, so this is its "
            f"TOTAL length and not its longest line -- the ceiling was measured on the "
            f"total (8991 characters of 18-character lines through, 9009 broken), so a "
            f"multi-line shape buys no room here. `_SHELL_CHUNK_BYTES` is "
            f"{_SHELL_CHUNK_BYTES} bytes of plaintext; uu expands that to 95 framed lines "
            f"plus the scratch path TWICE and the temp path once, so a longer destination "
            f"spends this headroom about three times as fast as base64's does"
        )
        expected_chunks = -(-len(payload) // _SHELL_CHUNK_BYTES)
        assert len(chunk_cmds) == expected_chunks, (
            f"a {len(payload)}-byte payload at {_SHELL_CHUNK_BYTES} bytes per chunk is "
            f"{expected_chunks} chunk commands, and {len(chunk_cmds)} were emitted -- the "
            f"guard above measured a different transfer than the one it was written for"
        )

        if put_error is not None:
            raise AssertionError(
                f"the uu chunk commands were inside the measured ceiling, but the PUT "
                f"still failed on the transport: {type(put_error).__name__}: {put_error}"
            ) from put_error
        assert put.is_ok, f"PUT failed: {put.msg} {put.value}"
        assert_landed_in_the_root(tier3_dropbear, name, payload)

        got = await host.get(_DEVICE_TMP / name, landing, show_progress=False)
        assert got.is_ok, f"GET failed: {got.msg} {got.value}"

    device_tmp = device_path(tier3_dropbear, name).parent
    strays = sorted(p.name for p in device_tmp.iterdir() if p.name.startswith(f"{name}.otto-"))
    assert strays == [], (
        f"the uu transfer left {strays} in the device's /tmp. Both the staged temp and the "
        f"per-chunk scratch are named `<dest>.otto-<token>`, so anything still carrying "
        f"that prefix after a successful transfer was not cleaned up -- the scratch is "
        f"removed by the same command that creates it, and a real device would otherwise "
        f"accumulate one per file. Scoped to this transfer's own basename rather than to "
        f"the whole directory because the Tier 3 root is SESSION-scoped and every other "
        f"test in this module has already written into it"
    )
    back = landing / name
    assert back.read_bytes() == payload, (
        f"the uu GET reassembled {len(back.read_bytes())} bytes that are not the "
        f"{len(payload)} that were sent. Each chunk comes back as its own `begin`/`end` "
        f"frame and is unframed locally, so a dropped line, a transposed chunk or a short "
        f"`dd` read all land here"
    )
