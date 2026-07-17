"""Drift guards: the scaffolded settings.toml must cover SettingsModel exactly."""

import re

import tomli

from otto.cli.init_templates import SETTINGS_TEMPLATE
from otto.models.settings import (
    CoverageSettingsSpec,
    DockerSettingsSpec,
    LabConfigSpec,
    LoggingConfigSpec,
    MonitorSettingsSpec,
    ReservationConfigSpec,
    SettingsModel,
)

# Commented-out TOML is "#key" / "#[table]" (no space after #); prose is "# ".
# The "#:schema" editor directive is excluded by its ":".
_COMMENTED = re.compile(r"^#(?![ :])")

# Intentionally omitted from the template: legacy passthrough consumed by nobody.
_OMITTED_TOP_LEVEL = {"lab_data_type"}
# Per-section omissions: free-form sub-tables pointed at docs instead.
_SECTION_SPECS = {
    "lab": (LabConfigSpec, set()),
    "logging": (LoggingConfigSpec, set()),
    "monitor": (MonitorSettingsSpec, set()),
    "reservations": (ReservationConfigSpec, set()),
    "coverage": (CoverageSettingsSpec, {"embedded"}),
    "docker": (DockerSettingsSpec, set()),
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
    assert model.lab.backend == "json"
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
