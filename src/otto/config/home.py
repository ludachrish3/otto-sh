"""otto's user-level home, and the workspace key that shards it.

``~/.otto`` (relocatable wholesale via ``OTTO_HOME``) is where otto keeps
EVERYTHING IT DERIVED AND CAN REBUILD. That is the whole contract, and it is
the third line of a three-line taxonomy: a repo's ``.otto/`` holds source
config, the xdir holds run outputs, this holds derived state. One consequence
worth stating: anything under here may be deleted at any time, and otto must
rebuild it without complaint.

The top level is keyed by WORKSPACE -- the normalized ``OTTO_SUT_DIRS`` set --
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

_EMPTY_SLUG = "no-repos"
"""Slug for the empty SUT-dir set -- the bare-lab-directory case.

Legal, not an error: a lab-only user has a workspace, it just has no repos in
it, and giving them one stable home is better than a special case that refuses.
"""

_HASH_CHARS = 8
_SLUG_CHARS = 40
"""Cap on the slug half, so a ten-repo workspace cannot produce a directory
name that breaks ``ls``. The hash carries correctness, so truncating the slug
costs legibility only."""


def otto_home() -> Path:
    """Return otto's user-level home: ``$OTTO_HOME`` if set, else ``~/.otto``.

    PURE -- never creates the directory. Creation belongs to whoever writes
    into it, at the moment it writes, so a read-only code path (completion,
    ``--help``) can ask where the home *would* be without making one.
    """
    from ..models.settings import OttoEnvSettings

    home = OttoEnvSettings().home
    return home.expanduser() if home is not None else Path.home() / ".otto"


def _normalized(sut_dirs: "list[Path]") -> "list[Path]":
    """Absolute, symlink-resolved, sorted, de-duplicated.

    All four matter to the key. Absolute+resolved so the same workspace reached
    by a relative path or through a symlink is the SAME workspace; sorted so
    the order the operator happened to type does not shard the home; deduped so
    a repeated entry cannot key differently from the set it denotes.
    """
    return sorted({d.expanduser().resolve() for d in sut_dirs})


def workspace_key(sut_dirs: "list[Path]") -> str:
    """Return the ``<hash8>-<slug>`` directory name for *sut_dirs*.

    The hash is over the resolved paths, one per line -- the newline matters,
    because concatenating them bare would let ``/a/bc`` + ``/d`` and ``/a`` +
    ``/bc/d`` hash alike. The slug is over their basenames only, PEP-503
    normalized through the same ``normalize_name`` the ``-I``/``-E`` switches
    and inter-repo dependency matching already use, so this repo keeps ONE
    normalization rule rather than growing a second regex.
    """
    from ..models.dependencies import normalize_name

    resolved = _normalized(sut_dirs)
    digest = hashlib.sha256("\n".join(str(p) for p in resolved).encode()).hexdigest()
    slug = normalize_name("-".join(p.name for p in resolved)) if resolved else _EMPTY_SLUG
    return f"{digest[:_HASH_CHARS]}-{slug[:_SLUG_CHARS]}"


def workspace_home(sut_dirs: "list[Path] | None" = None) -> Path:
    """Return this workspace's home directory under :func:`otto_home`.

    *sut_dirs* defaults to the live ``OTTO_SUT_DIRS`` reading, so ordinary
    callers ask for "this run's workspace home" with no arguments and tests can
    pin a set explicitly. PURE -- never creates.
    """
    from ..models.settings import OttoEnvSettings

    dirs = OttoEnvSettings().sut_dirs if sut_dirs is None else sut_dirs
    return otto_home() / workspace_key(dirs)
