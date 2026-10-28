"""Finish a built embedded-gcov extension: strip it, then check its gcov stamp.

``otto cov embedded-gcov finish`` runs :func:`finish`. It lives beside
:mod:`otto.coverage.embedded_gcov.library`, which holds the section list, because
the stamp check (:mod:`otto.coverage.stamp_guard`) reads gcov files through
:mod:`otto.host.gcovformat`, a layer above that package.

objcopy runs synchronously on the build machine, as ``export`` and ``check`` do
their work: ``finish`` is a lab-free build step, so it spawns the toolchain
directly rather than through a host. Which objcopy is the caller's choice (GNU
objcopy, ``llvm-objcopy`` or an SDK's prefixed one); otto never looks for one.
"""

import shutil

# ast-grep-ignore: coverage-git-through-gitio
import subprocess
import tempfile
from pathlib import Path

from .embedded_gcov.library import STRIPPED_SECTIONS
from .stamp_guard import StampError, verify_stamps


class FinishError(StampError):
    """A strip or publish step of ``finish`` failed; the message names the file at fault."""


def strip_extension(elf: Path, out: Path, objcopy: Path) -> None:
    """Strip *elf* into *out* with *objcopy*, removing :data:`STRIPPED_SECTIONS` and debug info.

    The flags are ones GNU objcopy and ``llvm-objcopy`` both accept, section
    wildcards included.
    """
    if not elf.is_file():
        raise FinishError(f"{elf}: no such extension to strip")
    argv = [str(objcopy)]
    argv += [f"--remove-section={name}" for name in STRIPPED_SECTIONS]
    argv += ["--strip-debug", str(elf), str(out)]
    try:
        done = subprocess.run(argv, capture_output=True, text=True, check=False)  # noqa: S603
    except OSError as exc:
        raise FinishError(f"{objcopy}: cannot run objcopy ({exc})") from exc
    if done.returncode:
        raise FinishError(f"{objcopy}: stripping {elf} failed: {done.stderr.strip()}")


def finish(elf: Path, out: Path, objcopy: Path, gcno_dir: Path) -> list[int]:
    """Strip *elf* into *out*, verify its stamps against *gcno_dir*; return the stamps.

    objcopy writes into a private directory beside *out*, so the file it creates
    has objcopy's own mode, and the check reads it there; only then is it renamed
    onto *out*, so *out* never holds an unverified extension. Any failure removes
    the staged file and *out* too: a build that did not finish leaves nothing to
    install, not even the previous build's extension, which is stale against the
    rebuilt notes.
    """
    if out.is_dir():
        raise FinishError(f"{out}: is a directory, not the stripped extension's path")
    try:
        staging = Path(tempfile.mkdtemp(prefix=f".{out.name}.", dir=out.parent))
    except OSError as exc:
        raise FinishError(f"{out}: cannot write beside it ({exc})") from exc
    staged = staging / out.name
    try:
        strip_extension(elf, staged, objcopy)
        stamps = verify_stamps(staged, gcno_dir, shown_as=out)
        try:
            staged.replace(out)
        except OSError as exc:
            raise FinishError(f"{out}: cannot publish the stripped extension ({exc})") from exc
    except BaseException as failure:
        try:
            out.unlink(missing_ok=True)
        except OSError as exc:
            raise FinishError(
                f"{out}: finish failed and the old file could not be removed ({exc})"
            ) from failure
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return stamps
