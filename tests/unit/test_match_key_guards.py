"""The match keys stay in step with the host model (#1312).

``MATCH_KEYS`` is a hand-written set, and it once fell behind the host model:
``board`` and ``is_virtual`` were host fields no match table could name. These
guards make a new field a decision instead of an omission:

- every field a lab file can state, on a built-in host spec or on the element,
  is either a match key or in :data:`_NOT_MATCHABLE` with a reason;
- a match key whose host value is an object is in the table that reads its
  registered name;
- every match key resolves on a real host of each built-in family, built the
  way a lab file builds it, and on a container host.

The not-matchable set lives here, not in ``src``: no product code reads it.
The guard covers otto's own specs only. A project's registered spec is never
checked, at test time or at load.
"""

import re
from typing import Any

import pytest

from otto.declared import _OBJECT_KEY_PATHS, MATCH_KEYS, host_matches
from otto.host.construction import Convert, field_kind
from otto.host.docker_host import DockerContainerHost
from otto.host.element import Element
from otto.host.factory import create_host_from_dict
from otto.host.os_profile import registered_host_specs
from otto.models.lab import ElementSpec
from tests._fixtures.docs_sections import page_section
from tests._fixtures.paths import CHECKOUT_ROOT

_NOT_MATCHABLE: dict[str, frozenset[str]] = {
    "credentials": frozenset({"creds"}),
    "an option table, not a fact of the host": frozenset(
        {
            "console_options",
            "ftp_options",
            "nc_options",
            "reboot_options",
            "scp_options",
            "sftp_options",
            "ssh_options",
            "telnet_options",
            "userland_options",
        }
    ),
    "a selection otto makes, or the menu it selects from": frozenset(
        {"term", "transfer", "impairer", "valid_terms", "valid_transfers", "valid_impairers"}
    ),
    "tuning of how otto drives the host": frozenset(
        {
            "debug_log_globs",
            "default_dest_dir",
            "docker_priority",
            "has_bash",
            "init_timeout",
            "log",
            "max_filename_len",
            "session_setup",
            "shell_history",
        }
    ),
    "object-valued on the host, left out for now": frozenset({"command_frame", "landing_frame"}),
    "a structured value no scalar can match": frozenset(
        {
            "interfaces",
            "inventory",
            "power_control",
            "resources",
            "snmp",
            "toolchain",
            "element.hosts",
            "element.labs",
            "element.resources",
        }
    ),
    "reached by dotted path, not as a key": frozenset({"metadata", "element.metadata"}),
    "ruled out (spec 2026-11-24, ruling 2)": frozenset({"hop"}),
}
"""Every lab-stated field that is deliberately not a match key, by reason."""

_RUNTIME_ONLY = frozenset({"id", "source_lab"})
"""Match keys no lab entry states: otto computes the id and stamps the source lab."""


def _lab_stated_fields() -> set[str]:
    """Return every field of every built-in host spec, and the element's as ``element.<field>``."""
    fields: set[str] = set()
    for spec in registered_host_specs(builtins_only=True).values():
        fields |= set(spec.model_fields)
    return fields | {f"element.{name}" for name in ElementSpec.model_fields}


def test_every_lab_stated_field_is_classified():
    """A new host or element field must be made a match key or given a reason not to be.

    A name is classified once for every spec that has it, so it must mean the
    same thing on each.
    """
    fields = _lab_stated_fields()
    not_matchable = frozenset().union(*_NOT_MATCHABLE.values())
    assert sum(len(names) for names in _NOT_MATCHABLE.values()) == len(not_matchable), (
        "a field is in two _NOT_MATCHABLE groups"
    )

    both = sorted(MATCH_KEYS & not_matchable)
    assert not both, f"both a match key and not matchable: {both}"

    unclassified = sorted(fields - MATCH_KEYS - not_matchable)
    assert not unclassified, (
        f"unclassified host/element fields: {unclassified}. Add each to MATCH_KEYS "
        f"(src/otto/declared.py) if a match table should be able to name it, else to a "
        f"_NOT_MATCHABLE group here."
    )

    stale = sorted(not_matchable - fields)
    assert not stale, f"_NOT_MATCHABLE names no spec declares any more: {stale}"

    assert MATCH_KEYS - fields == _RUNTIME_ONLY, (
        "a match key that is neither a lab-stated field nor a known runtime-only key"
    )


def test_an_object_valued_key_is_in_the_path_table():
    """A match key the spec converts to an object must read the object's registered name.

    ``Convert`` is the spec's marker for a field whose host value is built from
    the lab file's text. For a match key that means the host holds an object
    while the table holds the text, so a plain attribute walk would compare a
    pattern against ``str(object)`` and silently never match.
    """
    converted: set[str] = set()
    for spec in registered_host_specs(builtins_only=True).values():
        converted |= {
            name
            for name in spec.model_fields
            if name in MATCH_KEYS and isinstance(field_kind(spec, name), Convert)
        }
    assert converted == set(_OBJECT_KEY_PATHS)
    for key, path in _OBJECT_KEY_PATHS.items():
        assert path.split(".")[0] == key, f"{key}: the path must start at the key's own attribute"


# ── every key resolves on a real host ────────────────────────────────────────

_ELEMENT = Element(name="dut", id=7)

_COMMON: dict[str, Any] = {
    "ip": "192.0.2.1",
    "name": "dut-main",
    "os_name": "Linux",
    "os_version": "6.8",
    "hw_version": "rev2",
    "sw_version": "1.2.3",
    "board": "qemu",
    "slot": 1,
    "site": "lab-a",
    "rack": "2",
    "shelf": 3,
    "is_virtual": True,
}
"""A non-default value for every match key every family has, as a lab entry states it."""

_FAMILY: dict[str, dict[str, Any]] = {
    "unix": {"docker_capable": True},
    # the bare embedded profile names no command frame of its own
    "embedded": {"command_frame": "zephyr", "filesystem": "littlefs", "loader": "llext-hex"},
    "zephyr": {"filesystem": "littlefs", "loader": "llext-hex"},
}
"""Each built-in family's own match keys, plus what its entry needs to load."""

_REQUIRED: dict[str, dict[str, Any]] = {
    "unix": {},
    "embedded": {"command_frame": "zephyr"},
    "zephyr": {},
}
"""What each family's entry needs beyond ``ip`` and ``os_type``."""

_DERIVED: dict[str, Any] = {
    "id": "dut-qemu-1",
    "element.name": "dut",
    "element.id": 7,
    "source_lab": "guardlab",
    # a rack written as digits is an integer on the host
    "rack": 2,
}
"""What the factory makes of the entry above, where that is not the entry's own text."""

_NOT_HOST_FIELDS = frozenset({"id", "element.name", "element.id", "source_lab"})
"""Match keys every lab host has that are not fields of its own spec."""


def _differs(value: Any) -> Any:
    """Return a match value of the same type that *value* does not equal."""
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 1
    return f"not-{value}"


def test_the_families_are_the_built_in_registry():
    """A new built-in family must be given an entry here, so it cannot go untested."""
    assert set(_FAMILY) == set(_REQUIRED) == set(registered_host_specs(builtins_only=True))


@pytest.mark.parametrize("family", sorted(_FAMILY))
def test_every_key_resolves_on_a_real_host(family: str):
    """Each key matches the value the lab entry gave, through the path a lab file takes.

    Built by the factory rather than by hand, so a host attribute that is
    renamed, or that becomes an object as ``filesystem`` is, fails here.
    """
    entry = {**_COMMON, **_FAMILY[family], "os_type": family}
    host = create_host_from_dict(entry, lab_name="guardlab", element=_ELEMENT)
    expected = {**entry, **_DERIVED}
    spec = registered_host_specs(builtins_only=True)[family]
    applicable = (MATCH_KEYS & set(spec.model_fields)) | _NOT_HOST_FIELDS
    # every key the family has must be given a value here: an omitted one
    # would be read as "the family lacks it" and pass the no-match leg below
    unvalued = sorted(applicable - set(expected))
    assert not unvalued, f"{family}: no value for {unvalued}"
    for key in sorted(MATCH_KEYS):
        if key in applicable:
            value = expected[key]
            assert host_matches({key: value}, host), f"{family}: {key} = {value!r} did not match"
            other = _differs(value)
            assert not host_matches({key: other}, host), f"{family}: {key} = {other!r} matched"
        else:
            # a key this family lacks never matches, and never raises
            assert not host_matches({key: ".*"}, host), f"{family}: {key} matched"
            assert not host_matches({key: True}, host), f"{family}: {key} matched"


@pytest.mark.parametrize("family", sorted(_FAMILY))
def test_unset_keys_read_their_defaults(family: str):
    """What a key reads when the lab entry does not set it: some are never absent."""
    entry = {"ip": "192.0.2.2", "os_type": family, **_REQUIRED[family]}
    host = create_host_from_dict(entry, element=Element(name="dut"))
    # always set, whatever the entry leaves out
    assert host_matches({"id": "dut"}, host)
    assert host_matches({"element.name": "dut"}, host)
    assert host_matches({"os_type": family}, host)
    # no name written: the generated label, here the element's name alone
    assert host_matches({"name": "dut"}, host)
    assert host_matches({"is_virtual": False}, host)
    never = ["element.id", "os_version", "hw_version", "sw_version"]
    never += ["board", "slot", "site", "rack", "shelf"]
    for key in never:
        assert not host_matches({key: ".*"}, host), key
        assert not host_matches({key: 0}, host), key
    # built outside a lab load, the host names no lab
    assert not host_matches({"source_lab": ".+"}, host)
    os_name = {"unix": "Linux", "zephyr": "Zephyr"}.get(family)
    if os_name is None:
        assert not host_matches({"os_name": ".*"}, host)
    else:
        assert host_matches({"os_name": os_name}, host)
    if family == "unix":
        assert host_matches({"docker_capable": False}, host)
    else:
        # no filesystem is the registered `none`, as a lab file writes it
        assert host_matches({"filesystem": "none"}, host)
        assert not host_matches({"loader": ".*"}, host)


def test_container_host_facts():
    """Declared entries also apply to container hosts, which no lab spec builds.

    A container has an id, a name, ``is_virtual`` and the parent's source lab,
    and none of a lab host's other facts.
    """
    # a parent whose name, id and label all differ, so the assertion below
    # cannot pass on the wrong one
    parent = create_host_from_dict(
        {**_COMMON, "os_type": "unix"}, lab_name="guardlab", element=_ELEMENT
    )
    assert len({parent.id, parent.name}) == 2
    host = DockerContainerHost(
        parent=parent,
        container_id="abc123def456",
        project="project1",
        service="api",
        compose_project="otto-project1-vagrant",
    )
    assert host_matches({"is_virtual": True}, host)
    assert host_matches({"name": f"{parent.id}:api"}, host)
    assert host_matches({"id": f"{parent.id}.project1.api"}, host)
    # compose stamps the parent's lab on the container it builds
    # (tests/unit/docker/test_compose.py pins that); here, only that the key reads it
    host.source_lab = parent.source_lab
    assert host_matches({"source_lab": "guardlab"}, host)
    for key in sorted(MATCH_KEYS - {"id", "name", "is_virtual", "source_lab"}):
        assert not host_matches({key: ".*"}, host), key


# ── the docs name exactly the keys ───────────────────────────────────────────

_DOCS_PAGE = CHECKOUT_ROOT / "docs" / "configuration" / "declared-products-tools.md"
_DOCS_HEADING = "## Matching"


def test_the_docs_table_is_the_key_set():
    """The reference's key table has one row per match key, and no other row.

    Scoped to the Matching section's key table, the one whose header starts
    ``| Key |``. Every row of it is read, whatever its other cells hold, so a
    row for a name that is not a key cannot hide behind an odd cell.
    """
    section = page_section(_DOCS_PAGE.read_text(), _DOCS_HEADING, _DOCS_PAGE)
    assert section.count("\n| Key |") == 1, "the Matching section needs exactly one key table"
    table = section.split("\n| Key |", 1)[1].split("\n\n", 1)[0]
    body = table.splitlines()[2:]  # past the header's own line and the |---| rule
    rows = []
    for line in body:
        cell = re.fullmatch(r"\| `([^`|]+)` \|.*\|", line)
        assert cell, f"not a key row: {line!r}"
        rows.append(cell.group(1))
    assert len(rows) == len(set(rows)), f"duplicate rows: {sorted(rows)}"
    assert set(rows) == MATCH_KEYS, (
        f"missing rows: {sorted(MATCH_KEYS - set(rows))}; "
        f"rows for no key: {sorted(set(rows) - MATCH_KEYS)}"
    )
