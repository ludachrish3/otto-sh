"""Applet and userland-resolution contracts, migrated to the live guests.

Migrated from ``tests/busybox/test_applet_resolution.py`` (the rootfs harness,
deleted in Task 9). The three expectation tables below are that file's
hand-recorded oracle, LIFTED VERBATIM before the deletion. They are never
regenerated from the guests, and never read out of the lab entries'
``userland_options`` pins: those pins came from probing these same builds, so
a probe compared against them would be comparing a measurement with itself.

The harness proved absence STRUCTURALLY -- its root was populated by BusyBox's
own ``--install -s``, so a name that is not there was compiled out rather than
merely unshimmed -- and that property survives the move intact: these guests'
whole userland is one BusyBox binary plus the symlinks its installer made
(``scripts/build_busybox_guest_images.py``), with no GNU anything beside it
(pinned by ``test_guest_smoke.py``). What the move adds is that the shell
answering ``command -v`` is now the one otto is actually driving, over the
same telnet session every other row uses, rather than a chroot this repo
built for the occasion.

Every table is INDEXED by the row's version, never iterated. A guest whose
version has no entry fails with a ``KeyError`` naming it, instead of quietly
running with no expectation at all -- the same "loud direction" the harness
chose, for the same reason.
"""

import pytest

from otto.host.factory import create_host_from_dict
from otto.host.userland import APPLET_PRESENT, applet_capability
from tests._fixtures.labdata import host_data

pytestmark = [pytest.mark.asyncio]

# Lifted verbatim from tests/busybox/test_applet_resolution.py's
# `_EXPECTED_BASE64`. `base64` absent on 1.16.1 is the load-bearing entry: it
# is the version transition the `shell` transfer backend answers for by
# switching codecs (see test_shell_codec.py).
_EXPECTED_BASE64 = {
    "1.16.1": False,
    "1.21.1": True,
    "1.28.1": True,
    "1.31.0": True,
    "1.35.0": True,
}

# Lifted verbatim from that file's `_EXPECTED_STANDALONE_SHELL`. Whether this
# build's `sh` can find an applet with PATH pointing somewhere it cannot use
# is the CONFIG_FEATURE_SH_STANDALONE switch -- a property of the ARTIFACT,
# not of the version, and off in BusyBox's own defconfig. Ubuntu's system
# busybox measures the opposite, which is what makes an all-False table worth
# asserting per row rather than asserting once as "no guest has it".
_EXPECTED_STANDALONE_SHELL = {
    "1.16.1": False,
    "1.21.1": False,
    "1.28.1": False,
    "1.31.0": False,
    "1.35.0": False,
}

# Lifted verbatim from that file's `_EXPECTED_APPLETS` -- the names
# `otto.host.userland.PROBED_APPLETS` asks about, and what each of the five
# builds answered on 2026-08-14 inside the harness. Every row carries an entry
# for every name and the test indexes rather than iterates, so an applet added
# to PROBED_APPLETS without a measurement here fails with a KeyError naming it
# instead of never being measured.
_EXPECTED_APPLETS = {
    "1.16.1": {
        "base64": False,
        "nc": True,
        "poweroff": True,
        "scp": False,
        "shutdown": False,
        "uudecode": True,
        "uuencode": True,
    },
    "1.21.1": {
        "base64": True,
        "nc": True,
        "poweroff": True,
        "scp": False,
        "shutdown": False,
        "uudecode": True,
        "uuencode": True,
    },
    "1.28.1": {
        "base64": True,
        "nc": True,
        "poweroff": True,
        "scp": False,
        "shutdown": False,
        "uudecode": True,
        "uuencode": True,
    },
    "1.31.0": {
        "base64": True,
        "nc": True,
        "poweroff": True,
        "scp": False,
        "shutdown": False,
        "uudecode": True,
        "uuencode": True,
    },
    "1.35.0": {
        "base64": True,
        "nc": True,
        "poweroff": True,
        "scp": False,
        "shutdown": False,
        "uudecode": True,
        "uuencode": True,
    },
}

# Where `busybox --list` is staged. Written and removed inside one command, so
# a red row leaves nothing behind on a guest whose whole filesystem is RAM.
_APPLET_LIST_PATH = "/tmp/otto-applet-list"


async def test_base64_presence_matches_what_the_matrix_records(guest):
    """A version transition, asserted per row rather than as "some rows lack it".

    An aggregate assertion ("at least one guest has no base64") passes when
    the WRONG guest is the one missing it, which is precisely the confusion a
    transfer backend keyed on this would suffer.
    """
    host, version = guest
    res = await host.exec("command -v base64 >/dev/null 2>&1 && echo yes || echo no")
    assert res.retcode == 0, f"the probe did not run on {host.element}: {res.value!r}"
    seen = res.value.strip() == "yes"
    assert seen is _EXPECTED_BASE64[version], (
        f"{host.element} (BusyBox {version}): base64 "
        f"{'appeared' if seen else 'vanished'} relative to the recorded matrix "
        f"({_EXPECTED_BASE64[version]}). The table is the oracle -- if this guest's "
        f"build really changed, update it in the same commit that explains why"
    )


async def test_standalone_shell_matches_what_the_matrix_records(guest):
    """Whether ash needs PATH to find an applet, asserted per row.

    SANE is the positive control and it shares the one probe: with PATH set to
    /bin, ``ls`` must resolve whatever this build thinks of standalone shell.
    Without that half, a probe that always failed -- a typo in the applet name,
    a guest whose symlinks went missing -- would satisfy the negative
    assertion on every row that expects False.

    ``PATH=/nonexistent``, not ``PATH=``. An empty PATH is not "no search
    path": POSIX reads it as one empty component, meaning the CURRENT
    DIRECTORY, so a probe run from a directory that happens to hold the applet
    would report a build-config change that did not happen.

    THE WHOLE PROBE RUNS IN A SUBSHELL, which the harness had no need of and
    this tier does. ``host.exec`` on a telnet host runs on a POOLED shell
    session that is handed back and reused by the next call, so a bare
    ``PATH=/nonexistent`` here would follow this test out and break whatever
    ran next on that session. The parentheses keep the damage inside one
    command; the assertion is unchanged.
    """
    host, version = guest
    res = await host.exec(
        "(PATH=/bin; command -v ls && echo SANE; "
        "PATH=/nonexistent; command -v ls && echo RESOLVED || echo BLOCKED)"
    )
    assert "SANE" in res.value, (
        f"ls did not resolve on {host.element} even with PATH=/bin, so the "
        f"no-PATH result says nothing about standalone-shell support: {res.value!r}"
    )
    seen = "RESOLVED" in res.value
    assert seen is _EXPECTED_STANDALONE_SHELL[version], (
        f"{host.element} (BusyBox {version}): standalone-shell resolution "
        f"{'appeared' if seen else 'is absent'} relative to the recorded matrix "
        f"({_EXPECTED_STANDALONE_SHELL[version]}): {res.value!r}"
    )


async def test_applet_enumeration_is_unavailable_on_the_oldest_row(guest):
    """Why the userland probe detects per name instead of asking for a list.

    ``busybox --list`` is the obvious design and it is measured wrong: 1.16.1
    exits non-zero with ``--list: applet not found`` and enumerates nothing,
    while the other four answer. A probe built on it covers four fifths of the
    matrix and REPORTS SUCCESS.

    Asserted per row rather than as "1.16.1 is special", so the claim kept
    alive is "enumeration is not portable across this matrix". A future guest
    that loses ``--list`` reddens here and grows the recorded reason a second
    instance rather than letting it go stale.

    The list goes to a file rather than to the console: the four rows that
    answer enumerate several hundred names, and this row has no interest in
    pushing them through a telnet pty one line at a time.
    """
    host, version = guest
    res = await host.exec(
        f"busybox --list >{_APPLET_LIST_PATH} 2>&1; echo rc=$?; "
        f"grep -c '^base64$' {_APPLET_LIST_PATH}; "
        f"grep -c 'applet not found' {_APPLET_LIST_PATH}; "
        f"rm -f {_APPLET_LIST_PATH}"
    )
    lines = res.value.splitlines()
    assert len(lines) == 3, f"the probe on {host.element} answered {res.value!r}, not three lines"
    rc, base64_lines, refusal_lines = lines[0], int(lines[1]), int(lines[2])

    if version == "1.16.1":
        assert rc != "rc=0", (
            f"`busybox --list` now works on {host.element} (BusyBox {version}); the "
            f"per-name batch in the userland resolver was chosen because it did not, "
            f"so re-read that decision rather than only this table"
        )
        assert refusal_lines >= 1, (
            f"{host.element} refused `--list` differently -- no `applet not found` "
            f"in its output: {res.value!r}"
        )
    else:
        assert rc == "rc=0", (
            f"`busybox --list` failed on {host.element} (BusyBox {version}); the "
            f"recorded finding is that 1.16.1 alone lacks it: {res.value!r}"
        )
        assert base64_lines == 1, (
            f"`--list` on {host.element} enumerated nothing recognisable -- no "
            f"`base64` line in {base64_lines} matches: {res.value!r}"
        )


async def test_a_pinless_live_recon_matches_the_recorded_expectations(guest):
    """The batched applet probe, run by a PIN-LESS host against the real guest.

    This is the migrated batched-probe contract, and the reason it builds its
    own host rather than using the fixture's is the whole point: every lab
    entry declares ``userland_options``, and a declared capability is never
    probed. Stripping that key is what puts the resolver on the wire, so what
    the table is compared against is the DEVICE's answer and not the pin's.

    is_settled is asked before any value is read, and it is the control that
    makes a green result mean something. An applet whose batch never landed
    reads as PRESENT (the cannot-ask default), so a resolution round that
    silently failed would answer "present" for all seven names -- a perfectly
    shaped reply saying nothing at all.
    """
    host, version = guest
    ne = host.element
    data = {k: v for k, v in host_data(ne).items() if k != "userland_options"}
    pinless = create_host_from_dict(data, lab_name="busybox")
    try:
        userland = pinless._userland()
        await userland.resolve()
        expected = _EXPECTED_APPLETS[version]
        unsettled = [a for a in expected if not userland.is_settled(applet_capability(a))]
        assert not unsettled, (
            f"{ne} (BusyBox {version}) left {unsettled} unsettled, so the applet "
            f"batch never landed and every answer below is the cannot-ask default "
            f"rather than a measurement"
        )
        measured = {a: userland.has_applet(a) == APPLET_PRESENT for a in expected}
        assert measured == expected, (
            f"{ne} (BusyBox {version}): the pin-less recon drifted from the recorded "
            f"matrix. Measured {measured}, recorded {expected}"
        )
    finally:
        await pinless.close()


async def test_the_applet_table_agrees_with_the_base64_table():
    """The two tables in this file record the same fact and must not disagree.

    ``_EXPECTED_BASE64`` and ``_EXPECTED_APPLETS[...]["base64"]`` are the same
    underlying measurement reached two ways -- a single ``command -v`` and one
    name inside the batch -- and the harness kept them in one file with
    nothing enforcing their agreement. This is that guard, migrated from
    ``test_the_flag_table_agrees_with_the_presence_table``: pure data, no
    device, so it needs no guest fixture (async only because the module's
    marker is).
    """
    disagreed = {
        version: (present, _EXPECTED_APPLETS[version]["base64"])
        for version, present in _EXPECTED_BASE64.items()
        if present != _EXPECTED_APPLETS[version]["base64"]
    }
    assert not disagreed, (
        f"_EXPECTED_BASE64 and _EXPECTED_APPLETS record different base64 presence "
        f"for {disagreed} (single-probe value, batch value) -- they are the same "
        f"fact recorded twice and must agree"
    )
