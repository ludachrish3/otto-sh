"""Project selection and the bootstrap gate: who takes part, and whether a broken one stops it."""

import dataclasses
from typing import TYPE_CHECKING, Literal

from .errors import ProjectLoadError, ProjectSelectionError

if TYPE_CHECKING:
    from ..config.project import Project
    from ..config.scope import BootstrapError
    from ..declared import BootstrapResult


@dataclasses.dataclass(frozen=True)
class ProjectSelection:
    """The normalised ``include_projects`` / ``exclude_projects`` a run carries."""

    include: list[str] = dataclasses.field(default_factory=list)
    exclude: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass(frozen=True)
class DemotedProject:
    """A load error that does not stop the run, because its project is inactive before the lab."""

    error: "BootstrapError"
    name: str
    """The project's name as discovered."""
    normalized_name: str
    """The project's normalised name, the spelling ``include_projects`` takes."""
    reason: 'Literal["excluded", "out_of_scope"]'
    labs: list[str] = dataclasses.field(default_factory=list)
    """The lab selection an ``"out_of_scope"`` verdict was made against."""

    @property
    def message(self) -> str:
        """The library's sentence for this demotion, in field names."""
        why = (
            f"exclude_projects {self.normalized_name}"
            if self.reason == "excluded"
            else f"not applicable to lab(s) [{', '.join(self.labs)}]"
        )
        return (
            f"project {self.name!r} failed to load, but is inactive for this run ({why})"
            " — continuing without it"
        )


@dataclasses.dataclass(frozen=True)
class ProjectCheck:
    """What the bootstrap gate let through."""

    demoted: list[DemotedProject] = dataclasses.field(default_factory=list)


@dataclasses.dataclass(frozen=True)
class LoadErrorVerdicts:
    """Which bootstrap errors stop a run, and which only demote their project."""

    fatal: "list[BootstrapError]" = dataclasses.field(default_factory=list)
    """An active project's error, one ``include`` forces, or one no discovered project owns."""
    demoted: list[DemotedProject] = dataclasses.field(default_factory=list)
    """Errors of projects inactive before the lab: switched off, or outside its labs."""


def classify_load_errors(
    result: "BootstrapResult",
    labs: list[str],
    *,
    include: list[str],
    exclude: list[str],
) -> LoadErrorVerdicts:
    """Decide, before the lab exists, which of *result*'s errors are fatal.

    The one rule :func:`check_projects` and :func:`otto.config.scope.scopes_of`
    share. An error is owned by the project whose ``root`` matches by
    ``str()``. An unowned error (a ``settings.toml`` that would not parse) is
    fatal: that project's declaration cannot be known. An owned error is demoted
    when its project is switched off or inactive before the lab, and fatal
    otherwise, whatever its kind.

    *include* and *exclude* may be spelled any way (PEP 503 normalised here,
    as :func:`otto.config.scope.active` does), and a name in both counts as
    excluded, by the same exclusion-first order. *labs* are the run's component
    lab names and are NOT normalised: lab patterns fullmatch the exact name.
    Validating the names (unknown, overlapping) is :func:`select_projects`'s
    job, at the CLI and ``open_context`` boundary.
    """
    from ..config.scope import inactive_before_lab
    from ..models.dependencies import normalize_name

    included, excluded = set(_normalised(list(include))), set(_normalised(list(exclude)))
    by_dir = {str(project.root): project for project in result.projects}
    fatal: "list[BootstrapError]" = []
    demoted: list[DemotedProject] = []
    for err in result.errors:
        project = by_dir.get(str(err.root))
        if project is None:
            fatal.append(err)
            continue
        name = normalize_name(project.name)
        reason: Literal["excluded", "out_of_scope"]
        if name in excluded:
            reason = "excluded"
        elif name in included:
            fatal.append(err)
            continue
        elif inactive_before_lab(project.project_scope, labs or None):
            reason = "out_of_scope"
        else:
            fatal.append(err)
            continue
        demoted.append(
            DemotedProject(
                error=err, name=project.name, normalized_name=name, reason=reason, labs=list(labs)
            )
        )
    return LoadErrorVerdicts(fatal=fatal, demoted=demoted)


def _normalised(names: list[str]) -> list[str]:
    """PEP 503-normalise *names*, dropping repeats, keeping first-seen order."""
    from ..models.dependencies import normalize_name

    out: list[str] = []
    for name in names:
        norm = normalize_name(name)
        if norm not in out:
            out.append(norm)
    return out


def check_project_overlap(include: list[str], exclude: list[str]) -> None:
    """Refuse a project named in both ``include_projects`` and ``exclude_projects``.

    Separate from :func:`select_projects` because it needs no discovered project:
    the CLI refuses a contradictory command line as soon as it is parsed.
    """
    overlap = sorted(set(_normalised(include)) & set(_normalised(exclude)))
    if overlap:
        raise ProjectSelectionError(
            f"project(s) {', '.join(overlap)} appear in both include_projects and "
            "exclude_projects — pick one",
            field="include_projects",
            kind="overlap",
            names=overlap,
        )


def refused_names(errors: "list[BootstrapError]") -> list[str]:
    """Return the normalized names discovery refused because two roots claimed them."""
    from ..config.scope import DuplicateProjectError

    return sorted({e.name for e in errors if isinstance(e, DuplicateProjectError)})


def select_projects(
    projects: "list[Project]", include: list[str], exclude: list[str]
) -> ProjectSelection:
    """Normalise and validate the project switches against the discovered projects.

    A name in both lists is refused first; then a name no discovered project
    carries, with the closest known name as a suggestion. Both sides of the
    comparison are normalised, so ``My_Project`` selects ``my-project``. Give one
    project name per list item: a comma-separated string is one (unknown)
    name here, since the CLI's ``-I a,b`` comma-splits before this point.
    """
    return select_projects_with_refused(projects, include, exclude, refused=[])


def select_projects_with_refused(
    projects: "list[Project]", include: list[str], exclude: list[str], refused: list[str]
) -> ProjectSelection:
    """:func:`select_projects`, with the names discovery refused counted as known.

    A name in *refused* (see :func:`refused_names`) counts as known, so
    ``-I``/``-E`` naming a project with two roots reaches the duplicate-root
    error instead of 'no project'.
    """
    import difflib

    from ..models.dependencies import normalize_name

    check_project_overlap(include, exclude)
    inc, exc = _normalised(include), _normalised(exclude)
    if not (inc or exc):
        return ProjectSelection()
    known = sorted({normalize_name(project.name) for project in projects} | set(refused))
    for field, names in (("include_projects", inc), ("exclude_projects", exc)):
        for name in names:
            if name in known:
                continue
            close = difflib.get_close_matches(name, known, n=1)
            suggestion = close[0] if close else None
            hint = f" — did you mean {suggestion!r}?" if suggestion else ""
            raise ProjectSelectionError(
                f"no project {name!r}{hint}",
                field=field,
                kind="unknown",
                names=[name],
                suggestion=suggestion,
            )
    return ProjectSelection(include=inc, exclude=exc)


def check_projects(
    result: "BootstrapResult", labs: list[str], selection: ProjectSelection
) -> ProjectCheck:
    """Run the bootstrap gate, decided before the lab exists.

    An error is fatal when its project is active, when ``include_projects`` names
    it, or when no discovered project owns it (a ``settings.toml`` that failed to
    parse). Inactivity is the pre-lab projection
    (:func:`otto.config.scope.inactive_before_lab`): the explicit switches plus
    lab-name inference. A project that is inactive only because it is
    host-starved cannot be seen before the lab, so its errors stay fatal.
    Bootstrap warnings never gate.

    *selection* must come from :func:`select_projects`: normalised, and with
    no name in both lists. The decision itself is
    ``classify_load_errors``'s, which tests exclusion first, as
    :func:`otto.config.scope.active` does.
    """
    if not result.errors:
        return ProjectCheck()
    verdicts = classify_load_errors(
        result, labs, include=selection.include, exclude=selection.exclude
    )
    if verdicts.fatal:
        raise ProjectLoadError(verdicts.fatal, verdicts.demoted)
    return ProjectCheck(demoted=verdicts.demoted)
