"""Pluggable link impairers: the ``LinkImpairer`` contract + ``IMPAIRERS`` registry.

Mirrors the transfer-backend registry (``otto.host.transfer.registry``): custom
impairers register from init modules under a name; a host's ``impairer`` pin /
``valid_impairers`` menu select one per placement host (spec §5). NetEm is the
only first-party registrant (``otto.link.netem``).
"""

from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import ClassVar, Literal

from ..registry import ClassEntry, Ref, Registry, registration_boundary, resolved
from .params import ImpairmentParams, Selector

FIRST_SELECTOR_BAND = 4
"""prio bands 1-3 keep kernel-default priomap semantics; selectors start at band 4."""

MAX_SELECTORS = 8
"""Per-netdev selector cap (bands 4..11 inside the fixed 11-band prio root)."""


@dataclass(frozen=True, slots=True)
class ScopedState:
    """Discriminated read-back of one placement netdev's impairment shape.

    Exactly one of four kinds (spec §1): ``clean`` (no otto state — including
    kernel-default root qdiscs), ``whole`` (today's root netem,
    :attr:`whole` set), ``scoped`` (:attr:`selectors` maps each
    :class:`~otto.link.params.Selector` to its ``(band, params)``), or
    ``foreign`` (a root qdisc otto did not generate: reported by ``list``,
    loudly refused on mutate).
    """

    kind: Literal["clean", "whole", "scoped", "foreign"]
    whole: ImpairmentParams | None = None
    selectors: dict[Selector, tuple[int, ImpairmentParams]] = dc_field(default_factory=dict)

    @classmethod
    def clean(cls) -> "ScopedState":
        """No otto impairment state on the netdev."""
        return cls("clean")

    @classmethod
    def whole_link(cls, params: ImpairmentParams) -> "ScopedState":
        """Today's whole-link root netem."""
        return cls("whole", whole=params)

    @classmethod
    def from_selectors(
        cls, selectors: dict[Selector, tuple[int, ImpairmentParams]]
    ) -> "ScopedState":
        """Create a port-scoped tree: selector -> (band, params)."""
        return cls("scoped", selectors=dict(selectors))

    @classmethod
    def foreign(cls) -> "ScopedState":
        """Report a root qdisc otto did not generate — never mutated, only reported."""
        return cls("foreign")


class LinkImpairer:
    """Builds the shell commands that apply/read/clear one placement's impairment.

    Stateless: implementations build command strings and parse output; the
    orchestration layer (``otto.link.manage``) runs them on hosts.
    """

    host_families: ClassVar[frozenset[str]] = frozenset[str]()
    """Host families this impairer serves (e.g. ``frozenset({"unix"})``)."""

    supports_selectors: ClassVar[bool] = False
    """Whether this impairer implements the optional port-scoped surface
    (the ``scoped_*`` builders + :meth:`parse_scoped`). Defaults off so
    third-party impairers are unaffected; a ``--port`` request routed to a
    non-supporting impairer is a loud capability error in orchestration."""

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

    def scoped_root_command(self, netdev: str) -> str:
        """Command creating the scoped classful root on *netdev* (idempotent)."""
        raise NotImplementedError

    def scoped_band_command(self, netdev: str, band: int, params: ImpairmentParams) -> str:
        """Command applying *params* as band *band*'s per-selector leaf (idempotent)."""
        raise NotImplementedError

    def scoped_filter_commands(self, netdev: str, band: int, selector: Selector) -> list[str]:
        """Commands steering *selector*'s traffic into band *band*."""
        raise NotImplementedError

    def scoped_clear_selector_commands(
        self, netdev: str, band: int, selector: Selector
    ) -> list[str]:
        """Commands removing *selector*'s filters and its band leaf (root kept)."""
        raise NotImplementedError

    def scoped_read_commands(self, netdev: str) -> list[str]:
        """Return the two read commands whose outputs :meth:`parse_scoped` understands."""
        raise NotImplementedError

    def parse_scoped(self, qdisc_output: str, filter_output: str) -> ScopedState:
        """Parse :meth:`scoped_read_commands` outputs into a :class:`ScopedState`."""
        raise NotImplementedError


def _check_impairer_class(name: str, cls: object) -> None:
    """Refuse anything but a :class:`LinkImpairer` subclass declaring a host family.

    One with no host family could never validate against any host. One check,
    two call sites: ``validate`` runs it on a class registered eagerly,
    ``check_resolved`` on the class a ``Ref`` names, once imported.
    """
    if not (isinstance(cls, type) and issubclass(cls, LinkImpairer)):
        raise TypeError(f"register_impairer({name!r}): {cls!r} is not a LinkImpairer subclass")
    if not cls.host_families:
        raise ValueError(
            f"register_impairer({name!r}): cls.host_families is empty; an impairer "
            f"must declare at least one host family (e.g. frozenset({{'unix'}}))."
        )


def _validate_impairer_entry(
    name: str, entry: "ClassEntry[LinkImpairer]", _proposed: object
) -> None:
    if not isinstance(entry.cls, Ref):
        _check_impairer_class(name, entry.cls)


IMPAIRERS: "Registry[ClassEntry[LinkImpairer]]" = Registry(
    "impairer",
    entry=ClassEntry,
    register_hint="otto.link.register_impairer()",
    validate=_validate_impairer_entry,
    check_resolved=lambda name, entry: _check_impairer_class(name, resolved(entry.cls)),
)
"""The impairer classes lab data can name, keyed by impairer name."""
IMPAIRERS.register("netem", ClassEntry(Ref("otto.link.netem:NetEmImpairer")))


@registration_boundary
def register_impairer(
    name: str, cls: "type[LinkImpairer] | Ref", *, overwrite: bool = False
) -> None:
    """Make a custom impairer available to lab data under *name*.

    Call from an init module listed in ``.otto/settings.toml``. The impairer
    must declare a non-empty :attr:`LinkImpairer.host_families`; otherwise it
    could never validate against any host and is rejected here. *cls* may be
    a :class:`~otto.registry.Ref` (``"module:Class"``), imported and checked
    at its first use.

    Raises:
        TypeError: If *cls* is not a :class:`LinkImpairer` subclass.
        ValueError: If ``cls.host_families`` is empty.
        otto.registry.DuplicateRegistration: If *name* is taken and *overwrite*
            is false.
    """
    IMPAIRERS.register(name, ClassEntry(cls), overwrite=overwrite)


def build_impairer(name: str) -> type[LinkImpairer]:
    """Return the impairer class registered under *name* (rich unknown-name error)."""
    return resolved(IMPAIRERS.get(name).cls)
