"""What only a real BusyBox root can measure: absence, and how a shell resolves.

Tier 1 can already tell "compiled out of this build" apart from "present but
merely unshimmed" — it parses BusyBox's own `applet not found` diagnostic
string. What it cannot do is prove absence STRUCTURALLY rather than by string
match: this tier's `--install -s` never creates the symlink for a missing
applet at all, so `command -v` here has nothing to find regardless of what any
diagnostic says. Reproducing a Tier 1 finding through that different mechanism
is what turns "the string says so" into "the filesystem agrees".

It also measures a second, independent thing: whether the shell itself needs
PATH at all to find an applet. That turns out to be a BUILD-CONFIG property
(`CONFIG_FEATURE_SH_STANDALONE`), not a fact about "BusyBox's ash" in general —
see `_EXPECTED_STANDALONE_SHELL` below for the measurement and why it matters.
"""

import pytest

from tests._fixtures.busybox import BUSYBOX_MATRIX, require_interpreter
from tests._fixtures.busybox_rootfs import busybox_rootfs, require_userns, run_in_rootfs

pytestmark = [pytest.mark.busybox]

# Measured inside the rootfs, one row per matrix entry. `base64` absent on
# 1.16.1 is the load-bearing entry: it is the version transition the `shell`
# transfer backend will have to answer for, and it was first found from
# Tier 1 — reproducing it here through a different mechanism is what makes it a
# fact about BusyBox rather than about one harness.
_EXPECTED_BASE64 = {
    "1.16.1": False,
    "1.21.1": True,
    "1.28.1": True,
    "1.31.0": True,
    "1.35.0": True,
}


@pytest.mark.parametrize("release", BUSYBOX_MATRIX, ids=[r.version for r in BUSYBOX_MATRIX])
def test_base64_presence_matches_what_the_matrix_records(release):
    """A version transition, asserted per row rather than as "some rows lack it".

    An aggregate assertion ("at least one row has no base64") passes when the
    wrong row is the one missing it, which is precisely the confusion a
    transfer backend keyed on this would suffer.

    Parametrised over `BUSYBOX_MATRIX` directly, NOT filtered down to
    `_EXPECTED_BASE64`'s keys. A filtered row set makes a future artifact
    added to the matrix silently get zero Tier 2 coverage — no failure, no
    row, nothing to notice. Indexing the table below is the loud direction
    instead: a matrix row with no table entry fails this test with a
    `KeyError` naming the version, rather than never running at all.
    """
    require_interpreter(release.arch)
    require_userns()

    with busybox_rootfs(release) as root:
        result = run_in_rootfs(root, "command -v base64 && echo PRESENT || echo ABSENT")

    assert result.returncode == 0, f"the probe did not run: {result.stderr}"
    seen = "PRESENT" in result.stdout
    assert seen is _EXPECTED_BASE64[release.version], (
        f"BusyBox {release.version}: base64 "
        f"{'appeared' if seen else 'vanished'} relative to the recorded matrix "
        f"({_EXPECTED_BASE64[release.version]}). If upstream's build config "
        f"changed, update the table in the same commit that explains why"
    )


# Whether this build's `sh` can find an applet with PATH pointed at a
# directory it cannot use — the CONFIG_FEATURE_SH_STANDALONE switch, not a
# fact about "BusyBox's ash" in general. It is OFF in BusyBox's own
# `defconfig`, and measured OFF across all five busybox.net prebuilts this
# project fetches: `command -v ls`, the `type` builtin, and invoking
# `/bin/ash` directly (bypassing the `sh` symlink entirely) all report "not
# found" on every row. Ubuntu's system busybox — not a `BUSYBOX_MATRIX` entry,
# but still part of this tier's TESTED CONTRACT via Tier 1's `system` row, and
# pinned there by `test_the_system_busybox_is_the_standalone_counterexample`
# in `tests/busybox/test_applet_contracts.py` — measures the opposite. So this
# table is a claim about the ARTIFACT, not about the version, and a future
# prebuilt that turns the switch on is exactly what this guards.
_EXPECTED_STANDALONE_SHELL = {
    "1.16.1": False,
    "1.21.1": False,
    "1.28.1": False,
    "1.31.0": False,
    "1.35.0": False,
}


@pytest.mark.parametrize("release", BUSYBOX_MATRIX, ids=[r.version for r in BUSYBOX_MATRIX])
def test_standalone_shell_matches_what_the_matrix_records(release):
    """Whether ash needs PATH to find an applet, asserted per row.

    `SANE` is the positive control, sharing the one probe rather than
    spending a second `run_in_rootfs` call on it: with PATH correctly set to
    `/bin`, `ls` must resolve regardless of standalone-shell support. Without
    it, a probe that always failed (a typo in the applet name, a rootfs that
    silently lost its symlinks) would satisfy the no-PATH assertion for
    every row that expects `False` — the same "everything fails, so it looks
    like a pass" shape the base64 test's aggregate-assertion note warns about.

    `PATH=/nonexistent`, not `PATH=`. An entirely empty PATH is not "no
    search path" — POSIX treats it as one empty component, meaning the
    CURRENT DIRECTORY, and `busybox_rootfs` happens to `chroot` with cwd left
    at `/`. Measured: from `/bin` inside this same root, `PATH=; command -v ls`
    resolves (`ls` is a file right there), while `PATH=/nonexistent` does not.
    A probe keyed on the accident of cwd being `/` would flip every row to
    `RESOLVED` — misreported as a build-config change — the moment a later
    caller sets `cwd=` on `run_in_rootfs` or a script does `cd /bin` first.
    `/nonexistent` names a real absence, not a directory-search fallback.
    """
    require_interpreter(release.arch)
    require_userns()

    with busybox_rootfs(release) as root:
        result = run_in_rootfs(
            root,
            "PATH=/bin; command -v ls && echo SANE; "
            "PATH=/nonexistent; command -v ls && echo RESOLVED || echo BLOCKED",
        )

    assert result.returncode == 0, f"the probe did not run: {result.stderr}"
    assert "SANE" in result.stdout, (
        f"ls did not resolve even with PATH=/bin, so the no-PATH result below "
        f"says nothing about standalone-shell support: {result.stdout!r}"
    )
    seen = "RESOLVED" in result.stdout
    assert seen is _EXPECTED_STANDALONE_SHELL[release.version], (
        f"BusyBox {release.version}: standalone-shell resolution "
        f"{'appeared' if seen else 'is absent'} relative to the recorded matrix "
        f"({_EXPECTED_STANDALONE_SHELL[release.version]}). If upstream's build "
        f"config changed, update the table in the same commit that explains why"
    )
