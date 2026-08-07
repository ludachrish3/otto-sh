"""Behavior coverage for the otto.examples.options reference options classes."""

import pytest
from pydantic import ValidationError

from otto.examples.options import (
    DeployInstructionOptions,
    DeviceSuiteOptions,
    RepoOptions,
)


def test_repo_options_defaults():
    opts = RepoOptions()
    assert opts.device_type == "router"
    assert opts.lab_env == "staging"
    assert opts.retries == 3


def test_repo_options_accepts_overrides():
    opts = RepoOptions(device_type="switch", lab_env="production", retries=5)
    assert opts.device_type == "switch"
    assert opts.lab_env == "production"
    assert opts.retries == 5


def test_retries_constraint_rejects_negative():
    # retries is Field(default=3, ge=0); @options validates at construction.
    with pytest.raises(
        ValidationError, match=r"(?m)^retries\n\s+Input should be greater than or equal to 0"
    ):
        RepoOptions(retries=-1)


def test_device_suite_options_inherits_and_adds_firmware():
    opts = DeviceSuiteOptions()
    # inherited repo-wide flags
    assert opts.device_type == "router"
    assert opts.lab_env == "staging"
    assert opts.retries == 3
    # local field
    assert opts.firmware == "latest"
    assert DeviceSuiteOptions(device_type="switch", firmware="2.1").firmware == "2.1"


def test_deploy_instruction_options_inherits_and_adds_debug():
    opts = DeployInstructionOptions()
    # inherited repo-wide flags
    assert opts.device_type == "router"
    assert opts.lab_env == "staging"
    assert opts.retries == 3
    # local field
    assert opts.debug is False
    assert DeployInstructionOptions(debug=True).debug is True


def test_subclasses_inherit_the_retries_constraint():
    # The ge=0 constraint on the base survives inheritance into both subclasses:
    # the failure must name ``retries`` and its bound, not any local field.
    ge_zero = r"(?m)^retries\n\s+Input should be greater than or equal to 0"
    with pytest.raises(ValidationError, match=ge_zero):
        DeviceSuiteOptions(retries=-1)
    with pytest.raises(ValidationError, match=ge_zero):
        DeployInstructionOptions(retries=-1)
