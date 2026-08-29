"""Drift guards: the scaffolded settings.toml must cover SettingsModel exactly."""

import re

import tomli

from otto.cli.init_templates import (
    EXAMPLE_HOST_ENTRY,
    LAB_JSON_TEMPLATE,
    LAB_README_TEMPLATE,
    SETTINGS_TEMPLATE,
)
from otto.models.settings import (
    CoverageSettingsSpec,
    DependenciesSpec,
    DockerSettingsSpec,
    EnvSettingsSpec,
    InventoryConfigSpec,
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
_SECTION_SPECS = {
    "lab": (LabConfigSpec, set()),
    "logging": (LoggingConfigSpec, set()),
    "monitor": (MonitorSettingsSpec, set()),
    "reservations": (ReservationConfigSpec, set()),
    "inventory": (InventoryConfigSpec, set()),
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


def test_lab_template_parses_through_the_runtime_parsers() -> None:
    """The scaffolded lab.json is accepted by the SAME parsers the loader uses.

    Not a re-implemented shape check: a template that only satisfied a
    hand-written assertion here could still be a file otto refuses to load.
    """
    from otto.labs.json_repository import parse_elements, parse_lab_entries, parse_lab_sections
    from otto.models.host import UnixHostSpec

    sections = parse_lab_sections(LAB_JSON_TEMPLATE, "lab.json")
    entries = parse_lab_entries(sections["labs"], "lab.json")
    (element,) = parse_elements(sections["elements"], "lab.json")
    assert entries["example_lab"].resources == {"example-device"}
    assert element.matches("example_lab")  # the lab it declares has a member
    UnixHostSpec.model_validate(element.flatten()[0])


def test_example_host_entry_carries_no_hoisted_key() -> None:
    """`element`/`element_id`/`labs`/`resources` are element- or lab-level in v2."""
    from otto.models.lab import HOISTED_HOST_KEYS

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
