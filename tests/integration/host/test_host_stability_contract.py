"""
Cross-OS stability contract — every backend must survive a sustained
sequential workload on a single host instance.

Parametrized over the same backends as :mod:`test_host_contract`:

- ``ssh`` / ``telnet`` / ``local`` — :class:`UnixHost` / :class:`LocalHost`.
- the Zephyr matrix in :data:`tests.conftest.EMBEDDED_BACKENDS` —
  :class:`EmbeddedHost` against the QEMU instances on the ``zephyr`` Vagrant
  VM ({2.7, 3.7, 4.4} x {FAT-on-RAM, LittleFS, no-FS}).

Iteration counts and payload sizes come from each backend's ``HostKit``
(see :mod:`tests.conftest`) so the embedded backends — whose console
encodes 32 hex chars per shell invoke — can run smaller, slower workloads
than the unix backends without losing the same invariants.

Stability concerns covered:

1. **Sequential ``run`` iteration** — a long string of ``run`` calls on
   one host returns intact, command-appropriate output every time
   (catches console drift, prompt-framing rot, shell-state leakage).
2. **Sequential put/get/verify/delete cycles** — repeated transfer
   round-trips don't corrupt content or exhaust the partition. Validates
   the partial-transfer auto-cleanup landed alongside the Zephyr 100 MiB
   partition scale-up.
3. **Large-file transfer** — one transfer at the backend's stability-class
   size round-trips byte-identically (orders of magnitude smaller for
   embedded than unix — see kit sizing).

The existing concurrency-focused stability suite
(``tests/unit/host/test_session_stability_integration.py``) covers unix
SSH/telnet fan-out dynamics that don't generalize to embedded; this file
covers what *does* generalize, including future embedded OS / version /
filesystem variants that drop into ``lab_data/tech1/hosts.json``.
"""

from __future__ import annotations

import secrets
from pathlib import Path

import pytest

from otto.utils import Status

from tests.conftest import EMBEDDED_BACKENDS, embedded_param_id


# Backend ids that carry the `embedded` marker. Single-sourced from
# :data:`tests.conftest` so the Zephyr version x filesystem matrix lives in
# exactly one place.
_EMBEDDED_BACKENDS = set(EMBEDDED_BACKENDS)


def _backend_param(backend_id: str) -> pytest.param:
    """Mirror of ``test_host_contract._backend_param`` with a lab-derived id.

    Embedded backends get an id like ``zephyr-3.7-fat`` synthesized from
    ``hosts.json`` so a new RTOS / version / filesystem in lab data
    surfaces descriptively in test output without test-code edits.
    """
    marks = [pytest.mark.integration]
    if backend_id in _EMBEDDED_BACKENDS:
        marks.append(pytest.mark.embedded)
    return pytest.param(
        backend_id, backend_id, marks=marks, id=embedded_param_id(backend_id),
    )


_ALL_BACKENDS = pytest.mark.parametrize(
    "host1, host1_kit",
    [_backend_param(b) for b in ("ssh", "telnet", "local", *EMBEDDED_BACKENDS)],
    indirect=True,
)


# Embedded transfers via the console are slow — give the suite room to
# breathe. Unix iterations finish well inside this ceiling.
pytestmark = pytest.mark.timeout(600)


@_ALL_BACKENDS
class TestRunIterationStability:

    @pytest.mark.asyncio
    async def test_sequential_run_iterations_stay_intact(self, host1, host1_kit):
        """N sequential ``run`` calls must each return ``Status.Success``
        with intact output. Catches console drift / prompt-framing rot
        that surfaces only after many iterations on a reused session."""
        n = host1_kit.stability_iterations
        for i in range(n):
            result = (await host1.run(host1_kit.successful_cmd)).only
            assert result.status == Status.Success, (
                f"iteration {i}/{n} failed: {result}"
            )
            assert result.retcode == 0, (
                f"iteration {i}/{n} non-zero retcode: {result.retcode}"
            )
            assert result.output, (
                f"iteration {i}/{n} produced empty output"
            )
            assert host1_kit.expect_in_output in result.output, (
                f"iteration {i}/{n} mangled output: {result.output!r}"
            )


@_ALL_BACKENDS
class TestTransferCycleStability:

    @pytest.mark.asyncio
    async def test_put_get_delete_cycles_round_trip(
        self, host1, host1_kit, tmp_path: Path,
    ):
        """N put/get/verify/delete cycles must all succeed without
        partition exhaustion or content corruption. On embedded backends
        this exercises the partial-transfer auto-cleanup path —
        cumulative bytes across N iterations far exceed any single
        transfer."""
        if host1_kit.stability_cycle_count <= 0:
            pytest.skip("backend has no filesystem — cycle test skipped")

        n = host1_kit.stability_cycle_count
        remote_dir = Path(host1_kit.temp_remote_dir)
        landing = tmp_path / "landing"
        landing.mkdir()

        for i in range(n):
            payload = f"cycle_{i}_".encode() + secrets.token_bytes(64)
            name = f"cyc_{i}.bin"
            local_src = tmp_path / name
            local_src.write_bytes(payload)

            put_status, put_err = await host1.put([local_src], remote_dir)
            assert put_status == Status.Success, (
                f"cycle {i}/{n} put failed: {put_err}"
            )

            get_status, get_err = await host1.get(
                [remote_dir / name], landing,
            )
            assert get_status == Status.Success, (
                f"cycle {i}/{n} get failed: {get_err}"
            )

            got = (landing / name).read_bytes()
            assert got == payload, (
                f"cycle {i}/{n} content corrupt: "
                f"len(got)={len(got)} len(payload)={len(payload)}"
            )

            # Clean up between iterations so the partition usage stays
            # bounded across the N cycles. On embedded backends this is
            # part of the contract under test — the partition-bounded
            # promise is meaningless if `fs rm` silently no-ops — so we
            # assert the delete actually succeeded.
            #
            # Note on log noise: `EmbeddedFileTransfer.put_files` issues
            # its own pre-clean `fs rm <dest>` before each write. On LFS
            # that prints "Failed to remove ... (-2)" on the first write
            # to a path (the file genuinely doesn't exist yet); on FAT
            # the same `fs rm` is silent. Both are expected. The delete
            # asserted on below runs *after* a confirmed-good round-trip,
            # so the file definitely exists.
            local_src.unlink()
            (landing / name).unlink()
            del_status = await _delete_remote(host1, remote_dir / name)
            assert del_status == Status.Success, (
                f"cycle {i}/{n} post-roundtrip delete of {name} failed "
                f"(retcode={del_status}); partition cleanup is broken on "
                f"this backend"
            )


@_ALL_BACKENDS
class TestLargeTransferStability:

    @pytest.mark.asyncio
    async def test_large_file_round_trips_byte_identical(
        self, host1, host1_kit, tmp_path: Path,
    ):
        """One transfer at the backend's stability-class size must
        round-trip byte-identically. Embedded sizes are orders of
        magnitude smaller than unix because the console transfer is
        slow — see kit sizing."""
        if host1_kit.stability_large_size <= 0:
            pytest.skip("backend has no filesystem — large transfer skipped")

        size = host1_kit.stability_large_size
        payload = secrets.token_bytes(size)
        local_src = tmp_path / "stability_large.bin"
        local_src.write_bytes(payload)

        remote_dir = Path(host1_kit.temp_remote_dir)
        put_status, put_err = await host1.put([local_src], remote_dir)
        assert put_status == Status.Success, (
            f"large put ({size} bytes) failed: {put_err}"
        )

        landing = tmp_path / "landing"
        landing.mkdir()
        get_status, get_err = await host1.get(
            [remote_dir / "stability_large.bin"], landing,
        )
        assert get_status == Status.Success, (
            f"large get ({size} bytes) failed: {get_err}"
        )

        got = (landing / "stability_large.bin").read_bytes()
        assert got == payload, (
            f"large file ({size} bytes) corrupt after round-trip: "
            f"len(got)={len(got)}"
        )

        del_status = await _delete_remote(host1, remote_dir / "stability_large.bin")
        assert del_status == Status.Success, (
            f"large-file post-roundtrip delete failed (retcode={del_status})"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _delete_remote(host, path: Path) -> Status:
    """Backend-agnostic remote delete; returns the resulting ``Status``.

    Unix uses ``rm -f``; Zephyr's shell uses ``fs rm <path>`` (see
    :mod:`otto.host.embedded_transfer`). Callers should assert on the
    returned status — the partition-bounded promise of the cycle test
    only holds if these deletes actually succeed, and silently ignoring
    the result hides real regressions (e.g. a future ``fs rm`` shape
    change on a new RTOS).
    """
    from otto.host.embeddedHost import EmbeddedHost
    if isinstance(host, EmbeddedHost):
        result = (await host.run(f"fs rm {path}")).only
    else:
        result = (await host.run(f"rm -f {path}")).only
    return result.status
