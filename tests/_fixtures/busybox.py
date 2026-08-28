"""Real BusyBox binaries for the matrix tiers, cached and verified.

Artifacts come from https://busybox.net/downloads/binaries/ — the project's
own prebuilds — by way of a mirror of the same five files published as GitHub
release assets (`_MIRROR_BASE`), which a consuming lane asks first so that
busybox.net alone being down does not red a default gate (issue #261).
Upstream publishes NO checksums and NO signatures for them
(source tarballs ship `.sha256`; the prebuilts do not), so verification is
two-layer and the layers do different jobs:

- The BEHAVIOURAL gate — the version banner the artifact prints when it is
  actually executed — is what protects the matrix's meaning. Its failure is a
  real finding about interface drift. It is the banner and ONLY the banner:
  no applet set is enumerated or compared anywhere here. The pin's message
  still asks a human to check the applet set, because a person investigating a
  hash change can and should; the automated gate cannot, and this file will
  not claim otherwise. Applet-level assertions belong to the tiers that drive
  individual applets, not to the artifact fixture.
- The committed SHA-256 is secondary and narrow: CI's `busybox` job re-fetches
  from upstream on every cold cache (`OTTO_BUSYBOX_SOURCE=upstream`), so a pin
  converts per-run trust in busybox.net into one-time trust taken at a
  reviewed moment. A mismatch is INVESTIGATED, not rubber-stamped.

Be exact about what "secondary" does and does not mean, because the two layers
do NOT run in that order. The hash runs FIRST — inside :func:`busybox_binary`,
before the caller ever executes the artifact — and the behavioural gate runs
second, in the test that calls :func:`probe_banner`. Cheap and local goes
first on purpose: it needs no interpreter, so it still speaks on a machine
that cannot run the bytes at all, and it fails in milliseconds instead of
after an emulated start-up.

The consequence is worth stating plainly rather than discovering: for a
BYTE-level substitution the hash fires and the banner never speaks. So the
pin's message does not adjudicate — it hands the meaning question straight to
the behavioural gate ("confirm the banner still reports vX"), which is the
layer whose verdict actually matters. "Secondary" ranks the two by AUTHORITY,
not by execution order. A consequence for anyone demonstrating the behavioural
gate on a substituted file: clear that entry's pin first, or the hash answers
in its place.

Architecture note: only x86 userland is ever fetched. No aarch64 build is
published for any version, and this project's dev VM has ARMv8 cores with no
AArch32 EL0 — 32-bit ARM artifacts fail ENOEXEC there even though
CONFIG_COMPAT=y. Running one x86 artifact per version everywhere (native in
CI, qemu-user-static on the dev VM) also means dev and CI exercise identical
bytes rather than two different builds.
"""

import hashlib
import http.client
import json
import os
import stat
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path

from tests._ambient_env import ambient

_BASE_URL = "https://busybox.net/downloads/binaries"
# The same five files, re-published as assets of one GitHub release so a lane
# that merely CONSUMES them has a source on the runner's own host. Issue #261
# was busybox.net alone being down while every default gate needed it on a cold
# cache; the actions/cache step keeps a WARM lane off the network, and this is
# what keeps a COLD one (new runner image, pin change, fresh clone) off
# busybox.net's critical path. The asset name is the cache filename, so one
# string names the byte in the cache, on the mirror, and in the pin file.
#
# The mirror is a second HOST, not a second TRUST ROOT. `_verify` runs on every
# fetch whatever answered, so a mirror serving a different byte is refused
# exactly as upstream would be — the pin is what is trusted, not the site. The
# bytes were uploaded from an upstream fetch that passed the same pins.
_MIRROR_BASE = "https://github.com/ludachrish3/otto-sh/releases/download/ci-assets-busybox-1"
# Which host each ATTEMPT asks, by policy. One entry per attempt, so the
# fallback rides the retry budget below instead of adding a second budget on
# top of it: the arithmetic in `test_the_fetch_budget_fits_inside_both_timeouts`
# is unchanged, and `test_every_source_order_spends_exactly_the_retry_budget`
# pins the two lengths together. Mirror-first alternates hosts so a single
# dead host costs one timeout and one sleep before the other is asked, and the
# third slot returns to the mirror because it is the preferred source.
#
# `upstream` exists for exactly one caller: the `busybox` CI job, whose reason
# to exist is noticing upstream rebuilding an artifact in place. A mirror in
# front of THAT job would report "still matches" about bytes upstream no
# longer serves, so it forces this policy and `test_busybox_artifacts.py`
# pins that it does — and that no consuming lane does.
_SOURCE_ORDERS: "dict[str, tuple[str, ...]]" = {
    "mirror-first": ("mirror", "upstream", "mirror"),
    "upstream": ("upstream", "upstream", "upstream"),
}
# Sleep before each RETRY, so the tuple's length is the number of retries and
# `len + 1` is the attempt budget. 3 attempts at 5s then 10s, mirroring the
# `web-install` npm-ci loop in the Makefile — same policy, same numbers, so
# there is one retry convention in the repo rather than two.
#
# This became load-bearing when the tier turned into a blocking CI job that is
# also in `report-failure.needs`: without it a single transient blip from
# busybox.net on main reds the merge gate AND auto-files a "CI failed on main"
# issue. Deliberately paired with the decision NOT to cache the artifacts
# between runs (see the job's comment) — no-cache is right for the pin, but it
# removed the slack that made a missing retry tolerable.
_RETRY_BACKOFF_S = (5, 10)
_PINS = Path(__file__).with_name("busybox_pins.json")
# A socket-INACTIVITY timeout, not a transfer budget: `urlopen`'s value is
# applied per blocking operation, so a slow-but-alive download never trips it
# — only genuine silence does. 15s of silence from busybox.net is a dead
# connection, and since the retry above now supplies the patience, one attempt
# no longer has to.
#
# THE THREE NUMBERS ARE COUPLED. Changing any one of them without redoing this
# arithmetic is how the tier loses its own diagnostic, so
# `test_the_fetch_budget_fits_inside_both_timeouts` asserts it:
#
#   DEAD-FROM-THE-START bound per artifact
#       = attempts x _FETCH_TIMEOUT_S + sum(_RETRY_BACKOFF_S)
#       = 3 x 15 + 15 = 60s          vs pyproject `timeout = 180`
#   DEAD-FROM-THE-START bound per session
#       = len(BUSYBOX_MATRIX) x 60 = 300s
#                                    vs `make busybox`'s 900s cap (PYTEST_TIMEOUT)
#
# Those hold for a peer that is silent FROM THE FIRST BYTE — not for any failure,
# and not as a hard ceiling, because the inactivity semantics above cut both
# ways. A peer that talks for T seconds and only then goes quiet costs
# 3 x (T + 15) + 15, which grows without limit in T; a peer that dribbles one
# byte every 14s never trips the timeout at all (measured: 900 KB over 90s
# completes under `timeout=15`). Both of those are caught by the per-test
# SIGALRM and the session cap, not by this arithmetic. What the arithmetic buys
# is narrower and still worth having: the dead-from-the-start case — the common
# one, and the one whose diagnosis we own — reports through our own error with
# the priming instructions, instead of being killed as a bare timeout.
#
# Both must hold with room to spare, and the reason is not tidiness: if a
# stalled fetch outlives the per-test SIGALRM, pytest kills it as a bare
# timeout and the caller gets "Timeout >180.0s" instead of the
# `after 3 attempt(s) … make busybox-cache` message this whole path exists to
# deliver. At 60s the fetch it was (3 x 60 + 15 = 195s) already exceeded the
# 180s budget — the retry had quietly broken the failure mode it was added to
# protect. The session bound is deliberately computed for the WORST case, five
# artifacts stalling one after another in a single worker; `-n auto` only ever
# makes it better.
#
# THE TWO BOUNDS ARE MEASURED AGAINST DIFFERENT CEILINGS and so do not
# compose. The per-artifact one is checked against a TEST's budget, the
# session one against a LANE's, and a caller that needs the whole matrix
# INSIDE ONE TEST falls between them at 300s vs 180s — which is what
# `_run_conformance` does, and what CI run 32888520702 died of. `preflight`
# closes that gap for a source that is DEAD FROM THE START: the first consumer
# in a process proves reachability once and every later one reuses the
# verdict, so that case costs one budget, not one per artifact. A source that
# dies MID-RUN, after an artifact has already landed, is not memoized and
# costs each later consumer its own budget — the same exposure as before the
# consumer-side probe existed, and one the per-test SIGALRM still bounds.
_FETCH_TIMEOUT_S = 15
# Long enough for an emulated start-up on a loaded VM, short enough that a
# wedged binary fails the run instead of hanging it. Not a discriminator: no
# assertion reads it, so widening it can only make the fixture more patient.
_BANNER_TIMEOUT_S = 30


class BusyBoxUnavailableError(RuntimeError):
    """No usable artifact, and the fixture will not pretend otherwise.

    Raised rather than skipped on purpose. A silent skip is how BusyBox
    coverage evaporates: the lane keeps reporting green while testing nothing.
    """


class BusyBoxDriftError(BusyBoxUnavailableError):
    """The source answered, and the bytes are not the ones the pin was taken from.

    A SUBCLASS, so every ``except BusyBoxUnavailableError`` already written
    keeps catching it: to a consumer this is still "no usable artifact", and
    the refusal is the same refusal. What the subclass adds is a distinction
    a MACHINE can act on, which prose could not.

    The two failures mean opposite things and want opposite handling. "Could
    not reach the source" is somebody else's outage: it says nothing about
    any byte, and nightly's drift detector must not redden a run over it
    (2026-08-27, issue #267, when a three-day busybox.net outage was on
    course to file a fresh issue every night). "The source served a different
    byte" is the one signal that detector exists to raise, and it must stop
    everything until a person has looked.

    Before this split both paths raised ``BusyBoxUnavailableError`` with the
    same exit code, and the workflow comment claimed "the log distinguishes
    them" — true for a human reading the message, false for every automated
    consumer, so nothing could branch on it. See
    ``scripts/check_busybox_upstream_drift.py``.
    """


class BusyBoxWithdrawnError(BusyBoxUnavailableError):
    """Every host in the source order answered, and every one of them said no.

    A SIBLING of :class:`BusyBoxDriftError`, not a flavour of the base class,
    because the three conditions want three different responses and only two
    of them were ever distinguishable. "Nothing answered" is an outage and
    excusable. "The bytes changed" is drift. THIS is the third: the artifact
    is no longer published where the pin says it is — 404, not silence.

    Excusing it as an outage is how a detector goes quiet without going red.
    A deterministic refusal is the source working correctly and telling us the
    pinned file is gone, so no amount of waiting fixes it and priming a cache
    cannot either. Under an upstream-only policy it is the strongest drift
    signal there is: a pinned artifact withdrawn rather than merely rewritten.

    Found in review of the 2026-08-27 sweep, which classified a 404 as
    "unreachable", stopped on it, and exited 0 reporting that busybox.net
    "could not be reached". ``BUSYBOX_MATRIX[0]`` is 1.16.1 — the oldest
    artifact published anywhere and the likeliest to be pruned — so that path
    would have checked ZERO pins, green, every night, forever.
    """


@dataclass(frozen=True, slots=True)
class BusyBoxRelease:
    """One matrix entry: a version and the arch build published for it."""

    version: str
    arch: str
    subdir: str
    # Upstream's file naming is per-DIRECTORY, not per-version, and the two
    # shapes are not interchangeable: the multiarch dirs publish one file per
    # arch (`busybox-x86_64`, `busybox-i686`), while the 1.35.0 dirs are
    # already arch-scoped in their own name and publish a plain `busybox`
    # (next to a `busybox_APPLET` per applet). Asking for `busybox-x86_64`
    # under 1.35.0-x86_64-linux-musl is a 404, so the exception is declared at
    # the matrix entry that needs it rather than inferred from the version.
    remote_name: str | None = None

    @property
    def url(self) -> str:
        """Upstream's address for this entry — the bytes the pin was taken from."""
        return f"{_BASE_URL}/{self.subdir}/{self.remote_name or f'busybox-{self.arch}'}"

    @property
    def mirror_url(self) -> str:
        """The release asset holding the same bytes, named as the cache names them."""
        return f"{_MIRROR_BASE}/{self.filename}"

    def url_for(self, source: str) -> str:
        """The address a *source* from `_SOURCE_ORDERS` resolves to for this entry."""
        return self.mirror_url if source == "mirror" else self.url

    @property
    def filename(self) -> str:
        return f"busybox-{self.version}-{self.arch}"


# Chosen at known behaviour transitions, not evenly spaced. 1.16.1 is the
# OLDEST artifact published anywhere — nothing exists for 1.0-1.15, which is
# why the spec calls below-1.16.1 "untested, not unsupported". x86_64 builds
# begin at 1.28.1; older entries use i686, which an x86_64 kernel runs
# natively and qemu-user handles on aarch64.
BUSYBOX_MATRIX = [
    BusyBoxRelease("1.16.1", "i686", "1.16.1"),
    BusyBoxRelease("1.21.1", "i686", "1.21.1"),
    BusyBoxRelease("1.28.1", "x86_64", "1.28.1-defconfig-multiarch"),
    BusyBoxRelease("1.31.0", "x86_64", "1.31.0-defconfig-multiarch-musl"),
    BusyBoxRelease("1.35.0", "x86_64", "1.35.0-x86_64-linux-musl", remote_name="busybox"),
]


# arch -> the qemu-user-static interpreter that runs it. Indexed, not
# defaulted, so an unmapped arch raises KeyError instead of quietly reporting
# "cannot run" forever. Be exact about where that loudness comes from: NOT
# from `can_run`, which short-circuits on an x86_64 machine and returns before
# the lookup — it comes from `_MATRIX_PARAMS` in the tier's test module, which
# indexes this map at IMPORT to build each entry's skip reason, so a matrix
# entry for an unmapped arch fails at collection on every machine.
QEMU_HANDLER = {"x86_64": "qemu-x86_64", "i686": "qemu-i386"}
_BINFMT_ROOT = Path("/proc/sys/fs/binfmt_misc")


def can_run(arch: str, machine: "str | None" = None, binfmt_root: Path = _BINFMT_ROOT) -> bool:
    """Whether THIS arch's artifacts can execute here — per arch, not per machine.

    Two corrections over the obvious version of this check, both of which are
    silently wrong:

    An x86_64 handler does not run an i686 binary. The matrix is mixed (1.16.1
    and 1.21.1 are i686, since upstream published no x86_64 build before
    1.28.1), so a single `qemu-x86_64` probe would call those two runnable on
    a machine that can only run 64-bit — where they do not skip, they die in
    execve and the run reports a missing interpreter it in fact has.

    And a binfmt_misc entry EXISTS while disabled: `echo 0 > .../qemu-x86_64`
    leaves the file in place and writes `disabled` as its first line. Presence
    is therefore not capability; the first line is.

    *machine* and *binfmt_root* are injectable so the mapping can be asserted
    on any host. Without that, every branch below is dead on an x86_64 runner
    — the arch bug this function exists to fix would be unobservable exactly
    where CI runs.
    """
    if (machine or os.uname().machine) == "x86_64":
        # An x86_64 kernel runs both arches natively (32-bit needs
        # CONFIG_IA32_EMULATION, on in every distro kernel otto targets).
        return True
    handler = binfmt_root / QEMU_HANDLER[arch]
    try:
        return handler.read_text().split("\n", 1)[0].strip() == "enabled"
    except OSError:
        return False


def require_interpreter(
    *arches: str, machine: "str | None" = None, binfmt_root: Path = _BINFMT_ROOT
) -> None:
    """Fail early and actionably when x86 artifacts cannot run here.

    Called with no arguments this covers every arch the matrix declares; pass
    arches to narrow it. Intended for a tier's session-scoped, autouse
    fixture (see `tests/busybox/conftest.py`'s `_interpreter`), so the
    missing prerequisite is named ONCE per session rather than surfacing as
    five identical ENOEXECs — and named as a *dependency*, since
    `Exec format error` on a file the reader did not know was x86_64 is a
    twenty-minute detour.

    Raises rather than skips. A skipped BusyBox tier and a passing one are
    the same line in a pytest summary, which is how this tier's coverage
    would quietly evaporate without anyone noticing — the rule every refusal
    in this tree follows.

    Reads binfmt_misc at the moment it is CALLED, inside the running
    session, rather than once earlier at collection. That distinction is
    load-bearing: the answer can change between those two moments — a
    session that started before `qemu-user-static` registered its handlers,
    or one where a handler was disabled partway through a run, needs the
    live answer, not the one collection saw. Fixture setup for a
    session-scoped fixture happens lazily, on first use, after collection
    has already finished — which is what makes calling this from one an
    answer to the live question rather than a restatement of the collected
    one.
    """
    wanted = arches or tuple(dict.fromkeys(release.arch for release in BUSYBOX_MATRIX))
    missing = [
        arch for arch in wanted if not can_run(arch, machine=machine, binfmt_root=binfmt_root)
    ]
    if not missing:
        return
    handlers = ", ".join(QEMU_HANDLER[arch] for arch in missing)
    raise BusyBoxUnavailableError(
        f"BusyBox artifacts are x86 userland and this host is "
        f"{machine or os.uname().machine}. Upstream publishes no aarch64 build for "
        f"any version, so the tier needs qemu-user-static with binfmt registered; "
        f"missing or disabled here: {handlers}.\n"
        f"    sudo apt update && sudo apt install qemu-user-static\n"
        f"The index refresh is not decoration — a stale apt list 404s on the .deb. "
        f"The package registers its handlers at install; confirm with\n"
        f"    cat {binfmt_root}/{QEMU_HANDLER[missing[0]]}\n"
        f"whose first line must read `enabled`. See docs/architecture/subsystems/busybox-bed.md."
    )


def cache_dir() -> Path:
    """Where artifacts live. Overridable so tests never touch the real cache.

    Read through :func:`tests._ambient_env.ambient`, not ``os.environ``, and
    that is load-bearing rather than stylistic: ``tests/conftest.py`` strips
    every ``OTTO_*`` variable it has not been told to spare, so a direct read
    here returned ``None`` for the whole suite and the override silently did
    nothing (the issue #192 failure shape). ``ambient`` raises on an
    undeclared name, which turns that silence into an import-time error.
    """
    override = ambient("OTTO_BUSYBOX_CACHE")
    if override:
        return Path(override)
    return Path.home() / ".cache" / "otto" / "busybox"


# HTTP statuses worth a second look: overload and gateway failures, which a
# retry genuinely clears. Everything else 4xx — above all 404 — is the URL
# being wrong, and three attempts at a wrong URL is thirty seconds spent
# confirming a typo.
_TRANSIENT_HTTP_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


def _is_transient(error: BaseException) -> bool:
    """Whether *error* is the kind a retry can clear. Fail fast on the rest.

    Same rule as the Makefile's `web-install` loop: retry network-class
    failures, fail fast on deterministic ones. The ordering below is not
    stylistic — `HTTPError` subclasses `URLError` which subclasses `OSError`,
    so a status check that came second would never run.

    The final `False` is the important branch. `_fetch` catches bare `OSError`
    because it also writes the `.part` file inside the same `try`; ENOSPC and
    EACCES arrive as OSError and are not transient in any useful sense, so a
    full disk fails on attempt one instead of after fifteen seconds of sleep.
    """
    if isinstance(error, urllib.error.HTTPError):
        return error.code in _TRANSIENT_HTTP_STATUS
    return isinstance(
        error, (urllib.error.URLError, http.client.HTTPException, TimeoutError, ConnectionError)
    )


def _source_order() -> "tuple[str, ...]":
    """The per-attempt host sequence the `OTTO_BUSYBOX_SOURCE` policy names.

    Read through :func:`tests._ambient_env.ambient` for the reason
    :func:`cache_dir` gives: the root conftest strips undeclared ``OTTO_*``
    names, and an unread policy would put the `busybox` job on the mirror
    without a word. An unknown policy is refused by name rather than mapped
    to a default, because a misspelt "upstream" on that job is the same
    silent defeat.
    """
    policy = ambient("OTTO_BUSYBOX_SOURCE") or "mirror-first"
    try:
        return _SOURCE_ORDERS[policy]
    except KeyError:
        raise BusyBoxUnavailableError(
            f"OTTO_BUSYBOX_SOURCE={policy!r} names no artifact-source policy; "
            f"expected one of: {', '.join(_SOURCE_ORDERS)}"
        ) from None


def _load_pins() -> dict:
    return json.loads(_PINS.read_text()) if _PINS.exists() else {}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def probe_banner(path: Path) -> str:
    """First line the binary prints — 'BusyBox v1.21.1 (...) multi-call binary.'"""
    try:
        proc = subprocess.run(
            [str(path)], capture_output=True, text=True, timeout=_BANNER_TIMEOUT_S, check=False
        )
    except OSError as e:
        raise BusyBoxUnavailableError(
            f"{path} could not be executed ({e.strerror}). On aarch64 this needs "
            f"qemu-user-static with binfmt registered; see "
            f"docs/architecture/subsystems/busybox-bed.md"
        ) from e
    out = (proc.stdout or proc.stderr).strip().splitlines()
    return out[0] if out else ""


@dataclass
class _PreflightVerdict:
    """What this process has already learned about reaching the artifact source.

    One mutable object rather than two module-level flags, so recording a
    verdict needs no ``global`` statement — and so a test can hand the module a
    FRESH verdict in one assignment instead of resetting fields it has to know
    the names of.
    """

    #: Set once the process has proven it can obtain what it is missing, so the
    #: probe is paid at most once per worker rather than once per artifact.
    done: bool = False
    #: What a completed probe recorded when it could not reach the source.
    #: Every later call re-raises it rather than spending a second budget
    #: proving the same outage.
    unreachable: "BusyBoxUnavailableError | None" = None


_PREFLIGHT = _PreflightVerdict()


def preflight(
    releases: "list[BusyBoxRelease] | None" = None, *, runnable_only: bool = True
) -> None:
    """Prove ONCE that this process can obtain the artifacts it is missing.

    *runnable_only* (the default, and the tier's form) considers only entries
    this host can execute, because the tier fetches nothing it will not run.
    A consumer that fetches for its own reasons passes ``False``. What the
    filter must never do is RECORD a verdict it did not measure: when it
    empties the probe list, nothing was proven and ``done`` stays unset, or
    the next consumer of a runnable arch would skip the probe on the strength
    of a no-op — measured as five retry budgets across the matrix on an
    aarch64 host with no i686 handler, where one was promised.

    A precondition, not a timeout. `_fetch` already passes `_FETCH_TIMEOUT_S`
    to `urlopen`, and the arithmetic above bounds ONE artifact at 60s against
    pytest's 180s SIGALRM and FIVE at 300s against `make busybox`'s 900s
    session cap (the Makefile's PYTEST_TIMEOUT). Both bounds are right and
    they DO NOT COMPOSE, because they are measured against different
    ceilings: the per-artifact one against a TEST's budget, the session one
    against a LANE's. A caller that needs the
    whole matrix inside a SINGLE test falls between them —
    `tests/unit/test_support_matrix.py`'s `_run_conformance` builds the entire
    hermetic space in one subprocess, so a dead source costs it 5 x 60 = 300s
    against a 180s SIGALRM. It is killed as a bare timeout and reports
    "Timeout (>180.0s)" instead of the `make busybox-cache` instructions this
    path exists to deliver. CI run 32888520702 is exactly that, five times
    over, plus the tier's own two legs.

    One probe is enough because the condition being diagnosed is a property of
    the SOURCE, not of any one artifact: if no host in the source order
    answers for `missing[0]`, the remaining four will not disagree. So the worst case
    collapses from `len(missing) x 60` to a single 60 — inside every ceiling
    above with room left — and the caller gets the real error.

    A WARM CACHE COSTS NO NETWORK AT ALL. `missing` is computed from
    `Path.exists`, and an empty one returns before any socket is opened, which
    is what keeps this safe to call from a lane that may well have everything
    it needs already.

    Called from :func:`busybox_binary`, the consumer, at the moment an
    artifact is about to be fetched — and from `make busybox-preflight`, the
    tier's Makefile prerequisite. NEVER from a collection hook and never at
    session start. `tests/conformance/` is in `testpaths`, so every path-less
    run collects that tree and `make coverage`'s legs then deselect all of it
    (issue #261 was a session-start probe in that lane); and `--collect-only`
    collects everything and executes nothing, yet still fires
    `pytest_collection_finish` (issue #264 was a probe hung off that hook).
    Both placements put busybox.net on the critical path of a run that
    fetches nothing. Firing at the fetch is what makes "nothing runs, nothing
    is reached" true by construction.

    `_verify` is deliberately OUTSIDE the memo: a hash mismatch says the pin
    is stale, not that the source is unreachable, and poisoning the session
    with it would report artifact A's mismatch to a test asking for B.
    """
    if _PREFLIGHT.unreachable is not None:
        raise _PREFLIGHT.unreachable
    if _PREFLIGHT.done:
        return
    matrix = BUSYBOX_MATRIX if releases is None else releases
    absent = [r for r in matrix if not (cache_dir() / r.filename).exists()]
    if not absent:
        # A warm cache is a measured fact about this process: it needs nothing.
        _PREFLIGHT.done = True
        return
    missing = [r for r in absent if can_run(r.arch)] if runnable_only else absent
    if not missing:
        # Emptied by the FILTER, not by the cache: nothing was proven, so
        # nothing is recorded.
        return
    probe = missing[0]
    target = cache_dir() / probe.filename
    try:
        _fetch(probe, target)
    except BusyBoxUnavailableError as e:
        _PREFLIGHT.unreachable = e
        raise
    _verify(probe, target)
    _PREFLIGHT.done = True


DRIFT_VERIFIED = "verified"
DRIFT_MISMATCH = "drifted"
DRIFT_WITHDRAWN = "withdrawn"
DRIFT_UNREACHABLE = "unreachable"


@dataclass(frozen=True, slots=True)
class DriftResult:
    """What upstream answered when asked for one pinned artifact."""

    release: BusyBoxRelease
    kind: str
    detail: str


def reads_upstream_only() -> bool:
    """True when EVERY fetch attempt would ask busybox.net and none would ask the mirror.

    The precondition for any drift verdict. The mirror holds bytes taken FROM
    upstream at a reviewed moment, so a source order containing it would answer
    "still matches" about bytes upstream may no longer serve — a drift check
    run mirror-first is not a weaker check, it is a vacuous one.

    Asks what `_fetch` WOULD do rather than reading the variable, because the
    policy reaches it through `_source_order`: a guard on the env var alone
    would still pass if `tests/conftest.py`'s strip ever ate the opt-in.
    """
    return set(_source_order()) == {"upstream"}


def upstream_drift_report(
    workdir: Path, releases: "list[BusyBoxRelease] | None" = None
) -> "list[DriftResult]":
    """Fetch every pinned artifact from upstream into *workdir* and verify its pin.

    THE WHOLE MATRIX, one :class:`DriftResult` per artifact ASKED — which is
    every entry, unless the first was unreachable. That is the difference
    between this and :func:`preflight`, and the reason it is a separate
    function rather than a flag on that one.

    :func:`preflight` fetches ``missing[0]`` and returns, which is correct for
    the question IT answers: reachability is a property of the SOURCE, so if no
    host answers for one artifact the remaining four will not disagree. Drift
    is a property of each FILE. Nightly's detector was built on that shortcut
    until 2026-08-27 and therefore hash-compared ``busybox-1.16.1-i686`` alone
    while reporting success, leaving a rebuild-in-place of the other four
    invisible for as long as it lasted.

    The source-level shortcut is still taken, for the source-level question
    only: an unreachable FIRST artifact ends the run rather than spending five
    retry budgets to learn the same thing five times.

    *workdir* is a caller-owned throwaway directory and never :func:`cache_dir`.
    A drifted byte must not land where a later lane would find it cached and
    skip its own fetch — and neither should an upstream byte, in a repo whose
    consumers are supposed to prefer the mirror.

    Consumes no memo and sets none: `_PREFLIGHT` records whether THIS process
    can obtain artifacts, and a drift sweep is a different question asked of a
    deliberately different source.
    """
    results: "list[DriftResult]" = []
    for release in releases if releases is not None else BUSYBOX_MATRIX:
        target = workdir / release.filename
        try:
            _fetch(release, target)
            _verify(release, target)
        except BusyBoxDriftError as e:
            # THE SUBCLASSES FIRST. Ordered the other way, every mismatch and
            # every withdrawal would be classified as an outage and the two
            # signals this sweep exists to raise would be lost as noise.
            results.append(DriftResult(release, DRIFT_MISMATCH, str(e)))
        except BusyBoxWithdrawnError as e:
            # No break: the host ANSWERED. One pruned artifact says nothing
            # about the other four, and stopping here would let a withdrawal
            # of BUSYBOX_MATRIX[0] — the oldest and likeliest-pruned entry —
            # hide a rewrite of everything behind it.
            results.append(DriftResult(release, DRIFT_WITHDRAWN, str(e)))
        except BusyBoxUnavailableError as e:
            results.append(DriftResult(release, DRIFT_UNREACHABLE, str(e)))
            if len(results) == 1:
                break
        else:
            results.append(DriftResult(release, DRIFT_VERIFIED, ""))
    return results


def busybox_binary(release: BusyBoxRelease) -> Path:
    """Return a verified, executable artifact for *release*, fetching if needed.

    The source precondition lives HERE, at the consumer, and nowhere earlier
    (issue #264). It used to hang off ``pytest_collection_finish`` in the
    artifact trees' conftests, keyed on items surviving collection — and
    ``--collect-only`` collects items, executes none of them, and fires that
    hook regardless, so every child pytest that enumerated the suite fetched
    from busybox.net on a cold cache and died as INTERNALERROR when it could
    not. Placed at the fetch, the probe fires exactly when an artifact is
    about to be needed, inside a running test: a dead source is a test
    failure carrying the priming instructions, and a run that fetches nothing
    reaches nothing — by construction, not by a guard on the session.

    :func:`preflight` is asked about THIS release, so on a live source the
    probe IS the fetch (no artifact is downloaded on another's behalf), and on
    a source that is dead from the start every later consumer in the process
    re-raises the recorded verdict without spending a second retry budget.
    ``runnable_only=False`` because a consumer has already decided what it
    wants: `make busybox-cache` and the guest-image builder fetch every entry
    on machines that may execute none of them, and filtering here would leave
    those fetches unbounded (see :func:`preflight`).
    """
    target = cache_dir() / release.filename
    if not target.exists():
        preflight([release], runnable_only=False)
    if not target.exists():
        _fetch(release, target)
    _verify(release, target)
    return target


def _fetch(release: BusyBoxRelease, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    # `with_name(... + ".part")`, never `with_suffix`: the filename's dots are
    # version separators, so with_suffix would rewrite `busybox-1.16.1-i686`
    # to `busybox-1.16.part` — eating the patch digit AND the arch, so two
    # arches of one version would race for one temp file under `-n auto`.
    #
    # That analysis was right and stopped one step short of the same hazard's
    # other half: the WHOLE artifact name is still ONE name, and `-n auto` puts
    # several workers on the SAME release at once — five releases, four
    # workers, and a cold cache in CI, where nothing primes the cache before
    # the tier starts. Both workers see `target.exists()` false, both enter
    # here, both write one `.part`; the first to reach `tmp.replace(target)`
    # renames it away, and the second is stranded on the very next line, where
    # `tmp.stat()` raises FileNotFoundError (CI run 31893627225, arm64 leg,
    # 1.21.1). The error path collides the same way with the sign reversed:
    # the cleanup below would delete a download still in flight in another
    # worker, turning one worker's transient blip into another's corruption.
    #
    # So the temp name carries a token unique to THIS CALL. The pid names the
    # owner for a human running `ls` during a hung fetch; the uuid is what
    # makes it unique, and it is per CALL rather than per process because two
    # calls in one process are the same hazard as two processes — and because
    # `OTTO_BUSYBOX_CACHE` may point two machines at one shared directory,
    # where pids are not unique at all.
    #
    # Uniqueness alone is the whole fix; no lock is needed. `replace` is
    # atomic, so two workers fetching the same bytes and both publishing is
    # harmless — the loser's rename is a no-op in effect — and `_verify` hashes
    # what was published afterwards, so a wrong publish is caught rather than
    # trusted. A lock would serialise a cost that does not need serialising.
    tmp = target.with_name(f"{target.name}.{os.getpid()}.{uuid.uuid4().hex}.part")
    order = _source_order()
    asked: "list[str]" = []
    # Sources that answered DETERMINISTICALLY (a 404, a 403): that host has
    # said no, and asking it again is the "three attempts at a typo" the
    # fail-fast rule in `_is_transient` exists to refuse. What changed with the
    # mirror is only the scope of "fail fast": per SOURCE, not per fetch. A
    # host that says no hands the attempt to the next host at once, with no
    # backoff — the refusal was not the network's — and the fetch ends when
    # every remaining slot names a host that has already answered.
    refused: "set[str]" = set()
    try:
        for attempt, source in enumerate(order, start=1):
            url = release.url_for(source)
            asked.append(url)
            try:
                with urllib.request.urlopen(url, timeout=_FETCH_TIMEOUT_S) as resp:
                    tmp.write_bytes(resp.read())
                break
            except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as e:
                # HTTPException is NOT an OSError, and it is the truncation
                # case: a short read against a declared Content-Length raises
                # http.client.IncompleteRead, which without this clause escapes
                # as itself and denies the caller the priming instructions
                # below — the one failure that most needs them.
                # Only a deterministic HTTP status is a SOURCE's answer. A
                # transient one (503) is the network's and the host may
                # recover; a local OSError (ENOSPC, EACCES) is this machine's,
                # and no second host can free the disk — it fails here, on
                # attempt one, as before.
                if isinstance(e, urllib.error.HTTPError) and not _is_transient(e):
                    refused.add(source)
                # The slots still worth spending: hosts that have not said no.
                # Filtered on BOTH paths, so a transient on one host never
                # sends the next attempt back to a host that already refused
                # (found in review: that cost a sleep, a request, and reported
                # the refusal as the cause of a fetch the flake had ended).
                remaining = [s for s in order[attempt:] if s not in refused]
                if remaining and _is_transient(e):
                    time.sleep(_RETRY_BACKOFF_S[attempt - 1])
                    continue
                if remaining and isinstance(e, urllib.error.HTTPError):
                    continue
                if refused >= set(order):
                    # Every distinct host in the order answered, and answered
                    # no. Priming a cache cannot conjure a file nobody
                    # publishes, so this does NOT carry the instructions
                    # below — it carries the pin, which is the thing that has
                    # to change (or be investigated) before anything works.
                    raise BusyBoxWithdrawnError(
                        f"BusyBox {release.version} ({release.arch}) is no longer published at "
                        f"{' then '.join(dict.fromkeys(asked))} after {attempt} attempt(s): {e}. "
                        f"Every source ANSWERED and refused, so this is not an outage and waiting "
                        f"will not fix it. The pin in {_PINS.name} names bytes that no longer "
                        f"exist at that address — INVESTIGATE before repointing it."
                    ) from e
                raise BusyBoxUnavailableError(
                    f"could not fetch BusyBox {release.version} ({release.arch}) from "
                    f"{' then '.join(dict.fromkeys(asked))} after {attempt} attempt(s): {e}. "
                    f"Prime the cache on a networked machine with `make busybox-cache`, "
                    f"or set OTTO_BUSYBOX_CACHE to a populated dir."
                ) from e
        # Mode before publish, then rename. Rename only after a complete
        # download, so an interrupted fetch cannot leave a truncated binary
        # that later runs treat as cached and valid — and chmod before that
        # rename, so it cannot leave a COMPLETE, hash-valid, non-executable one
        # either. `replace` is atomic; a chmod after it is a second window in
        # which the artifact is already cached but not yet runnable, and the
        # next run's failure would be an EACCES that probe_banner reports as a
        # missing qemu-user-static.
        tmp.chmod(tmp.stat().st_mode | stat.S_IXUSR)
        tmp.replace(target)
    finally:
        # Unique names make this cleanup mandatory rather than self-correcting.
        # A shared `.part` was at worst ONE stale file per artifact, truncated
        # by the next attempt; a unique one is a fresh orphan every time. So
        # the unlink moved out of the retry handler — where it also could not
        # see a failing chmod or a Ctrl-C mid-download — into a `finally` that
        # covers every in-process exit. It is a no-op after a successful
        # publish: `replace` has already renamed the file away.
        #
        # What no `finally` covers is a SIGKILL mid-download, which does orphan
        # one file. That is accepted rather than swept, and the reason it does
        # not grow is that a POPULATED cache never calls `_fetch` again:
        # `busybox_binary` fetches only when `target.exists()` is false, so the
        # number of fetches a cache ever sees is the length of the matrix, not
        # the number of runs. The debris is also inert — nothing reads a
        # `.part` — and `rm ~/.cache/otto/busybox/*.part` clears it.
        tmp.unlink(missing_ok=True)


def _verify(release: BusyBoxRelease, target: Path) -> None:
    pins = _load_pins()
    expected = pins.get(release.filename)
    actual = _sha256(target)
    if expected and expected != actual:
        raise BusyBoxDriftError(
            f"BusyBox {release.version} ({release.arch}) hash mismatch.\n"
            f"  pinned: {expected}\n  actual: {actual}\n"
            f"Upstream publishes no signatures, so this pin is trust-on-first-use. "
            f"INVESTIGATE before updating it: confirm the banner still reports "
            f"v{release.version} and the applet set is unchanged, then update "
            f"{_PINS.name} in a reviewed commit."
        )
