"""How a lab's declared links are composed, once, after its hosts are (lab templating D5).

Every route that loads a lab — a source loaded directly, and the composite
over every ``[[lab.sources]]`` entry — hands its sources' contributions to
:func:`compose_links`, which is the one place that decides which declared
links a lab holds and what each endpoint resolves to. A link may therefore
name a host another source declares.

Importing this module is light: the link layer loads when a link resolves.
"""

import functools
import logging
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, TypeVar

from typing_extensions import final, override

from .errors import LabRepositoryError

if TYPE_CHECKING:
    from ..link.derive import HostAddressing
    from ..link.model import Link
    from .loader import Lab
    from .protocol import DeclaredLinkInputs, LabContribution

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


@final
@dataclass(frozen=True, kw_only=True)
class SourceContribution:
    """One source's part in a lab's links: unresolved inputs, or links it resolved itself."""

    label: str
    """The source's label, as messages name it."""

    inputs: "DeclaredLinkInputs | None"
    """A capable source's declared links and addressing; ``None`` for a legacy source."""

    resolved_links: "list[Link]" = field(default_factory=list)
    """A legacy source's own resolved ``Lab.links``, kept as it resolved them."""

    host_ids: frozenset[str] = frozenset()
    """The ids of the hosts this source itself contributed to the lab.

    A link touching one of them attaches even when a later source's override
    removed it, as it did when each source resolved its own links.
    """

    holds_lab: bool = True
    """Whether the source contributed the lab at all; ``False`` for one holding only links.

    Such a source joins the merge only with a link it resolves, so a source
    that contributes nothing leaves the other sources' links as they were.
    """


def contribution_part(*, label: str, contribution: "LabContribution") -> SourceContribution:
    """Return a capable source's part in a lab's links, from its *contribution*.

    The one translation of a :class:`~otto.labs.protocol.LabContribution`, for
    the composite and for a source loaded directly: its links unresolved, the
    ids of the hosts it contributed, and whether it contributed the lab at
    all. The contribution's own ``lab.links`` are not read.
    """
    lab = contribution.lab
    return SourceContribution(
        label=label,
        inputs=contribution.links,
        host_ids=frozenset(lab.hosts) if lab is not None else frozenset(),
        holds_lab=lab is not None,
    )


class ContributionGuard(Protocol):
    """Run one step of a contribution's link resolution under its source's error rules.

    The step is the contribution's addressing, or the resolution of its own
    links; whatever it raises is that source's failure, as the caller
    classifies a failure of the source's load. :func:`compose_links` calls it
    once per step, with the contribution the step belongs to.
    """

    def __call__(self, contribution: SourceContribution, call: "Callable[[], _T]", /) -> _T:
        """Return *call*'s result, raising its failure as *contribution*'s source's."""
        ...


def _unguarded(contribution: SourceContribution, call: "Callable[[], _T]", /) -> _T:
    """Return *call*'s result: every failure propagates as raised."""
    del contribution
    return call()


def compose_links(
    *,
    lab: "Lab",
    contributions: Sequence[SourceContribution],
    guard: "ContributionGuard | None" = None,
) -> "list[Link]":
    """Return *lab*'s declared links, composed from *contributions* in source order.

    *lab* holds the composed hosts. A capable contribution's entries are
    resolved once (:func:`~otto.link.derive.resolve_declared_links`); a link
    attaches when an endpoint is a host of *lab*, or one its own source
    contributed. An endpoint resolves through, in order: the declaring
    contribution's own addressing, which is each source's own result as
    before; the composed host in *lab* (:func:`~otto.link.derive.addressing_of_host`),
    so a host of any backend is an endpoint; the last contribution, in source
    order, that addresses it. Only an id none of these addresses is an
    unknown host. A legacy contribution's links are kept as resolved.

    The links merge by id, the later source winning (:func:`merge_links_by_id`),
    as two sources' links always have. Each layer is computed at most once per
    load, and only when an endpoint misses the layers before it: a source's
    addressing when one of its own links needs an endpoint, or when an
    attached link needs an endpoint that neither its own source nor *lab*
    addresses.

    *guard* runs each contribution's addressing, and the resolution of its
    own links, so a failure of either is that contribution's source's
    (:class:`ContributionGuard`); ``None`` lets every failure propagate as
    raised, as a source loaded directly does.

    Raises
    ------
    LabRepositoryError
        Naming the link's document and index, when a link that attaches does
        not resolve; or whatever *guard* raises for it.
    """
    run: ContributionGuard = guard if guard is not None else _unguarded
    # Every layer is memoized here, once per load: a source's thunk is called
    # at most once, however many endpoints read it, whether or not it caches.
    composed = functools.cache(functools.partial(_composed_addressing, lab))
    own = {
        id(c): functools.cache(functools.partial(run, c, c.inputs.addressing))
        for c in contributions
        if c.inputs is not None
    }
    others = functools.cache(functools.partial(_last_addressing_wins, list(own.values())))
    merged: list[Link] | None = None
    owner: dict[str, str] = {}
    for contribution in contributions:
        links = run(
            contribution,
            functools.partial(
                _links_of,
                contribution,
                lab=lab,
                layers=[own[id(contribution)], composed, others] if id(contribution) in own else [],
            ),
        )
        if not contribution.holds_lab and not links:
            continue
        for link in links:
            if link.id in owner and owner[link.id] != contribution.label:
                logger.debug(
                    f"link {link.id!r} in lab {lab.name!r}: {contribution.label} overrides "
                    f"{owner[link.id]}"
                )
            owner[link.id] = contribution.label
        # The first source's links stand as resolved, a repeated id included;
        # each later source's merge by id.
        merged = links if merged is None else merge_links_by_id(merged, links)
    return merged if merged is not None else []


def _links_of(
    contribution: SourceContribution,
    *,
    lab: "Lab",
    layers: "Sequence[Callable[[], Mapping[str, HostAddressing]]]",
) -> "list[Link]":
    """Return *contribution*'s links: a legacy source's as it resolved them, else resolved now.

    *layers* are the endpoint addressing in resolution order, its own first.
    """
    inputs = contribution.inputs
    if inputs is None:
        return list(contribution.resolved_links)
    if not inputs.entries:
        return []
    from ..link.derive import resolve_declared_links  # lazy: only a lab with links resolves one

    try:
        return resolve_declared_links(
            inputs.entries,
            _LayeredAddressing(layers=layers),
            loaded_ids=set(lab.hosts) | contribution.host_ids,
            selection=frozenset({lab.name}),
        )
    except ValueError as e:
        raise LabRepositoryError(str(e)) from e


def merge_links_by_id(merged: "list[Link]", links: "list[Link]") -> "list[Link]":
    """Return *merged* with *links* merged in by id, a later link replacing one in place.

    THE link merge, of two sources' links and of two labs' (``a + b`` on a
    :class:`~otto.labs.loader.Lab`).
    """
    by_id = {link.id: link for link in merged}
    by_id.update({link.id: link for link in links})
    return list(by_id.values())


def _composed_addressing(lab: "Lab") -> "dict[str, HostAddressing]":
    """Return the addressing of every composed host of *lab*, by id."""
    from ..link.derive import addressing_of_host

    return {host_id: addressing_of_host(host) for host_id, host in lab.hosts.items()}


def _last_addressing_wins(
    sources: "Sequence[Callable[[], Mapping[str, HostAddressing]]]",
) -> "dict[str, HostAddressing]":
    """Return every capable source's addressing by id, a later source's address winning."""
    out: dict[str, HostAddressing] = {}
    for addressing in sources:
        out.update(addressing())
    return out


class _LayeredAddressing(Mapping[str, "HostAddressing"]):
    """Endpoint addressing over ordered layers, each computed only when the ones before it miss."""

    def __init__(self, *, layers: "Sequence[Callable[[], Mapping[str, HostAddressing]]]") -> None:
        self._layers = layers

    @override
    def __getitem__(self, host_id: str) -> "HostAddressing":
        for layer in self._layers:
            found = layer().get(host_id)
            if found is not None:
                return found
        raise KeyError(host_id)

    @override
    def __iter__(self) -> Iterator[str]:
        # Every layer: only an unknown endpoint's near-miss hint lists the ids.
        return iter(dict.fromkeys(host_id for layer in self._layers for host_id in layer()))

    @override
    def __len__(self) -> int:
        return sum(1 for _ in self)
