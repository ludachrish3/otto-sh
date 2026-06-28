"""
Docker support for otto.

This package provides a library API that the CLI (``otto docker ...``) and
project instructions/suites both call into. Anything the CLI can do, an
instruction can do too::

    from otto.docker import build_images, compose_up, compose_down, composed


    @instruction()
    async def smoke():
        async with composed(repo, lab, own=True) as containers:
            await containers["api"].run("./run-tests")

See the design notes in ``docs/design/docker_hosts.md`` for the full
architecture (parent-delegation pattern, hop inheritance, naming scheme).
"""

from __future__ import annotations

from ._context_hash import context_hash
from .build import build_images, image_full_tag, image_latest_tag
from .compose import (
    compose_down,
    compose_ps,
    compose_up,
    composed,
    get_container_host,
    get_user_compose_project,
)

__all__ = [
    "build_images",
    "compose_down",
    "compose_ps",
    "compose_up",
    "composed",
    "context_hash",
    "get_container_host",
    "get_user_compose_project",
    "image_full_tag",
    "image_latest_tag",
]
