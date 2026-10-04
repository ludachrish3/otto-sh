"""The build verbs: ``build_on`` (image-level) and ``compose_build`` (use-case).

Both compose :func:`~otto.docker.build.build_images` and own every rule about
their inputs, so ``otto docker build`` / ``otto docker compose build`` and a
Python caller get identical refusals and identical reports. ``build_on``
takes a PARENT and places nothing: an image is built on the daemon that will
run its container, and with no use-case in play the caller names that daemon
(or leaves it to the one default-parent rule).
``compose_build`` places the winners of a use-case through the same
``resolve_use_case`` call ``deploy`` and ``teardown`` make, so a use-case build lands
on exactly the hosts a deployment would use.
"""

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from ..host.host import is_dry_run
from ..host.unix_host import UnixHost
from ..result import CommandNotRunError
from . import deployment
from .build import BuildOptions, build_command, build_images
from .observe import DockerVerbError, docker_parent
from .reports import BuildReport, RepoBuild
from .staging import stage_key

if TYPE_CHECKING:
    from ..config.repo import Repo
    from .resolve import Displacement


def _select_docker_repos(repo: "str | None") -> "list[Repo]":
    """Every loaded repo with a ``[docker]`` section, narrowed to *repo* when given."""
    from ..config.bootstrapped import get_repos

    docker_repos = [
        r
        for r in get_repos()
        if r.docker_settings.images or r.docker_settings.composes or r.docker_settings.use_cases
    ]
    if repo is None:
        return docker_repos
    matches = [r for r in docker_repos if r.name == repo]
    if not matches:
        raise DockerVerbError(
            f"repo {repo!r} is not a loaded repo with a [docker] section; docker repos: "
            f"{sorted(r.name for r in docker_repos)}",
            field="repo",
        )
    return matches


def _check_images(repos: "Sequence[Repo]", images: "Sequence[str] | None") -> None:
    """Refuse a selection that cannot be built as asked.

    The refusals: an unknown image name; a selection with nothing to build; an
    archive context that is not a file; one image name declared by more than
    one selected repo (the name is the name docker sees); and two selected
    images of one repo whose names flatten to one staging directory key
    (staging is per repo, so only a repo's own images can share a directory).
    """
    declared = {img.name for r in repos for img in r.docker_settings.images}
    if images is not None:
        unknown = sorted(set(images) - declared)
        if unknown:
            raise DockerVerbError(
                f"no selected repo declares an image named {', '.join(map(repr, unknown))}; "
                f"declared: {sorted(declared)}",
                field="images",
            )
    for r in repos:
        for img in r.docker_settings.images:
            if images is not None and img.name not in images:
                continue
            if img.is_archive and not img.context.is_file():
                raise DockerVerbError(
                    f"image {img.name!r} of repo {r.name!r}: the context archive "
                    f"{img.context} is not a file",
                    field="images",
                )
    if not declared:
        raise DockerVerbError(
            f"nothing to build: none of {sorted(r.name for r in repos)} declares [[docker.images]]",
            field=None,
        )
    owners: "dict[str, list[str]]" = {}
    for r in repos:
        for img in r.docker_settings.images:
            if images is None or img.name in images:
                owners.setdefault(img.name, []).append(r.name)
    clashes = {name: who for name, who in owners.items() if len(who) > 1}
    if clashes:
        described = "; ".join(
            f"{name!r} by {', '.join(who)}" for name, who in sorted(clashes.items())
        )
        raise DockerVerbError(
            f"one image name is declared by more than one selected repo ({described}); "
            f"an image name is the name docker sees, so narrow the selection to one repo "
            f"or rename one declaration",
            field="images",
        )
    for r in repos:
        by_key: "dict[str, str]" = {}
        for img in r.docker_settings.images:
            if images is not None and img.name not in images:
                continue
            key = stage_key(img.name)
            other = by_key.setdefault(key, img.name)
            if other != img.name:
                raise DockerVerbError(
                    f"images {other!r} and {img.name!r} of repo {r.name!r} would share "
                    f"the staging directory key {key!r}; rename one declaration",
                    field="images",
                )


def _dependency_order(repos: "Sequence[Repo]") -> "list[Repo]":
    """*repos* in bootstrap dependency order; a repo the order omits sorts last."""
    from ..config.bootstrapped import get_ordered_repos

    order = {r.name: i for i, r in enumerate(get_ordered_repos())}
    return sorted(repos, key=lambda r: order.get(r.name, len(order)))


def _names_for(repo: "Repo", images: "Sequence[str] | None") -> "list[str] | None":
    """Return the subset of *images* this repo declares (None = all), in declared order."""
    declared = [img.name for img in repo.docker_settings.images]
    if images is None:
        return None
    wanted = set(images)
    return [n for n in declared if n in wanted]


def _check_tags(
    plan: "list[tuple[UnixHost, Repo, list[str] | None]]", tags: "Sequence[str] | None"
) -> None:
    """Refuse ``tags`` when the selection holds more than one image."""
    if not tags:
        return
    selected: "list[str]" = []
    for _parent, repo, names in plan:
        declared = [img.name for img in repo.docker_settings.images]
        selected.extend(declared if names is None else names)
    if len(selected) > 1:
        raise DockerVerbError(
            f"tags name one image, and {len(selected)} are selected ({', '.join(selected)}); "
            f"name the one image to tag",
            field="tag",
        )


async def _build_plan(
    plan: "list[tuple[UnixHost, Repo, list[str] | None]]",
    *,
    options: BuildOptions,
) -> "list[RepoBuild]":
    """Run *plan* in order: one ``build_images`` per (host, repo), each with *options*."""
    entries: "list[RepoBuild]" = []
    for parent, repo, names in plan:
        if not repo.docker_settings.images:
            entries.append(RepoBuild(repo.name, parent.id, "no_images"))
            continue
        if names is not None and not names:
            continue  # declares images, but none of the requested ones
        results = await build_images(repo, parent, image_names=names, options=options)
        entries.append(RepoBuild(repo.name, parent.id, "built", dict(results)))
    return entries


def _plan_text(
    plan: "list[tuple[UnixHost, Repo, list[str] | None]]",
    displaced: "Sequence[Displacement]" = (),
    *,
    options: BuildOptions,
) -> str:
    """Render a build plan the way ``deploy``'s dry run renders its own.

    Each selected image also gets the exact ``docker build`` command
    :func:`~otto.docker.build.build_command` renders for *options*, so the plan
    names what a real run would execute.

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
    displaced_text = ". ".join(d.describe() for d in displaced)
    displaced_note = f" Displaced: {displaced_text}." if displaced_text else ""
    commands = [
        f"on {parent.id}: {build_command(repo.name, img, options)}"
        for parent, repo, names in plan
        for img in repo.docker_settings.images
        if names is None or img.name in names
    ]
    command_note = f" Would run: {'; '.join(commands)}." if commands else ""
    return (
        f"Build plan: {per_host}.{displaced_note}{command_note} "
        f"No context was staged and no image was built."
    )


async def build_on(
    parent: "str | None" = None,
    *,
    repo: "str | None" = None,
    images: "Sequence[str] | None" = None,
    tags: "Sequence[str] | None" = None,
    no_cache: bool = False,
    pull: bool = False,
    build_args: "Mapping[str, str] | None" = None,
    target: "str | None" = None,
) -> BuildReport:
    """Build the selected repos' declared images on *parent* (``otto docker build``).

    Args:
        parent: The lab id of a docker-capable unix host to build on; ``None``
            is the default parent (spec §2).
        repo: Narrow the selection to this repo by name.
        images: Build only these declared ``[[docker.images]]`` names; each
            selected repo builds the subset it declares.
        tags: ``docker build -t``: the references to tag, as typed. They
            replace the default ``<name>:latest`` and nothing is added to them.
            A tag names one image, so more than one selected image refuses.
        no_cache: ``docker build --no-cache``.
        pull: ``docker build --pull``.
        build_args: ``docker build --build-arg`` pairs, added to the declared
            ones; a key given here wins over a declared one.
        target: ``docker build --target``, replacing a declared target.

    Returns:
        A :class:`~otto.docker.reports.BuildReport`, one entry per selected repo
        in dependency order; a repo declaring no images is a ``no_images`` entry.

    Raises:
        DockerVerbError: *parent* is not a docker-capable unix host in
            the active lab, or ``None`` and the one rule picks none; *repo*
            names no docker repo; an *images* name no selected repo declares;
            no selected repo declares any image; two selected repos declare one
            image name, or two selected images of one repo share a staging
            directory key; or *tags* is given with more than one image selected.
        ~otto.result.CommandNotRunError: this is a dry run; the message carries
            the whole plan.
    """
    from ..config.fleet import get_lab

    lab = get_lab()
    parent_host = docker_parent(lab, parent)
    repos = _dependency_order(_select_docker_repos(repo))
    _check_images(repos, images)
    plan = [(parent_host, r, _names_for(r, images)) for r in repos]
    _check_tags(plan, tags)
    options = BuildOptions(
        tags=list(tags or []),
        no_cache=no_cache,
        pull=pull,
        build_args=dict(build_args or {}),
        target=target,
    )
    if is_dry_run():
        raise CommandNotRunError(
            f"build_on({parent_host.id})", parent_host.id, _plan_text(plan, options=options)
        )
    return BuildReport(repos=await _build_plan(plan, options=options))


async def compose_build(
    use_case: str,
    *,
    parent: "str | None" = None,
    provide: "Mapping[str, str] | None" = None,
    images: "Sequence[str] | None" = None,
    no_cache: bool = False,
    pull: bool = False,
    build_args: "Mapping[str, str] | None" = None,
) -> BuildReport:
    """Build the images a deployment of *use_case* would use (``otto docker compose build``).

    The selection is :func:`~otto.docker.deployment.deploy`'s own: the winners
    of the provider competition, built on *parent* (the default parent when
    ``None``). A use-case build therefore lands on exactly the host ``deploy``
    would deploy to; a test pins the two against each other.

    Args:
        use_case: The declared use-case name.
        parent: The parent to build on, by host id; ``None`` is the lab's
            default parent by the one rule (spec §2).
        provide: ``capability -> repo`` overrides for the competition (§4).
        images: Build only these declared image names, over the winners.
        no_cache: ``docker build --no-cache``, for every image.
        pull: ``docker build --pull``, for every image.
        build_args: ``docker build --build-arg`` pairs, added to the declared
            ones of every image; a key given here wins over a declared one.

    Returns:
        A :class:`~otto.docker.reports.BuildReport`: hosts in sorted id order,
        each host's winners in dependency order, with the competition's
        displacements on ``displaced``.

    Raises:
        ~otto.docker.resolve.UseCaseResolutionError: a selection refusal,
            identical to ``deploy``'s.
        DockerVerbError: the parent refusal (identical to ``deploy``'s), an
            *images* name no winner declares, no winner declares any image, or
            two winners declare one image name (or two images of one winner
            share a staging directory key).
        ~otto.result.CommandNotRunError: this is a dry run; the message carries
            the whole plan.
    """
    # Helpers called through the module object (not `from .deployment
    # import ...`) so a test can patch `deployment.resolve_use_case` and have THIS
    # call see it -- the placement differential pins compose_build against
    # deploy by patching exactly that seam.
    resolution = deployment.resolve_use_case(use_case, parent=parent, provide=provide)
    plan: "list[tuple[UnixHost, Repo, list[str] | None]]" = []
    winners: "list[Repo]" = []
    seen: "set[str]" = set()
    for host_id in sorted(resolution.placed):
        parent_host = deployment.parent_for(resolution.lab, host_id)
        for unit in deployment._units(  # noqa: SLF001 — see seam note above
            deployment._ordered(resolution.placed[host_id], resolution.order)  # noqa: SLF001 — see seam note above
        ):
            plan.append((parent_host, unit.repo, _names_for(unit.repo, images)))
            if unit.repo.name not in seen:
                seen.add(unit.repo.name)
                winners.append(unit.repo)
    _check_images(winners, images)
    options = BuildOptions(no_cache=no_cache, pull=pull, build_args=dict(build_args or {}))
    if is_dry_run():
        raise CommandNotRunError(
            f"compose_build({use_case})",
            ", ".join(sorted(resolution.placed)),
            _plan_text(plan, resolution.selection.displaced, options=options),
        )
    return BuildReport(
        repos=await _build_plan(plan, options=options),
        displaced=list(resolution.selection.displaced),
    )
