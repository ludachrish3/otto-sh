"""
Docker support for otto.

This package provides a library API that the CLI (``otto docker ...``) and
project instructions/suites both call into. Anything the CLI can do, an
instruction can do too::

    from otto.docker import build_images, compose_up, compose_down, composed, deployed


    @instruction()
    async def smoke():
        async with deployed("integration", own=True) as stack:
            await stack.hosts["api"].run("./run-tests")

See the design notes in ``docs/design/docker_hosts.md`` for the full
architecture (parent-delegation pattern, hop inheritance, naming scheme).

Every name is exported lazily (PEP 562), including ``AdapterResult`` /
``register_compose_adapter`` (repo-registered compose adapters, spec §7) and
``deploy`` / ``teardown`` / ``deployed`` / ``UseCaseStack`` (the use-case
deploy pipeline, spec §8/§11): a caller pays for the one module that defines
the name it asks for. Every command that loads a lab imports ``.compose`` to
place the declared container hosts, and must not pay for ``.build`` and its
build-context staging with it.

The deploy pipeline lives in ``.deployment``, NOT ``.deploy``, and the name
is load-bearing: a submodule and a lazy export sharing one name is resolved
by the SUBMODULE. Importing ``otto.docker.deploy`` (which the lazy resolver
itself would do on the first access) rebinds ``otto.docker.deploy`` to the
module, so ``from otto.docker import deploy`` would hand back the function
exactly once per process and the module every time after -- silently, with
no error to notice. Renaming the module is what makes the spec §11 API
(``from otto.docker import deploy, teardown, deployed``) mean one thing.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ._context_hash import context_hash as context_hash
    from .adapter import AdapterResult as AdapterResult
    from .adapter import register_compose_adapter as register_compose_adapter
    from .build import build_images as build_images
    from .build import image_full_tag as image_full_tag
    from .build import image_latest_tag as image_latest_tag
    from .compose import compose_down as compose_down
    from .compose import compose_ps as compose_ps
    from .compose import compose_up as compose_up
    from .compose import composed as composed
    from .compose import get_container_host as get_container_host
    from .compose import get_user_compose_project as get_user_compose_project
    from .deployment import UseCaseStack as UseCaseStack
    from .deployment import deploy as deploy
    from .deployment import deployed as deployed
    from .deployment import teardown as teardown

# name -> the module that defines it, imported on first access by __getattr__.
_LAZY_ATTRS: dict[str, str] = {
    "context_hash": "otto.docker._context_hash",
    "AdapterResult": "otto.docker.adapter",
    "register_compose_adapter": "otto.docker.adapter",
    "build_images": "otto.docker.build",
    "image_full_tag": "otto.docker.build",
    "image_latest_tag": "otto.docker.build",
    "compose_down": "otto.docker.compose",
    "compose_ps": "otto.docker.compose",
    "compose_up": "otto.docker.compose",
    "composed": "otto.docker.compose",
    "get_container_host": "otto.docker.compose",
    "get_user_compose_project": "otto.docker.compose",
    "UseCaseStack": "otto.docker.deployment",
    "deploy": "otto.docker.deployment",
    "deployed": "otto.docker.deployment",
    "teardown": "otto.docker.deployment",
}


def __getattr__(name: str) -> object:
    """PEP 562 lazy resolver for otto.docker's public exports."""
    import importlib

    if name in _LAZY_ATTRS:
        return getattr(importlib.import_module(_LAZY_ATTRS[name]), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """Include the lazy exports in dir()/tab-completion; the module dict holds none of them."""
    return sorted(set(globals()) | set(_LAZY_ATTRS))


__all__ = [
    "AdapterResult",
    "UseCaseStack",
    "build_images",
    "compose_down",
    "compose_ps",
    "compose_up",
    "composed",
    "context_hash",
    "deploy",
    "deployed",
    "get_container_host",
    "get_user_compose_project",
    "image_full_tag",
    "image_latest_tag",
    "register_compose_adapter",
    "teardown",
]
