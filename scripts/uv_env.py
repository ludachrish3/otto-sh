"""The environment a uv command runs in: what it must not inherit, and how long a sync may take.

The one home for these. scripts/api_regen.py imports it as a package module;
scripts/import_budget.py loads it by path beside itself, because the import
budget's delta loads that harness by path from a tip worktree. So this file
imports nothing but the standard library, from otto or from scripts, and it
is in the delta's INSTRUMENT_PATHS.
"""

import os

# Inherited variables that would point uv at another project, lock, environment
# or working directory, or switch `--locked` validation off, without the cache
# key seeing them (from `uv help sync` / `uv help lock`; UV_PROJECT_ENVIRONMENT
# is set explicitly where a sync needs it). Cache, index, network and offline
# settings pass through: an air-gapped install relies on them.
UV_TARGET_VARS = frozenset(
    {
        "VIRTUAL_ENV",
        "UV_PROJECT",
        "UV_WORKING_DIR",
        "UV_PROJECT_ENVIRONMENT",
        "UV_LOCKED",
        "UV_FROZEN",
    }
)

SYNC_TIMEOUT = 1200
"""Seconds a ``uv sync`` may take before it is a named failure, never a hang (cold cache
included)."""


def venv_clean() -> dict[str, str]:
    """Return this process's environment without the venv, project or paths it runs under.

    Every :data:`UV_TARGET_VARS` entry goes, so a uv command acts on the
    project and venv it is aimed at, never the caller's (``uv run``'s
    ``VIRTUAL_ENV``, an inherited ``UV_PROJECT``). ``PYTHONHOME`` and
    ``PYTHONPATH`` go too: they would leak the caller's modules into the
    interpreter uv runs or syncs.
    """
    dropped = UV_TARGET_VARS | {"PYTHONHOME", "PYTHONPATH"}
    return {k: v for k, v in os.environ.items() if k not in dropped}
