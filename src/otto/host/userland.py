"""Version- and build-config-dependent facts about a host's userland.

otto's unix path assumes a GNU/coreutils userland with bash. Where that
assumption is wrong, the answer cannot be derived from anything otto declares
— a BusyBox image can have applets compiled out, and ``sh`` may be ash or
hush — so the DEVICE is the only authority. Each capability here is probed
once, cached for the host's lifetime, and degrades to a documented fallback
when the probe cannot answer.

Declared values in ``userland_options`` win outright and skip the probe
entirely, which is the point of declaring them.

Probe traffic runs at ``LogMode.NEVER``: it is otto's internal machinery, not
the user's command output, and it would otherwise dominate a session log.
Probe RESULTS are emitted at DEBUG and only at DEBUG — ``LogMode`` governs
command I/O, not record level, so the level is the only thing keeping these
off the console and out of both log files at otto's default ``INFO``. They are
written in a form that can be pasted into ``lab.json`` to skip the probes next
time.

**One round asks about all six, whatever the caller came for.** There is no
per-capability resolution: :meth:`Userland.resolve` settles everything not
already declared, and every consumer awaits that same whole round. Who reads
what today:

``elevation``
    ``otto.host.privilege.PosixPrivilege._elevate``
``timeout_style``
    ``otto.host.transfer.nc.NcFileTransfer._nc_listener_prefix``
``stat_size``
    ``otto.host.transfer.shell.ShellFileTransfer._run_get`` (via
    ``_remote_size``) — ``transfer/nc.py`` still writes its own
    ``stat -c %s`` at three sites, unrelated to this probe
``base64_flag``
    ``otto.host.transfer.shell.ShellFileTransfer`` (both ``_run_put`` and
    ``_run_get``)
``checksum``
    ``otto.host.transfer.shell.ShellFileTransfer`` (both ``_put_one`` and
    ``_get_one``, for post-transfer integrity verification — ``md5sum`` on
    both sides when present, a byte-size comparison via ``stat_size``
    otherwise)
``shell_dialect``
    nothing yet — an ``ash`` frame is registered, but nothing routes this
    probe's result to it; see the hole below

So the first ``run(sudo=True)`` against a host issues up to eleven probes to
read one answer that probes 1-2 settled. The whole round is deliberate — splitting
it means a second set of exec channels against a server that refuses rather
than queues them (see ``_RETRY_COOLDOWN_S``), a second chance to strand a
capability at its cannot-ask default, and a partial ``userland_options``
payload in the pasteable log line — but the price is real and it is stated
here rather than left to be discovered: **up to ``_RESOLVE_BUDGET_S`` (30s)
on the first call, and again on any later call that falls outside
``_RETRY_COOLDOWN_S`` (60s) while the host is still refusing probes.** That
time is NOT charged to the caller's ``timeout=``; see
:meth:`otto.host.host.BaseHost.run`.

**Known hole: ``shell_dialect`` is measured but not yet consumable.** Every
BusyBox host resolves it to ``"ash"``. An ``ash`` frame is now registered
(:class:`~otto.host.command_frame.AshFrame`, in
``otto.host.command_frame``) and ``build_command_frame("ash")`` succeeds —
that half of the original hole is closed. What remains missing is the
WIRING: no call site in this codebase reads :attr:`Userland.shell_dialect`
and passes it to ``build_command_frame`` — every existing
``build_command_frame`` call site (``models/host.py``, ``unix_host.py``,
``embedded_host.py``) resolves a DECLARED ``command_frame`` string, never
this probe's result. So the resolved value is still a MEASUREMENT to record
and pin, not a frame name any caller looks up through this probe. Until
something does that routing, do not feed :attr:`Userland.shell_dialect` to
the frame registry; the other five are safe to consume today, and now all
five of them have a consumer — see the table above — leaving ``shell_dialect``
the only capability in this module still without one.

Most of the probe COMMAND SPELLINGS here have two other copies, and where a
copy exists all three have to agree: ``tests/busybox/test_applet_contracts.py``
measures them against real BusyBox binaries, and
``tests/unit/host/test_userland.py`` pins the exact list and order this module
issues. They are deliberately NOT shared through an import — a module that
read its spellings from the test that checks them could not be caught
drifting by that test — so each of the three names the other two instead.

That third, Tier 1 copy exists only for spellings with a real ARGUMENT-PARSING
question a BusyBox version could answer differently -- ``timeout``'s
``-t SECS PROG`` vs ``SECS PROG``, ``base64``'s ``-d`` vs ``--decode``,
``stat``'s ``-c %s`` vs ``--format=%s``, ``wc``'s ``-c <`` vs ``-c FILE``.
``elevation`` (a bare ``command -v`` presence check), ``shell_dialect`` (a
shell-builtin variable read), and ``checksum`` (``Userland._probe_checksum``'s
own docstring: one spelling, not a contested pair) have no such question, so
Tier 1 carries no copy of theirs and the two-copy rule applies to them
instead. ``checksum``'s OUTPUT format (lowercase hex, matching
``hashlib.md5().hexdigest()``) is substantiated a different way: Tier 1's
``tests/busybox/test_shell_codec_contracts.py`` runs a real ``md5sum
/tmp/payload.bin`` (not this module's ``< /dev/null`` probe spelling) across
the matrix and compares its first field byte-for-byte -- presence and format,
not this exact probe command.

**The other half of this module is the GAP REGISTRY** (:class:`Gap`,
:data:`GAPS`, :func:`gap_for`, :func:`refuse_if_gapped`, at the bottom of the
file). The capabilities above are what otto ASKS A DEVICE; the registry is what
otto has already MEASURED about a whole class of userland and therefore never
needs to ask again. They sit in one module because they answer the same
question from two directions and a caller usually needs both -- and because a
gap that turns out to be probe-answerable should become a capability here
rather than a second table somewhere else.
"""

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from ..logger.mode import LogMode
from ..result import CommandResult
from .errors import UnsupportedOnUserlandError
from .options import UserlandOptions

_logger = logging.getLogger(__name__)

# Indirected so a test can drive resolution's budget from a fake clock instead
# of by waiting, which is what keeps the budget guard off the wall clock.
_monotonic = time.monotonic

# Every probe is a single short command whose only job is to exit 0 or not.
# Bounded so an unreachable or wedged host fails resolution instead of hanging
# the first command the caller actually cares about. Its PRESENCE on the call
# is load-bearing and is pinned -- but its VALUE is NOT free to widen, and
# claiming so was the same fallacy _RESOLVE_BUDGET_S's own comment below
# corrects for that constant: this one is the OTHER half of the same ratio,
# ceil(_RESOLVE_BUDGET_S / _PROBE_TIMEOUT_S), and widening it shrinks how many
# probes one resolution affords exactly as widening the budget grows that
# count. MEASURED, by editing just this constant and running
# tests/unit/host/test_userland.py: 20.0 keeps all 55 tests green (30/20 still
# affords 2 probes, ceil'd); 31.0 already reds
# test_resolution_stops_once_the_whole_budget_is_spent[as-shipped] -- the
# budget now affords only 1 probe instead of 2, so elevation is no longer
# measured before the budget runs out. Widening this safely means changing
# the constant, then running tests/unit/host/test_userland.py and reading
# what reds; that run IS the guard, same as below.
#
# ENFORCED BY US, not by the callee, and the difference is the whole reason
# this number means anything. `_probe` wraps the call in `asyncio.wait_for`
# AND passes the same number down as `timeout=`. That is not belt-and-braces:
# otto's own `UnixHost.exec` applies its `timeout` to the COMMAND and not to
# establishing the SSH connection the command needs first — measured at over
# 120s for a nominal 2s call against an unroutable address. Delete the wrapper
# and every number below becomes fiction on exactly the hosts it exists for.
_PROBE_TIMEOUT_S = 10.0

# Ceiling on one whole resolution, and the reason both numbers have to be read
# together. THE ARITHMETIC: resolution issues at most eleven probes (2 elevation +
# 3 timeout + 2 base64 + 2 stat + 1 checksum + 1 dialect), so a host that swallows
# every one of them costs 11 x _PROBE_TIMEOUT_S = 110s unbounded. resolve() holds its
# lock for that whole span, so on a concurrent consumer — nc's bulk put fans
# its files out and each one awaits resolution — every queued caller waits the
# full span out BEFORE its own timeout starts counting. This budget converts
# that into a stated bound: the deadline is set once per resolution, each probe
# is granted min(_PROBE_TIMEOUT_S, whatever is left), and a probe with nothing
# left is never sent. So a wedged host costs at most ceil(30/10) = 3 probe
# timeouts, not eleven.
#
# What the unreached capabilities get is a NO-INFORMATION DEFAULT, not a "no"
# (see _UNASKABLE_DEFAULTS), and they are asked again on a later resolve()
# because nothing about them was measured. "Later" is the operative word and
# _RETRY_COOLDOWN_S is what supplies it: without a gap between attempts the
# queued callers this paragraph is about would each start their own, and the
# stated bound would be per CALLER rather than per host. With it, the sentence
# above stays true — one attempt's worth of probes, however wide the fan-out.
#
# CEIL, not floor: a probe the budget can only PARTLY afford is still sent,
# granted the remainder rather than the full _PROBE_TIMEOUT_S, which is why the
# total can never exceed this number even though the count rounds up.
#
# A runaway guard, deliberately generous: a healthy host resolves in well under
# a second, and no assertion reads this VALUE — the budget guard recomputes
# ceil(_RESOLVE_BUDGET_S / _PROBE_TIMEOUT_S) from whatever the two constants
# are, and is parametrized over ratios that do NOT divide evenly, because two
# separate defects once hid behind the fact that these two divide exactly.
#
# WHAT NOT TO DO WHEN WIDENING THIS: state a "safe up to N seconds" number in
# this comment. An earlier version of this note did exactly that (first 90s,
# then "corrected" to 100s) and BOTH were wrong -- measured directly by
# editing just this constant and running tests/unit/host/test_userland.py:
# 40.0 keeps all 55 tests green; 41.0 already reds
# test_resolution_stops_once_the_whole_budget_is_spent[as-shipped] on one
# assertion; 90.0 reds the SAME row on a DIFFERENT assertion (see 1. below --
# one test, two distinct ways to fail it, not two tests); 100.0 adds a second
# test failing; 101.0 adds a third. Neither prior number was ever guarded by
# an assertion that reads it, which is exactly how both survived being wrong
# until someone happened to measure them.
#
# There is no clean replacement formula, because the real ceiling is the
# INTERACTION of three unrelated mechanisms in three different tests, not one
# arithmetic bound on probe count:
#
# 1. test_resolution_stops_once_the_whole_budget_is_spent's "as-shipped" row
#    hard-codes what ONE SPECIFIC scripted device (answers only
#    `command -v timeout`) measures at the SHIPPED budget. Widening the
#    budget alone -- with that row's own device script unchanged -- lets
#    LATER probes in that script actually run: at affordable=5,
#    timeout_style stops being merely assumed and becomes genuinely
#    MEASURED (as "absent" -- the same value the assumed default already
#    had, but now a settled one, which changes what as_lab_json() reports),
#    reddening the row's own pasteable-dict assertion (41.0); at affordable=9,
#    stat_size is fully measured too, this time to a VALUE that actually
#    differs from its assumed default ("absent" vs "stat"), reddening the
#    row's whole-map _answers() assertion for an unrelated reason (90.0).
# 2. test_a_wedged_host_is_not_amplified_by_the_fan_out scripts a device
#    where EVERY probe is unreachable. Against that script,
#    `_probe_timeout` short-circuits (`if present is None: return None`)
#    the moment `command -v timeout` itself cannot be asked, so the whole
#    resolution can only ever ISSUE 9 commands (2 elevation + 1 timeout +
#    2 base64 + 2 stat + 1 checksum + 1 dialect -- confirmed directly from
#    the failing assertion's own captured call list), never 11, no matter
#    how much budget the caller affords. Once the budget affords 10 or
#    more, the test's own `affordable = ceil(budget / probe)` prediction
#    exceeds what the algorithm can structurally send, and
#    `len(runner.calls) == affordable` reds on a probe-tree ceiling that
#    has nothing to do with the time budget (100.0).
# 3. test_every_logger_call_site_is_exercised_at_debug is the third red, at
#    101.0, and it breaks for a reason that has nothing to do with either
#    mechanism above. The "resolution budget spent" line (`_probe`, guarding
#    `if remaining <= 0`) is only emitted when a probe is skipped for lack of
#    budget. Once the budget affords every probe -- which is exactly what (2)
#    measures becoming structurally true once affordable reaches 10 -- nothing
#    is ever skipped, that line is never emitted, and the log-site coverage
#    guard reds reporting a call site "never exercised". BEWARE when chasing
#    this one: the failure message is about logging and reads like a logging
#    defect; the actual cause is this constant. Check the budget row of
#    test_resolution_stops_once_the_whole_budget_is_spent (and mechanism 2
#    above) before believing the message.
#
# All three are properties of their SCRIPTS (and, for 3., of what (2) does
# structurally), not of this constant in general -- a differently-scripted
# device would move any of the three boundaries. So widening this safely
# means changing the constant, then running tests/unit/host/test_userland.py
# and reading what reds; that run IS the guard, and no number typed into this
# comment can substitute for it.
_RESOLVE_BUDGET_S = 30.0

# What each capability answers when its probes could not be ASKED — the
# transport raised, or the budget above ran out before the command was sent.
#
# NOT the same as the answer for a device that says "no", and conflating the
# two is the defect this table exists to prevent. A probe that exits non-zero
# is a measurement; a probe that never arrived is an absence of one, and every
# value here is therefore WHAT OTTO DID BEFORE IT ASKED ANYTHING:
#
# ``elevation``
#     ``sudo``, which is what :meth:`~otto.host.privilege.PosixPrivilege._elevate`
#     still builds for a host with no resolver at all. The alternative is an
#     outage: an sshd at its ``MaxSessions`` ceiling REFUSES an exec channel
#     rather than queueing it, and reading that refusal as "no sudo here"
#     turns one bad moment into a permanent refusal of every elevated command.
# ``timeout_style``
#     ``absent`` — no prefix, which is the listener nc spawned before the CAP
#     existed. Say the baseline precisely, because there are two and only one
#     of them is flattering: before the cap there was no cap, but the cap's
#     first implementation resolved the convention IN-BAND, spliced into the
#     spawn command, so it could only be lost on a device that genuinely had
#     no usable ``timeout``. Out-of-band, this default is also what a host
#     WITH ``timeout`` gets when its probe round was refused — and for up to
#     ``_RETRY_COOLDOWN_S`` after. That is a real capability regression in
#     that one case, taken because the alternative is worse: guessing a
#     spelling builds ``timeout 3600 nc -l`` on a ``-t`` host, the applet
#     fails to exec ``3600``, and nothing listens at all. See
#     ``NcFileTransfer._nc_listener_prefix``, which enumerates the four ways
#     the cap is lost.
# ``base64_flag``
#     ``absent``. Nothing consumes it yet, so the conservative answer wins:
#     claiming a decode flag works builds a command that fails.
# ``stat_size``
#     ``stat``. ``transfer/nc.py`` has always sized remote files with
#     ``stat -c %s`` (three call sites), so this is the status quo; ``absent``
#     would be a capability regression rather than a neutral guess.
# ``checksum``
#     ``absent`` — and the reasoning here is the OPPOSITE of ``stat_size``'s.
#     ``stat_size``'s default encodes the status quo (nc already assumed
#     ``stat`` before this table existed); ``checksum`` has no status quo to
#     preserve, because nothing consumed a checksum capability before this
#     one was added. What ``absent`` actually buys is degrading to the check
#     that ALWAYS works: :class:`~otto.host.transfer.shell.ShellFileTransfer`
#     falls back to a byte-size comparison via ``stat_size`` when
#     ``checksum == "absent"``, and that fallback never depends on a binary
#     otto never confirmed exists. Defaulting to ``"md5sum"`` instead would
#     do the opposite of degrading safely: an unasked host would have a
#     transfer emit ``md5sum`` on faith, and on the host that could not even
#     answer the probe that command is exactly the one likely to 127 —
#     turning a transfer that actually landed correctly into a reported
#     failure, not a weaker check.
# ``shell_dialect``
#     ``bash``. otto's unix path has always assumed it, and no ``CommandFrame``
#     is registered under ``ash`` at all — see the module docstring's hole.
#
# These are provisional: an unasked capability is not recorded as settled, so
# ``resolve()`` asks again on the next call. See :meth:`Userland.resolve`.
_UNASKABLE_DEFAULTS = {
    "elevation": "sudo",
    "timeout_style": "absent",
    "base64_flag": "absent",
    "stat_size": "stat",
    "checksum": "absent",
    "shell_dialect": "bash",
}

# Minimum gap between resolution attempts once one has left something unasked.
#
# WHY A RETRY NEEDS A BOUND AT ALL. Re-asking an unsettled capability is what
# stops a blip becoming permanent, but the consumers call resolve() per unit of
# work — nc's bulk put re-enters prepare() once per FILE — so on a host that
# answers nothing, every caller is entitled to its own attempt. Measured
# without this constant: 12 concurrent callers cost 36 probes over 360s where
# one costs 3 over 30s.
#
# The reason that is not merely expensive is that the trigger and the cost are
# THE SAME RESOURCE. The thing that leaves a key unsettled is most often an
# sshd at its `MaxSessions` ceiling refusing an exec channel — it refuses, it
# does not queue — and an unbounded retry answers by opening more channels into
# the server already refusing them. Bounding by caller count would not help
# either, because the fan-out width is the caller's choice.
#
# THE ARITHMETIC: an attempt costs at most ceil(_RESOLVE_BUDGET_S /
# _PROBE_TIMEOUT_S) = 3 probes and _RESOLVE_BUDGET_S = 30s. One attempt is
# allowed per cooldown window, whatever the fan-out, so a wedged host costs at
# most 3 probes per 60s — a duty cycle of half one resolution's worth of
# channel traffic, and independent of how many files the caller passed.
#
# 60s is a runaway guard, not a discriminator: no assertion reads the VALUE
# (the guard advances a fake clock by whatever this is), so it can be widened
# freely. It must stay >= _RESOLVE_BUDGET_S or attempts chain back to back and
# the cooldown stops separating them. Widening only delays recovery from a
# blip, and what it delays TO is the pre-branch behaviour — the assumed
# defaults are exactly what otto did before it asked anything — so the cost of
# a long window is small and the cost of a short one is the amplification
# above.
_RETRY_COOLDOWN_S = 60.0


class Userland:
    """Resolved userland capabilities for one host."""

    def __init__(
        self,
        options: UserlandOptions,
        run: Callable[..., Coroutine[Any, Any, CommandResult]],
    ) -> None:
        self._options = options
        self._run = run
        self._resolved: dict[str, str] = {}
        # Keys whose value came from a declaration or a real answer, as opposed
        # to _UNASKABLE_DEFAULTS. Only these make resolve() idempotent.
        self._settled: set[str] = set()
        self._lock = asyncio.Lock()
        self._deadline = 0.0
        # Monotonic time before which another attempt is refused. Zero until an
        # attempt leaves something unasked, so the first caller never waits.
        self._retry_after = 0.0

    async def _probe(self, cmd: str) -> bool | None:
        """Report whether *cmd* exits 0, or ``None`` when it could not be asked.

        On ``rc == 0`` and nothing else. The same "this does not work" outcome
        reaches otto as 127 from the shell when an applet is absent and as 1
        from the applet when a flag is rejected (both measured across the
        BusyBox matrix), so a probe keyed to either code misclassifies the
        other.

        **A probe that could not run is not a NO — it is nothing.** An earlier
        version returned ``False`` there, reasoning that resolution is an
        adaptation step and the real command would report a broken transport
        with its own context. That reasoning holds for a capability whose
        fallback still runs; it collapses for one that GATES the operation,
        because then the real command never runs and there is nothing left to
        report. Measured: with only the first probe refused on an otherwise
        healthy sudo host, elevation resolved to ``su`` and stayed there for
        the object's lifetime. Callers turn ``None`` into no conclusion for
        the whole capability, and :meth:`resolve` declines to cache it.

        Cancellation is not caught — ``CancelledError`` is a ``BaseException``
        — so a shutdown still ends resolution immediately instead of waiting
        out the remaining probes.

        **The grant is enforced here, not delegated.** ``asyncio.wait_for``
        wraps the call and the same number is passed down as ``timeout=``,
        which looks redundant and is not: otto's own ``UnixHost.exec`` applies
        its ``timeout`` to the COMMAND and not to establishing the connection
        the command needs first — measured at over 120s for a nominal 2s call
        against an unroutable address. Without the wrapper every number in
        ``_RESOLVE_BUDGET_S``'s arithmetic is fiction. The kwarg stays because
        a callee that CAN bound itself should, and because it is what lets the
        remote command be abandoned cleanly rather than orphaned.
        """
        remaining = self._deadline - _monotonic()
        if remaining <= 0:
            _logger.debug("userland: resolution budget spent before %r; leaving it unasked", cmd)
            return None
        grant = min(_PROBE_TIMEOUT_S, remaining)
        try:
            result = await asyncio.wait_for(
                self._run(cmd, log=LogMode.NEVER, timeout=grant), timeout=grant
            )
        except Exception as exc:  # noqa: BLE001 — a probe that cannot run is an absence of measurement, not an error
            _logger.debug("userland: probe %r could not be asked (%s)", cmd, exc)
            return None
        return result.retcode == 0

    async def resolve(self) -> None:
        """Settle every capability not already declared. Idempotent once settled.

        Idempotent on what is SETTLED — declared, or answered by the device —
        rather than on having run before. A capability whose probes could not
        be asked holds a provisional value from ``_UNASKABLE_DEFAULTS`` and is
        asked again on the next call, so a transport blip during the first
        elevated command does not pin a guess for the object's lifetime.
        Nothing else would ever undo it: not a later command, not
        ``rebuild_connections()``, nothing short of a new host object.

        The retry is per capability, not per round. A device that answered
        five of six has already paid for those five, and re-issuing them
        would put probe traffic back on the fan-out path the lock protects.

        It is also RATE LIMITED, by ``_RETRY_COOLDOWN_S``, and that bound is
        load-bearing rather than tidy. The consumers call this per unit of work
        — nc's bulk put re-enters ``prepare()`` once per file — so on a host
        that answers nothing, "everyone may retry" means the probe traffic
        scales with the caller's fan-out. Worse, the thing that usually leaves
        a key unsettled is a server refusing an exec channel, so the retry
        would answer a refusal with more channels. Inside the window, callers
        get the values already resolved and issue nothing.

        Serialized, and the checks are INSIDE the lock: concurrent first
        callers are the normal case for the consumers, and a check-then-probe
        would let all of them past and multiply the probe traffic by the
        fan-out — against a server that refuses excess SSH channels rather
        than queueing them.

        **WHAT A CALLER PAYS.** All six capabilities, not the one it came
        for: there is no scoped form of this call, so ``run(sudo=True)``
        issues up to eleven probes to read ``elevation``, which probes 1-2
        settled. On a healthy host that is eleven fast round trips; on a
        refusing one it is up to ``_RESOLVE_BUDGET_S`` (30s), and up to that
        again on the next call outside ``_RETRY_COOLDOWN_S`` (60s). None of it
        is charged to the caller's ``timeout=`` — ``BaseHost.run`` awaits this
        above the single-vs-sequence split, so ``run("reboot", sudo=True,
        timeout=10.0)`` is a call of up to 40s on a host that answers nothing.
        The module docstring's table says which capabilities are read at all
        and why the round is whole anyway.
        """
        async with self._lock:
            if len(self._settled) == len(_UNASKABLE_DEFAULTS):
                return
            if _monotonic() < self._retry_after:
                return
            await self._resolve_once()

    async def _resolve_once(self) -> None:
        """Settle whatever is not settled yet and record the answers. Caller holds the lock.

        Both maps are built as locals and published together at the end, so a
        ``CancelledError`` part-way through leaves the object exactly as it
        was rather than half-resolved — which is what keeps ``_get``'s
        "read before resolve()" error honest.
        """
        self._deadline = _monotonic() + _RESOLVE_BUDGET_S
        resolved = dict(self._resolved)
        settled = set(self._settled)
        probes = (
            ("elevation", self._probe_elevation),
            ("timeout_style", self._probe_timeout),
            ("base64_flag", self._probe_base64),
            ("stat_size", self._probe_stat),
            ("checksum", self._probe_checksum),
            ("shell_dialect", self._probe_dialect),
        )
        sources: dict[str, str] = {}
        for name, probe in probes:
            if name in settled:
                continue
            declared = getattr(self._options, name)
            if declared:
                resolved[name], sources[name] = declared, "declared"
                settled.add(name)
                continue
            answer = await probe()
            if answer is None:
                resolved[name], sources[name] = _UNASKABLE_DEFAULTS[name], "assumed"
            else:
                resolved[name], sources[name] = answer, "probed"
                settled.add(name)
        self._resolved = resolved
        self._settled = settled
        if len(settled) < len(_UNASKABLE_DEFAULTS):
            self._retry_after = _monotonic() + _RETRY_COOLDOWN_S
        for name in sorted(sources):
            _logger.debug("userland: %s = %s (%s)", name, resolved[name], sources[name])
        _logger.debug(
            'userland: pin these to skip the probes -- "userland_options": %s',
            json.dumps(self.as_lab_json(), sort_keys=True),
        )

    async def _probe_elevation(self) -> str | None:
        """``sudo`` > ``su`` > ``none``, or None when the preference cannot be settled.

        The arms are a PREFERENCE, not two spellings of one thing: ``sudo``
        authenticates as the user already logged in and ``su -c`` as the
        account being entered, whose password the lab may not hold. So a ``su``
        that answers yes says nothing about which mechanism otto should reach
        for while ``sudo`` is unknown, and ``none`` — the one answer that makes
        ``_elevate`` refuse outright — may only be reported when BOTH arms
        genuinely answered no.

        ``su`` is still asked when the sudo arm could not be, rather than
        short-circuiting: a host can recover mid-resolution, and abandoning
        the round on the first failure is a separate defect this module is
        already guarded against.
        """
        sudo = await self._probe("command -v sudo")
        if sudo:
            return "sudo"
        su = await self._probe("command -v su")
        if su:
            return None if sudo is None else "su"
        if sudo is None or su is None:
            return None
        return "none"

    async def _probe_timeout(self) -> str | None:
        # Order does not matter between the two SPELLINGS: each implementation
        # rejects the other's, so the arms are mutually exclusive and converge
        # either way round. Measured against real BusyBox binaries, and pinned
        # by the mutual-exclusivity assertion in
        # ``tests/busybox/test_applet_contracts.py`` (see the module
        # docstring's note on where the three copies of these spellings live).
        # Being mutually exclusive is also why a spelling that answers yes is
        # a conclusion even if its sibling could not be asked.
        present = await self._probe("command -v timeout")
        if present is None:
            return None
        if not present:
            return "absent"
        coreutils = await self._probe("timeout 1 true")
        if coreutils:
            return "coreutils"
        dash_t = await self._probe("timeout -t 1 true")
        if dash_t:
            return "dash-t"
        if coreutils is None or dash_t is None:
            return None
        return "absent"

    async def _probe_base64(self) -> str | None:
        short = await self._probe("echo aGk= | base64 -d")
        if short:
            return "-d"
        long = await self._probe("echo aGk= | base64 --decode")
        if long:
            return "--decode"
        if short is None or long is None:
            return None
        return "absent"

    async def _probe_stat(self) -> str | None:
        # Unlike elevation's, these arms are interchangeable — both answer with
        # a byte count — so the fallback winning while the preferred arm is
        # unknown is a usable measurement, not a guess.
        stat = await self._probe("stat -c %s /dev/null")
        if stat:
            return "stat"
        wc = await self._probe("wc -c < /dev/null")
        if wc:
            return "wc"
        if stat is None or wc is None:
            return None
        return "absent"

    async def _probe_checksum(self) -> str | None:
        # One spelling only, unlike stat_size's stat/wc pair: there is no
        # second, widely-available checksum tool this module falls back to,
        # so a single probe answers the whole capability the same way
        # _probe_dialect's single `$BASH_VERSION` check does. `< /dev/null`
        # is a redirect, feeding md5sum's stdin -- matching wc's probe just
        # above, not stat's, which passes `/dev/null` as a plain positional
        # argument and reads no stdin at all. `_probe` only checks the exit
        # code (see its own docstring), never the output, so a positional
        # `md5sum /dev/null` would answer identically; redirect form was
        # chosen only to match the OTHER redirect-based probe in this set.
        md5sum = await self._probe("md5sum < /dev/null")
        if md5sum is None:
            return None
        return "md5sum" if md5sum else "absent"

    async def _probe_dialect(self) -> str | None:
        # Behaviour, not a name: BusyBox ships both ash and hush and `sh` may
        # be either, so `$BASH_VERSION` presence is the only thing that
        # actually distinguishes the frame otto should use.
        bash = await self._probe('test -n "$BASH_VERSION"')
        if bash is None:
            return None
        return "bash" if bash else "ash"

    def _get(self, name: str) -> str:
        if not self._resolved:
            raise RuntimeError(f"Userland.{name} read before resolve()")
        return self._resolved[name]

    @property
    def elevation(self) -> str:
        """``"sudo"`` | ``"su"`` | ``"none"``."""
        return self._get("elevation")

    @property
    def timeout_style(self) -> str:
        """``"coreutils"`` | ``"dash-t"`` | ``"absent"``."""
        return self._get("timeout_style")

    @property
    def base64_flag(self) -> str:
        """``"-d"`` | ``"--decode"`` | ``"absent"``."""
        return self._get("base64_flag")

    @property
    def stat_size(self) -> str:
        """``"stat"`` | ``"wc"`` | ``"absent"``."""
        return self._get("stat_size")

    @property
    def checksum(self) -> str:
        """``"md5sum"`` | ``"absent"``.

        Consumed by :class:`~otto.host.transfer.shell.ShellFileTransfer` to
        verify a PUT or GET landed intact: ``md5sum`` on both sides when this
        resolves to ``"md5sum"``, a byte-size comparison via
        :attr:`stat_size` when it resolves to ``"absent"``.
        """
        return self._get("checksum")

    @property
    def shell_dialect(self) -> str:
        """The measured shell dialect — ``"bash"`` | ``"ash"``.

        An ``ash`` ``CommandFrame`` is now registered (``AshFrame``), so
        ``build_command_frame(self.shell_dialect)`` would no longer raise on
        the common BusyBox case — but nothing calls it that way yet. See the
        module docstring's hole note before wiring this property to a
        consumer.
        """
        return self._get("shell_dialect")

    def as_lab_json(self) -> dict[str, str]:
        """Return the SETTLED answers, as a table a user may safely pin.

        Deliberately not the complete map — read the properties for that. What
        this returns is what was declared or actually measured, and the
        difference matters because of what pinning MEANS: a declared value is
        settled forever and never re-probed. An assumed answer offered here
        would invite a user to make permanent, by hand and in their own lab
        data, exactly the stale verdict the tri-state probe exists to prevent
        — and inside a JSON payload a guess is indistinguishable from a
        measurement, whatever the surrounding log lines say.

        A partial table is the right output and not a degraded one: the keys
        it omits stay ``None`` in ``lab.json``, which means "ask the device",
        which is precisely what otto could not do this time.
        """
        return {k: v for k, v in sorted(self._resolved.items()) if k in self._settled}


# ===========================================================================
# The gap registry
# ===========================================================================
#
# ONE SOURCE OF TRUTH FOR THREE AUDIENCES.
# ``docs/superpowers/specs/2026-08-11-busybox-host-support-design.md`` §4:
# what otto knows about a non-GNU userland has to reach the runtime error, a
# user-facing docs page, and the parity queue -- and written three times it
# drifts three ways. It is written once, here, and the other two READ these
# records:
#
# 1. the runtime error -- :meth:`~otto.host.errors.UnsupportedOnUserlandError
#    .for_gap` renders its message from a record rather than from the caller's
#    f-string, so the surface, the evidence and the docs anchor arrive
#    together instead of as a bare ``sudo: not found``;
# 2. the docs page -- ``GAP_DOCS_PAGE``, one table row per record, anchored at
#    :attr:`Gap.docs_anchor`, with a test pinning the table to this list in
#    both directions;
# 3. the parity queue -- :attr:`Gap.queued_for` names the workstream that
#    would close each gap, so ``todo/busybox-parity-sweep-2026-08-11.md`` and
#    ``todo/busybox-tier3-fidelity-2026-08-13.md`` carry the PLAN for the work
#    and this table carries the FACT. Neither restates the other.
#
# THE FIRING RULE, verbatim from the spec, and it is the whole design:
#
#     **Measured-broken refuses up front; unmeasured runs.**
#
# A surface measured broken on the matrix raises
# :exc:`~otto.host.errors.UnsupportedOnUserlandError` immediately rather than
# emitting a command guaranteed to fail confusingly. A surface merely UNTESTED
# is not blocked: it runs, and the outcome is the measurement. Blocking
# untested surfaces would convert "we do not know" into "does not work" -- a
# lie in the expensive direction, which makes otto refuse things that work.
# :func:`refuse_if_gapped` is the only place that rule is spelled out in code,
# and :attr:`Gap.refuses` is the only place it is decided.
#
# WHAT MAY BE WRITTEN HERE. Measurements, not predictions. The spec's
# design-time list was a survey's guesses; every ``measured-broken`` record
# below instead names a command that was RUN and what it answered, and the
# three design-time candidates measurement did NOT support are recorded in the
# comment block below :data:`GAPS` rather than quietly dropped.
#
# KNOWN HOLE, in the same sense as the ``shell_dialect`` hole above: NO
# PRODUCT CALL SITE CONSULTS :func:`refuse_if_gapped` YET. The refusals that
# fire today (``PosixPrivilege._elevate``,
# ``ShellFileTransfer._run_put``/``_run_get``) are PROBE-driven -- they refuse
# on what this host answered, which is a different trigger from this table's
# "otto measured this on the matrix" -- and rewiring them is a behaviour
# change that belongs with the call site being changed, not with the table
# landing. So today this registry is a declared, tested, renderable record
# that the docs page and the parity queue consume; wiring a raise site to it
# is a per-surface decision, and the record's ``reason`` is written to say
# what such a call site would refuse.

GAP_DOCS_PAGE = "docs/architecture/subsystems/busybox-support.md"
"""Where the user-facing rendering of this table lives.

Named here rather than in the docs test so that a page MOVE is one edit and
every rendered error message follows it. ``tests/unit/test_docs_gap_sync.py``
resolves the page through this constant and pins its table to :data:`GAPS` in
both directions, so the anchors below are checked rather than promised.

It sits under ``subsystems/`` because that is where the architecture toctree
keeps its per-area pages; the top level of ``docs/architecture/`` is
cross-cutting material only.
"""

MEASURED_BROKEN = "measured-broken"
"""Ran it, watched it fail, recorded what it said. This status REFUSES."""

UNTESTED = "untested"
"""Nobody has run it on this class of userland. This status does NOT refuse."""

_STATUSES = [MEASURED_BROKEN, UNTESTED]

# Anchor-safe by construction, because :attr:`Gap.docs_anchor` puts it after a
# `#`. Validated rather than trusted: a surface with a space or a backtick in
# it renders a docs link that silently goes nowhere, and a link that goes
# nowhere is exactly the drift this table exists to prevent.
_SURFACE_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


@dataclass(frozen=True)
class Gap:
    """One thing otto cannot do on a userland, with the evidence behind it.

    Frozen because every consumer reads and none writes: the docs page renders
    it, the error renders it, and the parity queue points at it.

    The invariant tying :attr:`status` to :attr:`measured_on` is enforced in
    ``__post_init__`` rather than left to review, because it IS the firing
    rule in data form: a record that refuses must carry the measurement that
    earns the refusal, and a record that carries no measurement must not
    refuse. Getting that pair wrong in either direction is the only way this
    table can lie. It is checked at CONSTRUCTION, which for the declared table
    means at import: a malformed record is a broken build, loudly, rather than
    a surprise the first time someone is refused.
    """

    surface: str
    """Stable, anchor-safe id -- ``[a-z0-9]`` words joined by ``-``.

    The lookup key, the docs anchor, and the name the error message prints.
    Not prose: it has to survive being pasted into a URL fragment.
    """

    status: str
    """:data:`MEASURED_BROKEN` or :data:`UNTESTED`. Decides whether this refuses."""

    reason: str
    """What breaks (or what is unknown), and what a caller should do instead.

    Written for the person reading the exception, so it names otto's own
    surface and the device's answer, not the internal that noticed.
    """

    measured_on: str
    """What was run, against which artifacts, and when -- the evidence.

    EMPTY for :data:`UNTESTED` and non-empty for :data:`MEASURED_BROKEN`, as
    ``__post_init__`` enforces. "We measured this on 2026-08-13" with no
    command named is not evidence; every string in the table names the
    artifact rows and the output they gave.
    """

    queued_for: str
    """The workstream that would close this gap, or why nothing is queued.

    The parity queue's entry point. Never empty: "nobody owns this" is itself
    an answer a reader needs, and it is spelled out rather than left blank.
    """

    def __post_init__(self) -> None:
        if not _SURFACE_RE.match(self.surface):
            raise ValueError(
                f"gap surface {self.surface!r} is not anchor-safe: it must be lowercase "
                f"`[a-z0-9]` words joined by `-`, because it is used verbatim as the "
                f"fragment of {GAP_DOCS_PAGE}#<surface>"
            )
        if self.status not in _STATUSES:
            raise ValueError(
                f"gap {self.surface!r} has status {self.status!r}, not one of {_STATUSES}"
            )
        if not self.reason or not self.queued_for:
            raise ValueError(
                f"gap {self.surface!r} needs both a reason and a queued_for; a gap with no "
                f"stated consequence and no stated owner is a note, not a record"
            )
        # The firing rule, as an invariant on the data rather than a comment.
        if self.status == MEASURED_BROKEN and not self.measured_on:
            raise ValueError(
                f"gap {self.surface!r} is {MEASURED_BROKEN!r} and carries no measurement. "
                f"Measured-broken refuses up front, so the refusal has to be earned by a "
                f"command that was actually run; declare it {UNTESTED!r} instead"
            )
        if self.status == UNTESTED and self.measured_on:
            raise ValueError(
                f"gap {self.surface!r} is {UNTESTED!r} and carries a measurement "
                f"({self.measured_on!r}). Something measured is not untested -- if the "
                f"measurement showed it broken, declare it {MEASURED_BROKEN!r}"
            )

    @property
    def refuses(self) -> bool:
        """Whether this gap blocks the call. THE firing rule, decided once.

        ``True`` only for :data:`MEASURED_BROKEN`. Every other value --
        including a status this build has never heard of, were one to reach
        here past ``__post_init__`` -- answers ``False``, because the
        expensive mistake is refusing something that works.
        """
        return self.status == MEASURED_BROKEN

    @property
    def docs_anchor(self) -> str:
        """:data:`GAP_DOCS_PAGE` plus ``#<surface>`` -- where a user reads more."""
        return f"{GAP_DOCS_PAGE}#{self.surface}"


# The records. ORDER IS THE DOCS TABLE'S ORDER, so it is grouped the way a
# reader meets these: the file-moving surfaces first (the ones the `shell`
# backend exists for), then the command surfaces, then what is merely unknown.
#
# Every ``measured_on`` string names a command and its answer. The five matrix
# artifacts are 1.16.1, 1.21.1, 1.28.1, 1.31.0 and 1.35.0 -- the same rows
# `tests/_fixtures/busybox.py`'s BUSYBOX_MATRIX fetches and pins.
GAPS: list[Gap] = [
    Gap(
        surface="shell-transfer-base64",
        status=MEASURED_BROKEN,
        reason=(
            "the `shell` transfer backend encodes every chunk with the device's own "
            "`base64`, so a userland with no `base64` applet cannot use it at all. "
            "Nothing is attempted: on such a device every file of the batch would fail "
            "identically. Use a backend the device supports, or install base64 on it."
        ),
        measured_on=(
            "BusyBox 1.16.1 ships no `base64` applet -- `tests/busybox/"
            "test_applet_resolution.py`'s `_EXPECTED_BASE64` records False for that row "
            "and `tests/busybox/test_shell_codec_contracts.py`'s `_EXPECTED_BASE64_FLAG` "
            "records None, while 1.21.1 and every later matrix row decode with `-d`"
        ),
        queued_for=(
            "the full-parity workstream, `todo/busybox-parity-sweep-2026-08-11.md`: "
            "`uuencode`/`uudecode` is measured-feasible on all five rows including "
            "1.16.1, and needs a codec probe plus a second codec path in the backend"
        ),
    ),
    Gap(
        surface="file-ops-base64",
        status=MEASURED_BROKEN,
        reason=(
            "`Host.read_file` and `Host.write_file` move their payload through the "
            "device's `base64` too, but unlike the `shell` transfer they hard-code it: "
            "`src/otto/host/file_ops.py` emits `base64 <path>` and `... | base64 -d` "
            "without ever reading `Userland.base64_flag`. So on a userland with no "
            "`base64` they cannot refuse up front and cannot adapt -- the caller gets "
            "the device's own `not found`, attributed to the file it asked for"
        ),
        measured_on=(
            "the same 1.16.1 rows as `shell-transfer-base64`, against these two call "
            "sites: `src/otto/host/file_ops.py:131` (read) and `:156` (write). Both "
            "read as unconditional in the source: no `Userland` is consulted on either "
            "path"
        ),
        queued_for=(
            "the full-parity workstream, `todo/busybox-parity-sweep-2026-08-11.md`: the "
            "codec probe queued for `shell-transfer-base64` is what these two would "
            "read, so the two are one change and not two"
        ),
    ),
    Gap(
        surface="sftp-transfer",
        status=MEASURED_BROKEN,
        reason=(
            "the `sftp` transfer backend needs a server-side sftp subsystem, and a "
            "stock BusyBox userland ships none. Note what does NOT decide this: the "
            "ssh daemon. Packaged dropbear serves sftp perfectly well when the machine "
            "provides an sftp-server binary, so the question is what the DEVICE has, "
            "not which daemon answered. Use the `shell` backend"
        ),
        measured_on=(
            "Tier 3, 2026-08-13: an `sftp` session into the pinned BusyBox root fails "
            "with `/bin/sh: /usr/lib/sftp-server: not found` -- ash inside the chroot, "
            "not the host's shell. See `tests/busybox/test_tier3_session.py::"
            "test_sftp_and_scp_are_both_refused_inside_the_root`"
        ),
        queued_for=(
            "nothing, deliberately: the `shell` backend is the answer for these devices "
            "and it is verified over real ssh in Tier 3 (spec exit criterion 3)"
        ),
    ),
    Gap(
        surface="scp-transfer",
        status=MEASURED_BROKEN,
        reason=(
            "the legacy `scp` protocol needs an `scp` binary on the far side, and a "
            "stock BusyBox userland has none. Same caveat as `sftp-transfer`: the "
            "daemon is not the authority, the device's userland is. Use the `shell` "
            "backend"
        ),
        measured_on=(
            "Tier 3, 2026-08-13: `scp -O` into the pinned BusyBox root fails with "
            "`/bin/sh: scp: not found`, and the file does not land. Same test as "
            "`sftp-transfer`; the two take different routes on purpose, since `scp -O` "
            "reaches for a remote binary while `sftp` opens a subsystem"
        ),
        queued_for=(
            "nothing, deliberately: the `shell` backend is the answer for these devices "
            "and it is verified over real ssh in Tier 3 (spec exit criterion 3)"
        ),
    ),
    Gap(
        surface="nc-transfer",
        status=MEASURED_BROKEN,
        reason=(
            "the `nc` transfer backend cannot drive BusyBox's own `nc` APPLET: it sends "
            "with `nc -N <ip> <port>` and listens OpenBSD-style with `nc -l <port>`, and "
            "the applet supports neither spelling. A BusyBox device with a real OpenBSD "
            "netcat installed alongside is fine -- point `NcOptions.exec_name` at it -- "
            "so this is a gap in the applet, not in every BusyBox host"
        ),
        measured_on=(
            "the five matrix artifacts, 2026-08-13: `nc -N 127.0.0.1 1` is rejected on "
            "every row (`nc: invalid option -- N` on 1.16.1 and 1.21.1, `nc: "
            "unrecognized option: N` on 1.28.1, 1.31.0 and 1.35.0), and every row's own "
            "usage line spells the listener `nc [OPTIONS] -l -p PORT`. otto emits `-N` "
            "at `src/otto/host/transfer/nc.py:1106`"
        ),
        queued_for=(
            "the full-parity workstream, `todo/busybox-parity-sweep-2026-08-11.md`: the "
            "spec queues a BusyBox `nc` variant (`-l -p PORT`, size-terminated reads to "
            "replace the missing `-N`) and explicitly keeps it out of the phases that "
            "built the `shell` backend"
        ),
    ),
    Gap(
        surface="daemon-launch",
        status=MEASURED_BROKEN,
        reason=(
            "`otto.host.daemon.launch_command` wraps every daemon in "
            '`setsid bash -c \'exec -a "$1" "${@:2}"\' _ <sentinel> <argv>` so the '
            "process carries a findable `argv[0]`, and a stock BusyBox userland has no "
            "bash. The body is not portable to ash either, so this is not a `bash`->`sh` "
            "substitution: it needs a different argv[0] mechanism"
        ),
        measured_on=(
            "the five matrix artifacts, 2026-08-13. `bash` is not an applet on any row, "
            "and Tier 3 measures the pinned root as having no `/usr/bin` at all "
            "(`tests/busybox/test_tier3_session.py`). Running the wrapper body under "
            "each row's own ash instead: 1.16.1 and 1.21.1 answer `ash: exec: line 1: "
            "-a: not found`, having no `exec -a`; 1.28.1, 1.31.0 and 1.35.0 DO parse "
            '`exec -a` and then mis-expand `"${@:2}"` into a substring of `$1` -- with '
            "`$1=SENTINEL` the launch execs `NTINEL` and answers `_: exec: line 1: "
            "NTINEL: not found`. So the naive fix trades a clean `not found` for a "
            "corrupted program name"
        ),
        queued_for=(
            "the full-parity workstream, `todo/busybox-parity-sweep-2026-08-11.md`. Not "
            "yet written up there: this table is the record, and the queue file carries "
            "the plan for work that has one"
        ),
    ),
    Gap(
        surface="shutdown-command",
        status=MEASURED_BROKEN,
        reason=(
            "`Host.shutdown()` emits `shutdown -h now` (`src/otto/host/unix_host.py:912`) "
            "and BusyBox has no `shutdown` applet; the BusyBox spelling is `poweroff`. "
            "`Host.reboot()` is NOT affected and must not be lumped in with this -- "
            "`reboot` is present on every matrix row and `UnixHost._soft_reboot` works "
            "as shipped"
        ),
        measured_on=(
            "the five matrix artifacts, 2026-08-13: `shutdown` is absent from the applet "
            "list on 1.16.1, 1.21.1, 1.28.1, 1.31.0 and 1.35.0, while `reboot` and "
            "`poweroff` are present on all five. BusyBox will only run an applet its own "
            "list carries, so the list is the whole answer here"
        ),
        queued_for=(
            "the full-parity workstream, `todo/busybox-parity-sweep-2026-08-11.md`. "
            "`poweroff` is the measured spelling, but choosing between the two is a "
            "userland probe (the pattern `Userland.timeout_style` already sets), not a "
            "hard-coded swap that would break every GNU host"
        ),
    ),
    Gap(
        surface="run-command-line-length",
        status=MEASURED_BROKEN,
        reason=(
            "a command longer than 1022 characters sent through `Host.run()` is "
            "SILENTLY TRUNCATED on a BusyBox target -- a different, shorter command "
            "runs and its success is reported as the caller's. `run()` drives a "
            'PERSISTENT session, which `SshSession._open` opens with `term_type="dumb"` '
            "(`src/otto/host/session.py:765,770`), so the far side allocates a pty and "
            "the command arrives as a TYPED LINE through ash's line editor. `Host.exec()` "
            "opens a bare exec channel with no pty and is unaffected, so the same command "
            "is safe through `exec()`"
        ),
        measured_on=(
            "the phase-5 spike, 2026-08-13, dropbear 2022.83 against BusyBox 1.35.0: "
            "largest line delivered intact 1022, first truncated 1023, with no error and "
            "no log line. Measured identical against OpenSSH and against a bare LOCAL "
            "pty, which is what identifies it as BusyBox ash's "
            "`CONFIG_FEATURE_EDITING_MAX_LEN` and not the transport. For contrast the "
            "exec channel took 9000 characters intact and broke at 9001"
        ),
        queued_for=(
            "unqueued, deliberately: the phase-5 plan lists fixing this under `Out of "
            "scope, deliberately` and records it here instead. A fix is a pty-free "
            "`run()` path, not a larger buffer -- the buffer belongs to the device"
        ),
    ),
    Gap(
        surface="legacy-dropbear-crypto",
        status=UNTESTED,
        reason=(
            "real BusyBox devices run dropbear, and an OLD dropbear negotiates only "
            "SHA-1-era algorithms that modern asyncssh disables by default. otto carries "
            "cipher/host-key/kex lists in `ssh_options`, so the spec calls this "
            "configuration rather than code -- UNVERIFIED in either direction. Nothing "
            "is blocked and nothing should be: otto connects, and the outcome is the "
            "measurement this entry is waiting for"
        ),
        measured_on="",
        queued_for=(
            "Tier 3 fidelity item C, `todo/busybox-tier3-fidelity-2026-08-13.md`: run "
            "the phase-5 harness against a period-appropriate dropbear instead of "
            "2022.83. Two things it must measure first -- whether an old dropbear even "
            "builds on a modern toolchain, and whether `ssh_options` really suffices"
        ),
    ),
    Gap(
        surface="busybox-over-a-real-network",
        status=UNTESTED,
        reason=(
            "no BusyBox target is exercised over a real network path. Every tier is "
            "local: Tier 1 runs the artifact as a subprocess, Tiers 2 and 3 run it "
            "inside an unprivileged namespace on loopback. Loopback has a ~64 KB MTU and "
            "no real latency, so nothing measured so far can surface an interaction "
            "between the transfer's chunking and a real path's MTU, window or timeouts. "
            "Untested, therefore not blocked"
        ),
        measured_on="",
        queued_for=(
            "Tier 3 fidelity item B, `todo/busybox-tier3-fidelity-2026-08-13.md`: let "
            "the harness aim at a remote host when one is configured, defaulting to "
            "loopback. Option A (a real BusyBox lab VM) is declined for now -- it needs "
            "VM provisioning, which is not this workstream's call"
        ),
    ),
]
"""Every declared userland gap, in the order the docs table renders them.

The single source of truth §4 of the spec asks for. Read it, do not edit it in
flight: the entries are declared constants, and the three consumers (the
runtime error, the docs page, the parity queue) all assume this list is what
the repo says it is.

Adding one is a four-part claim -- what breaks, why, what proved it, and who
would fix it -- and :class:`Gap` refuses a record that leaves any of the four
out.
"""

# Design-time candidates that are NOT records above, kept here rather than
# quietly dropped. The spec's §4 "Known entries at design time" came from a
# survey; the entries above are what phases 1-5 actually ran. Where the two
# disagree the measurement wins, and a reader comparing the spec to this file
# is owed the reason each missing candidate is missing.
#
# FOUR ENTRIES, TWO REASONS, and they must not be read as one list. THREE were
# measured and the measurement did not support the prediction -- ``pgrep``/
# ``pkill``, ``sudo`` and ``reboot``. That is the count the docs page quotes
# ("three candidates ... dropped for exactly this reason once they were
# measured"), and it is the count to keep in sync with it. The FOURTH,
# ``install``/``stage``, is here for the opposite reason: nothing measured it
# at all. It is listed so a reader does not conclude it was cleared.
#
# Recorded as a comment and not as records, because a `Gap` is a gap: a
# candidate that measurement CLEARED is not one, and putting it in `GAPS`
# would put a non-gap in the docs table and in the parity queue.
#
# ``pgrep``/``pkill`` -- predicted absent, MEASURED PRESENT on all five matrix
#     artifacts (2026-08-13). The spec's item was test hygiene rather than
#     product behaviour anyway: nothing under ``src/otto/`` spawns either.
# ``sudo`` -- absent on all five rows, but this is ADAPTATION, not a gap.
#     ``su`` is present on all five and ``Userland.elevation`` already probes
#     for exactly this, picking ``su``; it refuses only when BOTH answer no.
# ``reboot`` -- the spec pairs it with ``shutdown``; measurement separates
#     them. ``reboot`` is an applet on all five rows, so only the ``shutdown``
#     half is a gap (see ``shutdown-command``).
# product ``install``/``stage`` -- NOT MEASURED by any tier, and therefore not
#     a record: unmeasured runs. Whoever measures it first adds it, in either
#     status, with what it answered.


def gap_for(surface: str) -> "Gap | None":
    """Return the declared gap for *surface*, or ``None`` if none is declared.

    ``None`` is the answer for a surface nobody has written down, and that is
    not the same as a surface known to work -- but it leads to the same place,
    because :func:`refuse_if_gapped` does not block what it has no measurement
    for.
    """
    for gap in GAPS:
        if gap.surface == surface:
            return gap
    return None


def refuse_if_gapped(surface: str, *, host: str = "", attempted: str = "") -> None:
    """Raise if *surface* is measured broken; return quietly otherwise.

    **Measured-broken refuses up front; unmeasured runs.** The spec's rule,
    and the only implementation of it. Three outcomes and only one of them
    raises:

    * *surface* has a :data:`MEASURED_BROKEN` record -- raise, before anything
      is emitted, with the record's own evidence and docs anchor in the
      message;
    * *surface* has an :data:`UNTESTED` record -- return. otto does not know
      this fails, and refusing would turn "we do not know" into "does not
      work", which is a lie in the expensive direction: it makes otto decline
      things that work;
    * *surface* is not in the table at all -- return, for the same reason,
      more so.

    *host* and *attempted* only decorate the message. Neither changes the
    verdict: the table is about a class of userland, and the caller is the one
    that decided this host belongs to it.

    Raises:
        UnsupportedOnUserlandError: *surface* is declared
            :data:`MEASURED_BROKEN`. Nothing was attempted, which is precisely
            why it is this exception and not
            :class:`~otto.host.errors.HostCommandError`.
    """
    gap = gap_for(surface)
    if gap is None or not gap.refuses:
        return
    raise UnsupportedOnUserlandError.for_gap(gap, host=host, attempted=attempted)
