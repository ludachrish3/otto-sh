"""
API-level contract that every otto host backend must satisfy.

This is the *cross-OS* contract: tests assert on otto behavior — status,
retcode shape, output presence, file-transfer round-trip — never on
backend-specific command text. There is no command both Unix and Zephyr can
run (Zephyr has no ``echo`` builtin), so each backend's kit (``host1_kit``,
defined in :mod:`tests.conftest`) supplies backend-appropriate command
strings. The test treats those strings as opaque.

Parametrized over all six backends:

- ``ssh`` / ``telnet`` / ``local`` — :class:`UnixHost` / :class:`LocalHost`.
- ``zephyr_fat`` / ``zephyr_lfs`` / ``zephyr_no_fs`` — :class:`EmbeddedHost`
  against the three QEMU instances on the ``zephyr`` Vagrant VM.

Unix-specific bash-isms (``cd`` / ``export`` / ``uname``) and the SSH
transfer-protocol matrix stay in :mod:`test_unix_host_integration`. The
Zephyr-specific live tests (``kernel uptime`` shape, single-console
caveat) live in :mod:`test_embedded_host_integration`.
"""

from pathlib import Path

import pytest

from otto.utils import Status


# Backend ids that need a live VM (everything except `local`, which runs
# inside the dev VM itself).
_VM_BACKENDS = {"ssh", "telnet", "zephyr_fat", "zephyr_lfs", "zephyr_no_fs"}

# Backend ids that need the zephyr QEMU instances specifically (carry both
# the `integration` and `embedded` markers via pytest.param below).
_EMBEDDED_BACKENDS = {"zephyr_fat", "zephyr_lfs", "zephyr_no_fs"}


def _backend_param(backend_id: str) -> pytest.param:
    """Wrap a backend id with the right markers.

    Two indirect fixtures (``host1`` + ``host1_kit``) get the same id, so the
    parametrize value is a 2-tuple. Marks are derived from the id:

    - VM-requiring backends carry ``integration``.
    - Zephyr backends additionally carry ``embedded``.
    """
    marks = []
    if backend_id in _VM_BACKENDS:
        marks.append(pytest.mark.integration)
    if backend_id in _EMBEDDED_BACKENDS:
        marks.append(pytest.mark.embedded)
    return pytest.param(backend_id, backend_id, marks=marks)


_ALL_BACKENDS = pytest.mark.parametrize(
    "host1, host1_kit",
    [
        _backend_param("ssh"),
        _backend_param("telnet"),
        _backend_param("local"),
        _backend_param("zephyr_fat"),
        _backend_param("zephyr_lfs"),
        _backend_param("zephyr_no_fs"),
    ],
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
        assert result.output != "", "successful command produced empty output"

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


# ---------------------------------------------------------------------------
# File transfer: get / put round-trip (or graceful-degradation on no-FS)
# ---------------------------------------------------------------------------

@_ALL_BACKENDS
class TestTransferContract:

    @pytest.mark.asyncio
    async def test_put_get_roundtrip_byte_identical(
        self, host1, host1_kit, tmp_path: Path,
    ):
        """``put`` then ``get`` must round-trip a small binary file
        byte-identically. Skipped for backends with no filesystem — they
        cover the graceful-degradation path in the next test."""
        if host1_kit.temp_remote_dir is None:
            pytest.skip("backend has no filesystem — see no-FS error test")

        payload = b"otto contract test payload\n\x00\x01\x02"
        local_src = tmp_path / "contract.bin"
        local_src.write_bytes(payload)

        put_status, put_err = await host1.put(
            [local_src], Path(host1_kit.temp_remote_dir),
        )
        assert put_status == Status.Success, f"put failed: {put_err}"

        get_dir = tmp_path / "received"
        get_dir.mkdir()
        remote_path = Path(host1_kit.temp_remote_dir) / "contract.bin"
        get_status, get_err = await host1.get([remote_path], get_dir)
        assert get_status == Status.Success, f"get failed: {get_err}"

        assert (get_dir / "contract.bin").read_bytes() == payload

    @pytest.mark.asyncio
    async def test_no_filesystem_backend_surfaces_clear_error(
        self, host1, host1_kit, tmp_path: Path,
    ):
        """On a backend whose target has no filesystem (e.g. a Zephyr build
        without ``CONFIG_FILE_SYSTEM_SHELL``), ``put`` / ``get`` must
        surface a clear error rather than hanging or producing garbage."""
        if host1_kit.temp_remote_dir is not None:
            pytest.skip("backend has a filesystem — see round-trip test")

        local_src = tmp_path / "ignored.bin"
        local_src.write_bytes(b"")

        # The contract: either a non-Success status or a raised exception is
        # acceptable. Silent Success is not.
        try:
            status, err = await host1.put(
                [local_src], Path("/nonexistent_otto_contract"),
            )
        except Exception:
            return  # exception is one acceptable failure mode
        assert status != Status.Success, (
            f"no-FS backend reported Success for put — expected an error "
            f"(err={err!r})"
        )
