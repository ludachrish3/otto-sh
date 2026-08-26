"""Tier 1: real BusyBox argument parsing, per version, without a container.

DRIVEN FROM /bin/sh (dash), NEVER from `busybox sh`. Applet resolution that
ignores PATH is a BUILD-CONFIG property (`CONFIG_FEATURE_SH_STANDALONE`), not
a universal fact about "BusyBox's ash" — measured off in every busybox.net
prebuilt this project fetches, see
`tests/integration/busybox_bed/test_applet_userland.py`'s
`_EXPECTED_STANDALONE_SHELL` table — but the system package apt installs for
the `system` row below HAS it on: during the 2026-08-10 timeout work a control
'verified' broken code because `busybox sh` resolved applets internally
regardless of PATH, silently exercising BusyBox's own builtin instead of the
shim under test. Anything asserting applet RESOLUTION belongs on the live
guests (`tests/integration/busybox_bed/test_applet_userland.py`), not here.

The command SPELLINGS measured here have two other copies, and all three have
to agree: `src/otto/host/userland.py` issues them at runtime, and
`tests/unit/host/test_userland.py` pins the exact list and order it issues.
They are deliberately not shared through an import — a product reading its
spellings from a test could not be caught drifting by that test — so each of
the three names the other two.

Not every probe `Userland` issues has a row here, only the ones with a real
argument-parsing question a BusyBox version could answer differently:
`timeout`, `base64`, `stat`, `wc`, `nc`. `Userland.elevation`'s `command -v`
presence checks, `Userland.shell_dialect`'s `$BASH_VERSION` variable read, and
`Userland.checksum`'s single-spelling `md5sum < /dev/null` probe have no such
question, so they carry no Tier 1 row and only the other two copies exist for
them -- `checksum`'s PRESENCE and OUTPUT format (not this exact probe
spelling) is substantiated instead by the live bed's real
`md5sum` round trips over the matrix.
"""

import functools
import re
import shutil
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from otto.utils import wait_for
from tests._fixtures.busybox import (
    BUSYBOX_MATRIX,
    busybox_binary,
    probe_banner,
)

# BusyBox switched `timeout` from `-t SECS PROG` to the coreutils-compatible
# `SECS PROG` in 1.30. Pinned here as the matrix's discriminating transition:
# a probe that only checks `command -v timeout` builds `timeout 3600 nc -l` on
# an older box, the applet fails to exec "3600", and the listener never starts.
_COREUTILS_STYLE_FROM = (1, 30)

# The `base64` applet is NOT universal: 1.16.1 answers `applet not found` and
# 1.21.1 decodes, so the matrix brackets the transition. Upstream added the
# applet in 1.19.0, but nothing between the two is pinned, so this constant is
# the matrix's measured boundary and not a claim about 1.19 or 1.20 — an
# artifact added inside the gap REDS here, which is the loud way to learn
# where the transition really is.
_BASE64_APPLET_FROM = (1, 21)


# The distro's own BusyBox — the sixth matrix row, and the only one that is a
# real DISTRO BUILD with distro config rather than an upstream defconfig
# prebuild. That is exactly the spec's point that behaviour is a function of
# version AND build config, so it is the row most able to contradict the other
# five. It is deliberately not a `busybox_binary()` artifact: there is nothing
# to fetch, nothing to pin (its bytes are whatever the distro shipped, and its
# updates are the distro's), and it needs no interpreter because it is built
# for this machine. Resolved through `which` rather than hardcoded, so the row
# exercises the binary a shell on this host would actually reach.
_SYSTEM_BUSYBOX = shutil.which("busybox")


@functools.cache
def _banner_version(path: Path) -> str:
    """The version a local build REPORTS. Read, never declared.

    Cached because it costs a process and the answer cannot change inside a
    session, and read lazily rather than at import: this module is collected
    (and then deselected) by every catch-all lane, and an import-time
    subprocess would be paid by all of them.
    """
    banner = probe_banner(path)
    # Identity before form. `vX.Y.Z` is a shape a great many binaries print, so
    # matching it alone would let any `busybox` on PATH — a wrapper script, a
    # toybox symlink, something a distro renamed — feed a row of the measured
    # table that six product decisions in `src/otto/host/userland.py` rest on.
    # Only bites the `[system]` row: the other five are hash-pinned artifacts.
    if "BusyBox" not in banner:
        pytest.fail(
            f"{path} announced {banner!r}, which is not a BusyBox banner — this "
            f"row measures applet behaviour and attributes it to BusyBox"
        )
    match = re.search(r"\bv(\d+\.\d+\.\d+)", banner)
    if not match:
        pytest.fail(f"{path} announced {banner!r}, which carries no vX.Y.Z version")
    return match.group(1)


@dataclass(frozen=True)
class _LocalBuild:
    """A matrix row backed by a local binary, quacking like a BusyBoxRelease."""

    path: Path

    @property
    def version(self) -> str:
        return _banner_version(self.path)


_ROWS = [pytest.param(release, id=release.version) for release in BUSYBOX_MATRIX] + [
    pytest.param(
        _LocalBuild(Path(_SYSTEM_BUSYBOX)) if _SYSTEM_BUSYBOX else None,
        id="system",
        marks=pytest.mark.skipif(
            _SYSTEM_BUSYBOX is None,
            reason=(
                "no `busybox` on PATH: this row measures the DISTRO's own build "
                "(Ubuntu ships it in `busybox-static`, which is not installed by "
                "default), so it is skipped rather than failed — unlike the five "
                "pinned artifacts, nothing can fetch it"
            ),
        ),
    )
]


@pytest.mark.busybox
def test_the_system_busybox_is_the_standalone_counterexample():
    """The only `True` in the standalone-shell story, pinned rather than prose.

    `_EXPECTED_STANDALONE_SHELL` in
    `tests/integration/busybox_bed/test_applet_userland.py`
    is five identical `False`s. Without an assertion somewhere that a
    DIFFERENT build measures `True`, that table is a constant wearing a
    discrimination's clothing: if Ubuntu ever shipped a `busybox` without
    `CONFIG_FEATURE_SH_STANDALONE`, every docstring citing this contrast —
    including the reason this module drives applets from dash rather than
    `busybox sh` — would go false at once, and nothing here would notice.
    This is that assertion, on the same `system` build `_ROWS` already
    exercises, skipped the same way when nothing is installed to test.

    Invokes `busybox sh` directly, the ONE construction this module's own
    module docstring forbids everywhere else — deliberately: proving the
    system build resolves internally is the whole point here, not a control
    to avoid.

    `PATH=/nonexistent`, not `PATH=`: an entirely empty PATH is one empty
    component, which POSIX resolves to the CURRENT DIRECTORY, so a probe run
    from a directory that happens to contain a file named `ls` would pass for
    the wrong reason regardless of which shell is running it.
    """
    if _SYSTEM_BUSYBOX is None:
        pytest.skip(
            "no `busybox` on PATH: this pins the DISTRO's own build, same as "
            "the `system` row above — nothing can fetch it"
        )
    result = subprocess.run(
        [
            _SYSTEM_BUSYBOX,
            "sh",
            "-c",
            (
                "PATH=/nonexistent; command -v ls && echo RESOLVED; "
                "command -v definitely_not_an_applet || echo NEGATIVE_CONTROL_OK"
            ),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, f"the probe did not run: {result.stderr}"
    assert "NEGATIVE_CONTROL_OK" in result.stdout, (
        f"a name that is not an applet must still fail even under this "
        f"build's standalone shell, or the RESOLVED assertion below would "
        f"prove nothing: {result.stdout!r} {result.stderr!r}"
    )
    assert "RESOLVED" in result.stdout, (
        f"the system busybox was expected to resolve `ls` with PATH pointed "
        f"nowhere useful (CONFIG_FEATURE_SH_STANDALONE) — got "
        f"{result.stdout!r} {result.stderr!r}. If the distro build changed, "
        f"every docstring citing this build as the standalone counterexample "
        f"needs revisiting in the same commit that explains why"
    )


def _binary_for(release) -> Path:
    """The artifact under test: fetched for a pinned row, local for the system."""
    return release.path if isinstance(release, _LocalBuild) else busybox_binary(release)


def _version_tuple(version: str) -> tuple:
    return tuple(int(p) for p in version.split("."))


def _run_via_dash(applets: Path, script: str) -> subprocess.CompletedProcess:
    """Run *script* under dash with PATH scoped to the applet shims."""
    return subprocess.run(
        ["/bin/sh", "-c", script],
        env={"PATH": str(applets)},
        capture_output=True,
        text=True,
        timeout=60,
        # Every caller here is MEASURING an exit status, so a raise would
        # destroy the answer rather than report a problem.
        check=False,
    )


@pytest.fixture
def applet_dir(tmp_path):
    def _make(release, applets):
        binary = _binary_for(release)
        d = tmp_path / f"applets-{release.version}"
        d.mkdir()
        for name in applets:
            (d / name).symlink_to(binary)
        return d

    return _make


@pytest.mark.busybox
@pytest.mark.parametrize("release", _ROWS)
def test_the_timeout_convention_matches_the_version(release, applet_dir):
    """`-t SECS` before 1.30, bare `SECS` from 1.30 — measured, not assumed."""
    d = applet_dir(release, ["timeout", "true"])
    coreutils = _run_via_dash(d, "timeout 1 true")
    dash_t = _run_via_dash(d, "timeout -t 1 true")

    expects_coreutils = _version_tuple(release.version) >= _COREUTILS_STYLE_FROM
    if expects_coreutils:
        assert coreutils.returncode == 0, (
            f"BusyBox {release.version} rejected coreutils-style `timeout 1 true`: "
            f"{coreutils.stderr.strip()!r}"
        )
    else:
        assert dash_t.returncode == 0, (
            f"BusyBox {release.version} rejected `timeout -t 1 true`: {dash_t.stderr.strip()!r}"
        )

    # The two spellings must be mutually exclusive, which is what lets the
    # product probe try them in either order and converge.
    #
    # The plan also carried `assert coreutils.rc == 0 or dash_t.rc == 0` here
    # and it is NOT reproduced, because it cannot fail: whichever branch above
    # ran has already asserted its own half of that disjunction. Demonstrated
    # rather than reasoned — running BOTH spellings as `timeout -Z 1 true`
    # reds all five entries at the branch assertion above and never at the
    # disjunction. A guard that cannot fail is worse than no guard: it reads
    # as coverage of "some spelling works" while pinning nothing.
    assert not (coreutils.returncode == 0 and dash_t.returncode == 0), (
        f"BusyBox {release.version} accepted BOTH spellings — the product probe "
        f"assumes they are mutually exclusive and would pick arbitrarily"
    )


@pytest.mark.busybox
@pytest.mark.parametrize("release", _ROWS)
def test_which_decode_spelling_base64_accepts(release, applet_dir):
    """The `shell` transfer needs a decode spelling that works on the target.

    Recorded per version rather than assumed: the applet did not always exist,
    and `-d` vs `--decode` is exactly the kind of difference that turns a
    transfer into a silent corruption rather than a loud failure.

    Both directions are pinned because both are informative. A version below
    the boundary that suddenly decodes means the artifact changed under us; a
    version above it that stops decoding means the transfer lost a target it
    was built for. Neither may pass quietly.
    """
    d = applet_dir(release, ["base64", "echo"])
    short = _run_via_dash(d, "echo aGk= | base64 -d")
    gnu_long = _run_via_dash(d, "echo aGk= | base64 --decode")

    if _version_tuple(release.version) >= _BASE64_APPLET_FROM:
        # The DECODED VALUE, not the exit status: an applet that echoed its
        # input back, or printed nothing at all, would exit 0 and corrupt a
        # transfer silently. `hi` is what `aGk=` must become.
        assert short.stdout.strip() == "hi", (
            f"BusyBox {release.version} `base64 -d` gave "
            f"{short.stdout.strip()!r} (rc={short.returncode}, "
            f"{short.stderr.strip()!r}) — the shell transfer decodes with this"
        )
    else:
        # ABSENT means the applet is not in the build, and that is a stronger
        # claim than a non-zero exit — which `base64 -d` would also give if the
        # applet existed and merely rejected the flag. The two need different
        # gap-registry entries and different product behaviour (no decode at
        # all, versus a decode under another spelling), and the exit status
        # cannot tell them apart: the same missing-applet failure exits 1 on
        # 1.16.1 and 127 on 1.21.1. Only the diagnostic distinguishes them.
        assert "applet not found" in short.stderr, (
            f"BusyBox {release.version} was expected to have no `base64` applet "
            f"at all, but failed with {short.stderr.strip()!r} (rc="
            f"{short.returncode}) — if the applet is present and only the "
            f"spelling is wrong, this is a different gap and a different fix"
        )

    assert gnu_long.returncode != 0, (
        f"BusyBox {release.version} accepted GNU's `--decode` — the probe's "
        f"`--decode` arm is reachable after all, so it is no longer dead code"
    )


# Measured, not assumed: `stat -c %s FILE` reports the byte size on EVERY
# artifact in the matrix, including 1.16.1, and GNU's long spelling
# `--format=%s` is rejected by every one of them ("unrecognized option"). So
# the probe must spell it `-c`, and `wc -c` is the fallback for a build with
# `stat` compiled out rather than an alternative spelling of the same thing.
_STAT_SHORT_FORMAT = "stat -c %s"
_STAT_GNU_LONG_FORMAT = "stat --format=%s"
# Neither round nor zero, and that is the whole point. The product probes
# `stat -c %s /dev/null`, which a `stat` that ignored the format string
# entirely would still answer "0" to, exit 0, and be believed. A size no
# implementation can produce by accident is what makes this a measurement
# rather than a liveness check.
_SIZE_PROBE_BYTES = 12345


@pytest.mark.busybox
@pytest.mark.parametrize("release", _ROWS)
def test_which_stat_spelling_reports_a_files_size(release, applet_dir, tmp_path):
    """Which spelling yields a file's size in bytes, per version.

    `stat_size` is a probed capability precisely because `stat` is optional in
    a BusyBox build; the transfer needs a size to verify what it moved, and
    the answer recorded here is the one the product probe is built from.
    """
    d = applet_dir(release, ["stat", "wc"])
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"x" * _SIZE_PROBE_BYTES)
    size = str(_SIZE_PROBE_BYTES)

    short = _run_via_dash(d, f"{_STAT_SHORT_FORMAT} {payload}")
    gnu_long = _run_via_dash(d, f"{_STAT_GNU_LONG_FORMAT} {payload}")
    redirected = _run_via_dash(d, f"wc -c < {payload}")
    with_operand = _run_via_dash(d, f"wc -c {payload}")

    assert short.stdout.strip() == size, (
        f"BusyBox {release.version} `{_STAT_SHORT_FORMAT}` gave "
        f"{short.stdout.strip()!r} for a {size}-byte file (rc={short.returncode}, "
        f"{short.stderr.strip()!r}) — the probe's `stat` arm reports sizes with this"
    )
    assert gnu_long.returncode != 0, (
        f"BusyBox {release.version} accepted GNU's `{_STAT_GNU_LONG_FORMAT}` — "
        f"the probe may spell the format either way, not only `-c`"
    )
    # The universal fallback, and the reason it is spelled with a redirect:
    # given the file as an OPERAND, `wc` echoes the name back beside the
    # count, so a caller parsing int(stdout) breaks on the very hosts the
    # fallback exists for.
    assert redirected.stdout.strip() == size, (
        f"BusyBox {release.version} `wc -c <` gave {redirected.stdout.strip()!r} "
        f"for a {size}-byte file — the probe's fallback arm has no answer left"
    )
    # Asserted as what it DOES print, not as "something other than the count":
    # a `wc` that failed outright would also satisfy `!= [size]` and would
    # report the redirect as load-bearing for the wrong reason.
    assert with_operand.returncode == 0, (
        f"BusyBox {release.version} `wc -c FILE` failed outright "
        f"({with_operand.stderr.strip()!r}) — this arm exists to show what the "
        f"OPERAND form prints, and it printed nothing"
    )
    assert with_operand.stdout.split() == [size, str(payload)], (
        f"BusyBox {release.version} `wc -c FILE` printed "
        f"{with_operand.stdout.strip()!r} — the fallback's redirect is "
        f"load-bearing only while this form appends the filename"
    )


# The GET prefetch's spelling, measured the same day on every row and on
# coreutils. `-L` is load-bearing: the sender's `< src` follows a symlink,
# and a bare `stat` reports the LINK's own length -- 23 for a link to a
# 12-byte file -- so without `-L` a size-terminated read would stop at the
# wrong N. `%F` is what lets the backend refuse a source that is not a regular
# file by name; its phrases are coreutils' on every build here. It cannot
# tell a procfs pseudo-file from an empty regular file ("0 regular empty
# file" for both), which is why that case is documented rather than refused.
_STAT_GET_FORMAT = "stat -L -c '%s %F'"


@pytest.mark.busybox
@pytest.mark.parametrize("release", _ROWS)
def test_the_get_prefetch_spelling_reports_the_targets_size_and_type(release, applet_dir, tmp_path):
    """`stat -L -c '%s %F'` answers "SIZE TYPE" for what `< src` will send, per row.

    `src/otto/host/transfer/nc.py` reads exactly this many bytes and then
    closes, so a row whose `stat` stopped following the link, dropped `%F`,
    or worded the type differently would either truncate every symlinked GET
    or refuse every regular file. The bare-`stat` line is asserted UNEQUAL to
    the size so `-L` is measured as doing something, not merely tolerated.
    """
    d = applet_dir(release, ["stat"])
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"x" * _SIZE_PROBE_BYTES)
    link = tmp_path / "link"
    link.symlink_to(payload)
    empty = tmp_path / "empty"
    empty.write_bytes(b"")

    via_link = _run_via_dash(d, f"{_STAT_GET_FORMAT} {link}")
    bare = _run_via_dash(d, f"{_STAT_SHORT_FORMAT} {link}")
    empty_answer = _run_via_dash(d, f"{_STAT_GET_FORMAT} {empty}")
    device = _run_via_dash(d, f"{_STAT_GET_FORMAT} /dev/null")

    assert via_link.stdout.strip() == f"{_SIZE_PROBE_BYTES} regular file", (
        f"BusyBox {release.version} `{_STAT_GET_FORMAT}` gave {via_link.stdout.strip()!r} "
        f"through a symlink to a {_SIZE_PROBE_BYTES}-byte file (rc={via_link.returncode}, "
        f"{via_link.stderr.strip()!r}) — the GET prefetch reads exactly this many bytes"
    )
    assert bare.stdout.strip() != str(_SIZE_PROBE_BYTES), (
        f"BusyBox {release.version} `{_STAT_SHORT_FORMAT}` on a symlink gave the TARGET's "
        f"size {bare.stdout.strip()!r} — `-L` is no longer doing anything measurable here"
    )
    assert empty_answer.stdout.strip() == "0 regular empty file", (
        f"BusyBox {release.version} `{_STAT_GET_FORMAT}` gave {empty_answer.stdout.strip()!r} "
        f"for an empty file — the backend's regular-file allow-list would refuse it"
    )
    assert device.stdout.strip() == "0 character special file", (
        f"BusyBox {release.version} `{_STAT_GET_FORMAT}` gave {device.stdout.strip()!r} "
        f"for /dev/null — the type phrase the backend refuses by is not this build's"
    )


# `nc -N` is an OpenBSD netcat option BusyBox has never had, and it was otto's
# sender spelling until 2026-08-25. The rejection is worded two ways across the
# matrix (`invalid option -- N` up to 1.21.1, `unrecognized option: N` from
# 1.28.1), so the differential below compares OUTPUT rather than matching
# either string. It was the product's own probe spelling until the universal
# spelling retired both the probe and the option; the differential stays here
# because it is the only form that separates the two cases on real binaries.
_NC_DIFFERENTIAL = '[ "$(nc 2>&1 </dev/null)" = "$(nc -N 2>&1 </dev/null)" ]'


@pytest.mark.busybox
@pytest.mark.parametrize("release", _ROWS)
def test_no_row_accepts_the_dash_n_the_nc_backend_once_sent_with(release, applet_dir):
    """The `nc` backend's GET used to ask the device to send with `nc -N`. No row can.

    A MEASUREMENT OF THE APPLET, NOT A DEPENDENCY OF otto'S. It was taken
    2026-08-13 for the `nc-transfer` gap, which the universal spelling closed on
    2026-08-25 (`docs/superpowers/specs/2026-08-25-nc-universal-spelling-design.md`):
    otto emits no `-N` in any direction now. The row stays because what it pins
    is true of these binaries and is the fact any future `-N` would have to
    argue with -- not because anything otto sends depends on the answer.

    THE DIFFERENTIAL IS WHAT IS MEASURED, not the error text and not the exit
    code, because neither of those separates the two cases on real binaries: a
    destination-less `nc` exits 1 on every artifact here AND on a real OpenBSD
    netcat, and the rejection wording moved between 1.21.1 and 1.28.1. What
    does separate them is whether adding `-N` CHANGES the answer, and that is
    the product's own predicate.

    Both halves are asserted, and the second is the one that stops this being
    a test that would pass against anything: a control option `-Q` -- which no
    netcat here has either -- must also come back different, so a probe that
    simply reported "differs" for every input could not satisfy the first
    assertion while the emitted `-N` genuinely parsed. What this tier cannot
    show is the ACCEPTING side; that is not a BusyBox row. It was measured all
    the same -- OpenBSD netcat 1.226 answers "supported", 2026-08-14 -- but it
    was the closed record's `measured_on` that carried it, so that half is git
    history now and not a table this row can send a reader to.

    NOTHING IS CONNECTED. Every invocation here is destination-less, so no
    socket is opened and nothing binds -- the same property that lets otto
    issue this against a real device during resolution.
    """
    d = applet_dir(release, ["nc"])

    bare = _run_via_dash(d, "nc </dev/null")
    with_n = _run_via_dash(d, "nc -N </dev/null")
    differential = _run_via_dash(d, _NC_DIFFERENTIAL)
    control = _run_via_dash(d, _NC_DIFFERENTIAL.replace("nc -N 2>&1", "nc -Q 2>&1"))

    assert differential.returncode != 0, (
        f"BusyBox {release.version} answered `nc` and `nc -N` identically, so this "
        f"build parses `-N` -- measured 2026-08-13 for the since-closed `nc-transfer` "
        f"gap (spec 2026-08-25-nc-universal-spelling): otto no longer emits `-N`, so "
        f"this row is a measurement of the applet, not a dependency of otto's"
    )
    assert control.returncode != 0, (
        f"BusyBox {release.version} answered `nc -Q` identically to bare `nc`, so this "
        f"artifact does not reject unknown options at all and the differential above "
        f"measures nothing on it"
    )
    # The same comparison made in PYTHON, from the two invocations' real
    # output, so the shell one-liner above is checked against what the binary
    # actually printed rather than trusted to mean what it says.
    assert bare.stdout or bare.stderr, (
        f"BusyBox {release.version} printed nothing for a destination-less `nc`; the "
        f"differential compares those outputs and two empty strings would compare EQUAL, "
        f"reporting a rejected option as an accepted one"
    )
    assert (bare.stdout + bare.stderr) != (with_n.stdout + with_n.stderr), (
        f"BusyBox {release.version} printed the same thing for `nc` and `nc -N`, which "
        f"is what the shell differential is checking -- the two must not disagree"
    )


# The two applet-facing claims otto's universal `nc` spelling rests on, and
# the only two rows in this module that open a socket. Both come from the
# measurement campaign in
# `docs/superpowers/specs/2026-08-25-nc-universal-spelling-design.md`, taken
# 2026-08-25 against these same five artifacts plus OpenBSD netcat 1.226:
#
#   1. a listener spelled `nc -l -p PORT` binds, receives intact bytes, and
#      exits rc 0 when the PEER closes;
#   2. a sender spelled `nc HOST PORT < file` -- with NO `-N`, which no row
#      here parses (see the differential above) -- is terminated by the
#      RECEIVER's close after exactly N bytes.
#
# Claim 2 is what replaces `-N`, so it is not an optimisation to be traded
# away later: `src/otto/host/transfer/nc.py` reads exactly the stat-reported
# size and closes, and the remote sender exits because of that close and
# nothing else.
#
# Same three-copy rule as the spellings in the module docstring, with a
# different trio: `src/otto/host/transfer/nc.py` issues these at runtime
# (`_put_files_nc`, `_get_files_nc`, `_get_files_nc_tunneled`),
# `tests/unit/host/test_transfer_nc_get.py` pins the whole command string it
# issues and `tests/unit/host/test_transfer_nc_put.py` pins the ` -l -p `
# spelling inside it, both against a doubled transport; and the two rows
# below are the only place a REAL applet answers. An upstream artifact that changed either behaviour
# reddens the row that changed, per version, which no amount of doubled
# transport can do.
#
# The campaign's fourth row -- "receiver never closes" HANGS everywhere -- is
# deliberately NOT reproduced. It is the control that gives claim 2 its
# meaning, and it was measured on every userland that day, but a test whose
# pass condition is a deliberate hang costs its own timeout on every run and
# reds as a timeout when it regresses, which is the least diagnosable shape a
# failure has. The exit-on-close half is the load-bearing one and it fails
# fast.

# Binary-hostile on purpose: NUL, LF, CR, 0xFF, a quote and a backslash are
# the bytes a transfer path mangles the moment anything in it is text-shaped.
# The same prefix the live bed's `_HOSTILE_PAYLOAD` uses
# (`tests/integration/busybox_bed/test_shell_codec.py`), COPIED rather than
# imported: that module's copy is pinned by an md5 the BusyBox spec names, so
# an import would couple two tiers' payloads and let a change here silently
# rewrite what that pin claims to cover.
_HOSTILE_PREFIX = b"A\x00B\nC\rD\xffE'F\\G"
# 256 KiB, the size the campaign measured with, and far past the socket
# buffers in both directions -- a payload that fits in one buffer is delivered
# by a single non-blocking write and never exercises the flow control the
# receiver's close has to unblock.
_PAYLOAD_BYTES = 256 * 1024
# DETERMINISTIC filler, never `os.urandom`. A corruption inside a quarter of a
# megabyte is only diagnosable if the next run produces the same bytes at the
# same offset; a random payload turns a reproducible byte-level finding into
# an anecdote. Cycling all 256 values also puts every byte value on the wire,
# not only the hostile ones spelled out above.
_PAYLOAD = (_HOSTILE_PREFIX + bytes(range(256)) * (_PAYLOAD_BYTES // 256))[:_PAYLOAD_BYTES]

# RUNAWAY GUARDS, NOT DISCRIMINATORS. The campaign measured every row exiting
# within 0.02s of the peer's close, and that number is deliberately not
# asserted anywhere below: these artifacts run under qemu-user on the dev VM,
# so a 0.02s wall-clock bound would measure the machine's load and would red
# on a busy runner while the applet behaved perfectly. What is asserted is the
# qualitative claim the design actually rests on -- that the process exits AT
# ALL once the peer closes -- and every wait here is bounded only so that a
# regression arrives as a red instead of a hung lane.
_EXIT_TIMEOUT_S = 10
_SOCKET_TIMEOUT_S = 10
# A listener has to bind before anything can connect, and under qemu-user that
# costs an emulated start-up. Polled rather than slept, so the ordinary case
# connects on the first attempt and pays nothing.
_LISTEN_DEADLINE_S = 10
_LISTEN_POLL_S = 0.05


def _free_port() -> int:
    """An OS-assigned port, released before the applet is told to bind it.

    Never a hardcoded port. This tier runs under `-n auto`, and two workers
    sharing a constant collide as `bind: Address in use` on whichever loses --
    a flake that reads exactly like the parsing regression these rows exist to
    catch, which is the worst thing a flake can look like. The gap between
    this close and the applet's bind is a real race and it is the standard
    one: the kernel will not hand the same ephemeral port to another asker
    while this socket is open, and the applet claims it microseconds after it
    is not.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _reap(proc: subprocess.Popen) -> None:
    """Leave no applet running, however the block above exited.

    Every failure path here is a process that did NOT exit when it should
    have, so cleanup cannot be left to garbage collection: an abandoned
    listener holds its port and outlives the session that spawned it.
    """
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=_EXIT_TIMEOUT_S)


def _read_text(path: Path) -> str:
    """Whatever the applet said, safe to interpolate into a failure message."""
    return path.read_text(errors="replace").strip()


def _describe(seen: bytes, expected: bytes) -> str:
    """Where two payloads diverged -- never the payloads themselves.

    A 256 KiB literal in an assertion message buries the finding under a
    quarter of a megabyte of hex. The offset and the two bytes at it are what
    separates a truncation from a corruption, which are different bugs with
    different owners.
    """
    if len(seen) != len(expected):
        return f"got {len(seen)} bytes, expected {len(expected)}"
    # `strict=True` states the invariant the branch above just established:
    # the lengths are equal here, so a raise would mean this function's own
    # logic drifted, not that the payloads differ.
    for offset, (a, b) in enumerate(zip(seen, expected, strict=True)):
        if a != b:
            return f"first difference at byte {offset}: {a:#04x} != {b:#04x}"
    return "identical"


def _read_exactly(conn: socket.socket, count: int) -> bytes:
    """Read exactly *count* bytes, or as many as arrive before EOF.

    Returns short rather than raising, so the caller's assertion reports the
    shortfall against the expected payload instead of an exception hiding how
    far the transfer got. The same shape as `_get_files_nc`'s read loop, which
    turns a short read into an error naming got-versus-expected.
    """
    chunks = []
    remaining = count
    while remaining > 0:
        chunk = conn.recv(min(remaining, 1 << 16))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _argv(proc) -> str:
    """The applet command as actually spawned, for a message that cannot lie.

    Rendered from `proc.args` rather than written out again beside it: a
    hand-copied spelling in a failure message is a fourth copy nothing checks,
    and it says `-l -p PORT` most convincingly at exactly the moment somebody
    has changed the spawn to something else.
    """
    return " ".join(str(arg) for arg in proc.args[1:])


def _connect_to_the_listener(proc, port: int, stderr_path: Path, release) -> socket.socket:
    """Connect once the applet is listening, or fail saying why it never was.

    TWO ARMS BECAUSE THE MISSING `-p` FAILS THREE DIFFERENT WAYS, measured by
    dropping it from the spawn below (2026-08-25, this machine):

    ===========================  ================================================
    row                          `nc -l PORT`, no `-p`
    ===========================  ================================================
    1.16.1, 1.21.1               rc 1, ``nc: bad address 'PORT'``
    1.28.1, 1.31.0, 1.35.0       ALIVE, listening on an OS-assigned RANDOM port
    system (1.36.1)              rc 1, ``nc: bind: Cannot assign requested address``
    ===========================  ================================================

    The middle row is why the deadline exists and why neither arm can be
    dropped as redundant: three of six builds NEVER READ THE TRAILING
    POSITIONAL AT ALL -- not as a port, and not as an address either -- and
    listen on the wildcard at an OS-assigned port, perfectly healthy, on a
    port nobody will ever connect to.

    "Ignored" rather than "resolved as a hostname" is a measurement, taken on
    all three of those rows with a probe that separates the two (the same day;
    `ss -lntp` filtered by the child's pid). Resolution would bind the address
    it resolved: it does not. `nc -l 127.0.0.2` -- an assignable address --
    binds ``0.0.0.0:RANDOM``, as does `nc -l 32873` and as does
    `nc -l 0.0.128.105`, the address `inet_aton` decodes 32873 to. Bare
    `nc -l`, with no positional whatsoever, gives the same shape one family
    over (``*:RANDOM``, the dual-stack wildcard; supplying any positional is
    the only thing that changes, selecting the IPv4 wildcard instead). The
    rows that DO read it say so out loud, which is how the contrast was
    noticed: `bad address` on 1.16.1/1.21.1, and on the system build
    ``Cannot assign requested address`` -- 34919 decoding to 0.0.136.103,
    which is not an address this machine has.

    So the port comes from `-p` and from nowhere else, on every build here.
    Nothing is printed and nothing exits, so the only observable is that the
    connect never succeeds — which on a real transfer is the listener-timeout
    the closed `nc-transfer` gap record described. The `poll()` arm is what
    turns the other three into a one-millisecond red carrying the applet's own
    words instead of a ten-second connection refusal.
    """
    connection: socket.socket | None = None
    last_refusal: OSError | None = None

    def answered() -> bool:
        nonlocal connection, last_refusal
        if proc.poll() is not None:
            pytest.fail(
                f"BusyBox {release.version} `{_argv(proc)}` exited rc={proc.returncode} "
                f"without ever accepting a connection: {_read_text(stderr_path)!r}. This is "
                f"the listener spelling `src/otto/host/transfer/nc.py` emits for every PUT "
                f"and every tunnelled GET, so this row is a dependency of otto's, not a "
                f"measurement of the applet"
            )
        try:
            connection = socket.create_connection(("127.0.0.1", port), timeout=_SOCKET_TIMEOUT_S)
        except OSError as e:
            last_refusal = e
            return False
        return True

    wait_for(
        answered,
        _LISTEN_DEADLINE_S,
        interval=_LISTEN_POLL_S,
        on_timeout=lambda: (
            f"BusyBox {release.version} `{_argv(proc)}` is still running but nothing "
            f"answered on 127.0.0.1:{port} within {_LISTEN_DEADLINE_S}s ({last_refusal}); "
            f"it said {_read_text(stderr_path)!r}. A build that ignores the trailing "
            f"positional listens on the wildcard at an OS-assigned port instead, and "
            f"waits there, healthy, forever"
        ),
    )
    assert connection is not None
    return connection


def _wait_for_exit(proc, release, stderr_path: Path) -> int:
    """The peer has closed; the applet must now exit. Bounded, so a hang is a red."""
    try:
        return proc.wait(timeout=_EXIT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        pytest.fail(
            f"BusyBox {release.version} `{_argv(proc)}` was still running {_EXIT_TIMEOUT_S}s "
            f"after the peer closed the connection. The close is the ONLY thing otto uses to "
            f"end it -- there is no `-N` and no `-w` in any spelling "
            f"`src/otto/host/transfer/nc.py` emits, so a build that stops exiting here hangs "
            f"every transfer in that direction. It said {_read_text(stderr_path)!r}"
        )


@pytest.mark.busybox
@pytest.mark.parametrize("release", _ROWS)
def test_the_listener_accepts_dash_l_dash_p_and_exits_on_the_peers_close(release, tmp_path):
    """`-l -p PORT` binds, receives, and exits rc 0 at the peer's close -- per row.

    The universal PUT spelling's two applet-facing claims in one differential:
    a row that stopped parsing `-l -p` reddens before any byte moves -- at the
    bind on the builds that refuse it, at the connect deadline on the ones
    that ignore the positional and listen somewhere else entirely (see
    `_connect_to_the_listener`, which measured the split) -- and a row whose
    listener stopped exiting on the peer's close reddens at the bounded wait.
    The payload is binary-hostile, so a build that mangled it would red on the
    bytes rather than pass on a liveness check.

    RUN AS A MULTI-CALL ARGV, not through this module's `applet_dir`/dash
    harness, and neither half of that is a departure from the module
    docstring's rule. The rule forbids resolving an applet through a SHELL
    whose resolution is a build-config property; naming the applet as argv[1]
    of the binary asks no resolution question at all. And `_run_via_dash` is a
    blocking `subprocess.run`, which cannot express a process that has to be
    ALIVE while this test connects to it.
    """
    binary = _binary_for(release)
    received = tmp_path / "received.bin"
    errors = tmp_path / "listener.err"
    port = _free_port()

    # `stdin=DEVNULL` mirrors the product's `< /dev/null` on the receiving
    # listener, and stderr goes to a FILE rather than a pipe: the diagnostics
    # above read it while the process is still running, which a pipe cannot
    # serve without risking the reader deadlocking against a full buffer.
    with received.open("wb") as sink, errors.open("wb") as err:
        proc = subprocess.Popen(
            [str(binary), "nc", "-l", "-p", str(port)],
            stdin=subprocess.DEVNULL,
            stdout=sink,
            stderr=err,
        )
    try:
        with _connect_to_the_listener(proc, port, errors, release) as client:
            client.settimeout(_SOCKET_TIMEOUT_S)
            client.sendall(_PAYLOAD)
        # The `with` closed the client. Nothing else is done to the applet --
        # no signal, no timeout, no option -- so whatever happens next is the
        # applet's own response to the peer going away.
        rc = _wait_for_exit(proc, release, errors)
    finally:
        _reap(proc)

    seen = received.read_bytes()
    assert seen == _PAYLOAD, (
        f"BusyBox {release.version} `{_argv(proc)}` did not write back what was sent to "
        f"it ({_describe(seen, _PAYLOAD)}) -- otto's PUT verifies the destination size "
        f"afterwards, so this is a transfer that fails, not one that corrupts silently, but "
        f"it is a transfer that fails on every file"
    )
    assert rc == 0, (
        f"BusyBox {release.version} `{_argv(proc)}` exited rc={rc} after a COMPLETE "
        f"transfer and a clean peer close, having said {_read_text(errors)!r}. Measured rc 0 "
        f"on every row 2026-08-25; otto reads no remote rc as a success signal (the spec's "
        f"'Remote exit codes are not portable'), so a change here is a finding about the "
        f"artifact rather than a broken transfer -- record it, do not relax it"
    )


@pytest.mark.busybox
@pytest.mark.parametrize("release", _ROWS)
def test_a_dash_n_less_sender_is_terminated_by_the_receivers_close(release, tmp_path):
    """Reading exactly N bytes and closing ends `nc HOST PORT < file` -- per row.

    THIS IS WHAT REPLACES `-N`. The differential above shows no row here
    parses `-N`, so otto's GET cannot ask the sender to half-close when its
    stdin ends; it reads exactly the stat-reported size and closes, and the
    close is what ends the remote sender. That mechanism is a claim about the
    APPLET, and this is the only place it is checked against one.

    The control -- a receiver that never closes hangs every row -- was
    measured 2026-08-25 and is NOT re-run here. A test whose pass condition is
    a deliberate hang pays a timeout on every green run and reports its own
    regression as a timeout, which is the least diagnosable failure shape
    available. The exit-on-close half is the load-bearing one, and it fails in
    milliseconds.

    The Python listener is the receiver on purpose: it is otto's side of the
    transfer, and reading exactly `len(_PAYLOAD)` and closing is the same loop
    `_get_files_nc` runs against `sizes[src]`.
    """
    binary = _binary_for(release)
    source = tmp_path / "payload.bin"
    source.write_bytes(_PAYLOAD)
    errors = tmp_path / "sender.err"

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(_SOCKET_TIMEOUT_S)
        port = listener.getsockname()[1]

        # No `-N`, and no other option either: this argv is the whole GET
        # spelling `src/otto/host/transfer/nc.py` emits, `< src` included.
        with source.open("rb") as stdin, errors.open("wb") as err:
            proc = subprocess.Popen(
                [str(binary), "nc", "127.0.0.1", str(port)],
                stdin=stdin,
                stdout=subprocess.DEVNULL,
                stderr=err,
            )
        try:
            try:
                conn, _ = listener.accept()
            except OSError as e:
                pytest.fail(
                    f"BusyBox {release.version} `{_argv(proc)}` never connected within "
                    f"{_SOCKET_TIMEOUT_S}s ({e}); rc={proc.poll()}, it said "
                    f"{_read_text(errors)!r}"
                )
            with conn:
                conn.settimeout(_SOCKET_TIMEOUT_S)
                seen = _read_exactly(conn, len(_PAYLOAD))
            # `conn` is closed. The sender was given no `-N`, no `-w` and no
            # signal, so this close is the only thing that can end it.
            rc = _wait_for_exit(proc, release, errors)
        finally:
            _reap(proc)

    assert seen == _PAYLOAD, (
        f"BusyBox {release.version} `{_argv(proc)} < file` did not deliver the file "
        f"({_describe(seen, _PAYLOAD)}) -- under size-terminated reads a short delivery is an "
        f"explicit short-read error in otto, so this reds a GET rather than truncating one"
    )
    assert rc == 0, (
        f"BusyBox {release.version} `{_argv(proc)} < file` exited rc={rc} after the "
        f"receiver read all {len(_PAYLOAD)} bytes and closed, having said "
        f"{_read_text(errors)!r}. Measured rc 0 on every row 2026-08-25 for a close at N "
        f"(a close at N/2 splits the matrix, which is why otto reads no remote rc as a "
        f"success signal) -- a change here is a finding about the artifact, and the "
        f"TERMINATION asserted above is the half otto depends on"
    )
