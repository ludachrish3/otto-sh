"""End-to-end CLI tests for ``otto host <vm> put/get`` (file transfer) and
``otto host <target> --hop <hop> run`` (multi-hop execution) driven through the
real ``otto`` subprocess entry-point.

These tests require the live Vagrant bed (``vagrant up test1 test2 test3``).
On bed-unreachable they FAIL with a clear host-named error — they never skip.

Topology
--------
- carrot_seed (test1, 10.10.200.11) — used as SSH hop
- tomato_seed (test2, 10.10.200.12) — used as transfer target AND hop target

The single-hop topology (carrot → tomato) needs only 2 VMs, so the hop test
carries only ``integration`` (not ``hops``, which would imply 3 VMs).

xdist group
-----------
All tests in this module are pinned to ``host_transfer_e2e`` so that
subprocess-coverage finalisation from a single worker rather than racing
across multiple workers.
"""

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from tests._fixtures._host_pool import UNIX_POOL as _UNIX_POOL
from tests._fixtures._host_pool import lease_unix_host
from tests.e2e._otto_subprocess import assert_output_dir

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parents[3]
REPO1 = PROJECT_ROOT / "tests" / "repo1"
OTTO_BIN = Path(sys.executable).parent / "otto"
COVERAGERC = PROJECT_ROOT / ".coveragerc"
COVERAGE_BOOTSTRAP = PROJECT_ROOT / "tests" / "_coverage_bootstrap"

# Lab that contains carrot/tomato/pepper (tech1 lab data).
_LAB = "veggies"

# Fixed hop topology (mirrors tests/integration/host/test_hop_integration.py):
#   otto → carrot_seed (hop) → tomato_seed (target)
# Only 2 VMs needed — NO "hops" marker.
_HOP_HOST = "carrot_seed"
_HOP_TARGET = "tomato_seed"

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("host_transfer_e2e")]


# ---------------------------------------------------------------------------
# Subprocess helper
# ---------------------------------------------------------------------------


def _run_otto(
    *args: str,
    sut_dirs: str = str(REPO1),
    lab: str = _LAB,
    xdir: Path | None = None,
    timeout: int = 180,
) -> subprocess.CompletedProcess[str]:
    """Run ``otto --lab <lab> -R <args>`` as a subprocess.

    The ``-R`` flag bypasses the reservation gate, which is appropriate for
    automated e2e tests that do not hold a named reservation.  Subprocess
    coverage is wired via ``COVERAGE_PROCESS_START``.
    """
    env: dict[str, str] = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "OTTO_SUT_DIRS": sut_dirs,
        "COVERAGE_PROCESS_START": str(COVERAGERC),
        "PYTHONPATH": os.pathsep.join(
            [str(COVERAGE_BOOTSTRAP), os.environ.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep),
    }
    if xdir is not None:
        env["OTTO_XDIR"] = str(xdir)

    full_argv = [str(OTTO_BIN), "--lab", lab, "-R", *args]
    return subprocess.run(
        full_argv,
        env=env,
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        timeout=timeout,
        check=False,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def unix_host(tmp_path_factory) -> str:  # type: ignore[type-arg]
    """Lease one Unix host from the pool; yield its seed id (e.g. ``carrot_seed``).

    Uses the same fd-flock lease mechanism as the docker e2e tests so that
    concurrent workers never race on the same host.
    """
    lock_dir = tmp_path_factory.getbasetemp().parent
    with lease_unix_host(lock_dir, _UNIX_POOL) as element:
        yield f"{element}_seed"


# ---------------------------------------------------------------------------
# Test: put / get round-trip
# ---------------------------------------------------------------------------


def test_host_put_get_roundtrip(unix_host: str, tmp_path: Path) -> None:
    """``otto host <vm> put <file> <remote_dir>`` then ``otto host <vm> get`` must
    round-trip the file byte-for-byte through the real SSH/SCP transfer path.

    A unique remote path (``/tmp/otto_e2e_<uuid>/``) prevents collisions with
    concurrent test runs on the same host.  The remote directory is cleaned up
    best-effort after the get step.
    """
    # Unique remote staging dir to avoid collisions with concurrent runs.
    remote_dir = f"/tmp/otto_e2e_{uuid.uuid4().hex}"
    filename = "transfer_payload.bin"
    payload = b"otto-e2e-transfer-" + b"\xab\xcd" * 64

    # --- Create source file ---
    src = tmp_path / filename
    src.write_bytes(payload)

    try:
        # --- Create remote staging directory ---
        mkdir = _run_otto("host", unix_host, "run", f"mkdir -p {remote_dir}", xdir=tmp_path)
        assert mkdir.returncode == 0, (
            f"Failed to create remote staging dir {remote_dir!r} on {unix_host!r}:\n"
            f"stdout: {mkdir.stdout}\nstderr: {mkdir.stderr}"
        )

        # --- put: local → remote ---
        put = _run_otto("host", unix_host, "put", str(src), remote_dir, xdir=tmp_path)
        assert put.returncode == 0, (
            f"``otto host {unix_host} put`` failed:\nstdout: {put.stdout}\nstderr: {put.stderr}"
        )
        assert "Transfer complete" in put.stdout, (
            f"Expected 'Transfer complete' in put output, got:\n{put.stdout}"
        )

        # --- get: remote → local ---
        back_dir = tmp_path / "back"
        back_dir.mkdir()
        get = _run_otto(
            "host", unix_host, "get", f"{remote_dir}/{filename}", str(back_dir), xdir=tmp_path
        )
        assert get.returncode == 0, (
            f"``otto host {unix_host} get`` failed:\nstdout: {get.stdout}\nstderr: {get.stderr}"
        )
        assert "Download complete" in get.stdout, (
            f"Expected 'Download complete' in get output, got:\n{get.stdout}"
        )

        # --- byte-identical roundtrip assertion ---
        back_file = back_dir / filename
        assert back_file.exists(), f"Expected {back_file} after get"
        assert back_file.read_bytes() == payload, "Round-tripped file content differs from original"
        # put/get are host verbs doing real transfers — output dirs created.
        assert_output_dir(tmp_path, "host")
    finally:
        # --- best-effort cleanup of remote staging dir (always runs) ---
        _run_otto("host", unix_host, "run", f"rm -rf {remote_dir}", xdir=tmp_path)


# ---------------------------------------------------------------------------
# Test: --hop run (single-hop SSH: otto → carrot → tomato)
# ---------------------------------------------------------------------------


def test_host_hop_run(tmp_path: Path) -> None:
    """``otto host <target> --hop <hop> run "echo <token>"`` must execute the
    command on the TARGET (tomato) via the SSH hop (carrot) and include the
    echo token in the output.

    Topology: otto dev VM → carrot_seed (hop) → tomato_seed (target).
    This path requires only 2 VMs — the hops marker (3-VM requirement) is NOT
    applied.  The topology is the same single-hop SSH chain proven by
    ``tests/integration/host/test_hop_integration.py::TestSingleHopSsh``.
    """
    token = f"via-hops-{uuid.uuid4().hex[:8]}"
    # Note: --hop must precede the HOST_ID positional argument (Typer/Click option ordering).
    result = _run_otto(
        "host",
        "--hop",
        _HOP_HOST,
        _HOP_TARGET,
        "run",
        f"echo {token}",
        xdir=tmp_path,
    )
    assert result.returncode == 0, (
        f"``otto host --hop {_HOP_HOST} {_HOP_TARGET} run 'echo {token}'`` failed:\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert token in result.stdout, f"Expected echo token {token!r} in output, got:\n{result.stdout}"
    assert_output_dir(tmp_path, "host")  # a multi-hop run does real work — output dir created
