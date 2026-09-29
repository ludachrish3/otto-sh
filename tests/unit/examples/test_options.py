"""Behavior coverage for the otto.examples.options reference options classes."""

import pytest
from pydantic import ValidationError

from otto.examples.options import (
    DeployInstructionOptions,
    DeviceTestOptions,
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


def test_device_test_options_carries_only_its_own_field():
    # Registered for ``test`` beside RepoOptions, so it repeats none of its flags.
    opts = DeviceTestOptions()
    assert opts.firmware == "latest"
    assert DeviceTestOptions(firmware="2.1").firmware == "2.1"
    assert not isinstance(opts, RepoOptions)


def test_device_test_options_check_interfaces_is_an_on_by_default_bool():
    assert DeviceTestOptions().check_interfaces is True
    assert DeviceTestOptions(check_interfaces=False).check_interfaces is False


def test_deploy_instruction_options_inherits_and_adds_debug():
    opts = DeployInstructionOptions()
    # inherited repo-wide flags
    assert opts.device_type == "router"
    assert opts.lab_env == "staging"
    assert opts.retries == 3
    # local field
    assert opts.debug is False
    assert DeployInstructionOptions(debug=True).debug is True


def test_the_instruction_subclass_inherits_the_retries_constraint():
    # The ge=0 constraint on the base survives inheritance: the failure must
    # name ``retries`` and its bound, not the local field.
    ge_zero = r"(?m)^retries\n\s+Input should be greater than or equal to 0"
    with pytest.raises(ValidationError, match=ge_zero):
        DeployInstructionOptions(retries=-1)
