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
``register_compose_adapter`` (repo-registered compose adapters, spec §7),
``deploy`` / ``teardown`` / ``deployed`` / ``UseCaseStack`` (the use-case
deploy pipeline, spec §8/§11), and ``build_on`` / ``compose_build`` /
``DockerVerbError`` with the report types they and ``teardown`` return
(``BuildReport``, ``RepoBuild``, ``ImageBuild``, ``FailedImage``,
``HostReport``, ``TeardownReport``), and the read-only observe verbs
``list_containers`` / ``list_images`` with their ``ObserveReport``, and the
one parent rule (``default_docker_parent`` / ``docker_parent``): a caller
pays for the one module that defines the name it asks for. Every command that
loads a lab imports ``.compose`` to place the declared container hosts, and
must not pay for ``.build`` and its build-context staging with it.

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
    from .adapter import AdapterResult as AdapterResult
    from .adapter import register_compose_adapter as register_compose_adapter
    from .build import BuildOptions as BuildOptions
    from .build import build_images as build_images
    from .build_verbs import build_on as build_on
    from .build_verbs import compose_build as compose_build
    from .compose import compose_down as compose_down
    from .compose import compose_up as compose_up
    from .compose import composed as composed
    from .compose import get_container_host as get_container_host
    from .compose import get_user_compose_project as get_user_compose_project
    from .deployment import UseCaseStack as UseCaseStack
    from .deployment import deploy as deploy
    from .deployment import deployed as deployed
    from .deployment import teardown as teardown
    from .observe import DockerVerbError as DockerVerbError
    from .observe import HostOutput as HostOutput
    from .observe import LogsTarget as LogsTarget
    from .observe import ObserveReport as ObserveReport
    from .observe import compose_logs as compose_logs
    from .observe import compose_ps as compose_ps
    from .observe import container_logs as container_logs
    from .observe import default_docker_parent as default_docker_parent
    from .observe import docker_parent as docker_parent
    from .observe import docker_parents as docker_parents
    from .observe import follow_logs as follow_logs
    from .observe import list_containers as list_containers
    from .observe import list_images as list_images
    from .observe import resolve_compose_logs as resolve_compose_logs
    from .observe import resolve_logs as resolve_logs
    from .reports import BuildReport as BuildReport
    from .reports import FailedImage as FailedImage
    from .reports import HostReport as HostReport
    from .reports import ImageBuild as ImageBuild
    from .reports import RepoBuild as RepoBuild
    from .reports import TeardownReport as TeardownReport

# name -> the module that defines it, imported on first access by __getattr__.
_LAZY_ATTRS: dict[str, str] = {
    "AdapterResult": "otto.docker.adapter",
    "register_compose_adapter": "otto.docker.adapter",
    "BuildOptions": "otto.docker.build",
    "build_images": "otto.docker.build",
    "build_on": "otto.docker.build_verbs",
    "compose_build": "otto.docker.build_verbs",
    "compose_down": "otto.docker.compose",
    "compose_up": "otto.docker.compose",
    "composed": "otto.docker.compose",
    "get_container_host": "otto.docker.compose",
    "get_user_compose_project": "otto.docker.compose",
    "UseCaseStack": "otto.docker.deployment",
    "deploy": "otto.docker.deployment",
    "deployed": "otto.docker.deployment",
    "teardown": "otto.docker.deployment",
    "DockerVerbError": "otto.docker.observe",
    "HostOutput": "otto.docker.observe",
    "LogsTarget": "otto.docker.observe",
    "ObserveReport": "otto.docker.observe",
    "compose_logs": "otto.docker.observe",
    "compose_ps": "otto.docker.observe",
    "container_logs": "otto.docker.observe",
    "default_docker_parent": "otto.docker.observe",
    "docker_parent": "otto.docker.observe",
    "docker_parents": "otto.docker.observe",
    "follow_logs": "otto.docker.observe",
    "list_containers": "otto.docker.observe",
    "list_images": "otto.docker.observe",
    "resolve_compose_logs": "otto.docker.observe",
    "resolve_logs": "otto.docker.observe",
    "BuildReport": "otto.docker.reports",
    "FailedImage": "otto.docker.reports",
    "HostReport": "otto.docker.reports",
    "ImageBuild": "otto.docker.reports",
    "RepoBuild": "otto.docker.reports",
    "TeardownReport": "otto.docker.reports",
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
    "BuildOptions",
    "BuildReport",
    "DockerVerbError",
    "FailedImage",
    "HostOutput",
    "HostReport",
    "ImageBuild",
    "LogsTarget",
    "ObserveReport",
    "RepoBuild",
    "TeardownReport",
    "UseCaseStack",
    "build_images",
    "build_on",
    "compose_build",
    "compose_down",
    "compose_logs",
    "compose_ps",
    "compose_up",
    "composed",
    "container_logs",
    "default_docker_parent",
    "deploy",
    "deployed",
    "docker_parent",
    "docker_parents",
    "follow_logs",
    "get_container_host",
    "get_user_compose_project",
    "list_containers",
    "list_images",
    "register_compose_adapter",
    "resolve_compose_logs",
    "resolve_logs",
    "teardown",
]
