"""One rule picks the parent when --parent is omitted."""

import pytest

from otto.docker.observe import (
    DockerVerbError,
    default_docker_parent,
    docker_parent,
    docker_parents,
)

from .test_deploy import _host
from .test_deploy import _lab as _lab_of


def _lab(**hosts):
    """hosts: id -> (docker_capable, docker_priority). Builds a lab named 'east'."""
    built = []
    for n, (hid, (capable, priority)) in enumerate(hosts.items(), start=1):
        host = _host(hid, f"10.10.200.{n}")
        host.docker_capable = capable
        host.docker_priority = priority
        built.append(host)
    return _lab_of(*built, name="east")


def test_the_only_docker_capable_host_is_the_default():
    lab = _lab(dut1=(True, 0), dut2=(False, 0))
    assert default_docker_parent(lab).id == "dut1"
    assert docker_parent(lab, None).id == "dut1"


def test_the_highest_priority_wins_among_several():
    lab = _lab(a=(True, 0), b=(True, 10), c=(True, 3))
    assert default_docker_parent(lab).id == "b"


def test_a_negative_priority_loses_to_the_default_zero():
    lab = _lab(a=(True, 0), b=(True, -1))
    assert default_docker_parent(lab).id == "a"


def test_a_tie_at_the_top_refuses_and_names_the_tied_hosts():
    lab = _lab(alt2=(True, 0), test1=(True, 0), test3=(True, 0))
    with pytest.raises(DockerVerbError) as exc:
        default_docker_parent(lab)
    assert exc.value.field == "parent"
    assert "3 docker-capable hosts at priority 0 (alt2, test1, test3)" in str(exc.value)
    assert "--parent" in str(exc.value)
    assert "docker_priority" in str(exc.value)


def test_a_tie_between_the_top_two_refuses_even_with_a_third_below():
    lab = _lab(a=(True, 5), b=(True, 5), c=(True, 0))
    with pytest.raises(DockerVerbError, match=r"2 docker-capable hosts at priority 5 \(a, b\)"):
        default_docker_parent(lab)


def test_no_docker_capable_host_refuses():
    lab = _lab(dut1=(False, 0))
    with pytest.raises(DockerVerbError, match="no docker-capable unix host") as exc:
        default_docker_parent(lab)
    assert exc.value.field == "parent"


def test_an_explicit_parent_is_checked_not_ranked():
    lab = _lab(a=(True, 10), b=(True, 0))
    assert docker_parent(lab, "b").id == "b"
    with pytest.raises(DockerVerbError, match="'nope' is not a docker-capable unix host") as exc:
        docker_parent(lab, "nope")
    assert exc.value.field == "parent"


def test_the_fleet_is_every_docker_capable_host_or_the_one_named():
    lab = _lab(a=(True, 0), b=(True, 0), c=(False, 0))
    assert [h.id for h in docker_parents(lab, None)] == ["a", "b"]
    assert [h.id for h in docker_parents(lab, "b")] == ["b"]
