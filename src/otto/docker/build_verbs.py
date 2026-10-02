"""The build verbs: ``build_on`` (image-level) and ``compose_build`` (use-case).

Both compose :func:`~otto.docker.build.build_images` and own every rule about
their inputs, so ``otto docker build`` / ``otto docker compose build`` and a
Python caller get identical refusals and identical reports. ``build_on``
takes a HOST and places nothing: an image is built on the daemon that will
run its container, and with no use-case in play the caller names that daemon.
``compose_build`` places the winners of a use-case through the same
``_resolve`` call ``deploy`` and ``teardown`` make, so a use-case build lands
on exactly the hosts a deployment would use.
"""

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from ..config import get_lab, get_ordered_repos, get_repos
from ..errors import FieldError
from ..host.host import is_dry_run
from ..host.unix_host import UnixHost
from ..result import CommandNotRunError
from . import deployment
from .build import build_images
from .reports import BuildReport, RepoBuild

if TYPE_CHECKING:
    from ..config.lab import Lab
    from ..config.repo import Repo
    from .resolve import Displacement


class DockerBuildError(FieldError, ValueError):
    """A build verb's input is unusable; nothing was touched.

    ``field`` names the offending parameter (``host``, ``repo``, ``images``)
    or is ``None`` for a refusal about the selection as a whole.
    """


def _capable_ids(lab: "Lab") -> "list[str]":
    return sorted(
        hid for hid, h in lab.hosts.items() if isinstance(h, UnixHost) and h.docker_capable
    )


def _docker_parent(lab: "Lab", host: "str | None") -> UnixHost:
    """Return the docker-capable unix host *host* names in *lab*, or refuse."""
    capable = _capable_ids(lab)
    if host is None:
        raise DockerBuildError(
            f"host is required; docker-capable hosts in lab {lab.name!r}: {capable}",
            field="host",
        )
    candidate = lab.hosts.get(host)
    if not isinstance(candidate, UnixHost) or not candidate.docker_capable:
        raise DockerBuildError(
            f"host {host!r} is not a docker-capable unix host in lab {lab.name!r}; "
            f"docker-capable hosts here: {capable}",
            field="host",
        )
    return candidate


def _select_docker_repos(repo: "str | None") -> "list[Repo]":
    """Every loaded repo with a ``[docker]`` section, narrowed to *repo* when given."""
    docker_repos = [
        r
        for r in get_repos()
        if r.docker_settings.images or r.docker_settings.composes or r.docker_settings.use_cases
    ]
    if repo is None:
        return docker_repos
    matches = [r for r in docker_repos if r.name == repo]
    if not matches:
        raise DockerBuildError(
            f"repo {repo!r} is not a loaded repo with a [docker] section; docker repos: "
            f"{sorted(r.name for r in docker_repos)}",
            field="repo",
        )
    return matches


def _check_images(repos: "Sequence[Repo]", images: "Sequence[str] | None") -> None:
    """Refuse an unknown image name, or a selection with nothing to build."""
    declared = {img.name for r in repos for img in r.docker_settings.images}
    if images is not None:
        unknown = sorted(set(images) - declared)
        if unknown:
            raise DockerBuildError(
                f"no selected repo declares an image named {', '.join(map(repr, unknown))}; "
                f"declared: {sorted(declared)}",
                field="images",
            )
    if not declared:
        raise DockerBuildError(
            f"nothing to build: none of {sorted(r.name for r in repos)} declares [[docker.images]]",
            field=None,
        )


def _dependency_order(repos: "Sequence[Repo]") -> "list[Repo]":
    """*repos* in bootstrap dependency order; a repo the order omits sorts last."""
    order = {r.name: i for i, r in enumerate(get_ordered_repos())}
    return sorted(repos, key=lambda r: order.get(r.name, len(order)))


def _names_for(repo: "Repo", images: "Sequence[str] | None") -> "list[str] | None":
    """Return the subset of *images* this repo declares (None = all), in declared order."""
    declared = [img.name for img in repo.docker_settings.images]
    if images is None:
        return None
    wanted = set(images)
    return [n for n in declared if n in wanted]


async def _build_plan(
    plan: "list[tuple[UnixHost, Repo, list[str] | None]]", *, rebuild: bool
) -> "list[RepoBuild]":
    """Run *plan* in order: one ``build_images`` per (host, repo)."""
    entries: "list[RepoBuild]" = []
    for parent, repo, names in plan:
        if not repo.docker_settings.images:
            entries.append(RepoBuild(repo.name, parent.id, "no_images"))
            continue
        if names is not None and not names:
            continue  # declares images, but none of the requested ones
        results = await build_images(repo, parent, image_names=names, rebuild=rebuild)
        entries.append(RepoBuild(repo.name, parent.id, "built", dict(results)))
    return entries


def _plan_text(
    plan: "list[tuple[UnixHost, Repo, list[str] | None]]",
    displaced: "Sequence[Displacement]" = (),
) -> str:
    """Render a build plan the way ``deploy``'s dry run renders its own.

    *displaced* is spec §6: ``compose_build`` places through the same
    provider competition ``deploy`` runs, so its dry run owes the same
    "Displaced: ..." clause, rendered exactly the way
    :func:`~otto.docker.deployment._plan` renders it. ``build_on`` places
    nothing and never has one to show, so it keeps the default ``()``.
    """
    by_host: "dict[str, list[str]]" = {}
    for parent, repo, names in plan:
        declared = [img.name for img in repo.docker_settings.images]
        chosen = declared if names is None else names
        if not chosen:
            continue
        by_host.setdefault(parent.id, []).append(f"{repo.name}[{','.join(chosen)}]")
    per_host = "; ".join(f"{host} <- {'; '.join(items)}" for host, items in by_host.items())
    displaced_text = "; ".join(
        f"{d.capability} -> {d.winner_repo} (priority {d.winner_priority}), "
        f"{d.loser_repo} (priority {d.loser_priority}) stands down"
        for d in displaced
    )
    displaced_note = f" Displaced: {displaced_text}." if displaced_text else ""
    return f"Build plan: {per_host}.{displaced_note} No context was staged and no image was built."


async def build_on(
    host: "str | None",
    *,
    repo: "str | None" = None,
    images: "Sequence[str] | None" = None,
    rebuild: bool = False,
) -> BuildReport:
    """Build the selected repos' declared images on *host* (``otto docker build``).

    Args:
        host: The lab id of a docker-capable unix host. Required: an image is
            built on the daemon that will run it, and with no use-case there is
            nothing else to derive the daemon from. ``None`` is accepted by the
            signature so the CLI can hand an omitted ``--on`` straight through,
            and is refused here, as the rule's owner.
        repo: Narrow the selection to this repo by name.
        images: Build only these declared ``[[docker.images]]`` names; each
            selected repo builds the subset it declares.
        rebuild: Skip the context-hash cache and always ``docker build``.

    Returns:
        A :class:`~otto.docker.reports.BuildReport`, one entry per selected repo
        in dependency order; a repo declaring no images is a ``no_images`` entry.

    Raises:
        DockerBuildError: *host* is missing or not a docker-capable unix host in
            the active lab; *repo* names no docker repo; an *images* name no
            selected repo declares; or no selected repo declares any image.
        ~otto.result.CommandNotRunError: this is a dry run; the message carries
            the whole plan.
    """
    lab = get_lab()
    parent = _docker_parent(lab, host)
    repos = _dependency_order(_select_docker_repos(repo))
    _check_images(repos, images)
    plan = [(parent, r, _names_for(r, images)) for r in repos]
    if is_dry_run():
        raise CommandNotRunError(f"build_on({parent.id})", parent.id, _plan_text(plan))
    return BuildReport(repos=await _build_plan(plan, rebuild=rebuild))


async def compose_build(
    use_case: str,
    *,
    on: "str | None" = None,
    provide: "Mapping[str, str] | None" = None,
    images: "Sequence[str] | None" = None,
    rebuild: bool = False,
) -> BuildReport:
    """Build the images a deployment of *use_case* would use (``otto docker compose build``).

    Placement is :func:`~otto.docker.deployment.deploy`'s own: the winners of
    the provider competition, placed by the engine, collapsed onto *on* when
    given. A use-case build therefore lands on exactly the hosts ``deploy``
    would deploy to; a test pins the two against each other.

    Args:
        use_case: The declared use-case name.
        on: Collapse every winner onto this lab host (spec §5 knob 1).
        provide: ``capability -> repo`` overrides for the competition (§4).
        images: Build only these declared image names, over the winners.
        rebuild: Skip the context-hash cache and always ``docker build``.

    Returns:
        A :class:`~otto.docker.reports.BuildReport`: hosts in sorted id order,
        each host's winners in dependency order, with the competition's
        displacements on ``displaced``.

    Raises:
        ~otto.docker.resolve.UseCaseResolutionError: a placement refusal,
            identical to ``deploy``'s.
        DockerBuildError: an *images* name no winner declares, or no winner
            declares any image.
        ~otto.result.CommandNotRunError: this is a dry run; the message carries
            the whole plan.
    """
    # Private helpers, called through the module object (not `from .deployment
    # import ...`) so a test can patch `deployment._resolve` and have THIS
    # call see it -- the placement differential pins compose_build against
    # deploy by patching exactly that seam.
    lab, selection, placed, order = deployment._resolve(  # noqa: SLF001 — see seam note above
        use_case, on=on, provide=provide
    )
    plan: "list[tuple[UnixHost, Repo, list[str] | None]]" = []
    winners: "list[Repo]" = []
    seen: "set[str]" = set()
    for host_id in sorted(placed):
        parent = deployment._parent_for(lab, host_id)  # noqa: SLF001 — see seam note above
        for unit in deployment._units(  # noqa: SLF001 — see seam note above
            deployment._ordered(placed[host_id], order)  # noqa: SLF001 — see seam note above
        ):
            plan.append((parent, unit.repo, _names_for(unit.repo, images)))
            if unit.repo.name not in seen:
                seen.add(unit.repo.name)
                winners.append(unit.repo)
    _check_images(winners, images)
    if is_dry_run():
        raise CommandNotRunError(
            f"compose_build({use_case})",
            ", ".join(sorted(placed)),
            _plan_text(plan, selection.displaced),
        )
    return BuildReport(
        repos=await _build_plan(plan, rebuild=rebuild), displaced=list(selection.displaced)
    )
