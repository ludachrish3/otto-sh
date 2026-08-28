"""CompositeLabRepository: ordered sources, later-wins override, loud errors."""

import logging
from dataclasses import dataclass

import pytest

from otto.config.lab import Lab
from otto.examples.lab_repository import ExampleLabRepository
from otto.host.factory import create_host_from_dict
from otto.labs import (
    CompositeLabRepository,
    LabNotFoundError,
    LabRepositoryError,
    LabSource,
)
from otto.testing import assert_lab_repository_conforms


def _host(element: str, ip: str) -> dict:
    return {"ip": ip, "element": element, "creds": [{"login": "u", "password": "p"}]}


def _composite(*labeled: tuple) -> CompositeLabRepository:
    """Build a composite from ``(label, labs)`` or ``(label, labs, resources)`` entries.

    Resources are per LAB since v2 (spec §8.1), so they are a source-level
    table here, not a host field; omitting them declares none.
    """
    return CompositeLabRepository(
        [
            LabSource(
                label=item[0],
                repository=ExampleLabRepository(
                    labs=item[1], resources=item[2] if len(item) > 2 else {}
                ),
            )
            for item in labeled
        ]
    )


def test_union_of_disjoint_sources() -> None:
    comp = _composite(
        ("r/global", {"site": [_host("alt1", "10.0.0.1")]}),
        ("r/virtual", {"site": [_host("test2", "10.0.0.2")]}),
    )
    lab = comp.load_lab("site")
    assert set(lab.hosts) == {"alt1", "test2"}
    assert lab.name == "site"
    assert lab.component_names == ["site"]


def test_later_source_overrides_wholesale_and_warns(caplog) -> None:
    comp = _composite(
        ("r/global", {"site": [_host("alt1", "10.0.0.1")]}),
        ("r/virtual", {"site": [_host("alt1", "10.9.9.9")]}),
    )
    with caplog.at_level(logging.WARNING, logger="otto.labs.composite"):
        lab = comp.load_lab("site")
    assert lab.hosts["alt1"].ip == "10.9.9.9"
    msgs = [r.getMessage() for r in caplog.records]
    # The warning names the ELEMENT, not the host id: the element is the unit
    # that was replaced (spec §6), and restating the id would hide that its
    # siblings went with it.
    assert any(
        "('alt1', None)" in m and "'site'" in m and "r/virtual" in m and "r/global" in m
        for m in msgs
    )
    assert any("overrides" in m for m in msgs)


def test_no_element_warning_without_collision(caplog) -> None:
    """Disjoint elements merge silently — the only warning is the labs-entry one.

    Both sources DECLARE ``site`` here (this sample backend's dataset key IS
    its declaration), and two declarations of one lab are a labs-entry
    collision, which warns by design (spec §9). So the silence pinned here is
    the ELEMENT merge's, and the hosts assertion proves both contributions
    really did land in one lab rather than one source being skipped.
    """
    comp = _composite(
        ("r/global", {"site": [_host("alt1", "10.0.0.1")]}),
        ("r/virtual", {"site": [_host("test2", "10.0.0.2")]}),
    )
    with caplog.at_level(logging.WARNING, logger="otto.labs.composite"):
        lab = comp.load_lab("site")
    assert set(lab.hosts) == {"alt1", "test2"}
    assert not [r for r in caplog.records if "element" in r.getMessage()]


def test_declared_resources_come_from_the_declaring_source() -> None:
    """Spec §8.2: declared per lab, replaced wholesale — never recomputed or unioned.

    Both sources declare ``site``, so the later one's ``labs`` entry replaces
    the earlier one's entirely; a union would keep ``r-old``, which the
    override deliberately dropped, and the reservation gate would then demand
    a resource nobody declares.
    """
    comp = _composite(
        ("r/global", {"site": [_host("alt1", "10.0.0.1")]}, {"site": {"r-old"}}),
        ("r/virtual", {"site": [_host("test4", "10.0.0.3")]}, {"site": {"r-new"}}),
    )
    assert comp.load_lab("site").resources == {"r-new"}


def test_lab_backlink_repaired_on_override() -> None:
    comp = _composite(
        ("r/global", {"site": [_host("alt1", "10.0.0.1")]}),
        ("r/virtual", {"site": [_host("alt1", "10.9.9.9")]}),
    )
    lab = comp.load_lab("site")
    assert lab.hosts["alt1"]._lab is lab


def test_links_merge_keyed_by_id_later_wins() -> None:
    @dataclass
    class FakeLink:
        id: str
        tag: str

    class LinkSource:
        def __init__(self, element, links):
            self._element = element
            self._links = links

        def load_lab(self, name, preferences=None):
            lab = Lab(name=name)
            # A member, because a declared lab that no element matches is an
            # error (spec §9): even a link-only source must carry a host.
            lab.add_host(create_host_from_dict(_host(self._element, "10.0.0.1"), lab_name=name))
            lab.links = list(self._links)
            return lab

        def list_labs(self):
            return ["site"]

    comp = CompositeLabRepository(
        [
            LabSource(
                "r/a", LinkSource("la", [FakeLink("l1", "from-a"), FakeLink("l2", "from-a")])
            ),
            LabSource("r/b", LinkSource("lb", [FakeLink("l1", "from-b")])),
        ]
    )
    links = {link.id: link.tag for link in comp.load_lab("site").links}
    assert links == {"l1": "from-b", "l2": "from-a"}


def test_preferences_forwarded_verbatim_to_every_source() -> None:
    seen: list[object] = []

    class Recorder:
        def __init__(self, element):
            self._element = element

        def load_lab(self, name, preferences=None):
            seen.append(preferences)
            lab = Lab(name=name)
            # A member, for the same reason as the link source above.
            lab.add_host(create_host_from_dict(_host(self._element, "10.0.0.1"), lab_name=name))
            return lab

        def list_labs(self):
            return ["site"]

    prefs = {".*": {"term": ["ssh"]}}
    CompositeLabRepository(
        [LabSource("r/a", Recorder("ra")), LabSource("r/b", Recorder("rb"))]
    ).load_lab("site", preferences=prefs)
    assert seen == [prefs, prefs]
    assert seen[0] is prefs
    assert seen[1] is prefs


def test_not_found_absorbed_when_any_source_knows() -> None:
    comp = _composite(
        ("r/global", {"other": [_host("test4", "10.0.0.3")]}),
        ("r/virtual", {"site": [_host("alt1", "10.0.0.1")]}),
    )
    assert set(comp.load_lab("site").hosts) == {"alt1"}


def test_all_miss_raises_naming_every_label() -> None:
    comp = _composite(
        ("r/global", {"other": [_host("test4", "10.0.0.3")]}),
        ("r/virtual", {"more": [_host("alt1", "10.0.0.1")]}),
    )
    with pytest.raises(LabNotFoundError, match=r"r/global.*r/virtual"):
        comp.load_lab("site")


def test_backend_error_propagates_not_absorbed() -> None:
    class Broken:
        def load_lab(self, name, preferences=None):
            raise LabRepositoryError("db down")

        def list_labs(self):
            # Raises rather than returning: a source whose list_labs answers
            # normally could not prove list_labs propagates (spec §6.2).
            raise LabRepositoryError("db down")

    comp = CompositeLabRepository(
        [
            LabSource("r/ok", ExampleLabRepository(labs={"site": [_host("alt1", "10.0.0.1")]})),
            LabSource("r/db", Broken()),
        ]
    )
    with pytest.raises(LabRepositoryError, match="db down"):
        comp.load_lab("site")
    with pytest.raises(LabRepositoryError, match="db down"):
        comp.list_labs()  # list_labs propagates too (spec §6.2)


def test_empty_composite() -> None:
    comp = CompositeLabRepository([])
    assert comp.list_labs() == []
    assert comp.list_host_summaries() == []
    with pytest.raises(LabNotFoundError, match=r"\[\[lab\.sources\]\]"):
        comp.load_lab("site")


def test_summaries_union_later_wins_labs_unioned() -> None:
    a = ExampleLabRepository(labs={"east": [_host("alt1", "10.0.0.1")]})
    b = ExampleLabRepository(labs={"west": [_host("alt1", "10.9.9.9")]})
    comp = CompositeLabRepository([LabSource("r/a", a), LabSource("r/b", b)])
    (s,) = comp.list_host_summaries()
    assert s.ip == "10.9.9.9"  # later source wins the fields
    assert set(s.labs) == {"east", "west"}  # memberships unioned


def test_summaries_stay_silent_on_a_colliding_host_id(caplog) -> None:
    """Spec §6.3: summaries merge silently — only ``load_lab`` warns.

    Completion reads ``list_host_summaries``, and a WARNING emitted here goes
    to whatever handler the completing process has installed, corrupting the
    candidate list the shell is parsing. The e2e completion test cannot prove
    this: that subprocess never opens a CLI session, so the library
    ``NullHandler`` swallows every record and its ``"overrides" not in output``
    assertion passes whether or not the warning was emitted. This observes the
    logger directly, so it is the one place the silence is actually tested.

    The collision is INJECTED, not assumed: same lab, same host id, different
    ip — and the merged ip is asserted, so a refactor that stopped colliding
    could not leave this passing vacuously.
    """
    comp = _composite(
        ("r/global", {"site": [_host("alt1", "10.0.0.1")]}),
        ("r/virtual", {"site": [_host("alt1", "10.9.9.9")]}),
    )
    with caplog.at_level(logging.WARNING, logger="otto.labs.composite"):
        summaries = comp.list_host_summaries()
    assert [s.ip for s in summaries] == ["10.9.9.9"]  # the collision really happened
    assert not caplog.records


def test_summaries_skip_broken_source() -> None:
    class Broken:
        def load_lab(self, name, preferences=None):
            raise LabRepositoryError("db down")

        def list_labs(self):
            raise LabRepositoryError("db down")

    comp = CompositeLabRepository(
        [
            LabSource("r/db", Broken()),
            LabSource("r/ok", ExampleLabRepository(labs={"site": [_host("alt1", "10.0.0.1")]})),
        ]
    )
    assert [s.id for s in comp.list_host_summaries()] == ["alt1"]


def test_list_labs_sorted_union() -> None:
    comp = _composite(
        ("r/a", {"zeta": [_host("alt1", "10.0.0.1")], "alpha": [_host("test4", "10.0.0.3")]}),
        ("r/b", {"alpha": [_host("test2", "10.0.0.2")]}),
    )
    assert comp.list_labs() == ["alpha", "zeta"]


def test_composite_satisfies_full_conformance_contract() -> None:
    comp = _composite(
        ("r/a", {"east": [_host("alt1", "10.0.0.1")]}),
        (
            "r/b",
            {"west": [_host("test2", "10.0.0.2")], "east": [_host("alt1", "10.9.9.9")]},
        ),
    )
    assert_lab_repository_conforms(comp, expected_labs=["east", "west"])
