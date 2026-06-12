"""
Zephyr-specific live integration tests for :class:`EmbeddedHost`.

The OS-agnostic contract (every backend must satisfy a basic ``run`` /
``oneshot`` / file-transfer shape) lives in :mod:`test_host_contract` and is
parametrized over Unix and Zephyr backends both. This file covers Zephyr
**implementation-detail** behavior the contract suite intentionally does
not assert on:

- **Signed errno retcodes**: Zephyr returns ``-8`` (``-ENOEXEC``) for an
  unknown command, distinct from Unix bash's ``127``. The shared contract
  only asserts ``Status.Failed``; this file confirms the integer travels.
- **Multi-line shell output stays clean** through :class:`ZephyrSession`'s
  positional parser and ANSI stripping (the prompt is colorized).
- **A Zephyr-stock command runs**: ``kernel uptime`` produces a bare
  integer of microseconds — a sanity check that builtins not exercised by
  the contract suite still work.

Parametrized over the full Zephyr matrix in
:data:`tests.conftest.EMBEDDED_BACKENDS` ({2.7, 3.7, 4.4} x {FAT-on-RAM,
LittleFS, no-FS}). Carries both ``integration`` and ``embedded`` markers so it
is opted into via ``pytest -m embedded``.
"""

import asyncio
import re
from pathlib import Path

import pytest

from otto.host.embeddedHost import EmbeddedHost
from otto.host.unixHost import UnixHost
from otto.storage.factory import create_host_from_dict
from otto.utils import Status
from tests.conftest import (
    _ZEPHYR_BACKEND_NE as _BACKEND_NE,
)
from tests.conftest import (
    EMBEDDED_BACKENDS,
    embedded_param_id,
    host_data,
    make_host,
)

_ALL_ZEPHYR = pytest.mark.parametrize(
    "host1",
    [
        pytest.param(
            backend,
            marks=[pytest.mark.integration, pytest.mark.embedded],
            id=embedded_param_id(backend),
        )
        for backend in EMBEDDED_BACKENDS
    ],
    indirect=True,
)


pytestmark = pytest.mark.timeout(30)


# ---------------------------------------------------------------------------
# Signed errno retcodes (Zephyr-isms)
# ---------------------------------------------------------------------------

@_ALL_ZEPHYR
class TestSignedRetcode:

    @pytest.mark.asyncio
    async def test_unknown_command_returns_negative_enoexec(self, host1):
        """The Zephyr shell sets ``retval`` to ``-8`` (``-ENOEXEC``) after an
        unknown command — the signed errno convention. Unix bash would use
        ``127`` here, so this distinguishes the framing path.
        """
        result = (await host1.run("definitely_not_a_zephyr_command")).only
        assert result.status == Status.Failed
        assert result.retcode == -8, (
            f"expected -ENOEXEC (-8) for unknown Zephyr command, "
            f"got {result.retcode}"
        )


# ---------------------------------------------------------------------------
# Multi-line output stays clean through the Zephyr frame's positional parser
# ---------------------------------------------------------------------------

@_ALL_ZEPHYR
class TestMultilineOutputClean:

    @pytest.mark.asyncio
    async def test_multiline_output_has_no_marker_or_prompt_noise(self, host1):
        """A command with several lines of output must parse cleanly.

        ``help`` is used because it is a stock shell builtin that produces
        multi-line output and exits 0 on **every** Zephyr LTS — unlike
        ``kernel threads``, whose subcommand name changed across versions
        (3.7 has ``threads``; 4.4 renamed the group to ``thread``), which
        would conflate a command-vocabulary difference with a parser bug.

        The positional parser must drop the bracketing prompt lines without
        leaking the BEGIN marker, the ``retval`` line, or any ANSI escapes
        into the captured output.
        """
        result = (await host1.run("help")).only
        assert result.status == Status.Success
        # No otto sentinels leaked into the output.
        assert "__OTTO_" not in result.output
        # No `retval` echo (the parser must take only the command's output).
        # `retval` itself as a substring could legitimately appear in other
        # contexts, so we only check for it on a line of its own.
        for line in result.output.splitlines():
            assert line.strip() != "retval", (
                f"retval line leaked into output: {result.output!r}"
            )
        # No raw ANSI escapes (the shell's colored prompt is stripped before
        # parsing).
        assert "\x1b[" not in result.output


# ---------------------------------------------------------------------------
# Stock Zephyr builtins
# ---------------------------------------------------------------------------

@_ALL_ZEPHYR
class TestStockBuiltins:

    @pytest.mark.asyncio
    async def test_kernel_uptime_yields_integer_microseconds(self, host1):
        """``kernel uptime`` prints a single bare integer (microseconds since
        boot) on Zephyr 3.7. A clean parse of that integer is a small
        sanity check that ``_parse_output`` and the framing seam agree on
        what counts as "the command's output" for a one-line command.
        """
        result = (await host1.run("kernel uptime")).only
        assert result.status == Status.Success
        # The output may include a unit-suffix label depending on the build;
        # we just need an integer somewhere in the first line.
        first_line = result.output.splitlines()[0] if result.output else ""
        assert re.search(r"\d+", first_line), (
            f"kernel uptime first line had no integer: {first_line!r}"
        )


# ---------------------------------------------------------------------------
# Single-console caveat
# ---------------------------------------------------------------------------

@_ALL_ZEPHYR
class TestSingleConsole:
    """An embedded target exposes a single shell console.
    :meth:`EmbeddedHost.open_session` documents that opening a second named
    session is not concurrency-safe — the Zephyr telnet backend
    (``CONFIG_SHELL_BACKEND_TELNET``) accepts only one client at a time.
    These tests pin down the observed behavior so a silent regression — a
    second connection being accepted and working, or worse, succeeding but
    quietly clobbering the first — is caught.
    """

    @pytest.mark.asyncio
    async def test_second_concurrent_session_is_not_silent(self, host1):
        """Opening a second named session while the default session is live
        must not silently succeed.

        An embedded target exposes a single shell console, so a second
        session has nowhere safe to go. :meth:`SessionManager.open_session`
        builds a *fresh* ``TelnetClient`` and opens a **second telnet
        connection** to the target — it does not reuse the default session's
        client. That second connection cannot succeed, and how it fails is
        environment-dependent:

        - On a directly-reachable single-client backend, the device refuses
          or immediately closes the extra connection
          (``ConnectionError``/``IncompleteReadError``), or the readiness
          handshake gets no shell and the bounded ``wait_for`` cancels it.
        - On a hop-tunneled bed (our embedded CI), the named-session path
          does not replicate the SSH port-forward that the default path
          (``ConnectionManager.telnet``) sets up, so it dials the raw device
          IP — which the runner can only route through the hop. The connect
          is rejected at the network layer with a *non-deterministic* errno:
          ``ECONNREFUSED``, a SYN timeout, or ``ENETUNREACH``/``EHOSTUNREACH``
          (the last two are plain ``OSError``, *not* ``ConnectionError``).

        All of these are acceptable. The property this guards is simply that
        the second ``open_session`` **raises** rather than returning a working
        session that quietly shares state with the first — hence the broad
        ``OSError`` (plus ``asyncio`` cancellation/timeout) below. Narrowing
        it to ``ConnectionError`` makes the test flaky whenever the rejection
        arrives as ``ENETUNREACH``.
        """
        # Warm the default session so it holds the device's one telnet slot.
        warmup = (await host1.run("kernel version")).only
        assert warmup.status == Status.Success

        # Bounded wait_for so a "hangs forever" regression fails the test
        # rather than the CI job. 5 s is comfortably longer than a real
        # connection-failure path (~ms) and short enough to keep the
        # suite fast.
        with pytest.raises((
            asyncio.TimeoutError,
            asyncio.CancelledError,
            OSError,  # parent of ConnectionError; also covers ENETUNREACH/EHOSTUNREACH
            asyncio.IncompleteReadError,
        )):
            await asyncio.wait_for(host1.open_session("aux"), timeout=5.0)

    @pytest.mark.asyncio
    async def test_default_session_survives_second_open_attempt(self, host1):
        """The critical safety property: after a rejected/hung second-
        session attempt, the **default** session must still be usable.
        Otherwise a user catching the second-open exception would be left
        with a host they can't drive any further.
        """
        # Establish the default session.
        before = (await host1.run("kernel version")).only
        assert before.status == Status.Success

        # Best-effort second open — we expect failure (see above test for
        # the exhaustive list of acceptable failure modes); the assertion
        # is on what comes after, not on the exception type.
        with pytest.raises(BaseException):
            await asyncio.wait_for(host1.open_session("aux"), timeout=5.0)

        # Default session must still work.
        after = (await host1.run("kernel uptime")).only
        assert after.status == Status.Success, (
            f"default session broke after second-open attempt: {after.output!r}"
        )


@pytest.mark.integration
@pytest.mark.embedded
@pytest.mark.asyncio
@pytest.mark.xdist_group("zephyr_fat")
async def test_concurrent_clients_to_one_console_contend_and_recover():
    """Two *separate* telnet clients to one device contend cleanly and the
    console recovers — regression guard for the single-telnet-client wedge.

    Unlike :meth:`TestSingleConsole.test_second_concurrent_session_is_not_silent`
    (which opens a second *named session* on one host — that reuses the cached
    ``TelnetClient``), this opens two independent ``EmbeddedHost``s to the same
    backend, i.e. two real telnet connections. That is what two xdist workers —
    or a fan-out test overlapping a per-backend test — produced before the
    ``zephyr_bed`` serialization (see the conftest): the Zephyr
    ``shell_telnet`` backend serves one client at a time, logs ``Telnet client
    already connected`` for the second, and the loser's readiness handshake
    gets no shell → ``shell never became ready``. The bug was that this could
    cascade and leave the console wedged for later connects.

    This reproduces the contention *within a single test* (so it is immune to
    the cross-worker serialization that prevents it in the suite at large) and
    pins the safe invariants:

    1. **At most one client wins** — the second never silently shares the one
       console (the dangerous regression).
    2. **The loser fails bounded**, not hung — a bare ``ConnectionError`` /
       ``TimeoutError`` from the init ceiling; the module ``timeout(30)`` marker
       fails the test if a regression turns it into an unbounded stall.
    3. **The console recovers** — a fresh connection works once the contention
       clears, so the single client slot is released, not left wedged.

    One representative backend (3.7 FAT) suffices: the single-client constraint
    lives in ``CONFIG_SHELL_BACKEND_TELNET`` and is identical across the matrix,
    so this keeps the cost to a single ~15s loser timeout rather than one per
    backend.
    """
    data = host_data(_BACKEND_NE["zephyr_fat"])
    host_a = create_host_from_dict(data)
    host_b = create_host_from_dict(data)
    try:
        results = await asyncio.gather(
            host_a.oneshot("kernel uptime"),
            host_b.oneshot("kernel uptime"),
            return_exceptions=True,
        )
    finally:
        await asyncio.gather(host_a.close(), host_b.close(), return_exceptions=True)

    winners = [r for r in results if not isinstance(r, BaseException)]
    losers = [r for r in results if isinstance(r, BaseException)]

    # (1) The single console must never serve two clients at once.
    assert len(winners) <= 1, (
        f"both telnet clients won against a single-console device: {results!r}"
    )
    # (2) Any loser failed within the bounded init ceiling (a true hang would
    #     trip the module-level timeout(30) marker before reaching here).
    for loser in losers:
        assert isinstance(loser, (ConnectionError, asyncio.TimeoutError)), (
            f"unexpected loser exception type: {loser!r}"
        )

    # (3) The console is usable again once the contention clears — the slot was
    #     released, not left wedged.
    host_c = create_host_from_dict(data)
    try:
        recovered = (await host_c.oneshot("kernel uptime"))
    finally:
        await host_c.close()
    assert recovered.status == Status.Success, (
        f"console left wedged after contention: {recovered.output!r}"
    )


# ---------------------------------------------------------------------------
# Concurrent multi-target file transfer (regression guard for the
# `otto -l embedded run test-instruction` failure: every Zephyr target's
# put() collapsed with `IncompleteReadError` on the readiness handshake
# while basil's concurrent SCP transfer ran over the shared `basil_seed`
# hop. The contract suite only exercises put/get per-backend in isolation,
# so the multi-target fan-out path that `do_for_all_hosts` takes had no
# coverage. These tests reproduce the fan-out semantics directly via
# `asyncio.gather` — that is what `do_for_all_hosts` does internally —
# without relying on the lab/configmodule layer.
# ---------------------------------------------------------------------------

# Per-target writable filesystem mount, keyed by lab `ne` name and derived
# from each host's declared `filesystem` variant (the FS class's mount path,
# the same source of truth the kits use). Covers the full Zephyr matrix in
# `tests.conftest.EMBEDDED_BACKENDS`; the no-FS targets map to `None` and are
# expected to surface a graceful Status.Error rather than succeed.
def _zephyr_dest_map() -> dict[str, str | None]:
    from otto.host.embedded_filesystem import build_filesystem

    dest: dict[str, str | None] = {}
    for backend in EMBEDDED_BACKENDS:
        data = host_data(_BACKEND_NE[backend])
        dest[data["ne"]] = build_filesystem(data.get("filesystem", "none")).mount
    return dest


_ZEPHYR_DEST: dict[str, str | None] = _zephyr_dest_map()


@pytest.mark.integration
@pytest.mark.embedded
@pytest.mark.xdist_group("zephyr_fanout")
class TestConcurrentEmbeddedTransfer:
    # This fan-out class opens one telnet client per device across *all*
    # devices at once, so its tests must run together on a single worker —
    # hence the explicit ``zephyr_fanout`` group (the conftest leaves it as-is
    # rather than reassigning a per-backend group). Residual known gap: this
    # group can still land on a different worker than the per-backend device
    # groups, so a fan-out test may briefly overlap a per-backend test on the
    # same device's single console. See the conftest grouping note.
    """Fan-out file transfer across multiple Zephyr targets sharing one
    SSH hop. Reproduces the failure mode hit by ``test_instruction`` in
    ``tests/repo1/pylib/repo1_instructions/install.py``: every Zephyr
    host's session handshake collapsed with ``IncompleteReadError(0 bytes)``
    when the puts were launched concurrently, leaving every embedded
    transfer dead while basil's SCP put succeeded.

    The bug is a session-init race, not a payload-size issue, so these
    tests use a small payload to keep the suite fast.
    """

    _PAYLOAD = b"otto concurrent transfer test\n\x00\x01\x02\x03"
    # Payload filename must fit FAT 8.3 — the Zephyr v3.7 FAT-on-RAM build
    # under test (``v3_7_fat_ram``) does not enable ``CONFIG_FS_FATFS_LFN``,
    # so any name longer than 8.3 (e.g. "concurrent.bin") opens with -ENOENT.
    _PAYLOAD_NAME = "fanout.bin"

    @staticmethod
    def _build_zephyr_hosts() -> list[EmbeddedHost]:
        """Fresh EmbeddedHost per Zephyr target, built the same way the
        production factory does (``create_host_from_dict``). Each test gets
        its own instances so a previous test's session state cannot leak.
        """
        return [
            create_host_from_dict(host_data(ne)) for ne in _ZEPHYR_DEST
        ]

    @staticmethod
    def _check_put_result(host_id: str, result) -> None:
        """Assert a single per-host put() result matches the contract:
        success for fs-capable Zephyr targets, graceful Status.Error for
        no-filesystem targets. A raised exception is a regression — this is
        the exact shape that the test_instruction failure produced.

        The fs-vs-no-fs distinction is derived from ``_ZEPHYR_DEST`` (mount is
        ``None`` for a no-FS target), so the no-FS backend in the matrix —
        ``sprout_no_fs`` — is checked the same way without hard-coding ne names.
        """
        assert not isinstance(result, BaseException), (
            f"{host_id}: put raised — concurrent session init regressed. "
            f"Exception: {result!r}"
        )
        status, err = result
        if _ZEPHYR_DEST[host_id] is None:
            assert status != Status.Success, (
                f"{host_id}: no-FS target reported Success — expected a "
                f"graceful error (err={err!r})"
            )
        else:
            assert status == Status.Success, (
                f"{host_id}: put failed: {err!r}"
            )

    @pytest.mark.asyncio
    @pytest.mark.timeout(120)
    async def test_concurrent_puts_across_zephyr_targets(
        self, tmp_path: Path,
    ):
        """Every Zephyr target in the matrix receives a put() concurrently.
        They share ``hop=basil_seed`` — all the telnet-over-SSH legs open into
        basil at the same instant. This is the reproducer for the
        readiness-handshake collapse, now stressed across the full 2.7/3.7/4.4
        matrix rather than a single LTS.
        """
        src = tmp_path / "fanout.bin"
        src.write_bytes(self._PAYLOAD)

        hosts = self._build_zephyr_hosts()
        try:
            results = await asyncio.gather(
                *(
                    h.put([src], Path(_ZEPHYR_DEST[h.ne] or "/"))
                    for h in hosts
                ),
                return_exceptions=True,
            )
            for h, result in zip(hosts, results):
                self._check_put_result(h.ne, result)
        finally:
            await asyncio.gather(
                *(h.close() for h in hosts), return_exceptions=True,
            )

    @pytest.mark.asyncio
    @pytest.mark.timeout(180)
    async def test_concurrent_puts_with_unix_scp_on_shared_hop(
        self, tmp_path: Path,
    ):
        """The exact fan-out shape that ``test_instruction`` triggers: a
        Unix host (``basil``) and every Zephyr target receive a put
        concurrently. basil's SCP transfer flows over the same VM that
        proxies the Zephyr telnet legs, exercising the hop under load while
        the Zephyr sessions are still mid-handshake.
        """
        src = tmp_path / "fanout.bin"
        src.write_bytes(self._PAYLOAD)

        basil = make_host("basil", term="ssh", transfer="scp")
        zephyrs = self._build_zephyr_hosts()
        try:
            results = await asyncio.gather(
                basil.put([src], Path("/tmp")),
                *(
                    h.put([src], Path(_ZEPHYR_DEST[h.ne] or "/"))
                    for h in zephyrs
                ),
                return_exceptions=True,
            )
            basil_result, *zephyr_results = results

            assert not isinstance(basil_result, BaseException), (
                f"basil: SCP put raised: {basil_result!r}"
            )
            basil_status, basil_err = basil_result
            assert basil_status == Status.Success, (
                f"basil: SCP put failed: {basil_err!r}"
            )

            for h, result in zip(zephyrs, zephyr_results):
                self._check_put_result(h.ne, result)
        finally:
            await asyncio.gather(
                basil.close(),
                *(h.close() for h in zephyrs),
                return_exceptions=True,
            )

    @pytest.mark.asyncio
    @pytest.mark.timeout(180)
    async def test_concurrent_gets_across_zephyr_targets(
        self, tmp_path: Path,
    ):
        """Symmetric fan-out for get(): pre-stage the payload sequentially
        on each fs-capable target, then drive a concurrent get(). The
        sequential put phase intentionally avoids the fan-out race so a
        get-side regression is not masked by a put-side failure.
        """
        src = tmp_path / "fanout.bin"
        src.write_bytes(self._PAYLOAD)

        hosts = self._build_zephyr_hosts()
        try:
            # Sequential pre-stage on the two fs-capable backends.
            for h in hosts:
                dest = _ZEPHYR_DEST[h.ne]
                if dest is None:
                    continue
                status, err = await h.put([src], Path(dest))
                assert status == Status.Success, (
                    f"{h.ne}: pre-stage put failed: {err!r}"
                )

            # Per-host local landing dir so concurrent gets don't collide
            # on the same destination file.
            fs_hosts = [h for h in hosts if _ZEPHYR_DEST[h.ne] is not None]
            for h in fs_hosts:
                (tmp_path / f"got_{h.ne}").mkdir()
            results = await asyncio.gather(
                *(
                    h.get(
                        [Path(_ZEPHYR_DEST[h.ne]) / "fanout.bin"],
                        tmp_path / f"got_{h.ne}",
                    )
                    for h in fs_hosts
                ),
                return_exceptions=True,
            )

            for h, result in zip(fs_hosts, results):
                landing = tmp_path / f"got_{h.ne}"
                assert not isinstance(result, BaseException), (
                    f"{h.ne}: get raised: {result!r}"
                )
                status, err = result
                assert status == Status.Success, (
                    f"{h.ne}: get failed: {err!r}"
                )
                assert (landing / "fanout.bin").read_bytes() == self._PAYLOAD
        finally:
            await asyncio.gather(
                *(h.close() for h in hosts), return_exceptions=True,
            )
