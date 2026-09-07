"""Drift guards: the scaffolded settings.toml must cover SettingsModel exactly."""

import json
import re

import tomli

from otto.cli.init_templates import (
    CREDS_JSON_TEMPLATE,
    EXAMPLE_HOST_ENTRY,
    EXAMPLE_INVENTORY_KEY,
    INVENTORY_JSON_TEMPLATE,
    LAB_JSON_TEMPLATE,
    LAB_README_TEMPLATE,
    SETTINGS_TEMPLATE,
)
from otto.models.settings import (
    CoverageSettingsSpec,
    DependenciesSpec,
    DockerSettingsSpec,
    EnvSettingsSpec,
    LabConfigSpec,
    LoggingConfigSpec,
    MonitorSettingsSpec,
    ProjectScopeSpec,
    ReservationConfigSpec,
    SettingsModel,
)

# Commented-out TOML is "#key" / "#[table]" (no space after #); prose is "# ".
# The "#:schema" editor directive is excluded by its ":".
_COMMENTED = re.compile(r"^#(?![ :])")

# Every top-level SettingsModel field is templated — nothing is intentionally
# omitted now that the legacy passthrough fields are gone.
_OMITTED_TOP_LEVEL: set[str] = set()
# Per-section omissions: free-form sub-tables pointed at docs instead.
#
# [inventory]/[creds] are NOT here: unlike every other section below, they are
# LIVE (spec 2026-09-06 §8.1), so their table is a working json-backend
# example, not a commented reference enumerating every field — `extra="allow"`
# kwargs (`path`, `supplies`) legitimately sit beside the declared ones. Their
# own coverage is test_uncommented_template_is_settings_model_valid's spot
# checks, not this exhaustiveness sweep.
_SECTION_SPECS = {
    "lab": (LabConfigSpec, set()),
    "logging": (LoggingConfigSpec, set()),
    "monitor": (MonitorSettingsSpec, set()),
    "reservations": (ReservationConfigSpec, set()),
    "coverage": (CoverageSettingsSpec, {"embedded"}),
    "docker": (DockerSettingsSpec, set()),
    "dependencies": (DependenciesSpec, set()),
    "project": (ProjectScopeSpec, set()),
    "env": (EnvSettingsSpec, set()),
}


def _uncommented() -> dict:
    rendered = SETTINGS_TEMPLATE.format(
        name="widget", version="0.1.0", init_module="widget_instructions"
    )
    text = "\n".join(_COMMENTED.sub("", line) for line in rendered.splitlines())
    return tomli.loads(text)


def test_uncommented_template_is_settings_model_valid() -> None:
    model = SettingsModel.model_validate(_uncommented())
    assert model.name == "widget"
    # spot-check each section survived into the model, not just parsed
    assert model.lab is not None
    assert [(s.backend, s.model_extra) for s in model.lab.sources] == [
        ("json", {"paths": ["lab_data"]})
    ]
    assert model.reservations.backend == "none"
    assert "nightly" in model.coverage.tiers
    assert model.docker.images[0].name == "widget-test"
    assert model.host_preferences[".*"]["term"] == ["ssh", "telnet"]
    assert model.os_profiles["my-os"].base == "unix"
    assert model.inventory is not None
    assert model.inventory.model_extra["path"] == "lab_data/inventory.json"
    assert model.inventory.model_extra["supplies"] == ["ip"]
    assert model.creds is not None
    assert model.creds.model_extra["path"] == "lab_data/creds.json"


def test_template_mentions_every_top_level_settings_field() -> None:
    data = _uncommented()
    model_fields = set(SettingsModel.model_fields)
    assert model_fields - set(data) == _OMITTED_TOP_LEVEL
    assert set(data) <= model_fields  # no keys the model doesn't know


def test_template_mentions_every_fixed_section_field() -> None:
    data = _uncommented()
    for section, (spec, omitted) in _SECTION_SPECS.items():
        assert set(spec.model_fields) - set(data[section]) == omitted, section
        assert set(data[section]) <= set(spec.model_fields), section


def test_lab_template_parses_through_the_runtime_parsers_and_joins_its_siblings(tmp_path) -> None:
    """The scaffolded files are accepted by the SAME parsers and join the loader uses."""
    from otto.creds import JsonCredsStore
    from otto.inventory import CredsOverlay, JsonInventory, resolve_host_entry
    from otto.labs.json_repository import parse_elements, parse_lab_entries, parse_lab_sections
    from otto.models.host import UnixHostSpec
    from otto.models.lab import HOISTED_HOST_KEYS

    sections = parse_lab_sections(LAB_JSON_TEMPLATE, "lab.json")
    entries = parse_lab_entries(sections["labs"], "lab.json")
    (element,) = parse_elements(sections["elements"], "lab.json")
    assert entries["example_lab"].resources == {"example-device"}
    assert element.matches("example_lab")
    (tmp_path / "inventory.json").write_text(json.dumps(INVENTORY_JSON_TEMPLATE))
    (tmp_path / "creds.json").write_text(json.dumps(CREDS_JSON_TEMPLATE))
    inventory = CredsOverlay(
        JsonInventory(tmp_path / "inventory.json", supplies=["ip"]),
        store=JsonCredsStore(tmp_path / "creds.json"),
    )
    resolved = resolve_host_entry(element.hosts[0], inventory, element.to_element()).host_data
    spec = UnixHostSpec.model_validate(resolved)
    assert spec.ip == "192.0.2.1"
    assert EXAMPLE_HOST_ENTRY["inventory"] == EXAMPLE_INVENTORY_KEY
    assert (
        EXAMPLE_INVENTORY_KEY != "example-device"
    )  # a key is the machine's name, never an otto id
    # `element`/`element_id`/`labs` are element-level in v2; not `resources`
    # (it came back to the host entry with spec 2026-08-28
    # three-level-reservations, so it is no longer a hoisted key). Driven off
    # the live set, so this follows the rule rather than restating it.
    assert set(EXAMPLE_HOST_ENTRY) & HOISTED_HOST_KEYS == set()


def test_lab_readme_documents_the_v2_shape_and_the_migration() -> None:
    """The scaffolded README is the first lab-file reference a user reads."""
    for section in ("## The `labs` table", "## Fields in the example element"):
        assert section in LAB_README_TEMPLATE
    for field in ("`resources`", "`metadata`", "`labs`", "`name`", "`id`", "`hosts`"):
        assert field in LAB_README_TEMPLATE
    # The hard cutover has to be named, not implied — a reader arriving from a
    # v1 repo needs to be told where their `hosts` array went.
    assert "Migrating from the hosts array" in LAB_README_TEMPLATE
    assert "are NOT host fields" in LAB_README_TEMPLATE
    for phrase in ("inventory.json", "creds.json", "inventory key", "lab.json over"):
        assert phrase in LAB_README_TEMPLATE
