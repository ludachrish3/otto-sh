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

``AdapterResult`` / ``register_compose_adapter`` (repo-registered compose
adapters, spec §7) and ``deploy`` / ``teardown`` / ``deployed`` /
``UseCaseStack`` (the use-case deploy pipeline, spec §8/§11) are exported
lazily (PEP 562): a bare ``import otto.docker`` (what the CLI surface does)
must not pull in ``.adapter`` or ``.deployment`` — only a caller that actually
names one of these attributes pays for that import. This keeps the ``docker``
import-budget snapshot (``tests/unit/import_budget``) from growing for
modules the CLI's own import path never touches.

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

if TYPE_CHECKING:
    from .adapter import AdapterResult as AdapterResult
    from .adapter import register_compose_adapter as register_compose_adapter
    from .deployment import UseCaseStack as UseCaseStack
    from .deployment import deploy as deploy
    from .deployment import deployed as deployed
    from .deployment import teardown as teardown

_LAZY_ATTRS: dict[str, str] = {
    "AdapterResult": "otto.docker.adapter",
    "register_compose_adapter": "otto.docker.adapter",
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
    """Include the lazy exports in dir()/tab-completion, alongside the eager names."""
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
