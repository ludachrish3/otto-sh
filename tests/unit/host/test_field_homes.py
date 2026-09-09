"""Structural guard: every host field has exactly one home (spec 2026-09-09)."""

import dataclasses
import inspect

import pytest

from otto.host.docker_host import DockerContainerHost
from otto.host.embedded_host import EmbeddedHost, ZephyrHost
from otto.host.host import BaseHost
from otto.host.local_host import LocalHost
from otto.host.remote_host import RemoteHost
from otto.host.unix_host import UnixHost

BASES = (BaseHost, RemoteHost)
LEAVES = (UnixHost, EmbeddedHost, ZephyrHost, LocalHost, DockerContainerHost)


@pytest.mark.parametrize("base", BASES)
def test_bases_are_kw_only_unslotted_dataclasses(base):
    assert dataclasses.is_dataclass(base)
    assert "__slots__" not in vars(base), "a slotted base strips __dict__ from every family"
    positional = [f.name for f in dataclasses.fields(base) if f.init and not f.kw_only]
    assert positional == ([] if base is BaseHost else ["ip"])


@pytest.mark.parametrize(
    ("leaf", "positional"),
    [
        (UnixHost, ["ip", "creds"]),
        (EmbeddedHost, ["ip"]),
        (ZephyrHost, ["ip"]),
        (LocalHost, []),
        (
            DockerContainerHost,
            ["parent", "container_id", "project", "service", "compose_project"],
        ),
    ],
)
def test_leaf_positional_constructor_shape(leaf, positional):
    params = [
        p.name
        for p in inspect.signature(leaf).parameters.values()
        if p.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    ]
    assert params == positional


@pytest.mark.parametrize("leaf", LEAVES)
def test_resources_default_factory_is_frozenset(leaf):
    """A plain str would iterate as its CHARACTERS at the gate; the factory is
    the one place that rejects it (kept from the retired contract sweep)."""
    by_name = {f.name: f for f in dataclasses.fields(leaf)}
    assert by_name["resources"].default_factory is frozenset
