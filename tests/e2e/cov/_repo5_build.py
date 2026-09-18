"""Shared support for repo5's two bed coverage e2es.

Two things both e2es need, factored here so neither carries its own,
drifting copy:

- Build-and-freshness, PER FAMILY. ``otto test``/``OttoSuite`` never touch
  a product verb (``stage``/``install``/``uninstall``/``get_product_logs``
  are reachable only from the project-CLI actions, ``otto install`` and
  friends) — each suite installs only its OWN products, on the hosts it
  itself selects: ``TestKmodDemo`` on test1/test2, ``TestCovContainer`` on
  test3. So the kmod e2e needs only the kernel-module half built
  (``ensure_kmod_artifacts``), and the docker e2e needs only the
  container-image half (``ensure_image_artifacts``) — neither e2e's
  fallback build depends on the other family's toolchain (the docker e2e
  needs no kernel headers). What DOES cross families: ``--cov-clean`` and
  the post-run fetch run every INSTRUMENTED, MATCHED product's
  ``reset_coverage``/``prepare_coverage`` on every ``[coverage] hosts``
  host, regardless of which suite ran — harmless (a module that was never
  loaded writes nothing; a ``find`` under an absent ``cov_dir`` is just a
  warning) and needs no artifact on disk, which is why neither ensure
  function needs to know about the other's outputs. One staleness rule
  (:func:`_artifacts_are_stale`) serves both, so it stays a single copy.
- Coverage-store lookups: :func:`_line_of`/:func:`_record`/:func:`_hits`
  read a captured line's hit count off a source file and a
  :class:`~otto.coverage.store.model.CoverageStore`, failing with a message
  that names the file/line rather than a bare ``KeyError``/``StopIteration``.
"""

import subprocess
from pathlib import Path

from otto.coverage.store.model import CoverageStore, FileRecord
from tests._fixtures.gitrepo import git_env
from tests._fixtures.paths import PROJECT_ROOT
from tests.e2e._otto_subprocess import REPO5

BUILD = REPO5 / "build"
DEMO_SRC = REPO5 / "kmod" / "demo"
KGCOV = PROJECT_ROOT / "docs" / "examples" / "kgcov"
DOCKER = REPO5 / "docker"
DOCKER_SRC = DOCKER / "src"
TARBALL = DOCKER / "otto-cov-demo.tar"


def _line_of(path: Path, needle: str) -> int:
    """1-based line number of the first source line containing *needle*."""
    for i, line in enumerate(path.read_text().splitlines(), start=1):
        if needle in line:
            return i
    raise AssertionError(f"{needle!r} not found in {path}")


def _record(store: CoverageStore, name: str) -> FileRecord:
    """The store's :class:`FileRecord` whose path ends with *name*, or a named failure."""
    for fr in store.files():
        if str(fr.path).endswith(name):
            return fr
    have = [str(f.path) for f in store.files()]
    raise AssertionError(f"no FileRecord ending with {name!r}; have {have}")


def _hits(rec: FileRecord, lineno: int) -> int:
    """*lineno*'s system-tier hit count, or a named failure when it carries no ``DA:`` record."""
    assert lineno in rec.lines, f"{rec.path}:{lineno} carries no coverage data"
    return rec.lines[lineno].hits.for_tier("system")


def _demo_and_kgcov_sources() -> list[Path]:
    """Everything the kernel-module rebuild depends on: the demo's own files and otto_kgcov's."""
    return [
        # Kbuild writes the generated <module>.mod.c beside the sources AFTER the
        # library .ko; counting it would make every build look stale.
        *(p for p in DEMO_SRC.glob("*.c") if not p.name.endswith(".mod.c")),
        *DEMO_SRC.glob("*.h"),
        DEMO_SRC / "Kbuild",
        DEMO_SRC / "Makefile",
        *(p for p in KGCOV.rglob("*") if p.is_file()),
    ]


def _image_sources() -> list[Path]:
    """Everything the container-image rebuild depends on."""
    return [
        *DOCKER_SRC.glob("*.c"),
        *DOCKER_SRC.glob("*.h"),
        DOCKER / "Dockerfile",
        DOCKER / "build.sh",
    ]


def _artifacts_are_stale(artifacts: list[Path], sources: list[Path]) -> bool:
    """True when any of *artifacts* is missing, or older than any source it was built from.

    One rule for both artifact sets: a list of two ``.ko``\\ s (the kmod half)
    or a single-element list holding the tarball (the image half) — either
    way "missing" or "older than the newest source" means stale. *sources*
    must be non-empty — a caller whose glob silently returned nothing (a
    moved source directory) must not read that as "nothing to compare
    against, so never stale".
    """
    assert sources, "no sources to check staleness against — a source list moved or emptied?"
    if any(not a.is_file() for a in artifacts):
        return True
    newest_source = max(p.stat().st_mtime for p in sources if p.is_file())
    oldest_artifact = min(a.stat().st_mtime for a in artifacts)
    return oldest_artifact < newest_source


def _assert_committed(paths: list[Path], tmp_path_factory) -> None:
    """Fail loudly when any of *paths* carries an uncommitted OR untracked change.

    A capture anchors every measured file to a committed git blob at HEAD,
    while each e2e's own ``_line_of`` helper reads the worktree — an
    uncommitted file makes the two disagree on line numbers silently, and a
    brand-new, never-``git add``-ed file has no blob at HEAD at all, so it
    drops out of the capture entirely. ``git status --porcelain
    --untracked-files=all``, not ``git diff``: a diff against HEAD is silent
    about a file git has never seen — exactly the trap this guard exists to
    catch.
    """
    committed = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all", "--", *(str(p) for p in paths)],
        cwd=REPO5,
        env=git_env(tmp_path_factory.mktemp("githome")),
        check=False,
        capture_output=True,
        text=True,
    )
    assert committed.returncode == 0, f"git status failed: {committed.stderr}"
    assert not committed.stdout.strip(), (
        f"{', '.join(str(p) for p in paths)} have uncommitted or untracked changes: a capture "
        "anchors every measured file to a committed git blob at HEAD, while each e2e's "
        "_line_of() helper reads the worktree — such a file makes the two disagree on line "
        "numbers silently, or (if untracked) drops out of the capture entirely. Commit or "
        f"revert before running these e2es:\n{committed.stdout}"
    )


def ensure_kmod_artifacts(tmp_path_factory) -> None:
    """Build the kernel-module half (both .ko's) when missing or stale.

    Runs ``tests/repo5/build.sh`` — the fixture's human entry point, which
    also builds the container-image half as its last step; harmless (and
    unavoidable without splitting that script) for a suite that already
    needs kernel headers, ``make`` and ``modinfo`` on the dev VM.
    """
    _assert_committed([DEMO_SRC], tmp_path_factory)
    lib_ko = BUILD / "lib" / "otto_kgcov.ko"
    demo_ko = DEMO_SRC / "otto_kmod_demo.ko"
    if _artifacts_are_stale([lib_ko, demo_ko], _demo_and_kgcov_sources()):
        try:
            subprocess.run([str(REPO5 / "build.sh")], check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            raise AssertionError(
                f"{REPO5 / 'build.sh'} failed (exit {exc.returncode}):\n"
                f"stdout:\n{exc.stdout}\nstderr:\n{exc.stderr}"
            ) from exc


def ensure_image_artifacts(tmp_path_factory) -> None:
    """Build the container-image half (the tarball) when missing or stale.

    Runs ``docker/build.sh`` directly, not ``tests/repo5/build.sh`` — the
    image half stands alone and needs no kernel headers, so the e2e that
    only exercises it must not inherit the kmod half's prerequisites.
    """
    _assert_committed([DOCKER_SRC], tmp_path_factory)
    if _artifacts_are_stale([TARBALL], _image_sources()):
        try:
            subprocess.run([str(DOCKER / "build.sh")], check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            raise AssertionError(
                f"{DOCKER / 'build.sh'} failed (exit {exc.returncode}):\n"
                f"stdout:\n{exc.stdout}\nstderr:\n{exc.stderr}"
            ) from exc
