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

THIRD, it is the tier that substantiates `Userland`'s BATCHED applet probe.
That probe is a shell CONSTRUCT rather than a flag spelling, so its copy lives
here rather than in Tier 1 (`test_applet_contracts.py`): Tier 1 scopes PATH to
a directory of symlinks it wrote itself, so an applet is missing there because
Tier 1 did not shim it, while this tier's `/bin` came from BusyBox's own
`--install -s` and a name that is not there was compiled out. Three copies of
that spelling exist and all three must agree — `src/otto/host/userland.py`'s
`_applet_probe_command` builds it, `tests/unit/host/test_userland.py` pins the
exact text the product issues, and `_BATCH_PROBE` below is what real binaries
answer. Deliberately NOT shared through an import.
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


# The applet names `Userland` asks about, and what each of the five artifacts
# answered on 2026-08-14. THE FILE'S OWN COPY of both — the list and the
# spelling — because a table imported from the product could not catch the
# product changing.
#
# Every row carries an entry for every name, and the test indexes rather than
# iterates, so an applet added to `PROBED_APPLETS` without a measurement here
# fails with a `KeyError` naming it instead of never being measured. That is
# the only place in this repo where the per-row applet claim is made against
# real binaries; the scripted rows in `tests/unit/host/test_userland.py` model
# a device for the resolver to talk to and prove nothing about hardware.
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

# `echo`, first, is the POSITIVE CONTROL, and it is the reason this batch can
# be trusted at all: it is a shell BUILTIN, so `command -v echo` answers 0
# without depending on PATH, on a symlink, or on
# `CONFIG_FEATURE_SH_STANDALONE`. A zero for it can only mean `command -v`
# itself did not work — which otherwise reaches the caller as a perfectly
# shaped answer saying the device has none of these applets.
_BATCH_CONTROL = "echo"
_BATCH_NAMES = [
    _BATCH_CONTROL,
    "base64",
    "nc",
    "poweroff",
    "scp",
    "shutdown",
    "uudecode",
    "uuencode",
]
_BATCH_PROBE = (
    f'for a in {" ".join(_BATCH_NAMES)}; do command -v "$a" >/dev/null 2>&1 '
    f'&& echo "$a=1" || echo "$a=0"; done'
)


@pytest.mark.parametrize("release", BUSYBOX_MATRIX, ids=[r.version for r in BUSYBOX_MATRIX])
def test_the_batched_applet_probe_answers_every_name_in_one_command(release):
    """`Userland`'s batch, run against the real artifact, name by name.

    ONE COMMAND FOR EVERY NAME is the property being measured, not a detail of
    how the test is written: BusyBox devices are typically slow and
    `Userland.resolve()` fans out over an sshd that REFUSES excess exec
    channels rather than queueing them, so per-applet round trips are the cost
    this shape exists to avoid. Enumeration is not the alternative —
    `busybox --list` exits 1 with `--list: applet not found` on 1.16.1, pinned
    by the test below.

    THE ANSWERS ARE ASSERTED PER NAME AND PER ROW, against a table indexed
    rather than iterated. An aggregate ("at least one absent", "the same set on
    every row") passes when the wrong name is the missing one, which is exactly
    the confusion a refusal keyed on this would suffer.

    `base64` is the cross-check: it is measured a second way here, by
    `test_base64_presence_matches_what_the_matrix_records` above, through a
    single `command -v` rather than through the loop. The two must agree, and
    that is what says the loop reports what the primitive reports.
    """
    require_interpreter(release.arch)
    require_userns()

    with busybox_rootfs(release) as root:
        result = run_in_rootfs(root, _BATCH_PROBE)

    assert result.returncode == 0, f"the batch did not run: {result.stderr}"
    seen = {}
    for line in result.stdout.splitlines():
        name, sep, value = line.strip().partition("=")
        if sep:
            seen[name] = value
    assert seen.get(_BATCH_CONTROL) == "1", (
        f"the {_BATCH_CONTROL!r} control answered {seen.get(_BATCH_CONTROL)!r}, so "
        f"`command -v` did not work on BusyBox {release.version} and every other "
        f"answer below is worthless: {result.stdout!r}"
    )
    assert set(seen) == set(_BATCH_NAMES), (
        f"the batch answered for {sorted(seen)}, not {sorted(_BATCH_NAMES)}"
    )
    expected = _EXPECTED_APPLETS[release.version]
    measured = {n: seen[n] == "1" for n in expected}
    assert measured == expected, (
        f"BusyBox {release.version}: applet presence drifted from the recorded "
        f"matrix. Measured {measured}, recorded {expected}. If upstream's build "
        f"config changed, update the table in the same commit that explains why"
    )


@pytest.mark.parametrize("release", BUSYBOX_MATRIX, ids=[r.version for r in BUSYBOX_MATRIX])
def test_applet_enumeration_is_unavailable_on_the_oldest_row(release):
    """Why the probe detects per name instead of asking the device for a list.

    `busybox --list` is the obvious design and it is measured wrong: 1.16.1
    exits 1 with `--list: applet not found` and enumerates nothing, while the
    other four rows answer. A probe built on it covers four fifths of the
    matrix and REPORTS SUCCESS — the same finding that made
    `tests/_fixtures/busybox_rootfs.py` populate its root with `--install -s`.

    Asserted per row rather than as "1.16.1 is special", so the claim being
    kept alive is "enumeration is not portable across this matrix" and not a
    fact about one file. If a future artifact loses `--list` too, this reddens
    on that row and the recorded reason grows a second instance rather than
    going stale.
    """
    require_interpreter(release.arch)
    require_userns()

    with busybox_rootfs(release) as root:
        result = run_in_rootfs(root, "busybox --list")

    if release.version == "1.16.1":
        assert result.returncode != 0, (
            f"`busybox --list` now works on 1.16.1 (rc={result.returncode}); the "
            f"per-name batch in `Userland._probe_applets` was chosen because it did "
            f"not, so re-read that decision rather than only this table"
        )
        assert "applet not found" in (result.stdout + result.stderr), (
            f"1.16.1 refused `--list` differently: {result.stdout!r} {result.stderr!r}"
        )
    else:
        assert result.returncode == 0, (
            f"`busybox --list` failed on {release.version} (rc={result.returncode}); "
            f"the recorded finding is that 1.16.1 alone lacks it"
        )
        assert "base64" in result.stdout, (
            f"`--list` on {release.version} enumerated nothing recognisable: "
            f"{result.stdout[:200]!r}"
        )
