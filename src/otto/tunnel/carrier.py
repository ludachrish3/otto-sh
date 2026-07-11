"""Pluggable tunnel carriers: the ``TunnelCarrier`` contract + ``CARRIERS`` registry.

Mirrors the impairer registry (``otto.link.impairer``): custom carriers
register from init modules under a name; ``otto tunnel add --carrier`` selects
one per tunnel (chain-wide). Socat is the only first-party registrant
(``otto.tunnel.socat``).

A carrier decides what each tagged process EXECUTES — nothing more. The 2n
process topology (ingress/relay/egress x fwd/rev), the sentinel v1 wire
format, free-port allocation, discovery, verify, and remove are all
carrier-agnostic (spec 2026-07-11). The carrier name is deliberately not on
the wire: a tunnel's identity is its path+protocol+port, and remove reaps by
pid, so it tears down any carrier's processes.
"""

from typing import ClassVar

from ..registry import Registry, caller_module

DEFAULT_CARRIER = "socat"
"""Name of the first-party carrier — the ``--carrier`` default at the CLI and
the ``add_tunnel`` default in the library; one source of truth for both."""


class TunnelCarrier:
    """Builds the argv each tunnel process executes.

    Stateless: implementations build argv lists; the orchestration layer
    (``otto.tunnel.manage``) launches them on hosts via
    ``otto.host.daemon.launch_command``.
    """

    supported_protocols: ClassVar[frozenset[str]] = frozenset()
    """Service protocols this carrier can forward (e.g. ``frozenset({"tcp"})``)."""

    requirements_command: ClassVar[str] = ""
    """Complete shell probe run on every chain host; prints the bare word
    ``ok`` on a line of its own iff satisfied (the check is a whole-line
    match, so failure text may safely contain the substring ``ok``)."""

    tools_description: ClassVar[str] = ""
    """Human summary of the required tools, for the missing-tools error."""

    def ingress_args(
        self, protocol: str, service_port: int, bind_ip: str, next_ip: str, carrier_port: int
    ) -> list[str]:
        """Argv accepting client traffic on the service port, shipping to the carrier."""
        raise NotImplementedError

    def relay_args(self, carrier_port: int, next_ip: str) -> list[str]:
        """Argv for an intermediate-hop pass-through (same carrier port both sides)."""
        raise NotImplementedError

    def egress_args(
        self, protocol: str, service_port: int, deliver_ip: str, carrier_port: int
    ) -> list[str]:
        """Argv accepting the carrier and delivering to the local service."""
        raise NotImplementedError


CARRIERS: Registry[type[TunnelCarrier]] = Registry(
    "carrier", register_hint="otto.tunnel.register_carrier()"
)


def register_carrier(name: str, cls: type[TunnelCarrier], *, overwrite: bool = False) -> None:
    """Make a custom carrier selectable via ``--carrier <name>``.

    Call from an init module listed in ``.otto/settings.toml``. The carrier
    must declare a non-empty :attr:`TunnelCarrier.supported_protocols`;
    otherwise it could never validate any tunnel and is rejected here.
    """
    if not cls.supported_protocols:
        raise ValueError(
            f"register_carrier({name!r}): cls.supported_protocols is empty; a carrier "
            f"must declare at least one protocol (e.g. frozenset({{'tcp'}}))."
        )
    CARRIERS.register(name, cls, overwrite=overwrite, origin=caller_module())


def build_carrier(name: str) -> type[TunnelCarrier]:
    """Return the carrier class registered under *name* (rich unknown-name error)."""
    return CARRIERS.get(name)
