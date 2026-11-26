"""Load failures name the project root.

The run gate's wording (otto.session.errors) is pinned beside its package, in
``tests/unit/session/test_run_gate_wording.py``.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from otto.config.scope import BootstrapError, DependencyError
from otto.models.base import failure_reason, validation_message
from otto.models.link import LinkSpec


def test_a_load_failure_names_the_project_root():
    err = BootstrapError(Path("/w/p"), source=".otto/settings.toml", cause=ValueError("x"))
    assert str(err).startswith("project root /w/p: failed to load .otto/settings.toml")
    assert str(err).endswith(": ValueError('x')"), "without a reason, the cause's repr"
    assert str(DependencyError(Path("/w/p"), message="boom")) == "project root /w/p: boom"


def test_a_load_failure_says_the_reason_it_is_given():
    err = BootstrapError(
        Path("/w/p"), source=".otto/settings.toml", cause=ValueError("x"), reason="name: missing"
    )
    assert str(err) == "project root /w/p: failed to load .otto/settings.toml: name: missing"
    assert isinstance(err.__cause__, ValueError)


def test_a_validation_error_is_its_compact_form_and_any_other_error_its_text():
    with pytest.raises(ValidationError, match="protocol") as refused:
        LinkSpec.model_validate({"endpoints": [], "protocol": {"token": "hunter2"}})
    compact = validation_message(refused.value)
    assert compact is not None
    assert "\n" not in compact
    assert "hunter2" not in compact
    assert failure_reason(refused.value) == compact
    other = ValueError("unknown host 'ghost'")
    assert validation_message(other) is None
    assert failure_reason(other) == "unknown host 'ghost'"
