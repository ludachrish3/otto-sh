"""
File staging onto a parent host.

Building an image and running ``docker compose`` both require getting the
relevant files (Dockerfile, build context, compose YAML) onto the parent
host that runs the docker daemon. This module wraps that with a small,
predictable layout under ``/tmp/otto-docker/<project>/`` on the parent.

Layout::

    /tmp/otto-docker/<project>/
        build/<image>/<context-as-tar>     # extracted build context
        compose/<n>/<basename>.yml         # one numbered dir per compose file

Cleanup is the caller's responsibility (and is best-effort: a previous
crash is recovered from on the next stage). The directory layout is
stable across runs so nothing leaks into per-invocation subdirs.
"""

from __future__ import annotations

import shlex
import tarfile
import tempfile
from pathlib import Path

from ..configmodule.repo import DockerCompose, DockerImage
from ..host.host import Host
from ..utils import Status

PARENT_ROOT = Path("/tmp/otto-docker")  # noqa: S108 — deliberate staging path


def project_root(project: str) -> Path:
    """Per-project staging root on the parent host."""
    return PARENT_ROOT / project


def image_build_dir(project: str, image_name: str) -> Path:
    """Where this image's context will live on the parent."""
    return project_root(project) / "build" / image_name


def compose_dir(project: str) -> Path:
    """Per-project compose staging directory on the parent host."""
    return project_root(project) / "compose"


async def stage_image_context(
    parent: Host,
    project: str,
    image: DockerImage,
) -> Path:
    """Tar the build context locally, ship it to the parent, untar it.

    Returns the absolute path on the parent of the extracted context
    directory. The Dockerfile is included verbatim under its declared
    name so ``docker build -f`` resolves it.
    """
    remote_dir = image_build_dir(project, image.name)

    # Wipe and recreate to avoid mixing leftover files from an earlier build.
    await parent.oneshot(
        f"rm -rf {shlex.quote(str(remote_dir))} && mkdir -p {shlex.quote(str(remote_dir))}"
    )

    with tempfile.NamedTemporaryFile(
        prefix=f"otto-docker-{image.name}-",
        suffix=".tar",
        delete=False,
    ) as tmp:
        tmp_path = Path(tmp.name)

    try:
        with tarfile.open(tmp_path, "w") as tar:
            # Add the entire context with relative arcnames.
            tar.add(image.context, arcname=".")
            # If the Dockerfile lives outside the context, include it as
            # well at the path the user declared (relative to context).
            try:
                image.dockerfile.relative_to(image.context)
            except ValueError:
                tar.add(image.dockerfile, arcname=image.dockerfile.name)

        status, msg = await parent.put([tmp_path], remote_dir)
        if not status.is_ok:
            raise RuntimeError(f"failed to stage build context to parent: {msg}")

        # Extract on parent.
        remote_tar = remote_dir / tmp_path.name
        result = await parent.oneshot(
            f"tar -xf {shlex.quote(str(remote_tar))} -C {shlex.quote(str(remote_dir))} "
            f"&& rm -f {shlex.quote(str(remote_tar))}"
        )
        if not result.status.is_ok:
            raise RuntimeError(f"failed to extract build context on parent: {result.output}")
    finally:
        tmp_path.unlink(missing_ok=True)

    return remote_dir


async def stage_compose_files(
    parent: Host,
    project: str,
    composes: list[DockerCompose],
) -> list[Path]:
    """Copy compose files to numbered directories on the parent.

    Numbered directories preserve the order the project listed them
    (which determines override precedence in ``docker compose -f a -f b``).
    Returns the absolute paths on the parent in the same order.
    """
    base = compose_dir(project)
    await parent.oneshot(f"rm -rf {shlex.quote(str(base))} && mkdir -p {shlex.quote(str(base))}")

    out: list[Path] = []
    for idx, compose in enumerate(composes):
        sub = base / str(idx)
        await parent.oneshot(f"mkdir -p {shlex.quote(str(sub))}")
        status, msg = await parent.put([compose.path], sub)
        if not status.is_ok:
            raise RuntimeError(f"failed to stage compose file {compose.path}: {msg}")
        out.append(sub / compose.path.name)
    return out


async def cleanup_project(parent: Host, project: str) -> Status:
    """Remove the per-project staging tree on the parent. Best-effort."""
    result = await parent.oneshot(f"rm -rf {shlex.quote(str(project_root(project)))}")
    return result.status
