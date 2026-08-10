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
"""

import ast
import inspect
from pathlib import Path

import pytest

from otto.host.transfer import nc as nc_module

_SOURCE = Path(inspect.getfile(nc_module)).read_text()
_TREE = ast.parse(_SOURCE)

# The one function allowed to cancel a listener task, because it is the one
# that reaps immediately afterwards.
_REAPING_FUNCTION = "_cancel_and_reap"


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


@pytest.mark.parametrize(
    ("path", "expect_wrapped"),
    [("/nonexistent", False), (None, True)],
)
def test_hard_cap_prefix_degrades_when_timeout_is_absent(path, expect_wrapped):
    """Actually run the prefix in a shell, with and without `timeout` on PATH.

    The first version of this test asserted three substrings were present in a
    string, which proves nothing about behaviour: a prefix that failed to exec
    would pass it. A backstop that makes transfers impossible on a host lacking
    coreutils would be a worse bug than the leak it fixes, so the degradation
    is executed, not described.
    """
    import os
    import subprocess

    from otto.host.transfer.nc import NcFileTransfer

    prefix = NcFileTransfer._nc_listener_prefix.fget(object.__new__(NcFileTransfer))  # type: ignore[attr-defined]
    env = {"PATH": path} if path else {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    proc = subprocess.run(
        ["/bin/sh", "-c", f"{prefix}/bin/echo RAN"],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, f"prefix broke the command: {proc.stderr!r}"
    assert proc.stdout.strip() == "RAN", f"command did not run: {proc.stdout!r}"

    which = subprocess.run(
        ["/bin/sh", "-c", f"{prefix}true; command -v timeout >/dev/null 2>&1 && echo HAVE"],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )
    assert (which.stdout.strip() == "HAVE") is expect_wrapped
