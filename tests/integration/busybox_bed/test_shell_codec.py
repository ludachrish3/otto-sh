"""Shell-transfer codec contracts, as live put/get through each userland.

Migrated from the retired artifact-tier harness: its codec-contract module
(codec facts inside a chroot, no transport) and its dropbear-backed
shell-transfer module (a real transfer, but over an ssh daemon this repo
grafted onto that chroot). Both were deleted with the harness; git history
holds their names.

A REAL ROUND TRIP THROUGH THE VERSION'S OWN USERLAND SUBSUMES MOST OF WHAT
THEY MEASURED SEPARATELY. Byte equality after ``host.put`` followed by
``host.get`` is one assertion that stands on the codec commands being right,
the chunks arriving in order, and the reassembly being faithful -- on a
transport that is not ours, into a filesystem that is not ours. What it cannot
say is which spellings the device would have REFUSED, so the decode-spelling
matrix stays an explicit per-row assertion with its table lifted verbatim.

THE VERSION SPREAD COVERS BOTH CODECS, and that is asserted rather than
assumed: 1.16.1 has no ``base64`` applet, so its round trip rides uuencode by
construction, and
``test_the_codec_the_backend_selects_matches_what_the_matrix_records`` pins
that the backend really does switch on the row rather than on a habit.
"""

import hashlib
from pathlib import Path

import pytest

from otto.host.transfer.shell import _SHELL_CHUNK_BYTES, Base64Codec, UuencodeCodec, _uu_frame
from tests.integration.busybox_bed.test_applet_userland import _EXPECTED_BASE64

pytestmark = [pytest.mark.asyncio]

# Lifted verbatim from the retired harness's
# `tests/busybox/test_shell_codec_contracts.py::_EXPECTED_BASE64_FLAG`; this
# module is where that table lives now. `None` is not "unknown": it is "no
# base64 applet in this build at all", the gap the `shell` backend answers for
# 1.16.1 by switching codecs rather than by falling back to another spelling.
_EXPECTED_BASE64_FLAG = {
    "1.16.1": None,
    "1.21.1": "-d",
    "1.28.1": "-d",
    "1.31.0": "-d",
    "1.35.0": "-d",
}

# Lifted verbatim from that file. 13 bytes chosen to be hostile to every layer
# a naive transport might use: a NUL at offset 1 (truncates a C-string read),
# an embedded LF and CR (breaks anything line-oriented), a 0xFF byte (not
# valid UTF-8), a single quote and a backslash (shell metacharacters, which
# matter because this backend moves bytes THROUGH a shell).
_HOSTILE_PAYLOAD = b"A\x00B\nC\rD\xffE'F\\G"

# The digest the busybox design spec's exit criterion 3 names for those bytes.
# The ONLY literal digest in this directory, and a drift guard rather than a
# measurement: every other comparison here is bytes against bytes, which is
# self-consistent by construction and says nothing about WHICH bytes. Inherited
# from the retired harness, where it was the last thing standing between
# "simplify the 13 bytes" and five green rows measuring a friendlier payload.
_HOSTILE_PAYLOAD_SPEC_MD5 = "a4119bcf623a896e535fc44c74e94d1d"

# Sized off the backend's chunk CEILING so the payload spans several chunks on
# every route: the fitted size a pty-routed guest actually uses is smaller than
# the ceiling (the line budget), never larger, so more than three ceilings of
# bytes is more than three chunks whichever codec and whichever transport the
# row turns out to have.
#
# THE CONTENT IS ROTATED PER BLOCK rather than the same 256-byte ramp repeated.
# Every block covers all 256 byte values -- NUL, LF, CR and 0xFF land inside
# every chunk -- but no two blocks are equal, so a chunk delivered out of order
# changes the bytes. A plain repeat would not: the uuencode row's chunk is
# 4096 bytes, exactly sixteen periods of a 256-byte ramp, and transposing two
# of those chunks would produce a byte-identical file.
_MULTI_CHUNK_BLOCKS = (3 * _SHELL_CHUNK_BYTES) // 256 + 1
_MULTI_CHUNK_PAYLOAD = b"".join(
    bytes((value + block) % 256 for value in range(256)) for block in range(_MULTI_CHUNK_BLOCKS)
)

# Staging directory on the guest. Removed and recreated at the start of each
# transfer row, so a red row leaves a rerun clean on a guest whose entire
# filesystem lives in RAM.
_XFER_DIR = "/tmp/otto-xfer"


async def _staged(host) -> Path:
    """Return an empty transfer directory on *host*, creating it if needed."""
    res = await host.exec(f"rm -rf {_XFER_DIR} && mkdir -p {_XFER_DIR}")
    assert res.retcode == 0, f"could not stage {_XFER_DIR} on {host.element}: {res.value!r}"
    return Path(_XFER_DIR)


async def test_the_binary_hostile_payload_round_trips(guest, tmp_path: Path):
    """A NUL/LF/CR/0xFF/quote/backslash payload survives put then get, byte for byte.

    Compared as bytes rather than as text, because a payload with an embedded
    NUL cannot be read as a string on either side of the comparison without
    risking exactly the truncation it was chosen to expose.
    """
    host, version = guest
    src = tmp_path / "hostile.bin"
    src.write_bytes(_HOSTILE_PAYLOAD)
    back = tmp_path / "back"
    back.mkdir()

    dest = await _staged(host)
    put = await host.put(src, dest)
    assert put.status.is_ok, f"put failed on {host.element}: {put}"
    got = await host.get(dest / src.name, back)
    assert got.status.is_ok, f"get failed on {host.element}: {got}"

    assert (back / src.name).read_bytes() == _HOSTILE_PAYLOAD, (
        f"{host.element} (BusyBox {version}) corrupted the binary-hostile payload "
        f"on the way through its own codec"
    )
    await host.exec(f"rm -rf {_XFER_DIR}")


async def test_a_multi_chunk_payload_reassembles_in_order(guest, tmp_path: Path):
    """Several chunks, sent and fetched by the product, reassemble in order.

    This is the property PUT and GET both depend on and the one nobody would
    notice breaking: a loop that transposed two pieces still produces a file
    of the right length and the right byte SET, so only an order-sensitive
    comparison catches it -- which is what the rotated blocks in the payload
    are for.

    IT IS ALSO THIS TIER'S EVIDENCE ABOUT CHUNK ORDER FOR base64. The emitted
    per-chunk command is a decode into an appending redirect, so the DEVICE
    decodes each chunk on arrival and appends the plaintext; the concatenation
    of encoded chunks is never decoded as one stream. A green row here on four
    base64 guests is that order measured end to end. (The class docstring on
    the shell backend's codec base class still describes the other order --
    left standing deliberately by the task that found it, and now measured.)
    """
    assert len(_MULTI_CHUNK_PAYLOAD) > 3 * _SHELL_CHUNK_BYTES, (
        "the payload must exceed three chunk ceilings, or this row measures a "
        "single-chunk transfer and proves nothing about reassembly"
    )
    host, version = guest
    src = tmp_path / "chunky.bin"
    src.write_bytes(_MULTI_CHUNK_PAYLOAD)
    back = tmp_path / "back"
    back.mkdir()

    dest = await _staged(host)
    put = await host.put(src, dest)
    assert put.status.is_ok, f"put failed on {host.element}: {put}"
    got = await host.get(dest / src.name, back)
    assert got.status.is_ok, f"get failed on {host.element}: {got}"

    assert (back / src.name).read_bytes() == _MULTI_CHUNK_PAYLOAD, (
        f"{host.element} (BusyBox {version}) reassembled {len(_MULTI_CHUNK_PAYLOAD)} "
        f"bytes into something else -- a chunk is missing, truncated, or landed "
        f"out of order"
    )
    await host.exec(f"rm -rf {_XFER_DIR}")


async def test_appending_uu_frames_and_decoding_once_truncates_at_rc_zero(guest, tmp_path: Path):
    """The measured hazard ``UuencodeCodec``'s whole shape exists to avoid.

    A NEGATIVE MEASUREMENT, and the only one in this directory. ``base64`` is a
    stream codec while ``uuencode`` is a CONTAINER format, so the naive port of
    PUT's base64 order -- append every chunk's framed text, let the device
    decode the concatenation once at the end -- returns only the FIRST chunk.
    It does not fail while doing it: rc is 0 and one chunk's worth of bytes
    lands under a clean exit. The harness measured 4096 of 10253 bytes on all
    five rows; this measures the same shape against the same five userlands on
    real hardware-ish guests, with the payload this module already carries.

    NOTHING LIVE CAN REACH THIS THROUGH THE PRODUCT, which is why it is a
    hand-built file rather than a transfer: otto never emits that order, so the
    positive rows (which are green here) would go on passing if someone
    "simplified" the uu loop back into base64's shape -- on a device this suite
    never runs. The frames are built with the backend's OWN
    :func:`~otto.host.transfer.shell._uu_frame`, so what is decoded here is
    exactly the text the rewritten loop would have appended.

    ``FRAMES`` is the positive control and is not decoration: with a single
    frame in the file the length assertion below passes for the wrong reason,
    so the count of ``begin`` lines the device actually received is checked
    before the truncation is believed.
    """
    host, version = guest
    chunks = [
        _MULTI_CHUNK_PAYLOAD[i : i + _SHELL_CHUNK_BYTES]
        for i in range(0, len(_MULTI_CHUNK_PAYLOAD), _SHELL_CHUNK_BYTES)
    ]
    assert len(chunks) > 1, (
        "a single-frame payload cannot show a concatenation truncating -- the "
        "assertions below would pass on a device that decoded everything"
    )
    src = tmp_path / "all.uu"
    src.write_text("\n".join(_uu_frame(chunk) for chunk in chunks) + "\n")

    dest = await _staged(host)
    put = await host.put(src, dest)
    assert put.status.is_ok, f"put failed on {host.element}: {put}"

    res = await host.exec(
        f'uudecode -o {_XFER_DIR}/decoded.bin < {_XFER_DIR}/all.uu; echo "RC=$?"; '
        f'echo "LEN=$(wc -c < {_XFER_DIR}/decoded.bin)"; '
        f"echo \"FRAMES=$(grep -c '^begin ' {_XFER_DIR}/all.uu)\""
    )
    fields = dict(line.partition("=")[::2] for line in res.value.splitlines() if "=" in line)
    missing = [key for key in ("RC", "LEN", "FRAMES") if key not in fields]
    assert not missing, (
        f"the probe on {host.element} did not print {missing} -- a missing field "
        f"must fail here rather than be read as a negative result by whichever "
        f"assertion reads it next: {res.value!r}"
    )
    assert fields["FRAMES"].strip() == str(len(chunks)), (
        f"{host.element} (BusyBox {version}) received {fields['FRAMES'].strip()} uu frames, "
        f"not the {len(chunks)} this row appended -- the truncation below would then be "
        f"the file being short, not the decode stopping early ({res.value!r})"
    )
    assert fields["RC"].strip() == "0", (
        f"{host.element} (BusyBox {version}): appending {len(chunks)} uu frames and "
        f"decoding once exited {fields['RC']!r} rather than 0 ({res.value!r}). A LOUD "
        f"failure here would be good news and would change this codec's rationale -- the "
        f"reason PUT decodes per chunk is that this shape is SILENT"
    )
    assert fields["LEN"].strip() == str(len(chunks[0])), (
        f"{host.element} (BusyBox {version}): appending {len(chunks)} uu frames and "
        f"decoding once yielded {fields['LEN']!r} bytes, not the first chunk's "
        f"{len(chunks[0])} ({res.value!r}). The `shell` backend's uu path is shaped "
        f"around this returning exactly one chunk at rc=0; if that changed, re-read "
        f"`_uu_frame`'s docstring before relying on it"
    )
    await host.exec(f"rm -rf {_XFER_DIR}")


async def test_the_decode_spelling_matches_what_the_matrix_records(guest):
    """``base64 -d`` decodes; ``base64 --decode`` must not.

    The negative half is the point rather than a bonus. ``--decode`` fails
    LOUD on every row that has the applet, so this is not guarding against
    silent corruption the way the round trips above are: it guards against a
    regression that would only be noticed the day someone generalises the
    backend to GNU's long spelling and ships it to a device where every decode
    then fails outright.

    The 1.16.1 row proves absence STRUCTURALLY instead of skipping. A skipped
    row and a passing one are the same line in a summary, and the fact that
    row records -- no base64 applet at all -- is what selects its codec.
    """
    host, version = guest
    flag = _EXPECTED_BASE64_FLAG[version]

    if flag is None:
        res = await host.exec("command -v base64 >/dev/null 2>&1 && echo PRESENT || echo ABSENT")
        assert res.value.strip() == "ABSENT", (
            f"{host.element} (BusyBox {version}) was recorded as having no base64 "
            f"applet at all, but command -v found one ({res.value!r}) -- the "
            f"backend's declared codec gap for this row is stale"
        )
        return

    res = await host.exec(
        f'SHORT_OUT=$(echo aGk= | base64 {flag} 2>/dev/null); echo "SHORT_RC=$?"; '
        'echo "SHORT_OUT=$SHORT_OUT"; '
        'echo aGk= | base64 --decode >/dev/null 2>&1; echo "LONG_RC=$?"'
    )
    fields = dict(line.partition("=")[::2] for line in res.value.splitlines() if "=" in line)
    missing = [key for key in ("SHORT_RC", "SHORT_OUT", "LONG_RC") if key not in fields]
    assert not missing, (
        f"the probe on {host.element} did not print {missing} -- a missing field "
        f"must fail here rather than be read as a negative result by whichever "
        f"assertion reads it next: {res.value!r}"
    )
    assert fields["SHORT_RC"] == "0", (
        f"{host.element} (BusyBox {version}): the recorded decode spelling "
        f"`base64 {flag}` exited {fields['SHORT_RC']!r} -- the shell transfer "
        f"backend would treat every decode on this row as a hard failure"
    )
    assert fields["SHORT_OUT"] == "hi", (
        f"{host.element} (BusyBox {version}): `base64 {flag}` did not decode "
        f"`aGk=` to `hi` ({res.value!r}) -- the backend would corrupt every file "
        f"it decodes on this row"
    )
    assert fields["LONG_RC"] != "0", (
        f"{host.element} (BusyBox {version}) accepted GNU's `base64 --decode` "
        f"({res.value!r}) -- this is the row where generalising the backend to "
        f"the long spelling would silently keep working while every other row "
        f"broke loud"
    )


async def test_the_codec_the_backend_selects_matches_what_the_matrix_records(guest):
    """Which codec this row's transfer would use, asked of the backend itself.

    The two round trips above are green on all five guests and neither says
    WHICH codec carried them, so the claim that the version spread exercises
    both would rest on prose. This asks the one function that decides
    (base64 wherever the device has it, uuencode where it does not) and
    compares its answer against the lifted flag table: uuencode exactly on the
    row the table records as having no applet.

    It is also the successor to the harness's table-agreement guard on the
    other side of the seam. There the two tables were both in the test tree;
    here one of them is the guest's committed ``userland_options`` pin, and a
    pin that drifted from the recorded matrix would silently pick the other
    codec. Both directions are covered: the pin-less recon in
    ``test_applet_userland.py`` checks the pin against the device, this checks
    it against the oracle.
    """
    host, version = guest
    await host._userland().resolve()
    transfer = host._file_transfer
    expected = UuencodeCodec if _EXPECTED_BASE64_FLAG[version] is None else Base64Codec
    for direction, applet in (("put", "uudecode"), ("get", "uuencode")):
        codec = transfer._select_codec(direction, applet)
        assert isinstance(codec, expected), (
            f"{host.element} (BusyBox {version}) would {direction} with "
            f"{type(codec).__name__}, not {expected.__name__} -- the recorded matrix "
            f"says base64_flag={_EXPECTED_BASE64_FLAG[version]!r} for this row, so "
            f"either the guest's userland_options pin drifted or the selection rule did"
        )


async def test_the_hostile_payload_is_the_one_the_spec_names():
    """These 13 bytes still digest to the value exit criterion 3 names.

    Every other assertion about this payload compares bytes to bytes -- the
    round trip asserts what came back equals what went out -- which is
    self-consistent whatever the bytes are. Edit them to something friendlier
    and every row above goes on passing while the criterion the spec worded
    (NUL, LF, CR, 0xFF, quote, backslash, over a real device) is no longer met
    by anything.

    So the literal digest is pinned once, here, and nowhere else. Pure data, no
    device (async only because the module's marker is).
    """
    seen = hashlib.md5(_HOSTILE_PAYLOAD, usedforsecurity=False).hexdigest()
    assert seen == _HOSTILE_PAYLOAD_SPEC_MD5, (
        f"the hostile payload digests to {seen!r}, not the "
        f"{_HOSTILE_PAYLOAD_SPEC_MD5!r} the busybox design spec names for exit "
        f"criterion 3. Either these bytes changed -- in which case this directory is "
        f"no longer round-tripping the NUL/LF/CR/0xFF/quote/backslash case the "
        f"criterion is about -- or the spec moved and this constant moves with it. "
        f"Payload: {_HOSTILE_PAYLOAD!r}"
    )


async def test_the_flag_table_agrees_with_the_presence_table():
    """The decode-spelling table and the applet-presence table record one fact.

    Migrated verbatim in intent from the harness's
    ``test_the_flag_table_agrees_with_the_presence_table``: a version has a
    flag here if and only if ``test_applet_userland.py`` records the applet as
    present, and nothing else stops an edit to one file from leaving the two
    silently contradicting each other about the same version. Pure data, no
    device (async only because the module's marker is).
    """
    disagreed = {
        version: (flag, _EXPECTED_BASE64[version])
        for version, flag in _EXPECTED_BASE64_FLAG.items()
        if (flag is not None) != _EXPECTED_BASE64[version]
    }
    assert not disagreed, (
        f"_EXPECTED_BASE64_FLAG and test_applet_userland._EXPECTED_BASE64 disagree "
        f"about base64 presence for {disagreed} (flag, recorded presence) -- they "
        f"record the same fact and must agree"
    )
