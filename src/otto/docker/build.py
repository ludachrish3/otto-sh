"""
Docker image building.

The public entry point is :func:`build_images`. It can be called from the
CLI (``otto docker build``) and directly from instructions/suites; both
share the exact same code path so semantics never diverge.
"""

import logging
import shlex
from collections.abc import Iterable
from dataclasses import dataclass, field

from ..config.repo import DockerImage, Repo
from ..host.host import Host, is_dry_run
from ..result import CommandNotRunError, CommandResult
from ..utils import Status
from .reports import ImageBuild
from .staging import (
    image_archive_path,
    image_build_dir,
    stage_image_archive,
    stage_image_context,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BuildOptions:
    """The ``docker build`` flags a caller may add; each is docker's own."""

    tags: list[str] = field(default_factory=list)
    """``-t`` references, as typed. Empty: the image is tagged ``<name>:latest``."""
    no_cache: bool = False
    """``--no-cache``."""
    pull: bool = False
    """``--pull``."""
    build_args: dict[str, str] = field(default_factory=dict)
    """``--build-arg`` pairs, added to the declared ones; a repeated key wins."""
    target: str | None = None
    """``--target``, replacing the declared one."""


def image_references(image: DockerImage, options: BuildOptions) -> list[str]:
    """Return the references this build tags: the typed ones, or ``<name>:latest``."""
    return list(options.tags) or [f"{image.name}:latest"]


def build_command(project: str, image: DockerImage, options: BuildOptions) -> str:
    """Render the one ``docker build`` command for *image*; pure, so a dry run can show it.

    A directory context is built from its staged copy. An archive context is
    given to docker on stdin, unopened, and its Dockerfile is named by the
    path it has inside the archive.
    """
    if image.is_archive:
        dockerfile_arg = image.dockerfile_in_archive
        context_arg = f"- < {shlex.quote(str(image_archive_path(project, image)))}"
    else:
        remote_ctx = image_build_dir(project, image.name)
        # Resolve the Dockerfile path relative to the staged context. If the
        # user-declared Dockerfile lives outside the original context, the tar
        # step put it at the root of remote_ctx under its basename.
        try:
            dockerfile_rel = image.dockerfile.relative_to(image.context).as_posix()
        except ValueError:
            dockerfile_rel = image.dockerfile.name
        dockerfile_arg = str(remote_ctx / dockerfile_rel)
        context_arg = shlex.quote(str(remote_ctx))

    flags: list[str] = []
    for reference in image_references(image, options):
        flags.extend(["-t", shlex.quote(reference)])
    flags.extend(["-f", shlex.quote(dockerfile_arg)])
    target = options.target or image.target
    if target:
        flags.extend(["--target", shlex.quote(target)])
    merged = {**dict(image.build_args), **options.build_args}
    for arg_name, arg_value in merged.items():
        flags.extend(["--build-arg", shlex.quote(f"{arg_name}={arg_value}")])
    if options.no_cache:
        flags.append("--no-cache")
    if options.pull:
        flags.append("--pull")
    return f"docker build {' '.join(flags)} {context_arg}"


_ID_FORMAT = "{{.Id}}"
_LIST_FORMAT = "{{.Repository}}:{{.Tag}} {{.ID}}"


async def _read_back(
    parent: Host, references: list[str]
) -> "tuple[list[str], str | None, CommandResult]":
    """Ask the daemon what it lists for the image the first of *references* names.

    The id is the one ``docker images`` prints, and the references are the rows
    of ``docker images <reference>`` carrying that id: a reference with no tag
    part lists every tag of its repository, and only the just-built image's are
    this build's. Nothing here is composed by otto.
    """
    inspected = await parent.exec(
        f"docker image inspect --format {shlex.quote(_ID_FORMAT)} {shlex.quote(references[0])}"
    )
    if not inspected.status.is_ok:
        return [], None, inspected
    full_id = str(inspected.value).strip().removeprefix("sha256:")

    found: list[str] = []
    image_id: str | None = None
    listed = inspected
    for reference in references:
        listed = await parent.exec(
            f"docker images --format {shlex.quote(_LIST_FORMAT)} {shlex.quote(reference)}"
        )
        if not listed.status.is_ok:
            return [], None, listed
        for line in str(listed.value).splitlines():
            row_reference, _, row_id = line.strip().rpartition(" ")
            if row_reference and row_id and full_id.startswith(row_id):
                image_id = row_id
                if row_reference not in found:
                    found.append(row_reference)
    if not found:
        missing = CommandResult(
            Status.Failed,
            value=(
                f"docker build succeeded, but the daemon lists no image for {', '.join(references)}"
            ),
            command=listed.command,
            retcode=listed.retcode,
        )
        return [], None, missing
    return found, image_id, listed


async def _build_one(
    parent: Host, project: str, image: DockerImage, options: BuildOptions
) -> ImageBuild:
    """Build a single image on *parent* and read back what the daemon lists for it.

    Nothing is asked of the daemon first and nothing is skipped: docker's layer
    cache decides what a rebuild reuses. A failed build returns its own result
    whole, so ``value`` holds the captured output and ``command`` / ``retcode`` /
    ``timed_out`` survive; the daemon is asked only after a build that succeeded,
    and a build it does not list is a failure.
    """
    references = image_references(image, options)
    logger.info(rf"\[docker] building {references[0]}")
    # Unbounded on purpose: an image build/pull has no defensible bound, and a
    # made-up constant would be wrong on a slower builder. `inf` states that.
    if image.is_archive:
        from ..host.connections import teardown_step  # lazy: keeps the import budget

        remote = await stage_image_archive(parent, project, image)
        try:
            result = await parent.exec(build_command(project, image, options), timeout=float("inf"))
        finally:
            # Best effort: a failed removal must not replace the build's own outcome.
            with teardown_step(parent.id, "build archive removal"):
                removed = await parent.exec(f"rm -f {shlex.quote(str(remote))}")
                if not removed.is_ok:
                    logger.warning(
                        f"{parent.id}: could not remove the uploaded archive {remote}: "
                        f"{removed.value.strip()}"
                    )
    else:
        await stage_image_context(parent, project, image)
        result = await parent.exec(build_command(project, image, options), timeout=float("inf"))
    if not result.is_ok:
        return ImageBuild(image.name, [], None, result)
    found, image_id, read = await _read_back(parent, references)
    if image_id is None:
        return ImageBuild(image.name, [], None, read)
    return ImageBuild(image.name, found, image_id, result)


async def build_images(
    repo: Repo,
    parent: Host,
    *,
    image_names: Iterable[str] | None = None,
    options: BuildOptions | None = None,
) -> dict[str, ImageBuild]:
    """Build all (or selected) images for *repo* on *parent*.

    Args:
        repo: The :class:`~otto.config.repo.Repo` whose ``[docker]`` settings
            declare the images.
        parent: A docker-capable lab host. Builds happen here.
        image_names: Optional filter — only build images whose ``name`` is
            in this iterable. ``None`` builds everything declared.
        options: The ``docker build`` flags to add; ``None`` adds none.

    Returns:
        Mapping of image name to its :class:`~otto.docker.reports.ImageBuild`:
        the references and id the daemon lists for the built image, or a
        failing ``result`` returned whole, so ``value`` holds the captured
        output and ``command`` / ``retcode`` are preserved.

    Raises:
        ~otto.result.CommandNotRunError: this is a dry run, and at least one
            image was selected. A dry run builds nothing, so there is no
            ``docker build`` result to return -- see the arm below.
    """
    settings = repo.docker_settings
    if not settings.images:
        return {}

    selected = (
        [img for img in settings.images if img.name in set(image_names)]
        if image_names is not None
        else list(settings.images)
    )

    # Below the pure filtering, above every device touch. An empty selection
    # still answers {} because "this repo declares no such image" is settled
    # from configuration and is equally true in a dry run; a NON-empty one has
    # no honest answer at all. Every value in the dict is the result of a
    # `docker build` that ran on the parent, and under a dry run none did --
    # so a synthesized dict would fabricate one verdict per image. Raising
    # here rather than at the first `exec` is what makes the message name the
    # BUILD; the decline reached from `stage_image_context` names an `rm -rf`,
    # which is the wrong story told at the wrong altitude.
    if selected and is_dry_run():
        raise CommandNotRunError(
            f"build_images({repo.name}: {', '.join(img.name for img in selected)})",
            parent.id,
            "No context was staged and no image was built.",
        )

    results: dict[str, ImageBuild] = {}
    for image in selected:
        results[image.name] = await _build_one(parent, repo.name, image, options or BuildOptions())
    return results
