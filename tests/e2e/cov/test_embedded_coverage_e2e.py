"""End-to-end CLI integration test for embedded (Zephyr LLEXT) coverage.

Invokes the real ``otto test --cov`` + report pipeline as a **subprocess**
against the live ``zephyr37_llext`` ``mps2_an385`` instance (in the ``embedded``
lab, selected by the ``[coverage].hosts`` regex, reached over the ``test4``
SSH hop). Mocked unit tests can't cover this
path; only the real CLI does, and only over the real multi-hop transport:

* the ``pytest.main()`` test-phase loop followed by the *separate*
  ``asyncio.run(collect_coverage)`` collection loop — the cross-event-loop seam
  that ``OttoSuite._otto_release_connections`` closes (a stale telnet session
  reused across that boundary hangs, and the single-client QEMU socket blocks
  the collector's reconnect);
* the cross-gcov report: a host ``lcov`` driving the SDK ``arm-zephyr-eabi-gcov``;
* the hop host (``test4``) being in the ``embedded`` lab for hop resolution
  *without* being mistaken for a Unix coverage target in the meta (it is
  excluded from coverage by the ``[coverage].hosts`` regex, not inference).

Requirements (else the test FAILS LOUD, naming what is missing — G12: a
bed-certifying lane never skips):
    - the zephyr VM up with ``zephyr37_llext`` running (``zephyr-qemu-cov.service``);
    - the repo3 coverage product built into ``[coverage.embedded].build_dir``
      (see ``tests/repo3/product/README.md``).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import tomli

from otto.coverage.capture.model import Capture
from otto.logger.mode import LogMode
from tests._fixtures.labdata import element_for
from tests.e2e._otto_subprocess import PROJECT_ROOT, assert_output_dir, run_otto

REPO3 = PROJECT_ROOT / "tests" / "repo3"

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("zephyr37_llext")]


def _repo3_settings() -> dict:
    return tomli.loads((REPO3 / ".otto" / "settings.toml").read_text())


def _llext_products() -> list[dict]:
    """repo3's ``kind = "llext"`` ``[[products]]`` entries, in declaration order."""
    return [e for e in _repo3_settings().get("products", []) if e.get("kind") == "llext"]


PRODUCT = "cov_ext"
"""The declared product name — the ``<product>`` segment of the run tree, the
name each capture records, and the extension the collector dumps."""


def _extension_artifacts() -> list[Path]:
    """Every declared LLEXT artifact, one per Zephyr version bed."""
    entries = _llext_products()
    if not entries:
        pytest.fail(
            "no [[products]] entry of kind 'llext' in tests/repo3/.otto/settings.toml "
            "— this lane fails loud rather than retiring behind a skip (G12): declare "
            "the coverage extension or deselect the lane, don't hollow it."
        )
    names = {e["name"] for e in entries}
    assert names == {PRODUCT}, f"expected one product name {PRODUCT!r}, got {sorted(names)}"
    return [Path(e["artifact"]) for e in entries]


@pytest.fixture
def clean_zephyr37_llext():
    """Fail loud unless ``zephyr37_llext`` answers, and clear any loaded extension.

    Populates the active :class:`~otto.context.OttoContext` with the
    ``test4`` hop (as the integration host conftest does) so the embedded
    host's ``test4`` hop resolves, probes the console, and best-effort
    unloads ``cov_ext`` so the suite's ``load_hex`` starts from a clean slate
    (``--cov`` runs leave it resident).
    """
    import asyncio

    from otto.config.lab import Lab
    from otto.context import OttoContext, set_context
    from otto.host.factory import create_host_from_dict
    from otto.host.login_proxy import Cred
    from otto.host.unix_host import UnixHost
    from otto.utils import Status
    from tests.conftest import host_data

    lab = Lab(name="embedded_cov_e2e")
    test4 = host_data("test4")
    lab.add_host(
        UnixHost(
            ip=test4["ip"],
            element=element_for("test4"),
            creds=[Cred(**c) for c in test4["creds"]],
            board=test4.get("board"),
            is_virtual=True,
            term="ssh",
            transfer="scp",
            log=LogMode.QUIET,
        )
    )
    set_context(OttoContext(lab=lab))

    host = create_host_from_dict(host_data("zephyr37_llext"), element=element_for("zephyr37_llext"))

    async def _prep() -> bool:
        try:
            res = (await host.run("kernel version", timeout=20)).only
            # Result-family API: the command's output lives in .value (the
            # pre-unification .output attribute no longer exists).
            if res.status != Status.Success or "Zephyr" not in (res.value or ""):
                return False
            await host.run("llext unload cov_ext", timeout=20)  # best-effort
            return True
        finally:
            await host.close()

    if not asyncio.run(_prep()):
        pytest.fail(
            "zephyr37_llext console not reachable/healthy — the embedded bed is down. "
            "Bring the zephyr VM/QEMU back up (e.g. `make qemu-restart`) and retry. "
            "This is a hard failure by design (not a skip) so a dead bed can't hide "
            "behind a green run."
        )


def _run_otto(*args: str, xdir: Path, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    """Run ``otto -R --lab embedded ARGS`` against the repo3 fixture SUT.

    *xdir* keeps otto's run-log dirs under the test's tmp_path (auto-cleaned)
    rather than the default CWD (== the project root).
    """
    return run_otto(
        list(args),
        xdir=xdir,
        sut_dirs=REPO3,
        lab="embedded",
        extra_argv_prefix=["-R"],
        timeout=timeout,
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


def test_embedded_coverage_cli_e2e(clean_zephyr37_llext, tmp_path):
    """`otto test --cov` + report against the live zephyr37_llext yields product coverage."""
    # Every version's artifact must exist BEFORE otto starts, not just by the
    # time the suite's build step has run: `--cov` now resolves on a local
    # instrumentation scan of each declared artifact, and a missing file scans
    # as "unknown" — which refuses the run rather than collecting nothing.
    for artifact in _extension_artifacts():
        if not artifact.exists():
            pytest.fail(
                f"embedded-coverage product not built: {artifact} — build it per "
                "tests/repo3/product/README.md; this lane fails loud rather than "
                "skipping (G12), a skip here certified nothing."
            )

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
    # Staged under <host>/<product>/: the host id is the slug of element
    # "zephyr37_llext" -> "zephyr37-llext", the product is the [[products]]
    # entry's name.
    gcda = cov_dir / "zephyr37-llext" / PRODUCT / "cov_ext.c.gcda"
    assert gcda.exists(), f"no decoded .gcda staged for zephyr37-llext\n{result.stdout[-2000:]}"
    assert (report_dir / "index.html").exists(), "no HTML report rendered"

    # The capture names the product it came from — the dimension the store and
    # the report key their per-product rollups by (capture schema 3).
    capture = Capture.load(cov_dir / "zephyr37-llext" / PRODUCT / "capture.json")
    assert capture.product == PRODUCT, f"capture names product {capture.product!r}"

    # BOTH beds, not just the first. repo3's `[coverage] hosts` selector is
    # "zephyr(37|44)-llext" and its alternation is the whole point: the intent is
    # coverage across two Zephyr versions (3.7 and 4.4), whose ids share no
    # distinguishing prefix. Under fullmatch a bare "zephyr37-llext" selects only
    # the 3.7 bed — which would NOT fail anything above, because every assertion
    # so far names zephyr37-llext alone. It would quietly halve the collection
    # and stay green, so the selector's intent is pinned here rather than left to
    # the settings comment.
    staged = sorted(d.name for d in cov_dir.iterdir() if d.is_dir())
    assert staged == ["zephyr37-llext", "zephyr44-llext"], (
        f"expected both Zephyr coverage beds to be collected, got {staged} — "
        f"check `[coverage] hosts` in tests/repo3/.otto/settings.toml\n"
        f"{result.stdout[-2000:]}"
    )
    assert (cov_dir / "zephyr44-llext" / PRODUCT / "cov_ext.c.gcda").exists(), (
        "no decoded .gcda staged for the 4.4 bed zephyr44-llext"
    )
    # One product name across both beds, from two same-named [[products]]
    # entries with version-specific match tables: each board loaded its own
    # artifact, and the tree still has a single `cov_ext` segment per host.
    for host_id in staged:
        assert sorted(p.name for p in (cov_dir / host_id).iterdir()) == [PRODUCT], (
            f"{host_id} staged something other than a single {PRODUCT!r} product dir"
        )

    # The product file is covered (the cross-gcov processed the .gcda + .gcno).
    # The collection model stages the per-host lcov capture next to the decoded
    # .gcda at collect time (board.info, plus a path-resolved variant); the
    # report's _work/ dir only holds cross-host lcov merge products, which a
    # store-loaded run like this one never writes.
    info = cov_dir / "zephyr37-llext" / PRODUCT / "board.resolved.info"
    if not info.exists():
        info = cov_dir / "zephyr37-llext" / PRODUCT / "board.info"
    assert info.exists(), f"no lcov .info staged for zephyr37-llext\n{result.stdout[-2000:]}"
    lh, lf = _product_line_coverage(info)
    assert lf > 0, f"cov_ext.c shows no covered lines ({lh}/{lf})"
    assert lh > 0, f"cov_ext.c shows no covered lines ({lh}/{lf})"

    # a real embedded suite run produces results → test output dir created.
    # (NB: the `--cov-dir` here is tmp_path/"cov", unrelated to any `otto cov` run.)
    assert_output_dir(tmp_path, "test")
