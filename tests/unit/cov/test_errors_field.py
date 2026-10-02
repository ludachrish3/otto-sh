"""Field-named input errors pass through the CLI's one translation site untouched."""

import pytest

from otto.cli.invoke import usage_error_from
from otto.coverage.errors import CoverageInputError, NoCoverageHostsError
from otto.docker.build_verbs import DockerBuildError
from otto.errors import FieldError, OttoError


def test_field_error_carries_its_field():
    e = FieldError("tier 'x' is unknown", field="tier")
    assert isinstance(e, OttoError)
    assert e.field == "tier"
    assert str(e) == "tier 'x' is unknown"


@pytest.mark.parametrize("cls", [CoverageInputError, DockerBuildError])
def test_every_field_error_is_a_value_error_and_a_field_error(cls):
    e = cls("m", field="f")
    assert isinstance(e, FieldError)
    assert isinstance(e, ValueError)


@pytest.mark.parametrize(
    ("message", "field", "flags", "hint"),
    [
        ("unknown tier 'ticket'; configured tiers: a, b", "tier", {"tier": "--tier"}, "--tier"),
        (
            "tier 'manual' is a manual-kind tier and requires a ticket",
            "ticket",
            {"tier": "--tier", "ticket": "--ticket"},
            "--ticket",
        ),
        ("no output directory was given", "output_dir", {"output_dir": "--output"}, "--output"),
        ("nothing to build", None, {"tier": "--tier"}, None),
    ],
)
def test_a_field_error_is_never_rewritten_only_hinted(message, field, flags, hint):
    err = usage_error_from(CoverageInputError(message, field=field), flags=flags)
    assert err.message == message
    assert err.param_hint == hint


def test_no_coverage_hosts_error_is_a_value_error():
    assert issubclass(NoCoverageHostsError, ValueError)
