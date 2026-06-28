"""
Docker image building, with context-hash skipping.

The public entry point is :func:`build_images`. It can be called from the
CLI (``otto docker build``) and directly from instructions/suites; both
share the exact same code path so semantics never diverge.
"""

from __future__ import annotations

import shlex
from collections.abc import Iterable

from ..configmodule.repo import DockerImage, DockerSettings, Repo
from ..host.host import Host
from ..logger import get_otto_logger
from ..utils import Status
from ._context_hash import context_hash
from .staging import stage_image_context

logger = get_otto_logger()


_IMPLICIT_REGISTRIES = {"", "docker.io"}


def _tag_base(registry_url: str, project: str, image: DockerImage) -> str:
    """Return ``<project>-<image>``, optionally prefixed with a non-default registry.

    Bare names (no ``<registry>/`` prefix) are kept for the default registry
    so a user-authored ``compose.yml`` can reference the image as
    ``repo1-api:latest`` and resolve to what otto built locally. A
    non-default registry is included in the tag so push targets are explicit.
    """
    base = f"{project}-{image.name}"
    if registry_url and registry_url not in _IMPLICIT_REGISTRIES:
        base = f"{registry_url}/{base}"
    return base


def image_full_tag(registry_url: str, project: str, image: DockerImage, hash_hex: str) -> str:
    """Construct the ``<project>-<image>:<hash>`` tag (with registry prefix when non-default)."""
    return f"{_tag_base(registry_url, project, image)}:{hash_hex[:16]}"


def image_latest_tag(registry_url: str, project: str, image: DockerImage) -> str:
    return f"{_tag_base(registry_url, project, image)}:latest"


async def _image_exists(parent: Host, full_tag: str) -> bool:
    result = await parent.oneshot(f"docker image inspect {shlex.quote(full_tag)}")
    return result.status.is_ok


async def _build_one(
    parent: Host,
    project: str,
    settings: DockerSettings,
    image: DockerImage,
    *,
    rebuild: bool,
) -> tuple[Status, str]:
    """Build a single image on *parent*. Returns (status, full_tag_or_msg)."""
    hash_hex = context_hash(image)
    full_tag = image_full_tag(settings.registry_url, project, image, hash_hex)
    latest_tag = image_latest_tag(settings.registry_url, project, image)

    if not rebuild and await _image_exists(parent, full_tag):
        logger.info(f"[docker] {full_tag}: already built, skipping")
        # Make sure :latest also points at the cached digest.
        await parent.oneshot(f"docker tag {shlex.quote(full_tag)} {shlex.quote(latest_tag)}")
        return Status.Skipped, full_tag

    logger.info(f"[docker] building {full_tag}")
    remote_ctx = await stage_image_context(parent, project, image)

    # Resolve the Dockerfile path relative to the staged context. If the
    # user-declared Dockerfile lives outside the original context, the tar
    # step put it at the root of remote_ctx under its basename.
    try:
        dockerfile_rel = image.dockerfile.relative_to(image.context).as_posix()
    except ValueError:
        dockerfile_rel = image.dockerfile.name

    flags: list[str] = [
        "-t",
        shlex.quote(full_tag),
        "-t",
        shlex.quote(latest_tag),
        "-f",
        shlex.quote(str(remote_ctx / dockerfile_rel)),
    ]
    if image.target:
        flags.extend(["--target", shlex.quote(image.target)])
    for arg_name, arg_value in image.build_args:
        flags.extend(["--build-arg", shlex.quote(f"{arg_name}={arg_value}")])

    cmd = f"docker build {' '.join(flags)} {shlex.quote(str(remote_ctx))}"
    result = await parent.oneshot(cmd, timeout=None)
    if not result.status.is_ok:
        return result.status, result.output
    return Status.Success, full_tag


async def build_images(
    repo: Repo,
    parent: Host,
    *,
    image_names: Iterable[str] | None = None,
    rebuild: bool = False,
) -> dict[str, tuple[Status, str]]:
    """Build all (or selected) images for *repo* on *parent*.

    Args:
        repo: The :class:`~otto.configmodule.repo.Repo` whose ``[docker]`` settings declare the images.
        parent: A docker-capable lab host. Builds happen here.
        image_names: Optional filter — only build images whose ``name`` is
            in this iterable. ``None`` builds everything declared.
        rebuild: When ``True``, skip the context-hash existence check and
            always invoke ``docker build``.

    Returns:
        Mapping of image name to ``(Status, message_or_tag)``. Status is
        :attr:`~otto.utils.Status.Skipped` for images that already existed,
        :attr:`~otto.utils.Status.Success` for fresh builds, and a failure status
        otherwise. The message is the full tag on success/skip, or the
        captured stderr on failure.
    """
    settings = repo.docker_settings
    if not settings.images:
        return {}

    selected = (
        [img for img in settings.images if img.name in set(image_names)]
        if image_names is not None
        else list(settings.images)
    )

    results: dict[str, tuple[Status, str]] = {}
    for image in selected:
        results[image.name] = await _build_one(
            parent,
            repo.name,
            settings,
            image,
            rebuild=rebuild,
        )
    return results
