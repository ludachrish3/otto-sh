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
from otto.host.command_frame import AshFrame
from otto.host.options import UserlandOptions
from otto.host.session import refuse_if_line_editor_would_truncate
from otto.host.userland import (
    APPLET_ABSENT,
    APPLET_PRESENT,
    PROBED_APPLETS,
    Userland,
    applet_capability,
)
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

# The applet batch. THIS FILE'S OWN COPY of the spelling, like every other
# constant above -- the product builds it in `_applet_probe_command` and Tier 2
# measures it against real binaries in
# `tests/busybox/test_applet_resolution.py`, and all three have to agree
# without importing one another.
#
# The one probe here whose spelling is not a fixed string: it is built from the
# list of names still open, so the helper takes that list. That is the O(1)
# property in the pin -- ONE command for however many names.
_APPLET_CONTROL = "echo"


def _p_applets(applets) -> str:
    names = " ".join([_APPLET_CONTROL, *applets])
    return (
        f'for a in {names}; do command -v "$a" >/dev/null 2>&1 && echo "$a=1" || echo "$a=0"; done'
    )


_P_APPLETS = _p_applets(PROBED_APPLETS)


def _applet_answer(present=()) -> str:
    """The stdout a device gives for the whole-list batch, with *present* saying yes."""
    lines = [f"{_APPLET_CONTROL}=1"]
    lines += [f"{a}={'1' if a in present else '0'}" for a in PROBED_APPLETS]
    return "\n".join(lines) + "\n"


# ``version`` is the one field with no probe, by the plan's binding constraint:
# a declared version is documentation and must never gate behaviour. Every
# OTHER field of UserlandOptions is derived, so adding an eighth field without
# teaching Userland to resolve it reddens
# ``test_every_option_field_is_probed_or_documented_as_never_probed`` instead
# of quietly going unresolved.
_NEVER_PROBED = {"version"}


def _probeable_field_names() -> list[str]:
    return sorted(f.name for f in fields(UserlandOptions) if f.name not in _NEVER_PROBED)


# The applet capabilities, and the six that are not. Derived from
# `PROBED_APPLETS` through the product's own bridge rather than by matching a
# prefix here, so a rename of the prefix cannot leave this file quietly
# classifying every applet field as a fixed one.
_APPLET_FIELDS = [applet_capability(a) for a in PROBED_APPLETS]
_APPLET_BY_FIELD = {applet_capability(a): a for a in PROBED_APPLETS}


def _fixed_field_names() -> list[str]:
    return [n for n in _probeable_field_names() if n not in _APPLET_BY_FIELD]


def _read(userland, name: str) -> str:
    """Read capability *name* the way its own consumer would.

    The applet capabilities have no property -- one parameterized
    :meth:`Userland.has_applet` stands in for what would otherwise be seven of
    them -- so "read every capability" is two spellings, not one. Derived, so
    a capability that grows a reader of a third kind reddens rather than being
    skipped.
    """
    applet = _APPLET_BY_FIELD.get(name)
    return userland.has_applet(applet) if applet is not None else getattr(userland, name)


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
    return {n: _read(userland, n) for n in _probeable_field_names()}


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
    # Every applet name reported `<name>=0` by a batch that RAN, control and
    # all: a measurement of a userland with none of them, not a batch that
    # failed. The two are different states and `_UNASKABLE` below is the other.
    **dict.fromkeys(_APPLET_FIELDS, APPLET_ABSENT),
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
#   applet_*       `present`, uniformly, and it is the same rule the five
#                  above are instances of rather than a seventh argument:
#                  otto reached for `shutdown`, `scp` and `base64` by name
#                  before this probe existed, so "present" is what it did
#                  before it asked anything. `absent` would make a refused
#                  probe round read as a device that has none of them.
_UNASKABLE = {
    "base64_flag": "absent",
    "checksum": "absent",
    "elevation": "sudo",
    "shell_dialect": "bash",
    "stat_size": "stat",
    "timeout_style": "absent",
    **dict.fromkeys(_APPLET_FIELDS, APPLET_PRESENT),
}

# Every probe otto issues, in order, on a host that answers only `command -v
# timeout` — the one script that reaches all twelve arms, because a `timeout`
# that is absent short-circuits both spelling probes.
#
# TWELVE COMMANDS FOR THIRTEEN CAPABILITIES, and the last line is why: the
# applet batch is ONE command whatever the length of `PROBED_APPLETS`, and it
# is LAST so the eleven before it keep the order, the spellings and the count
# they had before applets existed.
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
    _P_APPLETS,
]

# The eleven that predate this capability, in the order and the spellings they
# were issued in then. Read by the no-regression guard, which is the only thing
# standing between "the applet batch was added" and "the applet batch changed
# what every existing host is asked".
_PROBES_BEFORE_APPLETS = _EVERY_PROBE_IN_ORDER[:-1]


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


_APPLET_BATCH_PREFIX = "for a in "


def _emulate_applet_batch(cmd: str, present) -> str:
    """Answer the batch the way a device's own ash would, for whatever it asked.

    Parsed rather than table-matched, and that is the one place this runner
    cannot match exactly: the batch's text DEPENDS ON WHICH NAMES ARE STILL
    OPEN, so a declared applet shortens it. A fixed-string entry would answer
    only the all-seven spelling and make every partial-declaration guard below
    fall through to "unknown command".
    """
    names = cmd[len(_APPLET_BATCH_PREFIX) :].split(";", 1)[0].split()
    return "".join(f"{n}={'1' if n in present else '0'}\n" for n in names)


class _Runner:
    """Records every command, its LogMode and its kwargs; answers a scripted device.

    Matching is EXACT, not substring: ``command -v su`` is a substring of
    ``command -v sudo``, so a substring table would answer the sudo probe from
    the su entry and make a su-only device look like a sudo device. The applet
    batch is the sole exception and it is not a relaxation of that rule -- see
    :func:`_emulate_applet_batch`.
    """

    def __init__(
        self,
        works=(),
        *,
        fail_retcode: int = 1,
        raises: BaseException | None = None,
        unreachable=(),
        clock: _FakeClock | None = None,
        applets=(),
        applet_control: bool = True,
        applet_stdout: str | None = None,
        applet_retcode: int = 0,
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
        # The applets this device HAS. Absent-by-default, matching the four
        # matrix answers that are absent everywhere (`scp`, `shutdown`) more
        # closely than a present-by-default would.
        self._applets = set(applets)
        # The three ways a batch can come back looking like a measurement
        # without being one, each drivable on its own: the control saying no,
        # the answer being the wrong shape, and a non-zero exit.
        #
        # `applet_control` is PUBLIC and mutable for the same reason
        # `unreachable` is: a device that recovers is one whose hostile
        # condition STOPPED, and a recovery guard has to be able to stop it
        # between two `resolve()` calls.
        self.applet_control = applet_control
        self._applet_stdout = applet_stdout
        self._applet_retcode = applet_retcode

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
        if cmd.startswith(_APPLET_BATCH_PREFIX):
            # The loop's own exit code is the LAST `echo`'s, so a real device
            # answers 0 even when every name is absent. A device with none of
            # these applets is a measurement, not a failed probe, and scripting
            # it as `fail_retcode` below would erase that distinction.
            present = set(self._applets)
            if self.applet_control:
                present.add(_APPLET_CONTROL)
            out = (
                _emulate_applet_batch(cmd, present)
                if self._applet_stdout is None
                else self._applet_stdout
            )
            return CommandResult(
                Status.Success if self._applet_retcode == 0 else Status.Error,
                value=out,
                retcode=self._applet_retcode,
            )
        if cmd in self._works:
            return _ok()
        return CommandResult(Status.Error, value="", retcode=self._fail_retcode)


@pytest.mark.asyncio
@pytest.mark.parametrize("field_name", _fixed_field_names())
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

    THE APPLET FIELDS ARE EXCLUDED HERE AND COVERED BY THE SIBLING BELOW, and
    the reason is the strict-subset assertion rather than a scoping preference:
    declaring one applet does not REMOVE a command, it SHORTENS one, so the
    declared run issues a batch string that is not in the baseline's set and
    fails a subset check while behaving exactly as it should. The split is
    derived from ``PROBED_APPLETS`` on both sides, so neither test can lose a
    field to the other.
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
@pytest.mark.parametrize("applet", PROBED_APPLETS)
async def test_a_declared_applet_is_never_asked_about(applet):
    """A declared applet leaves the batch, and the batch is still one command.

    The applet half of the guard above, and it asserts the thing that shape
    makes possible: a partial pin costs LESS, and a complete one (the sibling
    below) costs nothing. A design where the seven answers rode one opaque
    value could not do this -- a part-filled declaration would either be read
    as a complete answer or ignored entirely.

    The batch's exact text is pinned, not merely its length, because "the
    declared name is gone" and "the other six are still asked" are different
    claims and dropping a second name would satisfy the first.
    """
    baseline = _Runner()
    await Userland(UserlandOptions(), baseline).resolve()

    runner = _Runner()
    userland = Userland(UserlandOptions(**{applet_capability(applet): APPLET_ABSENT}), runner)
    await userland.resolve()

    assert userland.as_lab_json()[applet_capability(applet)] == APPLET_ABSENT
    assert userland.has_applet(applet) == APPLET_ABSENT
    assert len(runner.calls) == len(baseline.calls), (
        f"declaring one applet changed the ROUND COUNT ({len(baseline.calls)} -> "
        f"{len(runner.calls)}); the whole point of the batch is that it is one "
        f"command however many names are open"
    )
    expected = _p_applets([a for a in PROBED_APPLETS if a != applet])
    assert runner.calls[-1] == expected, (
        f"the batch was {runner.calls[-1]!r}; declaring {applet!r} must drop that "
        f"one name and no other"
    )


@pytest.mark.asyncio
async def test_declaring_every_capability_issues_no_command_at_all():
    """THE REQUIREMENT: a maintainer who has recorded the answers pays nothing.

    BusyBox devices are typically slow, so the pin exists to skip the probe
    round entirely -- and "entirely" is the word this asserts. A batch that
    still went out to ask about nothing, or to re-confirm what was declared,
    would be a round trip bought back on the one class of host the pin was
    written for.

    Derived from ``UserlandOptions``, so a capability added later without a
    declared arm reddens here instead of quietly costing every pinned host a
    command.
    """
    declared = dict.fromkeys(_fixed_field_names(), "DECLARED")
    declared |= dict.fromkeys(_APPLET_FIELDS, APPLET_PRESENT)
    runner = _Runner()

    userland = Userland(UserlandOptions(**declared), runner)
    await userland.resolve()

    assert runner.calls == [], f"a fully declared host still probed: {runner.calls}"
    assert userland.as_lab_json() == declared


@pytest.mark.asyncio
async def test_the_pasteable_pin_round_trips_through_lab_data_and_then_costs_nothing():
    """The whole loop: probe once, paste the line, and the next host asks nothing.

    Every step of what the debug line promises, executed rather than described:
    the emitted payload validates at the lab-data boundary
    (``UserlandOptionsSpec``, ``extra='forbid'``), builds the runtime options a
    host would carry, and a ``Userland`` over those options issues no command
    and reports the same answers. Any capability that cannot make that trip --
    a probe with no field, a field with no spec entry, a value outside the
    spec's Literal -- breaks it at the step it is missing from.

    A SETTLED-ONLY payload is what makes this honest: the device below answers
    everything, so nothing is dropped and the second host really does get the
    complete table.
    """
    device = next(d for d in _DEVICES if d.name == "gnu-coreutils-bash")
    first = Userland(UserlandOptions(), _Runner(device.works, applets=device.applets))
    await first.resolve()
    pinned = first.as_lab_json()
    assert set(pinned) == set(_probeable_field_names()), (
        f"the device answered everything, so the pin must be complete; it omitted "
        f"{sorted(set(_probeable_field_names()) - set(pinned))}"
    )

    options = UserlandOptionsSpec(**json.loads(json.dumps(pinned))).to_runtime()
    second_runner = _Runner()
    second = Userland(options, second_runner)
    await second.resolve()

    assert second_runner.calls == [], (
        f"a host carrying the pasted pin still probed: {second_runner.calls}"
    )
    assert _answers(second) == _answers(first)


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

    Every reader, derived: one added later would otherwise be the one that
    answers ``None`` quietly. ``_read`` covers both spellings -- the six
    properties and :meth:`Userland.has_applet` -- because a parameterized
    reader is just as capable of resolving on first read as a property is.
    """
    userland = Userland(UserlandOptions(), _Runner())

    with pytest.raises(RuntimeError, match="before resolve"):
        _read(userland, field_name)


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
@pytest.mark.parametrize("field_name", _probeable_field_names())
async def test_is_settled_separates_a_measurement_from_an_assumption(field_name):
    """The tri-state, exposed for consumers that REFUSE rather than degrade.

    Every field, because the distinction is a property of the module and not of
    one capability: a device that answered has settled it, and a device that
    could not be asked has not, whatever the value ends up reading as. The
    property alone cannot tell them apart -- ``base64_flag`` says ``"absent"``
    both ways round -- which is why
    ``otto.host.file_ops.refuse_if_base64_is_absent`` asks this first. All three
    arms are asserted in one test on purpose: an implementation that always
    answered True, and one that always answered False, each satisfy one of them.
    """
    answered = Userland(UserlandOptions(), _Runner())
    await answered.resolve()
    assert answered.is_settled(field_name), (
        f"{field_name} was answered by the device (a NO is still an answer) and must "
        f"count as settled, or a refusal keyed on it can never fire"
    )

    unasked = Userland(UserlandOptions(), _Runner(unreachable=_EVERY_PROBE_IN_ORDER))
    await unasked.resolve()
    assert not unasked.is_settled(field_name), (
        f"{field_name} was never asked, so calling it settled would let a refused "
        f"probe round become a verdict about the device"
    )

    declared = Userland(UserlandOptions(**{field_name: "DECLARED"}), _Runner())
    await declared.resolve()
    assert declared.is_settled(field_name), "a declaration is settled by definition"


def test_is_settled_refuses_a_name_it_does_not_resolve():
    """A typo must be loud, because the quiet answer is the dangerous one.

    ``is_settled("base64flag")`` answering False forever is a caller's refusal
    that can never fire -- this repo's most common defect, delivered by a
    missing underscore.
    """
    userland = Userland(UserlandOptions(), _Runner())
    with pytest.raises(ValueError, match="is not a userland capability"):
        userland.is_settled("base64flag")


def test_is_settled_is_false_before_anything_is_resolved():
    """Honest rather than a special case: nothing has been settled yet."""
    assert Userland(UserlandOptions(base64_flag="-d"), _Runner()).is_settled("base64_flag") is False


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
    """A scripted device: the probes it answers 0 to, and what must resolve.

    ``applets`` is separate from ``works`` because the applet answers do not
    arrive as exit codes: one batched command carries them all as stdout, so
    the script for them is a SET OF NAMES the device has, not a set of commands
    it succeeds at.

    ABSENT-BY-DEFAULT for anything a row does not list, unlike ``expected``,
    which every row must fill in completely. That asymmetry is deliberate and
    it is not a weaker claim about hardware: what a real matrix row answers is
    pinned per row and per applet in Tier 2
    (``tests/busybox/test_applet_resolution.py``, which reds with a ``KeyError``
    on an applet nobody recorded). These rows script a DEVICE for the resolver
    to talk to, and a new applet nobody has scripted is one the scripted device
    does not have.
    """

    name: str
    works: frozenset = field(default_factory=frozenset)
    expected: dict = field(default_factory=dict)
    applets: frozenset = field(default_factory=frozenset)

    @property
    def all_expected(self) -> dict:
        """Every capability's answer -- the row's own six plus its applets."""
        return {
            **self.expected,
            **{
                applet_capability(a): APPLET_PRESENT if a in self.applets else APPLET_ABSENT
                for a in PROBED_APPLETS
            },
        }


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
        # Measured 2026-08-14, Tier 2 rootfs: no `base64`, no `scp`, no
        # `shutdown`; `nc`, `poweroff` and both uu halves are there. The row
        # that makes uu the only measured codec path.
        applets=frozenset({"nc", "poweroff", "uudecode", "uuencode"}),
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
        # Measured 2026-08-14: `base64` has arrived; `scp` and `shutdown`
        # are still absent, as on every matrix row.
        applets=frozenset({"base64", "nc", "poweroff", "uudecode", "uuencode"}),
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
        # Measured 2026-08-14: identical applet set to 1.28.1 -- the applet
        # answers do not move across the `timeout` convention change.
        applets=frozenset({"base64", "nc", "poweroff", "uudecode", "uuencode"}),
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
        applets=frozenset({"base64", "nc", "poweroff", "uudecode", "uuencode"}),
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
        # The row where `scp` and `shutdown` are PRESENT, so the table is not
        # uniform in the two names the `scp-transfer` and `shutdown-command`
        # surfaces turn on -- an always-absent column would let a probe that
        # never reported "present" pass every other row. `uuencode` is absent
        # because a stock Debian/Ubuntu has it in `sharutils`, unshipped by
        # default, which is also what makes uu a codec otto must ASK about
        # rather than assume on a GNU host.
        applets=frozenset({"base64", "nc", "poweroff", "scp", "shutdown"}),
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
        applets=frozenset({"base64", "nc", "poweroff", "uudecode", "uuencode"}),
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
        applets=frozenset({"base64", "nc", "poweroff", "uudecode", "uuencode"}),
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
    assert set(device.all_expected) == set(_probeable_field_names()), (
        f"device {device.name} has no expected answer for every field"
    )
    userland = Userland(UserlandOptions(), _Runner(device.works, applets=device.applets))

    await userland.resolve()

    assert userland.as_lab_json() == device.all_expected
    # Read through the READERS too, derived from the field list: a property (or
    # a `has_applet` wired to the wrong key) returns a real string and is
    # invisible to a map-only assertion.
    assert _answers(userland) == device.all_expected
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
        UserlandOptions(),
        _Runner(
            device.works,
            applets=device.applets,
            unreachable={_P_STAT, _P_WC, _P_BASH, _P_APPLETS},
        ),
    )

    with caplog.at_level(logging.DEBUG, logger="otto.host.userland"):
        await userland.resolve()

    summary = [r.getMessage() for r in caplog.records if "pin these" in r.getMessage()]
    assert summary, "no pasteable summary was emitted"
    payload = json.loads(summary[0].split('-- "userland_options": ', 1)[1])

    # The applet batch is refused too, and every one of its seven keys is
    # therefore absent from the payload -- the sharpest version of this hazard,
    # since their no-information default is `present` and a complete payload
    # would invite the user to pin seven applets nothing looked for.
    assert payload == {
        "elevation": "su",
        "timeout_style": "dash-t",
        "base64_flag": "-d",
        "checksum": "md5sum",
    }, f"the paste line offered {payload}; every key in it must have been measured"
    UserlandOptionsSpec(**payload)
    # The host still HAS answers for the ones it could not ask — it just may
    # not invite anyone to make them permanent.
    assert userland.stat_size == "stat"
    assert userland.shell_dialect == "bash"
    assert userland.has_applet("scp") == APPLET_PRESENT
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
    # `scp` rather than an applet the row HAS: the assumed answer is `present`
    # for every applet, so a row whose device also says present could not tell
    # a re-probe from the standing guess. 1.28.1 has no `scp`, so the two
    # differ.
    (applet_capability("scp"), {_P_APPLETS}, "busybox-1.28.1", APPLET_PRESENT, APPLET_ABSENT),
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
    runner = _Runner(device.works, applets=device.applets, unreachable=unreachable)
    userland = Userland(UserlandOptions(), runner)

    await userland.resolve()
    assert _read(userland, field_name) == assumed
    during_blip = len(runner.calls)

    # The blip passes. Nothing else about the host changes.
    runner.unreachable = set()
    clock.now += userland_module._RETRY_COOLDOWN_S
    await userland.resolve()

    assert len(runner.calls) > during_blip, (
        f"{field_name} was never re-probed after the blip cleared, so the "
        f"guess {assumed!r} is now permanent"
    )
    assert _read(userland, field_name) == measured


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
    runner = _Runner(device.works, applets=device.applets, unreachable={_P_BASH})
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


# ── the applet capability ───────────────────────────────────────────────────
#
# `PROBED_APPLETS` answers "does this device have applet X" for a CLOSED list
# of names, and it is deliberately probe-only: nothing in otto refuses on one
# yet. The five pieces of work that will are named in the list's own docstring.
# What these guards hold is the mechanism -- one round trip, a declaration that
# short-circuits it, a settled/assumed split a refusal can key on, and a
# pasteable pin -- because each of those five will inherit exactly this and
# none of them will re-derive it.


def test_every_probed_applet_has_a_declarable_field_and_no_field_is_orphaned():
    """The closed list and the options dataclass are the same set. Both ways.

    THE TWO FAILURES THIS CATCHES, and they are different bugs. A name in
    ``PROBED_APPLETS`` with no ``UserlandOptions`` field is a capability that
    cannot be declared or pinned -- ``_resolve_once``'s ``getattr`` would raise
    on the first resolution against any host. A field with no list entry is the
    quieter one: a maintainer writes it into ``lab.json``, the boundary accepts
    it because the field exists, and nothing ever reads it -- a declaration
    that silently does nothing, which is this repo's most common defect wearing
    a config file.

    The prefix is written out here, once, because that is the only way to ask
    the question in the direction that matters: derived from
    ``applet_capability`` on both sides, an orphaned field is invisible.
    """
    from_list = {applet_capability(a) for a in PROBED_APPLETS}
    from_fields = {n for n in _probeable_field_names() if n.startswith("applet_")}

    assert from_list == from_fields, (
        f"PROBED_APPLETS and UserlandOptions disagree: no field for "
        f"{sorted(from_list - from_fields)}, no list entry for "
        f"{sorted(from_fields - from_list)}"
    )


def test_the_applet_bridge_rejects_a_name_nothing_probes():
    """A typo must be loud at the bridge, not silent at the guard.

    ``applet_capability("scpp")`` answering ``"applet_scpp"`` would give
    ``is_settled`` a key it rejects -- but only at the moment the guard runs,
    which for a refusal is the moment it was supposed to fire. Both readers are
    checked, because a consumer holds the applet name and reaches this module
    through two doors.
    """
    with pytest.raises(ValueError, match="is not an applet this module probes"):
        applet_capability("scpp")

    userland = Userland(UserlandOptions(), _Runner())
    with pytest.raises(ValueError, match="is not an applet this module probes"):
        userland.has_applet("scpp")


@pytest.mark.asyncio
async def test_the_whole_applet_list_costs_exactly_one_round_trip():
    """O(1) round trips, whatever the list length -- the requirement, measured.

    BusyBox devices are typically slow, so a per-applet round trip is the cost
    this capability was shaped to avoid. Proved by VARYING the list rather than
    by counting against a constant: the same resolution is run with one applet
    and with all of them, and the number of commands must not move. A
    per-applet probe passes any fixed-number assertion written against
    today's seven.

    ``busybox --list`` is why this is a batched loop rather than an
    enumeration: measured, it exits 1 with ``--list: applet not found`` on
    1.16.1 (see ``tests/busybox/test_applet_resolution.py``), so four of the
    five matrix rows would answer and the oldest would report nothing.
    """
    assert len(PROBED_APPLETS) > 1, "a one-name list cannot distinguish O(1) from O(n)"

    wide = _Runner()
    await Userland(UserlandOptions(), wide).resolve()

    narrow_options = UserlandOptions(
        **dict.fromkeys(_APPLET_FIELDS[1:], APPLET_PRESENT),
    )
    narrow = _Runner()
    await Userland(narrow_options, narrow).resolve()

    assert len(wide.calls) == len(narrow.calls), (
        f"asking about {len(PROBED_APPLETS)} applets cost {len(wide.calls)} commands "
        f"and asking about 1 cost {len(narrow.calls)}; the cost must not scale with "
        f"the list"
    )
    batches = [c for c in wide.calls if c.startswith(_APPLET_BATCH_PREFIX)]
    assert batches == [_P_APPLETS], (
        f"the applet round was {batches}; it must be exactly one command naming every open applet"
    )


@pytest.mark.asyncio
async def test_the_batch_asks_about_every_applet_and_reports_each_one():
    """The command's text is the contract, so it is pinned literally.

    A batch that dropped a name would resolve that capability from its
    no-information default and mark it PROBED anyway -- an unmeasured value
    presented as a measurement, and offered for pinning. Asserting the whole
    command string catches the drop wherever it happens: in the list, in the
    join, or in the loop body.

    The device is scripted so the answers are MIXED. An all-present or
    all-absent row would be satisfied by a parser that ignored the value and
    returned a constant.
    """
    present = {"base64", "poweroff"}
    runner = _Runner(applets=present)
    userland = Userland(UserlandOptions(), runner)

    await userland.resolve()

    assert runner.calls[-1] == _P_APPLETS, (
        f"the applet batch was {runner.calls[-1]!r}, not the measured spelling"
    )
    assert {a: userland.has_applet(a) for a in PROBED_APPLETS} == {
        a: APPLET_PRESENT if a in present else APPLET_ABSENT for a in PROBED_APPLETS
    }
    for applet in PROBED_APPLETS:
        assert userland.is_settled(applet_capability(applet)), (
            f"{applet} was answered by the device and must count as settled"
        )


# How a batch can come back looking like a measurement without being one.
# Every row leaves the capabilities UNASKED — never `absent`, which is the
# whole point: `absent` is a verdict about the device and none of these
# measured the device.
_HOSTILE_BATCHES = [
    # `command -v` missing or broken: every name reports 0 and the shape is
    # perfect. The one failure a batch cannot see without a positive control.
    ("no-control", {"applet_control": False}),
    # The `run-command-line-length` failure mode: a shorter command ran and
    # answered for fewer names, with rc 0 and no error anywhere.
    ("truncated", {"applet_stdout": f"{_APPLET_CONTROL}=1\nbase64=1\n"}),
    # A login banner where the answers should be — nothing parses.
    ("garbage", {"applet_stdout": "Welcome to the device\n"}),
    # The construct itself did not run. Real devices answer 0 here even with
    # no applets at all, because the loop's exit code is the last `echo`'s.
    ("nonzero", {"applet_retcode": 2}),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("shape", "kwargs"), _HOSTILE_BATCHES, ids=[r[0] for r in _HOSTILE_BATCHES]
)
async def test_a_batch_that_did_not_measure_the_device_leaves_it_unasked(shape, kwargs, caplog):
    """An untrustworthy answer is an absence of measurement, not a "no".

    THE HAZARD, and it is the expensive direction: all four shapes below reach
    otto as a well-formed, rc-0-ish reply, and reading any of them as answers
    would settle seven capabilities at ``absent`` on the strength of a probe
    that measured nothing -- then offer all seven for pinning into ``lab.json``,
    where nothing would ever revisit them.

    ``is_settled`` is asserted alongside the value because the value alone
    cannot carry this: a consumer that REFUSES has to be able to tell "the
    device has no scp" from "otto never found out".
    """
    runner = _Runner(applets={"base64", "nc"}, **kwargs)
    userland = Userland(UserlandOptions(), runner)

    with caplog.at_level(logging.DEBUG, logger="otto.host.userland"):
        await userland.resolve()

    for applet in PROBED_APPLETS:
        name = applet_capability(applet)
        assert userland.has_applet(applet) == _UNASKABLE[name], (
            f"the {shape} batch was read as an answer for {applet}"
        )
        assert not userland.is_settled(name), (
            f"the {shape} batch settled {applet}, so a refused probe round became "
            f"a verdict about the device"
        )
    assert userland.as_lab_json() == {
        k: v for k, v in _FULLY_DEGRADED.items() if k not in _APPLET_FIELDS
    }, "an unmeasured applet answer reached the pasteable pin"
    _assert_debug_only(caplog)


@pytest.mark.asyncio
async def test_an_untrusted_batch_is_asked_again_once_the_cooldown_expires(monkeypatch):
    """Unasked is provisional. Only a measurement is cached.

    The companion to the row above: leaving the batch unsettled is only the
    right answer if it is also re-asked, or one bad reply pins ``present`` for
    every applet for the object's lifetime -- which is the state a consumer
    keyed on ``is_settled`` would never escape.
    """
    clock = _FakeClock()
    monkeypatch.setattr(userland_module, "_monotonic", clock)
    runner = _Runner(applets={"base64"}, applet_control=False)
    userland = Userland(UserlandOptions(), runner)

    await userland.resolve()
    assert userland.has_applet("scp") == APPLET_PRESENT
    during = len(runner.calls)

    runner.applet_control = True
    clock.now += userland_module._RETRY_COOLDOWN_S
    await userland.resolve()

    assert runner.calls[during:] == [_P_APPLETS], (
        f"the retry issued {runner.calls[during:]}; only the batch was unsettled"
    )
    assert userland.has_applet("scp") == APPLET_ABSENT
    assert userland.has_applet("base64") == APPLET_PRESENT
    assert userland.is_settled(applet_capability("scp"))


@pytest.mark.asyncio
async def test_the_six_that_predate_applets_are_asked_exactly_as_before():
    """No behaviour change for a host that never reads an applet capability.

    The applet batch is appended, not woven in: the eleven commands the six
    fixed capabilities cost are the same commands, in the same order, with the
    same spellings, and the batch is the twelfth. Asserted as a PREFIX rather
    than as a set, because an interleaving that put the batch third would keep
    every set-based assertion in this module green while changing which probes
    a budget-limited host reaches.
    """
    runner = _Runner({_P_TIMEOUT})

    await Userland(UserlandOptions(), runner).resolve()

    assert runner.calls[: len(_PROBES_BEFORE_APPLETS)] == _PROBES_BEFORE_APPLETS
    assert runner.calls[len(_PROBES_BEFORE_APPLETS) :] == [_P_APPLETS], (
        "the applet batch must be the last command and the only one added"
    )


@pytest.mark.asyncio
async def test_every_applet_gets_its_own_debug_line_with_its_own_source(caplog):
    """One line per applet, not one for the batch, and the source is per name.

    The debug lines are what tell a user WHICH answers cost a round trip, and
    the applets are the one group where the answer and the cost do not line up
    one-to-one: seven answers, one command, and some of the seven may be
    declared. A single "applets = ..." line would hide both facts.
    """
    runner = _Runner(applets={"base64"})
    userland = Userland(UserlandOptions(applet_scp=APPLET_ABSENT), runner)

    with caplog.at_level(logging.DEBUG, logger="otto.host.userland"):
        await userland.resolve()

    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("userland: ")]
    assert f"userland: {applet_capability('scp')} = {APPLET_ABSENT} (declared)" in lines
    assert f"userland: {applet_capability('base64')} = {APPLET_PRESENT} (probed)" in lines
    assert f"userland: {applet_capability('nc')} = {APPLET_ABSENT} (probed)" in lines
    _assert_debug_only(caplog)


def test_the_batch_fits_the_line_bound_the_tighter_transport_imposes():
    """The batch is a command, so it is measured against otto's own line guard.

    NOT A LENGTH ASSERTION WITH A NUMBER IN IT. The bound is
    ``ASH_TYPED_LINE_MAX`` minus otto's BEGIN/END framing, and both halves
    already live in ``refuse_if_line_editor_would_truncate`` -- so this runs
    that function over the real emitted command rather than restating either
    one. Adding applets until the batch would truncate reds here, on the guard
    that would have refused it, instead of on a device.

    THE TIGHTER TRANSPORT IS REACHABLE, which is what makes this worth
    asserting at all rather than waving at the 9000-character exec ceiling:
    probes go out through ``Host.exec``, and ``exec`` has no stateless
    primitive on a ``term: telnet`` host or on any host whose login is proxied
    (see the ``run-command-line-length`` record's two OPEN paths), so there it
    is line-edited exactly like a typed command.
    """
    refuse_if_line_editor_would_truncate(AshFrame(), _P_APPLETS, host="probe")
