"""
API-level contract that every otto host backend must satisfy.

This is the *cross-OS* contract: tests assert on otto behavior — status,
retcode shape, output presence, file-transfer round-trip — never on
backend-specific command text. There is no command both Unix and Zephyr can
run (Zephyr has no ``echo`` builtin), so each backend's kit (``host1_kit``,
defined in :mod:`tests.conftest`) supplies backend-appropriate command
strings. The test treats those strings as opaque.

Parametrized over all backends:

- ``ssh`` / ``telnet`` / ``local`` — :class:`UnixHost` / :class:`LocalHost`.
- the Zephyr matrix in :data:`tests.conftest.EMBEDDED_BACKENDS` —
  :class:`EmbeddedHost` against the QEMU instances on the ``zephyr`` Vagrant
  VM ({2.7, 3.7, 4.4} x {FAT-on-RAM, LittleFS, no-FS}).

Unix-specific bash-isms (``cd`` / ``export`` / ``uname``) and the SSH
transfer-protocol matrix stay in :mod:`test_unix_host_integration`. The
Zephyr-specific live tests (``kernel uptime`` shape, single-console
caveat) live in :mod:`test_embedded_host_integration`.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

# The progress factory lives in (and is read by BaseFileTransfer's lazy import
# from) otto.host.transfer.progress; patch it there, not on the package re-export.
import otto.host.transfer.progress as transfer_mod
from otto.utils import Status
from tests.conftest import EMBEDDED_BACKENDS, remote_name

# Backend ids that carry the `embedded` marker (the Zephyr QEMU instances on
# the `zephyr` Vagrant VM). Single-sourced from :data:`tests.conftest` so the
# Zephyr version x filesystem matrix is defined in exactly one place.
_EMBEDDED_BACKENDS = set(EMBEDDED_BACKENDS)


def _backend_param(backend_id: str) -> pytest.param:
    """Wrap a backend id with the right markers.

    Two indirect fixtures (``host1`` + ``host1_kit``) get the same id, so the
    parametrize value is a 2-tuple.

    Every backend carries ``integration`` — including ``local``. The
    local-backend contract cases need otto's process-spawn machinery and a
    real filesystem (the round-trip uses ``tmp_path``); the original "no VM
    needed" objection that left ``local`` unmarked also had the unwanted
    side effect of deselecting it from every marker-filtered run. Pinning
    ``local`` under ``integration`` makes ``pytest -m "integration and not
    embedded"`` cover ssh + telnet + local — the full Unix matrix.

    Zephyr backends additionally carry ``embedded``.
    """
    marks = []
    if backend_id in _EMBEDDED_BACKENDS:
        marks.append(pytest.mark.embedded)
    return pytest.param(backend_id, backend_id, marks=marks)


_ALL_BACKENDS = pytest.mark.parametrize(
    ("host1", "host1_kit"),
    [_backend_param(b) for b in ("ssh", "telnet", "local", *EMBEDDED_BACKENDS)],
    indirect=True,
)


pytestmark = pytest.mark.timeout(45)


# ---------------------------------------------------------------------------
# run / oneshot
# ---------------------------------------------------------------------------


@_ALL_BACKENDS
class TestRunContract:
    @pytest.mark.asyncio
    async def test_successful_command_returns_status_success(self, host1, host1_kit):
        """A command that exits 0 must yield ``Status.Success`` with retcode
        0 and non-empty output. The *command text* differs per backend; the
        *otto behavior* does not."""
        result = (await host1.run(host1_kit.successful_cmd)).only
        assert result.status == Status.Success
        assert result.retcode == 0
        assert result.value != "", "successful command produced empty output"

    @pytest.mark.asyncio
    async def test_failing_command_returns_status_failed(self, host1, host1_kit):
        """A command that produces a non-zero retcode must yield
        ``Status.Failed``. The retcode value itself varies (Unix: 127 for
        unknown; Zephyr: -8 for unknown — signed errno), so the contract is
        on the Status mapping, not the integer."""
        result = (await host1.run(host1_kit.failing_cmd)).only
        assert result.status == Status.Failed
        assert result.retcode != 0

    @pytest.mark.asyncio
    async def test_oneshot_works_cold(self, host1, host1_kit):
        """``oneshot`` must succeed without a prior ``run`` warming the
        session. On UnixHost this exercises the stateless exec primitive;
        on EmbeddedHost it shares the persistent session (documented
        single-console caveat) but is still expected to work cold."""
        result = await host1.oneshot(host1_kit.successful_cmd)
        assert result.status == Status.Success
        assert result.retcode == 0

    @pytest.mark.asyncio
    async def test_send_expect_drives_raw_output(self, host1, host1_kit):
        """``send`` writes a raw command to the shell's stdin; ``expect``
        waits for a substring of the response. The contract is on otto's
        I/O surface — *what* is sent and *what fragment* to look for in
        the echo come from the kit so the test stays OS-agnostic."""
        await host1.send(host1_kit.successful_cmd + host1_kit.send_line_ending)
        matched = await host1.expect(host1_kit.expect_in_output, timeout=10.0)
        assert host1_kit.expect_in_output in matched


# ---------------------------------------------------------------------------
# File transfer: get / put round-trip (or graceful-degradation on no-FS)
# ---------------------------------------------------------------------------


@_ALL_BACKENDS
class TestTransferContract:
    @pytest.mark.asyncio
    async def test_put_get_roundtrip_byte_identical(
        self,
        host1,
        host1_kit,
        worker_id,
        tmp_path: Path,
    ):
        """``put`` then ``get`` must round-trip a small binary file
        byte-identically. Skipped for backends with no filesystem — they
        cover the graceful-degradation path in the next test."""
        if host1_kit.temp_remote_dir is None:
            pytest.skip("backend has no filesystem — see no-FS error test")

        payload = b"otto contract test payload\n\x00\x01\x02"
        name = remote_name(worker_id, "contract.bin")
        local_src = tmp_path / name
        local_src.write_bytes(payload)

        put_result = await host1.put(
            [local_src],
            Path(host1_kit.temp_remote_dir),
        )
        assert put_result.status == Status.Success, f"put failed: {put_result.msg}"

        get_dir = tmp_path / "received"
        get_dir.mkdir()
        remote_path = Path(host1_kit.temp_remote_dir) / name
        get_result = await host1.get([remote_path], get_dir)
        assert get_result.status == Status.Success, f"get failed: {get_result.msg}"

        assert (get_dir / name).read_bytes() == payload

    @pytest.mark.asyncio
    async def test_put_get_roundtrip_survives_back_to_back_calls(
        self,
        host1,
        host1_kit,
        worker_id,
        tmp_path: Path,
    ):
        """A second ``put → get`` against the same host must succeed. The
        first round-trip warms whatever per-host state exists (the
        embedded ``_ensure_mounted`` latch, the SSH multiplexer, the
        Zephyr telnet session); the second exercises the "hot host"
        branch of ``_ensure_session`` / ``_ensure_mounted``. Cross-backend
        coverage catches regressions where a hot host's second transfer
        regresses on one backend without affecting the others."""
        if host1_kit.temp_remote_dir is None:
            pytest.skip("backend has no filesystem — see no-FS error test")

        payload_a = b"otto contract test payload A\n\x00\x01"
        payload_b = b"otto contract test payload B\n\x02\x03"
        name_a = remote_name(worker_id, "a.bin")
        name_b = remote_name(worker_id, "b.bin")
        local_a = tmp_path / name_a
        local_a.write_bytes(payload_a)
        local_b = tmp_path / name_b
        local_b.write_bytes(payload_b)

        remote_dir = Path(host1_kit.temp_remote_dir)
        landing = tmp_path / "received"
        landing.mkdir()

        put_a_result = await host1.put([local_a], remote_dir)
        assert put_a_result.status == Status.Success, f"first put failed: {put_a_result.msg}"
        get_a_result = await host1.get(
            [remote_dir / name_a],
            landing,
        )
        assert get_a_result.status == Status.Success, f"first get failed: {get_a_result.msg}"
        assert (landing / name_a).read_bytes() == payload_a

        put_b_result = await host1.put([local_b], remote_dir)
        assert put_b_result.status == Status.Success, (
            f"second put on hot host failed: {put_b_result.msg}"
        )
        get_b_result = await host1.get(
            [remote_dir / name_b],
            landing,
        )
        assert get_b_result.status == Status.Success, (
            f"second get on hot host failed: {get_b_result.msg}"
        )
        assert (landing / name_b).read_bytes() == payload_b

    @pytest.mark.asyncio
    async def test_no_filesystem_backend_surfaces_clear_error(
        self,
        host1,
        host1_kit,
        tmp_path: Path,
    ):
        """On a backend whose target has no filesystem (e.g. a Zephyr build
        without ``CONFIG_FILE_SYSTEM_SHELL``), ``put`` / ``get`` must
        surface a clear error rather than hanging or producing garbage."""
        if host1_kit.temp_remote_dir is not None:
            pytest.skip("backend has a filesystem — see round-trip test")

        local_src = tmp_path / "ignored.bin"
        local_src.write_bytes(b"")

        # The contract: either a non-Success status or a raised exception is
        # acceptable. Silent Success is not. The put call and Result check
        # happen outside the except-guarded region so a shape error (e.g. an
        # unpack of a non-tuple Result) cannot be swallowed as an "acceptable"
        # exception — only a genuine raise from `put` itself may short-circuit.
        try:
            result = await host1.put(
                [local_src],
                Path("/nonexistent_otto_contract"),
            )
        except Exception:  # noqa: BLE001 — contract test: exception is one acceptable failure mode for no-FS backend
            return  # exception is one acceptable failure mode
        assert result.status != Status.Success, (
            f"no-FS backend reported Success for put — expected an error (err={result.msg!r})"
        )


# ---------------------------------------------------------------------------
# Progress contract: every backend must emit at least one completion event
# per src file. Enforced first at the type-system level (BaseFileTransfer's
# ``_run_put`` / ``_run_get`` are abstract — see
# tests/unit/host/test_transfer_progress.py) and again here at runtime so a
# backend that accepts the factory but forgets to invoke it still fails.
# ---------------------------------------------------------------------------


@_ALL_BACKENDS
class TestTransferProgressContract:
    @pytest.mark.asyncio
    async def test_put_emits_completion_event(
        self,
        host1,
        host1_kit,
        worker_id,
        tmp_path: Path,
    ):
        """Backend-agnostic contract: ``host.put(...)`` must invoke the
        per-file progress handler at least once with ``bytes_done ==
        bytes_total > 0``, signalling file completion. We patch
        :func:`make_rich_progress_factory` so the factory the backend
        consumes returns a spy handler — the handler's invocation history
        is what we assert on."""
        if host1_kit.temp_remote_dir is None:
            pytest.skip("backend has no filesystem — no progress to report")

        payload = b"otto progress contract\n\x00\x01\x02"
        local_src = tmp_path / remote_name(worker_id, "prog.bin")
        local_src.write_bytes(payload)

        # Every factory() call returns a fresh handler that appends into
        # the shared `events` list. Mirrors the per-file factory pattern
        # used by make_rich_progress_factory.
        events: list[tuple[str, str, int, int]] = []

        def spy_factory(progress, host_name):
            def factory():
                def handler(src, dst, done, total):
                    events.append((src, dst, done, total))

                return handler

            return factory

        with patch.object(
            transfer_mod,
            "make_rich_progress_factory",
            new=spy_factory,
        ):
            put_result = await host1.put(
                [local_src],
                Path(host1_kit.temp_remote_dir),
            )
        assert put_result.status == Status.Success, f"put failed: {put_result.msg}"
        assert events, (
            "backend produced no progress events — "
            "`_run_put` ignored the progress_factory parameter"
        )
        # At least one event marks file completion (done == total > 0).
        completions = [(d, t) for _, _, d, t in events if d == t > 0]
        assert completions, (
            f"backend never emitted a completion event (done == total > 0). Events: {events}"
        )

    @pytest.mark.asyncio
    async def test_get_emits_completion_event(
        self,
        host1,
        host1_kit,
        worker_id,
        tmp_path: Path,
    ):
        """Symmetric to the put case: ``get`` must report file completion
        through the progress handler. Round-trips via put so the source
        file exists on the remote side."""
        if host1_kit.temp_remote_dir is None:
            pytest.skip("backend has no filesystem — no progress to report")

        payload = b"otto progress contract get\n\x03\x04"
        name = remote_name(worker_id, "progget.bin")
        local_src = tmp_path / name
        local_src.write_bytes(payload)
        remote_dir = Path(host1_kit.temp_remote_dir)
        put_result = await host1.put([local_src], remote_dir)
        assert put_result.status == Status.Success, f"setup put failed: {put_result.msg}"

        landing = tmp_path / "got"
        landing.mkdir()
        events: list[tuple[str, str, int, int]] = []

        def spy_factory(progress, host_name):
            def factory():
                def handler(src, dst, done, total):
                    events.append((src, dst, done, total))

                return handler

            return factory

        with patch.object(
            transfer_mod,
            "make_rich_progress_factory",
            new=spy_factory,
        ):
            get_result = await host1.get(
                [remote_dir / name],
                landing,
            )
        assert get_result.status == Status.Success, f"get failed: {get_result.msg}"
        assert events, (
            "backend produced no progress events on get — "
            "`_run_get` ignored the progress_factory parameter"
        )
        completions = [(d, t) for _, _, d, t in events if d == t > 0]
        assert completions, f"backend never emitted a get completion event. Events: {events}"
