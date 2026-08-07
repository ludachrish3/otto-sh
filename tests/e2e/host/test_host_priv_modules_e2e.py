"""End-to-end CLI tests for ``otto host <vm> run --sudo`` elevation and
``otto host <vm> lsmod`` kernel-module listing driven through the real
``otto`` subprocess entry-point.

These tests require the live Vagrant bed (``vagrant up test1 test2 test3``).
On bed-unreachable they FAIL with a clear host-named error — they never skip.

Scope
-----
- ``run --sudo``: elevation to root via passwordless sudo on the Vagrant hosts.
  Verified: the beds use passwordless sudo (``sudo -S -p 'otto-sudo:'``);
  no password is echoed in stdout/stderr.
- ``lsmod``: read-only listing of loaded kernel modules from ``/proc/modules``.
  Vagrant Linux hosts always have loaded modules — the assertion is non-empty.
- ``load``/``unload``: DEFERRED.  Loading/unloading a real kernel module on the
  shared bed requires a ``.ko`` file to stage, then ``insmod``; that is invasive
  on a shared host and there is no safe always-loadable no-op module that does
  not require root-owned paths already on the host.  ``lsmod`` gives full
  coverage of the ``/proc/modules`` read path; the load/unload path is covered
  by the integration tests in ``tests/integration/host/``.

xdist group
-----------
All tests are pinned to ``host_priv_e2e`` to serialise subprocess-coverage
finalisation from a single worker.
"""

import re
import subprocess
from pathlib import Path

import pytest

from tests._fixtures._host_pool import UNIX_POOL as _UNIX_POOL
from tests._fixtures._host_pool import lease_unix_host
from tests.e2e._otto_subprocess import REPO1, assert_output_dir, output_dirs, run_otto

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Lab that contains carrot/tomato/pepper (tech1 lab data).
_LAB = "veggies"

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("host_priv_e2e")]


# ---------------------------------------------------------------------------
# Subprocess helper
# ---------------------------------------------------------------------------


def _run_otto(
    *args: str,
    sut_dirs: Path = REPO1,
    lab: str = _LAB,
    xdir: Path | None = None,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    """Run ``otto -R --lab <lab> <args>`` through the shared subprocess harness.

    The ``-R`` flag bypasses the reservation gate, which is appropriate for
    automated e2e tests that do not hold a named reservation.  Both are root
    options, so their relative order is immaterial.  Argv assembly and the
    120 s default timeout are this module's; the environment (subprocess
    coverage + the otto keys) comes from ``run_otto``.
    """
    return run_otto(
        list(args),
        xdir=xdir,
        sut_dirs=sut_dirs,
        lab=lab,
        extra_argv_prefix=["-R"],
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def unix_host(tmp_path_factory) -> str:  # type: ignore[type-arg]
    """Lease one Unix host from the pool; yield its seed id (e.g. ``carrot_seed``).

    Uses the same fd-flock lease mechanism as the transfer e2e tests so that
    concurrent workers never race on the same host.
    """
    lock_dir = tmp_path_factory.getbasetemp().parent
    with lease_unix_host(lock_dir, _UNIX_POOL) as element:
        yield f"{element}_seed"


# ---------------------------------------------------------------------------
# Test: run --sudo elevation
# ---------------------------------------------------------------------------


def test_host_run_sudo_elevates(unix_host: str, tmp_path: Path) -> None:
    """``otto host <vm> run --sudo "id"`` must exit 0 and show ``uid=0`` in
    the output — confirming that the Vagrant host grants passwordless sudo
    and that otto's ``--sudo`` flag actually elevates the command.

    Also asserts:
    - No password text is echoed into stdout or stderr (passwordless sudo;
      otto's expect-based ``otto-sudo:`` prompt handling suppresses the sudo
      challenge from the visible output).
    - No "password" or "passwd" appears in captured stdout/stderr (extra
      guard against credential leaks from a misconfigured host).
    """
    result = _run_otto("host", unix_host, "run", "--sudo", "id", xdir=tmp_path)
    assert result.returncode == 0, (
        f"``otto host {unix_host} run --sudo 'id'`` failed (exit {result.returncode}):\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )

    combined = result.stdout + result.stderr
    assert "uid=0" in combined, (
        f"Expected 'uid=0' in output of sudo'd 'id' on {unix_host!r}, got:\n{combined}"
    )

    # Passwordless sudo should NOT echo a password prompt or credential text.
    lower = combined.lower()
    assert "password" not in lower, (
        f"Unexpected 'password' text in output of 'run --sudo id' on {unix_host!r}:\n{combined}"
    )
    assert "passwd" not in lower, (
        f"Unexpected 'passwd' text in output of 'run --sudo id' on {unix_host!r}:\n{combined}"
    )
    assert_output_dir(tmp_path, "host")  # `host run` does real work — output dir created


# ---------------------------------------------------------------------------
# Test: lsmod lists kernel modules
# ---------------------------------------------------------------------------


def test_host_lsmod_lists_modules(unix_host: str, tmp_path: Path) -> None:
    """``otto host <vm> lsmod`` must exit 0 and return a non-empty list of
    kernel module names.

    The implementation reads ``/proc/modules`` (world-readable; no sudo
    needed) and returns the first-column names.  Any live Linux Vagrant host
    has at minimum the virtualisation modules loaded, so the list is always
    non-empty.  We also assert that at least one entry looks like a module
    name (alphanumeric with underscores — no whitespace, no brackets).
    """
    result = _run_otto("host", unix_host, "lsmod", xdir=tmp_path)
    assert result.returncode == 0, (
        f"``otto host {unix_host} lsmod`` failed (exit {result.returncode}):\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )

    combined = result.stdout + result.stderr
    # lsmod returns a Python list rendered by rich's rprint — the output
    # contains quoted module names separated by commas.  Assert at least one
    # token that looks like a kernel module name (letters/digits/underscores).
    # A non-empty list (rather than a specific name) keeps this robust across
    # kernel versions — any live Linux Vagrant host has modules loaded.
    module_names = re.findall(r"'([A-Za-z0-9_]+)'", combined)
    assert module_names, (
        f"Expected at least one module name in ``lsmod`` output on {unix_host!r}, got:\n{combined}"
    )
    # lsmod is a read-only query (output_dir=False) — it prints module names and
    # creates no per-invocation output dir (the reservation gate still runs).
    assert not output_dirs(tmp_path, "host"), "lsmod must not create an output dir"
