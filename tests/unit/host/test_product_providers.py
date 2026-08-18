"""Product-provider registry + ingest application (code-customization model)."""

from types import SimpleNamespace

import pytest

from otto.host import product as product_mod
from otto.host.product import apply_product_providers, register_product_provider


@pytest.fixture(autouse=True)
def _isolate_provider_registry():
    saved = list(product_mod._PRODUCT_PROVIDERS)
    try:
        yield
    finally:
        product_mod._PRODUCT_PROVIDERS[:] = saved


def _prod(name):
    """Minimal product double — apply_product_providers reads ``.name``/``.owner``."""
    return SimpleNamespace(name=name, owner=None)


def _host(**attrs):
    attrs.setdefault("products", [])
    attrs.setdefault("id", "h1")
    return SimpleNamespace(**attrs)


def test_registered_provider_attaches_products():
    register_product_provider(lambda host: [_prod("app")])
    host = _host()
    apply_product_providers(host)
    assert [p.name for p in host.products] == ["app"]


def test_provider_keys_on_host_attributes():
    register_product_provider(lambda host: [_prod("linux-app")] if host.os_type == "unix" else None)
    unix, embedded = _host(os_type="unix"), _host(os_type="embedded")
    apply_product_providers(unix)
    apply_product_providers(embedded)
    assert [p.name for p in unix.products] == ["linux-app"]
    assert embedded.products == []


def test_multiple_providers_aggregate_in_registration_order():
    register_product_provider(lambda host: [_prod("a")])
    register_product_provider(lambda host: [_prod("b")])
    host = _host()
    apply_product_providers(host)
    assert [p.name for p in host.products] == ["a", "b"]


def test_duplicate_name_from_two_providers_is_skipped():
    register_product_provider(lambda host: [_prod("dup")])
    register_product_provider(lambda host: [_prod("dup"), _prod("other")])
    host = _host()
    apply_product_providers(host)
    assert [p.name for p in host.products] == ["dup", "other"]


def test_duplicate_against_preexisting_product_is_skipped():
    register_product_provider(lambda host: [_prod("pre")])
    host = _host(products=[_prod("pre")])
    apply_product_providers(host)
    assert [p.name for p in host.products] == ["pre"]


def test_a_preexisting_product_is_never_stamped_by_a_provider_run():
    # Kills: stamping the host's WHOLE product list rather than the instances
    # this run attached — a sweep like `for p in host.products: p.owner = ...`
    # after the loop. That hands a product the host already carried (lab data,
    # a host class's own construction, an earlier repo's ingest) to whichever
    # repo happened to register a provider, and every owner-scoped verb then
    # acts on it. The dedup test above cannot see this: the NAMES are identical
    # either way, so only the owner stamp distinguishes them.
    #
    # NOT killed, and not a hole: a `seen`-based skip that stamped before it
    # `continue`d would stamp the PROVIDER'S instance — the one the skip
    # discards — so `preexisting.owner` stays None and nothing here could
    # observe the difference.
    from otto.registry import registering_repo

    preexisting = _prod("pre")
    with registering_repo("acme"):
        register_product_provider(lambda host: [_prod("pre"), _prod("fresh")])
    host = _host(products=[preexisting])
    apply_product_providers(host)
    assert preexisting.owner is None, "a product otto did not attach here is not otto's to own"
    assert [(p.name, p.owner) for p in host.products] == [("pre", None), ("fresh", "acme")]


def test_none_and_empty_returns_are_noops():
    register_product_provider(lambda host: None)
    register_product_provider(lambda host: [])
    host = _host()
    apply_product_providers(host)
    assert host.products == []


def test_provider_exception_propagates():
    def boom(host):
        raise RuntimeError("bad provider")

    register_product_provider(boom)
    with pytest.raises(RuntimeError, match="bad provider"):
        apply_product_providers(_host())


def test_no_providers_is_noop():
    host = _host()
    apply_product_providers(host)
    assert host.products == []


def test_public_reexports_available():
    import otto.host as host_pkg

    assert hasattr(host_pkg, "register_product_provider")
    assert hasattr(host_pkg, "ProductProvider")


def test_products_are_stamped_with_registering_repo():
    # Kills: capturing the marker at APPLY time instead of REGISTER time —
    # apply runs at lab ingest, long after init imports, when the marker is
    # None, so every product would be unowned.
    from otto.registry import registering_repo

    with registering_repo("acme"):
        register_product_provider(lambda host: [_prod("app")])
    host = _host()
    apply_product_providers(host)
    assert host.products[0].owner == "acme"


def test_products_registered_outside_any_repo_stay_unowned():
    register_product_provider(lambda host: [_prod("app")])
    host = _host()
    apply_product_providers(host)
    assert host.products[0].owner is None


def test_explicit_owner_is_not_clobbered():
    # Kills: unconditional stamping, which would erase a provider that
    # deliberately hands one repo's product to another's ownership.
    from otto.registry import registering_repo

    explicit = _prod("app")
    explicit.owner = "other"
    with registering_repo("acme"):
        register_product_provider(lambda host: [explicit])
    host = _host()
    apply_product_providers(host)
    assert host.products[0].owner == "other"
