"""One batched probe per host: what a check needs to know before it tests anything.

Every line the probe prints is ``key=value``; the parser keeps what it
recognises and ignores the rest, so a chatty login banner cannot break it.
Tool presence uses the same ``command -v`` idiom as userland detection
(``otto.host.userland``), and a tool whose resolved path is a BusyBox
multi-call binary is recorded as such, because BusyBox ``tc`` and ``ping``
are different programs from their GNU namesakes.
"""

import re
import shlex
from dataclasses import dataclass
from typing import Any

from ..host.errors import UnsupportedOnUserlandError, exec_or_raise
from ..logger.mode import LogMode
from ..result import CommandResult
from .errors import CheckCommandFailedError, CheckHostUnreachableError

CHECK_HOST_TIMEOUT = 30.0
LINK_TOOLS = ["tc", "ip", "ping", "socat", "python3", "bash"]
LINK_VERSIONS = {"iproute2": "tc -V 2>&1 | head -n1"}

_IPROUTE2_RE = re.compile(r"iproute2-((?:ss\d{6})|(?:\d+(?:\.\d+)*))")
_EXTRACTORS = {"iproute2": _IPROUTE2_RE}


@dataclass(frozen=True)
class HostFingerprint:
    """What one host is, as far as a check is concerned."""

    host_id: str
    address: str
    kernel: str | None
    isa: str | None
    userland: str
    user: str | None
    privileged: bool
    """Whether otto can run commands as root here. The read-only probe only
    knows a root login; :func:`~otto.check.fingerprint.probe_elevation` asks
    otto's own elevation and the check records its answer here."""
    netns: bool
    netem_module: bool | None
    """``True`` loaded, built in or installed; ``False`` when ``modinfo`` says
    the module does not exist; ``None`` when the host cannot tell."""
    tools: dict[str, bool]
    versions: dict[str, str | None]
    raw: str


async def check_exec(host: Any, cmd: str) -> CommandResult:
    """Run read-only *cmd* on *host*; a down host or failed read raises host-named."""
    return await exec_or_raise(
        host,
        cmd,
        timeout=CHECK_HOST_TIMEOUT,
        unreachable=CheckHostUnreachableError,
        failed=CheckCommandFailedError,
    )


def one_command(cmd: str) -> str:
    """Wrap shell *cmd* as ``sh -c <cmd>``: one word an elevation prefix cannot split.

    Otto elevates by prefixing (``sudo -S -p '…' <cmd>``), which only covers a
    single simple command. Anything else a check sends — a ``for`` loop, a
    ``;`` list, a pipe, a redirection, a trailing ``&`` — would otherwise be
    split by the login shell: a syntax error, the tail run unelevated, or the
    redirections applied to ``sudo`` itself (so ``sudo -S`` reads its password
    from ``/dev/null``). ``sh`` may be dash or BusyBox ash, so a command that
    needs bash features carries its own ``bash -c`` layer inside.
    """
    return f"sh -c {shlex.quote(cmd)}"


async def check_read(host: Any, cmd: str) -> CommandResult:
    """Run read-only *cmd* on *host*; a failed command is RETURNED, not raised.

    For the reads inside a check whose failure is a row's verdict rather
    than the end of the run. Only a down host or a timed-out command raises
    :class:`~otto.check.errors.CheckHostUnreachableError`, host-named.
    """
    try:
        result = await host.exec(cmd, timeout=CHECK_HOST_TIMEOUT, log=LogMode.QUIET)
    except (OSError, ConnectionError) as e:
        raise CheckHostUnreachableError(
            f"host {host.id!r} unreachable running {cmd!r}: {e!r}"
        ) from e
    if result.timed_out:
        raise CheckHostUnreachableError(
            f"host {host.id!r} unreachable running {cmd!r}: timed out after {CHECK_HOST_TIMEOUT}s"
        )
    return result


async def _elevated(host: Any, cmd: str, timeout: float) -> CommandResult:
    """Run *cmd* as :func:`one_command`, sudo'd unless already root; a timeout is returned.

    Only a transport failure raises
    :class:`~otto.check.errors.CheckHostUnreachableError`, host-named.
    """
    need_sudo = host.current_user != "root"
    try:
        results = await host.run(
            one_command(cmd), sudo=need_sudo, timeout=timeout, log=LogMode.QUIET
        )
    except (OSError, ConnectionError) as e:
        raise CheckHostUnreachableError(
            f"host {host.id!r} unreachable running {cmd!r}: {e!r}"
        ) from e
    return results[0]


async def check_root_run(
    host: Any, cmd: str, *, timeout: float = CHECK_HOST_TIMEOUT
) -> CommandResult:
    """Run a privileged *cmd* on *host*, sudo'd unless already root.

    *cmd* always runs as :func:`one_command` — sudo'd or not, so the root and
    the sudo paths run exactly the same shell text.

    Unlike :func:`~otto.check.check_exec`, this NEVER raises on a non-ok result: a
    rejected ``tc`` command is a verdict the caller judges, not a transport
    failure. Only a down host or a command still running after *timeout*
    raises :class:`~otto.check.errors.CheckHostUnreachableError`, host-named.
    """
    result = await _elevated(host, cmd, timeout)
    if result.timed_out:
        raise CheckHostUnreachableError(
            f"host {host.id!r} unreachable running {cmd!r}: timed out after {timeout}s"
        )
    return result


def _netem_probe(sys_root: str = "/sys", lib_root: str = "/lib") -> str:
    """Build the ``netem=1|0|?`` probe; the roots are parameters only so tests can fake them.

    ``1``: loaded, built in (``modules.builtin``) or installed (``modinfo``).
    ``0`` only where a module tree exists for the module to be missing from
    and ``modinfo`` searched it. A kernel with no tree (monolithic, custom) or
    a host without ``modinfo`` gives ``?``: otto cannot tell.
    """
    tree = f"{lib_root}/modules/$(uname -r)"
    return (
        f"if [ -d {sys_root}/module/sch_netem ] || "
        f'grep -qs sch_netem "{tree}/modules.builtin" || '
        "modinfo sch_netem >/dev/null 2>&1; then echo netem=1; "
        f'elif [ -d "{tree}" ] && command -v modinfo >/dev/null 2>&1; then echo netem=0; '
        "else echo netem=?; fi"
    )


def fingerprint_command(tools: list[str], versions: dict[str, str]) -> str:
    """Build the single batched shell command behind :func:`probe_fingerprint`."""
    names = " ".join(tools)
    parts = [
        'echo "kernel=$(uname -r)"',
        'echo "isa=$(uname -m)"',
        'echo "user=$(id -un 2>/dev/null)"',
        (
            f'for t in {names}; do p=$(command -v "$t" 2>/dev/null); '
            'if [ -n "$p" ]; then echo "tool:$t=1"; '
            'case "$(readlink -f "$p" 2>/dev/null)" in *busybox*) echo "busybox:$t=1";; esac; '
            'else echo "tool:$t=0"; fi; done'
        ),
        "ip netns list >/dev/null 2>&1 && echo netns=1 || echo netns=0",
        _netem_probe(),
    ]
    parts += [f'echo "ver:{name}=$({cmd})"' for name, cmd in versions.items()]
    return "; ".join(parts)


def _extract_version(component: str, raw: str) -> str | None:
    pattern = _EXTRACTORS.get(component)
    if pattern is None:
        return raw.strip() or None
    match = pattern.search(raw)
    return match.group(1) if match else None


def parse_fingerprint(host_id: str, address: str, output: str) -> HostFingerprint:
    """Parse :func:`fingerprint_command` output; unknown lines are ignored."""
    fields: dict[str, str] = {}
    tools: dict[str, bool] = {}
    busybox: set[str] = set()
    versions: dict[str, str | None] = {}
    for line in output.splitlines():
        key, sep, value = line.partition("=")
        if not sep:
            continue
        if key.startswith("tool:") and value in ("0", "1"):
            tools[key[5:]] = value == "1"
        elif key.startswith("busybox:") and value == "1":
            busybox.add(key[8:])
        elif key.startswith("ver:"):
            versions[key[4:]] = _extract_version(key[4:], value)
        elif key in ("kernel", "isa", "user", "netns", "netem"):
            fields[key] = value.strip()
    present = [t for t, ok in tools.items() if ok]
    if busybox:
        userland = "busybox"
    elif present:
        userland = "gnu"
    else:
        userland = "unknown"
    user = fields.get("user") or None
    return HostFingerprint(
        host_id=host_id,
        address=address,
        kernel=fields.get("kernel") or None,
        isa=fields.get("isa") or None,
        userland=userland,
        user=user,
        privileged=user == "root",
        netns=fields.get("netns") == "1",
        netem_module={"1": True, "0": False}.get(fields.get("netem", "?")),
        tools=tools,
        versions=versions,
        raw=output,
    )


ELEVATION_PROBE = "id -u"
"""What :func:`probe_elevation` runs through otto's own elevation: root prints ``0``."""

ELEVATION_TIMEOUT_S = 8.0
"""How long :func:`probe_elevation` waits for ``id -u``: a root shell answers at once."""

CREDS_DOCS = "docs/configuration/lab-config (Per-host fields: creds)"


@dataclass(frozen=True)
class Elevation:
    """Whether otto could become root on one host, and what it saw trying."""

    ok: bool
    command: str
    output: str | None
    hint: str | None = None
    """Why it failed, when otto can tell more than the output says."""


async def probe_elevation(host: Any) -> Elevation:
    """Ask *host* whether otto can run commands as root, the way every check command runs.

    :func:`check_root_run` elevates through the host's own mechanism (``sudo``
    or ``su``, answering a password prompt from the lab's credentials), so a
    sudo that asks for a password counts, and so does a BusyBox host with only
    ``su``. A host whose userland offers neither is not root either.

    The probe waits only :data:`ELEVATION_TIMEOUT_S`: a prompt the lab has no
    password for never answers, and that host is not root, not unreachable.
    A host that drops the connection still raises
    :class:`~otto.check.errors.CheckHostUnreachableError`.
    """
    try:
        result = await _elevated(host, ELEVATION_PROBE, ELEVATION_TIMEOUT_S)
    except (UnsupportedOnUserlandError, NotImplementedError) as e:
        return Elevation(False, ELEVATION_PROBE, str(e))
    output = (result.value or "").strip()
    if result.timed_out:
        # The host answered the session but not the command: sudo or su is
        # sitting at a password prompt otto had no password to answer. The
        # session interrupts it (Ctrl-C) when the run times out.
        return Elevation(
            False,
            ELEVATION_PROBE,
            output or None,
            hint=(
                f"`{ELEVATION_PROBE}` got no answer within {ELEVATION_TIMEOUT_S:g} s — sudo "
                "or su is likely waiting for a password the lab does not declare; declare it "
                f"in the host's creds (see {CREDS_DOCS})"
            ),
        )
    last = output.splitlines()[-1].strip() if output else ""
    return Elevation(result.is_ok and last == "0", ELEVATION_PROBE, output or None)


async def probe_fingerprint(
    host: Any, *, tools: list[str], versions: dict[str, str]
) -> HostFingerprint:
    """Fingerprint *host* with one batched read-only command."""
    result = await check_exec(host, fingerprint_command(tools, versions))
    return parse_fingerprint(host.id, getattr(host, "ip", ""), result.value or "")
