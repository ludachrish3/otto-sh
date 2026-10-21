"""What the unit tier counts: a view's products, their source set, the capture filter.

A unit build compiles product sources and test sources side by side. The
unit report keeps only files some product's own objects were built from —
read from the local product build's notes files — so test bodies, gtest and
mocks never count, and never reach ``geninfo``.
"""

import logging
import posixpath
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ..utils import anchor_path
from .merge.gcovfile import gcno_sources
from .objects import ObjectList, ObjectListCache, Unavailable
from .pairing import GcnoIndex, notes_for_stem
from .sources import SourceResolver

if TYPE_CHECKING:
    from ..host.host import Host
    from ..host.product import Product
    from ..host.toolchain import Toolchain
    from .tiers import TierConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NamedSources:
    """A notes file's source names split into project files and everything else."""

    project: list[Path]
    external: list[str]


def _under(path: Path, root: Path) -> Path | None:
    try:
        return path.resolve().relative_to(root)
    except ValueError:
        return None


def _anchored(name: str, cwd: str | None, gcno: Path) -> str:
    """Return *name* made absolute the way the compiler's working directory would.

    A recorded cwd (gcc) anchors it; with none (clang) the notes file's own
    directory does, the rule ``geninfo`` applies in place. A name that then
    exists nowhere still falls to the resolver's tail match.
    """
    if posixpath.isabs(name):
        return name
    if cwd is not None:
        return posixpath.normpath(posixpath.join(cwd, name))
    beside = posixpath.normpath(posixpath.join(gcno.parent.as_posix(), name))
    return beside if Path(beside).exists() else name


def gcno_named_sources(gcno: Path, project_root: Path, resolver: SourceResolver) -> NamedSources:
    """Split the sources *gcno* names into project-relative paths and the rest (as recorded)."""
    got = gcno_sources(gcno)
    if got is None:
        logger.debug("unit: %s is not a notes file this reader knows; no sources from it", gcno)
        return NamedSources(project=[], external=[])
    root = project_root.resolve()
    kept: dict[Path, None] = {}
    external: dict[str, None] = {}
    for name in got.names:
        resolved = resolver.resolve(_anchored(name, got.cwd, gcno))
        rel = _under(resolved, root) if resolved is not None else None
        if rel is None:
            # a system header, gtest, or a name no project file matches
            external.setdefault(name, None)
        else:
            kept.setdefault(rel, None)
    return NamedSources(project=list(kept), external=list(external))


def gcno_project_sources(gcno: Path, project_root: Path, resolver: SourceResolver) -> list[Path]:
    """Project-relative paths of the sources *gcno* names that resolve under *project_root*."""
    return gcno_named_sources(gcno, project_root, resolver).project


def product_source_set(
    objects: ObjectList, project_root: Path, resolver: SourceResolver, index: GcnoIndex
) -> set[Path]:
    """Return the project files *objects*' notes name, headers included.

    An object with no local notes contributes nothing; one INFO line per
    product counts them and names the first few, since a product built
    elsewhere than the unit tests silently loses those files otherwise.
    """
    sources: set[Path] = set()
    unpaired: list[str] = []
    for stem in objects.paths:
        gcno = notes_for_stem(stem, index)
        if gcno is None:
            unpaired.append(str(stem))
            continue
        sources.update(gcno_project_sources(gcno, project_root, resolver))
    if unpaired:
        shown = ", ".join(unpaired[:_SHOWN])
        logger.info(
            "unit: product %s: %d of %d objects have no local notes, so their sources are "
            "not in its source set: %s%s",
            objects.product,
            len(unpaired),
            len(objects.paths),
            shown,
            ", ..." if len(unpaired) > _SHOWN else "",
        )
    return sources


_SHOWN = 3
"""How many objects without local notes the per-product INFO line names."""


_GLOB_META = str.maketrans({"*": "?", "?": "?", "[": "?", "]": "?"})


@dataclass(frozen=True)
class UnitSelection:
    """A unit directory's sources split by ownership.

    ``kept`` maps project-relative paths to their owning product; ``dropped``
    holds project-relative paths no product owns (POSIX strings), then every
    external name (system or gtest headers) as recorded.
    """

    kept: dict[Path, str]
    dropped: list[str]
    notes: list[Path] = field(default_factory=list)
    """The notes files that name a kept source; their stamps say which compiler built them."""


def select_unit_sources(
    hdir: Path, project_root: Path, resolver: SourceResolver, owners: dict[Path, str]
) -> UnitSelection:
    """Read every notes file under *hdir* and split the project sources it names by *owners*."""
    kept: dict[Path, str] = {}
    dropped_project: dict[Path, None] = {}
    dropped_external: dict[str, None] = {}
    notes: list[Path] = []
    for gcno in sorted(hdir.rglob("*.gcno")):
        named = gcno_named_sources(gcno, project_root, resolver)
        for rel in named.project:
            if rel in owners:
                kept.setdefault(rel, owners[rel])
            else:
                dropped_project.setdefault(rel, None)
        if any(rel in owners for rel in named.project):
            notes.append(gcno)
        for raw in named.external:
            dropped_external.setdefault(raw, None)
    dropped = [p.as_posix() for p in dropped_project] + list(dropped_external)
    return UnitSelection(kept=kept, dropped=dropped, notes=notes)


def include_patterns(kept: list[Path]) -> list[str]:
    """Return one unanchored tail glob per kept file; glob metacharacters become ``?``."""
    return sorted(f"*/{p.as_posix().translate(_GLOB_META)}" for p in kept)


def filter_info(
    info: Path, out: Path, keep: set[Path], project_root: Path, resolver: SourceResolver
) -> None:
    """Copy *info*'s records whose ``SF:`` resolves to a kept project-relative path.

    A final record with no ``end_of_record`` (a truncated file) is never
    copied; one WARNING names the file.
    """
    root = project_root.resolve()
    lines_out: list[str] = []
    record: list[str] = []
    keep_record = False
    for line in info.read_text().splitlines():
        if line.startswith("SF:"):
            resolved = resolver.resolve(line[3:])
            try:
                keep_record = resolved is not None and resolved.resolve().relative_to(root) in keep
            except ValueError:
                keep_record = False
        record.append(line)
        if line.strip() == "end_of_record":
            if keep_record:
                lines_out.extend(record)
            record = []
            keep_record = False
    if any(line.strip() for line in record):
        logger.warning(
            "unit: %s ends inside a record (no end_of_record): truncated, nothing kept from it",
            info,
        )
    out.write_text("\n".join(lines_out) + ("\n" if lines_out else ""))


@dataclass(frozen=True)
class UnitDir:
    """One directory of a unit view that the harvest captures, as the preflight resolved it."""

    index: int
    """The directory's position among the view's directories; it names its tracefiles."""
    path: Path
    selection: UnitSelection
    toolchain: "Toolchain"
    """The tools that capture it, the tier's ``toolchain`` setting over the build's stamps."""


@dataclass(frozen=True)
class UnitView:
    """One unit capture target: a tier's plain ``harvest_dirs`` or one ``products`` entry.

    ``owners`` maps every project-relative source the view's products were
    built from to the product that owns it; a view whose product selection
    is empty is refused before it is ever built.
    """

    tier: str
    product: str
    """The ``products`` entry's name; ``""`` for plain ``harvest_dirs``."""
    label: str
    """``<tier>`` or ``<tier>/<product>``: the view as log lines and errors name it."""
    dirs: list[Path]
    """The view's directories, anchored at the project root."""
    owners: dict[Path, str]
    captures: list[UnitDir] = field(default_factory=list)
    """The directories the harvest captures, those that exist, hold counters and keep a
    product source, each with its resolved toolchain."""


@dataclass(frozen=True)
class UnitPlan:
    """Every unit view of a report, validated before anything is captured."""

    views: list[UnitView]
    resolver: SourceResolver
    """The quiet resolver the plan was built with; the harvest reuses its walk of the tree."""


_BUILD_IT = (
    "the unit tier reports product sources only; declare the products and build them with "
    "coverage instrumentation (--coverage) before collecting unit coverage."
)


async def plan_unit_views(
    tier_configs: "list[TierConfig]",
    project_root: Path,
    project_name: str | None,
    cache: ObjectListCache,
    resolver: SourceResolver,
    index: GcnoIndex,
) -> UnitPlan:
    """Select each unit view's products, its directories' sources and their toolchains.

    Plain ``harvest_dirs`` filter by every product of *project_name* in the
    active lab, a ``products`` entry by the product of its name; a product
    declared ``instrumented = false`` takes no part. With no *project_name*
    no product is selected, so every view is refused. Each directory the
    harvest will capture gets its kept sources and its full toolchain here:
    every unit tier's configured tools are resolved first, whether or not
    its directories are captured; a directory that is missing, holds no
    counters or keeps no product source is skipped with one log line. Nothing
    is captured here, so every refusal precedes every capture of the report.

    Raises:
        otto.coverage.errors.CoverageNotInstrumentedError: a view selects no
            product, or a selected product's artifact is absent, missing or
            holds no coverage paths.
        otto.coverage.errors.CaptureFailedError: a selected product's object
            list could not be read (an operational failure).
        otto.coverage.errors.CoverageInputError: a selected image's container
            is not running, a configured tool cannot be resolved, or a
            directory's notes contradict the configured reader or each other
            (``field=None``).
        otto.host.errors.CoverageToolMissingError: a default tool a
            directory's data needs is not on ``PATH``.
    """
    from ..run_context import try_get_context
    from .unit_toolchain import UnitToolchains, resolve_configured

    configured = {
        t.name: resolve_configured(t, project_root) for t in tier_configs if t.kind == "unit"
    }
    tiers = [t for t in tier_configs if t.kind == "unit" and (t.harvest_dirs or t.products)]
    if not tiers:
        return UnitPlan(views=[], resolver=resolver)
    toolchains = UnitToolchains()
    ctx = try_get_context()
    hosts: "list[Host]" = list(ctx.lab.hosts.values()) if ctx is not None else []
    # No project name selects nothing: an owner-less product is no project's.
    owned = [
        _Selected(host, product)
        for host in hosts
        for product in host.products
        if project_name is not None and product.owner == project_name
    ]
    lab = ctx.lab.name if ctx is not None else None
    views: list[UnitView] = []
    for tier in tiers:
        entries = ([("", tier.harvest_dirs)] if tier.harvest_dirs else []) + list(
            tier.products.items()
        )
        for name, dirs in entries:
            label = f"{tier.name}/{name}" if name else tier.name
            candidates = [s for s in owned if not name or s.product.name == name]
            selected = [
                s
                for s in candidates
                if getattr(s.product, "instrumented_override", None) is not False
            ]
            if not selected:
                raise _no_products(label, project_name, lab, name, declared_false=bool(candidates))
            owners: dict[Path, str] = {}
            for s in selected:
                listed = await _read_objects(s.product, s.host, cache)
                if isinstance(listed, Unavailable):
                    _refuse(label, s.product, listed)
                    continue
                for rel in sorted(product_source_set(listed, project_root, resolver, index)):
                    owners.setdefault(rel, s.product.name)
            anchored = [anchor_path(d, project_root) for d in dirs]
            captures = []
            for idx, hdir in enumerate(anchored):
                selection = _select_dir(label, hdir, project_root, resolver, owners)
                if selection is None:
                    continue
                toolchain = toolchains.resolve(configured[tier.name], label, hdir, selection.notes)
                captures.append(
                    UnitDir(index=idx, path=hdir, selection=selection, toolchain=toolchain)
                )
            views.append(
                UnitView(
                    tier=tier.name,
                    product=name,
                    label=label,
                    dirs=anchored,
                    owners=owners,
                    captures=captures,
                )
            )
    return UnitPlan(views=views, resolver=resolver)


def _select_dir(
    label: str, hdir: Path, project_root: Path, resolver: SourceResolver, owners: dict[Path, str]
) -> UnitSelection | None:
    """Return *hdir*'s kept sources, or ``None`` (with one log line) when it is not captured."""
    if not hdir.is_dir():
        logger.warning("Unit tier %s: harvest dir does not exist: %s — skipping", label, hdir)
        return None
    gcda_files = list(hdir.rglob("*.gcda"))
    if not gcda_files:
        logger.warning(
            "Unit tier %s: harvest dir has no .gcda files: %s — run the unit tests first; skipping",
            label,
            hdir,
        )
        return None
    _warn_if_stale_counters(label, hdir, gcda_files)
    selection = select_unit_sources(hdir, project_root, resolver, owners)
    logger.info(
        "unit %s: kept %d product source files, dropped %d not in any product",
        label,
        len(selection.kept),
        len(selection.dropped),
    )
    for rel, owner in selection.kept.items():
        logger.debug("unit %s: keep %s (product %s)", label, rel.as_posix(), owner)
    for dropped in selection.dropped:
        logger.debug("unit %s: drop %s (not in any product)", label, dropped)
    if not selection.kept:
        logger.info("unit %s: no product source files under %s; nothing to capture", label, hdir)
        return None
    return selection


def _warn_if_stale_counters(label: str, hdir: Path, gcda_files: list[Path]) -> None:
    """Warn (but still load) when counters look older than the build notes."""
    gcno_files = list(hdir.rglob("*.gcno"))
    if not gcno_files:
        return
    newest_gcda = max(p.stat().st_mtime for p in gcda_files)
    newest_gcno = max(p.stat().st_mtime for p in gcno_files)
    if newest_gcda < newest_gcno:
        logger.warning(
            "Unit tier %s: newest .gcda under %s predates newest .gcno — "
            "counters may be stale (loading anyway).",
            label,
            hdir,
        )


@dataclass(frozen=True)
class _Selected:
    host: "Host"
    product: "Product"


async def _read_objects(
    product: "Product", host: "Host", cache: ObjectListCache
) -> ObjectList | Unavailable:
    """Return *product*'s object list: the one place the unit tier reads one.

    An image named by reference has no local artifact, so its list is read
    from its running container on *host*, the lab host it is installed on.
    """
    if getattr(product, "is_tarball", None) is False:
        return await cache.read_running(product, host)
    return cache.read(product)


def _no_products(
    label: str, project: str | None, lab: str | None, name: str, *, declared_false: bool
) -> Exception:
    from .errors import CoverageNotInstrumentedError

    if project is None:
        what = "no project is named, so no products are selected"
    elif lab is None:
        what = f"no products of project {project} found: no lab is active"
    elif declared_false and name:
        what = f"product {name!r} of project {project} is declared instrumented = false"
    elif declared_false:
        what = f"every product of project {project} is declared instrumented = false"
    elif name:
        what = f"no product named {name!r} of project {project} found in the active lab ({lab})"
    else:
        what = f"no products of project {project} found in the active lab ({lab})"
    return CoverageNotInstrumentedError(f"unit tier {label}: {what} — {_BUILD_IT}")


def _refuse(label: str, product: "Product", listed: Unavailable) -> None:
    """Raise the refusal *listed* calls for; return only for a dry run."""
    from .errors import CaptureFailedError, CoverageInputError, CoverageNotInstrumentedError

    name = product.name
    if listed.kind == "dry-run":
        logger.debug("unit tier %s: product %r skipped: %s", label, name, listed.reason)
        return
    if listed.kind in ("no-artifact", "missing", "not-instrumented"):
        artifact = getattr(product, "artifact", None) or "no local artifact"
        raise CoverageNotInstrumentedError(
            f"unit tier {label}: product {name!r} ({artifact}) is not instrumented: "
            f"{listed.reason}. Build it with coverage instrumentation (--coverage) before "
            "collecting unit coverage, or declare it instrumented = false."
        )
    if listed.kind == "not-running":
        container = getattr(product, "container_name", None) or name
        raise CoverageInputError(
            f"unit tier {label}: product {name!r} reads its object list from container "
            f"{container!r}, which is not running — install the product first (otto run install).",
            field=None,
        )
    raise CaptureFailedError(
        f"unit tier {label}: could not read product {name!r}'s object list: {listed.reason}"
    )
