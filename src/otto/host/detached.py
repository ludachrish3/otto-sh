"""Detached, sentinel-tagged process launching on remote hosts.

``launch_command`` wraps an argv in ``bash -c 'exec -a "$1" "${@:2}"'`` so the
process's argv[0] IS the sentinel (discoverable via ``ps -eo args=``), launched
under ``systemd-run --user`` with a ``setsid`` fallback. Extracted from
``otto.tunnel.socat`` (#2b) so both tunnels and link-impairment timers use one
proven launcher without a tunnel<->link import edge.
"""

import shlex

# Ceiling (seconds) on the *real* ``systemd-run --user`` invocation folded into
# the ``if`` condition below. ``systemd-run`` being present and even connecting
# to a user bus doesn't guarantee the unit actually starts promptly: a broken
# or half-configured user session (e.g. sudo'd root inheriting a stale/foreign
# ``XDG_RUNTIME_DIR``/dbus address from the login user's environment) can make
# ``systemd-run --user`` connect-and-hang instead of failing fast. The fold-
# through to the ``setsid`` fallback below only works when the broken branch
# fails FAST (see the centos:7 lesson two paragraphs down) — a HANG-shaped
# failure defeats it, since the caller's own timeout fires first. ``timeout``
# is coreutils, present on every unix host otto targets (centos:7 included),
# so bounding the probe here degrades any hang-shaped breakage into a fast,
# fold-through-compatible failure (live-bed finding 2026-07-10).
_SYSTEMD_RUN_PROBE_TIMEOUT = 5


def launch_command(sentinel: str, socat_args: list[str]) -> str:
    """Build the ``host.exec`` line for a detached, tagged, session-surviving tunnel.

    ``bash -c 'exec -a "$1" "${@:2}"' _ <sentinel> <socat argv…>`` sets the
    process's ``argv[0]`` to the sentinel (``exec -a`` — a bash builtin; bash is
    required on tunnel hosts). ``socat_args`` is the FULL program argv (it begins
    with ``"socat"``), so the template must NOT hardcode ``socat`` — hardcoding it
    runs ``socat socat <addr> <addr>`` and dies on the bogus third address.

    Surviving the ssh session is the subtle part (found via live-bed e2e). On a
    systemd host, a process left in the ssh session's scope is killed when that
    session ends, and ``setsid`` does NOT escape the session cgroup — so we
    launch it in the USER manager's scope via ``systemd-run --user`` (no sudo, no
    root; the transient unit is ``--collect``ed on exit). On a non-systemd host
    (older distros — the portability floor), ``systemd-run`` is absent and a
    plain ``setsid``-detached background process survives normally, so we fall
    back to that.

    ``systemd-run`` being present on ``PATH`` doesn't guarantee it *works*
    (found via live-bed e2e against a centos:7 container): the binary ships in
    the base image even though nothing runs an actual systemd/dbus user
    session inside the container, so invoking it fails fast with "Failed to
    create bus connection: Connection refused" rather than being absent. The
    ``&&`` folds that real invocation into the ``if`` condition itself (not
    just a ``command -v`` existence probe), so any failure — missing binary or
    a present-but-unusable one — falls through to the ``setsid`` branch. The
    real invocation is additionally bounded by :data:`_SYSTEMD_RUN_PROBE_TIMEOUT`
    so a hang-shaped breakage (not just a fast-failing one) still folds through.

    The whole ``if``/``then``/``else``/``fi`` conditional is wrapped in an outer
    ``bash -c '<body>'`` so the returned string is one opaque, quoted word —
    safe for a caller to compose into a larger command by naive textual
    prefixing (found via live-bed e2e: ``otto.host.privilege.PosixPrivilege
    ._elevate`` builds ``f"sudo -S -p 'x' {cmd}"``; bash only recognizes ``if``
    as the reserved word that opens a conditional in command-start position,
    so splicing it in as ``sudo``'s trailing argument list demotes ``if`` to a
    literal word and leaves the later ``then``/``else``/``fi`` as syntax errors
    with no matching ``if``. bash then refuses to parse the *entire* input line
    — not just this fragment — so not even the caller's own sentinel echoes
    run, and the caller hangs until its own outer timeout. This is what
    ``otto.link``'s expire-timer launch (the only ``sudo=True`` caller of this
    function) was actually hitting, not a systemd-run/dbus stall: reproduced
    3/3 unpatched (12s bound, never completes) and 3/3 resolved by this wrap
    (sub-20ms, exit 0) against the live bed. A single outer ``bash -c`` layer
    is harmless to the survival mechanics — it only dispatches the real branch
    (``systemd-run --user --collect`` or the backgrounded ``setsid`` subshell)
    and exits; the final ``exec -a``'d process is unaffected either way).
    """
    inner = shlex.quote('exec -a "$1" "${@:2}"')
    tagged = " ".join(shlex.quote(a) for a in (sentinel, *socat_args))
    systemd = (
        f"timeout {_SYSTEMD_RUN_PROBE_TIMEOUT} systemd-run --user --collect --quiet "
        f"-- bash -c {inner} _ {tagged}"
    )
    setsid = f"setsid bash -c {inner} _ {tagged} </dev/null >/dev/null 2>&1 &"
    body = (
        f"if command -v systemd-run >/dev/null 2>&1 && {systemd} 2>/dev/null; "
        f"then :; else ( {setsid} ); fi"
    )
    return f"bash -c {shlex.quote(body)}"
