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


# ── per-host staging collisions (issue #368) ─────────────────────────────────


def _staging_host(**attrs):
    attrs.setdefault("default_dest_dir", Path("/srv/stage"))
    return SimpleNamespace(id="h1", products=[], dev_tools=[], source_lab="", **attrs)


def test_two_products_staging_one_basename_into_one_dir_are_refused():
    """The overwrite #368 reported: the second put lands on the first's file."""
    product_mod.register_product_provider(
        lambda host: [
            _P(artifact=Path("/builds/a/demo.ko"), name="alpha"),
            _P(artifact=Path("/builds/b/demo.ko"), name="beta"),
        ]
    )
    with pytest.raises(ValueError, match="overwrite") as excinfo:
        factory.apply_providers(_staging_host())
    msg = str(excinfo.value)
    assert "alpha" in msg  # both products, by name
    assert "beta" in msg
    assert "h1" in msg  # the host it happens on
    assert "/srv/stage" in msg  # the directory
    assert "demo.ko" in msg  # the basename


def test_two_products_falling_back_to_the_login_home_are_refused_at_lab_load():
    """Nothing is connected at ingest, so the home is NAMED rather than resolved."""
    from otto.host.product import LOGIN_HOME

    product_mod.register_product_provider(
        lambda host: [
            _P(artifact=Path("/builds/a/demo.ko"), name="alpha"),
            _P(artifact=Path("/builds/b/demo.ko"), name="beta"),
        ]
    )
    with pytest.raises(ValueError, match="overwrite") as excinfo:
        factory.apply_providers(_staging_host(default_dest_dir=Path()))
    assert LOGIN_HOME in str(excinfo.value)


def test_an_empty_stage_dir_collides_with_an_explicit_copy_of_the_host_default():
    # The key is the RESOLVED directory, not the declared spelling: an unset
    # stage_dir and a stage_dir written out as the host's own default are the
    # same directory, and the second put lands on the first's file.
    product_mod.register_product_provider(
        lambda host: [
            _P(artifact=Path("/builds/a/demo.ko"), name="alpha"),
            _P(artifact=Path("/builds/b/demo.ko"), name="beta", stage_dir=Path("/srv/stage")),
        ]
    )
    with pytest.raises(ValueError, match="overwrite") as excinfo:
        factory.apply_providers(_staging_host())
    assert "/srv/stage" in str(excinfo.value)


def test_an_empty_stage_dir_collides_with_an_explicit_copy_of_the_login_home():
    # Same, one rung further down the precedence chain: once the host has been
    # asked for its home, the key is that path, so writing it out explicitly
    # is recognised as the same directory.
    product_mod.register_product_provider(
        lambda host: [
            _P(artifact=Path("/builds/a/demo.ko"), name="alpha"),
            _P(artifact=Path("/builds/b/demo.ko"), name="beta", stage_dir=Path("/home/t")),
        ]
    )
    host = _staging_host(default_dest_dir=Path(), cached_login_home=Path("/home/t"))
    with pytest.raises(ValueError, match="overwrite") as excinfo:
        factory.apply_providers(host)
    assert "/home/t" in str(excinfo.value)


def test_the_same_basename_in_different_stage_dirs_is_accepted():
    product_mod.register_product_provider(
        lambda host: [
            _P(artifact=Path("/builds/a/demo.ko"), name="alpha", stage_dir=Path("/opt/a")),
            _P(artifact=Path("/builds/b/demo.ko"), name="beta", stage_dir=Path("/opt/b")),
        ]
    )
    host = _staging_host()
    factory.apply_providers(host)
    assert [p.name for p in host.products] == ["alpha", "beta"]


def test_the_same_basename_on_different_hosts_is_accepted():
    # The check is per host, deliberately: two boards each carrying their own
    # build of demo.ko is the normal shape, not a collision.
    product_mod.register_product_provider(
        lambda host: [_P(artifact=Path(f"/builds/{host.id}/demo.ko"), name="demo")]
    )
    for host_id in ("h1", "h2"):
        host = _staging_host()
        host.id = host_id
        factory.apply_providers(host)
        assert [p.name for p in host.products] == ["demo"]


def test_one_product_staging_one_basename_is_accepted():
    product_mod.register_product_provider(
        lambda host: [_P(artifact=Path("/builds/a/demo.ko"), name="alpha")]
    )
    host = _staging_host()
    factory.apply_providers(host)
    assert [p.name for p in host.products] == ["alpha"]


def test_products_that_stage_nothing_never_collide():
    # A bare Product places no file at <stage_dir>/<basename>, so two of them
    # are not an overwrite — the check must key on stages_artifact, not on
    # "has an artifact attribute".
    product_mod.register_product_provider(lambda host: [_BareProduct("a"), _BareProduct("b")])
    host = _staging_host()
    factory.apply_providers(host)
    assert [p.name for p in host.products] == ["a", "b"]


@pytest.mark.parametrize("tool_kind", ["kmod", "kgcov"])
def test_a_dev_tool_and_a_product_staging_one_basename_collide(tool_kind):
    """A REAL dev tool, so the production ``stages_artifact`` is what admits it.

    A hand-written double that set the flag itself would pass even with the
    flag missing from :class:`~otto.host.kmod_tool_kind.KmodTool` — and the
    check skips anything whose flag is falsy, so every real dev tool would
    have been silently exempt. Both concrete kinds are built here; ``KgcovTool``
    inherits the flag rather than declaring its own.
    """
    from otto.host.kmod_tool_kind import KgcovTool, KmodTool

    cls = KmodTool if tool_kind == "kmod" else KgcovTool
    tool = cls(name="tracer", artifact=Path("/builds/b/demo.ko"))
    assert tool.stages_artifact is True  # the production flag, not a test's

    product_mod.register_product_provider(
        lambda host: [_P(artifact=Path("/builds/a/demo.ko"), name="alpha")]
    )
    dev_tool_mod.register_dev_tool_provider(lambda host: [tool])
    with pytest.raises(ValueError, match="overwrite") as excinfo:
        factory.apply_providers(_staging_host())
    msg = str(excinfo.value)
    assert "product 'alpha'" in msg  # each side named WITH its seam
    assert "dev tool 'tracer'" in msg
    assert "demo.ko" in msg


def test_a_dev_tool_that_stages_nothing_is_not_a_collision():
    """The base default: a tool installed from a package feed places no file."""

    class _T(DevTool):
        def __init__(self, name):
            self.name, self.artifact, self.stage_dir = name, Path("/builds/demo.ko"), Path()

        def stage_key(self, host):
            return product_mod.stage_dir_key(self.stage_dir, host, who="dev tool")

        async def stage(self, host):
            return Result(Status.Success)

        async def install(self, host):
            return Result(Status.Success)

        async def uninstall(self, host):
            return Result(Status.Success)

        async def is_installed(self, host):
            return True

    assert _T("probe").stages_artifact is False
    product_mod.register_product_provider(
        lambda host: [_P(artifact=Path("/builds/a/demo.ko"), name="alpha")]
    )
    dev_tool_mod.register_dev_tool_provider(lambda host: [_T("probe")])
    host = _staging_host()
    factory.apply_providers(host)
    assert [p.name for p in host.products] == ["alpha"]
