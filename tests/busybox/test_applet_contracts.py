"""Tier 1: real BusyBox argument parsing, per version, without a container.

DRIVEN FROM /bin/sh (dash), NEVER from `busybox sh`. Applet resolution that
ignores PATH is a BUILD-CONFIG property (`CONFIG_FEATURE_SH_STANDALONE`), not
a universal fact about "BusyBox's ash" — measured off in every busybox.net
prebuilt this project fetches, see `tests/busybox/test_applet_resolution.py`'s
`_EXPECTED_STANDALONE_SHELL` table — but the system package apt installs for
the `system` row below HAS it on: during the 2026-08-10 timeout work a control
'verified' broken code because `busybox sh` resolved applets internally
regardless of PATH, silently exercising BusyBox's own builtin instead of the
shim under test. Anything asserting applet RESOLUTION belongs in Tier 2, not
here.

The command SPELLINGS measured here have two other copies, and all three have
to agree: `src/otto/host/userland.py` issues them at runtime, and
`tests/unit/host/test_userland.py` pins the exact list and order it issues.
They are deliberately not shared through an import — a product reading its
spellings from a test could not be caught drifting by that test — so each of
the three names the other two.

Not every probe `Userland` issues has a row here, only the ones with a real
argument-parsing question a BusyBox version could answer differently:
`timeout`, `base64`, `stat`, `wc`. `Userland.elevation`'s `command -v`
presence checks, `Userland.shell_dialect`'s `$BASH_VERSION` variable read, and
`Userland.checksum`'s single-spelling `md5sum < /dev/null` probe have no such
question, so they carry no Tier 1 row and only the other two copies exist for
them -- `checksum`'s PRESENCE and OUTPUT format (not this exact probe
spelling) is substantiated instead by `test_shell_codec_contracts.py`'s real
`md5sum` round trips over the matrix.
"""

import functools
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

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

    `_EXPECTED_STANDALONE_SHELL` in `tests/busybox/test_applet_resolution.py`
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
