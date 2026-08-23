"""`find_link` resolving a link is not the same as a command being able to act on it.

The gap is not small: EVERY implicit link resolves and none can be impaired.
That cost one wrong turn already — a completer was changed to "offer whatever
find_link accepts", which in the three-host unix fixture takes the
candidate list from 1 to 4, three of them guaranteed errors (`test1--local`,
`local--test3`, `local--test2`).
"""

import pytest

from otto.host.builtin_hosts import BUILTIN_LOCAL_HOST_ID
from otto.link.model import Link, LinkEndpoint, Provenance
from otto.link.placement import (
    BOTH_DIRECTIONS as _BOTH,
)
from otto.link.placement import (
    FlowDirection,
    endpoint_placements,
    ensure_not_local_link,
    impairment_refusal,
)


def _link(a: LinkEndpoint, b: LinkEndpoint, **kw) -> Link:
    return Link(a=a, b=b, **kw)


def test_a_declared_link_with_named_interfaces_is_impairable() -> None:
    """Positive control — every refusal below must be on its own merits."""
    link = _link(
        LinkEndpoint(host="test1", interface="eth1"),
        LinkEndpoint(host="test2", interface="eth1"),
    )
    assert impairment_refusal(link) is None


def test_an_endpoint_on_the_local_host_is_refused() -> None:
    """otto's own path to the bed — the shape every hop-less host produces."""
    link = _link(
        LinkEndpoint(host=BUILTIN_LOCAL_HOST_ID),
        LinkEndpoint(host="test1", interface="eth1"),
        provenance=Provenance.IMPLICIT,
    )
    assert "path to the bed" in (impairment_refusal(link) or "")


def test_an_endpoint_without_a_named_interface_is_refused() -> None:
    """The other implicit shape: a hop edge between two real hosts."""
    link = _link(
        LinkEndpoint(host="test4"),
        LinkEndpoint(host="zephyr37-fat"),
        provenance=Provenance.IMPLICIT,
    )
    refusal = impairment_refusal(link) or ""
    assert "no named interface" in refusal
    assert "test4" in refusal
    assert "zephyr37-fat" in refusal


def test_an_in_path_link_is_not_judged_on_interfaces() -> None:
    """In-path placements come from the middlebox's LIVE address table, so the
    only structural rule that applies is the local-host refusal."""
    in_path = _link(LinkEndpoint(host="a"), LinkEndpoint(host="b"), impair="mid")
    assert impairment_refusal(in_path) is None
    assert "middlebox" in (
        impairment_refusal(
            _link(LinkEndpoint(host="a"), LinkEndpoint(host="b"), impair=BUILTIN_LOCAL_HOST_ID)
        )
        or ""
    )


def test_the_predicate_agrees_with_the_placement_layer() -> None:
    """The predicate must not drift from the code that actually refuses.

    It is a pure restatement of the structural half of `_resolve_placements`,
    so each of its two rules is checked against the FUNCTION that enforces
    it — a single `pytest.raises(ValueError)` would let the local-host case
    pass on the unnamed-interface rule instead, which is the same trap the
    predicate exists to avoid.
    """
    local_link = Link(
        a=LinkEndpoint(host=BUILTIN_LOCAL_HOST_ID, interface="eth1"),
        b=LinkEndpoint(host="test1", interface="eth1"),
        provenance=Provenance.IMPLICIT,
    )
    assert impairment_refusal(local_link) is not None
    with pytest.raises(ValueError, match="local host"):
        ensure_not_local_link(local_link)
    # ...and it is NOT the interface rule doing the work here.
    endpoint_placements(local_link, _BOTH)

    hop_link = Link(
        a=LinkEndpoint(host="test4"),
        b=LinkEndpoint(host="zephyr37-fat"),
        provenance=Provenance.IMPLICIT,
    )
    assert impairment_refusal(hop_link) is not None
    ensure_not_local_link(hop_link)  # ...and here it is not the local rule.
    with pytest.raises(ValueError, match="not impairable"):
        endpoint_placements(hop_link, _BOTH)


def test_a_half_interfaced_link_is_refused_only_in_the_bare_direction() -> None:
    """`endpoint_placements` refuses PER DIRECTION, so a link between one
    interfaced host and one bare host is dead for `--all` and alive for
    `impair --from <the interfaced end>`.

    A direction-blind predicate would call this link unimpairable, which is a
    lie in the exact direction a user would reach for — and it would drop the
    id from anything that filters on the predicate.
    """
    half = _link(
        LinkEndpoint(host="test1", interface="eth1"),
        LinkEndpoint(host="test2"),
    )
    assert impairment_refusal(half, {FlowDirection.A_TO_B}) is None
    endpoint_placements(half, {FlowDirection.A_TO_B})

    refusal = impairment_refusal(half, {FlowDirection.B_TO_A})
    assert refusal is not None
    assert "test2" in refusal
    assert "test1" not in refusal
    with pytest.raises(ValueError, match="not impairable"):
        endpoint_placements(half, {FlowDirection.B_TO_A})

    # ...and the default is both, which is what `list` and a bare `impair` ask.
    assert impairment_refusal(half) == impairment_refusal(half, _BOTH)
    assert impairment_refusal(half) is not None
