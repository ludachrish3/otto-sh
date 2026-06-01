"""Root test conftest — shared fixtures across unit and integration tests."""

import os

# Disable colored CLI output before typer/click/rich are imported anywhere.
# CI runners (e.g. GitHub Actions) set FORCE_COLOR, which causes Rich to embed
# ANSI escapes in help/error text and breaks substring assertions like
# `'--flag' in result.output`.
os.environ["NO_COLOR"] = "1"
os.environ["TERM"] = "dumb"
for _var in ("FORCE_COLOR", "CLICOLOR_FORCE", "PY_COLORS", "CLICOLOR"):
    os.environ.pop(_var, None)

import json  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

from otto.host.host import setDryRun  # noqa: E402
from otto.host.localHost import LocalHost  # noqa: E402
from otto.host.unixHost import UnixHost  # noqa: E402
from otto.logger import getOttoLogger  # noqa: E402
from otto.storage.factory import create_host_from_dict  # noqa: E402

_logger = getOttoLogger()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item: pytest.Item):
    """Implement ``@pytest.mark.retry(n)`` for dev pytest runs.

    Provides the marker under bare ``pytest`` — ``otto.suite.plugin.OttoPlugin``
    only registers under ``otto test``. Used to gate known-flaky integration
    tests (nc transfers through an SSH hop) — see
    ``todo/hop_nc_transfer_flake.md`` for the underlying issue.

    Implemented as a hookwrapper so the first attempt runs through the default
    hook and retries override the outcome on success — a plain ``tryfirst``
    impl would let the default re-run (and possibly fail) the test after a
    retry succeeded.
    """
    outcome = yield
    retry_marker = item.get_closest_marker('retry')
    if retry_marker is None or outcome.excinfo is None:
        return
    n = int(retry_marker.args[0]) if retry_marker.args else 1
    first_exc = outcome.excinfo[1]
    _logger.warning(f'retry: {item.nodeid} attempt 1/{n} failed: {first_exc}')
    for attempt in range(1, n):
        try:
            item.runtest()
        except Exception as exc:
            _logger.warning(
                f'retry: {item.nodeid} attempt {attempt + 1}/{n} failed: {exc}'
            )
            outcome.force_exception(exc)
            continue
        outcome.force_result(None)
        return


def pytest_configure(config):  # type: ignore[no-untyped-def]
    _install_sigint_traceback_dump()


def _install_sigint_traceback_dump() -> None:
    """Dump every thread's stack on the first Ctrl-C, then fall through to
    pytest's normal interrupt handling so the JUnit report is still emitted.

    ``pytest-timeout`` (configured in ``pyproject.toml``: 180s, signal method)
    already covers *hung* tests — it fails the test and lets the session reach
    sessionfinish. This covers the third case: *you* decide to bail early.
    Without it, a Ctrl-C while a worker is wedged in a blocking C call gives no
    diagnostics and, under xdist, often no report.

    ``chain=True`` runs faulthandler's C-level dump and then the previous
    SIGINT handler (CPython's, which raises ``KeyboardInterrupt``), so pytest
    still unwinds to ``pytest_sessionfinish`` and the junitxml plugin writes
    its file. Registered in the controller and every xdist worker (conftest is
    imported in each), so the dump shows the worker actually stuck, not just
    the controller. Stacks go to the real stderr fd.
    """
    import faulthandler
    import signal
    import sys

    if not hasattr(faulthandler, 'register'):  # not available on Windows
        return
    faulthandler.register(
        signal.SIGINT, file=sys.stderr, all_threads=True, chain=True,
    )


def pytest_unconfigure(config):  # type: ignore[no-untyped-def]
    import faulthandler
    import signal

    if hasattr(faulthandler, 'unregister'):
        faulthandler.unregister(signal.SIGINT)


# ---------------------------------------------------------------------------
# Asyncio leak detector (diagnostic, autouse on host tests)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _detect_asyncio_leaks(request):
    """Attribute leaked asyncio transports to the test that created them.

    Recipe from ``~/wiki/inbox/2026-04-24-detect-asyncio-leaks-at-source.md``:
    after each test, look for live transports whose ``_loop`` is closed.
    Those are the things that fire ``ResourceWarning`` from ``__del__`` at
    GC time and are then escalated by pytest's ``[unraisable]`` plugin into
    a ``PytestUnraisableExceptionWarning`` on whichever *next* test happens
    to be running — the source of the xdist-flake symptom.

    Only enabled by setting ``OTTO_DETECT_ASYNCIO_LEAKS=1`` in the env so
    it doesn't slow the regular run with the per-test ``gc.collect()``.
    """
    yield
    import os
    if not os.environ.get('OTTO_DETECT_ASYNCIO_LEAKS'):
        return
    import gc
    from asyncio.base_subprocess import BaseSubprocessTransport
    from asyncio.selector_events import _SelectorTransport
    gc.collect()
    leaks = []
    for o in gc.get_objects():
        if not isinstance(o, (BaseSubprocessTransport, _SelectorTransport)):
            continue
        loop = getattr(o, '_loop', None)
        if loop is None or not loop.is_closed():
            continue
        # Filter to ones that would actually emit a ResourceWarning from
        # __del__: i.e., the transport is still "open" (closing flag unset).
        # Already-closed transports don't warn even if they linger in GC.
        closing = getattr(o, '_closing', None)
        sock = getattr(o, '_sock', None)
        details = f' closing={closing} sock={sock!r}'
        # Show what's referencing this transport so we can find the leak.
        referrers = gc.get_referrers(o)
        ref_summary = ', '.join(
            f'{type(r).__module__}.{type(r).__name__}'
            for r in referrers[:5] if r is not gc.get_referrers and r is not leaks
        )
        leaks.append(f"{o!r}{details}\n    referrers: {ref_summary}")
    if leaks:
        # Print rather than raise: we want to *attribute* the leak, not
        # fail the test that detected it.
        print(f"\nLEAK after {request.node.nodeid}: {len(leaks)} live transport(s) bound to closed loop:")
        for l in leaks:
            print(f"  {l}")


# ---------------------------------------------------------------------------
# Dry-run reset (autouse on every test — leaks across tests via the module
# global otherwise).
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_dry_run():
    """Ensure the global dry-run flag is off before and after every test.

    Without this, tests in test_dry_run.py that call ``setDryRun(True)`` can
    leak into other tests when pytest-xdist runs them in the same worker.
    """
    setDryRun(False)
    yield
    setDryRun(False)


# ---------------------------------------------------------------------------
# Lab-data helpers
# ---------------------------------------------------------------------------

_LAB_DATA = Path(__file__).parent / "lab_data" / "tech1" / "hosts.json"


def host_data(ne: str) -> dict[str, Any]:
    """Return the raw host dict for a given NE name from the lab JSON."""
    hosts = json.loads(_LAB_DATA.read_text())
    for host in hosts:
        if host["ne"] == ne:
            return host
    raise KeyError(f"NE {ne!r} not found in {_LAB_DATA}")


def make_host(ne: str, **kwargs: Any) -> UnixHost:
    """Build a UnixHost from lab data with optional field overrides."""
    data = host_data(ne)
    return UnixHost(
        ip=data["ip"],
        ne=data["ne"],
        creds=data["creds"],
        board=data.get("board"),
        is_virtual=data.get("is_virtual", False),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Parameterized host fixtures (driven by @pytest.mark.parametrize + indirect)
#
# These are shared by both unit-tree integration tests (currently in
# tests/unit/host/) and the new tests/integration/host/ tree. They yield real
# host objects backed by the Vagrant test bed — tests must be gated by
# @pytest.mark.integration (Unix) or @pytest.mark.embedded (Zephyr).
# ---------------------------------------------------------------------------

# Mapping from `host1` parametrize value -> the embedded host's lab `ne` name.
# Lets host1 construct an EmbeddedHost directly via the factory without
# special-casing each Zephyr config in the fixture body.
#
# The matrix is anchored on Zephyr 3.7 LTS, which carries the full
# {FAT-on-RAM, LittleFS, no-FS} set; 2.7 and 4.4 each contribute a single fs
# cell — five QEMU instances on the `zephyr` Vagrant VM. The trim is
# deliberate: otto's exercised device surface is only `fs read` / `fs write` /
# `fs rm` plus the command-frame retcode parse (perf/disk metrics ride SNMP,
# not the shell), and none of the per-version `fs` divergences (cp/mv, the
# `ls` size column, the `kernel thread` rename) are touched. So fat-vs-lfs is
# a version-independent EmbeddedFileSystem distinction fully covered once on
# 3.7, and a second fs cell on another version would only re-smoke that
# firmware's identical fs surface. 2.7 stays because its command frame is
# genuinely different (ZephyrInlineRetcodeFrame, inline retcode); 4.4 stays as
# a newest-LTS firmware-drift sentinel; no-FS likewise needs only one backend
# (the transfer gate short-circuits before any frame). The 3.7 ids are
# unversioned for backwards compatibility (they predate the multi-version
# matrix and are referenced by name in the unit tree); 2.7 and 4.4 carry an
# explicit version token. This dict is the single source of truth for the
# embedded backend list — the integration test files import
# :data:`EMBEDDED_BACKENDS` rather than re-listing the ids, so a new row here
# flows into every parametrized contract suite without touching the test files.
_ZEPHYR_BACKEND_NE: dict[str, str] = {
    # Zephyr 3.7 LTS — primary version: full {FAT, LittleFS, no-FS}. ids kept
    # unversioned (predate the matrix; referenced by name in the unit tree).
    "zephyr_fat": "sprout",
    "zephyr_lfs": "sprout_lfs",
    "zephyr_no_fs": "sprout_no_fs",
    # Zephyr 2.7 LTS — distinct command frame (inline retcode); one fs cell.
    "zephyr_27_fat": "sprout27",
    # Zephyr 4.4 LTS — newest-LTS firmware-drift sentinel; one fs cell.
    "zephyr_44_lfs": "sprout44_lfs",
}

# Ordered list of embedded backend ids — imported by the integration contract
# suites so the parametrize lists stay in lockstep with the lab matrix.
EMBEDDED_BACKENDS: list[str] = list(_ZEPHYR_BACKEND_NE)


def embedded_param_id(backend_id: str) -> str:
    """Descriptive test id for an embedded backend, derived from lab data.

    Returns ``"{osName}-{osVersion}-{fs}"`` so a new entry in
    ``lab_data/tech1/hosts.json`` (e.g. a future Zephyr 4.x or a different
    RTOS) surfaces its identity in test output without test-code edits.
    Non-embedded backend ids pass through unchanged so the same helper can
    be used by parametrize callers that mix unix and embedded backends.
    """
    if backend_id not in _ZEPHYR_BACKEND_NE:
        return backend_id
    data = host_data(_ZEPHYR_BACKEND_NE[backend_id])
    osname = str(data.get("osName", "embedded"))
    osver = str(data.get("osVersion", ""))
    # Filesystem token from the declared `filesystem` variant — the source of
    # truth in lab data (``default_dest_dir`` is usually unset, defaulting to
    # the FS mount at construction time). Maps the lab string to a short tag.
    fs = {
        "fat-ram": "fat",
        "littlefs": "lfs",
        "none": "nofs",
    }.get(str(data.get("filesystem", "none")), str(data.get("filesystem")))
    parts = [p for p in (osname, osver, fs) if p]
    return "-".join(parts).lower().replace(" ", "")


@pytest_asyncio.fixture
async def host1(request):
    """Integration host parameterized by backend id.

    Accepted values:

    - ``"ssh"`` / ``"telnet"`` -> UnixHost on `carrot`, with the matching term.
    - ``"local"``              -> LocalHost.
    - any id in :data:`EMBEDDED_BACKENDS` -> EmbeddedHost on the matching
      Zephyr QEMU target, built via the storage factory from its lab-data
      entry. The matrix anchors the full {FAT-on-RAM, LittleFS, no-FS} set on
      3.7, with a single fs cell on 2.7 and 4.4; see :data:`_ZEPHYR_BACKEND_NE`
      for the id -> `ne` mapping and the trim rationale.
    """
    backend = request.param
    if backend == "local":
        h = LocalHost()
        yield h
        await h.close()
        return
    if backend in _ZEPHYR_BACKEND_NE:
        # Embedded backends round-trip through the factory so the same lab-data
        # entry tests target as `otto host` / `EmbeddedHost(...)` users do.
        data = host_data(_ZEPHYR_BACKEND_NE[backend])
        h = create_host_from_dict(data)
        yield h
        await h.close()
        return
    # Unix terms ("ssh" / "telnet").
    kwargs: dict[str, str] = {"term": backend}
    if backend == "telnet":
        kwargs["transfer"] = "ftp"
    h = make_host("carrot", **kwargs)
    yield h
    await h.close()


@pytest_asyncio.fixture
async def host2(request):
    """Integration host2, parameterized by term type ('ssh' or 'telnet')."""
    term = request.param
    kwargs: dict[str, str] = {"term": term}
    if term == "telnet":
        kwargs["transfer"] = "ftp"
    h = make_host("tomato", **kwargs)
    yield h
    await h.close()


@pytest_asyncio.fixture
async def host3(request):
    """Integration host3, parameterized by term type ('ssh' or 'telnet')."""
    term = request.param
    kwargs: dict[str, str] = {"term": term}
    if term == "ssh":
        kwargs["transfer"] = "scp"
    h = make_host("pepper", **kwargs)
    yield h
    await h.close()


@pytest_asyncio.fixture
async def hop_host(request):
    """Integration host reached through one or two SSH hops.

    Parameterized by ``(ne, hop_ne, term, transfer)`` tuples — e.g.
    ``("tomato", "carrot", "ssh", "scp")`` means "reach tomato through carrot".

    For two-hop chains, *hop_ne* is the first hop and the intermediate host
    must itself have a hop configured at fixture construction time.
    """
    ne, hop_ne, term, transfer = request.param
    target_data = host_data(ne)
    hop_data = host_data(hop_ne)
    hop_id = f"{hop_data['ne']}_{hop_data.get('board', 'seed')}"
    h = UnixHost(
        ip=target_data["ip"],
        ne=target_data["ne"],
        creds=target_data["creds"],
        board=target_data.get("board"),
        is_virtual=target_data.get("is_virtual", False),
        term=term,
        transfer=transfer,
        hop=hop_id,
    )
    yield h
    await h.close()


@pytest_asyncio.fixture
async def transfer_host(request):
    """Integration host, parameterized by transfer type ('scp', 'sftp', 'ftp', 'nc').

    Accepts either a plain transfer string (uses default ssh term) or a
    ``(transfer, term)`` tuple for explicit term control — e.g. ``('nc', 'telnet')``.
    """
    param = request.param
    if isinstance(param, tuple):
        transfer, term = param
        h = make_host("carrot", transfer=transfer, term=term)
    else:
        h = make_host("carrot", transfer=param)
    yield h
    await h.close()


# ---------------------------------------------------------------------------
# OS-agnostic host kits — backend-appropriate command strings for the
# parametrized contract suite. There is no command both Unix and Zephyr can
# run (Zephyr has no `echo` builtin), so the contract asserts on otto
# behavior while each backend's kit supplies the actual commands.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class HostKit:
    """Backend-appropriate command strings for OS-agnostic contract tests.

    A test using ``host1_kit`` should treat all fields as opaque shell
    fragments — no Unix-isms, no Zephyr-isms — and assert only on the otto
    behavior they trigger (status, retcode shape, output presence).
    """

    successful_cmd: str
    """A command that exits 0 with non-empty stdout."""

    failing_cmd: str
    """A command that produces a non-zero retcode (Status.Failed)."""

    temp_remote_dir: str | None
    """Writable directory on the target for get/put round-trips, or None
    when the target has no filesystem (graceful-degradation case)."""

    send_line_ending: str
    """Line ending the backend's shell accepts to commit a sent command.

    The Zephyr telnet shell takes ``\\r`` (the framing seam writes ``\\r``-
    separated lines); bash shells (Unix, local) take ``\\n``.
    """

    expect_in_output: str
    """A stable substring of ``successful_cmd``'s output that
    :meth:`Host.expect` can match against. Used by the send/expect
    contract case so the test stays OS-agnostic — the kit provides both
    the command and what to look for in its echo."""

    stability_iterations: int = 20
    """Number of sequential ``run`` iterations the cross-OS stability
    contract performs. Embedded backends keep this modest because the
    Zephyr telnet console is slow; unix backends can comfortably run
    higher counts."""

    stability_cycle_count: int = 10
    """Number of sequential put/get/verify/delete cycles. Set to ``0`` for
    backends without a filesystem so the cycle test self-skips."""

    stability_large_size: int = 0
    """Size in bytes for the large-file stability transfer. Embedded
    backends keep this orders of magnitude smaller than unix because the
    console transfer encodes 32 hex chars per shell invoke
    (see :mod:`otto.host.embedded_transfer`). Set to ``0`` to skip the
    large-file test on backends without a filesystem."""


_UNIX_KIT = HostKit(
    successful_cmd="echo hello",
    failing_cmd="ls /this_path_does_not_exist_otto_contract",
    temp_remote_dir="/tmp",
    send_line_ending="\n",
    expect_in_output="hello",
    stability_iterations=50,
    stability_cycle_count=20,
    stability_large_size=5 * 1024 * 1024,
)

# Zephyr has no echo builtin — pick a stock command that prints non-empty
# output and exits 0. `version` is universally available on the Zephyr
# shell and prints "Zephyr version X.Y.Z" — both the command name and
# "Zephyr" appear in the output, so either is a fine expect-fragment.
_ZEPHYR_COMMON = {
    "successful_cmd": "version",
    "failing_cmd": "bogus_otto_contract_cmd",
    "send_line_ending": "\r",
    "expect_in_output": "Zephyr",
}

# Embedded backends share these stability numbers — the slow per-invoke
# console encoding dominates wall time, so we keep iteration counts modest
# and large transfers in the tens of KiB rather than MiB.
_ZEPHYR_STABILITY = {
    "stability_iterations": 20,
    "stability_cycle_count": 10,
    "stability_large_size": 32 * 1024,
}

def _zephyr_kit(backend_id: str) -> HostKit:
    """Build the contract kit for a Zephyr backend from its lab data.

    ``temp_remote_dir`` is the on-device mount path, resolved from the host's
    declared ``filesystem`` variant via :func:`build_filesystem` — one source
    of truth for "where does this FS live on the device", shared with the
    production factory. A no-filesystem target (mount ``None``) self-skips the
    file-transfer stability cycles by zeroing their counts.

    Deriving the kit from lab data (rather than a hand-written table per
    backend) means a new Zephyr version added to :data:`_ZEPHYR_BACKEND_NE`
    and ``hosts.json`` gets a correct kit for free.
    """
    from otto.host.embedded_filesystem import build_filesystem

    data = host_data(_ZEPHYR_BACKEND_NE[backend_id])
    fs = build_filesystem(data.get("filesystem", "none"))
    if fs.mount is None:
        return HostKit(
            temp_remote_dir=None, **_ZEPHYR_COMMON,
            stability_iterations=20,
            stability_cycle_count=0,
            stability_large_size=0,
        )
    return HostKit(temp_remote_dir=fs.mount, **_ZEPHYR_COMMON, **_ZEPHYR_STABILITY)


_KITS: dict[str, HostKit] = {
    "ssh": _UNIX_KIT,
    "telnet": _UNIX_KIT,
    "local": _UNIX_KIT,
    **{b: _zephyr_kit(b) for b in EMBEDDED_BACKENDS},
}


@pytest.fixture
def host1_kit(request) -> HostKit:
    """Backend-appropriate command kit for the parametrized host1 fixture.

    Indirect-parametrize ``host1_kit`` alongside ``host1`` with the same
    backend id so the kit lines up with whichever host is built::

        @pytest.mark.parametrize(
            "host1, host1_kit",
            [(b, b) for b in ALL_BACKENDS],
            indirect=True,
        )
    """
    return _KITS[request.param]
