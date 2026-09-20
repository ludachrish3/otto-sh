"""The ingest chokepoint stamps cov_dir and validates product names (spec §4)."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from otto.host import dev_tool as dev_tool_mod
from otto.host import factory
from otto.host import product as product_mod
from otto.host.dev_tool import DevTool
from otto.host.product import Product, ShellProduct
from otto.result import Result
from otto.utils import Status


class _P(ShellProduct):
    async def install(self, host):
        return Result(Status.Success)

    async def uninstall(self, host):
        return Result(Status.Success)

    async def is_installed(self, host):
        return True


class _BareProduct(Product):
    """A non-ShellProduct product — no ``__post_init__`` name auto-fill, so it
    can actually carry an empty ``name`` for the bad-name-rejection test."""

    def __init__(self, name: str) -> None:
        self.name = name

    async def stage(self, host):
        return Result(Status.Success)

    async def install(self, host):
        return Result(Status.Success)

    async def uninstall(self, host):
        return Result(Status.Success)

    async def is_installed(self, host):
        return True


class _DevTool(DevTool):
    def __init__(self, name: str) -> None:
        self.name = name

    async def stage(self, host):
        return Result(Status.Success)

    async def install(self, host):
        return Result(Status.Success)

    async def uninstall(self, host):
        return Result(Status.Success)

    async def is_installed(self, host):
        return True


@pytest.fixture(autouse=True)
def _isolate_providers():
    saved_products = list(product_mod._PRODUCT_PROVIDERS)
    saved_dev_tools = list(dev_tool_mod._DEV_TOOL_PROVIDERS)
    try:
        yield
    finally:
        product_mod._PRODUCT_PROVIDERS[:] = saved_products
        dev_tool_mod._DEV_TOOL_PROVIDERS[:] = saved_dev_tools


def _host():
    return SimpleNamespace(id="h1", products=[], dev_tools=[], source_lab="")


def test_apply_providers_stamps_the_default_cov_dir():
    product_mod.register_product_provider(lambda host: [_P(artifact=Path("/a"), name="app")])
    host = _host()
    factory.apply_providers(host)
    assert [p.cov_dir for p in host.products] == ["/tmp/app"]


def test_apply_providers_keeps_an_explicit_cov_dir():
    product_mod.register_product_provider(
        lambda host: [_P(artifact=Path("/a"), name="app", cov_dir="/var/cov/app")]
    )
    host = _host()
    factory.apply_providers(host)
    assert host.products[0].cov_dir == "/var/cov/app"


@pytest.mark.parametrize("bad", ["debug", "a/b", ""])
def test_apply_providers_rejects_a_bad_product_name_naming_the_host(bad):
    # A bare Product, not a ShellProduct: ShellProduct.__post_init__ auto-fills
    # an empty name from the artifact's basename, which would silently turn
    # the "" case into a valid name and never reach validate_product_name.
    product_mod.register_product_provider(lambda host: [_BareProduct(bad)])
    with pytest.raises(ValueError, match=r"host h1.*product name"):
        factory.apply_providers(_host())


def test_apply_providers_does_not_stamp_cov_dir_on_dev_tools():
    # Only host.products is finished by the chokepoint's per-product loop;
    # a dev tool has no cov_dir attribute at all — it is a DevTool, not a
    # Product — so it must come back untouched.
    dev_tool_mod.register_dev_tool_provider(lambda host: [_DevTool("probe")])
    host = _host()
    factory.apply_providers(host)
    assert [t.name for t in host.dev_tools] == ["probe"]
    assert not hasattr(host.dev_tools[0], "cov_dir")


def test_apply_providers_runs_all_four_seams_in_order(monkeypatch):
    order = []
    monkeypatch.setattr(factory, "apply_declared_products", lambda h: order.append("dp"))
    monkeypatch.setattr(factory, "apply_product_providers", lambda h: order.append("pp"))
    monkeypatch.setattr(factory, "apply_declared_dev_tools", lambda h: order.append("dt"))
    monkeypatch.setattr(factory, "apply_dev_tool_providers", lambda h: order.append("tp"))
    factory.apply_providers(_host())
    assert order == ["dp", "pp", "dt", "tp"]


def test_apply_providers_runs_the_kgcov_binding_check(monkeypatch):
    from otto.host import kmod_tool_kind

    seen = []
    monkeypatch.setattr(kmod_tool_kind, "check_kgcov_bindings", lambda host: seen.append(host.id))
    host = SimpleNamespace(id="h1", products=[], dev_tools=[], source_lab="", inventory_ref=None)
    factory.apply_providers(host)
    assert seen == ["h1"]
