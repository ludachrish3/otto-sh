"""``[coverage]`` config resolution from ``.otto/settings.toml``.

Pure helpers over the already-parsed repo list: which repo (if any) declared
a ``[coverage]`` section, and what that section's raw settings dict looks
like. Every ``otto test --cov`` / ``otto cov`` code path resolves its
coverage settings through :func:`has_cov_config`, :func:`get_cov_repo`, and
:func:`get_cov_config`, so a lab with multiple SUT repos always picks the
same one. :func:`prepare_empty_dir` is the fourth function here — the
typer-free empty/overwrite directory gate shared by ``--cov-dir`` and
``--cov-report-dir``.
"""

import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..config.repo import Repo


def has_cov_config(cov: dict[str, Any]) -> bool:
    """Return True when the repo actually declared coverage settings."""
    return bool(
        cov.get("gcda_remote_dir") or cov.get("embedded") or cov.get("tiers") or cov.get("hosts")
    )


def get_cov_repo(repos: "list[Repo]") -> "Repo | None":
    """Return the first repo with a ``[coverage]`` section in its settings."""
    for repo in repos:
        if has_cov_config(repo.settings.get("coverage") or {}):
            return repo
    return None


def get_cov_config(repos: "list[Repo]") -> dict[str, Any]:
    """Extract the ``[coverage]`` config from the first repo that has one."""
    repo = get_cov_repo(repos)
    return repo.settings["coverage"] if repo else {}


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
