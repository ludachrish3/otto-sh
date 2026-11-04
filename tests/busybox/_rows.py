"""The BusyBox matrix rows and the drivers that run a script against one.

Shared by the tier's modules: ``test_applet_contracts.py`` (argv-level applet
spellings, driven from dash) and ``test_gcda_sweep.py`` (the coverage counter
removal, driven from ``busybox sh``). Nothing here runs at import: this module
is collected by every catch-all lane, so the banner read and the artifact
fetch happen lazily, inside a selected test.
"""

import functools
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests._fixtures.busybox import BUSYBOX_MATRIX, busybox_binary, probe_banner

# The distro's own BusyBox — the sixth matrix row, and the only one that is a
# real DISTRO BUILD with distro config rather than an upstream defconfig
# prebuild. That is exactly the spec's point that behaviour is a function of
# version AND build config, so it is the row most able to contradict the other
# five. It is deliberately not a `busybox_binary()` artifact: there is nothing
# to fetch, nothing to pin (its bytes are whatever the distro shipped, and its
# updates are the distro's), and it needs no interpreter because it is built
# for this machine. Resolved through `which` rather than hardcoded, so the row
# exercises the binary a shell on this host would actually reach.
SYSTEM_BUSYBOX = shutil.which("busybox")


@functools.cache
def banner_version(path: Path) -> str:
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
class LocalBuild:
    """A matrix row backed by a local binary, quacking like a BusyBoxRelease."""

    path: Path

    @property
    def version(self) -> str:
        return banner_version(self.path)


SYSTEM_BUILD = LocalBuild(Path(SYSTEM_BUSYBOX)) if SYSTEM_BUSYBOX else None
"""The ``system`` row's build, or ``None`` when no ``busybox`` is on PATH."""

ARTIFACT_ROWS = [pytest.param(release, id=release.version) for release in BUSYBOX_MATRIX]
"""The pinned busybox.net artifacts, one row each."""

ROWS = [
    *ARTIFACT_ROWS,
    pytest.param(
        SYSTEM_BUILD,
        id="system",
        marks=pytest.mark.skipif(
            SYSTEM_BUSYBOX is None,
            reason=(
                "no `busybox` on PATH: this row measures the DISTRO's own build "
                "(Ubuntu ships it in `busybox-static`, which is not installed by "
                "default), so it is skipped rather than failed — unlike the five "
                "pinned artifacts, nothing can fetch it"
            ),
        ),
    ),
]
"""Every row: the artifacts plus the system build, which this list skips when absent."""


def binary_for(release) -> Path:
    """The artifact under test: fetched for a pinned row, local for the system."""
    return release.path if isinstance(release, LocalBuild) else busybox_binary(release)


def version_tuple(version: str) -> tuple:
    return tuple(int(p) for p in version.split("."))


def run_via_dash(applets: Path, script: str) -> subprocess.CompletedProcess:
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


SWEEP_APPLETS = ["sh", "find", "rm", "rmdir", "ls", "printf", "test", "["]
"""The applets the coverage removal and its probes run, linked into the PATH dir."""


def run_in_busybox_sh(
    release, applets: Path, script: str, *, timeout: float = 120
) -> subprocess.CompletedProcess:
    """Run *script* under ``busybox sh`` with PATH holding only BusyBox applet links.

    The outer shell is the row's own ``busybox sh``, and every name the script
    runs resolves to a BusyBox applet whether or not the build's standalone
    shell is on (``CONFIG_FEATURE_SH_STANDALONE``; see
    ``test_applet_contracts.py``'s module docstring), so no GNU tool can stand
    in. ``LC_ALL=C`` makes the diagnostics the ones otto's parsers read.
    """
    return subprocess.run(
        [str(binary_for(release)), "sh", "-c", script],
        env={"PATH": str(applets), "LC_ALL": "C"},
        capture_output=True,
        text=True,
        timeout=timeout,
        # The callers measure the exit status and the output; a raise would
        # destroy the answer.
        check=False,
    )
