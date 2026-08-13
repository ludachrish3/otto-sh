"""Resolution, caching, override precedence and log discipline for Userland.

Every probe SPELLING WITH A REAL ARGUMENT-PARSING QUESTION -- one BusyBox
version could plausibly answer differently than another -- traces to a
measurement in ``tests/busybox/test_applet_contracts.py`` (Tier 1, real
BusyBox binaries), never to a guess about what a build does. The constants
below are the test's own copy of those spellings: the product is compared
against them, so a drift to a spelling Tier 1 measured as REJECTED (e.g.
``stat --format=%s``) reddens here rather than on a device. Not every probe
qualifies -- see the block below for which ones do and why the rest do not.
"""

import ast
import asyncio
import json
import logging
import math
from dataclasses import dataclass, field, fields
from pathlib import Path

import pytest

from otto.host import userland as userland_module
from otto.host.options import UserlandOptions
from otto.host.userland import Userland
from otto.logger.mode import LogMode
from otto.models.options import UserlandOptionsSpec
from otto.result import CommandResult, Status

# ── the probe spellings, as Tier 1 measured them ────────────────────────────
#
# THREE COPIES of these spellings exist and all three must agree: the product
# issues them inline in `src/otto/host/userland.py`, Tier 1 measures them
# against real binaries in `tests/busybox/test_applet_contracts.py`, and this
# block is the copy the product is compared against. They are not coupled by
# an import on purpose — a product that imported the test's list could not be
# caught drifting by it — so each of the three names the other two.
#
# That is true of `_P_TIMEOUT*`, `_P_BASE64*`, `_P_STAT`, and `_P_WC` --
# genuinely contested spellings a BusyBox version could reject. `_P_SUDO`,
# `_P_SU`, and `_P_MD5SUM` are NOT: a bare `command -v X` presence check and
# a single-spelling checksum probe have no argument-parsing variant for a
# BusyBox version to disagree about, so Tier 1 carries no copy of them and
# only TWO copies of theirs exist (this file's and the product's). Tier 1
# does substantiate `_P_MD5SUM`'s capability a different way -- see
# `src/otto/host/userland.py`'s module docstring.
#
# `command -v X` rather than `which X`: `which` is itself an optional applet.
_P_SUDO = "command -v sudo"
_P_SU = "command -v su"
# `-t SECS PROG` up to 1.28.1, bare `SECS PROG` from 1.31.0, mutually
# exclusive on every artifact measured — which is what lets the probe try one
# then the other and converge.
_P_TIMEOUT = "command -v timeout"
_P_TIMEOUT_COREUTILS = "timeout 1 true"
_P_TIMEOUT_DASH_T = "timeout -t 1 true"
# `--decode` was rejected by every BusyBox artifact measured; `-d` works from
# 1.21.1, and the applet is absent entirely on 1.16.1.
_P_BASE64_SHORT = "echo aGk= | base64 -d"
_P_BASE64_LONG = "echo aGk= | base64 --decode"
# `stat -c %s` works on every artifact measured; `stat --format=%s` is
# rejected by every one of them, so the long spelling must never appear here.
# `wc -c <` (redirect, not operand: the operand form appends the filename) is
# the universal fallback.
_P_STAT = "stat -c %s /dev/null"
_P_WC = "wc -c < /dev/null"
# One spelling only -- unlike stat_size's stat/wc pair there is no second,
# widely-available checksum tool this module falls back to, so the probe
# either answers or it does not. `< /dev/null` is a redirect, matching
# `_P_WC` just above (not `_P_STAT`, which passes `/dev/null` as a plain
# positional argument and reads no stdin at all) -- the other REDIRECT-based
# probe in this set, picked for consistency, not because md5sum needs it.
_P_MD5SUM = "md5sum < /dev/null"
# Behaviour, not a name: BusyBox ships both ash and hush and `sh` may be
# either, so the presence of $BASH_VERSION is the only thing that separates
# the frame otto should use.
_P_BASH = 'test -n "$BASH_VERSION"'

# ``version`` is the one field with no probe, by the plan's binding constraint:
# a declared version is documentation and must never gate behaviour. Every
# OTHER field of UserlandOptions is derived, so adding an eighth field without
# teaching Userland to resolve it reddens
# ``test_every_option_field_is_probed_or_documented_as_never_probed`` instead
# of quietly going unresolved.
_NEVER_PROBED = {"version"}


def _probeable_field_names() -> list[str]:
    return sorted(f.name for f in fields(UserlandOptions) if f.name not in _NEVER_PROBED)


def _answers(userland) -> dict[str, str]:
    """Every capability the host is USING, read through the properties.

    Deliberately not ``as_lab_json()``, which since the paste-safety fix
    returns only what was actually settled. The two differ exactly when
    something could not be asked, which is the state most of the guards below
    are about — so a test that wants "what will this host do" has to ask the
    properties, and a test that wants "what may a user pin" has to ask
    ``as_lab_json()``. Conflating them is what let an unmeasured value reach
    the paste line in the first place.
    """
    return {n: getattr(userland, n) for n in _probeable_field_names()}


def _ok(value: str = "") -> CommandResult:
    return CommandResult(Status.Success, value=value, retcode=0)


# `shell_dialect` is the one resolved field the lab-data spec types as a bare
# `str | None` rather than a Literal, so UserlandOptionsSpec validates ANY
# string for it and the paste-ready round trip cannot police it. Its
# vocabulary is pinned here instead, or the field rides along on the other
# five fields' Literals and a probe answering "posix" ships unnoticed.
_MEASURED_DIALECTS = {"ash", "bash"}


def _assert_debug_only(caplog) -> None:
    """Every record this module emitted must be DEBUG, and there must be some.

    The user's requirement is that probes stay "silent on the console and the
    log file". ``LogMode`` cannot deliver that for these records — it governs
    command I/O, not otto's own logging — so the LEVEL is the whole mechanism,
    and otto's default is ``--log-level INFO``. Promoting one line to
    ``logger.info`` puts five lines per host in front of every user, and
    ``caplog.at_level(DEBUG)`` captures INFO and WARNING happily, so nothing
    notices unless the levels are read.
    """
    ours = [r for r in caplog.records if r.name == "otto.host.userland"]
    assert ours, "no records captured — a level assertion over nothing passes vacuously"
    assert {r.levelno for r in ours} == {logging.DEBUG}, (
        f"userland records escaped DEBUG: {sorted({logging.getLevelName(r.levelno) for r in ours})}"
    )


# What a host that can answer NOTHING must resolve to. Checked against the
# derived field list inside the test, so an eighth field cannot arrive without
# someone deciding what it degrades to.
#
# "Answers nothing" here means ANSWERS NO — the device is reachable and every
# probe exits non-zero. That is a measurement, and these are its conclusions.
# The case where the question could not be ASKED is a different map entirely;
# see _UNASKABLE below and do not merge the two.
_FULLY_DEGRADED = {
    "base64_flag": "absent",
    "checksum": "absent",
    "elevation": "none",
    "shell_dialect": "ash",
    "stat_size": "absent",
    "timeout_style": "absent",
}

# What each capability answers when the probe could not be ASKED — the
# transport raised, or the resolution budget ran out before the command was
# sent. Not a measurement, so it is never cached, and every value here is
# WHAT OTTO DID BEFORE IT ASKED ANYTHING:
#
#   elevation      pre-branch `_elevate` hard-coded sudo, and still does for a
#                  host with no resolver at all.
#   timeout_style  "absent" means no `timeout` prefix, which is exactly the
#                  listener nc spawned before this layer existed. Guessing a
#                  spelling builds `timeout 3600 nc -l` on a `-t` host, where
#                  the applet fails to exec `3600` and nothing listens.
#   stat_size      nc has always sized remote files with `stat -c %s`
#                  (three call sites in transfer/nc.py), so "stat" is the
#                  status quo and "absent" would be a capability regression.
#   base64_flag    nothing consumes it yet, so the conservative answer wins:
#                  claiming a decode flag works builds a command that fails.
#   checksum       "absent" degrades to the byte-size comparison that always
#                  works, the OPPOSITE reasoning from stat_size: there is no
#                  status quo to preserve (nothing consumed a checksum before
#                  this capability existed), so assuming "md5sum" on a host
#                  that could not even answer the probe would build a command
#                  likely to 127 and report a good transfer as a bad one.
#   shell_dialect  otto's unix path has always assumed bash, and nothing
#                  routes this PROBE's value to frame selection yet — an
#                  `ash` CommandFrame IS registered now
#                  (build_command_frame("ash") succeeds), but recording
#                  "ash" for an unasked host would still claim a measurement
#                  nobody took, not merely name a frame that can't be built.
_UNASKABLE = {
    "base64_flag": "absent",
    "checksum": "absent",
    "elevation": "sudo",
    "shell_dialect": "bash",
    "stat_size": "stat",
    "timeout_style": "absent",
}

# Every probe otto issues, in order, on a host that answers only `command -v
# timeout` — the one script that reaches all eleven arms, because a `timeout`
# that is absent short-circuits both spelling probes.
_EVERY_PROBE_IN_ORDER = [
    _P_SUDO,
    _P_SU,
    _P_TIMEOUT,
    _P_TIMEOUT_COREUTILS,
    _P_TIMEOUT_DASH_T,
    _P_BASE64_SHORT,
    _P_BASE64_LONG,
    _P_STAT,
    _P_WC,
    _P_MD5SUM,
    _P_BASH,
]


class _FakeClock:
    """A monotonic clock the harness advances, so no guard has to wait.

    The budget is measured in seconds, and the only honest way to test a
    seconds-scale bound in a unit lane is to control the clock: sleeping for
    real would make the guard a wall-clock discriminator, which this branch
    forbids, and shrinking the product's constants to milliseconds would test
    numbers nothing ships with.
    """

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _Runner:
    """Records every command, its LogMode and its kwargs; answers a scripted device.

    Matching is EXACT, not substring: ``command -v su`` is a substring of
    ``command -v sudo``, so a substring table would answer the sudo probe from
    the su entry and make a su-only device look like a sudo device.
    """

    def __init__(
        self,
        works=(),
        *,
        fail_retcode: int = 1,
        raises: BaseException | None = None,
        unreachable=(),
        clock: _FakeClock | None = None,
    ):
        self.calls: list[str] = []
        self.modes: list[LogMode] = []
        self.kwargs: list[dict] = []
        # Mutable on purpose: a blip is something that STOPS, and the recovery
        # guards clear this between two resolve() calls to model that.
        self.unreachable = set(unreachable)
        # Public and mutable for the same reason as `unreachable`: a host that
        # has RECOVERED answers promptly, so a guard that models recovery
        # detaches the clock rather than keeping every later probe burning its
        # full grant.
        self.clock = clock
        self._works = set(works)
        self._fail_retcode = fail_retcode
        self._raises = raises

    async def __call__(self, cmd, *, log=LogMode.NORMAL, **kw):
        self.calls.append(cmd)
        self.modes.append(log)
        self.kwargs.append(kw)
        # A real exec suspends on the network. Without a suspension point here
        # no two resolve() coroutines can ever interleave, and
        # test_concurrent_resolves_still_probe_once could not fail however the
        # product were written.
        await asyncio.sleep(0)
        if self.clock is not None:
            # A host that swallows each probe whole: the command burns exactly
            # the timeout it was granted.
            self.clock.now += kw.get("timeout", 0.0)
        if self._raises is not None:
            raise self._raises
        if cmd in self.unreachable:
            # The transport failing, NOT the device answering. The distinction
            # is the whole subject of the guards below: this must never be
            # recorded as a "no".
            raise OSError(f"channel refused before {cmd!r} was delivered")
        if cmd in self._works:
            return _ok()
        return CommandResult(Status.Error, value="", retcode=self._fail_retcode)


@pytest.mark.asyncio
@pytest.mark.parametrize("field_name", _probeable_field_names())
async def test_a_declared_answer_is_never_probed(field_name):
    """A pin exists to SKIP the round trip, so probing anyway defeats it.

    Asserting on the calls rather than on the resolved value: returning the
    right answer while still paying for the probe would satisfy a value-only
    assertion and silently keep the cost the pin was bought to avoid.

    Parametrized over the fields derived from ``UserlandOptions`` rather than
    over a hand-picked one, because the defect this guards against arrives one
    field at a time. The comparison is a PROPER SUBSET of the undeclared run's
    commands, which fails both ways: equal means the declaration bought
    nothing, and a command outside the baseline means the declaration changed
    what gets probed instead of removing it.
    """
    baseline = _Runner()
    await Userland(UserlandOptions(), baseline).resolve()

    runner = _Runner()
    userland = Userland(UserlandOptions(**{field_name: "DECLARED"}), runner)
    await userland.resolve()

    assert userland.as_lab_json()[field_name] == "DECLARED"
    assert set(runner.calls) < set(baseline.calls), (
        f"declaring {field_name} removed no probe: declared run issued "
        f"{runner.calls}, undeclared run issued {baseline.calls}"
    )


@pytest.mark.asyncio
async def test_probe_traffic_is_redacted_from_every_sink():
    """Probes are otto's machinery; users must never see them.

    LogMode.NEVER, not QUIET: QUIET still writes to verbose.log, and five
    probes per host would bury the command the user actually ran.
    """
    runner = _Runner({_P_SUDO})
    await Userland(UserlandOptions(), runner).resolve()

    assert runner.modes, "no probes ran — this guard would pass vacuously"
    assert set(runner.modes) == {LogMode.NEVER}, (
        f"probe traffic escaped redaction: {set(runner.modes)}"
    )


@pytest.mark.asyncio
async def test_resolution_happens_once():
    """Cached for the host's lifetime — a second resolve() must cost nothing."""
    runner = _Runner({_P_SUDO})
    userland = Userland(UserlandOptions(), runner)

    await userland.resolve()
    first = len(runner.calls)
    await userland.resolve()

    # Without this, a resolve() that probed NOTHING would satisfy the guard
    # below with 0 == 0 and read as proof of caching.
    assert first > 0, "the first resolve() probed nothing — 0 == 0 proves no caching"
    assert len(runner.calls) == first


@pytest.mark.asyncio
@pytest.mark.parametrize("field_name", _probeable_field_names())
async def test_reading_a_capability_before_resolve_fails_loudly(field_name):
    """Silently probing on first read would hide a round trip inside a property.

    Every property, derived: a sixth one added later would otherwise be the
    one that answers ``None`` quietly.
    """
    userland = Userland(UserlandOptions(), _Runner())

    with pytest.raises(RuntimeError, match="before resolve"):
        getattr(userland, field_name)


@pytest.mark.asyncio
async def test_the_debug_summary_is_pasteable_into_lab_json(caplog):
    """The debug line exists so a user can pin what was probed.

    Two things have to hold for it to serve that purpose, and only one of them
    is about JSON: the payload must parse, AND every value in it must be a
    legal member of the lab-data boundary spec. A probe that answered
    ``"gnu"`` or ``"sudo "`` would still round-trip through ``json`` and be
    rejected by ``lab.json`` at the point the user pasted it — which is the
    only place it would ever be discovered.
    """
    runner = _Runner({_P_SUDO, _P_TIMEOUT, _P_TIMEOUT_COREUTILS})
    userland = Userland(UserlandOptions(), runner)

    with caplog.at_level(logging.DEBUG, logger="otto.host.userland"):
        await userland.resolve()

    summary = [r.getMessage() for r in caplog.records if "pin these" in r.getMessage()]
    assert summary, "no pasteable summary was emitted"
    payload = json.loads(summary[0].split('-- "userland_options": ', 1)[1])
    assert payload == userland.as_lab_json()
    assert payload["elevation"] == "sudo"
    assert payload["timeout_style"] == "coreutils"
    UserlandOptionsSpec(**payload)
    # The spec types shell_dialect as a free `str`, so the line above accepts
    # anything at all for it — see _MEASURED_DIALECTS.
    assert payload["shell_dialect"] in _MEASURED_DIALECTS
    _assert_debug_only(caplog)


@pytest.mark.asyncio
async def test_every_resolved_field_gets_its_own_debug_line(caplog):
    """One debug line per probe result, which is the stated requirement.

    The summary alone would not tell a user WHICH answers cost a round trip,
    so each line carries its source and the declared/probed split is asserted
    both ways: a line that always said "probed" would pass a one-sided check.
    """
    runner = _Runner({_P_SUDO})
    userland = Userland(UserlandOptions(elevation="su"), runner)

    with caplog.at_level(logging.DEBUG, logger="otto.host.userland"):
        await userland.resolve()

    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("userland: ")]
    for name, value in userland.as_lab_json().items():
        source = "declared" if name == "elevation" else "probed"
        assert f"userland: {name} = {value} ({source})" in lines, (
            f"no {source} debug line for {name}; got {lines}"
        )
    _assert_debug_only(caplog)


def _logger_call_sites() -> list[tuple[int, str, object]]:
    """Every ``_logger.<level>(template, ...)`` in the product, read from source.

    Enumerated rather than listed. Three hand-placed level assertions covered
    three call sites, and the very commit that added them introduced a fourth
    that none of them reached — so the pin has to grow with the module by
    construction, not by someone remembering.

    TWO SPELLINGS THIS DOES NOT SEE, both verified as the only escapes found:
    binding the logger to another name first (``_log = _logger`` then
    ``_log.info(...)``), and re-fetching it inline
    (``logging.getLogger(__name__).info(...)``). Either would have to sit on a
    branch no test reaches to matter at all — on a reached branch the runtime
    half of the pin still catches it, because that guard reads the LEVEL of
    every record the module emits and does not care how the logger was named.
    Recorded rather than chased: matching those forms means tracking aliases,
    which is a second thing that can drift, and this parser's value is that it
    cannot. ``.log(logging.INFO, ...)``, f-string and ``%``-formatted
    templates, nested functions, comprehensions, module scope and duplicate
    templates are all seen.
    """
    tree = ast.parse(Path(userland_module.__file__).read_text())
    sites = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "_logger"
        ):
            first = node.args[0] if node.args else None
            template = first.value if isinstance(first, ast.Constant) else None
            sites.append((node.lineno, node.func.attr, template))
    return sites


def test_every_logger_call_in_the_module_is_debug():
    """No call site may be written at a level users see, exercised or not.

    The behavioural guards can only judge records that were emitted; this one
    judges the source, so a new ``_logger.info`` on a branch no test reaches
    is still caught.
    """
    sites = _logger_call_sites()
    assert sites, "found no _logger calls — the scan is broken, not the module"

    offenders = [(line, attr) for line, attr, _ in sites if attr != "debug"]

    assert not offenders, (
        f"{userland_module.__file__} logs above DEBUG at {offenders}; at otto's "
        f"default --log-level INFO that reaches the console and both log files"
    )


@pytest.mark.asyncio
async def test_every_logger_call_site_is_exercised_at_debug(caplog, monkeypatch):
    """Every call site in the source is reached by this test, and all are DEBUG.

    Set equality against the templates parsed out of the module is what makes
    the level coverage structural: a fifth call site is either exercised by
    one of the scenarios below — and so level-checked — or it reddens here as
    a template nothing emitted. Adding one more hand-placed assertion to
    whichever test happens to hit the new line is the mechanism that already
    failed once.
    """
    templates = {t for _, _, t in _logger_call_sites()}
    assert None not in templates, "a _logger call has a non-literal template; teach the scan"

    with caplog.at_level(logging.DEBUG, logger="otto.host.userland"):
        # A host that answers: the per-field lines and the summary.
        await Userland(UserlandOptions(), _Runner({_P_SUDO})).resolve()
        # A host whose transport is broken: the probe-failed line.
        await Userland(UserlandOptions(), _Runner(raises=OSError("channel closed"))).resolve()
        # A host that swallows probes whole: the budget-spent line.
        clock = _FakeClock()
        monkeypatch.setattr(userland_module, "_monotonic", clock)
        await Userland(UserlandOptions(), _Runner({_P_TIMEOUT}, clock=clock)).resolve()

    emitted = {r.msg for r in caplog.records if r.name == "otto.host.userland"}
    assert emitted == templates, (
        f"call sites never exercised: {sorted(templates - emitted)}; "
        f"records with no call site: {sorted(emitted - templates)}"
    )
    _assert_debug_only(caplog)


# ── what the device actually answers ────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_probe_spellings_and_their_order_are_the_measured_ones():
    """The commands themselves are the contract, so they are pinned literally.

    Tier 1 measured that ``stat --format=%s`` and ``base64 --decode`` are
    rejected by every BusyBox artifact and that ``wc -c`` needs a redirect,
    not an operand. A probe that drifts to one of those spellings still
    returns a plausible answer on a GNU host and answers WRONG on the hosts
    this feature exists for, so the drift has to redden here — off-device —
    rather than in a transfer.

    The order is part of the pin: the elevation and stat arms are
    PREFERENCES, so reversing them changes the answer on a host that has both.
    """
    runner = _Runner({_P_TIMEOUT})

    await Userland(UserlandOptions(), runner).resolve()

    assert runner.calls == _EVERY_PROBE_IN_ORDER


@pytest.mark.asyncio
async def test_every_option_field_is_probed_or_documented_as_never_probed():
    """A field added to UserlandOptions must get a probe or an exemption.

    Derived from the dataclass rather than listed, because the failure this
    guards against is silent: field seven simply never resolves, and every
    hand-written six-field assertion in this module stays green.
    """
    userland = Userland(UserlandOptions(), _Runner())
    await userland.resolve()

    assert set(userland.as_lab_json()) == set(_probeable_field_names()), (
        "Userland resolves a different set of fields than UserlandOptions declares; "
        "add a probe, or add the field to _NEVER_PROBED with a reason"
    )


@pytest.mark.asyncio
async def test_a_host_answering_nothing_degrades_rather_than_raising():
    """A minimal host must stay usable: absent is an answer, not an error."""
    assert set(_FULLY_DEGRADED) == set(_probeable_field_names()), (
        "a field has no documented degraded answer — decide what a host that "
        "cannot answer it should get"
    )
    assert set(_UNASKABLE) == set(_probeable_field_names()), (
        "a field has no documented no-information answer — decide what otto "
        "should assume when the question cannot be asked at all"
    )
    userland = Userland(UserlandOptions(), _Runner())

    await userland.resolve()

    assert userland.as_lab_json() == _FULLY_DEGRADED


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_retcode", [1, 127])
async def test_only_a_zero_exit_counts_as_a_yes(fail_retcode):
    """Probe on rc == 0, never on a particular failure code.

    Measured in Tier 1: the SAME missing-applet failure exits 1 on BusyBox
    1.16.1 and 127 on 1.21.1, and a rejected FLAG on a present applet exits 1
    too. Any probe keyed to a specific code (``!= 127``, ``!= 1``) therefore
    reads one of those two as success and pins the host to a capability it
    does not have.
    """
    userland = Userland(UserlandOptions(), _Runner(fail_retcode=fail_retcode))

    await userland.resolve()

    assert userland.as_lab_json() == _FULLY_DEGRADED


@dataclass(frozen=True)
class _Device:
    """A scripted device: the probes it answers 0 to, and what must resolve."""

    name: str
    works: frozenset = field(default_factory=frozenset)
    expected: dict = field(default_factory=dict)


_DEVICES = [
    # BusyBox 1.16.1: `base64` applet absent entirely, `timeout` still `-t`.
    # `md5sum` is measured present even here (this task's brief), unlike
    # `base64` on the same build.
    _Device(
        "busybox-1.16.1",
        frozenset({_P_SU, _P_TIMEOUT, _P_TIMEOUT_DASH_T, _P_STAT, _P_WC, _P_MD5SUM}),
        {
            "elevation": "su",
            "timeout_style": "dash-t",
            "base64_flag": "absent",
            "stat_size": "stat",
            "checksum": "md5sum",
            "shell_dialect": "ash",
        },
    ),
    # BusyBox 1.28.1: the last measured build on the `-t` side, with base64.
    _Device(
        "busybox-1.28.1",
        frozenset(
            {_P_SU, _P_TIMEOUT, _P_TIMEOUT_DASH_T, _P_BASE64_SHORT, _P_STAT, _P_WC, _P_MD5SUM}
        ),
        {
            "elevation": "su",
            "timeout_style": "dash-t",
            "base64_flag": "-d",
            "stat_size": "stat",
            "checksum": "md5sum",
            "shell_dialect": "ash",
        },
    ),
    # BusyBox 1.35.0: coreutils-style `timeout`, still no `--decode`.
    _Device(
        "busybox-1.35.0",
        frozenset(
            {_P_SU, _P_TIMEOUT, _P_TIMEOUT_COREUTILS, _P_BASE64_SHORT, _P_STAT, _P_WC, _P_MD5SUM}
        ),
        {
            "elevation": "su",
            "timeout_style": "coreutils",
            "base64_flag": "-d",
            "stat_size": "stat",
            "checksum": "md5sum",
            "shell_dialect": "ash",
        },
    ),
    # `stat` compiled out — the only script that produces the `wc` answer, and
    # the reason the fallback arm is not dead code.
    _Device(
        "busybox-stat-compiled-out",
        frozenset({_P_SU, _P_TIMEOUT, _P_TIMEOUT_DASH_T, _P_BASE64_SHORT, _P_WC, _P_MD5SUM}),
        {
            "elevation": "su",
            "timeout_style": "dash-t",
            "base64_flag": "-d",
            "stat_size": "wc",
            "checksum": "md5sum",
            "shell_dialect": "ash",
        },
    ),
    # A GNU/coreutils host with bash: accepts BOTH decode spellings and has
    # both elevation paths, so it is where the preference order shows.
    _Device(
        "gnu-coreutils-bash",
        frozenset(
            {
                _P_SUDO,
                _P_SU,
                _P_TIMEOUT,
                _P_TIMEOUT_COREUTILS,
                _P_BASE64_SHORT,
                _P_BASE64_LONG,
                _P_STAT,
                _P_WC,
                _P_MD5SUM,
                _P_BASH,
            }
        ),
        {
            "elevation": "sudo",
            "timeout_style": "coreutils",
            "base64_flag": "-d",
            "stat_size": "stat",
            "checksum": "md5sum",
            "shell_dialect": "bash",
        },
    ),
    # `timeout` present but speaking neither spelling. Not measured on any
    # artifact — it is the shape a future build could take, and the arm that
    # decides whether "present" is allowed to imply "usable". It must not:
    # `timeout_style` feeds a command prefix, and a wrong prefix turns nc's
    # backstop into an outage.
    _Device(
        "timeout-present-but-unusable",
        frozenset({_P_SU, _P_TIMEOUT, _P_BASE64_SHORT, _P_STAT, _P_WC, _P_MD5SUM}),
        {
            "elevation": "su",
            "timeout_style": "absent",
            "base64_flag": "-d",
            "stat_size": "stat",
            "checksum": "md5sum",
            "shell_dialect": "ash",
        },
    ),
    # SYNTHETIC, and deliberately labelled so. No measured device behaves this
    # way: every BusyBox artifact rejects `--decode`, and GNU accepts `-d`, so
    # the long-spelling arm can never win a probe on anything measured. It is
    # kept because `--decode` stays a legal DECLARED value and costs one probe
    # only on a host with no `-d`; this row exists so the arm is exercised
    # rather than silently dead. If the arm is ever removed, delete this row —
    # do not weaken it.
    _Device(
        "synthetic-long-decode-only",
        frozenset(
            {_P_SU, _P_TIMEOUT, _P_TIMEOUT_COREUTILS, _P_BASE64_LONG, _P_STAT, _P_WC, _P_MD5SUM}
        ),
        {
            "elevation": "su",
            "timeout_style": "coreutils",
            "base64_flag": "--decode",
            "stat_size": "stat",
            "checksum": "md5sum",
            "shell_dialect": "ash",
        },
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("device", _DEVICES, ids=lambda d: d.name)
async def test_a_scripted_device_resolves_to_its_measured_answers(device):
    """End to end, per device, with the whole answer asserted at once.

    Whole-map equality rather than a field or two: a probe that answered the
    right thing for `timeout` and the wrong thing for `stat` would pass any
    per-field selection, and a field added later is unanswered by every row
    until its author fills one in.

    Every row is then fed back through the lab-data boundary spec, which is
    the actual definition of "paste-ready": the probe's vocabulary and the
    spec's Literal members are written in different files by different tasks,
    and nothing else makes them agree.
    """
    assert set(device.expected) == set(_probeable_field_names()), (
        f"device {device.name} has no expected answer for every field"
    )
    userland = Userland(UserlandOptions(), _Runner(device.works))

    await userland.resolve()

    assert userland.as_lab_json() == device.expected
    # Read through the PROPERTIES too, derived from the field list: a property
    # wired to the wrong key returns a real string and is invisible to a
    # map-only assertion.
    assert {n: getattr(userland, n) for n in _probeable_field_names()} == device.expected
    UserlandOptionsSpec(**userland.as_lab_json())
    # Independent of the row's own expectation, and of the spec: this is the
    # one field the spec types as a free string, so a probe answering "posix"
    # would satisfy both the row (once someone updated it) and the validator.
    assert userland.shell_dialect in _MEASURED_DIALECTS, (
        f"{userland.shell_dialect!r} is not a dialect anything measured"
    )


# ── the bounds on resolution ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_every_probe_is_sent_with_a_bound():
    """A probe with no timeout hangs the command the caller actually wanted.

    THIS test reads only the kwarg's PRESENCE and its upper bound, never the
    constant's exact VALUE — deleting the kwarg is invisible to every other
    guard in this module, since the scripted runner answers instantly either
    way. Asserting presence is not a wall-clock discriminator: no clock is
    consulted. That does not mean the constant is free to widen: it is one
    half of the ratio ceil(_RESOLVE_BUDGET_S / _PROBE_TIMEOUT_S), and
    `_RESOLVE_BUDGET_S`'s own comment in userland.py measures what widening
    that ratio breaks (31.0 already reds
    test_resolution_stops_once_the_whole_budget_is_spent[as-shipped]) — this
    test's silence on the value is not evidence that no test cares about it.
    """
    runner = _Runner({_P_TIMEOUT})

    await Userland(UserlandOptions(), runner).resolve()

    assert runner.kwargs, "no probes ran — this guard would pass vacuously"
    unbounded = [
        c for c, kw in zip(runner.calls, runner.kwargs, strict=True) if "timeout" not in kw
    ]
    assert not unbounded, f"probes sent with no timeout: {unbounded}"
    assert all(0 < kw["timeout"] <= userland_module._PROBE_TIMEOUT_S for kw in runner.kwargs), (
        f"a probe was granted a bound outside (0, _PROBE_TIMEOUT_S]: "
        f"{[kw['timeout'] for kw in runner.kwargs]}"
    )


def _run_call_sites() -> tuple[list[int], list[int]]:
    """Line numbers of every ``self._run(...)`` call, and of those inside ``asyncio.wait_for``.

    Read from source because the alternative is unusable. The behavioural
    version of this guard needs a callee that never returns, and a callee that
    never returns fails an UNFIXED product by hanging until the suite's alarm —
    a red whose kind says "deadlock somewhere" and whose duration is the whole
    per-test budget. This branch forbids exactly that. The companion
    behavioural guard below reaches the same property deterministically, with
    no clock at all, by shrinking the grant to zero.
    """
    tree = ast.parse(Path(userland_module.__file__).read_text())

    def _is_self_run(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_run"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "self"
        )

    def _is_wait_for(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "wait_for"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "asyncio"
        )

    every = [n.lineno for n in ast.walk(tree) if _is_self_run(n)]
    wrapped = [
        inner.lineno
        for outer in ast.walk(tree)
        if _is_wait_for(outer)
        for inner in ast.walk(outer)
        if _is_self_run(inner)
    ]
    return every, wrapped


def test_every_probe_is_bounded_by_otto_and_not_by_the_callee():
    """The grant is enforced here, not delegated to whatever ``run`` is.

    MEASURED, and this is why the module cannot trust its callee: otto's own
    ``UnixHost.exec`` accepts ``timeout=`` and applies it to the COMMAND, not
    to establishing the SSH connection the command needs first.
    ``exec("true", timeout=2.0)`` against an unroutable address had still not
    returned after 120 seconds. Passing the grant down is therefore advice,
    and the arithmetic in this module's budget comment — eleven probes reduced
    to three, one resolution bounded at ``_RESOLVE_BUDGET_S`` — is only true if
    something up here actually stops waiting.

    That matters more now than when it was written: the first elevated command
    on every unix host resolves before it runs, so an unreachable host would
    hang the caller inside otto's adaptation step rather than in the caller's
    own command.
    """
    every, wrapped = _run_call_sites()
    assert every, "found no self._run calls — the scan is broken, not the module"
    assert sorted(every) == sorted(wrapped), (
        f"probes issued outside asyncio.wait_for at line(s) "
        f"{sorted(set(every) - set(wrapped))}; the grant passed to the callee is "
        f"advice, so an unbounded callee makes the resolution budget fiction"
    )


@pytest.mark.asyncio
async def test_a_probe_whose_grant_is_spent_is_abandoned_not_awaited(monkeypatch):
    """Companion to the structural guard: the bound actually bites.

    Driven by shrinking ``_PROBE_TIMEOUT_S`` to zero rather than by racing a
    sleep against a threshold, so nothing here reads a clock and no load can
    reorder the outcome: ``asyncio.wait_for`` with a non-positive timeout
    abandons a coroutine that is not already finished, so the runner is never
    reached at all. Verified on every interpreter this repo supports — 3.10,
    3.11, 3.12, 3.13 and 3.14 all raise ``TimeoutError`` with the callee
    untouched. Recorded because the dependency is real, not because it is
    silent: were that special case ever dropped, the runner would simply be
    called and this guard would RED on a correct product. The note is here so
    whoever meets that failure recognises it as a interpreter change rather
    than a regression in ``_probe``.

    Unwrapped, the zero is merely passed down as a ``timeout=`` kwarg, and this
    runner — like a real ``exec`` mid-handshake — answers anyway. Every
    capability then resolves from the device, which is what the assertion
    below distinguishes.
    """
    monkeypatch.setattr(userland_module, "_PROBE_TIMEOUT_S", 0)
    device = next(d for d in _DEVICES if d.name == "gnu-coreutils-bash")
    runner = _Runner(device.works)
    userland = Userland(UserlandOptions(), runner)

    await userland.resolve()

    assert runner.calls == [], (
        f"probes were delivered despite a zero grant: {runner.calls}; the bound "
        f"is being left to the callee"
    )
    assert _answers(userland) == _UNASKABLE
    assert userland.as_lab_json() == {}, "nothing was measured, so nothing is pasteable"


@pytest.mark.asyncio
async def test_the_bound_the_wrapper_enforces_is_the_one_it_advertises(monkeypatch):
    """The wrapper's timeout is the budget-aware grant, not the raw constant.

    The structural guard above only says a wrapper EXISTS, and the zero-grant
    guard collapses both spellings to the same zero — so between them a
    ``wait_for(..., timeout=_PROBE_TIMEOUT_S)`` wrapping a call advertised at
    the shrunken grant passes everything. That mutant restores precisely the
    defect the wrapper was added for: the last probe of a nearly-spent budget
    would be enforced at the full per-probe bound and overrun the resolution's
    total.

    Reading the enforced value directly, by recording what ``wait_for`` is
    handed. ``runner.kwargs`` cannot serve: that is the ADVISORY kwarg passed
    down to the callee, which is the number this module explicitly does not
    trust. The ratio is 25/10 so the third grant is shrunk to 5 — at a ratio
    that divides evenly the two spellings agree and nothing here could fail.

    ``asyncio.wait_for`` is patched on the module object, which is shared;
    monkeypatch restores it, and nothing but ``resolve()`` runs inside the
    window.
    """
    clock = _FakeClock()
    monkeypatch.setattr(userland_module, "_monotonic", clock)
    monkeypatch.setattr(userland_module, "_RESOLVE_BUDGET_S", 25.0)
    monkeypatch.setattr(userland_module, "_PROBE_TIMEOUT_S", 10.0)
    enforced: list[float] = []
    real_wait_for = asyncio.wait_for

    async def _recording_wait_for(awaitable, timeout):
        enforced.append(timeout)
        return await real_wait_for(awaitable, timeout)

    monkeypatch.setattr(asyncio, "wait_for", _recording_wait_for)
    runner = _Runner({_P_TIMEOUT}, clock=clock)

    await Userland(UserlandOptions(), runner).resolve()

    advertised = [kw["timeout"] for kw in runner.kwargs]
    assert enforced == advertised, (
        f"the wrapper enforced {enforced} while telling the callee {advertised}; "
        f"the two cannot diverge or the resolution budget is unbounded again"
    )
    assert enforced[-1] < userland_module._PROBE_TIMEOUT_S, (
        f"the last grant was {enforced[-1]}, the full per-probe bound — a budget "
        f"with 5s left must not authorise a 10s wait"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("budget", "probe", "pasteable"),
    [
        (None, None, {"elevation": "none"}),
        (25.0, 10.0, {"elevation": "none"}),
        (35.0, 10.0, {"elevation": "none"}),
        # The only row whose budget reaches BOTH timeout spellings, so the
        # "absent" it records is measured rather than assumed — and the one
        # row where the pasteable table is bigger than the elevation answer.
        (30.0, 7.0, {"elevation": "none", "timeout_style": "absent"}),
    ],
    ids=["as-shipped", "25-over-10", "35-over-10", "30-over-7"],
)
async def test_resolution_stops_once_the_whole_budget_is_spent(
    monkeypatch, budget, probe, pasteable
):
    """One resolution is bounded in total, not just probe by probe.

    Eleven probes at _PROBE_TIMEOUT_S each is 110s of lock held on a host that
    swallows everything, and resolve() holds that lock across the lot — so a
    concurrent consumer's queued callers wait the whole span out before their
    own timeouts even start. The budget converts that into a stated bound.

    Driven from a fake clock the runner advances by exactly the bound each
    probe was granted, so the guard is deterministic and reads no wall clock.

    THE RATIOS ARE THE POINT, and three of the four deliberately do not divide
    evenly. The shipped pair is 30/10, and two separate defects hid behind
    that exact division: this guard computed its expectation with FLOOR when
    the product (rightly) sends a partially affordable probe, and dropping the
    ``min()`` that shrinks the last probe's grant was inert. Both are only
    visible at a ratio with a remainder — 25/10 sends 3 probes, not 2, and
    without the ``min()`` the third would overrun to 30s of a 25s budget. A
    row is kept for the shipped pair so the values that actually run are
    covered, but it is the non-dividing rows that exercise the arithmetic.
    """
    if budget is not None:
        monkeypatch.setattr(userland_module, "_RESOLVE_BUDGET_S", budget)
        monkeypatch.setattr(userland_module, "_PROBE_TIMEOUT_S", probe)
    budget = userland_module._RESOLVE_BUDGET_S
    probe = userland_module._PROBE_TIMEOUT_S
    clock = _FakeClock()
    monkeypatch.setattr(userland_module, "_monotonic", clock)
    runner = _Runner({_P_TIMEOUT}, clock=clock)
    userland = Userland(UserlandOptions(), runner)

    await userland.resolve()

    # CEIL, not floor: a probe the budget can only partly afford is still
    # sent, granted whatever is left. Floor passes only while the two
    # constants divide exactly.
    affordable = math.ceil(budget / probe)
    assert affordable < len(_EVERY_PROBE_IN_ORDER), (
        f"a {budget}s budget at {probe}s a probe affords every probe, so this "
        f"row cannot distinguish a bounded resolution from an unbounded one"
    )
    assert len(runner.calls) == affordable, (
        f"budget {budget}s at {probe}s a probe affords {affordable} probes, "
        f"but {len(runner.calls)} were sent"
    )
    # The budget is a CEILING, not a target: the last probe's grant is
    # shrunk to what is left, so the total cannot exceed it. Inert at 30/10
    # (3 x 10 lands exactly on 30) and load-bearing at every other ratio.
    assert clock.now <= budget, f"resolution overran its budget: {clock.now}s of {budget}s"
    # A spent budget is not an answer. The two elevation probes fit inside
    # every row's budget and really are measured, so elevation is the device's
    # "none"; everything the budget cut off was never asked, and takes the
    # no-information default rather than a "no" nobody heard.
    assert _answers(userland) == {**_UNASKABLE, "elevation": "none"}
    # ...and only what the budget actually reached may be pinned. Stated per
    # row rather than derived, so a change to the arithmetic has to be looked
    # at rather than absorbed.
    assert userland.as_lab_json() == pasteable


# ── the constraints the plan binds this module to ───────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [None, "1.28.1", "1.31.0"])
async def test_a_declared_version_changes_nothing(version):
    """No version gate in product code, asserted across the tempting boundary.

    1.28.1 and 1.31.0 are exactly where a version gate would be written, since
    that is where the `timeout` convention changes. The device script is held
    identical across all three, so any behavioural difference can only have
    come from the declared version — which must never be consulted, because
    BusyBox applets can be compiled out and the number cannot know that.
    """
    device = frozenset({_P_SU, _P_TIMEOUT, _P_TIMEOUT_DASH_T, _P_BASE64_SHORT, _P_STAT, _P_WC})
    baseline = _Runner(device)
    reference = Userland(UserlandOptions(), baseline)
    await reference.resolve()

    runner = _Runner(device)
    userland = Userland(UserlandOptions(version=version), runner)
    await userland.resolve()

    assert userland.as_lab_json() == reference.as_lab_json()
    assert runner.calls == baseline.calls


def test_the_module_never_reads_the_declared_version():
    """Structural companion to the behavioural guard above.

    The behavioural one only catches a gate that the two chosen versions
    happen to straddle. This one catches the read itself, wherever it is
    spelled, so "documentation only" cannot decay into a condition.
    """
    tree = ast.parse(Path(userland_module.__file__).read_text())
    docstrings = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }
    offenders = [
        node.lineno
        for node in ast.walk(tree)
        if (isinstance(node, ast.Attribute) and node.attr == "version")
        or (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value == "version"
            and id(node) not in docstrings
        )
    ]

    assert not offenders, (
        f"{userland_module.__file__} reads the declared version at line(s) {offenders}; "
        "a version must never gate behaviour"
    )


@pytest.mark.asyncio
async def test_concurrent_resolves_still_probe_once():
    """The cache has to hold against CONCURRENT first callers, not just repeat ones.

    This is not hypothetical for the consumer this feeds: nc's bulk put fans
    its files out concurrently, so N files would enter resolve() together
    and a check-then-probe with no lock issues N x 10 probes on the first
    transfer. Each one is an SSH exec channel against a server whose default
    `MaxSessions` is 10 and which REFUSES rather than queues the excess — the
    exact failure this branch has already paid for once.
    """
    solo = _Runner({_P_SUDO})
    await Userland(UserlandOptions(), solo).resolve()

    runner = _Runner({_P_SUDO})
    userland = Userland(UserlandOptions(), runner)
    await asyncio.gather(*(userland.resolve() for _ in range(5)))

    assert len(runner.calls) == len(solo.calls), (
        f"5 concurrent resolve() calls issued {len(runner.calls)} probes; "
        f"one resolve() issues {len(solo.calls)}"
    )


@pytest.mark.asyncio
async def test_a_wedged_host_is_not_amplified_by_the_fan_out(monkeypatch):
    """Retrying an unsettled capability must not scale with the number of callers.

    The companion to ``test_concurrent_resolves_still_probe_once``, and the
    case that one cannot see: its device ANSWERS, so everything settles on the
    first call and the lock alone is enough. Against a device that refuses,
    nothing settles, so every queued caller is entitled to retry — and nc's
    bulk put re-enters ``prepare()`` once per file. Measured before the
    cooldown existed: 12 callers cost 36 probes and 360s where one costs 3
    and 30s.

    The reason that is worse than a cost regression is that the trigger and
    the cost are the SAME RESOURCE. An sshd at its ``MaxSessions`` ceiling
    refuses an exec channel; the refusal is what leaves the key unsettled; the
    retry answers by opening more channels into the thing already refusing
    them. So the retry has to be bounded by TIME rather than by caller count.

    Driven from the fake clock, so the cooldown is exercised without waiting
    and no wall clock is read.
    """
    clock = _FakeClock()
    monkeypatch.setattr(userland_module, "_monotonic", clock)
    budget = userland_module._RESOLVE_BUDGET_S
    affordable = math.ceil(budget / userland_module._PROBE_TIMEOUT_S)
    runner = _Runner({_P_SUDO}, unreachable=set(_EVERY_PROBE_IN_ORDER), clock=clock)
    userland = Userland(UserlandOptions(), runner)

    await asyncio.gather(*(userland.resolve() for _ in range(12)))

    assert len(runner.calls) == affordable, (
        f"12 concurrent callers against a wedged host issued {len(runner.calls)} "
        f"probes; one resolution attempt affords {affordable}, and the extra "
        f"channels go into the server that is already refusing them"
    )
    assert clock.now <= budget, f"the fan-out spent {clock.now}s of a {budget}s bound"

    # A cooldown, not a surrender: the host recovers and is asked again. It
    # answers promptly now, so it no longer burns a full grant per probe.
    runner.unreachable = set()
    runner.clock = None
    await userland.resolve()
    assert len(runner.calls) == affordable, "the cooldown did not hold the immediate retry"

    clock.now += userland_module._RETRY_COOLDOWN_S
    await userland.resolve()

    assert len(runner.calls) > affordable, "the cooldown never expired, so it is a surrender"
    # `ash` is what the recovered device says; `bash` is what was assumed while
    # it was wedged, so this cannot pass on a stale value.
    assert userland.shell_dialect == "ash"


@pytest.mark.asyncio
async def test_a_probe_that_raises_degrades_instead_of_escaping(caplog):
    """A failed probe leaves a defined state; it never becomes the caller's error.

    Resolution is an ADAPTATION step in front of the work the user asked for.
    If the transport is broken, the command the user actually ran will say so
    with its own context; a connection error thrown from inside a probe would
    surface instead as an unexplained failure of a transfer, naming a command
    the user never issued.

    The call count is asserted as well as the answers: catching the first
    raise and abandoning the rest would also produce a degraded map, and would
    silently stop probing a host that recovered mid-resolution.

    The map is the UNASKABLE one, not the fully-degraded one, and the two must
    never be conflated — a host that answered no to everything is a minimal
    device, while this one said nothing at all.
    """
    runner = _Runner(raises=OSError("channel closed by peer"))
    userland = Userland(UserlandOptions(), runner)

    with caplog.at_level(logging.DEBUG, logger="otto.host.userland"):
        await userland.resolve()

    assert _answers(userland) == _UNASKABLE
    assert userland.as_lab_json() == {}, "nothing was measured, so nothing is pasteable"
    # The degrade path logs too, and a broken host is exactly when someone is
    # tempted to promote it to warning — which would put it on the console.
    _assert_debug_only(caplog)
    assert len(runner.calls) == len(_EVERY_PROBE_IN_ORDER) - 2, (
        "a raising probe stopped the others; every arm should still be tried "
        "(minus the two timeout spellings, unreachable once `command -v timeout` fails)"
    )


@pytest.mark.asyncio
async def test_the_pasteable_summary_never_offers_a_value_otto_did_not_measure(caplog):
    """A user must not be able to pin, by hand, a value the device never gave.

    THE HAZARD this closes. The line says "pin these to skip the probes next
    time", and a pinned value is DECLARED — settled forever, never re-probed.
    So an unmeasured answer reaching that payload re-creates by hand exactly
    the permanence the tri-state probe removed, except now it is in the user's
    lab data where nothing will ever revisit it. Marking the provenance on
    separate lines above the payload does not help: the payload is what gets
    copied, and inside it a guess is indistinguishable from a measurement.

    THE DEVICE, chosen so the guess is WRONG rather than harmlessly right: an
    old BusyBox box — really ``ash`` with no ``stat`` applet — whose stat and
    bash arms are refused. The no-information defaults are ``bash`` and
    ``stat``, so a complete payload would hand the user two values that are
    the opposite of the truth about their own hardware.

    Both directions are asserted. Omitting the unmeasured keys is only correct
    if what remains is real, and a payload that dropped everything would pass
    a "no guesses" check while making the feature useless.
    """
    device = next(d for d in _DEVICES if d.name == "busybox-1.28.1")
    userland = Userland(
        UserlandOptions(), _Runner(device.works, unreachable={_P_STAT, _P_WC, _P_BASH})
    )

    with caplog.at_level(logging.DEBUG, logger="otto.host.userland"):
        await userland.resolve()

    summary = [r.getMessage() for r in caplog.records if "pin these" in r.getMessage()]
    assert summary, "no pasteable summary was emitted"
    payload = json.loads(summary[0].split('-- "userland_options": ', 1)[1])

    assert payload == {
        "elevation": "su",
        "timeout_style": "dash-t",
        "base64_flag": "-d",
        "checksum": "md5sum",
    }, f"the paste line offered {payload}; every key in it must have been measured"
    UserlandOptionsSpec(**payload)
    # The host still HAS answers for the two it could not ask — it just may not
    # invite anyone to make them permanent.
    assert userland.stat_size == "stat"
    assert userland.shell_dialect == "bash"
    _assert_debug_only(caplog)


@pytest.mark.asyncio
async def test_a_transport_blip_never_becomes_a_measurement(caplog):
    """One probe that never reached the device must not settle the capability.

    THE SCENARIO, on a perfectly healthy GNU/sudo host: the very first probe
    hits a refused exec channel — an sshd at its ``MaxSessions`` ceiling
    refuses rather than queues, which this repo has already paid for once —
    and every later probe succeeds. The su arm then answers yes, because a
    sudo host has ``su`` too.

    Reading that as ``elevation = "su"`` is the bug: nothing measured sudo, so
    nothing contradicted it, and ``su -c`` authenticates as the TARGET account
    whose password the lab may not even hold. It is also a regression against
    the behaviour this branch inherited, where the sudo command simply ran and
    reported the transport error itself.

    So an arm that could not be asked yields no conclusion for the whole
    capability, and the no-information answer is the one otto used before it
    asked anything. The provenance assertion is not decoration: the value is
    indistinguishable from a measured ``sudo``, and the debug line is the only
    place a user can tell that otto guessed.
    """
    gnu = next(d for d in _DEVICES if d.name == "gnu-coreutils-bash")
    runner = _Runner(gnu.works, unreachable={_P_SUDO})
    userland = Userland(UserlandOptions(), runner)

    with caplog.at_level(logging.DEBUG, logger="otto.host.userland"):
        await userland.resolve()

    assert userland.elevation == _UNASKABLE["elevation"], (
        f"a sudo host whose sudo probe never reached it resolved "
        f"{userland.elevation!r}; the su arm answering yes is not evidence about sudo"
    )
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("userland: ")]
    assert "userland: elevation = sudo (assumed)" in lines, (
        f"an unmeasured answer was not labelled as one; got {lines}"
    )
    # Everything the blip did not touch is still measured from the device.
    assert userland.timeout_style == "coreutils"
    _assert_debug_only(caplog)


# (field, arms that never arrive, what a blip must answer, what the device says)
_BLIP_RECOVERY = [
    ("elevation", {_P_SUDO}, "busybox-1.28.1", "sudo", "su"),
    ("timeout_style", {_P_TIMEOUT}, "gnu-coreutils-bash", "absent", "coreutils"),
    ("base64_flag", {_P_BASE64_SHORT, _P_BASE64_LONG}, "gnu-coreutils-bash", "absent", "-d"),
    ("stat_size", {_P_STAT, _P_WC}, "busybox-stat-compiled-out", "stat", "wc"),
    ("checksum", {_P_MD5SUM}, "busybox-1.28.1", "absent", "md5sum"),
    ("shell_dialect", {_P_BASH}, "busybox-1.28.1", "bash", "ash"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field_name", "unreachable", "device_name", "assumed", "measured"),
    _BLIP_RECOVERY,
    ids=[row[0] for row in _BLIP_RECOVERY],
)
async def test_an_unasked_capability_is_re_probed_until_the_device_answers(
    field_name, unreachable, device_name, assumed, measured, monkeypatch
):
    """A guess is provisional; only a measurement is cached.

    ``resolve()`` is idempotent on what it has SETTLED — declared or measured —
    not on having run once. Without that, the first call during a blip is the
    only call that ever happens: nothing re-probes, not a later command, not
    ``rebuild_connections()``, nothing short of a new host object, and the
    guess outlives the outage that caused it for the whole session.

    Every row's assumed answer DIFFERS from what the device says, so the
    second assertion cannot be satisfied by the first still standing. The
    probe count is asserted too — a resolve() that returned the right answer
    without re-probing would mean the first call had cached a measurement it
    never took.

    The clock is faked and advanced past the retry cooldown, because "the next
    call" means the next one the cooldown allows. Advancing it rather than
    waiting is what keeps this off the wall clock.
    """
    assert assumed != measured, "this row cannot tell a re-probe from a cached guess"
    clock = _FakeClock()
    monkeypatch.setattr(userland_module, "_monotonic", clock)
    device = next(d for d in _DEVICES if d.name == device_name)
    runner = _Runner(device.works, unreachable=unreachable)
    userland = Userland(UserlandOptions(), runner)

    await userland.resolve()
    assert getattr(userland, field_name) == assumed
    during_blip = len(runner.calls)

    # The blip passes. Nothing else about the host changes.
    runner.unreachable = set()
    clock.now += userland_module._RETRY_COOLDOWN_S
    await userland.resolve()

    assert len(runner.calls) > during_blip, (
        f"{field_name} was never re-probed after the blip cleared, so the "
        f"guess {assumed!r} is now permanent"
    )
    assert getattr(userland, field_name) == measured


@pytest.mark.asyncio
async def test_a_settled_capability_is_not_re_probed_by_a_neighbour_s_retry(monkeypatch):
    """Recovery re-probes the unsettled keys only, never the whole round.

    The retry above must not become "resolve everything again on every call".
    A device that answered five of six capabilities has already paid for
    those five, and re-issuing them on the next elevated command puts the
    probe traffic back on the fan-out path the lock was added to protect.
    """
    clock = _FakeClock()
    monkeypatch.setattr(userland_module, "_monotonic", clock)
    device = next(d for d in _DEVICES if d.name == "gnu-coreutils-bash")
    runner = _Runner(device.works, unreachable={_P_BASH})
    userland = Userland(UserlandOptions(), runner)

    await userland.resolve()
    during_blip = list(runner.calls)
    runner.unreachable = set()
    clock.now += userland_module._RETRY_COOLDOWN_S
    await userland.resolve()

    retried = runner.calls[len(during_blip) :]
    assert retried == [_P_BASH], (
        f"the retry re-issued {retried}; only the capability that could not be "
        f"asked should be asked again"
    )


@pytest.mark.asyncio
async def test_a_cancelled_probe_is_not_swallowed():
    """Degrading on failure must not extend to cancellation.

    Swallowing CancelledError turns a cancelled resolve into nine more probes
    against a host otto is trying to let go of, and the shutdown waits for
    every one of them.
    """
    runner = _Runner(raises=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await Userland(UserlandOptions(), runner).resolve()

    assert len(runner.calls) == 1, f"probing continued after cancellation: {runner.calls}"
