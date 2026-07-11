"""Sentinel-tagged daemons on remote hosts: launch, discover, reap.

``launch_command`` wraps an argv in ``bash -c 'exec -a "$1" "${@:2}"'`` so the
process's argv[0] IS the sentinel (discoverable via ``ps -eo args=``), launched
under ``systemd-run --user`` with a ``setsid`` fallback. Extracted from
``otto.tunnel.socat`` (#2b), renamed from ``otto.host.detached``
(2026-07-11): the module owns the daemon lifecycle vocabulary — launch,
discover (ps scan), reap — shared by tunnels and link-impairment timers
without a tunnel<->link import edge.
"""

import re
import shlex
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from urllib.parse import quote, unquote

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


def launch_command(sentinel: str, argv: list[str]) -> str:
    """Build the ``host.exec`` line for a detached, tagged, session-surviving daemon.

    ``bash -c 'exec -a "$1" "${@:2}"' _ <sentinel> <program argv…>`` sets the
    process's ``argv[0]`` to the sentinel (``exec -a`` — a bash builtin; bash is
    required on the target host). ``argv`` is the FULL program argv (its first element
    is the program to run, e.g. ``"socat"``), so the template must NOT hardcode a
    program name — hardcoding one runs ``prog prog <args…>`` and dies on the bogus
    duplicate.

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
    real invocation is additionally bounded by ``_SYSTEMD_RUN_PROBE_TIMEOUT``
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
    tagged = " ".join(shlex.quote(a) for a in (sentinel, *argv))
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


_ETIME_MAX_FIELDS = 3
_MIN_PS_FIELDS = 3


_PREFIX_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")
"""Sentinel prefixes are spliced into a single-quoted grep BRE (see below), so
they are restricted to characters that are inert in BOTH contexts."""


def ps_scan_command(prefix: str) -> str:
    """Portable ``ps`` scan for daemons whose argv[0] starts with ``<prefix>:``.

    Each field is its own ``-eo`` flag rather than one comma-joined
    ``-eo pid=,etime=,args=`` (found via live-bed e2e against a centos:7
    container): procps-ng 3.3.10 silently mis-parses the comma-combined form
    (columns bleed into each other), while the separate-flag form produces
    identical output on modern procps (4.x) too. Formatted ``etime`` (not
    ``etimes``) keeps 2.6.32-era userland working; ``|| true`` so a no-match
    grep (exit 1) is not a command failure.

    *prefix* must match ``[A-Za-z0-9][A-Za-z0-9-]*``: it lands inside a
    single-quoted grep BRE, where a quote would break the shell line, a regex
    metacharacter would change match semantics, and the ``|| true`` tail would
    mask the resulting grep failure as an empty (falsely clean) scan.

    Raises:
        ValueError: If *prefix* contains characters outside that safe set.
    """
    if not _PREFIX_RE.match(prefix):
        raise ValueError(
            f"daemon sentinel prefix {prefix!r} must match [A-Za-z0-9][A-Za-z0-9-]* — "
            "it is spliced into a single-quoted grep pattern"
        )
    return f"ps -eo pid= -eo etime= -eo args= 2>/dev/null | grep -a ' {prefix}:' || true"


def parse_etime(text: str) -> int:
    """Procps ``etime`` (``[[DD-]HH:]MM:SS`` or bare ``SS``) → seconds.

    Returns ``0`` for anything unparseable rather than raising — one host
    emitting a malformed ``etime`` must not take down a whole scan.
    """
    try:
        days = 0
        if "-" in text:
            d, _, text = text.partition("-")
            days = int(d)
        parts = [int(p) for p in text.split(":")]
        while len(parts) < _ETIME_MAX_FIELDS:
            parts.insert(0, 0)
        h, m, s = parts[-3], parts[-2], parts[-1]
        return days * 86400 + h * 3600 + m * 60 + s
    except ValueError:
        return 0


@dataclass(frozen=True, slots=True)
class DaemonProcess:
    """One sentinel-tagged daemon seen in a ps scan (token not yet decoded)."""

    pid: int
    age_seconds: int
    token: str


def parse_ps_output(output: str, prefix: str) -> list[DaemonProcess]:
    """Reconstruct tagged daemons from :func:`ps_scan_command` output.

    Domain modules decode each :attr:`DaemonProcess.token` with their own
    sentinel parser; anything undecodable is theirs to skip.
    """
    needle = f"{prefix}:"
    out: list[DaemonProcess] = []
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < _MIN_PS_FIELDS or not fields[0].isdigit():
            continue
        token = next((w for w in fields[2:] if w.startswith(needle)), None)
        if token is None:
            continue
        out.append(
            DaemonProcess(pid=int(fields[0]), age_seconds=parse_etime(fields[1]), token=token)
        )
    return out


def kill_command(pids: Iterable[int]) -> str:
    """``kill <sorted pids>`` line for reaping tagged daemons on one host.

    Raises:
        ValueError: If *pids* is empty — a bare ``kill`` is a usage error on
            the host, so an empty reap set must be handled by the caller.
    """
    sorted_pids = sorted(pids)
    if not sorted_pids:
        raise ValueError("kill_command needs at least one pid")
    return f"kill {' '.join(str(p) for p in sorted_pids)}"


def enc(value: str | int | None) -> str:
    """Percent-encode one sentinel segment (``safe=""``); ``None`` → empty."""
    return quote(str(value), safe="") if value is not None else ""


def dec(segment: str) -> str:
    """Decode one percent-encoded sentinel segment."""
    return unquote(segment)


def encode_token(prefix: str, version: str, segments: Sequence[str]) -> str:
    """``<prefix>:<version>:<payload segments joined with ':'>``.

    *segments* are the payload only and must be FINAL strings (already
    percent-encoded as the domain codec requires) — framing never re-encodes,
    so a domain is free to double-encode a compound segment.
    """
    return ":".join((prefix, version, *segments))


def split_token(token: str, prefix: str, version: str, count: int) -> list[str] | None:
    """Split a wire token; ``None`` for non-matching / other-version / malformed.

    *count* is the expected PAYLOAD segment count (prefix and version are
    checked separately). Unknown versions parse to ``None``, never an error —
    the stability contract lets old parsers ignore newer wire formats.
    """
    parts = token.split(":")
    if len(parts) != count + 2 or parts[0] != prefix or parts[1] != version:
        return None
    return parts[2:]
