"""Inter-project dependency resolution: statuses, satisfiability, skip set, ordering.

Runs inside ``bootstrap()`` between phase-1 discovery and phase-2
registration. Everything here is index-based over the discovered project list
(``Project`` defines ``__eq__`` via dataclass, so instances are unhashable).
"""

import bisect
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from ..models.dependencies import DependencyClause, clauses_satisfiable, normalize_name
from .scope import BootstrapWarning, DependencyError

if TYPE_CHECKING:
    from ..models.dependencies import ParsedDependency
    from .project import Project
    from .version import Version

# Named DepStatus, not Status: the core `otto.utils.Status` enum is a
# different thing entirely, and the outcome-convention gate
# (.ast-grep/rules/no-bare-status-return.yml) matches the bare name in a
# return annotation — a collision here would fire it on correct code with
# advice that makes no sense for a resolution verdict.
DepStatus = Literal["satisfied", "missing", "incompatible"]

_MIN_CONFLICTING_ENTRIES = 2
"""Below this, a single declared entry was already proven satisfiable at settings-parse time."""


@dataclass(frozen=True)
class ResolvedDependency:
    """Resolution outcome for one declared dependency of one project."""

    name: str
    """Dependency name as declared."""

    normalized: str
    """PEP-503-normalized name used for matching."""

    constraint: str
    """Raw clause text (``""`` = any version)."""

    required: bool
    status: DepStatus
    provider_version: "Version | None"
    """The providing project's version; ``None`` for ``missing``."""


@dataclass(frozen=True)
class _StatusOutcome:
    """Intermediate product of the status pass; the ordering pass builds on it."""

    errors: list[DependencyError]
    warnings: list[BootstrapWarning]
    skip_reason: dict[int, str]
    required_edges: set[tuple[int, int]]
    soft_edges: list[tuple[int, int]]
    satisfied_optionals: list[tuple[int, int, "ParsedDependency"]]
    """``(provider, dependent, dep)`` for every OPTIONAL dep that was
    ``"satisfied"`` at status-pass time -- before the skip set was finalized.
    Lets the caller warn when a provider is later skipped even though its
    dependent's optional dep still reads "satisfied"."""


def _resolve_statuses(projects: "list[Project]") -> _StatusOutcome:
    """Resolve every declared dep; populate ``project.dependencies`` on each project."""
    errors: list[DependencyError] = []
    warnings: list[BootstrapWarning] = []
    skip_reason: dict[int, str] = {}
    required_edges: set[tuple[int, int]] = set()
    soft_edges: list[tuple[int, int]] = []
    satisfied_optionals: list[tuple[int, int, "ParsedDependency"]] = []

    providers: dict[str, list[int]] = {}
    for i, project in enumerate(projects):
        providers.setdefault(normalize_name(project.name), []).append(i)

    for i, project in enumerate(projects):
        resolved: list[ResolvedDependency] = []
        for dep in project.declared_dependencies:
            status, version = _resolve_one(
                i,
                dep,
                projects,
                providers,
                errors,
                warnings,
                required_edges,
                soft_edges,
                satisfied_optionals,
            )
            resolved.append(
                ResolvedDependency(
                    name=dep.name,
                    normalized=dep.normalized,
                    constraint=dep.constraint,
                    required=dep.required,
                    status=status,
                    provider_version=version,
                )
            )
            if dep.required and status != "satisfied" and i not in skip_reason:
                skip_reason[i] = f"dependency {dep.raw!r} ({status})"
        project.dependencies = resolved

    _check_combined_satisfiability(projects, errors, skip_reason)
    return _StatusOutcome(
        errors=errors,
        warnings=warnings,
        skip_reason=skip_reason,
        required_edges=required_edges,
        soft_edges=soft_edges,
        satisfied_optionals=satisfied_optionals,
    )


def _resolve_one(
    i: int,
    dep: "ParsedDependency",
    projects: "list[Project]",
    providers: dict[str, list[int]],
    errors: list[DependencyError],
    warnings: list[BootstrapWarning],
    required_edges: set[tuple[int, int]],
    soft_edges: list[tuple[int, int]],
    satisfied_optionals: list[tuple[int, int, "ParsedDependency"]],
) -> "tuple[DepStatus, Version | None]":
    """Status + provider version for one dep; append its error/warning/edge."""
    project = projects[i]
    candidates = providers.get(dep.normalized, [])
    if not candidates:
        if dep.required:
            errors.append(
                DependencyError(
                    project.root,
                    f"dependency {dep.raw!r} is not satisfied: no project named "
                    f"{dep.name!r} in OTTOPATH",
                )
            )
        return "missing", None
    provider_idx = candidates[0]
    version = projects[provider_idx].version
    if all(c.matches(version.key) for c in dep.clauses):
        if dep.required:
            required_edges.add((provider_idx, i))
        else:
            soft_edges.append((provider_idx, i))
            satisfied_optionals.append((provider_idx, i, dep))
        return "satisfied", version
    if dep.required:
        errors.append(
            DependencyError(
                project.root,
                f"dependency {dep.raw!r} is not satisfied: found {dep.name} {version}",
            )
        )
    else:
        warnings.append(
            BootstrapWarning(
                project.root,
                f"project root {project.root}: optional dependency {dep.raw!r} not satisfied "
                f"(found {version}) — feature disabled",
            )
        )
    return "incompatible", version


def _check_combined_satisfiability(
    projects: "list[Project]",
    errors: list[DependencyError],
    skip_reason: dict[int, str],
) -> None:
    """Error every project whose required constraints on a name can NEVER all hold."""
    by_name: dict[str, list[tuple[int, "ParsedDependency"]]] = {}
    for i, project in enumerate(projects):
        for dep in project.declared_dependencies:
            if dep.required:
                by_name.setdefault(dep.normalized, []).append((i, dep))
    for _norm, entries in sorted(by_name.items()):
        if len(entries) < _MIN_CONFLICTING_ENTRIES:
            continue  # single entries were proven satisfiable at settings parse
        combined: list[DependencyClause] = [c for _, dep in entries for c in dep.clauses]
        if clauses_satisfiable(combined):
            continue
        detail = ", ".join(
            f"{projects[i].name} requires {(dep.constraint or 'any')!r}" for i, dep in entries
        )
        seen: set[int] = set()
        for i, dep in entries:
            if i in seen:
                continue
            seen.add(i)
            errors.append(
                DependencyError(
                    projects[i].root,
                    f"no possible version of {dep.name!r} satisfies all required "
                    f"constraints: {detail}",
                )
            )
            skip_reason.setdefault(i, f"unsatisfiable combined constraints on {dep.name!r}")


@dataclass(frozen=True)
class ResolutionOutcome:
    """Everything the dependency pass produced for ``bootstrap()``."""

    ordered: "list[Project]"
    """Non-skipped projects in registration order (stable topo sort, project-root tie-break)."""

    errors: list[DependencyError]
    warnings: list[BootstrapWarning]


def resolve_dependencies(projects: "list[Project]") -> ResolutionOutcome:
    """Resolve declared dependencies; return ordering, errors, warnings.

    Side effect: populates ``project.dependencies`` on every project (including
    skipped ones — the statuses are the diagnostic).
    """
    status = _resolve_statuses(projects)
    errors = list(status.errors)
    warnings = list(status.warnings)
    skip_reason = dict(status.skip_reason)

    _propagate_skips(projects, status.required_edges, skip_reason, errors)
    alive = [i for i in range(len(projects)) if i not in skip_reason]
    survivors = _skip_required_cycles(projects, alive, status.required_edges, skip_reason, errors)
    # skip_reason is now final (propagation + cycle skips both landed): warn
    # about any non-skipped project whose satisfied optional dep's provider
    # nonetheless never registers.
    warnings.extend(_skipped_provider_warnings(projects, status.satisfied_optionals, skip_reason))
    order = _stable_topo_order(survivors, status.required_edges, status.soft_edges)
    return ResolutionOutcome(
        ordered=[projects[i] for i in order],
        errors=errors,
        warnings=warnings,
    )


def _skipped_provider_warnings(
    projects: "list[Project]",
    satisfied_optionals: list[tuple[int, int, "ParsedDependency"]],
    skip_reason: dict[int, str],
) -> list[BootstrapWarning]:
    """Warn when a satisfied optional dependency's provider ends up skipped.

    Each entry in *satisfied_optionals* was ``"satisfied"`` during the status
    pass, before the skip set was finalized. If its provider is later
    skipped (its own required deps unsatisfied, or a required-dependency
    cycle) it never registers, so the dependent's "feature enabled" signal
    — still reading ``"satisfied"`` in ``project.dependencies`` — is stale. This
    does not change the stored status (it describes the discovered set, not
    registration success); it only surfaces the gap as a startup warning.
    """
    out: list[BootstrapWarning] = []
    for provider, dependent, dep in satisfied_optionals:
        if dependent in skip_reason or provider not in skip_reason:
            continue
        out.append(
            BootstrapWarning(
                projects[dependent].root,
                f"project root {projects[dependent].root}: optional dependency {dep.raw!r} "
                f"unavailable — provider project root {projects[provider].root} was skipped "
                "— feature disabled",
            )
        )
    return out


def _propagate_skips(
    projects: "list[Project]",
    required_edges: set[tuple[int, int]],
    skip_reason: dict[int, str],
    errors: list[DependencyError],
) -> None:
    """BFS along satisfied-required edges: a dependent of a skipped project is skipped."""
    dependents: dict[int, list[int]] = {}
    for provider, dependent in sorted(required_edges):
        dependents.setdefault(provider, []).append(dependent)
    queue = sorted(skip_reason)
    while queue:
        current = queue.pop(0)
        for dependent in dependents.get(current, []):
            if dependent in skip_reason:
                continue
            skip_reason[dependent] = skip_reason[current]
            errors.append(
                DependencyError(
                    projects[dependent].root,
                    f"skipped: required dependency {projects[current].name!r} "
                    f"(project root {projects[current].root}) was skipped — "
                    f"root cause: {skip_reason[current]}",
                )
            )
            queue.append(dependent)


def _skip_required_cycles(
    projects: "list[Project]",
    alive: list[int],
    required_edges: set[tuple[int, int]],
    skip_reason: dict[int, str],
    errors: list[DependencyError],
) -> list[int]:
    """Kahn over required edges; leftover nodes are in (or downstream of) a cycle."""
    indeg = dict.fromkeys(alive, 0)
    out: dict[int, list[int]] = {i: [] for i in alive}
    preds: dict[int, list[int]] = {i: [] for i in alive}
    for provider, dependent in sorted(required_edges):
        if provider in indeg and dependent in indeg:
            out[provider].append(dependent)
            preds[dependent].append(provider)
            indeg[dependent] += 1
    ready = sorted(i for i in alive if indeg[i] == 0)
    visited: set[int] = set()
    while ready:
        current = ready.pop(0)
        visited.add(current)
        for dependent in out[current]:
            indeg[dependent] -= 1
            if indeg[dependent] == 0:
                bisect.insort(ready, dependent)
    leftover = [i for i in alive if i not in visited]
    if not leftover:
        return alive
    in_cycle: set[int] = set()
    for cycle in _find_cycles(leftover, preds):
        in_cycle.update(cycle)
        path = " -> ".join(projects[i].name for i in [*cycle, cycle[0]])
        for i in cycle:
            skip_reason[i] = f"required dependency cycle: {path}"
            errors.append(DependencyError(projects[i].root, f"required dependency cycle: {path}"))
    for i in leftover:
        if i not in in_cycle:
            skip_reason[i] = "downstream of a required dependency cycle"
            errors.append(
                DependencyError(
                    projects[i].root,
                    "skipped: a required dependency is part of a dependency cycle "
                    "(see cycle errors above)",
                )
            )
    return [i for i in alive if i not in skip_reason]


def _find_cycles(leftover: list[int], preds: dict[int, list[int]]) -> list[list[int]]:
    """Walk predecessors within *leftover* to find cycles.

    Every node there has one, so a walk must revisit its own path; the
    revisited segment is a cycle. ``preds[node]`` are node's *required*
    providers, so walking it already moves in the "requires" direction
    (node -> what it requires) -- the path needs no reversal for display:
    ``a requires b, b requires c, c requires a`` walks as ``[a, b, c]`` and
    renders ``"a -> b -> c -> a"``. Each node lands in at most one cycle.
    """
    leftover_set = set(leftover)
    assigned: set[int] = set()
    cycles: list[list[int]] = []
    for start in leftover:
        if start in assigned:
            continue
        path: list[int] = []
        on_path: dict[int, int] = {}
        current = start
        while current not in assigned and current not in on_path:
            on_path[current] = len(path)
            path.append(current)
            current = next(p for p in preds[current] if p in leftover_set)
        if current in on_path:
            cycle = path[on_path[current] :]
            cycles.append(cycle)
            assigned.update(path)
        else:
            assigned.update(path)
    return cycles


def _stable_topo_order(
    survivors: list[int],
    required_edges: set[tuple[int, int]],
    soft_edges: list[tuple[int, int]],
) -> list[int]:
    """Kahn with a project-root-ordered ready queue; soft edges dropped if cycle-closing."""
    graph: dict[int, set[int]] = {i: set() for i in survivors}
    for provider, dependent in sorted(required_edges):
        if provider in graph and dependent in graph:
            graph[provider].add(dependent)
    for provider, dependent in soft_edges:  # already (dependent project-root, declaration) order
        if provider not in graph or dependent not in graph:
            continue
        if _reachable(graph, start=dependent, target=provider):
            continue  # soft edge would close a cycle — drop silently
        graph[provider].add(dependent)
    indeg = dict.fromkeys(survivors, 0)
    for _provider, dependent in ((p, d) for p, targets in graph.items() for d in targets):
        indeg[dependent] += 1
    ready = sorted(i for i in survivors if indeg[i] == 0)
    order: list[int] = []
    while ready:
        current = ready.pop(0)
        order.append(current)
        for dependent in sorted(graph[current]):
            indeg[dependent] -= 1
            if indeg[dependent] == 0:
                bisect.insort(ready, dependent)
    return order


def _reachable(graph: dict[int, set[int]], *, start: int, target: int) -> bool:
    """Return whether *target* is reachable from *start* along *graph* edges."""
    stack = [start]
    seen = {start}
    while stack:
        node = stack.pop()
        if node == target:
            return True
        for nxt in graph.get(node, ()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return False
