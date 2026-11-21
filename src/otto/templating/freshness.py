"""The freshness rule: which files a render may depend on, known without rendering.

A render reads its entry and whatever its imports reach, and an import can
only reach a file under an import root, or otto's own ``otto.libsonnet``
(:mod:`otto.templating.imports`). So the entry, every root, every directory
and file under each root, and the library over-approximate the import
closure, including the absent candidates that could later shadow a served
file: a new file anywhere a lookup could reach moves its directory's mtime.
Fingerprints and completion key paths stat this list (each through
:func:`~otto.models.base.observed_fingerprint`), so they never evaluate a
template. The cost scales with the files under the import
roots, not with the size of the fleet a template generates.
"""

import os
from collections.abc import Sequence
from pathlib import Path


def rendering_dependencies(*, entries: Sequence[Path], roots: Sequence[Path]) -> list[Path]:
    """Return every file rendering *entries* under *roots* may read: THE complete set.

    *entries* in their order, then, sorted: each of *roots* and every
    directory and file under each (:func:`root_tree`, walked once however
    many entries there are), and the packaged ``otto.libsonnet``
    (:func:`~otto.templating.imports.library_file`), which every render may
    import whatever its roots, so an otto upgrade that changes the library
    moves it, ``import_paths = []`` included. A jsonnet lab source's
    completion key paths and a ``.jsonnet`` inventory's ``stat_paths()``
    are both this list. Stats only; nothing is evaluated.
    """
    from .imports import library_file

    extra = root_tree(roots)
    library = library_file()
    if library is not None:
        extra.add(library)
    return [*entries, *sorted(extra - set(entries))]


def root_tree(roots: Sequence[Path]) -> set[Path]:
    """Return each of *roots* and every directory and file under each: the walk, done once.

    A root that does not exist is listed itself, so its later creation is
    seen. The walk does not descend into a symlinked directory: an import
    through one that leaves the roots is refused, and one that stays inside
    reaches files the walk lists at their real place.
    """
    found: set[Path] = set(roots)
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            here = Path(dirpath)
            found.update(here / name for name in dirnames)
            found.update(here / name for name in filenames)
    return found
