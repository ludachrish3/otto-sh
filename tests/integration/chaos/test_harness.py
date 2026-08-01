"""Certification of the tier-2 harness: the full chain works untouched.

If these fail, every scenario test in this package is meaningless — fix
here first. Sequential (xdist_group) like the whole chaos suite.
"""

from pathlib import Path

import pytest

from . import _sshd
from ._driver import spawn_otto
from ._target import ChaosTarget, probe

pytestmark = [pytest.mark.xdist_group("chaos"), pytest.mark.timeout(120)]


def test_probe_round_trips(chaos_target: ChaosTarget) -> None:
    status, out = probe(chaos_target, "echo chaos-probe-ok")
    assert status == 0
    assert "chaos-probe-ok" in out


def test_untouched_run_completes_cleanly(chaos_target: ChaosTarget, tmp_path: Path) -> None:
    p = spawn_otto(
        ["host", chaos_target.host_id, "run", "true"], xdir=tmp_path, target=chaos_target
    )
    rc = p.wait(timeout=90)
    assert rc == 0, f"stderr:\n{p.stderr_text()}\nstdout:\n{p.stdout_text()}"
    # The file sinks landed and recorded the command-start marker.
    p.wait_for_log(r"\| true", timeout=10)
    p.assert_no_process_group()


def test_start_terminates_hung_child_before_raising(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression: start()'s bind-timeout branch used to raise without killing the
    child, leaking a daemon that would outlive the fixture (conftest's ``stop()``
    only ever runs after ``start()`` returns). A shim that ignores its argv and
    just sleeps stands in for an sshd that starts but never binds the port.
    """
    shim = tmp_path / "never_binds.sh"
    shim.write_text("#!/bin/sh\nexec sleep 300\n")
    shim.chmod(0o755)
    monkeypatch.setattr(_sshd, "_SSHD", str(shim))
    monkeypatch.setattr(_sshd, "_READY_TIMEOUT", 0.5)

    sshd = _sshd.LoopbackSshd(tmp_path / "unused_config", tmp_path / "sshd.log")
    with pytest.raises(RuntimeError, match="not accepting"):
        sshd.start(_sshd.free_port())

    assert sshd._proc is not None
    assert sshd._proc.poll() is not None, "start()'s timeout branch leaked the child process"
