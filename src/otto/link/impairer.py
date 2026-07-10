"""Pluggable link impairers: the ``LinkImpairer`` contract + ``IMPAIRERS`` registry.

Mirrors the transfer-backend registry (``otto.host.transfer.registry``): custom
impairers register from init modules under a name; a host's ``impairer`` pin /
``valid_impairers`` menu select one per placement host (spec §5). NetEm is the
only first-party registrant (``otto.link.netem``).
"""

from typing import ClassVar

from ..registry import Registry, caller_module
from .params import ImpairmentParams


class LinkImpairer:
    """Builds the shell commands that apply/read/clear one placement's impairment.

    Stateless: implementations build command strings and parse output; the
    orchestration layer (``otto.link.manage``) runs them on hosts.
    """

    host_families: ClassVar[frozenset[str]] = frozenset()
    """Host families this impairer serves (e.g. ``frozenset({"unix"})``)."""

    def apply_command(self, netdev: str, params: ImpairmentParams) -> str:
        """Shell command applying *params* to *netdev* (idempotent replace)."""
        raise NotImplementedError

    def read_command(self, netdev: str) -> str:
        """Shell command whose output :meth:`parse_read` understands."""
        raise NotImplementedError

    def clear_command(self, netdev: str) -> str:
        """Shell command removing this impairer's state from *netdev*."""
        raise NotImplementedError

    def parse_read(self, output: str) -> ImpairmentParams | None:
        """Parse :meth:`read_command` output; ``None`` = no impairment present."""
        raise NotImplementedError


IMPAIRERS: Registry[type[LinkImpairer]] = Registry(
    "impairer", register_hint="otto.link.register_impairer()"
)


def register_impairer(name: str, cls: type[LinkImpairer], *, overwrite: bool = False) -> None:
    """Make a custom impairer available to lab data under *name*.

    Call from an init module listed in ``.otto/settings.toml``. The impairer
    must declare a non-empty :attr:`LinkImpairer.host_families`; otherwise it
    could never validate against any host and is rejected here.
    """
    if not cls.host_families:
        raise ValueError(
            f"register_impairer({name!r}): cls.host_families is empty; an impairer "
            f"must declare at least one host family (e.g. frozenset({{'unix'}}))."
        )
    IMPAIRERS.register(name, cls, overwrite=overwrite, origin=caller_module())


def build_impairer(name: str) -> type[LinkImpairer]:
    """Return the impairer class registered under *name* (rich unknown-name error)."""
    return IMPAIRERS.get(name)
