"""End-to-end: container-image coverage on the bed's daemon host (test3).

    otto -l unix test --cov --cov-clean TestCovContainer      (project5)
    otto -l unix cov report <run> --dir <report>

``TestInstallOverAStoppedContainer`` is #783's CLI proof, on a test3-only lab:

    otto -l docker3 run install                                  (project5 + docker3 overlay)
    otto -l docker3 host test3 exec "docker stop cov_container cov_container_ref"
    otto -l docker3 run install
    otto -l docker3 host test3 is-installed

``TestStoppedContainerReport`` is #782's proof: a report reads the objects of
products whose containers are stopped (project5 + the bind overlay):

    docker/build-unit/test_math_ops                              (the unit view's counts)
    otto -l unix test --cov --cov-clean TestContainerStopFlow    (installs, stops: the flush)
    otto -l unix cov report <run> --dir <report>
    otto -l unix test TestScanLeftovers                          (nothing left; the store)
    otto -l unix test TestContainerStopCleanup

Prerequisites: test3 up with docker; docker and gcc on the dev VM (the
tarball is built here, by tests/project5/docker/build.sh directly, when
missing or stale — no kernel headers needed, since TestCovContainer's own
fixture only installs the two container products, on test3, itself; `otto
test` never touches a product verb). Pinned to one xdist worker:
/var/cov/cov_container* on test3 is shared state.
"""

import json
import os
import subprocess

import pytest

from otto import layout
from otto.coverage.capture.model import Capture
from otto.coverage.store.model import CoverageStore
from tests._fixtures._host_pool import docker_host_lease
from tests.e2e._otto_subprocess import PROJECT5, run_otto
from tests.e2e.cov._project5_build import (
    DOCKER3_LAB,
    LISTING_ENV,
    TARBALL,
    _hits,
    _line_of,
    _record,
    bind_overlay_project,
    clear_unit_counters,
    docker3_overlay_project,
    ensure_image_artifacts,
    listed_products,
    run_unit_tests,
)

PRODUCTS = {"cov_container", "cov_container_ref"}
_LAB = "unix"


@pytest.fixture(scope="module", autouse=True)
def built_image(tmp_path_factory):
    """Build the container-image artifact set when stale."""
    ensure_image_artifacts(tmp_path_factory)
    assert TARBALL.stat().st_size > 0


def _run_otto(argv, *, xdir, timeout):
    result = run_otto(argv, xdir=xdir, roots=PROJECT5, lab=_LAB, timeout=timeout)
    if result.returncode != 0:
        raise AssertionError(
            f"otto --lab {_LAB} {' '.join(argv)} exited {result.returncode}\n"
            f"{result.stdout}\n{result.stderr}"
        )
    return result


@pytest.fixture(scope="module")
def coverage_run(tmp_path_factory):
    """Run TestCovContainer under ``--cov`` and report it; list test3's daemon right after.

    Returns ``(store, cov_dir, log_dir, listings)``. The ``docker ps -a`` and
    ``docker images`` listings are taken inside the same ``docker_host_lease()``,
    straight after the run, so the hygiene test reads what TestCovContainer
    itself left, whatever other test (and its uninstalls) runs before it.
    """
    tmp = tmp_path_factory.mktemp("docker_image_e2e")
    xdir = tmp / "xdir"
    xdir.mkdir()
    # The listings' own xdir, not this one: xdir/test is the directory the
    # glob below unpacks a single child from.
    hygiene = tmp / "hygiene"
    hygiene.mkdir()
    report_dir = tmp / "report"
    # TestCovContainer loads, runs and removes images on test3's daemon, which
    # the docker e2e tests list whole: they hold its lease, so this run does too.
    with docker_host_lease():
        _run_otto(["test", "--cov", "--cov-clean", "TestCovContainer"], xdir=xdir, timeout=900)
        listings = {
            "containers": _run_otto(
                ["host", "test3", "exec", "docker ps -a --format '{{.Names}}'"],
                xdir=hygiene,
                timeout=120,
            ).stdout,
            "images": _run_otto(
                ["host", "test3", "exec", "docker images --format '{{.Repository}}:{{.Tag}}'"],
                xdir=hygiene,
                timeout=120,
            ).stdout,
        }
    (log_dir,) = sorted(layout.command_output_dir(xdir, "test").glob("*"))
    _run_otto(["cov", "report", str(log_dir), "--dir", str(report_dir)], xdir=xdir, timeout=120)
    store = CoverageStore.load(path=report_dir / "store.json")
    return store, layout.cov_dir(log_dir), log_dir, listings


@pytest.mark.integration
@pytest.mark.xdist_group("coverage_e2e")
class TestDockerImageCoverage:
    def test_container_products_land_only_on_test3(self, coverage_run):
        # TestCovContainer's own fixture installs only on test3 — otto test
        # never touches a product verb, so no other host's leaf can appear.
        _, cov_dir, *_ = coverage_run
        hosts = {d.name for d in cov_dir.iterdir() if d.is_dir() and not d.name.startswith(".")}
        assert hosts == {"test3"}
        assert not (cov_dir / ".otto-stage").exists(), "staging outlived collection"
        assert {p.name for p in (cov_dir / "test3").iterdir()} == PRODUCTS
        for name in PRODUCTS:
            assert Capture.load(cov_dir / "test3" / name / "capture.json").product == name
            assert sorted(p.name for p in (cov_dir / "test3" / name).glob("*.gcda")) == [
                "product-main.gcda",
                "product-math_ops.gcda",
            ]

    def test_each_container_contributed_its_own_paths(self, coverage_run):
        store, *_ = coverage_run
        rec = _record(store, "math_ops.c")
        src = PROJECT5 / "docker" / "src" / "math_ops.c"
        # tarball container
        assert _hits(rec, _line_of(src, "return a + b;")) == 1
        # reference container
        assert _hits(rec, _line_of(src, "return a - b;")) == 1
        # clamp above, reference
        assert _hits(rec, _line_of(src, "return hi;")) == 1
        # nobody ran mul
        assert _hits(rec, _line_of(src, "return a * b;")) == 0

    def test_container_log_is_hauled(self, coverage_run):
        """The haul writes the file even though it is legitimately empty: `docker logs`
        captures the container's own PID1 output (`/opt/product serve`, which
        prints nothing), not what `docker exec` ran inside it — so an empty
        container.log is the correct pin here, not merely the weakest one.
        """
        _, _, log_dir, _ = coverage_run
        for name in PRODUCTS:
            log = log_dir / "logs" / "test3" / name / "debug" / "container.log"
            assert log.is_file(), f"missing {log}"
            assert log.read_text() == "", f"{log}: expected empty, got {log.read_text()!r}"

    def test_the_bed_is_clean_afterwards(self, coverage_run):
        listings = coverage_run[3]
        assert "cov_container" not in listings["containers"]
        # No repository filter in the command itself (unlike `docker images
        # otto-cov-demo`, which would put the very name being checked for
        # into otto's own echoed-command log line, making the substring
        # check below pass vacuously) — list every image and check none of
        # them is ours, the same shape as the container check above.
        assert "otto-cov-demo" not in listings["images"]
        # A presence anchor: the base image the tarball was built FROM stays
        # in the daemon's cache (only the loaded reference's own rmi runs),
        # so an all-absent listing (a broken `docker images` call, a wrong
        # host) reads the same as a genuinely clean one without this.
        assert "alpine:3.20" in listings["images"]

    def test_cleanup_removes_the_root_owned_cov_dirs_through_sudo(self, tmp_path):
        """#777: cleanup sweeps the root-owned bind-mount ``cov_dir``s, elevated.

        All preconditions first, then one cleanup, then all postconditions.
        It needs no earlier run: the precondition makes each ``cov_dir``
        itself. The cleanup is scoped to test3 by the docker3 overlay lab, so
        no other host's products are uninstalled under this module, and it
        leaves the lab's tunnels and impairments to the other groups using
        them. The probe and removal observables are cov_sweep's DEBUG lines,
        read from the captured output (``coverage-dirs-clean`` writes no
        output dir) at a console wide enough that none wraps.

        The fetch already took the counters, and a flat ``cov_dir`` left empty
        is clean by the spec, so the precondition plants what gcov leaves
        under a prefix: a root-owned empty subdirectory (as the container's
        root user creates them) in each ``cov_dir``. Root runs the removal, so
        each ``cov_dir`` itself goes too.
        """
        overlay = docker3_overlay_project(tmp_path / "overlays")
        env = {"OTTOPATH": os.pathsep.join([str(PROJECT5), str(overlay)]), **LISTING_ENV}
        xdir = tmp_path / "xdir"
        xdir.mkdir()
        plant = " && ".join(
            f"sudo -n mkdir -p /var/cov/{name}/otto-bed-777" for name in sorted(PRODUCTS)
        )
        gone = " && ".join(f"test ! -e /var/cov/{name}" for name in sorted(PRODUCTS))
        unelevated_probe = (
            "coverage probe on test3: 2 product(s) answered unelevated, 0 re-probed elevated"
        )

        def otto(argv: "list[str]", timeout: int = 300) -> "subprocess.CompletedProcess[str]":
            return run_otto(
                argv,
                xdir=xdir,
                roots=PROJECT5,
                lab=DOCKER3_LAB,
                extra_argv_prefix=["--log-level", "DEBUG"],
                extra_env=env,
                timeout=timeout,
            )

        def flat(result: "subprocess.CompletedProcess[str]") -> str:
            return " ".join((result.stdout + result.stderr).split())

        with docker_host_lease():
            planted = otto(["host", "test3", "exec", plant])
            assert planted.returncode == 0, _said(planted)
            dirty = otto(["host", "test3", "coverage-dirs-clean"])
            assert dirty.returncode == 1, _said(dirty)
            assert unelevated_probe in flat(dirty), _said(dirty)

            cleanup = otto(
                ["run", "cleanup", "--no-reset-impairments", "--no-remove-tunnels"], timeout=900
            )
            assert cleanup.returncode == 0, _said(cleanup)
            assert "coverage removal on test3: 0 plain, 2 elevated" in flat(cleanup), _said(cleanup)

            absent = otto(["host", "test3", "exec", gone])
            assert absent.returncode == 0, _said(absent)
            clean = otto(["host", "test3", "coverage-dirs-clean"])
            assert clean.returncode == 0, _said(clean)
            assert unelevated_probe in flat(clean), _said(clean)


# --- #783: `otto run install` over a stopped container of the same name -----


def _said(result: "subprocess.CompletedProcess[str]") -> str:
    return f"exited {result.returncode}\n{result.stdout}\n{result.stderr}"


@pytest.fixture(scope="module")
def reinstall_run(tmp_path_factory) -> "dict[str, subprocess.CompletedProcess[str]]":
    """Install, stop and reinstall on the docker3 lab (test3 alone); every step's result.

    ``otto run install`` installs every project's products on every fleet host
    and has no product filter, so the scope is the lab: the docker3 overlay
    (``OTTOPATH=<project5>:<overlay>``) makes test3 the only host. Exit codes
    are read, and the install over running containers is read for its skip
    lines; never ``otto host … exec`` stdout, which echoes its command.
    """
    tmp = tmp_path_factory.mktemp("docker_reinstall_e2e")
    overlay = docker3_overlay_project(tmp / "overlays")
    # LISTING_ENV: the guard below reads the --list-products table back.
    env = {"OTTOPATH": os.pathsep.join([str(PROJECT5), str(overlay)]), **LISTING_ENV}
    # The cleanup suite uninstalls the stop suites' products, the bind one
    # among them, so it needs the bind overlay too: an earlier stop flow's
    # leftover is removed here as well.
    bind = bind_overlay_project(tmp / "overlays")
    cleanup_env = {"OTTOPATH": os.pathsep.join([str(PROJECT5), str(overlay), str(bind)])}
    xdir = tmp / "xdir"
    xdir.mkdir()

    def otto(
        argv: "list[str]", timeout: int = 900, extra_env: "dict[str, str] | None" = None
    ) -> "subprocess.CompletedProcess[str]":
        return run_otto(
            argv,
            xdir=xdir,
            roots=PROJECT5,
            lab=DOCKER3_LAB,
            extra_env=extra_env or env,
            timeout=timeout,
        )

    steps: "dict[str, subprocess.CompletedProcess[str]]" = {}
    with docker_host_lease():
        listed = otto(["--list-products"], timeout=120)
        assert listed.returncode == 0, _said(listed)
        assert listed_products(listed.stdout) == {
            "cov_container": ["test3"],
            "cov_container_ref": ["test3"],
        }, _said(listed)
        try:
            first = otto(["run", "install"])
            assert first.returncode == 0, _said(first)
            # #367: running containers are passed over, not re-run into a name clash.
            steps["over_running"] = otto(["run", "install"])
            stop = "docker stop cov_container cov_container_ref"
            stopped = otto(["host", "test3", "exec", stop], timeout=180)
            assert stopped.returncode == 0, _said(stopped)
            steps["reinstall"] = otto(["run", "install"])
            steps["is_installed"] = otto(["host", "test3", "is-installed"], timeout=120)
            steps["hook"] = otto(["test", "TestContainerReinstall"])
        finally:
            steps["cleanup"] = otto(["test", "TestContainerStopCleanup"], extra_env=cleanup_env)
    return steps


@pytest.mark.integration
@pytest.mark.xdist_group("coverage_e2e")
class TestInstallOverAStoppedContainer:
    def test_install_over_running_containers_skips_them(self, reinstall_run):
        over = reinstall_run["over_running"]
        assert over.returncode == 0, _said(over)
        for name in ("cov_container", "cov_container_ref"):
            assert f"{name}: skipped — already installed" in _said(over), _said(over)

    def test_run_install_over_stopped_containers_succeeds(self, reinstall_run):
        assert reinstall_run["reinstall"].returncode == 0, _said(reinstall_run["reinstall"])

    def test_the_products_read_installed_after_it(self, reinstall_run):
        assert reinstall_run["is_installed"].returncode == 0, _said(reinstall_run["is_installed"])

    def test_the_install_hook_reinstalls_each_product(self, reinstall_run):
        assert reinstall_run["hook"].returncode == 0, _said(reinstall_run["hook"])

    def test_the_reinstall_cleaned_up(self, reinstall_run):
        assert reinstall_run["cleanup"].returncode == 0, _said(reinstall_run["cleanup"])


# --- #782: a report reads the objects of stopped containers ------------------

_CONTAINERD_STORE = ["driver-type", "io.containerd.snapshotter.v1"]


@pytest.fixture(scope="module")
def stopped_report(tmp_path_factory) -> dict:
    """Unit counts, a flushing stop, the report, the leftover check and the cleanup.

    Each step's result is kept and asserted by the tests, never inside the
    ``finally``: a failing cleanup cannot mask the report's failure, and the
    unit counters are cleared whatever the cleanup does.
    """
    tmp = tmp_path_factory.mktemp("docker_stopped_report_e2e")
    bind = bind_overlay_project(tmp / "overlays")
    env = {"OTTOPATH": os.pathsep.join([str(PROJECT5), str(bind)])}
    xdir = tmp / "xdir"
    xdir.mkdir()
    report_dir = tmp / "report"
    record_path = tmp / "image_store.json"

    def otto(
        argv: "list[str]", timeout: int = 900, extra_env: "dict[str, str] | None" = None
    ) -> "subprocess.CompletedProcess[str]":
        return run_otto(
            argv,
            xdir=xdir,
            roots=PROJECT5,
            lab=_LAB,
            extra_env={**env, **(extra_env or {})},
            timeout=timeout,
        )

    steps: "dict[str, subprocess.CompletedProcess[str]]" = {}
    log_dir = None
    with docker_host_lease():
        try:
            try:
                run_unit_tests()
                steps["test"] = otto(["test", "--cov", "--cov-clean", "TestContainerStopFlow"])
                (log_dir,) = sorted(layout.command_output_dir(xdir, "test").glob("*"))
                steps["report"] = otto(
                    ["cov", "report", str(log_dir), "--dir", str(report_dir)], timeout=900
                )
                steps["leftovers"] = otto(
                    ["test", "TestScanLeftovers"],
                    extra_env={"OTTO_E2E_IMAGE_STORE_RECORD": str(record_path)},
                )
            finally:
                steps["cleanup"] = otto(["test", "TestContainerStopCleanup"])
        finally:
            clear_unit_counters()
    store_json = report_dir / "store.json"
    return {
        "store": CoverageStore.load(path=store_json) if store_json.is_file() else None,
        "log_dir": log_dir,
        "record": json.loads(record_path.read_text()) if record_path.is_file() else None,
        "steps": steps,
    }


@pytest.mark.integration
@pytest.mark.xdist_group("coverage_e2e")
class TestStoppedContainerReport:
    def test_the_report_ran_on_the_stopped_products(self, stopped_report):
        steps = stopped_report["steps"]
        assert steps["test"].returncode == 0, _said(steps["test"])
        assert steps["report"].returncode == 0, _said(steps["report"])

    def test_the_unit_tier_counts_the_stopped_products_sources(self, stopped_report):
        store = stopped_report["store"]
        assert store is not None, "the report wrote no store.json"
        rec = _record(store, "math_ops.c")
        src = PROJECT5 / "docker" / "src" / "math_ops.c"
        assert _hits(rec, _line_of(src, "return a + b;"), tier="unit") == 2
        assert _hits(rec, _line_of(src, "return a * b;"), tier="unit") == 1
        assert _hits(rec, _line_of(src, "return a - b;"), tier="unit") == 0
        # The unit view keeps only the product's own sources: the test's own
        # file is not one (meaningful because ensure_image_artifacts proves it
        # is committed, so it could not have dropped out of the capture).
        assert not [f.path for f in store.files() if str(f.path).endswith("test_math_ops.c")]

    def test_the_stop_flushed_the_serving_products_counters(self, stopped_report):
        store = stopped_report["store"]
        assert store is not None, "the report wrote no store.json"
        main = _record(store, "main.c")
        msrc = PROJECT5 / "docker" / "src" / "main.c"
        # One SIGTERM per instrumented product (cov_container, cov_container_ref);
        # only a stop executes this line.
        assert _hits(main, _line_of(msrc, "stop_requested = 1;"), tier="system") == 2

    def test_the_report_left_no_scan_snapshot(self, stopped_report):
        leftovers = stopped_report["steps"]["leftovers"]
        assert leftovers.returncode == 0, _said(leftovers)

    def test_the_image_store_was_containerd(self, stopped_report):
        record = stopped_report["record"]
        assert record is not None, "TestScanLeftovers wrote no image-store record"
        assert record["driver"], record
        assert _CONTAINERD_STORE in record["driver_status"], record

    def test_the_stop_flow_cleaned_up(self, stopped_report):
        cleanup = stopped_report["steps"]["cleanup"]
        assert cleanup.returncode == 0, _said(cleanup)
