"""docker_priority replaces roles on the unix host spec."""

import pytest
from pydantic import ValidationError

from otto.host.element import Element
from otto.models.host import UnixHostSpec

BASE = {"ip": "10.0.0.1", "name": "dut1", "creds": [{"login": "u", "password": "p"}]}


def test_docker_priority_defaults_to_zero_and_forwards_to_the_host():
    spec = UnixHostSpec.model_validate({**BASE, "docker_capable": True})
    assert spec.docker_priority == 0
    spec = UnixHostSpec.model_validate({**BASE, "docker_capable": True, "docker_priority": 10})
    host = spec.to_host(element=Element("server"))
    assert host.docker_priority == 10
    assert host.docker_capable is True


def test_a_negative_priority_ranks_below_the_default():
    spec = UnixHostSpec.model_validate({**BASE, "docker_capable": True, "docker_priority": -5})
    assert spec.docker_priority == -5


def test_a_priority_on_a_host_that_is_not_docker_capable_is_refused():
    with pytest.raises(
        ValidationError, match="docker_priority on 'dut1', which is not docker_capable"
    ):
        UnixHostSpec.model_validate({**BASE, "docker_priority": 3})


def test_roles_is_gone_and_names_its_replacement():
    with pytest.raises(ValidationError, match="roles is gone; see docker_priority"):
        UnixHostSpec.model_validate({**BASE, "roles": ["edge"]})


def test_the_summary_carries_the_priority():
    from otto.labs.protocol import HostSummary

    assert HostSummary(id="x").docker_priority == 0


def test_the_identity_carries_the_priority_the_spec_declares():
    from otto.host.factory import host_identity

    identity = host_identity(
        {**BASE, "docker_capable": True, "docker_priority": 7}, element=Element("server")
    )
    assert identity.docker_priority == 7
