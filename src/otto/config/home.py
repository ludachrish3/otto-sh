"""otto's user-level home, and the workspace key that shards it.

``~/.otto`` (relocatable wholesale via ``OTTO_HOME``) is where otto keeps
EVERYTHING IT DERIVED AND CAN REBUILD. That is the whole contract, and it is
the third line of a three-line taxonomy: a project root's ``.otto/`` holds source
config, the xdir holds run outputs, this holds derived state. One consequence
worth stating: anything under here may be deleted at any time, and otto must
rebuild it without complaint.

The top level is keyed by WORKSPACE -- the normalized ``OTTOPATH`` set --
rather than flat, so per-workspace reset is one ``rm -rf`` and there is one
answer to "where do my otto caches live". The key is two parts with two jobs:
an 8-character hash that makes it CORRECT (distinct workspaces cannot collide,
including two whose directories happen to share basenames), and a slug that
makes it LEGIBLE (an operator running ``ls ~/.otto`` can tell which workspace
is which without decoding a hash).

This module is the one owner of both. Nothing else derives a path under the
home -- a second derivation is how the two completion caches would drift apart
again, which is the wart the relocation exists to remove.
"""

import hashlib
from pathlib import Path

_EMPTY_SLUG = "no-projects"
"""Slug for an empty ``OTTOPATH`` -- the bare-lab-directory case.

Legal, not an error: a lab-only user has a workspace, it just has no projects in
it, and giving them one stable home is better than a special case that refuses.
"""

_HASH_CHARS = 8
_SLUG_CHARS = 40
"""Cap on the slug half, so a ten-project workspace cannot produce a directory
name that breaks ``ls``. The hash carries correctness, so truncating the slug
costs legibility only."""

SNAPSHOT_CACHE_DIRNAME = "inventory-cache"
"""Directory holding the inventory snapshot cache (host-inventory spec §9.5)."""

PYCACHE_DIRNAME = "pycache"
"""Workspace directory where every pytest session otto starts writes its bytecode.

Used as ``sys.pycache_prefix`` for the session, unless the user set a prefix
(``PYTHONPYCACHEPREFIX``). Bytecode written beside a test file would add a
``__pycache__`` entry to its directory, moving the stat the
test-names cache keys on."""

PYTEST_CACHE_DIRNAME = "pytest-cache"
"""Workspace directory holding pytest's cache (``-o cache_dir=``) for otto's runs.

pytest otherwise writes ``.pytest_cache/`` into its rootdir, which can be a
test directory."""


def otto_home() -> Path:
    """Return otto's user-level home: ``$OTTO_HOME`` if set, else ``~/.otto``.

    PURE -- never creates the directory. Creation belongs to whoever writes
    into it, at the moment it writes, so a read-only code path (completion,
    ``--help``) can ask where the home *would* be without making one.
    """
    from ..models.settings import OttoEnvSettings

    home = OttoEnvSettings().home
    return home.expanduser() if home is not None else Path.home() / ".otto"


def snapshot_cache_dir() -> Path:
    """Return ``<otto home>/inventory-cache`` — where inventory snapshots live (§9.5).

    Lives here, not in :mod:`otto.inventory.cache`, because this module is the
    one owner of every path under the home; a second deriver is how the two
    completion caches drifted apart before the relocation.

    Deliberately NOT under a workspace key, which is the one place this differs
    from :func:`workspace_home`. A snapshot is identified by the inventory's own
    configuration (backend, normalized URL, filter), and §8 gives a process
    exactly one inventory however many projects it spans, so two workspaces
    pointing at one NetBox SHOULD share one snapshot and one refresh schedule.
    Sharding by workspace would give them one file each, refreshing
    independently and neither ever seeing the other's fetch.

    PURE -- never creates the directory, same as :func:`otto_home`.
    """
    return otto_home() / SNAPSHOT_CACHE_DIRNAME


def _normalized(roots: "list[Path]") -> "list[Path]":
    """Kept spelling, then the cached :func:`otto.utils.root_key`, sorted, de-duplicated.

    All four matter to the key. The kept spelling makes a relative or ``~``
    root absolute, and :func:`~otto.utils.root_key` resolves it, so the same
    workspace reached by a relative path or through a symlink is the SAME
    workspace; sorted so the order the operator happened to type does not shard
    the home; de-duplicated so a repeated root cannot key differently from the
    set it denotes.
    """
    from ..utils import kept_path_spelling, root_key

    return sorted({Path(root_key(kept_path_spelling(str(d)))) for d in roots})


def workspace_key(roots: "list[Path]") -> str:
    """Return the ``<hash8>-<slug>`` directory name for *roots*.

    The hash is over the resolved paths, one per line -- the newline matters,
    because concatenating them bare would let ``/a/bc`` + ``/d`` and ``/a`` +
    ``/bc/d`` hash alike. The slug is over their basenames only, PEP-503
    normalized through the same ``normalize_name`` the ``-I``/``-E`` switches
    and inter-project dependency matching already use, so otto keeps ONE
    normalization rule rather than growing a second regex.
    """
    from ..models.dependencies import normalize_name

    resolved = _normalized(roots)
    digest = hashlib.sha256("\n".join(str(p) for p in resolved).encode()).hexdigest()
    slug = normalize_name("-".join(p.name for p in resolved)) if resolved else _EMPTY_SLUG
    return f"{digest[:_HASH_CHARS]}-{slug[:_SLUG_CHARS]}"


def workspace_home(roots: "list[Path] | None" = None) -> Path:
    """Return this workspace's home directory under :func:`otto_home`.

    *roots* defaults to the live ``OTTOPATH`` reading, so ordinary
    callers ask for "this run's workspace home" with no arguments and tests can
    pin a set explicitly. PURE -- never creates.
    """
    from ..models.settings import OttoEnvSettings

    dirs = OttoEnvSettings().ottopath if roots is None else roots
    return otto_home() / workspace_key(dirs)
