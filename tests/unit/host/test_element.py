"""``Element`` — one frozen object per element (spec 2026-09-05 §3)."""

from dataclasses import FrozenInstanceError

import pytest

from otto.host.element import Element
from otto.models.lab import ElementSpec


def test_slug_is_the_lowercased_name():
    element = Element("Lab X Server")
    assert element.slug == "lab-x-server"
    assert element.name == "Lab X Server"  # the raw string the author wrote, untouched


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [({}, None), ({"id": 103}, 103), ({"id": 0}, 0)],
    ids=["absent", "positive", "zero"],
)
def test_id_is_optional_data(kwargs, expected):
    """``id`` is data, and ``0`` is a value an author may write.

    The zero row is the one that matters: the guard is ``< 0``, so a ``<= 0``
    spelling would refuse a legitimate id and this row would fail.
    """
    assert Element("server", **kwargs).id == expected


def test_name_that_slugs_empty_is_refused():
    with pytest.raises(ValueError, match="slugs to an empty id"):
        Element("---")


def test_negative_id_is_refused():
    with pytest.raises(ValueError, match=">= 0"):
        Element("server", id=-1)


def test_blank_resource_is_refused():
    with pytest.raises(ValueError, match="non-empty"):
        Element("server", resources=frozenset({" "}))


def test_metadata_is_copied_on_construction():
    table = {"rev": 2}
    element = Element("server", metadata=table)
    table["rev"] = 3
    assert element.metadata == {"rev": 2}


def test_resources_are_coerced_to_a_frozenset():
    element = Element("server", resources={"a", "b"})  # type: ignore[arg-type]
    assert element.resources == frozenset({"a", "b"})


def test_a_string_of_resources_is_refused():
    """A bare ``str`` is iterable, so coercion would splatter it into characters.

    ``frozenset("chassis-1")`` is ten one-character resources, every one of
    them non-empty — so the ``resources_nonempty`` check passes and the host
    silently reserves nothing that exists. The field names itself in the error.
    """
    with pytest.raises(ValueError, match="resources"):
        Element("server", resources="chassis-1")  # type: ignore[arg-type]


def test_a_one_shot_iterable_of_resources_is_consumed_exactly_once():
    """Validating from one pass and assigning from a second empties a generator."""
    element = Element("server", resources=(n for n in ["a", "b"]))  # type: ignore[arg-type]
    assert element.resources == frozenset({"a", "b"})


def test_instances_are_frozen():
    element = Element("server")
    with pytest.raises(FrozenInstanceError):
        element.name = "other"  # type: ignore[misc]


def test_to_element_carries_every_field():
    spec = ElementSpec.model_validate(
        {
            "name": "Chassis",
            "id": 7,
            "labs": ["bench"],
            "metadata": {"site": "b"},
            "resources": ["chassis-7"],
            "hosts": [{"ip": "10.0.0.1"}],
        }
    )
    assert spec.to_element() == Element(
        "Chassis", id=7, metadata={"site": "b"}, resources=frozenset({"chassis-7"})
    )


def test_to_element_defaults_when_the_entry_is_minimal():
    spec = ElementSpec.model_validate({"name": "dut", "labs": ["x"], "hosts": [{"ip": "1.1.1.1"}]})
    assert spec.to_element() == Element("dut")
