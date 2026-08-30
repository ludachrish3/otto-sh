"""The lab.json v2 wrapper layers: ``labs`` entries and ``elements`` (spec §2, §7, §9)."""

import pytest
from pydantic import ValidationError

from otto.models.lab import HOISTED_HOST_KEYS, ElementKey, ElementSpec, LabEntrySpec

_HOST = {"ip": "10.0.0.5", "creds": [{"login": "u", "password": "p"}]}


def test_lab_entry_defaults_and_comment_keys() -> None:
    entry = LabEntrySpec.model_validate({"_note": "x"})
    assert entry.resources == set()
    assert entry.metadata == {}
    entry = LabEntrySpec.model_validate({"resources": ["bed", "bed"], "metadata": {"k": 1}})
    assert entry.resources == {"bed"}
    assert entry.metadata == {"k": 1}


def test_lab_entry_forbids_unknown_keys_and_non_object_metadata() -> None:
    with pytest.raises(ValidationError, match=r"resource\s+Extra inputs"):
        LabEntrySpec.model_validate({"resource": ["typo"]})
    # Pinned to the dict_type error, not a bare "metadata": a matcher that only
    # names the field is also satisfied by an extra_forbidden on it, so it would
    # pass in a world where `metadata` was not a field at all (spec §9: a
    # metadata value that is not a JSON object is an error).
    with pytest.raises(ValidationError, match=r"(?m)^metadata\n\s+Input should be a valid dict"):
        LabEntrySpec.model_validate({"metadata": [1]})


def test_lab_entry_resources_must_be_nonempty_strings() -> None:
    """All three levels share one notion of a usable identifier (spec 2026-08-28
    three-level-reservations §2, §10).

    Anchored on the validator's exact sentence rather than a loose
    ``resources.*non-empty``: the failure text carries this module's locals, so
    a wide pattern can be satisfied by a name in the test itself.
    """
    with pytest.raises(ValidationError, match=r"resources must be non-empty strings"):
        LabEntrySpec.model_validate({"resources": [" "]})
    assert LabEntrySpec.model_validate({"resources": ["bed"]}).resources == {"bed"}


def test_element_flattens_identity_onto_hosts() -> None:
    el = ElementSpec.model_validate(
        {"name": "dut", "id": 3, "labs": ["embedded"], "hosts": [_HOST, {**_HOST, "board": "mgmt"}]}
    )
    flat = el.flatten()
    assert [h["element"] for h in flat] == ["dut", "dut"]
    assert [h["element_id"] for h in flat] == [3, 3]
    assert flat[1]["board"] == "mgmt"
    assert el.key == ElementKey("dut", 3)


def test_element_without_id_flattens_without_element_id() -> None:
    el = ElementSpec.model_validate({"name": "test1", "labs": ["unix"], "hosts": [_HOST]})
    assert "element_id" not in el.flatten()[0]
    assert el.key == ElementKey("test1", None)


def test_element_key_is_a_hashable_value_so_a_later_source_replaces_an_earlier_one() -> None:
    """``key`` is a frozen dataclass, not a tuple — but still the merge's dict key (spec §6)."""
    first = ElementSpec.model_validate({"name": "dut", "id": 3, "labs": ["a"], "hosts": [_HOST]})
    second = ElementSpec.model_validate({"name": "dut", "id": 3, "labs": ["b"], "hosts": [_HOST]})
    merged = {first.key: first}
    merged[second.key] = second
    assert merged == {ElementKey("dut", 3): second}
    assert ElementKey("dut", 3) != ElementKey("dut", None)


def test_element_key_str_is_bare_name_without_an_id_and_a_pair_with_one() -> None:
    """A key without a repeat ``id`` (the common case) renders as its bare name, not
    ``('bb1350', None)`` — the ``None`` reads as a bug in user-facing output (the
    ``owner`` column, doctor warnings, composite-lab messages).
    """
    assert str(ElementKey("bb1350")) == "bb1350"
    assert str(ElementKey("dut", 1)) == "('dut', 1)"


def test_flatten_does_not_alias_the_entry() -> None:
    el = ElementSpec.model_validate({"name": "a", "labs": ["l"], "hosts": [_HOST]})
    el.flatten()[0]["ip"] = "changed"
    assert el.hosts[0]["ip"] == "10.0.0.5"


@pytest.mark.parametrize("key", sorted(HOISTED_HOST_KEYS))
def test_hoisted_key_on_a_host_entry_names_key_and_element(key: str) -> None:
    with pytest.raises(ValidationError, match=rf"'{key}'.*element 'dut'|element 'dut'.*'{key}'"):
        ElementSpec.model_validate({"name": "dut", "labs": ["l"], "hosts": [{**_HOST, key: "x"}]})


def test_hoisted_keys_no_longer_include_resources() -> None:
    """Spec 2026-08-28 three-level-reservations §2: a host entry may carry resources again.

    Spelled ``sorted(...) == [...]`` rather than against a ``frozenset``
    literal: ruff's SIM300 reads the ALL-CAPS name as the constant and demands
    the literal on the left, which reads backwards.
    """
    assert sorted(HOISTED_HOST_KEYS) == ["element", "element_id", "labs"]


def test_element_and_host_resources_are_sets_of_nonempty_strings() -> None:
    """Spec 2026-08-28 three-level-reservations §2: the element and the slot declare too.

    Anchored on the validator's exact sentence rather than a loose
    ``resources.*non-empty``: the failure text carries this module's locals,
    so a wide pattern can be satisfied by a name in the test itself.
    """
    el = ElementSpec.model_validate(
        {
            "name": "chassis",
            "labs": ["rig"],
            "resources": ["chassis-1", "chassis-1"],
            "hosts": [{**_HOST, "resources": ["slot-1"]}],
        }
    )
    assert el.resources == {"chassis-1"}
    # The host's own set is not hoisted anywhere: it rides inside the entry,
    # untouched, to the host spec that validates it.
    assert el.flatten()[0]["resources"] == ["slot-1"]
    with pytest.raises(ValidationError, match=r"resources must be non-empty strings"):
        ElementSpec.model_validate(
            {"name": "c", "labs": ["l"], "resources": [""], "hosts": [_HOST]}
        )
    bare = ElementSpec.model_validate({"name": "c", "labs": ["l"], "hosts": [_HOST]})
    assert bare.resources == set()


def test_membership_is_fullmatch() -> None:
    el = ElementSpec.model_validate({"name": "a", "labs": ["unix"], "hosts": [_HOST]})
    assert el.matches("unix")
    assert not el.matches("unix2")  # re.search semantics would admit this — must not
    assert not el.matches("myunix")


def test_membership_patterns() -> None:
    el = ElementSpec.model_validate(
        {"name": "a", "labs": ["unix(\\..*)?", "bb.*"], "hosts": [_HOST]}
    )
    assert el.matches("unix")
    assert el.matches("unix.rack-b4")
    assert el.matches("bb1350")
    assert not el.matches("embedded")
    everything = ElementSpec.model_validate({"name": "b", "labs": [".*"], "hosts": [_HOST]})
    assert everything.matches("anything-at-all")


def test_invalid_pattern_names_element_and_pattern() -> None:
    with pytest.raises(ValidationError, match=r"element 'dut'.*'unix\('"):
        ElementSpec.model_validate({"name": "dut", "labs": ["unix("], "hosts": [_HOST]})


def test_empty_labs_and_empty_hosts_error() -> None:
    with pytest.raises(ValidationError, match="labs"):
        ElementSpec.model_validate({"name": "dut", "labs": [], "hosts": [_HOST]})
    with pytest.raises(ValidationError, match="hosts"):
        ElementSpec.model_validate({"name": "dut", "labs": ["l"], "hosts": []})


def test_name_must_slug_nonempty_and_id_nonnegative() -> None:
    with pytest.raises(ValidationError, match="slugs to an empty id"):
        ElementSpec.model_validate({"name": "---", "labs": ["l"], "hosts": [_HOST]})
    with pytest.raises(ValidationError, match=">= 0"):
        ElementSpec.model_validate({"name": "dut", "id": -1, "labs": ["l"], "hosts": [_HOST]})


def test_element_metadata_must_be_an_object() -> None:
    """Spec §9: a metadata value that is not a JSON object errors at EVERY level.

    The other two levels have their own guards — the ``labs`` entry above, and
    the host spec in ``tests/unit/host/test_metadata.py`` — so only the element
    was unpinned. Anchored on the loc line AND the dict_type message for the
    same reason as its siblings: a bare ``match="metadata"`` is satisfied by the
    extra_forbidden error a spec WITHOUT the field raises, so it would pass in
    exactly the world this test denies.
    """
    with pytest.raises(ValidationError, match=r"(?m)^metadata\n\s+Input should be a valid dict"):
        ElementSpec.model_validate(
            {"name": "dut", "labs": ["l"], "metadata": ["not", "a", "table"], "hosts": [_HOST]}
        )


def test_element_comment_keys_stripped_and_unknown_keys_forbidden() -> None:
    el = ElementSpec.model_validate({"_doc": "x", "name": "a", "labs": ["l"], "hosts": [_HOST]})
    assert el.name == "a"
    with pytest.raises(ValidationError, match=r"host\s+Extra inputs"):
        ElementSpec.model_validate({"name": "a", "labs": ["l"], "host": [_HOST]})
