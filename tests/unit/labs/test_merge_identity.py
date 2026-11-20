"""``Lab.__add__`` judges machines by address and inventory identity (lab templating, #1176)."""

import itertools
from functools import reduce

import pytest

from otto.host.element import Element
from otto.host.inventory_ref import InventoryRef, MachineIdentity, first_conflict, same_machine
from otto.host.unix_host import UnixHost
from otto.labs.loader import Lab

_IP = "10.0.0.5"


def bare(key: str) -> InventoryRef:
    return InventoryRef(key=key, backend="fake:mem")


def qual(name: str, key: str) -> InventoryRef:
    return InventoryRef(key=key, backend=f"fake:{name}", name=name, qualified=True)


def inline() -> InventoryRef:
    return InventoryRef()


_SPELLED = {
    "physical/k": qual("physical", "k"),
    "virtual/k": qual("virtual", "k"),
    "default/k2": qual("default", "k2"),
    "default/k3": qual("default", "k3"),
    "bare": bare("k1"),
    "inline": inline(),
}


def _lab(name: str, ref: InventoryRef, ip: str = _IP) -> Lab:
    lab = Lab(name=name)
    lab.add_host(UnixHost(ip=ip, creds=[], element=Element(name="server"), inventory_ref=ref))
    return lab


def _labs(order) -> list[Lab]:
    return [_lab(f"lab{i}", _SPELLED[spelled]) for i, spelled in enumerate(order)]


@pytest.mark.parametrize(
    ("a", "b", "merges"),
    [
        (bare("k1"), bare("k2"), True),
        (bare("k1"), inline(), True),
        (qual("physical", "k"), qual("physical", "k"), True),
        (qual("physical", "k1"), qual("physical", "k2"), False),
        (qual("physical", "k"), qual("virtual", "k"), False),
    ],
)
def test_lab_add_merges_by_machine(a, b, merges):
    assert (
        same_machine(a=MachineIdentity(ip=_IP, ref=a), b=MachineIdentity(ip=_IP, ref=b)) is merges
    )
    left, right = _lab("a", a), _lab("b", b)
    if merges:
        merged = left + right
        assert merged.hosts["server"].inventory_ref == b  # last wins
    else:
        with pytest.raises(ValueError, match="Duplicate host id 'server'"):
            _ = left + right


def test_different_addresses_are_never_one_machine():
    assert not same_machine(
        a=MachineIdentity(ip="10.0.0.1", ref=inline()),
        b=MachineIdentity(ip="10.0.0.2", ref=inline()),
    )
    with pytest.raises(ValueError, match=r"10\.0\.0\.1 in 'a' vs 10\.0\.0\.2 in 'b'"):
        _ = _lab("a", inline(), "10.0.0.1") + _lab("b", inline(), "10.0.0.2")


def test_first_conflict_returns_the_first_other_machine():
    incoming = MachineIdentity(ip=_IP, ref=qual("physical", "k"))
    seen = [
        MachineIdentity(ip=_IP, ref=inline()),
        MachineIdentity(ip=_IP, ref=qual("virtual", "k")),
        MachineIdentity(ip=_IP, ref=qual("lab", "k")),
    ]
    assert first_conflict(incoming=incoming, seen=seen) == seen[1]
    assert first_conflict(incoming=incoming, seen=seen[:1]) is None


@pytest.mark.parametrize(
    "order", list(itertools.permutations(["physical/k", "inline", "virtual/k"]))
)
def test_identity_constraints_accumulate_in_every_order(order):
    with pytest.raises(ValueError, match="Duplicate host id 'server'") as info:
        reduce(lambda a, b: a + b, _labs(order))
    message = str(info.value)
    assert "physical/k" in message
    assert "virtual/k" in message
    assert _IP in message


@pytest.mark.parametrize(
    "order", list(itertools.permutations(["default/k2", "bare", "default/k3"]))
)
def test_a_bare_reference_never_bridges_two_qualified_keys(order):
    with pytest.raises(ValueError, match=r"default/k2.*default/k3|default/k3.*default/k2"):
        reduce(lambda a, b: a + b, _labs(order))


@pytest.mark.parametrize("grouping", ["left", "right"])
def test_both_groupings_are_refused(grouping):
    physical, virtual, plain = _labs(["physical/k", "virtual/k", "inline"])

    def merge() -> Lab:
        if grouping == "left":
            return (physical + virtual) + plain
        return physical + (virtual + plain)

    with pytest.raises(ValueError, match="physical/k"):
        merge()


def test_the_right_grouping_carries_the_history():
    """``virtual + inline`` merges; joining it to ``physical`` must still see ``virtual``."""
    physical, virtual, plain = _labs(["physical/k", "virtual/k", "inline"])
    right = virtual + plain
    assert right.hosts["server"].inventory_ref == inline()
    with pytest.raises(ValueError, match="virtual/k"):
        _ = physical + right


def test_three_bare_aliases_still_merge():
    labs = [_lab(f"lab{i}", bare(f"k{i}")) for i in range(3)]
    merged = reduce(lambda a, b: a + b, labs)
    assert merged.hosts["server"].inventory_ref == bare("k2")
    assert merged.component_names == ["lab0", "lab1", "lab2"]


def test_a_lab_without_the_host_passes_the_history_on():
    """``a`` lacks the host; ``a + (b + c)`` still refuses a later conflicting ``d``."""
    empty = Lab(name="empty")
    physical, plain = _labs(["physical/k", "inline"])
    merged = empty + (physical + plain)
    with pytest.raises(ValueError, match="physical/k"):
        _ = merged + _lab("d", qual("virtual", "k"))


class _PrebuiltLabs:
    """A legacy backend that returns one prebuilt lab for every name."""

    def __init__(self, lab: Lab) -> None:
        self.lab = lab

    def list_labs(self) -> list[str]:
        return ["bench"]

    def load_lab(self, name, *, preferences=None, inventory=None) -> Lab:
        return self.lab

    def list_host_summaries(self, *, inventory=None) -> list:
        return []


def test_a_source_override_replaces_the_machine_history_with_the_host():
    """A composite override carries the winning host's history, not the loser's (Astra C2).

    The first source's lab was assembled with ``+``, so it holds a history for
    ``server`` at .1; a later source overrides that element with .2. Merging
    another lab with ``server`` at .1 must then be judged against .2 and
    refused, as on main, not silently replace .2.
    """
    from otto.labs import CompositeLabRepository, LabSource

    first = _lab("bench", inline(), "10.0.0.1") + _lab("bench", inline(), "10.0.0.1")
    first.name = "bench"
    composite = CompositeLabRepository(
        [
            LabSource(label="legacy:a", repository=_PrebuiltLabs(first)),
            LabSource(
                label="legacy:b", repository=_PrebuiltLabs(_lab("bench", inline(), "10.0.0.2"))
            ),
        ]
    )
    merged = composite.load_lab("bench")
    assert merged.hosts["server"].ip == "10.0.0.2"
    with pytest.raises(ValueError, match=r"10\.0\.0\.2 in 'bench' vs 10\.0\.0\.1 in 'other'"):
        _ = merged + _lab("other", inline(), "10.0.0.1")
