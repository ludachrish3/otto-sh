"""The live declaration agrees with the runtime (spec 1 §6, "Agreement tests").

Every namespace ``api/public.toml`` declares imports in a fresh interpreter, has a
literal ``__all__`` of unique names, and binds each of them at runtime (a name
bound only under ``TYPE_CHECKING`` fails); a name reachable from two namespaces is
one object at both. ``scripts/api_agreement.py`` is the machinery, and its fixture
tests (``tests/unit/scripts/test_api_agreement.py``) prove each check can fail.
This module runs the checks over the live declaration on every interpreter of the
matrix, because a binding can depend on the Python version.
"""

import importlib
from typing import NamedTuple

import pytest

from scripts.api_agreement import agreement_failures, namespace_reports
from scripts.api_manifest import load_manifest
from tests._fixtures.paths import CHECKOUT_ROOT

MANIFEST = CHECKOUT_ROOT / "api" / "public.toml"


class MovedBinding(NamedTuple):
    """A public ``facade:name`` whose defining module moved, and that module."""

    facade: str
    name: str
    module: str


# Every public binding whose definition moved when the host specs went into
# otto.host and the impairer contract into otto.link.params (spec 2026-10-11
# placement-and-phases §3.3, §4.1). The path a user imports stays; the object
# it serves is defined where the table says.
MOVED_BINDINGS = [
    MovedBinding("otto.host", "EmbeddedHostSpec", "otto.host.specs"),
    MovedBinding("otto.host", "HostSpec", "otto.host.specs"),
    MovedBinding("otto.host", "PowerControlSpec", "otto.host.specs"),
    MovedBinding("otto.host", "UnixHostSpec", "otto.host.specs"),
    MovedBinding("otto.models", "CredSpec", "otto.models.base"),
    MovedBinding("otto.models", "ToolchainSpec", "otto.host.specs"),
    MovedBinding("otto.models", "FtpOptionsSpec", "otto.host.option_specs"),
    MovedBinding("otto.models", "NcOptionsSpec", "otto.host.option_specs"),
    MovedBinding("otto.models", "ScpOptionsSpec", "otto.host.option_specs"),
    MovedBinding("otto.models", "SftpOptionsSpec", "otto.host.option_specs"),
    MovedBinding("otto.models", "SnmpOptionsSpec", "otto.host.option_specs"),
    MovedBinding("otto.models", "SshOptionsSpec", "otto.host.option_specs"),
    MovedBinding("otto.models", "TelnetOptionsSpec", "otto.host.option_specs"),
    MovedBinding("otto.models", "TftpOptionsSpec", "otto.host.option_specs"),
    MovedBinding("otto.link", "LinkImpairer", "otto.link.params"),
    MovedBinding("otto.link", "ScopedState", "otto.link.params"),
]


def test_every_declared_namespace_agrees_with_its_runtime_bindings():
    names = sorted(load_manifest(MANIFEST))
    assert agreement_failures(names, namespace_reports(names, CHECKOUT_ROOT)) == []


@pytest.mark.parametrize(
    "binding", MOVED_BINDINGS, ids=[f"{b.facade}:{b.name}" for b in MOVED_BINDINGS]
)
def test_a_moved_binding_keeps_its_public_path_and_names_its_new_home(binding):
    facade = importlib.import_module(binding.facade)
    obj = getattr(facade, binding.name)
    assert binding.name in facade.__all__
    assert obj.__module__ == binding.module
    assert getattr(importlib.import_module(binding.module), binding.name) is obj
