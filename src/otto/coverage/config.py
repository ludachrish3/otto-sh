"""Coverage directory handling shared by the ``otto test --cov`` / ``otto cov`` paths.

:func:`prepare_empty_dir` is the typer-free empty/overwrite directory gate
shared by ``--cov-dir`` and ``--cov-report-dir``; :func:`check_empty_dir` is
the same refusal for a dry run, which creates and clears nothing. The ``[coverage]`` settings
lookup (which repo declares it, its ``hosts`` selector) lives beneath the
coverage pipeline, in :mod:`otto.config.coverage_settings`.
"""

import shutil
from pathlib import Path


def check_empty_dir(path: Path, *, overwrite: bool, flag_name: str) -> None:
    """Refuse *path* exactly as :func:`prepare_empty_dir` would, touching nothing.

    A missing directory is fine (the run would create it); a non-empty one
    needs *overwrite*. A dry run calls this instead of
    :func:`prepare_empty_dir`, so it reports the same error a real run would
    without creating or clearing anything.
    """
    if overwrite or not path.is_dir() or not any(path.iterdir()):
        return
    overwrite_flag = f"--overwrite-{flag_name.lstrip('-')}"
    raise ValueError(f"{flag_name} target {path} is not empty; pass {overwrite_flag} to clear it.")


def prepare_empty_dir(path: Path, *, overwrite: bool, flag_name: str) -> None:
    """Ensure ``path`` is an empty, existing directory — typer-free.

    Create-if-missing plus the empty/overwrite contract shared by ``--cov-dir``
    and ``--cov-report-dir`` (and by the in-run report step in
    :func:`otto.suite.run.run_tests`). Raises a plain :class:`ValueError` — never
    ``typer.BadParameter`` — when the target is non-empty and *overwrite* is not
    set, so a library caller never has a Typer exception surface from
    ``otto.coverage`` / ``otto.suite``. The CLI callbacks (``otto.cli.test``)
    translate that ``ValueError`` back into ``typer.BadParameter`` themselves.

    ``flag_name`` is the user-visible flag (e.g. ``--cov-dir``) named in the
    non-empty error message; the matching overwrite flag is derived from it. The
    caller is responsible for having rejected non-directory targets (the CLI does
    this via ``click.Path(file_okay=False, dir_okay=True)``).
    """
    check_empty_dir(path, overwrite=overwrite, flag_name=flag_name)
    path.mkdir(parents=True, exist_ok=True)
    if not overwrite:
        return
    for child in path.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
