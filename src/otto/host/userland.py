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

**One round asks about all of them, whatever the caller came for.** There is
no per-capability resolution: :meth:`Userland.resolve` settles everything not
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
    ``_run_get``), and ``otto.host.file_ops.refuse_if_base64_is_absent``
    (``read_file``/``write_file``, which still hard-code the codec and read
    this only to REFUSE when the device has none). That second reader is why
    :class:`UserlandHost` below exists: it reaches the resolver through the
    host's own hook rather than being handed one, and two mixins now share
    that hook.
``checksum``
    ``otto.host.transfer.shell.ShellFileTransfer`` (both ``_put_one`` and
    ``_get_one``, for post-transfer integrity verification — ``md5sum`` on
    both sides when present, a byte-size comparison via ``stat_size``
    otherwise)
``shell_dialect``
    nothing yet — an ``ash`` frame is registered, but nothing routes this
    probe's result to it; see the hole below
``nc_dash_n``
    ``otto.host.transfer.nc.refuse_if_nc_rejects_dash_n``, which declines a
    netcat GET on a device whose ``nc`` was measured to reject the ``-N``
    otto's sender and its tunnelled listener both emit. The second
    OPTION-SUPPORT capability after ``timeout_style``, and the second one
    whose only consumer is the ``nc`` backend
``applet_<name>``, one per entry in :data:`PROBED_APPLETS`
    three consumers, and NOT the same one for all seven names -- which is the
    point of a per-name capability. ``applet_uudecode``/``applet_uuencode``
    choose the ``shell`` backend's codec
    (``otto.host.transfer.shell.ShellFileTransfer._select_codec``);
    ``applet_shutdown``/``applet_poweroff`` pick the power-off spelling
    (``otto.host.unix_host.shutdown_command``, the one consumer that ADAPTS
    rather than refusing); ``applet_scp`` refuses an scp transfer to a device
    with no such binary
    (``otto.host.transfer.scp.refuse_if_scp_is_absent``). ``applet_base64``
    and ``applet_nc`` still have none, for different reasons: the codec
    question is answered by ``base64_flag`` above (a spelling, not a
    presence), and ``nc``'s presence is necessary but not sufficient -- see
    :data:`PROBED_APPLETS`, and see ``nc_dash_n`` above for the SUFFICIENT
    half, which is a different question and therefore a different capability
    rather than a reading of this one. Read them all through
    :meth:`Userland.has_applet`.

So the first ``run(sudo=True)`` against a host issues up to fourteen probes to
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
the frame registry; the other six are safe to consume today, and now all
six of them have a consumer — see the table above — leaving ``shell_dialect``
the only one of the FIXED SEVEN still without one. Two of the seven applet
capabilities are likewise unconsumed (``applet_base64``, ``applet_nc``), each
for a different reason — see the table above.

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
``stat``'s ``-c %s`` vs ``--format=%s``, ``wc``'s ``-c <`` vs ``-c FILE``, and
``nc``'s ``-N``, which is the purest instance of the class: the probe asks
NOTHING BUT whether the option parses (see ``Userland._probe_nc_dash_n``).
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

THE APPLET BATCH'S THIRD COPY IS TIER 2, NOT TIER 1, and that is a property of
what it asks rather than a filing choice. ``_applet_probe_command`` is a
shell CONSTRUCT (``for``/``command -v``/``&&``/``||``/``>/dev/null``) whose
answer is "is there an applet by this name", and Tier 1 cannot substantiate an
ABSENCE structurally: it SCOPES PATH to a directory of symlinks it wrote
itself, so a missing applet is missing because Tier 1 did not shim it. Tier 2
builds a root whose ``/bin`` came from BusyBox's own ``--install -s``, so a
name that is not there was compiled out. The copy therefore lives in
``tests/busybox/test_applet_resolution.py``, which already measured
``command -v base64`` that way, and the three-copy rule holds with that file
in Tier 1's place.

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
from dataclasses import dataclass, field
from typing import Any

from ..logger.mode import LogMode
from ..result import CommandResult, Result
from ..utils import Status, cli_exposed
from .errors import UnsupportedOnUserlandError
from .host import is_dry_run
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
# tests/unit/host/test_userland.py: 20.0 keeps all 102 tests green (30/20 still
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
# together. THE ARITHMETIC: resolution issues at most fourteen probes (2 elevation +
# 3 timeout + 2 base64 + 2 stat + 1 checksum + 1 dialect + 1 applet batch + 2 nc_dash_n),
# so a host that swallows every one of them costs 14 x _PROBE_TIMEOUT_S = 140s
# unbounded. The
# applet batch is ONE probe however many names :data:`PROBED_APPLETS` carries --
# that is the whole point of ``_applet_probe_command``, and it is why adding an
# applet does not move this number; adding a CAPABILITY does, and ``nc_dash_n``
# is what moved it from twelve. resolve() holds its
# lock for that whole span, so on a concurrent consumer — nc's bulk put fans
# its files out and each one awaits resolution — every queued caller waits the
# full span out BEFORE its own timeout starts counting. This budget converts
# that into a stated bound: the deadline is set once per resolution, each probe
# is granted min(_PROBE_TIMEOUT_S, whatever is left), and a probe with nothing
# left is never sent. So a wedged host costs at most ceil(30/10) = 3 probe
# timeouts, not fourteen.
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
# 40.0 keeps all 102 tests green; 41.0 already reds
# test_resolution_stops_once_the_whole_budget_is_spent[as-shipped] on one
# assertion; 90.0 reds the SAME row on a DIFFERENT assertion (see 1. below --
# one test, two distinct ways to fail it, not two tests); 111.0 adds a second
# test failing; 121.0 adds a third. Neither prior number was ever guarded by
# an assertion that reads it, which is exactly how both survived being wrong
# until someone happened to measure them.
#
# RE-MEASURED 2026-08-14, when `nc_dash_n` added a probe: the last two moved
# (they were 100.0 and 101.0) and the first two did not. That is the whole
# lesson of this comment in one change -- the boundaries are properties of the
# probe TREE, so a capability added anywhere in it shifts the ones that count
# commands and leaves the ones that count budget alone.
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
#    resolution can only ever ISSUE 11 commands (2 elevation + 1 timeout +
#    2 base64 + 2 stat + 1 checksum + 1 dialect + 1 nc presence + 1 applet
#    batch -- confirmed directly from the failing assertion's own captured call
#    list), never 14, no matter
#    how much budget the caller affords. `nc_dash_n` short-circuits the same way
#    `timeout` does, so it contributes one command here and two to the full
#    count. Once the budget affords 12 or
#    more, the test's own `affordable = ceil(budget / probe)` prediction
#    exceeds what the algorithm can structurally send, and
#    `len(runner.calls) == affordable` reds on a probe-tree ceiling that
#    has nothing to do with the time budget (111.0).
# 3. test_every_logger_call_site_is_exercised_at_debug is the third red, at
#    121.0, and it breaks for a reason that has nothing to do with either
#    mechanism above. The "resolution budget spent" line (`_probe`, guarding
#    `if remaining <= 0`) is only emitted when a probe is skipped for lack of
#    budget. Once the budget affords every probe -- which is exactly what (2)
#    measures becoming structurally true once affordable reaches 11 -- nothing
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

PROBED_APPLETS = [
    "base64",
    "nc",
    "poweroff",
    "scp",
    "shutdown",
    "uudecode",
    "uuencode",
]
"""The closed list of applet names :meth:`Userland.has_applet` will answer for.

CLOSED ON PURPOSE, and that is the whole of the typo safety. Every name here
becomes a real capability key (``applet_<name>``, see :func:`applet_capability`)
and a real :class:`~otto.host.options.UserlandOptions` field, so a misspelling
is a ``TypeError`` from the dataclass, an ``extra='forbid'`` validation error
from :class:`~otto.models.options.UserlandOptionsSpec`, or a ``ValueError``
from :func:`applet_capability` -- never a guard that silently never fires.
An OPEN "ask the device about any name" probe would have none of those three.

TAKEN FROM THE :data:`GAPS` RECORDS' OWN ``measured_on`` FIELDS, never invented.
Which of the five blocked pieces of work reads which name, and how far presence
actually gets each one:

``base64``, ``uuencode``, ``uudecode``
    codec selection for the ``shell`` transfer backend
    (``todo/busybox-parity-sweep-2026-08-11.md``). Presence IS the question
    here: ``base64`` is absent on 1.16.1 and present from 1.21.1, and
    ``uuencode``/``uudecode`` round-trip on all five rows. Both halves of the
    uu pair are listed because a build is free to compile out either one and
    the backend needs both (PUT decodes on the device, GET encodes on it).
``shutdown``, ``poweroff``
    the ``shutdown-command`` surface, and the pair otto ACTS on rather than
    refuses. Presence IS the question, and it is a CHOICE between two names:
    ``shutdown`` is absent and ``poweroff`` present on all five rows, so
    :func:`otto.host.unix_host.shutdown_command` reads both and emits whichever
    the device has. Only a device answering ``absent`` to both is refused.
``scp``
    the ``scp-transfer`` surface, and the one entry where presence is the WHOLE
    question. ``scp`` is a remote BINARY the legacy protocol execs by name,
    absent on all five rows, and :class:`~otto.host.options.ScpOptions` carries
    no binary-name override -- so unlike ``nc`` below there is no second name
    the answer could be about, and unlike ``shutdown``/``poweroff`` above there
    is no alternative spelling to adapt to.
    :func:`otto.host.transfer.scp.refuse_if_scp_is_absent` reads a SETTLED
    ``absent`` here and declines the transfer; a device that answers
    ``present``, or one that could not be asked, transfers exactly as it did
    before that guard existed.
``nc``
    the ``nc-transfer`` surface, and the one entry where presence is NECESSARY
    BUT NOT SUFFICIENT -- do not read this probe as having solved it. That
    record's gap is that BusyBox's ``nc`` APPLET rejects ``-N`` and spells its
    listener ``-l -p PORT``; a device with a real OpenBSD netcat installed
    alongside works fine via ``NcOptions.exec_name``. So ``present`` says
    nothing about WHICH netcat answered, and ``exec_name`` may name a binary
    (``ncat``, ``netcat``) that is not in this closed list at all. Only the
    ``absent`` direction is a conclusion there: nothing to run is nothing to
    run. The SUFFICIENT half is :attr:`Userland.nc_dash_n`, a separate
    capability that asks whether the option parses rather than whether the name
    resolves -- and it is what ``otto.host.transfer.nc`` reads. This entry is
    still the one that has no consumer.

WHAT IS DELIBERATELY NOT HERE. ``sftp-transfer`` is the fifth blocked surface
and it gets NO entry, because its question is not a PATH-applet question and a
capability that answered it wrongly would be worse than none. That record's own
``measured_on`` names an ABSOLUTE path -- ``/bin/sh: /usr/lib/sftp-server: not
found`` -- and an sftp-server binary is not on ``PATH`` on a healthy GNU host
either (Debian ships ``/usr/lib/openssh/sftp-server``), so ``command -v
sftp-server`` answers "absent" on hosts where sftp works perfectly. That is a
false negative in the expensive direction: a refusal built on it would decline
a transfer the device can do. Which path to test, and whether the daemon's
configured subsystem is even reachable as a file, is a design question this
probe does not settle.

THAT QUESTION HAS SINCE BEEN ANSWERED, and the answer is that it has no answer
worth having: no fact available before the operation distinguishes a device
that serves sftp from one that does not, so ``sftp-transfer`` gets no
pre-emptive refusal from anywhere -- not from this probe and not from a
"known paths" list, which would fail the same way on the first unusual distro.
Its paths are :data:`PATH_ATTRIBUTED` instead: the attempt is made, and the
failure it produces is translated into the record's words. Read that state for
why an exclusion here is the beginning of that story rather than a gap in it.
"""

APPLET_PRESENT = "present"
"""The device resolved this name to something it can run."""

APPLET_ABSENT = "absent"
"""The device resolved this name to nothing. A MEASUREMENT, not a refusal."""

_APPLET_PREFIX = "applet_"
# The capability key and the `UserlandOptions` field share this prefix, and the
# sharing is load-bearing rather than tidy: `_resolve_once` reads a declaration
# with `getattr(self._options, name)` for the fixed seven, and the applet block
# reads its declarations exactly the same way. A prefix written twice would let
# the two drift into a declaration nothing consults.

NC_APPLET = "nc"
"""The ONE name :attr:`Userland.nc_dash_n` is an answer about. Not a default.

``nc_dash_n`` is the only capability here whose subject otto also lets an
operator RENAME: :attr:`~otto.host.options.NcOptions.exec_name` chooses which
netcat the transfer backend execs, and ``nc`` is merely its default. This
module probes this name and no other, because a capability is resolved once per
host and cached under a fixed key -- and because a probe that read
``exec_name`` would exec an operator-supplied binary on EVERY host, including
the ones that never transfer over netcat.

WHAT THAT COSTS, and it is the consumer's problem rather than this module's:
``otto.host.transfer.nc.refuse_if_nc_rejects_dash_n`` refuses only when the
backend's ``exec_name`` IS this name, so a host pointed at ``ncat`` is never
refused from this measurement. That is the cheap direction (the transfer is
attempted exactly as it was before this capability existed) and it is the
direction the ``nc-transfer`` record demands: a BusyBox device with a real
OpenBSD netcat installed alongside works fine, and refusing it would be a
refusal of a host that works.

WHAT IT BUYS is that the probe and the emitted command resolve THE SAME NAME
THROUGH THE SAME SHELL: both go out over ``Host.exec``, so whichever ``nc``
that host's ``PATH`` finds for the transfer is the one measured here. The
answer is not about a path this module guessed at.
"""

NC_DASH_N_SUPPORTED = "supported"
"""The device's ``nc`` PARSES ``-N``. What otto assumed before it asked."""

NC_DASH_N_REJECTED = "rejected"
"""The device has an ``nc`` and it rejects ``-N``. The refusing answer."""

NC_DASH_N_ABSENT = "absent"
"""There is no ``nc`` to ask. A MEASUREMENT, and NOT the refusing answer.

Distinguished from :data:`NC_DASH_N_REJECTED` rather than folded into it,
because the two are different facts and only one of them is what the
``nc-transfer`` record measured. That record is about an applet's OPTION SET;
a device with no netcat at all is :data:`APPLET_ABSENT` on ``applet_nc``, which
is a different (already recorded, still unwired) fact. Folding them would make
:meth:`UserlandHost.probe` report ``rejected`` for a host that has nothing to
reject with, and would have a refusal render a message about a spelling to an
operator whose device has no binary.
"""


def applet_capability(applet: str) -> str:
    """Return the capability key for *applet* -- ``"scp"`` -> ``"applet_scp"``.

    The bridge between the two vocabularies, and the reason a consumer can ask
    :meth:`Userland.is_settled` about an applet without spelling the prefix by
    hand. ``is_settled(f"applet_{name}")`` would work and is exactly what this
    exists to stop: an f-string bypasses the closed list, so a typo becomes a
    key ``is_settled`` rejects at the wrong moment (or, worse, one it accepts
    because the typo happened to name another capability).

    Raises:
        ValueError: *applet* is not in :data:`PROBED_APPLETS`. Loud for the
            same reason :meth:`Userland.is_settled` is loud about an unknown
            key -- a consumer gating a refusal on a name nothing probes has a
            guard that cannot fire.
    """
    if applet not in PROBED_APPLETS:
        raise ValueError(
            f"{applet!r} is not an applet this module probes; it asks about {PROBED_APPLETS}"
        )
    return f"{_APPLET_PREFIX}{applet}"


_APPLET_CONTROL = "echo"
"""The POSITIVE CONTROL carried in every applet batch. Never a reported answer.

WHAT IT DEFENDS AGAINST, which is the failure a naive batch has no way to see:
the loop reports ``<name>=0`` both when the device has no such applet and when
``command -v`` itself is missing or broken, and the second case is an ALL-ZEROS
answer that looks exactly like a device with none of the applets. Recorded as a
measurement, that would settle every capability at ``absent`` on the strength of
a probe that measured nothing.

``echo`` is the control because it is a SHELL BUILTIN -- ``command -v echo``
answers 0 with no dependence on ``PATH``, on an applet symlink, or on
``CONFIG_FEATURE_SH_STANDALONE``, so a zero for it can only mean the primitive
itself did not work. Measured 1 on all five matrix rows (2026-08-14, Tier 2
rootfs). :meth:`Userland._probe_applets` discards the whole batch when it comes
back anything else, which leaves the capabilities UNASKED rather than answered
-- see ``_UNASKABLE_DEFAULTS``.
"""


def _applet_probe_command(applets: "list[str]") -> str:
    """Build the ONE command that answers presence for every name in *applets*.

    O(1) ROUND TRIPS, WHATEVER THE LIST LENGTH, and that is the requirement
    rather than an optimisation: BusyBox devices are typically slow, and a
    per-applet round trip would put one SSH exec channel per name on the same
    path ``_RETRY_COOLDOWN_S`` exists to protect -- against a server that
    REFUSES excess channels rather than queueing them.

    WHAT THAT IS WORTH, measured 2026-08-14 over the Tier 3 transport (real
    ssh, rootless dropbear on loopback, BusyBox 1.35.0, connection already
    warm): the batch answered all seven names in a median 10.7 ms, while seven
    separate ``command -v`` execs cost a median 61.2 ms -- 5.7x, and that is
    the FLOOR of the saving rather than a typical figure, because loopback has
    no real latency and the per-channel cost is what a slow device on a real
    path multiplies. Six of the seven channels are also six chances to be
    refused by an sshd at its ``MaxSessions`` ceiling, which is the cost that
    does not show up in a timing.

    ENUMERATION IS NOT AVAILABLE, which is why this is per-name detection at
    all. ``busybox --list`` does not exist on 1.16.1: measured, it exits 1 with
    ``--list: applet not found`` and enumerates nothing (the same finding that
    made ``tests/_fixtures/busybox_rootfs.py`` build its root with
    ``--install -s``). It works on the other four rows, so a probe built on it
    covers four fifths of the matrix and reports success.

    ``command -v``, MEASURED rather than assumed, 2026-08-14, in the Tier 2
    rootfs on all five matrix rows. It answered correctly on every one --
    ``base64`` absent on 1.16.1 alone, ``scp`` and ``shutdown`` absent on all
    five, ``nc``/``poweroff``/``uuencode``/``uudecode`` present on all five.
    ``which`` and the ``type`` builtin answered identically on those same five
    artifacts, so this is a choice between three things that all work here and
    not a lone survivor: ``command -v`` wins because ``which`` is ITSELF an
    optional applet a build may compile out (in which case it reports every
    name absent -- the all-zeros failure ``_APPLET_CONTROL`` exists for),
    and because it is what :meth:`Userland._probe_elevation` already issues, so
    the module asks its presence questions one way.

    A PROBE IS NOT EXEMPT FROM THE LINE BOUNDS, which is why the length is
    guarded rather than eyeballed. Probes go out through ``Host.exec``, whose
    9000-character exec-channel ceiling is roomy -- but ``exec`` has no
    stateless primitive on a ``term: telnet`` host or on any host whose login
    is proxied, and there it routes through a pooled shell session where
    :data:`ASH_TYPED_LINE_MAX` (1022, minus otto's own BEGIN/END framing)
    applies instead. The shipped seven names emit 135 characters, so the slack
    against the tighter of the two is a factor of about seven -- comfortable,
    and nothing like the three orders of magnitude the exec ceiling suggests.
    ``test_the_batch_fits_the_line_bound_the_tighter_transport_imposes`` runs
    otto's real ``refuse_if_line_editor_would_truncate`` over this command, so
    the guard is the RELATIONSHIP and neither number is retyped: a list long
    enough to approach the bound reds there rather than truncating on a device.
    """
    names = " ".join([_APPLET_CONTROL, *applets])
    return (
        f'for a in {names}; do command -v "$a" >/dev/null 2>&1 && echo "$a=1" || echo "$a=0"; done'
    )


def _parse_applet_answers(output: str) -> "dict[str, bool]":
    """Parse ``<name>=1``/``<name>=0`` lines into a map. Unknown shapes are dropped.

    Dropping rather than raising, and the verdict is not this function's: the
    CALLER compares the parsed map against exactly the names it asked for (see
    ``Userland._probe_applets``), so an unparseable line reaches it as a
    MISSING answer and discards the batch, while a banner line that happens to
    parse reaches it as an EXTRA key and discards it too. Both are the safe
    direction. Raising here would instead turn a chatty login shell into a
    resolution error rather than an unasked capability.
    """
    answers: dict[str, bool] = {}
    for line in output.splitlines():
        name, sep, value = line.strip().partition("=")
        if sep and value in ("0", "1"):
            answers[name] = value == "1"
    return answers


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
#     ``absent``, because the alternative is to claim a decode flag works and
#     build a command that fails. Two consumers read it and they treat this
#     value DIFFERENTLY, which is a property of the consumer and not of the
#     table: :class:`~otto.host.transfer.shell.ShellFileTransfer` refuses a
#     transfer on ``"absent"`` however it got there (base64 is the whole
#     backend, so there is nothing to degrade to), while
#     ``otto.host.file_ops.refuse_if_base64_is_absent`` refuses only when the
#     absence was SETTLED -- see :meth:`Userland.is_settled`. An assumed value
#     is exactly what otto did before it asked anything, so a guard that
#     refused on one would convert a refused probe round into "this device has
#     no base64", which is the expensive direction.
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
# ``nc_dash_n``
#     ``supported``, and it is the same standard as ``stat_size``'s ``stat``:
#     ``transfer/nc.py`` has emitted ``nc -N`` unconditionally since it was
#     written, so this value is what otto did before it asked anything. The
#     other two candidates are both worse in the expensive direction.
#     ``rejected`` would let a refused probe round REFUSE a transfer, which is
#     the whole failure ``is_settled`` exists to prevent -- and unlike the
#     applet defaults, where the same reasoning ends at "a consumer that
#     refuses checks ``is_settled`` anyway", here the consumer's ONLY arm is a
#     refusal, so the default is the last line rather than a redundant one.
#     ``absent`` would be a claim that a device has no netcat, made by a probe
#     that never arrived.
# ``applet_<name>``
#     ``present``, for every name, DERIVED rather than typed -- and the seven
#     above are the reason it can be one uniform rule instead of seven
#     arguments. Each of those seven had to be reasoned about separately because
#     each names a SPELLING otto would emit; an applet capability names only
#     whether otto may reach for a binary at all, and what otto did before it
#     asked anything was reach for it. Before any of these capabilities existed
#     ``Host.shutdown()`` emitted ``shutdown -h now`` unconditionally,
#     ``ScpFileTransfer`` execed ``scp`` unconditionally, and
#     ``ShellFileTransfer`` emitted ``base64`` unconditionally. ``present`` is
#     precisely that status quo, for every name, which is the same standard
#     ``stat_size``'s ``stat`` is held to. ``absent`` would be the expensive
#     direction twice over: a consumer that DEGRADES on the value alone would
#     read a refused probe round as "this device has no scp" and pick a worse
#     backend, and a consumer that REFUSES is already required to check
#     :meth:`Userland.is_settled` first, so ``absent`` buys it nothing.
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
    "nc_dash_n": NC_DASH_N_SUPPORTED,
    **{applet_capability(a): APPLET_PRESENT for a in PROBED_APPLETS},
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

    async def _send(self, cmd: str) -> "CommandResult | None":
        """Issue *cmd* under the resolution budget; ``None`` if it could not be asked.

        The transport half of ``_probe``, split out so the applet batch can
        read the command's OUTPUT while every probe keeps one grant, one
        budget check and one ``wait_for``. Nothing here interprets the answer:
        the exit-code reading lives in ``_probe`` and the line parsing in
        ``_probe_applets``, so neither can quietly acquire its own
        timeout policy.

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

        **A DRY RUN CANNOT ASK, AND THAT IS THE ARM IT TAKES.** Every path into
        this method reads the answer as a measurement:
        :meth:`_probe` believes the exit code and ``_probe_applets`` believes
        the stdout. ``BaseHost.exec`` answered a dry run with
        ``_dry_run_result`` — ``retcode=0``, without leaving this machine — so
        every probe issued under one came back a YES and every capability
        settled on an answer nobody took. That is not a cosmetic wrong value:
        SETTLED is precisely what :meth:`as_lab_json` offers as a pasteable
        ``userland_options``, and inside a JSON payload a guess is
        indistinguishable from a measurement. Refusing here rather than at each
        reader is what makes it ONE authority: the applet batch and the seven
        single-capability probes share this method, and both need the same answer.

        The consequence is that nothing settles under a dry run, so every
        capability holds its ``_UNASKABLE_DEFAULTS`` value as ``assumed`` —
        which is exactly what it is, and exactly what otto did before it asked
        anything. The ``[DRY RUN]`` echo those probes used to print goes with
        them, and losing it makes the dry run MORE faithful rather than less:
        the probes are issued ``log=LogMode.NEVER`` (redacted from every sink),
        so a real run shows none of them, while ``_dry_run_result`` logged at
        the default ``NORMAL`` and showed all of them. That echo has since been
        taught to honour the caller's mode, so a probe reaching it would now be
        silent anyway — but silence there is a redaction, and this arm is the
        stronger property: the probe is never ASKED, so there is no answer to
        mistake for a measurement.

        ``_dry_run_result`` has since been hardened too — it returns a
        ``Status.NotRun`` decline whose ``value`` raises — so a probe that
        reached it would now break loudly rather than settle a fiction. This
        arm still comes first and still earns its place: belt and braces, and
        "never asked" beats "asked and refused to read the answer".

        The dry-run arm reuses this method's own "could not be asked" template
        rather than adding a second one, on the same ground ``_probe_applets``
        reuses it: that is exactly what this is.
        """
        if is_dry_run():
            _logger.debug(
                "userland: probe %r could not be asked (%s)", cmd, "a dry run reaches no device"
            )
            return None
        remaining = self._deadline - _monotonic()
        if remaining <= 0:
            _logger.debug("userland: resolution budget spent before %r; leaving it unasked", cmd)
            return None
        grant = min(_PROBE_TIMEOUT_S, remaining)
        try:
            return await asyncio.wait_for(
                self._run(cmd, log=LogMode.NEVER, timeout=grant), timeout=grant
            )
        except Exception as exc:  # noqa: BLE001 — a probe that cannot run is an absence of measurement, not an error
            _logger.debug("userland: probe %r could not be asked (%s)", cmd, exc)
            return None

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

        The transport, the grant and the budget check are ``_send``'s;
        this method is only the exit-code reading laid over them.
        """
        result = await self._send(cmd)
        return None if result is None else result.retcode == 0

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
        all but one has already paid for the rest, and re-issuing them
        would put probe traffic back on the fan-out path the lock protects.
        The applet capabilities are the one exception, and only because their
        probe is: they ride ONE command, so they settle together or not at all.

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

        **WHAT A CALLER PAYS.** Every capability, not the one it came
        for: there is no scoped form of this call, so ``run(sudo=True)``
        issues up to fourteen probes to read ``elevation``, which probes 1-2
        settled. Fourteen rather than one per capability because several
        capabilities need two or three, and because the applet
        names ride a single batched command whatever their number — see
        ``_applet_probe_command``. On a healthy host that is fourteen fast round trips; on a
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
            ("nc_dash_n", self._probe_nc_dash_n),
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
        # THE APPLET CAPABILITIES, RESOLVED AS A GROUP, and the second loop is
        # the point rather than a duplication of the first. Above, one probe
        # answers one capability; here ONE COMMAND answers as many as are still
        # open, so the (name, probe) pairing the loop above is built on cannot
        # express it. Everything else is identical -- same declared-wins
        # short-circuit, same "assumed" vs "probed" source, same settled rule --
        # because that is what makes the override, the debug line and the
        # pasteable pin apply to these without a second mechanism.
        #
        # LAST, deliberately. The seven above keep their exact order, their
        # exact spellings and their exact count, so a host that never reads an
        # applet capability sees the resolution it saw before; and when the
        # budget cuts the round short it is this batch that goes, not an
        # incumbent. `nc_dash_n` was appended to that list rather than inserted
        # into it for the same reason -- the newest capability is the one a
        # short budget should lose, and it costs the batch nothing at the
        # shipped budget, which affords 3 probes and never reaches either.
        #
        # ONLY THE UNSETTLED, UNDECLARED NAMES ARE ASKED ABOUT. A maintainer who
        # has pinned every applet costs zero round trips here, which is the
        # requirement the batch exists to serve -- and one who has pinned some
        # gets a shorter command rather than a wasted question.
        open_applets = []
        for applet in PROBED_APPLETS:
            name = applet_capability(applet)
            if name in settled:
                continue
            declared = getattr(self._options, name)
            if declared:
                resolved[name], sources[name] = declared, "declared"
                settled.add(name)
                continue
            open_applets.append(applet)
        if open_applets:
            answers = await self._probe_applets(open_applets)
            for applet in open_applets:
                name = applet_capability(applet)
                if answers is None:
                    resolved[name], sources[name] = _UNASKABLE_DEFAULTS[name], "assumed"
                else:
                    resolved[name], sources[name] = answers[applet], "probed"
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

    async def _probe_nc_dash_n(self) -> str | None:
        """Report whether this device's ``nc`` accepts ``-N``. A DIFFERENTIAL, not a text match.

        The same two-step shape as :meth:`_probe_timeout` -- presence first,
        then WHICH SPELLING the thing that is there actually speaks -- because
        the question is the same question: ``nc`` existing says nothing about
        whether otto's ``nc -N <ip> <port>`` can run on it, exactly as
        ``timeout`` existing says nothing about which calling convention it
        wants. :data:`NC_APPLET` is the one name asked about, and
        :data:`NC_DASH_N_ABSENT` keeps "there is nothing to ask" apart from
        "it answered no".

        **THE OPTION IS COMPARED, NEVER THE ERROR TEXT.** Both arms run ``nc``
        with no destination, which every netcat answers by printing its usage
        and exiting non-zero, and the probe asks whether ``-N`` CHANGED that
        answer. Equal output means the option parsed; different output means it
        did not. Three properties follow, and they are why this beats the two
        obvious alternatives:

        * it needs no diagnostic string. The BusyBox rejection is spelled
          ``nc: invalid option -- N`` on 1.16.1 and 1.21.1 and ``nc:
          unrecognized option: N`` on 1.28.1, 1.31.0 and 1.35.0 -- two spellings
          across five artifacts, and a grep for either is a guard that goes
          quietly blind on the sixth;
        * it needs no exit code to differ, and none does. Measured: a usage
          error exits 1 on OpenBSD netcat and on every BusyBox row, so a probe
          reading only the status cannot tell an accepted option from a
          rejected one;
        * it is not an identity test. ``nc -Q`` against the real OpenBSD netcat
          -- an option it genuinely lacks -- answers REJECTED here, so the probe
          measures the option and not "does this look like BusyBox". That
          distinction is the ``nc-transfer`` record's central caveat.

        NOTHING IS CONNECTED AND NOTHING IS BOUND. A destination-less ``nc``
        touches no socket, which is what makes this safe to issue against every
        host at resolution time; the record's own ``nc -N 127.0.0.1 1``
        measurement opens a connection, and a listener probe would bind a port.
        ``</dev/null`` is belt and braces: no netcat reads stdin before it has a
        destination, and a hang is bounded by ``_send``'s grant anyway.

        MEASURED, 2026-08-14, before it was written into this module. All five
        Tier 2 matrix artifacts (1.16.1, 1.21.1, 1.28.1, 1.31.0, 1.35.0) answer
        REJECTED; OpenBSD netcat 1.226 answers SUPPORTED, and answers REJECTED
        for the ``-Q`` control. ``tests/busybox/test_applet_contracts.py``
        carries the Tier 1 copy of the spelling, per row.

        WHAT IT DOES NOT ANSWER: the LISTENER spelling. otto's put path spawns
        ``nc -l -w SECS PORT``, which carries no ``-N``, and no probe can settle
        that one without binding a port on the device. See
        :func:`otto.host.transfer.nc.refuse_if_nc_rejects_dash_n` for why that
        path stays unguarded rather than being refused on this answer.
        """
        present = await self._probe(f"command -v {NC_APPLET}")
        if present is None:
            return None
        if not present:
            return NC_DASH_N_ABSENT
        parses = await self._probe(
            f'[ "$({NC_APPLET} 2>&1 </dev/null)" = "$({NC_APPLET} -N 2>&1 </dev/null)" ]'
        )
        if parses is None:
            return None
        return NC_DASH_N_SUPPORTED if parses else NC_DASH_N_REJECTED

    async def _probe_applets(self, applets: "list[str]") -> "dict[str, str] | None":
        """Answer presence for EVERY name in *applets* in ONE round trip, or ``None``.

        The whole list or nothing, and the all-or-nothing is honest rather than
        coarse: one command carries every answer, so either it came back and
        every name in it was measured, or it did not and none of them was. That
        is why there is no per-applet ``None`` arm -- a partial answer is not a
        state this probe can produce.

        THREE THINGS ARE CHECKED BEFORE THE ANSWER IS BELIEVED, and each is a
        way the batch can come back looking like a measurement without being
        one:

        * the exit code, which is 0 whenever the loop ran at all (the final
          ``echo`` succeeds even when every name is absent), so a non-zero here
          means the shell never got through the construct;
        * ``_APPLET_CONTROL``, which separates "this device has none of these
          applets" from "``command -v`` did not work" -- the one failure a
          batch has no other way to see, since both reach otto as a
          well-formed all-zeros reply;
        * that the parsed names are EXACTLY the ones asked about, which rejects
          both a truncated answer (the ``run-command-line-length`` failure
          mode: a silently shortened line running a shorter command, which
          would otherwise present as a device with fewer applets) and a login
          banner whose text happens to parse as an answer.

        Any of the three failing leaves the whole batch UNASKED, which is the
        expensive-direction-safe outcome: the capabilities take their
        ``_UNASKABLE_DEFAULTS`` (``present``, i.e. what otto did before it
        asked), stay unsettled, and are asked again after
        ``_RETRY_COOLDOWN_S``.

        Reuses ``_send``'s "could not be asked" log template rather than
        adding a second one, because that is exactly what this is.
        """
        cmd = _applet_probe_command(applets)
        result = await self._send(cmd)
        if result is None:
            return None
        seen = _parse_applet_answers(result.value)
        expected = {_APPLET_CONTROL, *applets}
        if result.retcode != 0:
            reason = f"the batch exited {result.retcode}"
        elif not seen.get(_APPLET_CONTROL):
            reason = f"the {_APPLET_CONTROL!r} control answered no, so `command -v` did not work"
        elif set(seen) != expected:
            reason = f"answered for {sorted(seen)}, not {sorted(expected)}"
        else:
            return {a: APPLET_PRESENT if seen[a] else APPLET_ABSENT for a in applets}
        _logger.debug("userland: probe %r could not be asked (%s)", cmd, reason)
        return None

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

    @property
    def nc_dash_n(self) -> str:
        """:data:`NC_DASH_N_SUPPORTED` | :data:`NC_DASH_N_REJECTED` | :data:`NC_DASH_N_ABSENT`.

        Whether the ``nc`` on this device parses the ``-N`` that
        ``otto.host.transfer.nc`` emits when it asks the device to SEND. About
        the name :data:`NC_APPLET` and no other -- read that constant before
        consuming this, because an operator may have pointed
        :attr:`~otto.host.options.NcOptions.exec_name` somewhere else entirely.

        **A VALUE, NOT A VERDICT.** Its one consumer REFUSES, so it asks
        :meth:`is_settled` first: the cannot-ask default is
        :data:`NC_DASH_N_SUPPORTED`, which is what otto emitted before this
        capability existed, and a probe round that never arrived must not become
        a verdict that a device cannot send its files.
        """
        return self._get("nc_dash_n")

    def has_applet(self, applet: str) -> str:
        """:data:`APPLET_PRESENT` or :data:`APPLET_ABSENT` for one *applet*.

        The parameterized reader the fixed seven do not need. Seven properties
        would say the same thing seven times and would have to grow with
        :data:`PROBED_APPLETS`; this cannot fall behind that list, because the
        list is what it validates against.

        **A VALUE, NOT A VERDICT** -- and for a consumer that REFUSES that
        distinction is the whole contract. :data:`APPLET_ABSENT` is what an
        unasked capability reads as only if someone changed
        ``_UNASKABLE_DEFAULTS`` (it is ``present`` today, so an unasked
        applet reads as present) -- but do not lean on that, because the rule
        it is an instance of is the one that holds: a consumer that refuses
        asks :meth:`is_settled` first, or a refused probe round becomes a
        verdict about the device. ``otto.host.file_ops.refuse_if_base64_is_absent``
        is the precedent, and the shape here is the same::

            settled = userland.is_settled(applet_capability("scp"))
            if settled and userland.has_applet("scp") == APPLET_ABSENT:
                ...

        Both spellings of the name are checked -- this method against
        :data:`PROBED_APPLETS`, :meth:`is_settled` against
        ``_UNASKABLE_DEFAULTS`` via :func:`applet_capability` -- so neither
        half of that condition can be a typo that quietly never fires.

        Raises:
            ValueError: *applet* is not in :data:`PROBED_APPLETS`.
            RuntimeError: read before :meth:`resolve` was awaited, exactly as
                the seven properties do.
        """
        return self._get(applet_capability(applet))

    def is_settled(self, name: str) -> bool:
        """Whether *name*'s value was DECLARED or actually MEASURED, not assumed.

        The tri-state ``_probe`` keeps, made readable at the consumer. A
        property cannot answer this: :attr:`base64_flag` says ``"absent"`` both
        for a device that answered "no base64 here" and for one whose probe
        round never arrived, and ``_UNASKABLE_DEFAULTS`` is the whole reason
        those two must not be conflated. A consumer that DEGRADES may read the
        value alone -- degrading on a guess costs a weaker mode. A consumer
        that REFUSES has to ask this first, or a refused probe round becomes a
        verdict about the device.

        ``False`` before :meth:`resolve` has been awaited, which is honest
        rather than a special case: nothing is settled yet.

        Takes an applet capability as readily as one of the fixed seven -- pass
        ``applet_capability("scp")``, never ``"applet_" + name``, so the closed
        list gets to reject a typo before this method does. The whole applet
        batch settles or none of it does (see ``_probe_applets``), so
        asking about one of those keys is asking whether the batch landed.

        Raises:
            ValueError: *name* is not a capability this module resolves. A
                typo'd key would otherwise answer ``False`` forever, which for
                a caller gating a refusal on it is a guard that cannot fire.
        """
        if name not in _UNASKABLE_DEFAULTS:
            raise ValueError(
                f"{name!r} is not a userland capability; this module resolves "
                f"{sorted(_UNASKABLE_DEFAULTS)}"
            )
        return name in self._settled

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

        The applet capabilities travel here on exactly the same terms, under
        their ``applet_<name>`` keys. That is what makes the batch worth
        having twice over: a maintainer pastes the whole table back and the
        next resolution against that slow device issues no applet round trip
        at all, because ``_resolve_once`` asks only about names that are
        neither settled nor declared.
        """
        return {k: v for k, v in sorted(self._resolved.items()) if k in self._settled}


# ===========================================================================
# The `probe` verb's rendering
# ===========================================================================
#
# Formatting only. :meth:`Userland.as_lab_json` is the authority on WHAT may be
# pinned -- these helpers never decide that, they only lay out what it returns
# and explain what it left behind.


def _capability_rows(userland: "Userland") -> "list[tuple[str, str, str]]":
    """``(capability, value, source)`` for every capability, sorted by name.

    EVERY capability, including the assumed ones :meth:`Userland.as_lab_json`
    drops -- the omission is the thing a human reader needs explained, so this
    view cannot be built from the pin.

    THE SOURCE IS RECONSTRUCTED, not read back, and that is a stated limit
    rather than an oversight. ``_resolve_once`` builds its ``sources`` map as a
    local and spends it on the debug lines, so the only two facts about a
    capability that outlive a resolution are :meth:`Userland.is_settled` and
    the declaration the options carry -- and those two are exactly what the
    three arms are made of there: ``declared`` on a truthy option, ``probed``
    on an answer that then settles, ``assumed`` on the arm that settles
    nothing. Reading them back the same way yields the same classification
    without teaching :class:`Userland` to retain a map nothing else consumes.
    ``tests/unit/host/test_userland_probe.py`` holds the two spellings in
    agreement by parsing the debug lines a resolution has just emitted, rather
    than by restating an expectation here -- so a drift in either reddens.
    """
    values = {applet_capability(a): userland.has_applet(a) for a in PROBED_APPLETS}
    # Whatever `_UNASKABLE_DEFAULTS` carries beyond the applets is the fixed
    # seven, and each of those names is spelled exactly like the property that
    # reads it. Derived rather than listed, so an eighth capability with no
    # reader raises AttributeError here instead of going quietly missing from
    # the report.
    values |= {n: getattr(userland, n) for n in _UNASKABLE_DEFAULTS if n not in values}
    rows: list[tuple[str, str, str]] = []
    for name in sorted(values):
        if not userland.is_settled(name):
            source = "assumed"
        elif getattr(userland._options, name):  # noqa: SLF001 — same-module read of the declarations `_resolve_once` classifies from
            source = "declared"
        else:
            source = "probed"
        rows.append((name, values[name], source))
    return rows


_SOURCE_LEGEND = (
    "declared = already pinned in this host's userland_options, and never re-probed. "
    "probed = the device answered, so this one is worth pinning. "
    "assumed = otto could not ask, so the value is only what otto did before it asked "
    "anything -- and it is left out of the pin for that reason, because inside a JSON "
    "payload a guess is indistinguishable from a measurement."
)


def _probe_report(userland: "Userland") -> "list[str]":
    """Lay out the resolved capabilities, then the pin, as :meth:`UserlandHost.probe` prints them.

    Two audiences in one output, in the order they are useful: the table says
    what this host will actually DO (assumed values included, since those are
    the values in force), and the payload below it says what a maintainer may
    safely RECORD. They differ exactly when something could not be asked, and
    the legend between them is what makes that difference actionable rather
    than mysterious.
    """
    rows = _capability_rows(userland)
    pin = userland.as_lab_json()
    assumed = [n for n, _, s in rows if s == "assumed"]
    name_w = max(len("capability"), *(len(n) for n, _, _ in rows))
    value_w = max(len("value"), *(len(v) for _, v, _ in rows))
    lines = [
        f"{'capability':<{name_w}}  {'value':<{value_w}}  source",
        *(f"{n:<{name_w}}  {v:<{value_w}}  {s}" for n, v, s in rows),
        "",
        _SOURCE_LEGEND,
        "",
    ]
    if not pin:
        lines.append(
            f"Nothing settled, so there is nothing to pin: all {len(rows)} values above are "
            "guesses, and recording one would make it permanent. otto abandons a resolution "
            f"after {_RESOLVE_BUDGET_S:.0f}s and refuses the next attempt for "
            f"{_RETRY_COOLDOWN_S:.0f}s once one has left anything unasked, so an empty pin "
            "says otto measured nothing this round -- not that the device has nothing. Run "
            "this again outside that window."
        )
        return lines
    if assumed:
        lines.append(
            f"Left out of the pin below because otto could not ask about them "
            f"({len(assumed)} of {len(rows)}): {', '.join(assumed)}. Pin the rest into this "
            "host's lab.json entry; otto asks about these again on a later connection."
        )
    elif all(s == "declared" for _, _, s in rows):
        lines.append(
            "Every capability is declared in this host's userland_options already, so none "
            "of them needed a probe. This is the pin in force:"
        )
    else:
        lines.append(
            "Every capability settled. Pin these into this host's lab.json entry and the "
            "next connection to this device issues no probe at all:"
        )
    lines.append("")
    payload = json.dumps(pin, indent=2, sort_keys=True).splitlines()
    lines.append(f'"userland_options": {payload[0]}')
    lines.extend(payload[1:])
    return lines


def _no_resolver_report(host_class: str) -> "list[str]":
    """Say plainly that a host whose ``_userland()`` answers ``None`` has no answers.

    Said rather than swallowed, because the alternative renderings are both
    worse than the hole: a crash blames the user's command for a property of
    the host class, and an empty pin is indistinguishable from a device that
    refused every probe. Naming it makes a recorded gap legible at the one
    moment someone is asking about it.
    """
    return [
        (
            f"{host_class} builds no Userland, so otto asked this host nothing and there is "
            "nothing to pin."
        ),
        "",
        (
            "That is a recorded hole and not a failure of this command: `_userland()` answers "
            "None by default, and otto's own UnixHost is the only host class that overrides "
            "it. What follows from that is written where the hook lives -- `UserlandHost` in "
            "`otto.host.userland` -- and was measured rather than assumed: elevation here "
            "keeps building today's `sudo`, `refuse_if_base64_is_absent` declines to refuse, "
            "and neither LocalHost nor DockerContainerHost carries a `userland_options` field "
            "to pin an answer into anyway."
        ),
    ]


def _dry_run_report() -> "list[str]":
    """Say plainly that a dry run measured nothing, instead of rendering an all-guess table.

    A MESSAGE, NOT A GUARD, and the distinction is worth stating because this
    used to be both. ``Userland._send`` refuses to issue a probe under a dry
    run, so the pin is empty here whether or not this branch exists -- the
    paste-safety property is enforced there, once, for every command that
    triggers a resolution rather than for this verb alone. What is left for
    this function is which of two true answers a user gets, and the table is
    the worse one: fourteen rows of ``assumed`` says what the host WOULD do,
    which is a real reading, but ``_probe_report``'s empty-pin paragraph then
    invites the reader to "run this again outside that window", and there is no
    window. A dry run will not settle anything however long they wait.
    """
    return [
        "Dry run: no probe was issued, so there is nothing to report and nothing to pin.",
        "",
        (
            "Deliberate. `Userland._send` declines to issue a probe under a dry run -- a "
            "dry run reaches no device, so a probe would have no exit code to read and "
            "nothing settles. There is genuinely nothing measured to show. Run this "
            "without --dry-run to reach the device."
        ),
    ]


class UserlandHost:
    """Mixin: the hook a host answers with its one :class:`Userland`, or ``None``.

    Declared here, once, because TWO sibling mixins read it and neither owns
    it: :class:`~otto.host.privilege.PosixPrivilege` (which mechanism
    ``run(sudo=True)`` elevates with) and
    :class:`~otto.host.file_ops.PosixFileOps` (whether the device has the
    ``base64`` its ``read_file``/``write_file`` are built on). It lived on the
    privilege mixin while elevation was its only reader; a second reader made
    that an accident of who arrived first. The two alternatives were a second
    identical default on the other mixin -- one hook with two definitions,
    where MRO decides which is in force -- and reaching into the privilege
    mixin's hook from ``file_ops``, which is the same coupling with less of a
    name. :class:`~otto.host.unix_host.UnixHost` overrides it and is the only
    host that does.

    ``None`` is the default and it means "this host has told us nothing", not
    "nothing works". :class:`~otto.host.local_host.LocalHost` and
    :class:`~otto.host.docker_host.DockerContainerHost` never acquire one, so
    each reader has to decide what an absent resolver means for it, and both
    answer the same way: ``PosixPrivilege._elevate`` keeps building today's
    ``sudo``, and ``otto.host.file_ops.refuse_if_base64_is_absent`` declines to
    refuse. That is :func:`refuse_if_gapped`'s own asymmetry one level down --
    "we were not told" must not become "does not work".

    **WHAT IT WOULD COST TO GIVE THOSE TWO A RESOLVER, MEASURED 2026-08-14.**
    Read this before adding one, because the second reader makes it look like a
    file-ops change and it is not: the hook is shared, so a resolver on either
    class also decides how every ``run(sudo=True)`` there elevates, and
    ``resolve()`` has no scoped form -- there is no way to settle
    ``base64_flag`` without also producing an ``elevation`` verdict. Three
    findings, each from a run rather than a reading:

    * **the mechanism really does move.** A ``Userland`` over
      ``LocalHost.exec`` on this machine issued 7 probes in 16 ms and answered
      ``elevation=sudo``, so the built command was byte-identical -- but that is
      a property of the machine, not of the change. Scripted against the shape
      ``alpine`` actually has (measured: BusyBox 1.36.1, ``/bin/su``, no
      ``sudo``), the same wiring builds ``su -c <cmd>`` with NO password expect
      (neither class has a ``creds`` field, so ``_switch_creds()`` is empty);
      with neither applet it RAISES
      :exc:`~otto.host.errors.UnsupportedOnUserlandError` where today the caller
      gets a non-ok ``CommandResult``. Two of the three arms are a change of
      behaviour on the two host families that reach otto's own machine.
    * **on ``LocalHost`` the probes would measure the wrong shell.** ``exec``
      runs them through ``loop.subprocess_shell`` (``/bin/sh``) while ``run``
      runs commands in a persistent ``bash`` (``LocalSession``), so the resolver
      would describe a shell the caller's commands never use: measured,
      ``shell_dialect`` resolved to ``"ash"`` on a machine that declares
      ``has_bash=True`` and really does run ``run()`` in bash, and the
      ``resolve()`` debug line offers that value as a pasteable ``lab.json``
      pin. Nothing consumes ``shell_dialect`` yet (see the module docstring's
      hole), which is the only reason this is latent rather than live.
    * **neither class can be pinned out of it.** ``userland_options`` is a
      :class:`~otto.host.unix_host.UnixHost` field, so the escape hatch
      ``otto.host.file_ops.refuse_if_base64_is_absent`` offers an operator --
      declare all seven and the round issues nothing -- does not exist here, and
      adding one is an init-field change that needs a spec field to reach a
      host from lab data.

    The decomposition that would make it safe is in
    ``todo/busybox-phase-5-followups-2026-08-13.md`` §2 under
    ``file-ops-base64``. The two ``PATH_OPEN`` records on that surface are the
    holes this leaves, and they say so.

    **IT ALSO CARRIES THE ``probe`` VERB**, and the hook is why it lives here
    rather than on :class:`~otto.host.unix_host.UnixHost`. ``@cli_exposed``
    scopes a verb by the class that defines it, so putting it on the hook's own
    class gives ``otto host <id> probe`` to exactly the hosts that answer the
    hook -- INCLUDING the two that answer ``None``. That is the point rather
    than a side effect: the paragraph above is a recorded hole, and a verb that
    was simply absent on those two classes would leave a user asking about the
    userland with no way to be told there is none.

    ``__slots__ = ()`` so it keeps composing with the ``@dataclass(slots=True)``
    hosts, exactly as the two mixins that inherit it do.
    """

    __slots__ = ()

    def _userland(self) -> "Userland | None":
        """Return this host's resolved userland capabilities, or None when it has none."""
        return None

    @cli_exposed(output_dir=False)
    async def probe(self) -> Result:
        """Resolve this host's userland capabilities and print the pin that skips them.

        RECON ONCE, THEN PIN -- that is the whole point, and it is why the
        pasteable payload is the product here rather than a footnote to a
        table. BusyBox devices are slow and :meth:`Userland.resolve` costs a
        probe round per fresh host object; a maintainer who runs this once and
        records the answers in that host's ``userland_options`` makes every
        later connection issue nothing, because ``_resolve_once`` asks only
        about names that are neither settled nor declared. Until now the pin
        was reachable only by reading a DEBUG log line, which is a poor place
        to keep the one output a user is meant to copy.

        ``value`` is the report's lines (``list[str]``), which the CLI renderer
        prints one per line. Ok in every arm, deliberately: the three
        interesting outcomes -- a full table, a partial one, and a host with no
        resolver at all -- are all ANSWERS, and none of them is this command
        failing. A non-zero exit on the last two would make a sweep across a
        lab look broken on exactly the hosts whose state otto has already
        recorded and explained.

        Named ``probe`` rather than ``userland`` for two reasons that point the
        same way: it names what running it COSTS on the device this exists for
        (a real probe round, bounded by ``_RESOLVE_BUDGET_S``), matching the
        other verbs that spend a round trip; and a public ``userland()`` beside
        the ``_userland()`` hook would read as that hook's public face while
        returning something else entirely.

        Inherited by every posix-shell host, including the two that answer
        ``None`` -- see ``_no_resolver_report`` for why that is a case to
        state rather than a case to hide.
        """
        if is_dry_run():
            return Result(Status.Skipped, value=_dry_run_report())
        userland = self._userland()
        if userland is None:
            return Result(Status.Success, value=_no_resolver_report(type(self).__name__))
        await userland.resolve()
        return Result(Status.Success, value=_probe_report(userland))


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
# WHICH CALL SITES CONSULT :func:`refuse_if_gapped` IS DATA, NOT PROSE. It is
# :attr:`Gap.paths` on each record below, one :class:`GapPath` per place otto
# touches the surface, and every count anything needs is derived from it --
# :func:`gap_path_totals`, :func:`table_guards`, :attr:`Gap.consults_the_table`,
# :attr:`Gap.fully_covered`. NO NUMBER IS WRITTEN IN THIS FILE BY HAND, and that
# is the whole reason the paths exist: this block used to say "THREE PRODUCT CALL
# SITES CONSULT refuse_if_gapped. EXACTLY THREE, and the count is the point",
# having previously said "none yet", then "exactly one", then "exactly two". A
# number a human retypes every change is a number that is wrong between changes,
# and this one was also AMBIGUOUS -- it counted GUARD FUNCTIONS while reading as
# though it counted sites, because ``read_file`` and ``write_file`` share one
# guard. Both counts are derived now and they are allowed to differ.
#
# WHY A SURFACE IS NOT A CALL SITE, which is the mistake the old count made
# structurally rather than by drifting. A surface is a fact about the DEVICE,
# measured once; a path is a place OTTO touches it, and otto touches most of
# these from more than one. Wiring one path leaves the others exactly as broken
# while a count of wired surfaces reports the surface done -- and three such
# holes were live in this repo, unmentioned by any count, when the paths were
# written: ``run-command-line-length`` is guarded on ``SessionManager.run_cmd``
# and open on a named session and on a pooled ``exec()``, and
# ``file-ops-base64`` can never fire on ``LocalHost`` or a docker container
# host. Read :data:`PATH_OPEN` for what that state is for.
#
# WHAT A WIRED PATH LOOKS LIKE, and it is the shape every registry consumer
# takes: the CALLER decides that this host belongs to the measured class -- a
# declared shell dialect of ``ash``, a declared ``has_bash=False``, a SETTLED
# ``base64_flag == "absent"``, a SETTLED ``applet_scp == "absent"`` -- and the
# TABLE decides whether that class is
# refused at all. Neither half is enough alone, which is why the record's own
# ``refuses`` cannot be the whole trigger and why no wired call site carries its
# own copy of the message. MOST of the guards key on a PROBE rather than a
# declaration and therefore await a :meth:`Userland.resolve`; that trade is
# argued at :func:`otto.host.file_ops.refuse_if_base64_is_absent`, at
# :func:`otto.host.unix_host.shutdown_command`, at
# :func:`otto.host.transfer.scp.refuse_if_scp_is_absent` and at
# :func:`otto.host.transfer.nc.refuse_if_nc_rejects_dash_n`, not here -- and they
# do not pay the same price, which is why each argues its own. Only the scp one
# ADDS a resolution to a path that awaited none before; the nc one rides the
# resolution ``NcFileTransfer.prepare`` already awaits on every transfer, and
# what it adds instead is two probes to the round, on every host, for a
# capability most hosts never read.
#
# NOT EVERY TABLE-BACKED PATH IS A REFUSAL, and :data:`PATH_ADAPTED` is where
# that stops being true. ``shutdown-command``'s guard has the same two halves --
# the caller decides the host is in the measured class, the table decides
# whether that class is refused -- and then does something no other consumer
# does: on the measured class it EMITS the spelling the device has and the
# operation succeeds. The record is the authority only for the residue, a device
# with neither spelling, which no matrix row is. Read :data:`PATH_ADAPTED`
# before adding the next consumer, because "wire it and refuse" is not the only
# shape any more.
#
# WIRING A RAISE SITE STAYS A PER-SURFACE DECISION that belongs with the call
# site being changed, and the ``reason`` strings of the unwired surfaces are
# still written to say what a call site WOULD refuse. Some surfaces having made
# that decision does not generalise to the rest, and the docs page states each
# surface's status separately for the same reason.
#
# NOT EVERY REFUSAL IN OTTO IS THIS TABLE'S, and the distinction is what
# :data:`PATH_PROBE_REFUSED` exists to keep straight. ``PosixPrivilege._elevate``
# and ``ShellFileTransfer._run_put``/``_run_get`` refuse on what the host in
# front of them ANSWERED and render their OWN messages -- a different thing from
# this table's "otto measured this on the matrix", and downgrading a record would
# not stop them. Only the transfer pair is recorded as a path, because
# ``shell-transfer-base64`` is a surface in this table and elevation is not.
# Note what a WIRED path does NOT do to that distinction: its PREDICATE may be a
# probe, but its VERDICT and its MESSAGE are the record's, so a probe may decide
# that a host is in the measured class without becoming a second authority on
# whether the class is refused.

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

# A dotted python name inside otto -- what :attr:`GapPath.site` and
# :attr:`GapPath.checked_by` are. Validated at construction for SHAPE only;
# whether the name RESOLVES, and whether the code it names does what the record
# claims, is checked in ``tests/unit/host/test_gap_registry.py``. Deliberately
# not resolved here: this module is imported on every CLI ``--help`` path, and a
# table that imported ``otto.link`` and ``otto.tunnel`` to validate itself would
# put the whole product behind the import budget guard.
_DOTTED_RE = re.compile(r"^otto(\.[A-Za-z_][A-Za-z0-9_]*)+$")

# ``tests/…/test_x.py::TestClass::test_name`` or ``…::test_name``.
_NODE_ID_RE = re.compile(r"^tests/[\w./-]+\.py(::[A-Za-z_][A-Za-z0-9_]*)+$")

PATH_ADAPTED = "ADAPTED"
"""This path meets the gapped surface and does the thing the device CAN do.

THE FIFTH STATE, and the first one that is good news. The other four all answer
"am I protected" -- three ways of being refused and one way of being broken --
because until ``shutdown-command`` every record described a surface otto could
only decline. This one answers a different question: otto asks the device which
spelling it has and emits that, so a host in the measured class is SERVED here
rather than refused. Recording it as :data:`PATH_WIRED` would tell a reader otto
still turns a BusyBox shutdown away, which is the one thing this state exists to
stop them concluding.

The measurement behind the record does NOT go away when a path reaches this
state, which is why the fix is a path state and not a deletion:
``shutdown`` really is absent on all five matrix rows, and a future caller that
hard-codes it needs to find that written down. The record's :attr:`Gap.status`
stays :data:`MEASURED_BROKEN` for the same reason it does on ``daemon-launch``
and ``run-command-line-length`` -- the status is about the DEVICE, and the
device has not changed.

WHAT IT OBLIGES, and both are enforced in ``__post_init__``.
:attr:`GapPath.checked_by` names the code that makes the choice, so the claim is
resolvable rather than prose. :attr:`GapPath.pinned_by` is REQUIRED, as it is for
:data:`PATH_PROTECTED` and for the same reason: "otto handles this now" is a
claim that stops being true silently. A refactor that lost the probe would put
``shutdown -h now`` back on the wire, the record would still read ADAPTED, and
nothing but the named test would notice.

WHY THE ``shell-transfer-base64`` PATHS ARE NOT THIS, though they also degrade
before they refuse. Those two take the refusal for a host otto meets in normal
service: ``ShellFileTransfer._select_codec`` raises on an UNSETTLED
``base64_flag``, because a probe round that never arrived does not get to choose
a codec, and a refused probe round is an ordinary event on a busy sshd. This
state's claim is stronger and is what earns it a name -- nothing otto can serve
is refused here, and an unsettled probe DEGRADES to the pre-existing GNU
spelling rather than raising. The only refusal left is for a device that
answered, on a batch that landed, that it has neither spelling.
"""

PATH_WIRED = "WIRED"
"""This path reads this record and refuses from it. The table is the authority.

:attr:`GapPath.checked_by` names the guard, and that claim is CHECKED rather
than trusted: the named guard has to exist, the site has to call it, and the
guard has to reach :func:`refuse_if_gapped` with this record's own surface. See
``tests/unit/host/test_gap_registry.py``.
"""

PATH_PROBE_REFUSED = "PROBE_REFUSED"
"""This path refuses, but on its OWN authority rather than this table's.

THE FOURTH STATE, and it exists because ``shell-transfer-base64`` is neither of
the obvious two. ``ShellFileTransfer._run_put`` (in ``otto.host.transfer.shell``)
reads :attr:`Userland.base64_flag`, raises
:exc:`~otto.host.errors.UnsupportedOnUserlandError` itself, and renders its own
message -- so calling it :data:`PATH_WIRED` would be false (downgrading this
record to :data:`UNTESTED` would not stop that refusal, and the message carries
none of the record's evidence or docs anchor), while calling it
:data:`PATH_OPEN` would be false the other way (nothing is silently broken; otto
declines before it sends).

WHAT A READER MUST TAKE FROM IT: the user is protected here, and this table is
a RECORD of the surface rather than the thing deciding it. WHAT A FUTURE
IMPLEMENTER MUST NOT DO: read it as a hole and add a second refusal. Wiring such
a path means MOVING the verdict onto the record, not adding one beside it.
"""

PATH_PROTECTED = "PROTECTED"
"""This path cannot reach the gapped operation -- something upstream refuses first.

Load-bearing on its own: without it ``otto.tunnel.manage.add_tunnel`` reads as a
permanent hole, and the fix someone reaches for is a guard that cannot fire --
this repo's most common defect. :attr:`GapPath.checked_by` names the protector
and :attr:`GapPath.pinned_by` the test that holds it in place.
"""

PATH_ATTRIBUTED = "ATTRIBUTED"
"""This path attempts the surface, it fails, and this record names the failure.

THE SIXTH STATE, and the first one that is neither a refusal nor a hole. The
other five all answer "was the attempt made" the same way -- four say no
(something declined first) and :data:`PATH_OPEN` says yes and nothing was
learned from it. This one says the attempt was made, it failed on the DEVICE's
authority, and otto turned the device's answer into this record's.

WHY ``sftp-transfer`` COULD NOT TAKE ANY OF THE OTHER FIVE, and the reason this
state exists rather than a sixth guard. Whether a device serves sftp is a
property of its SSH SERVER's subsystem configuration, and the only thing that
answers it is opening the subsystem -- which is the operation. There is no fact
to pre-check: ``sftp-server`` is not on ``PATH`` even on a healthy GNU host (see
the applet-probe block above, which excludes it for exactly this reason), the
absolute path differs across distros and is compiled in on dropbear, and the
daemon is not the authority either. Every pre-check available answers "absent"
on hosts where sftp works, which is a refusal of a working host -- the one
mistake this whole table is ordered to avoid. So the attempt happens, and what
this state adds is that its failure arrives as otto's sentence instead of
asyncssh's.

WHAT IT BUYS OVER :data:`PATH_OPEN`, measured rather than asserted. Left open,
``UnixHost.put`` against a device with no sftp-server raises
``asyncssh.sftp.SFTPConnectionLost: 0 bytes read on a total of 4 expected
bytes`` -- prompt (22ms), residue-free, and naming nothing: not the subsystem,
not the device, not otto's surface, and not the ``shell`` backend this record
says to use instead. It reads as a truncated connection, so the diagnosis it
invites is "the link is flaky". Attributed, the same failure carries the four
facts every other refusal here carries.

WHAT IT DOES NOT CLAIM: that anything is protected. A caller who puts a file
over sftp to such a device still gets an exception and still moves no bytes.
This state is about the MESSAGE, and a reader must not count it as coverage in
the sense :data:`PATH_WIRED` means -- nothing was prevented, only explained.

WHAT IT OBLIGES, both enforced in ``__post_init__`` and both for the reasons
:data:`PATH_ADAPTED` gives. :attr:`GapPath.checked_by` names the code that does
the translating, and it is table-backed, so the same three checks that hold a
:data:`PATH_WIRED` claim hold this one: the guard exists, the site calls it, and
it reaches :func:`refuse_if_gapped` with this record's surface. That last one is
what keeps the table the authority -- downgrading this record to
:data:`UNTESTED` does not merely stop a message, it puts asyncssh's own error
back in the caller's hands untouched. :attr:`GapPath.pinned_by` is REQUIRED
because "otto explains this now" stops being true as silently as "otto handles
this now" does.
"""

PATH_OPEN = "OPEN"
"""This path is reachable, touches the gapped surface, and is still unguarded.

The state that has to be impossible to miss: a surface can be wired at one call
site and silently broken at another, and the count of wired sites says nothing
about that. Every :data:`PATH_OPEN` path is rendered on
:data:`GAP_DOCS_PAGE`, pinned in both directions by
``tests/unit/test_docs_gap_sync.py``, so a hole is visible to a reader and not
only to this table.
"""

_PATH_STATES = [
    PATH_ADAPTED,
    PATH_WIRED,
    PATH_PROBE_REFUSED,
    PATH_PROTECTED,
    PATH_ATTRIBUTED,
    PATH_OPEN,
]
"""The six states, ordered strongest-coverage first. Renders in this order.

:data:`PATH_ADAPTED` leads because it is the only one where the operation
SUCCEEDS on the measured device; the three after it are three ways of being
refused; :data:`PATH_ATTRIBUTED` comes next because the operation still fails
there and only its message is otto's; and :data:`PATH_OPEN` is the hole.

The ordering is a claim about how much a reader is protected, so
:data:`PATH_ATTRIBUTED` sitting BELOW the three refusals is deliberate and not
alphabetical drift: an attributed path prevents nothing.
"""


@dataclass(frozen=True)
class GapPath:
    """One place otto TOUCHES a gapped surface, and what is true of it there.

    **A surface is a fact about the DEVICE; a path is a place otto touches it.**
    One measurement, many call sites -- which is why these hang off a
    :class:`Gap` rather than replacing it. otto reaches most of these surfaces
    from more than one place, and wiring one of them leaves the others silently
    broken while a count of wired surfaces reports the surface done. Three such
    holes were live in this repo when these records were written, all of them
    invisible to a table that recorded only surfaces.

    Frozen for the same reason :class:`Gap` is: every consumer reads and none
    writes.
    """

    site: str
    """The call site, as a dotted name under ``otto.`` -- ``module.Class.method``
    or ``module.function``.

    Resolvable, and RESOLVED by the test module: a renamed method or a moved
    function reddens rather than sitting here as stale prose. Shape only is
    checked at construction, because resolving it would mean importing
    ``otto.link`` and ``otto.tunnel`` from a module on the ``--help`` path.
    """

    state: str
    """One of :data:`PATH_ADAPTED`, :data:`PATH_WIRED`,
    :data:`PATH_PROBE_REFUSED`, :data:`PATH_PROTECTED`, :data:`PATH_OPEN`. Read
    those five for what each means."""

    detail: str
    """What this state MEANS at this site, in the reader's terms. Never empty.

    The state is the verdict and this is the evidence for it -- why the path is
    still open, or what makes it unreachable. A state with no detail is a claim
    with no argument, so ``__post_init__`` refuses one.
    """

    checked_by: str = ""
    """The dotted name of the code that makes this path safe, where one exists.

    :data:`PATH_WIRED`: the guard that consults this table -- REQUIRED, and
    checked to actually reach :func:`refuse_if_gapped` with this record's
    surface. :data:`PATH_ADAPTED`: the code that picks what the device can
    actually run -- REQUIRED, and checked the same way, because the one
    adaptation there is also the record's authority for the device that can run
    neither spelling. :data:`PATH_PROTECTED`: the upstream refusal -- REQUIRED,
    and checked to exist. Empty for the other two, and that is enforced:
    :data:`PATH_OPEN` has nothing to name, and :data:`PATH_PROBE_REFUSED`'s
    check is inline at the site rather than in a named function.
    """

    pinned_by: str = ""
    """A pytest node id for the test that holds this path where it is, if any.

    ``tests/…/test_x.py::TestClass::test_name``. Checked to EXIST -- the file is
    read and the named test looked for -- so a deleted or renamed test reddens
    here instead of leaving the record pointing at nothing. Optional, because
    not every path has one and inventing a name would be worse than admitting
    it; REQUIRED for :data:`PATH_PROTECTED`, whose whole claim is that something
    else refuses first and keeps refusing, and for :data:`PATH_ADAPTED`, whose
    claim that otto handles this now stops being true just as silently.
    """

    def __post_init__(self) -> None:
        if not _DOTTED_RE.match(self.site):
            raise ValueError(
                f"gap path site {self.site!r} is not a dotted name under `otto.`. It has "
                f"to be resolvable (`otto.host.session.SessionManager.run_cmd`), because "
                f"the site is what a stale claim is caught by -- prose cannot be resolved"
            )
        if self.state not in _PATH_STATES:
            raise ValueError(
                f"gap path {self.site!r} has state {self.state!r}, not one of {_PATH_STATES}"
            )
        if not self.detail:
            raise ValueError(
                f"gap path {self.site!r} is {self.state} and says nothing about what that "
                f"means here. A state with no detail is a verdict with no argument"
            )
        needs_checker = self.state in (
            PATH_WIRED,
            PATH_PROTECTED,
            PATH_ADAPTED,
            PATH_ATTRIBUTED,
        )
        if needs_checker and not self.checked_by:
            raise ValueError(
                f"gap path {self.site!r} is {self.state} and names nothing in `checked_by`. "
                f"{PATH_WIRED} has to name the guard that consults this table, "
                f"{PATH_ADAPTED} the code that picks what the device can run, "
                f"{PATH_ATTRIBUTED} the code that turns the device's failure into this "
                f"record's message, and {PATH_PROTECTED} the code that refuses upstream -- "
                f"unnamed, none of the four claims can be checked, which is the whole point "
                f"of recording it"
            )
        if not needs_checker and self.checked_by:
            raise ValueError(
                f"gap path {self.site!r} is {self.state} and names {self.checked_by!r} in "
                f"`checked_by`. {PATH_OPEN} means nothing guards it, and "
                f"{PATH_PROBE_REFUSED} means the check is inline at the site rather than a "
                f"named function -- neither may claim a checker"
            )
        if self.checked_by and not _DOTTED_RE.match(self.checked_by):
            raise ValueError(
                f"gap path {self.site!r} names checked_by={self.checked_by!r}, which is not "
                f"a dotted name under `otto.`; it has to resolve for the claim to be checked"
            )
        if self.state == PATH_PROTECTED and not self.pinned_by:
            raise ValueError(
                f"gap path {self.site!r} is {PATH_PROTECTED} and names no test. "
                f"'unreachable' is the one state that stops being true silently -- the "
                f"upstream refusal is somebody else's code and nothing here would notice "
                f"it going away, so the test that pins it is part of the claim"
            )
        if self.state == PATH_ADAPTED and not self.pinned_by:
            raise ValueError(
                f"gap path {self.site!r} is {PATH_ADAPTED} and names no test. "
                f"'otto handles this now' stops being true as quietly as 'unreachable' "
                f"does: a refactor that dropped the probe would put the broken spelling "
                f"back on the wire and leave this record reading ADAPTED, so the test "
                f"that pins the choice is part of the claim"
            )
        if self.state == PATH_ATTRIBUTED and not self.pinned_by:
            raise ValueError(
                f"gap path {self.site!r} is {PATH_ATTRIBUTED} and names no test. "
                f"'otto explains this now' stops being true as quietly as 'otto handles "
                f"this now' does, and more so: the translation only runs on a device that "
                f"has ALREADY failed, so losing it restores a green suite and an operator "
                f"back on asyncssh's own message, with nothing else reddening"
            )
        if self.pinned_by and not _NODE_ID_RE.match(self.pinned_by):
            raise ValueError(
                f"gap path {self.site!r} names pinned_by={self.pinned_by!r}, which is not a "
                f"pytest node id (`tests/…/test_x.py::TestClass::test_name`); it has to be "
                f"resolvable for the claim to be checked"
            )


@dataclass(frozen=True)
class Gap:
    """One thing otto cannot do on a userland, with the evidence behind it.

    Frozen because every consumer reads and none writes: the docs page renders
    it, the error renders it, and the parity queue points at it.

    **Frozen but NOT hashable, and that inference is the reason this is written
    down.** ``@dataclass(frozen=True)`` generates a ``__hash__`` over the
    fields, and :attr:`paths` is a ``list``, so the generated one raises
    ``TypeError: unhashable type: 'list'`` when it is finally called --
    ``set(GAPS)`` and ``{gap: ...}`` both fail that way, naming the list and not
    the field that holds it. Nothing hashes a record today, so this is a
    limitation rather than a bug, and the field type is not the thing to
    "fix": lists are this project's API default and a ``tuple[GapPath, ...]``
    would make every record's literal noisier to buy an ability nobody uses.
    :class:`GapPath` IS hashable (every field a ``str``), so a caller that wants
    a set of paths already has one. A caller that needs a set of RECORDS should
    key on :attr:`surface`, which is unique across the table and pinned so by
    ``test_every_surface_is_unique`` in ``tests/unit/host/test_gap_registry.py``.

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

    **Name the code by what it EMITS, not by ``file.py:<line>``.** This string
    renders into the operator's error message, and a line number in it is the
    one claim here that nothing resolves: ``file-ops-base64`` carried two, both
    two lines stale by the time anyone re-read them, and dropping them cost
    nothing because :attr:`paths` already carries the same call sites as dotted
    names a test resolves. NO RECORD BELOW CITES ONE ANY MORE: ``nc-transfer``
    carried the last of them until its own rewrite took it out, which is what
    ``shutdown-command``'s rewrite had already done with the second. Keep it
    that way -- the sites are in ``paths``, where something resolves them.
    """

    queued_for: str
    """The workstream that would close this gap, or why nothing is queued.

    The parity queue's entry point. Never empty: "nobody owns this" is itself
    an answer a reader needs, and it is spelled out rather than left blank.
    """

    paths: list[GapPath] = field(default_factory=list)
    """Every place otto TOUCHES this surface, and what is true of it there.

    The surface is one measurement; this is the list of call sites it reaches
    otto through, and the two are not the same shape. A surface wired at one
    site and unguarded at another is COVERED by any count of wired surfaces and
    still broken for the caller who took the other path.

    Compulsory for :data:`MEASURED_BROKEN` and forbidden for :data:`UNTESTED`,
    both enforced in ``__post_init__``: a refusing record with no paths says
    nothing about otto and would vanish from the coverage view, while the
    untested records are test-fidelity gaps rather than call sites and inventing
    paths for them would be inventing evidence.

    Every count anything needs is DERIVED from this -- :attr:`open_paths`,
    :attr:`fully_covered`, :func:`gap_path_totals`, :func:`table_guards`.
    Nothing retypes one.
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
        # The coverage half of the same idea: a record must not be able to claim
        # coverage it does not have, in either direction.
        if self.status == MEASURED_BROKEN and not self.paths:
            raise ValueError(
                f"gap {self.surface!r} is {MEASURED_BROKEN!r} and names no path otto "
                f"touches it from. The surface is a fact about the device; a path is a "
                f"place otto meets it, and a record with none says nothing about otto -- "
                f"it would sit in the coverage view as neither wired nor open. Name at "
                f"least the site the measurement was taken against"
            )
        if self.status == UNTESTED and self.paths:
            raise ValueError(
                f"gap {self.surface!r} is {UNTESTED!r} and carries "
                f"{len(self.paths)} path(s). An untested record is not a call site -- it is "
                f"a surface nobody has run, and two of the three are test-fidelity gaps "
                f"rather than otto code at all. Do not invent paths for them"
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

    def paths_in_state(self, state: str) -> list[GapPath]:
        """Return this record's paths in *state*, in declaration order.

        Raises:
            ValueError: *state* is not one of the four. A typo'd state would
                otherwise answer "no paths" forever, which for a caller counting
                holes is a count that cannot rise.
        """
        if state not in _PATH_STATES:
            raise ValueError(f"{state!r} is not a gap path state; there are {_PATH_STATES}")
        return [p for p in self.paths if p.state == state]

    @property
    def wired_paths(self) -> list[GapPath]:
        """The paths that read THIS record and refuse from it."""
        return self.paths_in_state(PATH_WIRED)

    @property
    def adapted_paths(self) -> list[GapPath]:
        """The paths where otto does what the device CAN do. The fixed ones."""
        return self.paths_in_state(PATH_ADAPTED)

    @property
    def attributed_paths(self) -> list[GapPath]:
        """The paths that still fail, with this record's sentence instead of a stranger's."""
        return self.paths_in_state(PATH_ATTRIBUTED)

    @property
    def open_paths(self) -> list[GapPath]:
        """The paths that are reachable and still unguarded. The holes."""
        return self.paths_in_state(PATH_OPEN)

    @property
    def table_backed_paths(self) -> list[GapPath]:
        """Every path whose MESSAGE is THIS record's.

        :data:`PATH_WIRED`, :data:`PATH_ADAPTED` and :data:`PATH_ATTRIBUTED`,
        in that order.

        Three states rather than one because all three reach
        :func:`refuse_if_gapped`, and for reasons that differ in what they do
        for the caller and agree in where the verdict lives. An
        :data:`PATH_ADAPTED` path serves the measured device and keeps the
        record as the floor for a device that can run neither spelling. An
        :data:`PATH_ATTRIBUTED` path serves nobody -- the operation has already
        failed -- and hands the record's four facts back instead of a
        stranger's byte count. A count that read only :data:`PATH_WIRED` would
        leave both guards out of :func:`table_guards` and answer
        :attr:`consults_the_table` ``False`` for records the guards name by
        surface -- a derived value that lies, which is the defect these paths
        exist to remove.

        NOT A COVERAGE COUNT, and the name says so: "table-backed" is about
        WHOSE SENTENCE the caller gets, not about whether anything was
        prevented. :attr:`fully_covered` is the other question and it treats
        these three alike for a different reason -- none of them is
        :data:`PATH_OPEN`, meaning none is silently broken.
        """
        return self.wired_paths + self.adapted_paths + self.attributed_paths

    @property
    def consults_the_table(self) -> bool:
        """Whether this record is the AUTHORITY anywhere -- any table-backed path.

        The question the docs page's "which surfaces does otto refuse from this
        table" prose is asking, and it is NOT :attr:`fully_covered`: the two
        disagree on ``shell-transfer-base64``, which refuses everywhere it is
        reachable and reads none of it from here. Answering that question with
        :attr:`fully_covered` would tell a reader the table decides something it
        does not.
        """
        return bool(self.table_backed_paths)

    @property
    def fully_covered(self) -> bool:
        """Whether no path otto touches this surface from is left silently broken.

        ``True`` when there is at least one path and none of them is
        :data:`PATH_OPEN` -- every place otto meets this surface either refuses
        (from this table, or on its own probe), cannot be reached at all, does
        what the device can do instead, or fails and SAYS SO in this record's
        words.

        SILENTLY is the load-bearing word, and :data:`PATH_ATTRIBUTED` is what
        made it earn its place: that state's operation still fails, so reading
        this as "the caller is protected" would be wrong there. What it means
        is that no path meets this surface and leaves the caller without the
        record -- which is the property the docs page's hole list is about.

        NOT NAMED ``fully_wired``, deliberately. A :data:`PATH_PROBE_REFUSED`
        path is covered and is not wired, so ``fully_wired`` would be a false
        name for ``shell-transfer-base64`` -- and a derived value that lies is
        exactly the defect paths exist to remove. Read :attr:`consults_the_table`
        for the wiring question; they are different questions and both are asked.
        """
        return bool(self.paths) and not self.open_paths


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
            "the `shell` transfer backend prefers the device's own `base64` for every "
            "chunk, and a userland with no `base64` applet used to be unable to use it "
            "at all. It now FALLS BACK to `uuencode`/`uudecode` on a device measured to "
            "have those instead -- which is every BusyBox row in this matrix, including "
            "the 1.16.1 one that has no `base64`. So this record is a measurement about "
            "the applet, not a verdict about the backend: the transfer still works, one "
            "command per chunk plus a scratch file, and the only devices refused are "
            "those with NEITHER codec. base64 stays the preference where it exists, "
            "being the cheaper shape on the wire."
        ),
        measured_on=(
            "BusyBox 1.16.1 ships no `base64` applet -- `tests/busybox/"
            "test_applet_resolution.py`'s `_EXPECTED_BASE64` records False for that row "
            "and `tests/busybox/test_shell_codec_contracts.py`'s `_EXPECTED_BASE64_FLAG` "
            "records None, while 1.21.1 and every later matrix row decode with `-d`"
        ),
        queued_for=(
            "nothing for the codec itself -- `todo/busybox-parity-sweep-2026-08-11.md`'s "
            "uu item is built and covered per row in "
            "`tests/busybox/test_shell_codec_contracts.py`. What remains queued is the "
            "PTY path: a `term: telnet` BusyBox host routes this backend through a pooled "
            "shell session whose line editor truncates at 1022 characters (the "
            "`run-command-line-length` record), and neither codec's chunk command has "
            "been measured there"
        ),
        paths=[
            GapPath(
                site="otto.host.transfer.shell.ShellFileTransfer._run_put",
                state=PATH_PROBE_REFUSED,
                detail=(
                    "DEGRADES first and refuses second, both on its OWN authority. "
                    "`_select_codec` reads `Userland.base64_flag` and, on a settled "
                    "absence, switches to the `uuencode` codec rather than declining -- so "
                    "on the 1.16.1-shaped device this record is about, the surface is "
                    "routed around and the transfer happens. What still refuses is a "
                    "device with NEITHER codec (`applet_uudecode` settled absent too) and "
                    "a host whose probe round never arrived at all, which is kept distinct "
                    "from a measured absence because a refused probe must not select a "
                    "codec. Both raise `UnsupportedOnUserlandError` with their own message "
                    "and consult this record for none of it, so the user is protected here "
                    "and this table is the RECORD rather than the thing deciding: "
                    "downgrading it to `untested` would not stop either refusal. Wiring it "
                    "means MOVING the verdict onto this record, never adding a second "
                    "refusal beside these"
                ),
                pinned_by=(
                    "tests/unit/host/transfer/test_shell_transfer.py::TestShellPutRefusal"
                    "::test_neither_codec_raises_before_any_command"
                ),
            ),
            GapPath(
                site="otto.host.transfer.shell.ShellFileTransfer._run_get",
                state=PATH_PROBE_REFUSED,
                detail=(
                    "the same degrade-then-refuse as `_run_put`, through the same "
                    "`_select_codec`, with one difference that is not cosmetic: GET needs "
                    "`uuencode` where PUT needs `uudecode`, because the device only "
                    "ENCODES here. They are separate applets, so a device could support "
                    "one direction and not the other. GET also checks its size probe "
                    "first and the codec second, which changes the order of two refusals "
                    "and nothing about this one"
                ),
                pinned_by=(
                    "tests/unit/host/transfer/test_shell_transfer.py::TestShellGetRefusal"
                    "::test_neither_codec_raises_before_any_command"
                ),
            ),
        ],
    ),
    Gap(
        surface="file-ops-base64",
        status=MEASURED_BROKEN,
        reason=(
            "`Host.read_file` and `Host.write_file` move their payload through the "
            "device's `base64` too, and unlike the `shell` transfer they hard-code it: "
            "`src/otto/host/file_ops.py` emits `base64 <path>` and `... | base64 -d` "
            "whatever `Userland.base64_flag` says. They cannot ADAPT, so otto REFUSES "
            "both up front instead, on any host whose userland actually settled "
            "`base64_flag` on `absent` -- rather than emitting a command that comes "
            "back as the device's own `not found`, attributed to the file the caller "
            "asked for. What the refusal replaces is a `FileNotFoundError` naming a "
            "file that is present, and, for a write, a destination truncated to zero "
            "bytes by the redirect before the missing applet was ever reached. Only "
            "these two are refused: `put`/`get` choose a transfer backend and are "
            "covered by `shell-transfer-base64`"
        ),
        measured_on=(
            "the same 1.16.1 rows as `shell-transfer-base64`, against the two call sites "
            "`paths` names below, both of which still emit the same fixed spelling. NO "
            "LINE NUMBERS, deliberately: `paths` carries each site as a dotted name a "
            "test RESOLVES, so a rename or a move reddens instead of sitting here as "
            "stale prose -- which is what the two numbers this field used to carry had "
            "already become, each off by two, in a string that renders into the "
            "operator's own error message. The DESTRUCTIVE half was "
            "measured directly, 2026-08-14, running the 1.16.1 artifact's own ash with "
            "`PATH=/nonexistent` (the isolation `tests/busybox/"
            "test_applet_resolution.py` records): `echo aGk= | base64 -d > <file>` "
            "against a 17-byte file answered rc=127 `sh: base64: not found` and left "
            "that file at 0 bytes, since the shell opens the redirect before it "
            "resolves the command. `>>` (otto's `append=True`) left it intact"
        ),
        queued_for=(
            "the REFUSAL has landed, in `otto.host.file_ops.refuse_if_base64_is_absent` "
            "-- this registry's third product call site, and the first whose predicate "
            "is a probe rather than a declaration. A FIX is still the full-parity "
            "workstream's, `todo/busybox-parity-sweep-2026-08-11.md`: the codec probe "
            "queued for `shell-transfer-base64` is what these two would read, so the "
            "two are one change and not two. The record stays `measured-broken` because "
            "the surface still is -- otto now declines the operation instead of "
            "emitting one it cannot run"
        ),
        paths=[
            GapPath(
                site="otto.host.file_ops.PosixFileOps.read_file",
                state=PATH_WIRED,
                checked_by="otto.host.file_ops.refuse_if_base64_is_absent",
                detail=(
                    "reads this record through the guard and declines before it emits "
                    "`base64 <path>`. The guard's PREDICATE is a probe -- a SETTLED "
                    "`base64_flag == 'absent'` -- and its VERDICT and MESSAGE are this "
                    "record's, which is what makes the table the authority here"
                ),
                pinned_by=(
                    "tests/unit/host/test_file_ops_base64_refusal.py::TestReadFileArrivesAtTheGuard"
                    "::test_a_device_with_no_base64_is_refused_before_anything_is_read"
                ),
            ),
            GapPath(
                site="otto.host.file_ops.PosixFileOps.write_file",
                state=PATH_WIRED,
                checked_by="otto.host.file_ops.refuse_if_base64_is_absent",
                detail=(
                    "the same guard, and the more valuable of the two: the command it "
                    "declines to emit is DESTRUCTIVE on exactly the device that cannot run "
                    "it, since the shell opens `> <path>` before it resolves `base64`. Two "
                    "sites, one guard -- which is why the count of wired PATHS and the count "
                    "of wired GUARDS are different numbers, both derived"
                ),
                pinned_by=(
                    "tests/unit/host/test_file_ops_base64_refusal.py::TestWriteFileArrivesAtTheGuard"
                    "::test_a_device_with_no_base64_is_refused_and_the_file_is_untouched"
                ),
            ),
            GapPath(
                site="otto.host.local_host.LocalHost._userland",
                state=PATH_OPEN,
                detail=(
                    "`LocalHost` mixes in `PosixFileOps` and never builds a `Userland`: it "
                    "inherits `UserlandHost._userland`, which answers `None`, so "
                    "`refuse_if_base64_is_absent` returns on its `None` arm before it ever "
                    "reads this record. Nothing has been measured about such a host's "
                    "userland, so the refusal correctly does not fire -- but the SURFACE is "
                    "still reachable, and a local shell without `base64` gets the "
                    "`FileNotFoundError`-blaming-a-present-file failure this record "
                    "describes. Closing it means giving the host a resolver, not widening "
                    "the guard -- and it stays open because that resolver was MEASURED "
                    "(2026-08-14) to change more than this surface: it also decides how "
                    "every local `run(sudo=True)` elevates, and its probes run in "
                    "`exec`'s `/bin/sh` rather than the `bash` `run()` uses, which "
                    "resolved `shell_dialect` to `ash` on a machine declaring "
                    "`has_bash=True`. See `UserlandHost` for the three findings and "
                    "`todo/busybox-phase-5-followups-2026-08-13.md` §2 for the "
                    "decomposition. The exposure this leaves is also the smaller of the "
                    "two: the device here is the machine otto itself runs on, and every "
                    "`base64` otto has measured absent was BusyBox 1.16.1"
                ),
                pinned_by=(
                    "tests/unit/host/test_file_ops_base64_refusal.py::TestTheFamiliesWithNoResolver"
                    "::test_the_hook_is_declared_once_and_only_unix_host_overrides_it"
                ),
            ),
            GapPath(
                site="otto.host.docker_host.DockerContainerHost._userland",
                state=PATH_OPEN,
                detail=(
                    "the same `None` arm as `LocalHost`, and the sharper case of the two: an "
                    "`alpine` container IS a BusyBox userland, so this is a host otto can "
                    "meet the measured class on and will never refuse. It has no resolver to "
                    "settle `base64_flag` with, so the guard has nothing to key on. Sharper, "
                    "but NOT live on the flagship image: measured 2026-08-14 against "
                    "`alpine:3.20`, BusyBox 1.36.1 ships `/bin/base64` and a `base64 | "
                    "base64 -d` round trip returns its input, matching the matrix "
                    "(`tests/busybox/test_applet_resolution.py` records `base64` absent on "
                    "1.16.1 alone). What is exposed is an image with the applet compiled "
                    "out. Wiring it is held for the same shared-hook reason as `LocalHost` "
                    "-- see `UserlandHost` -- with one cost that is this class's own: every "
                    "probe is a `docker exec` dispatched as one exec channel on the PARENT, "
                    "so the first elevated command or `read_file` would spend 7-11 of them "
                    "against a server that refuses excess channels rather than queueing "
                    "them, with no `userland_options` to pin them out. The elevation arm it "
                    "would take is measured rather than guessed: `alpine` has `su` and no "
                    "`sudo`, so `run(sudo=True)` would move from today's `sudo -S -p ...` "
                    "(rc 127, `sudo: not found`) to `su -c <cmd>`, which succeeds as root "
                    "and answers `su: must be suid to work properly` under `-u 1000`"
                ),
                pinned_by=(
                    "tests/unit/host/test_file_ops_base64_refusal.py::TestTheFamiliesWithNoResolver"
                    "::test_the_hook_is_declared_once_and_only_unix_host_overrides_it"
                ),
            ),
        ],
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
            "TWO measurements of the same device, and the second is what the message keys "
            "on. Tier 3, 2026-08-13: an `sftp` session into the pinned BusyBox root fails "
            "with `/bin/sh: /usr/lib/sftp-server: not found` -- ash inside the chroot, "
            "not the host's shell (`tests/busybox/test_tier3_session.py::"
            "test_sftp_and_scp_are_both_refused_inside_the_root`, driven by `sftp(1)`). "
            "Then otto's OWN backend against that same tier, 2026-08-14: "
            "`UnixHost.put` on a host built with `transfer: sftp` raises "
            "`asyncssh.sftp.SFTPConnectionLost: 0 bytes read on a total of 4 expected "
            "bytes` in 22ms, having moved no bytes and left nothing behind on either side "
            "-- the subsystem channel is accepted, the far side's exec of `sftp-server` "
            "exits 127, and the channel closes before the SFTP version exchange. The "
            "first measurement is what a DEVICE does; the second is what a CALLER gets, "
            "and only the second can be improved from a call site"
        ),
        queued_for=(
            "nothing for a fix, deliberately: the `shell` backend is the answer for these "
            "devices and it is verified over real ssh in Tier 3 (spec exit criterion 3). "
            "What HAS landed is the ATTRIBUTION, in "
            "`otto.host.transfer.sftp.open_sftp_or_attribute`. Note what deliberately did "
            "NOT land, because the absence is the finding: there is no pre-emptive refusal "
            "here and there is not meant to be one. Every fact a pre-check could key on "
            "answers `absent` on hosts where sftp works -- `sftp-server` is not on `PATH` "
            "even on Debian, its absolute path differs across distros and is compiled into "
            "dropbear, and the daemon is not the authority since packaged dropbear serves "
            "sftp fine when the machine provides the binary. The only definitive test is "
            "opening the subsystem, which IS the operation, so this record improves the "
            "failure instead of preventing it"
        ),
        paths=[
            GapPath(
                site="otto.host.transfer.sftp.SftpFileTransfer._run_get",
                state=PATH_ATTRIBUTED,
                checked_by="otto.host.transfer.sftp.open_sftp_or_attribute",
                detail=(
                    "opens the subsystem through the guard, which is where the attempt is "
                    "made and where its failure is translated. NOT a refusal and not "
                    "protection: a device with no sftp-server still fails, in the same "
                    "22ms and with the same nothing transferred. What changed is the "
                    "sentence -- `SFTPConnectionLost: 0 bytes read on a total of 4 "
                    "expected bytes`, which names no subsystem and reads as a flaky link, "
                    "becomes this record's reason, measurement and docs anchor, with "
                    "asyncssh's own error chained beneath it. It keys on nothing about the "
                    "host: the `busybox` profile lists `sftp` in `valid_transfers` "
                    "deliberately, and a device with an sftp-server installed never "
                    "reaches this arm because its subsystem starts"
                ),
                pinned_by=(
                    "tests/unit/host/test_sftp_transfer_attribution.py::TestGetArrivesAtTheGuard"
                    "::test_a_device_with_no_sftp_server_gets_this_record_instead_of_a_byte_count"
                ),
            ),
            GapPath(
                site="otto.host.transfer.sftp.SftpFileTransfer._run_put",
                state=PATH_ATTRIBUTED,
                checked_by="otto.host.transfer.sftp.open_sftp_or_attribute",
                detail=(
                    "the same guard for the same missing subsystem in the other direction, "
                    "and charged ONCE per `put()` rather than once per file because it sits "
                    "above the per-file fan-out `_put_files_sftp` gathers. That position is "
                    "also why the `file-ops-base64` defect shape -- a present file reported "
                    "missing -- does not occur here even without the guard: the failure "
                    "happens before any `src` is named, so the per-file "
                    "`Result(Status.Error, msg=f'{src}: {outcome}')` arm never renders it "
                    "and no file is ever blamed for the subsystem"
                ),
                pinned_by=(
                    "tests/unit/host/test_sftp_transfer_attribution.py::TestPutArrivesAtTheGuard"
                    "::test_a_device_with_no_sftp_server_gets_this_record_instead_of_a_byte_count"
                ),
            ),
        ],
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
            "TWO measurements, and the second is what the refusal keys on. Tier 3, "
            "2026-08-13: `scp -O` into the pinned BusyBox root fails with "
            "`/bin/sh: scp: not found`, and the file does not land -- same test as "
            "`sftp-transfer`; the two take different routes on purpose, since `scp -O` "
            "reaches for a remote binary while `sftp` opens a subsystem. Then the five "
            "matrix artifacts through the batched applet probe, 2026-08-14: `scp` is "
            "absent from the applet list on 1.16.1, 1.21.1, 1.28.1, 1.31.0 and 1.35.0 "
            "(`tests/busybox/test_applet_resolution.py` records it per row). The first "
            "measurement is what a device DOES; the second is what a device can be ASKED, "
            "and only the second can be read at a call site"
        ),
        queued_for=(
            "nothing for a fix, deliberately: the `shell` backend is the answer for these "
            "devices and it is verified over real ssh in Tier 3 (spec exit criterion 3), "
            "and there is no second spelling to adapt to -- `ScpOptions` carries no "
            "binary-name override, and the name the far side runs is the legacy protocol's "
            "rather than otto's. What HAS landed is the REFUSAL, in "
            "`otto.host.transfer.scp.refuse_if_scp_is_absent` -- this registry's fifth "
            "product call site. The record stays `measured-broken` because the surface "
            "still is: otto now declines the transfer instead of attempting one the device "
            "cannot serve"
        ),
        paths=[
            GapPath(
                site="otto.host.transfer.scp.ScpFileTransfer._run_get",
                state=PATH_WIRED,
                checked_by="otto.host.transfer.scp.refuse_if_scp_is_absent",
                detail=(
                    "reads this record through the guard and declines before "
                    "`_get_files_scp` opens the connection. The guard's PREDICATE is a "
                    "probe -- a SETTLED `applet_scp` of `absent` -- and its VERDICT and "
                    "MESSAGE are this record's, which is what makes the table the authority "
                    "here. It keys on what the DEVICE answered and never on the host's "
                    "profile: the `busybox` profile lists `scp` in `valid_transfers` "
                    "deliberately, because a BusyBox device with a real `scp` installed "
                    "alongside transfers perfectly well"
                ),
                pinned_by=(
                    "tests/unit/host/test_scp_transfer_refusal.py::TestGetArrivesAtTheGuard"
                    "::test_a_device_with_no_scp_is_refused_before_the_connection_is_opened"
                ),
            ),
            GapPath(
                site="otto.host.transfer.scp.ScpFileTransfer._run_put",
                state=PATH_WIRED,
                checked_by="otto.host.transfer.scp.refuse_if_scp_is_absent",
                detail=(
                    "the same guard for the same missing remote binary in the other "
                    "direction. Two sites, one guard -- which is why the count of wired "
                    "PATHS and the count of wired GUARDS are different numbers, both "
                    "derived. The guard is charged ONCE per `put()` and not once per file, "
                    "because it sits above the per-file fan-out `_put_files_scp` gathers"
                ),
                pinned_by=(
                    "tests/unit/host/test_scp_transfer_refusal.py::TestPutArrivesAtTheGuard"
                    "::test_a_device_with_no_scp_is_refused_before_the_connection_is_opened"
                ),
            ),
        ],
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
            "TWO measurements of the same option, and the second is what the refusal keys "
            "on. The five matrix artifacts, 2026-08-13: `nc -N 127.0.0.1 1` is rejected on "
            "every row (`nc: invalid option -- N` on 1.16.1 and 1.21.1, `nc: "
            "unrecognized option: N` on 1.28.1, 1.31.0 and 1.35.0), and every row's own "
            "usage line spells the listener `nc [OPTIONS] -l -p PORT`. That one CONNECTS, "
            "so no call site can issue it. Then, 2026-08-14, the same five rows through "
            "the probe a call site CAN issue -- `Userland._probe_nc_dash_n`, which compares "
            "a destination-less `nc` against `nc -N` and touches no socket: all five answer "
            "`rejected`, while OpenBSD netcat 1.226 answers `supported` and answers "
            "`rejected` for a `-Q` control it genuinely lacks. NO LINE NUMBER for the "
            "emitter: `paths` below carries each site as a dotted name a test RESOLVES, "
            "which is what this field used to cite instead"
        ),
        queued_for=(
            "the REFUSAL has landed for the GET direction, in "
            "`otto.host.transfer.nc.refuse_if_nc_rejects_dash_n` -- this registry's sixth "
            "product call site, and the first whose predicate is an OPTION rather than a "
            "presence. A FIX is still the full-parity workstream's, "
            "`todo/busybox-parity-sweep-2026-08-11.md`: the spec queues a BusyBox `nc` "
            "variant (`-l -p PORT`, size-terminated reads to replace the missing `-N`) and "
            "explicitly keeps it out of the phases that built the `shell` backend. The PUT "
            "direction is queued with it and stays open in the meantime -- see its path "
            "below for why the `-N` answer does not decide it. The record stays "
            "`measured-broken` because the surface still is"
        ),
        paths=[
            GapPath(
                site="otto.host.transfer.nc.NcFileTransfer._get_files_nc",
                state=PATH_WIRED,
                checked_by="otto.host.transfer.nc.refuse_if_nc_rejects_dash_n",
                detail=(
                    "asks the device to send with `nc -N <ip> <port>`, and `-N` is the option "
                    "every matrix row rejects outright. Reads this record through the guard "
                    "and declines before it binds its own local server or spawns anything. "
                    "The guard's PREDICATE is a probe -- a SETTLED `nc_dash_n` of `rejected` "
                    "-- and its VERDICT and MESSAGE are this record's. It keys on the OPTION "
                    "and on the BINARY otto would exec: `NcOptions.exec_name` pointed at a "
                    "real netcat is the workaround this record exists to preserve, so a host "
                    "configured that way is not refused at all"
                ),
                pinned_by=(
                    "tests/unit/host/test_nc_transfer_refusal.py::TestGetArrivesAtTheGuard"
                    "::test_a_device_whose_nc_rejects_dash_n_is_refused_before_anything_is_spawned"
                ),
            ),
            GapPath(
                site="otto.host.transfer.nc.NcFileTransfer._get_files_nc_tunneled",
                state=PATH_PROTECTED,
                checked_by="otto.host.transfer.nc.refuse_if_nc_rejects_dash_n",
                detail=(
                    "the hop-tunnelled GET, which spawns `nc -Nl <port>` -- both spellings "
                    "the applet rejects in one option string -- and is a SEPARATE emitter "
                    "rather than a restatement of the plain GET. It is not a hole and it "
                    "correctly has no guard of its own: `_get_files_nc` is its only caller, "
                    "and the refusal there sits ABOVE the `has_tunnel` dispatch, so on "
                    "exactly the devices this record covers this function is never entered. "
                    "A second call to the guard inside it could never be the one to fire"
                ),
                pinned_by=(
                    "tests/unit/host/test_nc_transfer_refusal.py"
                    "::TestTheTunnelledGetIsProtectedByItsOnlyCaller"
                    "::test_the_tunnelled_path_is_never_entered_on_a_refused_device"
                ),
            ),
            GapPath(
                site="otto.host.transfer.nc.NcFileTransfer._put_files_nc",
                state=PATH_OPEN,
                detail=(
                    "spawns the device-side listener as `nc -l -w <secs> <port>`, the "
                    "OpenBSD spelling the applet does not accept (it wants `-l -p PORT`), "
                    "and reads nothing from this record. So the listener never binds, otto "
                    "waits for a peer that cannot arrive, and `_cancel_and_reap` ends it -- "
                    "a timeout rather than the refusal this record describes. STILL OPEN "
                    "DELIBERATELY, now that the GET direction is wired: this command carries "
                    "no `-N`, so the `nc_dash_n` measurement is not about it. The two facts "
                    "coincide on every matrix row and remain two facts, and the one that "
                    "would decide this path -- whether the device's `nc` accepts a listener "
                    "spelled `-l PORT` -- cannot be settled without asking a device to BIND, "
                    "which is a probe with a side effect on the host it is asking about"
                ),
            ),
        ],
    ),
    Gap(
        surface="daemon-launch",
        status=MEASURED_BROKEN,
        reason=(
            "`otto.host.daemon.launch_command` wraps every daemon in "
            '`setsid bash -c \'exec -a "$1" "${@:2}"\' _ <sentinel> <argv>` so the '
            "process carries a findable `argv[0]`, and a stock BusyBox userland has no "
            "bash. The body is not portable to ash either, so this is not a `bash`->`sh` "
            "substitution: it needs a different argv[0] mechanism. otto REFUSES the "
            "launch instead, on any host declaring `has_bash=False`, rather than emitting "
            "a command whose failure it would not notice -- `otto.link.manage._root_run` "
            "does not raise on a non-ok result, a qdisc mutation's failure is caught by "
            "the caller's own re-read, and NOTHING re-reads after a timer launch, so `link "
            "impair --expire` used to report SUCCESS for a timer that was never running "
            "and an impairment that therefore never expired. "
            "What is refused is only the DAEMON: impair without `--expire` "
            "and repair when done, since `tc` needs no bash. `otto.tunnel`'s socat launch "
            "is not refused here because it is unreachable -- `_resolve_chain` rejects "
            "such a host as a tunnel path member first"
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
            "the REFUSAL has landed, in "
            "`otto.host.daemon.refuse_if_launch_wrapper_needs_bash` -- this registry's "
            "second product call site. A FIX has not, and is not written up in the "
            "full-parity workstream (`todo/busybox-parity-sweep-2026-08-11.md`) yet: this "
            "table is the record, and the queue file carries the plan for work that has "
            "one. A fix is a portable argv[0] mechanism, which is a design question and "
            "not a spelling change. The record stays `measured-broken` because the surface "
            "still is -- otto now declines the launch instead of emitting one it cannot "
            "run"
        ),
        paths=[
            GapPath(
                site="otto.link.manage._launch_daemon",
                state=PATH_WIRED,
                checked_by="otto.host.daemon.refuse_if_launch_wrapper_needs_bash",
                detail=(
                    "the only path in `otto.link` that reaches `launch_command`, shared by "
                    "both expire-timer flavours, so the refusal cannot be bypassed by adding "
                    "a third launch. Keys on a DECLARED `has_bash is False` -- free, no probe "
                    "-- and hands the verdict and the message to this record"
                ),
                pinned_by=(
                    "tests/unit/host/test_daemon_launch_refusal.py"
                    "::TestTheTableDecidesWhetherTheClassIsRefused"
                    "::test_flipping_the_record_to_untested_stops_the_refusal"
                ),
            ),
            GapPath(
                site="otto.tunnel.manage.add_tunnel",
                state=PATH_PROTECTED,
                checked_by="otto.tunnel.manage._validate_chain_shape",
                detail=(
                    "otto's other tagged-daemon launch, and NOT a hole: `add_tunnel` refuses a "
                    "`has_bash=False` host as a tunnel path member before it plans anything -- "
                    "so the `launch_command` further down is unreachable on exactly the hosts "
                    "this record covers. It correctly has no guard, and adding one here would "
                    "be a guard that cannot fire. The refusal is loud, is a `ValueError` rather "
                    "than this table's error, and predates this record. Named at "
                    "`_validate_chain_shape` and not at its caller because BOTH of "
                    "`add_tunnel`'s modes route through it -- `_resolve_chain` on the real "
                    "path, `_planned_chain` under `--dry-run`, which reaches no device and "
                    "launches nothing -- so this is where deleting the refusal has to red"
                ),
                pinned_by=(
                    "tests/unit/tunnel/test_manage_resolve.py::TestResolveChain"
                    "::test_busybox_profile_host_rejected_as_chain_member"
                ),
            ),
        ],
    ),
    Gap(
        surface="shutdown-command",
        status=MEASURED_BROKEN,
        reason=(
            "BusyBox has no `shutdown` applet, and `shutdown -h now` is what "
            "`Host.shutdown()` used to emit unconditionally. otto now ASKS: "
            "`otto.host.unix_host.shutdown_command` reads the resolved `applet_shutdown` "
            "and `applet_poweroff` capabilities and emits the spelling the device has, so "
            "a BusyBox device is powered off through `poweroff` rather than told otto "
            "cannot. THIS RECORD IS NOT A REFUSAL FOR ANY MEASURED DEVICE -- what is left "
            "of it is the floor: a device that answers `absent` to BOTH names has nothing "
            "otto can emit, and is refused before anything is sent instead of being told a "
            "shutdown succeeded. `Host.reboot()` is NOT affected and must not be lumped in "
            "with this -- `reboot` is present on every matrix row and "
            "`UnixHost._soft_reboot` works as shipped"
        ),
        measured_on=(
            "the five matrix artifacts, 2026-08-13, re-measured through the batched applet "
            "probe 2026-08-14: `shutdown` is absent from the applet list on 1.16.1, "
            "1.21.1, 1.28.1, 1.31.0 and 1.35.0, while `reboot` and `poweroff` are present "
            "on all five (`tests/busybox/test_applet_resolution.py` records it per row). "
            "BusyBox will only run an applet its own list carries, so the list is the whole "
            "answer here -- and it is also why the choice always has somewhere to go: no "
            "measured row is refused by this record"
        ),
        queued_for=(
            "nothing -- the FIX has landed, in `otto.host.unix_host.shutdown_command`, and "
            "it is this registry's first. The full-parity workstream "
            "(`todo/busybox-parity-sweep-2026-08-11.md`) asked for a userland probe rather "
            "than a hard-coded swap that would break every GNU host, and that is what "
            "`applet_shutdown`/`applet_poweroff` are. The record stays `measured-broken` "
            "because the DEVICE still is: `shutdown` is absent on every row, a future "
            "caller that hard-codes it needs to find that written down, and the "
            "neither-spelling refusal is still earned by this measurement"
        ),
        paths=[
            GapPath(
                site="otto.host.unix_host.UnixHost.shutdown",
                state=PATH_ADAPTED,
                checked_by="otto.host.unix_host.shutdown_command",
                detail=(
                    "resolves the userland, then emits `shutdown -h now` where the device "
                    "has that applet and `poweroff` where it does not -- so every matrix "
                    "row is shut down rather than refused. The choice reads the capability "
                    "VALUE alone, because degrading on a guess costs at worst the command "
                    "otto already emitted; the refusal for a device with neither asks "
                    "`is_settled` first, so a probe round that never arrived cannot become "
                    "a verdict. `UnixHost.reboot` is a DIFFERENT surface and is not a path "
                    "of this record: `reboot` is present on every matrix row"
                ),
                pinned_by=(
                    "tests/unit/host/test_power.py::TestShutdownPicksTheSpellingTheDeviceHas"
                    "::test_a_device_without_shutdown_is_powered_off_with_poweroff"
                ),
            ),
        ],
    ),
    Gap(
        surface="run-command-line-length",
        status=MEASURED_BROKEN,
        reason=(
            "BusyBox ash's line editor SILENTLY TRUNCATES a typed line longer than 1022 "
            "characters -- a different, shorter command runs and its success is reported "
            "as the caller's. `Host.run()` refuses instead, up front, on any host whose "
            "declared shell dialect is `ash`: `run()` drives a PERSISTENT session, which "
            '`SshSession._open` opens with `term_type="dumb"`, so the far side allocates '
            "a pty and the command arrives as a TYPED LINE through that editor. The bound "
            "is on the LINE and not on the command -- otto's own BEGIN/END framing costs "
            "74 characters, leaving 948 for the longest line of the command itself. "
            "`Host.exec()` opens a bare exec channel with no pty, is unaffected, and is "
            "NOT refused: it is the way to send this command. Two paths stay unguarded "
            "deliberately -- a named session's `HostSession.run()`, and `exec()` on a "
            "telnet or proxied-login host, which has no stateless primitive and so routes "
            "through a pooled shell session. Guarding those would make "
            "`ShellFileTransfer` refuse its own 5534-character chunk lines, which is the "
            "whole reason the `shell` backend exists"
        ),
        measured_on=(
            "TWO measurements, and the second is why the bound is a constant. The phase-5 "
            "spike, 2026-08-13, dropbear 2022.83 against BusyBox 1.35.0: largest line "
            "delivered intact 1022, first truncated 1023, with no error and no log line -- "
            "identical against OpenSSH and against a bare LOCAL pty, which is what "
            "identifies it as BusyBox ash's `CONFIG_FEATURE_EDITING_MAX_LEN` and not the "
            "transport, while the exec channel took 9000 characters intact and broke at "
            "9001. Then, 2026-08-13, because that config is BUILD-TIME and one artifact "
            "cannot speak for the matrix: all five pinned rows (1.16.1, 1.21.1, 1.28.1, "
            "1.31.0, 1.35.0) driven through a local pty answered 1022/1023 identically, "
            "and the same harness carried 18437 characters into bash and over 20000 into "
            "dash -- ruling itself out as the thing being measured. A CUSTOM build that "
            "raised the config would be refused a command it could actually run; that "
            "cost is stated on `ASH_TYPED_LINE_MAX` rather than hidden"
        ),
        queued_for=(
            "the REFUSAL has landed, in "
            "`otto.host.session.refuse_if_line_editor_would_truncate` -- this registry's "
            "first product call site. A FIX has not, and stays unqueued "
            "deliberately: it is a pty-free `run()` path, not a larger buffer, "
            "because the buffer belongs to the device. The record stays `measured-broken` "
            "because the surface still is -- otto now declines the command instead of "
            "running a shorter one. The two unguarded paths named in the reason are the "
            "next candidates and are likewise unqueued"
        ),
        paths=[
            GapPath(
                site="otto.host.session.SessionManager.run_cmd",
                state=PATH_WIRED,
                checked_by="otto.host.session.refuse_if_line_editor_would_truncate",
                detail=(
                    "the per-command path of `Host.run()` for every host family, and the "
                    "only one of the three below that reads this record. The guard keys on "
                    "`isinstance(frame, AshFrame)` -- the DECLARED dialect, free and "
                    "synchronous -- sizes the line otto would TYPE including its own framing, "
                    "and refuses before `_ensure_session`, so a refused command costs no "
                    "connection"
                ),
                pinned_by=(
                    "tests/unit/host/test_run_line_length.py::TestAnAshHostRefusesInsteadOfTruncating"
                    "::test_flipping_the_record_to_untested_stops_the_refusal"
                ),
            ),
            GapPath(
                site="otto.host.session.HostSession.run",
                state=PATH_OPEN,
                detail=(
                    "a NAMED session's `run()` calls `ShellSession.run_cmd` directly -- one "
                    "layer below the guard, which lives on `SessionManager.run_cmd` -- so a "
                    "typed line over the bound is still silently truncated here. Open "
                    "DELIBERATELY, and this is the one path that must not simply be closed: "
                    "`ShellFileTransfer` rides it with 5534-character chunk lines and would "
                    "be refused, which would stop every shell transfer to exactly the devices "
                    "the backend exists for. Moving the guard down a layer is the wrong fix; "
                    "a pty-free `run()` is the right one"
                ),
                pinned_by=(
                    "tests/unit/host/test_run_line_length.py::TestTheRefusalIsScoped"
                    "::test_a_named_session_is_not_refused_either"
                ),
            ),
            GapPath(
                site="otto.host.session.SessionManager.exec",
                state=PATH_OPEN,
                detail=(
                    "`exec()` is safe only where it has a stateless primitive. On a "
                    "`term: telnet` host, and on ANY host whose login is proxied, there is "
                    "none, so the call takes `_acquire_exec_session()` -> `HostSession.run` "
                    "-> `ShellSession.run_cmd` and is line-edited like a typed command. The "
                    "record's escape hatch -- 'send it through `exec()`' -- is therefore not "
                    "true on those two host shapes, which is what makes this path worth "
                    "recording rather than filing under the one above"
                ),
                pinned_by=(
                    "tests/unit/host/test_run_line_length.py::TestTheRefusalIsScoped"
                    "::test_the_pooled_shell_session_exec_path_is_not_refused_either"
                ),
            ),
        ],
    ),
    Gap(
        surface="product-lifecycle",
        status=UNTESTED,
        reason=(
            "nobody has run otto's product verbs -- `Host.stage()`, `Host.install()`, "
            "`Host.uninstall()` and `Host.is_installed()` -- against a BusyBox userland, "
            "and no tier can, because those four emit NO command of their own. Each "
            "iterates `Host.products` and delegates to a `Product`, and `Product` "
            "declares all four of its methods abstract. otto ships exactly ONE concrete "
            "body, `FileProduct.stage`, and it is a single `await host.put(...)` -- a "
            "surface this table already covers. Everything else that would reach the "
            "device is project-supplied product code otto does not own, so a test that "
            "measured `install` here would be measuring the `Product` subclass the test "
            "itself wrote. Untested, therefore not blocked -- and with no products "
            "declared, `stage`/`install`/`uninstall` are successful no-ops on any "
            "userland and `is_installed()` is False"
        ),
        measured_on="",
        queued_for=(
            "nothing, and not for the usual reason: there is no otto code here to fix. "
            "What would close this is a PROJECT taking a real `Product` to a real "
            "BusyBox device and reporting what its `install` emitted -- at which point "
            "the gap, if there is one, belongs to the command that failed (`run`, `put`) "
            "and is recorded under THAT surface rather than this one. "
            "`todo/busybox-parity-sweep-2026-08-11.md` is where such a finding lands"
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
# THREE ENTRIES, ONE REASON, and the reason is what gives this block its
# standing: each was MEASURED, and the measurement did not support the
# prediction -- ``pgrep``/``pkill``, ``sudo`` and ``reboot``. That is the count
# the docs page quotes ("three candidates ... dropped for exactly this reason
# once they were measured"), and it is the count to keep in sync with it.
#
# The survey's fourth candidate, product ``install``/``stage``/``uninstall``,
# is deliberately NOT here any more. Nothing ever measured it, so it never
# belonged in a block whose whole authority is "measured, not predicted" -- one
# entry cleared by reasoning devalues the other three. It is a record above
# instead, ``product-lifecycle``, ``untested``, which is where a surface nobody
# has run belongs. See that record for why nobody can: otto ships no
# implementation of the half that touches the device.
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
#     half is a gap (see ``shutdown-command``) -- and that half is now ADAPTED
#     to rather than refused, which is a second way this pair had to be kept
#     apart: `_soft_reboot` needed no change at all.


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


def gap_path_totals() -> dict[str, int]:
    """How many paths across :data:`GAPS` are in each state. DERIVED, never typed.

    The replacement for the count this module's comment block used to carry by
    hand ("EXACTLY THREE", after "none yet", "exactly one" and "exactly two" --
    a number a human retyped every change, and the reason this function exists).
    Every state is a key even at zero, so a reader of the output cannot mistake
    "no paths in that state" for "that state is not a thing here".

    :data:`GAP_DOCS_PAGE` prints this table verbatim and
    ``tests/unit/test_docs_gap_sync.py`` pins the page's numbers to these, so
    there is no number on either side that a human maintains.
    """
    totals = dict.fromkeys(_PATH_STATES, 0)
    for gap in GAPS:
        for path in gap.paths:
            totals[path.state] += 1
    return totals


def table_guards() -> list[str]:
    """Every distinct guard function a table-backed path names, sorted.

    Table-backed is :attr:`Gap.table_backed_paths` -- :data:`PATH_WIRED` and
    :data:`PATH_ADAPTED`, the two states whose verdict is this record's. Named
    for that rather than ``wired_guards``, which it used to be called: an
    ADAPTED path reaches :func:`refuse_if_gapped` exactly as a wired one does,
    so a name scoped to ``WIRED`` would have to either exclude a real table
    consumer or mean something other than what it says.

    A SECOND count, and it is not the first one: five table-backed paths reach
    this table through four guards, because ``read_file`` and ``write_file``
    share one. The prose that used to say "three product call sites" was
    counting guards and reading as though it counted sites, which is exactly the
    ambiguity a hand-maintained number buys. Both numbers are derived, and they
    are allowed to differ.
    """
    return sorted({p.checked_by for gap in GAPS for p in gap.table_backed_paths})


def refuse_if_gapped(
    surface: str, *, host: str = "", attempted: str = "", observed: str = ""
) -> None:
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

    *host*, *attempted* and *observed* only decorate the message. None of them
    changes the verdict: the table is about a class of userland, and the caller
    is the one that decided this host belongs to it.

    *observed* IS STILL A DECORATION, and it is worth saying plainly because it
    is the one that reads like a mode. It replaces the message's
    ``nothing was attempted`` lead for a caller whose attempt IS the probe --
    today only :func:`otto.host.transfer.sftp.open_sftp_or_attribute`, whose
    surface cannot be pre-checked without refusing hosts that work. The three
    outcomes above are unchanged by it, and so is the ``untested`` behaviour
    that makes such a call worth routing through here at all: downgrade the
    record and this returns, which for that caller means the device's own error
    reaches the operator untranslated rather than a second message being
    silently dropped.

    Raises:
        UnsupportedOnUserlandError: *surface* is declared
            :data:`MEASURED_BROKEN`. Nothing was attempted -- unless the caller
            passed *observed*, which says what was -- which is precisely why it
            is this exception and not
            :class:`~otto.host.errors.HostCommandError`.
    """
    gap = gap_for(surface)
    if gap is None or not gap.refuses:
        return
    raise UnsupportedOnUserlandError.for_gap(gap, host=host, attempted=attempted, observed=observed)


ASH_TYPED_LINE_MAX = 1022
"""Longest line BusyBox ash's line editor delivers intact. A DISCRIMINATOR.

The measurement behind the ``run-command-line-length`` record, hoisted into a
constant because :func:`otto.host.session.refuse_if_line_editor_would_truncate`
now compares against it rather than merely printing it. Read that record for
the evidence; this docstring is about the number's standing.

**NEVER WIDEN THIS.** It is not a budget otto chose and not a runaway guard --
it is the far side's buffer, ``CONFIG_FEATURE_EDITING_MAX_LEN`` minus the NUL,
and a larger value here does not buy patience, it re-opens the silent
truncation for the band between the two numbers. The only thing that may
change it is a new measurement against real artifacts, and then it moves to
whatever they answered.

WHICH BUILDS ANSWERED 1022, precisely, because ``CONFIG_FEATURE_EDITING_MAX_LEN``
is build-configurable and a device is free to disagree. All five pinned matrix
artifacts -- 1.16.1, 1.21.1, 1.28.1, 1.31.0 and 1.35.0 -- were driven through a
local pty on 2026-08-13 and every one of them delivered 1022 intact and
truncated at 1023. Twelve years of upstream prebuilds agreeing is why this is a
constant and not a per-row table, and it is still not a proof about a
CUSTOM build: a vendor who raised the config, or compiled the line editor out
altogether, has a device otto will now refuse a working command on. That is the
expensive direction, so it is stated rather than buried -- the refusal names
the measurement, and a device that disagrees is a new measurement, not a bug in
the caller's command.

The same harness measured bash at 18437 and dash at 20000+ on the machine that
took the BusyBox numbers, which is what rules the harness itself out as the
thing being measured.
"""
