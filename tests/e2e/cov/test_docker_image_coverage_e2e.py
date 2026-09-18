"""End-to-end: container-image coverage on the bed's daemon host (test3).

    otto -l unix test --cov --cov-clean TestCovContainer      (repo5)
    otto -l unix cov report <run> --dir <report>

Prerequisites: test3 up with docker; docker and gcc on the dev VM (the
tarball is built here, by tests/repo5/docker/build.sh directly, when
missing or stale — no kernel headers needed, since TestCovContainer's own
fixture only installs the two container products, on test3, itself; `otto
test` never touches a product verb). Pinned to one xdist worker:
/var/cov/cov_container* on test3 is shared state.
"""

import pytest

from otto.coverage.capture.model import Capture
from otto.coverage.store.model import CoverageStore
from tests.e2e._otto_subprocess import REPO5, run_otto
from tests.e2e.cov._repo5_build import TARBALL, _hits, _line_of, _record, ensure_image_artifacts

PRODUCTS = {"cov_container", "cov_container_ref"}
_LAB = "unix"


@pytest.fixture(scope="module", autouse=True)
def built_image(tmp_path_factory):
    """Build the container-image artifact set when stale."""
    ensure_image_artifacts(tmp_path_factory)
    assert TARBALL.stat().st_size > 0


def _run_otto(argv, *, xdir, timeout):
    result = run_otto(argv, xdir=xdir, sut_dirs=REPO5, lab=_LAB, timeout=timeout)
    if result.returncode != 0:
        raise AssertionError(
            f"otto --lab {_LAB} {' '.join(argv)} exited {result.returncode}\n"
            f"{result.stdout}\n{result.stderr}"
        )
    return result


@pytest.fixture(scope="module")
def coverage_run(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("docker_image_e2e")
    xdir = tmp / "xdir"
    xdir.mkdir()
    report_dir = tmp / "report"
    _run_otto(["test", "--cov", "--cov-clean", "TestCovContainer"], xdir=xdir, timeout=900)
    (log_dir,) = sorted((xdir / "test").glob("*"))
    _run_otto(["cov", "report", str(log_dir), "--dir", str(report_dir)], xdir=xdir, timeout=120)
    return CoverageStore.load(report_dir / "store.json"), log_dir / "cov", log_dir


@pytest.mark.integration
@pytest.mark.xdist_group("coverage_e2e")
class TestDockerImageCoverage:
    def test_container_products_land_only_on_test3(self, coverage_run):
        # TestCovContainer's own fixture installs only on test3 — otto test
        # never touches a product verb, so no other host's leaf can appear.
        _, cov_dir, _ = coverage_run
        assert {d.name for d in cov_dir.iterdir() if d.is_dir()} == {"test3"}
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
        src = REPO5 / "docker" / "src" / "math_ops.c"
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
        captures the container's own PID1 output (`sleep infinity`, which prints
        nothing), not what `docker exec` ran inside it — so an empty
        container.log is the correct pin here, not merely the weakest one.
        """
        _, _, log_dir = coverage_run
        for name in PRODUCTS:
            log = log_dir / "logs" / "test3" / name / "debug" / "container.log"
            assert log.is_file(), f"missing {log}"
            assert log.read_text() == "", f"{log}: expected empty, got {log.read_text()!r}"

    def test_the_bed_is_clean_afterwards(self, coverage_run, tmp_path):
        # Its own xdir, not coverage_run's: that xdir/test is the very
        # directory coverage_run's own fixture glob-unpacks a single child
        # from, and writing a second entry into it would break that on any
        # re-evaluation.
        xdir = tmp_path / "hygiene"
        xdir.mkdir()
        containers = _run_otto(
            ["host", "test3", "run", "docker ps -a --format '{{.Names}}'"], xdir=xdir, timeout=120
        )
        assert "cov_container" not in containers.stdout
        images = _run_otto(
            ["host", "test3", "run", "docker images --format '{{.Repository}}:{{.Tag}}'"],
            xdir=xdir,
            timeout=120,
        )
        # No repository filter in the command itself (unlike `docker images
        # otto-cov-demo`, which would put the very name being checked for
        # into otto's own echoed-command log line, making the substring
        # check below pass vacuously) — list every image and check none of
        # them is ours, the same shape as the container check above.
        assert "otto-cov-demo" not in images.stdout
        # A presence anchor: the base image the tarball was built FROM stays
        # in the daemon's cache (only the loaded reference's own rmi runs),
        # so an all-absent listing (a broken `docker images` call, a wrong
        # host) reads the same as a genuinely clean one without this.
        assert "alpine:3.20" in images.stdout
