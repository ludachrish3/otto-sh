"""End-to-end CLI integration test for embedded (Zephyr LLEXT) coverage.

Invokes the real ``otto test --cov`` + report pipeline as a **subprocess**
against the live ``sprout_cov`` ``mps2_an385`` instance (in the ``embedded``
lab, selected by the ``[coverage].hosts`` regex, reached over the ``basil_seed``
SSH hop). Mocked unit tests can't cover this
path; only the real CLI does, and only over the real multi-hop transport:

* the ``pytest.main()`` test-phase loop followed by the *separate*
  ``asyncio.run(_run_coverage)`` collection loop — the cross-event-loop seam
  that ``OttoSuite._otto_release_connections`` closes (a stale telnet session
  reused across that boundary hangs, and the single-client QEMU socket blocks
  the collector's reconnect);
* the cross-gcov report: a host ``lcov`` driving the SDK ``arm-zephyr-eabi-gcov``;
* the hop host (``basil``) being in the ``embedded`` lab for hop resolution
  *without* being mistaken for a Unix coverage target in the meta (it is
  excluded from coverage by the ``[coverage].hosts`` regex, not inference).

Requirements (else the test SKIPS, never fails spuriously):
    - the zephyr VM up with ``sprout_cov`` running (``zephyr-qemu-cov.service``);
    - the repo3 coverage product built into ``[coverage.embedded].build_dir``
      (see ``tests/repo3/product/README.md``).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import tomli

from otto.logger.mode import LogMode

PROJECT_ROOT = Path(__file__).resolve().parents[3]
REPO3 = PROJECT_ROOT / "tests" / "repo3"
OTTO_BIN = Path(sys.executable).parent / "otto"
COVERAGERC = PROJECT_ROOT / ".coveragerc"
COVERAGE_BOOTSTRAP = PROJECT_ROOT / "tests" / "_coverage_bootstrap"

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("sprout_cov")]


def _embedded_cov_settings() -> dict:
    """The ``[coverage.embedded]`` table from repo3's settings."""
    settings = tomli.loads((REPO3 / ".otto" / "settings.toml").read_text())
    return (settings.get("coverage") or {}).get("embedded") or {}


def _extension_artifact() -> Path:
    cfg = _embedded_cov_settings()
    build_dir = cfg.get("build_dir")
    ext = cfg.get("extension", "cov_ext")
    if not build_dir:
        pytest.skip("[coverage.embedded].build_dir not configured")
    return Path(build_dir) / "zephyr" / f"{ext}.stripped.llext"


@pytest.fixture
def clean_sprout_cov():
    """Skip unless ``sprout_cov`` answers, and clear any loaded extension.

    Populates the configModule with the ``basil`` hop (as the integration host
    conftest does) so the embedded host's ``basil_seed`` hop resolves, probes
    the console, and best-effort unloads ``cov_ext`` so the suite's ``load_hex``
    starts from a clean slate (``--cov`` runs leave it resident).
    """
    import asyncio

    from otto.configmodule.lab import Lab
    from otto.context import OttoContext, set_context
    from otto.host.unix_host import UnixHost
    from otto.storage.factory import create_host_from_dict
    from otto.utils import Status
    from tests.conftest import host_data

    lab = Lab(name="embedded_cov_e2e")
    basil = host_data("basil")
    lab.add_host(
        UnixHost(
            ip=basil["ip"],
            element=basil["element"],
            creds=basil["creds"],
            board=basil.get("board"),
            is_virtual=True,
            term="ssh",
            transfer="scp",
            log=LogMode.QUIET,
        )
    )
    set_context(OttoContext(lab=lab))

    host = create_host_from_dict(host_data("sprout_cov"))

    async def _prep() -> bool:
        try:
            res = (await host.run("kernel version", timeout=20)).only
            if res.status != Status.Success or "Zephyr" not in res.output:
                return False
            await host.run("llext unload cov_ext", timeout=20)  # best-effort
            return True
        finally:
            await host.close()

    if not asyncio.run(_prep()):
        pytest.fail(
            "sprout_cov console not reachable/healthy — the embedded bed is down. "
            "Bring the zephyr VM/QEMU back up (e.g. `make qemu-restart`) and retry. "
            "This is a hard failure by design (not a skip) so a dead bed can't hide "
            "behind a green run."
        )


def _run_otto(*args: str, xdir: Path, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "OTTO_SUT_DIRS": str(REPO3),
        # Keep otto's run-log dirs under the test's tmp_path (auto-cleaned)
        # rather than the default CWD (== PROJECT_ROOT), matching the sibling
        # subprocess runners in test_coverage_e2e.py / test_docker_e2e_cli.py.
        "OTTO_XDIR": str(xdir),
        "COVERAGE_PROCESS_START": str(COVERAGERC),
        "PYTHONPATH": os.pathsep.join(
            [str(COVERAGE_BOOTSTRAP), os.environ.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep),
    }
    full_argv = [str(OTTO_BIN), "--lab", "embedded", "-R", *args]
    return subprocess.run(
        full_argv,
        env=env,
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        timeout=timeout,
        check=False,
    )


def _product_line_coverage(info_file: Path) -> tuple[int, int]:
    """(lines hit, lines found) for the product cov_ext.c in an lcov .info."""
    cur = None
    lh = lf = 0
    for line in info_file.read_text().splitlines():
        if line.startswith("SF:"):
            cur = line[3:]
            lh = lf = 0
        elif line.startswith("LH:"):
            lh = int(line[3:])
        elif line.startswith("LF:"):
            lf = int(line[3:])
        elif line == "end_of_record" and cur and cur.endswith("/cov_ext.c"):
            return lh, lf
    return 0, 0


def test_embedded_coverage_cli_e2e(clean_sprout_cov, tmp_path):
    """`otto test --cov` + report against the live sprout_cov yields product coverage."""
    artifact = _extension_artifact()
    if not artifact.exists():
        pytest.skip(f"product not built: {artifact} (see tests/repo3/product/README.md)")

    report_dir = tmp_path / "report"
    cov_dir = tmp_path / "cov"

    result = _run_otto(
        "test",
        "--cov",
        "--cov-dir",
        str(cov_dir),
        "--cov-report",
        "--cov-report-dir",
        str(report_dir),
        "TestEmbeddedCoverage",
        xdir=tmp_path,
    )
    # A `.gcda` "stamp mismatch with notes file" is gcov refusing to merge a
    # `.gcda` whose gcov stamp differs from the `.gcno` used to decode it — the
    # bed ran a different compilation than the notes describe. Two causes, neither
    # a product/test bug:
    #   (1) host-build staleness — the loaded `.stripped.llext` didn't match the
    #       freshly-built `.gcno` (Zephyr's LLEXT codegen makes the recompiled
    #       object only an *order-only* ninja dep, so an incremental rebuild can
    #       regenerate the `.gcno` without re-linking the extension). build.sh now
    #       removes the link-tail outputs to force a relink and asserts stamp
    #       coherence *before* load, so a stale build should fail there, not here.
    #   (2) bed-resident staleness — the QEMU bed is still serving an older
    #       resident extension (llext refcount never drained; see _drain_unload),
    #       cleared by `make qemu-restart`.
    # Surface that as an actionable hint instead of the raw geninfo error.
    hint = ""
    if "stamp mismatch" in (result.stdout + result.stderr):
        hint = (
            "\n\nHINT: '.gcda stamp mismatch with notes file' means the extension the "
            "bed ran was built from a different compilation than the .gcno used to "
            "decode its .gcda. build.sh's pre-load stamp-coherence guard makes a stale "
            "*build* unlikely to reach here, so the usual cause is a stale bed-resident "
            "extension (llext refcount not drained / wedged QEMU): run "
            "`make qemu-restart` and retry."
        )
    assert result.returncode == 0, (
        f"otto test --cov failed (rc={result.returncode}):\n"
        f"STDOUT:\n{result.stdout[-3000:]}\nSTDERR:\n{result.stderr[-2000:]}"
        f"{hint}"
    )

    # The collector decoded a .gcda for the embedded host (cross-loop fix +
    # real hop transport), and the report rendered (cross-gcov lcov fix).
    gcda = cov_dir / "sprout_cov" / "cov_ext.c.gcda"
    assert gcda.exists(), f"no decoded .gcda staged for sprout_cov\n{result.stdout[-2000:]}"
    assert (report_dir / "index.html").exists(), "no HTML report rendered"

    # The product file is covered (the cross-gcov processed the .gcda + .gcno).
    info = report_dir / "_work" / "host_0.info"
    assert info.exists(), f"no lcov .info produced\n{result.stdout[-2000:]}"
    lh, lf = _product_line_coverage(info)
    assert lf > 0, f"cov_ext.c shows no covered lines ({lh}/{lf})"
    assert lh > 0, f"cov_ext.c shows no covered lines ({lh}/{lf})"
