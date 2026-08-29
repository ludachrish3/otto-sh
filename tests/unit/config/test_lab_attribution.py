"""``source_lab`` and ``component_names`` survive everything — merges included (spec §4)."""

from typing import Any

from otto.config.lab import (
    Lab,
    load_lab,
)


def _host(element: str, ip: str, lab_name: str | None = None) -> Any:
    from otto.host.factory import create_host_from_dict

    return create_host_from_dict(
        {
            "element": element,
            "os_type": "unix",
            "ip": ip,
            "creds": [{"login": "admin", "password": "admin"}],
        },
        lab_name=lab_name,
    )


def _lab(name: str, elements: list[str]) -> Lab:
    lab = Lab(name=name)
    for element in elements:
        lab.add_host(_host(element, f"10.0.0.{len(lab.hosts) + 1}", lab_name=name))
    return lab


class _UnstampedRepo:
    """A backend that builds hosts without ``lab_name`` AND bypasses ``add_host``.

    Deliberately the worst case: nothing but ``load_lab``'s own per-component
    sweep can attribute these hosts, so a sweep that ran after the merge (or
    lived only inside ``add_host``) would stamp them with the composite name.
    """

    def __init__(self) -> None:
        self._octet = 1

    def load_lab(
        self,
        name: str,
        preferences: dict[str, dict[str, Any]] | None = None,
        inventory: object = None,
    ) -> Lab:
        lab = Lab(name=name)
        host = _host(f"{name}-box", f"10.0.0.{self._octet}")
        self._octet += 1
        lab.hosts[host.id] = host  # straight into the dict: no add_host backstop
        return lab


def test_factory_stamps_source_lab():
    # No Lab anywhere in this test: only the factory can have stamped it.
    assert _host("h1", "10.0.0.1", lab_name="tech-1").source_lab == "tech-1"


def test_factory_leaves_an_unstamped_host_empty():
    assert _host("h1", "10.0.0.1").source_lab == ""


def test_factory_stamp_beats_the_lab_that_registers_the_host():
    host = _host("h1", "10.0.0.1", lab_name="tech-1")
    Lab(name="borrower").add_host(host)
    assert host.source_lab == "tech-1"


def test_unstamped_host_is_attributed_to_the_lab_it_joins():
    # The container-host shape: built post-load, outside the factory's lab_name.
    host = _host("h1", "10.0.0.1")
    Lab(name="late-joiner").add_host(host)
    assert host.source_lab == "late-joiner"


def test_a_fresh_lab_is_its_own_only_component():
    assert Lab(name="solo").component_names == ["solo"]


def test_merge_preserves_per_host_attribution_and_component_names():
    merged = _lab("a", ["h1"]) + _lab("b", ["h2"])

    assert merged.name == "a+b"
    assert merged.component_names == ["a", "b"]
    by_id = {h.id: h.source_lab for h in merged.hosts.values()}
    # ids include element composition; match on the stamped values instead
    assert sorted(by_id.values()) == ["a", "b"]


def test_load_lab_backstop_stamps_the_component_not_the_composite():
    lab = load_lab("a+b", repository=_UnstampedRepo())

    assert lab.name == "a+b"
    stamps = sorted(h.source_lab for h in lab.hosts.values() if h.id != "local")
    assert stamps == ["a", "b"]  # NOT ["a+b", "a+b"]


def test_load_lab_attributes_the_injected_local_host_to_the_first_component():
    lab = load_lab("a+b", repository=_UnstampedRepo())

    assert lab.hosts["local"].source_lab == "a"


def test_every_host_of_an_example_lab_carries_attribution():
    from otto.examples.lab_repository import ExampleLabRepository

    lab = load_lab("east", repository=ExampleLabRepository())

    assert "local" in lab.hosts
    unattributed = [h.id for h in lab.hosts.values() if not h.source_lab]
    assert not unattributed
    assert {h.source_lab for h in lab.hosts.values()} == {"east"}
