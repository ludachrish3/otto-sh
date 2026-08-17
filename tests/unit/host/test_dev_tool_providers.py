"""Dev-tool-provider registry + ingest application (code-customization model)."""

from types import SimpleNamespace

import pytest

from otto.host import dev_tool as dev_tool_mod
from otto.host.dev_tool import apply_dev_tool_providers, register_dev_tool_provider


@pytest.fixture(autouse=True)
def _isolate_provider_registry():
    saved = list(dev_tool_mod._DEV_TOOL_PROVIDERS)
    try:
        yield
    finally:
        dev_tool_mod._DEV_TOOL_PROVIDERS[:] = saved


@pytest.fixture
def _isolate_product_registry():
    """Sibling isolation for the tests that also touch the product registry."""
    from otto.host import product as product_mod

    saved = list(product_mod._PRODUCT_PROVIDERS)
    try:
        yield
    finally:
        product_mod._PRODUCT_PROVIDERS[:] = saved


def _tool(name):
    """Minimal dev-tool double — apply_dev_tool_providers reads ``.name``/``.owner``."""
    return SimpleNamespace(name=name, owner=None)


def _host(**attrs):
    attrs.setdefault("dev_tools", [])
    attrs.setdefault("products", [])
    attrs.setdefault("id", "h1")
    return SimpleNamespace(**attrs)


def test_registered_provider_attaches_dev_tools():
    register_dev_tool_provider(lambda host: [_tool("gdbserver")])
    host = _host()
    apply_dev_tool_providers(host)
    assert [t.name for t in host.dev_tools] == ["gdbserver"]


def test_provider_keys_on_host_attributes():
    register_dev_tool_provider(lambda host: [_tool("strace")] if host.os_type == "unix" else None)
    unix, embedded = _host(os_type="unix"), _host(os_type="embedded")
    apply_dev_tool_providers(unix)
    apply_dev_tool_providers(embedded)
    assert [t.name for t in unix.dev_tools] == ["strace"]
    assert embedded.dev_tools == []


def test_multiple_providers_aggregate_in_registration_order():
    register_dev_tool_provider(lambda host: [_tool("a")])
    register_dev_tool_provider(lambda host: [_tool("b")])
    host = _host()
    apply_dev_tool_providers(host)
    assert [t.name for t in host.dev_tools] == ["a", "b"]


def test_duplicate_name_from_two_providers_is_skipped():
    register_dev_tool_provider(lambda host: [_tool("dup")])
    register_dev_tool_provider(lambda host: [_tool("dup"), _tool("other")])
    host = _host()
    apply_dev_tool_providers(host)
    assert [t.name for t in host.dev_tools] == ["dup", "other"]


def test_duplicate_against_preexisting_dev_tool_is_skipped():
    register_dev_tool_provider(lambda host: [_tool("pre")])
    host = _host(dev_tools=[_tool("pre")])
    apply_dev_tool_providers(host)
    assert [t.name for t in host.dev_tools] == ["pre"]


def test_none_and_empty_returns_are_noops():
    register_dev_tool_provider(lambda host: None)
    register_dev_tool_provider(lambda host: [])
    host = _host()
    apply_dev_tool_providers(host)
    assert host.dev_tools == []


def test_provider_exception_propagates():
    def boom(host):
        raise RuntimeError("bad provider")

    register_dev_tool_provider(boom)
    with pytest.raises(RuntimeError, match="bad provider"):
        apply_dev_tool_providers(_host())


def test_no_providers_is_noop():
    host = _host()
    apply_dev_tool_providers(host)
    assert host.dev_tools == []


def test_public_reexports_available():
    import otto.host as host_pkg

    assert hasattr(host_pkg, "register_dev_tool_provider")
    assert hasattr(host_pkg, "DevToolProvider")
    assert hasattr(host_pkg, "DevTool")


def test_dev_tools_are_stamped_with_registering_repo():
    # Kills: capturing the marker at APPLY time instead of REGISTER time —
    # apply runs at lab ingest, long after init imports, when the marker is
    # None, so every dev tool would be unowned.
    from otto.registry import registering_repo

    with registering_repo("acme"):
        register_dev_tool_provider(lambda host: [_tool("gdbserver")])
    host = _host()
    apply_dev_tool_providers(host)
    assert host.dev_tools[0].owner == "acme"


def test_dev_tools_registered_outside_any_repo_stay_unowned():
    register_dev_tool_provider(lambda host: [_tool("gdbserver")])
    host = _host()
    apply_dev_tool_providers(host)
    assert host.dev_tools[0].owner is None


def test_explicit_owner_is_not_clobbered():
    # Kills: unconditional stamping, which would erase a provider that
    # deliberately hands one repo's dev tool to another's ownership.
    from otto.registry import registering_repo

    explicit = _tool("gdbserver")
    explicit.owner = "other"
    with registering_repo("acme"):
        register_dev_tool_provider(lambda host: [explicit])
    host = _host()
    apply_dev_tool_providers(host)
    assert host.dev_tools[0].owner == "other"


def test_dev_tools_and_products_are_separate_lists():
    # Kills: implementing DevTool attachment onto host.products — cleanup
    # would then uninstall dev tools as products and is_installed would
    # conflate the two lifecycles.
    register_dev_tool_provider(lambda host: [_tool("gdbserver")])
    host = _host()
    apply_dev_tool_providers(host)
    assert [t.name for t in host.dev_tools] == ["gdbserver"]
    assert host.products == []


@pytest.mark.usefixtures("_isolate_product_registry")
def test_registries_are_independent():
    # Kills: sharing one provider list between products and dev tools —
    # a dev-tool provider would then run at product apply and vice versa,
    # crossing each seam's attachments onto the other's list.
    from otto.host.product import apply_product_providers, register_product_provider

    register_dev_tool_provider(lambda host: [_tool("gdbserver")])
    register_product_provider(lambda host: [_tool("app")])
    host = _host()
    apply_dev_tool_providers(host)
    apply_product_providers(host)
    assert [t.name for t in host.dev_tools] == ["gdbserver"]
    assert [p.name for p in host.products] == ["app"]
