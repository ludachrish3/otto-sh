"""Where a library call's output goes: the one resolver for the three library entry points.

``Host.log_dest`` (and the log writers behind it),
:func:`otto.suite.run.run_tests` and :func:`otto.coverage.get.get_coverage`
each ask :func:`resolve_output_dir`, which answers in this order:

1. the explicit destination the caller passed, as given (nothing is created);
2. the run's output directory, ``current_policy().output_dir`` (the CLI sets it,
   and so does ``open_context(output_dir=...)``);
3. otherwise a fresh run dir under the default xdir,
   ``run_output_dir(default_xdir(), entry)`` -- the CLI's own builder, so a
   library run dir and a CLI run dir differ only in their timestamps.

The third answer is memoised per process, keyed by ``(default_xdir(), entry)``:
one directory per kind of call, so logs written through ``log_dest`` are found
again through it, and a changed ``OTTO_HOME`` gets its own tree. The resolver
creates nothing; a writer calls :func:`materialize_output_dir` on the dir it is
about to write into, which creates a memoised library run dir (with otto's
marker, :func:`otto.layout.make_run_dir`) and leaves any other dir to its owner.
"""

from typing import TYPE_CHECKING

from . import home_root, layout
from .layout import RunEntry

if TYPE_CHECKING:
    from pathlib import Path

LIBRARY_LOGS: RunEntry = RunEntry("host", "get_logs")
"""``Host.log_dest`` and the log verbs: ``<xdir>/host/<ts>_get_logs/``, as ``host get-logs``."""

LIBRARY_TEST: RunEntry = RunEntry("test")
"""``run_tests``: ``<xdir>/test/<ts>/``, as ``otto test``."""

LIBRARY_COV_GET: RunEntry = RunEntry("cov", "get")
"""``get_coverage``: ``<xdir>/cov/<ts>_get/``, as ``otto cov get``."""

_LIBRARY_RUN_DIRS: "dict[tuple[Path, RunEntry], Path]" = {}
"""The memo: one library run dir per ``(default xdir, entry)``, for the life of the process."""


def resolve_output_dir(dest: "Path | None", *, entry: RunEntry) -> "Path":
    """Return the directory a library call of kind *entry* writes into; create nothing.

    *dest* when given, else the installed run policy's ``output_dir``, else
    this process's run dir for *entry* under :func:`otto.home_root.default_xdir`.
    The default is never stamped onto an installed policy.
    """
    if dest is not None:
        return dest
    from .invocation import current_policy

    policy_dir = current_policy().output_dir
    if policy_dir is not None:
        return policy_dir
    key = (home_root.default_xdir(), entry)
    found = _LIBRARY_RUN_DIRS.get(key)
    if found is None:
        found = layout.run_output_dir(key[0], entry)
        _LIBRARY_RUN_DIRS[key] = found
    return found


def materialize_output_dir(path: "Path") -> "Path":
    """Create *path* as a marked run dir when it is a library run dir this process allocated.

    A writer calls this on the directory :func:`resolve_output_dir` gave it,
    just before writing. An explicit destination or a policy's directory is
    its owner's (the CLI made its run dir already), so it is left alone and
    the writer's own ``mkdir`` stands. Returns *path*.
    """
    if path in _LIBRARY_RUN_DIRS.values():
        layout.make_run_dir(path, exist_ok=True)
    return path


def forget_library_output_dirs() -> None:
    """Drop the memo, so the next library default gets a fresh run dir (for tests)."""
    _LIBRARY_RUN_DIRS.clear()
