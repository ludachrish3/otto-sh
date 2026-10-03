"""Report types the docker verbs return.

Every verb that runs a docker command per host returns a :class:`HostReport`
(or a subclass adding the verb's own fields); every verb that builds images
returns a :class:`BuildReport`. ``ok`` is derived from the results and
nothing else, so a caller and the CLI cannot disagree about whether the verb
succeeded. Frozen and equality-comparable; not hashable (they hold dicts).
"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from ..result import CommandResult
    from .resolve import Displacement


@dataclass(frozen=True)
class HostReport:
    """One verb's outcome as the commands it ran, per host."""

    hosts: "dict[str, list[CommandResult]]"
    """Host id -> the commands run there, in the order they ran."""

    @property
    def failed(self) -> "dict[str, list[CommandResult]]":
        """The failing results per host; hosts with none are absent."""
        out = {host: [r for r in results if not r.is_ok] for host, results in self.hosts.items()}
        return {host: results for host, results in out.items() if results}

    @property
    def ok(self) -> bool:
        """True when every command on every host succeeded."""
        return not self.failed


@dataclass(frozen=True)
class TeardownReport(HostReport):
    """:func:`~otto.docker.deployment.teardown`'s outcome."""

    use_case: str
    """The use-case that was torn down."""


@dataclass(frozen=True)
class ImageBuild:
    """One image's build, with what the daemon lists for it afterwards."""

    name: str
    """The declared image name."""
    references: "list[str]"
    """The references the build was asked to tag, as the daemon lists them.

    Another tag the daemon holds on the same image is not included. Empty on failure."""
    image_id: "str | None"
    """The image id as ``docker images`` prints it; ``None`` on failure."""
    result: "CommandResult"
    """The ``docker build``'s own result, or the read-back that failed."""

    @property
    def is_ok(self) -> bool:
        """True when the build ran and the daemon lists the image."""
        return self.result.is_ok


@dataclass(frozen=True)
class RepoBuild:
    """One repo's build on one host."""

    repo: str
    """The repo's name."""
    host: str
    """The lab id of the host it was built on."""
    kind: Literal["built", "no_images"]
    """``no_images``: the repo declares no ``[[docker.images]]``; nothing ran."""
    images: "dict[str, ImageBuild]" = field(default_factory=dict)
    """Maps each declared image name to its build."""


@dataclass(frozen=True)
class FailedImage:
    """One image whose build failed."""

    repo: str
    """The repo that declares the image."""
    image: str
    """The declared image name."""
    result: "CommandResult"
    """The failing build result."""


@dataclass(frozen=True)
class BuildReport:
    """A build verb's outcome.

    Returned by :func:`~otto.docker.build_verbs.build_on` and
    :func:`~otto.docker.build_verbs.compose_build`.
    """

    repos: "list[RepoBuild]"
    """In build order."""
    displaced: "list[Displacement]" = field(default_factory=list)
    """Provider fragments the competition excluded (empty for ``build_on``)."""

    @property
    def failed(self) -> "list[FailedImage]":
        """Every image whose build is not ok, in build order."""
        return [
            FailedImage(entry.repo, name, built.result)
            for entry in self.repos
            for name, built in entry.images.items()
            if not built.is_ok
        ]

    @property
    def ok(self) -> bool:
        """True when no image failed (a ``no_images`` repo is not a failure)."""
        return not self.failed
