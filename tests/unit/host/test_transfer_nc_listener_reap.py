"""Every error path out of an nc transfer must reap its remote listener.

Source-level guards, deliberately. The thing that broke here was not one
branch's logic — it was a *belief*, written into four comments and relied on by
ten branches: that ``nc -w`` makes a remote listener self-terminate. OpenBSD
netcat's manual says the opposite ("the -w flag has no effect on the -l
option"), and the bed proved it on 2026-08-10 with six leaked listeners alive
between one and three and a half days, each holding the port that the next
transfer's scan then had to skip.

What needs pinning is the shape, not a list of branches — the first version of
this file pinned a list, and review found an eleventh path it could not see.
So: each attempt reaps in a ``finally`` before releasing its port, no listener
task is cancelled outside the reaping helper, and no listener is spawned
without a hard cap. All three are properties of the source, checked against the
source, so a new branch written in the old style fails here without anyone
remembering this file exists.

The cap's CALLING CONVENTION is no longer decided here. ``nc`` used to carry a
shell-embedded probe of its own for which spelling the remote ``timeout``
accepts, which was correct but private — a sixth answer to a question five
sibling capabilities already resolve through
:class:`~otto.host.userland.Userland`. It now reads
:attr:`~otto.host.userland.Userland.timeout_style` instead, so what these tests
pin is the MAPPING and, just as importantly, the precondition that mapping
inherits: every capability property raises if it is read before ``resolve()``
has been awaited, and ``_nc_listener_prefix`` is synchronous, so where
resolution happens is a stated part of the design rather than a side effect of
whichever probe ran first.
"""

import ast
import inspect
import subprocess
from dataclasses import fields
from pathlib import Path
from typing import get_args
from unittest.mock import AsyncMock, MagicMock

import pytest

from otto.host.connections import ConnectionManager
from otto.host.options import NcOptions, UserlandOptions
from otto.host.transfer import nc as nc_module
from otto.host.transfer.nc import _NC_LISTENER_HARD_CAP_S, NcFileTransfer
from otto.host.userland import PROBED_APPLETS, Userland, applet_capability
from otto.models.options import UserlandOptionsSpec
from otto.result import CommandResult

_SOURCE = Path(inspect.getfile(nc_module)).read_text()
_TREE = ast.parse(_SOURCE)

# The one function allowed to cancel a listener task, because it is the one
# that reaps immediately afterwards.
_REAPING_FUNCTION = "_cancel_and_reap"


# ---------------------------------------------------------------------------
# Helpers for the cap, whose convention now comes from `Userland`
# ---------------------------------------------------------------------------


async def _never_probes(cmd: str, **_kwargs: object) -> CommandResult:
    """A ``Userland`` runner that cannot answer, because nothing here may ask it.

    Reaching this at all is the failure. Every probeable capability is declared
    (see ``_fully_declared_options``), so a correct ``Userland`` issues no
    command during resolution and this body is unreachable.

    BE PRECISE ABOUT WHAT THAT BUYS, because the obvious reading is wrong.
    Raising here does NOT surface as this message: ``Userland._probe`` catches
    every ``Exception`` by design — a probe that cannot run is a "no", not an
    error — so the raise is swallowed and resolution answers ``"absent"``
    instead. Measured by mutating the declared-value short-circuit out of
    ``Userland._resolve_once``: the guards below went red on
    ``assert '' == 'timeout 3600 '``, never on the text below.

    So what makes the runner unanswerable is still load-bearing, just for a
    different reason than "it raises loudly": it means a ``Userland`` that
    probed anyway CANNOT arrive at the declared answer by accident, and the
    value assertions catch it. A runner that returned a plausible
    ``CommandResult`` could agree with the declared value and hide the bug.
    """
    raise AssertionError(f"a declared userland must not probe, but it ran {cmd!r}")


# The five capabilities nc does not read, declared anyway. Not padding: a
# `Userland` with anything left to probe still CALLS its runner, and an earlier
# version of this file declared `timeout_style` alone — so `_never_probes` was
# reached seven times per helper call (both elevation spellings, both base64
# spellings, stat, wc, $BASH_VERSION) and its message was false every time.
# `checksum` joined this table when that capability was added; leaving it
# undeclared would add an eighth unwanted call ($BASH_VERSION's neighbour,
# `md5sum < /dev/null`).
# Values are ones Tier 1 measured as real answers; nothing here reads them.
#
# The `applet_*` block joined for the same reason `checksum` did, and it is
# derived from `PROBED_APPLETS` rather than typed out: undeclared, the applet
# BATCH would be an eighth unwanted call. Deriving it means an applet added to
# that list cannot silently restore the probe this table exists to remove.
# Values are what the matrix measured on 1.35.0 (`scp` and `shutdown` absent on
# every row); nothing here reads them.
_OTHER_DECLARED_CAPABILITIES = {
    "shell_dialect": "ash",
    "elevation": "none",
    "base64_flag": "-d",
    "stat_size": "stat",
    "checksum": "md5sum",
    # `rejected` is what 1.35.0 measures, like the applet values below, and
    # nothing in this module reads it: the guard that does
    # (`refuse_if_nc_rejects_dash_n`) sits on the GET path, and every test here
    # is about the listener PREFIX. Declared only so the round issues no probe.
    "nc_dash_n": "rejected",
    **{
        applet_capability(a): ("absent" if a in {"scp", "shutdown"} else "present")
        for a in PROBED_APPLETS
    },
}

# `timeout_style` is the parameter under test; `version` is documentation and is
# never probed, which `tests/unit/host/test_userland.py` pins as its own rule.
_NOT_IN_THE_TABLE_ABOVE = {"timeout_style", "version"}


def _fully_declared_options(timeout_style: str) -> UserlandOptions:
    """``UserlandOptions`` with every probeable field declared.

    The coverage check is derived from the dataclass, so a seventh probeable
    field arriving on ``UserlandOptions`` fails here rather than quietly
    restoring a probe — and with it the false assertion message that
    ``_never_probes`` exists to keep honest.
    """
    undeclared = (
        {f.name for f in fields(UserlandOptions)}
        - set(_OTHER_DECLARED_CAPABILITIES)
        - _NOT_IN_THE_TABLE_ABOVE
    )
    assert not undeclared, (
        f"{sorted(undeclared)} would still be probed, so `_never_probes` would be "
        "called and its message would be a lie. Declare them in "
        "_OTHER_DECLARED_CAPABILITIES."
    )
    return UserlandOptions(timeout_style=timeout_style, **_OTHER_DECLARED_CAPABILITIES)


def _unresolved_userland(timeout_style: str) -> Userland:
    """A ``Userland`` carrying a declared *timeout_style*, deliberately unresolved."""
    return Userland(_fully_declared_options(timeout_style), _never_probes)


async def _userland_with(timeout_style: str) -> Userland:
    """The same, resolved — which for fully declared options issues no command."""
    userland = _unresolved_userland(timeout_style)
    await userland.resolve()
    return userland


def _make_ft(*, userland: Userland | None) -> NcFileTransfer:
    """An ``NcFileTransfer`` wired to *userland* and to nothing that talks.

    Both strategy options are CONCRETE rather than ``auto`` on purpose. That
    is the shape in which ``prepare()`` has nothing of its own left to do and
    returns early — so a userland that resolved only as a side effect of the
    strategy probe would still be unresolved here, and the prefix would raise
    on a host configured perfectly legitimately.
    """
    connections = MagicMock(spec=ConnectionManager)
    connections.has_tunnel = False
    connections.ip = "10.0.0.1"
    connections.term = "ssh"
    return NcFileTransfer(
        connections=connections,
        name="tomato",
        transfer="nc",
        nc_options=NcOptions(port_strategy="ss", listener_check="ss"),
        get_local_ip=lambda: "127.0.0.1",
        exec_cmd=AsyncMock(side_effect=AssertionError("no remote command should run here")),
        userland=userland,
    )


def _declared_timeout_styles() -> set[str]:
    """The ``timeout_style`` vocabulary, read from the boundary spec that owns it.

    Derived rather than hand-listed so that adding a spelling to lab data's
    ``Literal`` and forgetting ``nc`` is a failure here, not a listener that
    silently loses its cap on the new host class.
    """
    annotation = UserlandOptionsSpec.model_fields["timeout_style"].annotation
    return {
        member
        for arg in get_args(annotation)
        for member in get_args(arg)
        if isinstance(member, str)
    }


def _enclosing_function(node: ast.AST, tree: ast.AST) -> str | None:
    """Name of the innermost function containing ``node``."""
    best: tuple[int, str] | None = None
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.AsyncFunctionDef | ast.FunctionDef):
            continue
        end = fn.end_lineno or fn.lineno
        if fn.lineno <= node.lineno <= end and (best is None or fn.lineno > best[0]):
            best = (fn.lineno, fn.name)
    return None if best is None else best[1]


def _listener_cancel_sites() -> list[tuple[int, str | None]]:
    """``(lineno, enclosing function)`` for every ``listen_task.cancel()``."""
    return [
        (node.lineno, _enclosing_function(node, _TREE))
        for node in ast.walk(_TREE)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "cancel"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "listen_task"
    ]


def test_the_guard_can_actually_see_a_cancel_site():
    """Guard the guard: an AST query that matches nothing would pass forever."""
    sites = _listener_cancel_sites()
    assert sites, "found no listen_task.cancel() at all — the AST query has rotted"
    assert any(fn == _REAPING_FUNCTION for _, fn in sites), (
        f"{_REAPING_FUNCTION} no longer cancels the task it reaps; this query is "
        "matching something else"
    )


def test_every_attempt_reaps_from_its_finally_not_per_branch():
    """The reap must be an exit, not an enumeration.

    The first version of this fix patched ten error branches found by grepping
    ``listen_task.cancel()``, and a review found an ELEVENTH path with no
    cancel to grep: PUT's bare ``await self._wait_for_remote_listener(port)``
    raises ``ConnectionError`` past every handler but ``CancelledError``. The
    guard below, built from the same enumeration, reported clean — a guard
    inherits the blind spot of the query that built it.

    So the invariant is structural: each attempt reaps on the way out,
    unconditionally, before releasing the port.
    """
    reaping_finallys = [
        node
        for node in ast.walk(_TREE)
        if isinstance(node, ast.Try)
        and any(
            isinstance(c, ast.Call)
            and isinstance(c.func, ast.Attribute)
            and c.func.attr == _REAPING_FUNCTION
            for f in node.finalbody
            for c in ast.walk(f)
        )
    ]
    assert len(reaping_finallys) == 2, (
        f"expected both nc attempts (get and put) to reap in `finally`, found "
        f"{len(reaping_finallys)}. Reaping only on enumerated branches misses "
        "any path that raises without cancelling — which is how the PUT "
        "listener-not-ready leak survived the first fix."
    )
    for node in reaping_finallys:
        names = [
            c.func.attr
            for f in node.finalbody
            for c in ast.walk(f)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
        ]
        assert names.index(_REAPING_FUNCTION) < names.index("_release_port"), (
            "the reap must run BEFORE _release_port, or the port can be handed "
            "to the next transfer while a listener still holds it"
        )


def test_every_attempt_releases_its_local_forward_where_it_releases_the_port():
    """The local half of the leak, pinned in the same structural shape.

    The remote listener and the local asyncssh forward are taken out together
    and have to come back together. Caching forwards in the transport bounds
    the leak only where the destination repeats; these files transfer
    concurrently on distinct reserved ports, so the cache cannot help and each
    attempt has to hand its own forward back.

    Unit tests over the transfer code cannot catch this by behaviour —
    ``_connections`` is a ``MagicMock`` there, which absorbs an
    ``unforward_port`` that is never called. Hence a structural guard.

    Ordering matters twice: after the reap, which needs the forward to reach
    the listener it is killing, and with ``_release_port``, so a destination is
    never left forwarded to a port that has been handed to someone else.
    """
    releasing_finallys = [
        node
        for node in ast.walk(_TREE)
        if isinstance(node, ast.Try)
        and any(
            isinstance(c, ast.Call)
            and isinstance(c.func, ast.Attribute)
            and c.func.attr == "unforward_port"
            for f in node.finalbody
            for c in ast.walk(f)
        )
    ]
    assert len(releasing_finallys) == 2, (
        f"expected both nc attempts (get and put) to release their forward in "
        f"`finally`, found {len(releasing_finallys)}. A forward held past its "
        "attempt lives until the host closes, so a bulk put of N files strands "
        "N listening sockets for the rest of the session."
    )
    for node in releasing_finallys:
        names = [
            c.func.attr
            for f in node.finalbody
            for c in ast.walk(f)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
        ]
        assert names.index(_REAPING_FUNCTION) < names.index("unforward_port"), (
            "the forward must be released AFTER the reap — the reap connects "
            "through that forward to make the remote listener exit"
        )
        assert names.index("unforward_port") < names.index("_release_port"), (
            "release the forward BEFORE the port, so the port is never handed "
            "to the next transfer while a forward still points at it"
        )


def test_no_listener_is_cancelled_without_being_reaped():
    """The defect, stated as an invariant.

    Ten branches used to cancel otto's local await and return an error, leaving
    the remote ``nc`` listening forever — including the branch whose own error
    message names the problem ("orphaned listener — likely a remote port
    collision"). Cancelling locally does nothing to a remote process.
    """
    offenders = [(line, fn) for line, fn in _listener_cancel_sites() if fn != _REAPING_FUNCTION]
    assert not offenders, (
        "listen_task.cancel() outside "
        f"{_REAPING_FUNCTION}() at {offenders} — cancelling otto's await does "
        "not stop the remote `nc`, which on OpenBSD netcat then listens "
        f"forever. Call `await self.{_REAPING_FUNCTION}(listen_task, port)` "
        "instead."
    )


def test_both_listener_spawns_carry_the_hard_cap():
    """The reap covers otto being alive. This covers otto not being alive.

    If otto is killed outright, or the SSH channel dies, no ``finally`` runs and
    nothing local can reap. The remote-side cap is the only thing left, so it
    must be on BOTH spawn sites — the get direction (``-Nl``) is the one that
    tends to be forgotten, and it is exactly the one the bed-hygiene probe was
    also blind to.
    """
    spawns = [
        line
        for line in _SOURCE.splitlines()
        if "self._nc_exec}" in line and ("-l " in line or "-Nl " in line)
    ]
    assert len(spawns) == 2, f"expected 2 listener spawn sites, found {len(spawns)}: {spawns}"
    unwrapped = [s for s in spawns if "_nc_listener_prefix" not in s]
    assert not unwrapped, (
        f"listener spawned without the hard-cap prefix: {unwrapped}. Without it, "
        "an otto that dies with a listener up strands it permanently."
    )


def test_the_hard_cap_is_a_backstop_not_a_transfer_deadline():
    """Pin the magnitude, because the failure mode of getting it wrong is silent.

    `timeout` bounds wall-clock lifetime, not idle time — there is no
    `--idle` — so this cap covers an ESTABLISHED transfer too. Set it near
    `listener_timeout` (30s) and every transfer slower than that is severed:
    on GET the reader then sees a clean EOF and the short read is deliberately
    NOT failed, so the caller gets Status.Success and a truncated file. Nothing
    else in the suite would go red. It must therefore stay far above both the
    data-path bounds and any plausible transfer, while still beating "nobody
    noticed for days" — the state this whole fix exists to end.
    """
    from otto.host.options import NcOptions
    from otto.host.transfer.nc import (
        _NC_FORWARD_SETUP_TIMEOUT,
        _NC_LISTENER_HARD_CAP_S,
        _NC_STALL_TIMEOUT,
    )

    data_path_bounds = _NC_STALL_TIMEOUT + _NC_FORWARD_SETUP_TIMEOUT
    assert 100 * data_path_bounds < _NC_LISTENER_HARD_CAP_S, (
        f"hard cap {_NC_LISTENER_HARD_CAP_S}s is not comfortably above the "
        f"data-path bounds ({data_path_bounds}s) — it would start cutting "
        "healthy transfers, and on GET that truncates silently"
    )
    assert 10 * NcOptions().listener_timeout < _NC_LISTENER_HARD_CAP_S, (
        "hard cap is close to listener_timeout; they bound different things "
        "(whole lifetime vs otto's post-transfer wait) and must not converge"
    )
    assert _NC_LISTENER_HARD_CAP_S <= 24 * 3600, (
        "a cap measured in days is not a backstop — the leak it replaces was found after three"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("timeout_style", "expected"),
    [
        ("coreutils", f"timeout {_NC_LISTENER_HARD_CAP_S} "),
        ("dash-t", f"timeout -t {_NC_LISTENER_HARD_CAP_S} "),
        ("absent", ""),
    ],
)
async def test_the_listener_cap_uses_the_hosts_resolved_timeout_style(timeout_style, expected):
    """One probe mechanism, not two.

    The shell-embedded probe was correct but private: leaving it here while
    five sibling capabilities resolve through ``Userland`` is the divergence
    the userland layer exists to prevent.

    Asserted as the WHOLE prefix rather than as ``"-t" in prefix``, which the
    plan proposed and which a prefix of the single word ``-t`` would satisfy.
    Parametrizing all three answers is what makes this read the userland at
    all: any prefix that ignores it agrees with at most one row.
    """
    ft = _make_ft(userland=await _userland_with(timeout_style))
    assert ft._nc_listener_prefix == expected


def test_every_declared_timeout_style_is_mapped_explicitly():
    """A new spelling must not be able to arrive un-noticed.

    Both sides are derived — the vocabulary from the lab-data spec that
    validates it, the handled set from nc's own table — so this cannot pass by
    agreeing with a list someone typed twice. ``absent`` is the one member with
    no prefix by design, and naming it here is what stops "handled" quietly
    meaning "degraded".
    """
    vocabulary = _declared_timeout_styles()
    assert "absent" in vocabulary, (
        "timeout_style no longer reads as a Literal union containing 'absent' — "
        "this guard is looking at the wrong annotation and would pass on anything"
    )
    assert vocabulary - {"absent"} == set(nc_module._TIMEOUT_STYLE_PREFIXES), (
        f"nc caps {sorted(nc_module._TIMEOUT_STYLE_PREFIXES)} but lab data accepts "
        f"{sorted(vocabulary)}. A style with no entry degrades to no cap at all, "
        "which is silent — the state the hard cap exists to end."
    )


@pytest.mark.asyncio
async def test_the_prefix_refuses_to_guess_when_the_userland_is_unresolved():
    """The integration hazard, stated rather than papered over.

    Every ``Userland`` capability raises when read before ``resolve()`` has
    been awaited, and ``_nc_listener_prefix`` is synchronous, so it cannot
    resolve on demand. Catching that and returning "no cap" would be the
    tempting fix and the wrong one: it would reinstate exactly the silent
    divergence this refactor removes, on the path where the cap matters. So it
    propagates, loudly, at the first listener spawn — and every path that
    reaches the prefix awaits resolution first (see the two guards below).
    """
    ft = _make_ft(userland=_unresolved_userland("coreutils"))
    with pytest.raises(RuntimeError, match="timeout_style read before resolve"):
        _ = ft._nc_listener_prefix


@pytest.mark.asyncio
async def test_a_userland_less_backend_costs_the_cap_and_not_the_transfer():
    """The degraded state, now chosen rather than inherited.

    ``None`` is no longer the production state — the host wires its resolver
    through ``TransferContext`` — but it stays reachable for a backend built
    directly with no host behind it, and it must degrade rather than fail: no
    cap, and a transfer that still works. The direction matters. Defaulting to
    the coreutils spelling instead would build ``timeout 3600 nc -l`` on an
    old-BusyBox host, where the applet fails to exec ``3600`` and the listener
    never starts — an outage in place of a missing backstop.

    Both halves are asserted, because the name claims both and only one of
    them used to be checked. ``prepare()`` is where the transfer half would
    break: it awaits ``resolve()`` as its first statement, so a version that
    reached through the ``None`` — or that made the resolver mandatory rather
    than the ARGUMENT mandatory — would raise there, before a single byte
    moved, on a backend the prefix assertion says is fine. This
    ``_make_ft`` declares both nc strategies, so ``prepare()`` has nothing
    else to do and cannot pass for some unrelated reason.
    """
    ft = _make_ft(userland=None)
    assert ft._nc_listener_prefix == ""

    await ft.prepare()  # the transfer half: no resolver is not an error path


def test_the_backend_refuses_to_default_its_way_into_an_uncapped_listener():
    """``userland`` has no default, so omitting it is a TypeError, not silence.

    What replaces the tripwire this file used to carry. An uncapped listener
    is invisible downstream — the spawn sites still interpolate a prefix, it
    is just empty — and ``test_both_listener_spawns_carry_the_hard_cap`` is
    structural and cannot see it either. A construction site that forgets the
    argument therefore has to fail at construction, because nothing later
    will.

    Reads the SIGNATURE rather than calling the constructor: a call with
    ``userland`` omitted but every other argument supplied would still raise
    ``TypeError`` if some unrelated parameter later lost its default, and the
    test would keep passing for the wrong reason.
    """
    userland_param = inspect.signature(NcFileTransfer.__init__).parameters["userland"]
    assert userland_param.default is inspect.Parameter.empty, (
        "`userland` has acquired a default, so a construction site can lose "
        "the listener's hard cap by saying nothing at all."
    )


# Older BusyBox: `timeout [-t SECS] [-s SIG] PROG [ARGS]`. The cut-over is no
# longer a lead: `tests/busybox/test_applet_contracts.py` measures pinned
# BusyBox binaries and finds `-t SECS PROG` accepted up to 1.28.1 and bare
# `SECS PROG` accepted from 1.31.0, mutually exclusive on every build tested.
# A bare leading number is taken as PROG, so the coreutils spelling fails to
# exec. This shim is faithful to the two behaviours that matter — accepts
# `-t SECS PROG`, rejects `SECS PROG` — because no old BusyBox is installable
# on this machine and the modern one rejects `-t`, so the two conventions
# cannot both be exercised here by a real binary.
_OLD_BUSYBOX_TIMEOUT = """#!/bin/sh
case "$1" in --*) echo "timeout: unrecognized option: $1" >&2; exit 1;; esac
if [ "$1" = "-t" ]; then shift 2; exec "$@"; fi
case "$1" in ''|*[!0-9]*) exec "$@";; esac
echo "timeout: can't execute '$1': No such file or directory" >&2
exit 127
"""


def _run_prefix(prefix: str, command: str, path: str) -> subprocess.CompletedProcess[str]:
    """Execute *prefix* + *command* under ``/bin/sh`` with *path* as PATH.

    Not BusyBox's own shell: the ``busybox`` apt installs on this machine has
    ``CONFIG_FEATURE_SH_STANDALONE`` on (measured; busybox.net's own prebuilt
    artifacts do not — see ``tests/busybox/test_applet_resolution.py``), so
    its ``sh`` resolves applets internally and ignores PATH entirely, and a
    shim placed on PATH is never reached. The first version of this control
    used ``busybox sh`` and reported the old-syntax host as working — it had
    silently tested the modern built-in applet.
    """
    return subprocess.run(
        ["/bin/sh", "-c", f"{prefix}{command}"],
        capture_output=True,
        text=True,
        env={"PATH": path},
        timeout=30,
        check=False,
    )


@pytest.mark.asyncio
async def test_the_dash_t_prefix_actually_runs_against_a_dash_t_timeout(tmp_path):
    """A host whose ``timeout`` takes ``-t SECS`` must still transfer, and be capped.

    The mapping test above pins WHICH string is chosen; this pins that the
    chosen string is a command an old-BusyBox host can actually run. Those are
    different failures: on BusyBox < 1.30 the wrong spelling builds
    ``timeout 3600 nc -l ...``, the applet fails to exec ``3600``, and the
    listener never starts — the backstop becoming an outage, strictly worse
    than no backstop. Executed rather than described, because a prefix that
    cannot exec passes any assertion about its text.
    """
    import os

    shim_dir = tmp_path / "oldbb"
    shim_dir.mkdir()
    shim = shim_dir / "timeout"
    shim.write_text(_OLD_BUSYBOX_TIMEOUT)
    shim.chmod(0o755)
    path = f"{shim_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}"

    ft = _make_ft(userland=await _userland_with("dash-t"))
    ran = _run_prefix(ft._nc_listener_prefix, "/bin/echo RAN", path)
    assert ran.returncode == 0, (
        f"the prefix broke the command on an old-BusyBox timeout: {ran.stderr!r}. "
        "This is the outage the convention mapping exists to prevent."
    )
    assert ran.stdout.strip() == "RAN", f"listener never started: {ran.stdout!r}"


@pytest.mark.asyncio
async def test_the_coreutils_prefix_actually_runs_against_a_coreutils_timeout():
    """The other half: the ordinary host gets a cap that works, not one that breaks.

    Same reasoning as the ``-t`` case, against this machine's real coreutils
    ``timeout`` rather than a shim. The two together are what stop a mapping
    that is internally consistent and universally wrong.
    """
    import os

    ft = _make_ft(userland=await _userland_with("coreutils"))
    ran = _run_prefix(
        ft._nc_listener_prefix, "/bin/echo RAN", os.environ.get("PATH", "/usr/bin:/bin")
    )
    assert ran.returncode == 0, f"prefix broke the command: {ran.stderr!r}"
    assert ran.stdout.strip() == "RAN", f"command did not run: {ran.stdout!r}"


@pytest.mark.asyncio
async def test_warming_a_transfer_resolves_the_userland_it_will_read():
    """Where resolution happens, made explicit — the answer is ``prepare()``.

    ``_nc_listener_prefix`` is synchronous and cannot resolve on demand, so
    something before it must. ``prepare()`` is that something, and the
    resolution is its FIRST statement, ahead of the early return it takes when
    both strategies are declared. Placed after that return it would resolve
    only on hosts that happen to use ``auto`` strategies — the incidental
    version of this guarantee, green on the default config and red on a
    perfectly legitimate one.

    ``_make_ft`` builds exactly that legitimate config, and the ``raises``
    below states the premise: without it, a userland that arrived already
    resolved would green this test while proving nothing.
    """
    ft = _make_ft(userland=_unresolved_userland("dash-t"))
    with pytest.raises(RuntimeError, match="read before resolve"):
        _ = ft._nc_listener_prefix

    await ft._warmup_for_transfer(1)

    assert ft._nc_listener_prefix == f"timeout -t {_NC_LISTENER_HARD_CAP_S} "


def test_every_method_that_spawns_a_listener_warms_the_transfer_first():
    """...and the warm-up is reachable from every spawn, checked against the source.

    The behavioural guard above proves warming resolves; this proves warming
    happens. Both are needed: a third spawn site added to a method that never
    warms would pass the first one and raise on a real host at the moment a
    listener was due to start.

    Read off the class rather than listed, so a new spawning method is
    enrolled by existing here at all.
    """
    class_def = next(
        node
        for node in ast.walk(_TREE)
        if isinstance(node, ast.ClassDef) and node.name == "NcFileTransfer"
    )
    methods = [
        node for node in class_def.body if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
    ]

    def _lines(node: ast.AST, attr: str) -> list[int]:
        return [n.lineno for n in ast.walk(node) if isinstance(n, ast.Attribute) and n.attr == attr]

    spawning = [m for m in methods if _lines(m, "_nc_listener_prefix")]
    assert len(spawning) == 2, (
        f"expected the two spawning methods (get-tunneled and put) to read the "
        f"prefix, found {[m.name for m in spawning]} — this query has rotted and "
        "would pass on anything"
    )
    unwarmed = [m.name for m in spawning if not _lines(m, "_warmup_for_transfer")]
    assert not unwarmed, (
        f"{unwarmed} spawns a capped listener without warming the transfer first. "
        "Warming is what awaits Userland.resolve(); without it the prefix raises "
        "instead of returning a cap."
    )
    # ORDER, not just co-occurrence: a warm-up moved below the spawn satisfies
    # the check above and resolves nothing in time. Source order is a proxy —
    # both prefixes are read inside a nested `_attempt` that is defined after
    # the warm-up and called after it, so textual position tracks execution
    # here. It would NOT for a nested helper defined early and called late;
    # that shape does not exist in this file and the behavioural guard above is
    # what would catch it if it appeared.
    late = [
        m.name
        for m in spawning
        if min(_lines(m, "_warmup_for_transfer")) > min(_lines(m, "_nc_listener_prefix"))
    ]
    assert not late, (
        f"{late} warms the transfer AFTER the listener spawn that needs it. The "
        "prefix is read while the userland is still unresolved, so it raises."
    )


def test_the_listener_spawns_are_plain_foreground_commands():
    """The precondition that makes the hard cap survivable, pinned at last.

    GNU ``timeout`` calls ``setpgid(0,0)``, which on the face of it moves
    ``nc`` out of the foreground process group and out of reach of the SIGHUP
    a hangup delivers — the cap outliving the session it should be bounded by.
    It does not, but only because a job-control shell has ALREADY given the
    foreground job its own process group and handed it the terminal, making
    the call a no-op. Measured on a pty by closing the master, with a real
    listener: unwrapped, wrapped, and wrapped with ``--foreground`` all die on
    hangup; only ``set +m`` leaves the listener alive at ``ppid=1``.

    So the correctness rests on HOW the command is composed, and that was
    unguarded. Put a spawn inside ``( … )`` or ``$( … )`` — a subshell is not
    a job and never gets the terminal — or background it, and the listener
    stops dying with its session. Silently, on the path where nothing else
    would notice.

    This exists instead of ``timeout --foreground``, which was added on the
    strength of a measurement taken inside ``$(...)``. Command substitution
    disables job control, so that reading described a shell otto never
    produces. The flag changes nothing measurable on either otto path, which
    is precisely why no behavioural test could fail when it was removed —
    whereas this one can.
    """
    # Over the whole f-string expression, not per line: the command is split
    # across continuation lines, so a line-wise check cannot see a trailing
    # `&` and would happily match the prose in this module's own comments.
    spawns = [
        node
        for node in ast.walk(_TREE)
        if isinstance(node, ast.JoinedStr)
        and any(
            isinstance(v, ast.FormattedValue)
            and isinstance(v.value, ast.Attribute)
            and v.value.attr == "_nc_listener_prefix"
            for v in node.values
        )
    ]
    assert len(spawns) == 2, (
        f"expected 2 listener spawn expressions carrying the prefix, found {len(spawns)}"
    )
    for node in spawns:
        text = ast.get_source_segment(_SOURCE, node) or ""
        flat = " ".join(text.split())
        before_prefix = flat.split("{self._nc_listener_prefix}", 1)[0]
        assert not before_prefix.rstrip("f\"' ").rstrip().endswith(("(", "$(")), (
            f"nc.py:{node.lineno} composes the listener into a subshell: {flat[:90]!r}. "
            "A subshell is not a job, so it never gets the terminal, and the "
            "listener stops receiving the hangup that currently kills it."
        )
        backgrounded = flat.replace("2>&1", "").replace("&&", "")
        assert "&" not in backgrounded, (
            f"nc.py:{node.lineno} backgrounds the listener: {flat[:90]!r}. A "
            "background job is not in the foreground process group either, so "
            "it survives the hangup that should end it."
        )


@pytest.mark.asyncio
async def test_an_unrecognised_timeout_style_degrades_instead_of_breaking():
    """The assumption the whole design rests on, which nothing else tests.

    Three answers are mapped; the bet is that a fourth — a vendor variant
    nobody here has seen, reaching ``UserlandOptions`` past the lab-data spec
    that would have rejected it — loses the cap rather than the transfer.
    Every other test in this file exercises an answer that IS handled, so all
    of them would still pass if that bet were wrong.

    NO SHIM, DELIBERATELY. This test used to build a ``--duration=N``-only
    ``timeout`` on PATH and then execute the prefix against it, on the stated
    grounds that "the degradation has to be that otto declines to wrap, not
    that it wraps and the wrapper happens to be forgiving". That was false: the
    equality below binds the prefix to ``""``, so nothing ever invoked the
    shim, and the execution was ``/bin/sh -c "/bin/echo RAN"``. Measured —
    deleting those three lines left 18 passed, and under the cap-by-default
    mutation the failure was byte-identical with and without them. An
    execution that cannot fail is worse than no execution, because its
    docstring tells the next reader a hazard is covered. The equality is the
    whole guard; the two spellings that DO produce a command are executed in
    their own tests above, where the exec is the only assertion.
    """
    ft = _make_ft(userland=await _userland_with("vendor-duration"))
    prefix = ft._nc_listener_prefix
    assert prefix == "", (
        f"expected no cap against an unrecognised style, got {prefix!r} — a "
        "wrapper otto cannot drive was selected anyway"
    )
