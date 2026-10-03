"""Project selection and the bootstrap gate: who takes part, and whether a broken repo stops it."""

import dataclasses
from typing import TYPE_CHECKING, Literal

from .errors import ProjectSelectionError, RepoLoadError

if TYPE_CHECKING:
    from ..bootstrap import BootstrapError, BootstrapResult
    from ..config.repo import Repo


@dataclasses.dataclass(frozen=True)
class ProjectSelection:
    """The normalised ``include_projects`` / ``exclude_projects`` a run carries."""

    include: list[str] = dataclasses.field(default_factory=list)
    exclude: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass(frozen=True)
class DemotedRepo:
    """A load error that does not stop the run, because its repo is inactive before the lab."""

    error: "BootstrapError"
    repo: str
    """The repo's name as discovered."""
    project: str
    """The repo's normalised name, the spelling ``include_projects`` takes."""
    reason: 'Literal["excluded", "out_of_scope"]'
    labs: list[str] = dataclasses.field(default_factory=list)
    """The lab selection an ``"out_of_scope"`` verdict was made against."""

    @property
    def message(self) -> str:
        """The library's sentence for this demotion, in field names."""
        why = (
            f"exclude_projects {self.project}"
            if self.reason == "excluded"
            else f"not applicable to lab(s) [{', '.join(self.labs)}]"
        )
        return (
            f"repo {self.repo!r} failed to load, but is inactive for this run ({why})"
            " — continuing without it"
        )


@dataclasses.dataclass(frozen=True)
class RepoCheck:
    """What the bootstrap gate let through."""

    demoted: list[DemotedRepo] = dataclasses.field(default_factory=list)


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

    Separate from :func:`select_projects` because it needs no discovered repo:
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


def select_projects(
    repos: "list[Repo]", include: list[str], exclude: list[str]
) -> ProjectSelection:
    """Normalise and validate the project switches against the discovered repos.

    A name in both lists is refused first; then a name no discovered repo
    carries, with the closest known name as a suggestion. Both sides of the
    comparison are normalised, so ``My_Repo`` selects ``my-repo``. Give one
    project name per list item: a comma-separated string is one (unknown)
    name here, since the CLI's ``-I a,b`` comma-splits before this point.
    """
    import difflib

    from ..models.dependencies import normalize_name

    check_project_overlap(include, exclude)
    inc, exc = _normalised(include), _normalised(exclude)
    if not (inc or exc):
        return ProjectSelection()
    known = sorted({normalize_name(repo.name) for repo in repos})
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


def check_repos(
    result: "BootstrapResult", labs: list[str], selection: ProjectSelection
) -> RepoCheck:
    """Run the bootstrap gate, decided before the lab exists.

    An error is fatal when its repo is active, when ``include_projects`` names
    it, or when no discovered repo owns it (a ``settings.toml`` that failed to
    parse). Inactivity is the pre-lab projection
    (:func:`otto.config.scope.inactive_before_lab`): the explicit switches plus
    lab-name inference. A repo that is inactive only because it is
    host-starved cannot be seen before the lab, so its errors stay fatal.
    Bootstrap warnings never gate.

    *selection* must come from :func:`select_projects`: normalised, and with
    no name in both lists. ``include`` is tested before ``exclude`` here,
    which agrees with :func:`otto.config.scope.active`'s exclude-first order
    only because the two lists cannot overlap.
    """
    if not result.errors:
        return RepoCheck()
    from ..config.scope import inactive_before_lab
    from ..models.dependencies import normalize_name

    by_dir = {str(repo.sut_dir): repo for repo in result.repos}
    fatal: "list[BootstrapError]" = []
    demoted: list[DemotedRepo] = []
    for err in result.errors:
        repo = by_dir.get(str(err.sut_dir))
        if repo is None:
            fatal.append(err)
            continue
        name = normalize_name(repo.name)
        reason: Literal["excluded", "out_of_scope"]
        if name in selection.include:
            fatal.append(err)
            continue
        if name in selection.exclude:
            reason = "excluded"
        elif inactive_before_lab(repo.project_scope, labs or None):
            reason = "out_of_scope"
        else:
            fatal.append(err)
            continue
        demoted.append(
            DemotedRepo(error=err, repo=repo.name, project=name, reason=reason, labs=list(labs))
        )
    if fatal:
        raise RepoLoadError(fatal, demoted)
    return RepoCheck(demoted=demoted)
