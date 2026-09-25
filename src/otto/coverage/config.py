"""Coverage directory handling shared by the ``otto test --cov`` / ``otto cov`` paths.

:func:`prepare_empty_dir` is the typer-free empty/overwrite directory gate
shared by ``--cov-dir`` and ``--cov-report-dir``. The ``[coverage]`` settings
lookup (which repo declares it, its ``hosts`` selector) lives beneath the
coverage pipeline, in :mod:`otto.config.coverage_settings`.
"""

import shutil
from pathlib import Path


def prepare_empty_dir(path: Path, *, overwrite: bool, flag_name: str) -> None:
    """Ensure ``path`` is an empty, existing directory — typer-free.

    Create-if-missing plus the empty/overwrite contract shared by ``--cov-dir``
    and ``--cov-report-dir`` (and by the in-run report step in
    :func:`otto.suite.run.run_suite`). Raises a plain :class:`ValueError` — never
    ``typer.BadParameter`` — when the target is non-empty and *overwrite* is not
    set, so a library caller never has a Typer exception surface from
    ``otto.coverage`` / ``otto.suite``. The CLI callbacks (``otto.cli.test``)
    translate that ``ValueError`` back into ``typer.BadParameter`` themselves.

    ``flag_name`` is the user-visible flag (e.g. ``--cov-dir``) named in the
    non-empty error message; the matching overwrite flag is derived from it. The
    caller is responsible for having rejected non-directory targets (the CLI does
    this via ``click.Path(file_okay=False, dir_okay=True)``).
    """
    path.mkdir(parents=True, exist_ok=True)
    if not any(path.iterdir()):
        return
    if not overwrite:
        overwrite_flag = f"--overwrite-{flag_name.lstrip('-')}"
        raise ValueError(
            f"{flag_name} target {path} is not empty; pass {overwrite_flag} to clear it."
        )
    for child in path.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
