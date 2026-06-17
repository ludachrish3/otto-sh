"""The public registry accessor backing JSON Schema export."""

import otto.host.os_profile as op
from otto.host.os_profile import registered_host_specs
from otto.models import EmbeddedHostSpec, UnixHostSpec


def test_returns_builtin_name_to_spec_mapping():
    specs = registered_host_specs()
    assert specs['unix'] is UnixHostSpec
    assert specs['embedded'] is EmbeddedHostSpec
    assert specs['zephyr'] is EmbeddedHostSpec  # zephyr shares the embedded spec


def test_returns_a_copy_not_the_live_registry():
    specs = registered_host_specs()
    specs['bogus'] = UnixHostSpec  # mutate the returned dict
    assert 'bogus' not in registered_host_specs()  # registry unaffected


def test_builtins_only_excludes_custom_registrations(monkeypatch):
    # A custom-registered type shows up by default but is filtered out by
    # builtins_only (which restricts to the in-tree unix/embedded/zephyr).
    monkeypatch.setitem(op._HOST_SPECS, 'acme', UnixHostSpec)
    assert 'acme' in registered_host_specs()
    assert 'acme' not in registered_host_specs(builtins_only=True)
    assert set(registered_host_specs(builtins_only=True)) == {'unix', 'embedded', 'zephyr'}
