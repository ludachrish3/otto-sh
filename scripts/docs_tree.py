"""Which files under ``docs/`` are hand-written source: the one answer every checker uses.

A checker that walks the docs (the teaching check, the doctest, version and
citation lints, the docs guard tests) judges what a person writes. Two kinds of
file under ``docs/`` are not that, and every checker leaves both out:

* :data:`NON_SOURCE_DIRS`: build output, the archived specs and plans, and the
  vendored intersphinx inventories, skipped at any depth;
* :data:`RENDERED`: the git-ignored pages a renderer writes at build time.
  ``docs/conf.py`` runs each renderer from this table, so the table is the one
  list. A rendered page is judged by its renderer's own tests, and a fix to
  one belongs in the renderer's source, never in the page.

A checker with a reason of its own to skip more passes it as ``also_skip`` and
states the reason at its call site. Sphinx's ``exclude_patterns`` is a
different decision, what Sphinx builds, and is not made here.

Spec ``docs/superpowers/specs/2026-11-19-docs-source-tree-design.md`` (#1188).
"""

import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

NON_SOURCE_DIRS = ("_build", "superpowers", "_inventories")
"""Directories under ``docs/`` that hold no hand-written source, at any depth."""

_NON_SOURCE = frozenset(NON_SOURCE_DIRS)

SOURCE_SUFFIXES = (".md", ".rst")
"""The suffixes of a docs page."""


@dataclass(frozen=True)
class Rendered:
    """One renderer ``docs/conf.py`` runs at ``builder-inited``, and the output it owns.

    *output* is docs-relative: a file, or a directory (ending in ``/``) when the
    renderer owns every page under it.
    """

    module: str
    output: str


RENDERED = (
    Rendered("scripts.render_support_matrix", "architecture/support-matrix.md"),
    Rendered("scripts.render_kmodcov_matrix", "cli/cov/instrumenting/kmodcov-matrix.md"),
    Rendered("scripts.render_proven_range", "cli/known-good.md"),
    Rendered("scripts.render_module_graph", "architecture/modules.md"),
    Rendered("scripts.render_python_badge", "_static/generated/python-badge.svg"),
    Rendered("scripts.render_release_notes", "release-notes/"),
)
"""Every git-ignored page (or page asset) rendered at build time, with its renderer.

Their sources are schemas/support_matrix.json, schemas/kmodcov_matrix.json,
src/otto/check/proven.json, tach.toml, pyproject.toml's Python classifiers and,
for the release notes, git history: the tags merged into HEAD and the commits
they hold, rendered through cliff.toml. Makefile SPHINX_SRCS lists every source
and renderer, so editing one re-triggers the build; git history is listed as
docs/_build/.release-tags, a stamp rewritten only when the tag set changes.
"""


def _skips(also_skip: Iterable[str]) -> tuple[str, ...]:
    """Every docs-relative skip: the rendered outputs, then a checker's own *also_skip*.

    An entry names a directory (ending in ``/``) or a file (with a suffix). A bare
    ``"examples"`` names neither, and as a file it would silently never match, so it
    is refused.
    """
    extra = tuple(also_skip)
    for entry in extra:
        if not entry.endswith("/") and not PurePosixPath(entry).suffix:
            raise ValueError(
                f"also_skip entry {entry!r}: a directory ends in '/', a file has a suffix"
            )
    return (*(r.output for r in RENDERED), *extra)


def _under(rel: str, prefix: str) -> bool:
    """Whether *rel* is *prefix* itself, or lies under the directory *prefix* names."""
    if prefix.endswith("/"):
        return rel.startswith(prefix)
    return rel == prefix


def is_source_dir(rel_dir: "str | PurePosixPath", *, also_skip: Iterable[str] = ()) -> bool:
    """Whether the docs-relative directory *rel_dir* may hold source, so a walk enters it.

    A walk prunes the directories this refuses, so it never reads ``_build``'s tree.
    """
    path = PurePosixPath(rel_dir)
    if _NON_SOURCE.intersection(path.parts):
        return False
    posix = f"{path.as_posix()}/"
    return not any(p.endswith("/") and _under(posix, p) for p in _skips(also_skip))


def _is_source(path: PurePosixPath, skips: "tuple[str, ...]") -> bool:
    """:func:`is_source` over already-resolved *skips*."""
    if _NON_SOURCE.intersection(path.parent.parts):
        return False
    posix = path.as_posix()
    return not any(_under(posix, prefix) for prefix in skips)


def is_source(rel: "str | PurePosixPath", *, also_skip: Iterable[str] = ()) -> bool:
    """Whether the docs-relative file *rel* is hand-written source a checker judges.

    *also_skip* holds a checker's own extra docs-relative directories (``"examples/"``,
    with the slash) or files, each matched whole, never as a name prefix.
    """
    return _is_source(PurePosixPath(rel), _skips(also_skip))


def docs_sources(
    root: Path,
    *,
    suffixes: Iterable[str] = SOURCE_SUFFIXES,
    also_skip: Iterable[str] = (),
) -> list[Path]:
    """Every hand-written source file under the docs root *root* with one of *suffixes*, sorted.

    *root* is any docs tree, the checkout's or a test's own; skips are matched
    relative to it, and a skipped directory is never entered.
    """
    wanted = tuple(suffixes)
    skip = tuple(also_skip)
    skips = _skips(skip)
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        rel_dir = here.relative_to(root).as_posix()
        prefix = "" if rel_dir == "." else f"{rel_dir}/"
        dirnames[:] = [d for d in dirnames if is_source_dir(f"{prefix}{d}", also_skip=skip)]
        found += [
            here / name
            for name in filenames
            if Path(name).suffix in wanted and _is_source(PurePosixPath(f"{prefix}{name}"), skips)
        ]
    return sorted(found)
