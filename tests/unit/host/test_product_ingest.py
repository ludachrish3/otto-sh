"""The ingest chokepoint stamps cov_dir and validates product names (spec §4)."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from otto.errors import OttoError
from otto.host import dev_tool as dev_tool_mod
from otto.host import factory
from otto.host import product as product_mod
from otto.host.dev_tool import DevTool
from otto.host.errors import StageCollisionError
from otto.host.product import Product, ShellProduct
from otto.result import Result
from otto.utils import Status


class _P(ShellProduct):
    async def install(self, host):
        return Result(status=Status.Success)

    async def uninstall(self, host):
        return Result(status=Status.Success)

    async def is_installed(self, host):
        return True


class _BareProduct(Product):
    """A non-ShellProduct product — no ``__post_init__`` name auto-fill, so it
    can actually carry an empty ``name`` for the bad-name-rejection test."""

    def __init__(self, name: str) -> None:
        self.name = name

    async def stage(self, host):
        return Result(status=Status.Success)

    async def install(self, host):
        return Result(status=Status.Success)

    async def uninstall(self, host):
        return Result(status=Status.Success)

    async def is_installed(self, host):
        return True


class _DevTool(DevTool):
    def __init__(self, name: str) -> None:
        self.name = name

    async def stage(self, host):
        return Result(status=Status.Success)

    async def install(self, host):
        return Result(status=Status.Success)

    async def uninstall(self, host):
        return Result(status=Status.Success)

    async def is_installed(self, host):
        return True


def _host():
    return SimpleNamespace(id="h1", products=[], dev_tools=[], source_lab="")


def test_apply_providers_stamps_the_default_cov_dir():
    product_mod.register_product_provider(lambda host: [_P(artifact=Path("/a"), name="app")])
    host = _host()
    factory.apply_providers(host)
    assert [p.cov_dir for p in host.products] == ["/tmp/app"]


def test_ingest_runs_a_subscribed_provider_with_its_project():
    from otto.registry import registering_project

    with registering_project("acme"):
        product_mod.register_product_provider(lambda host: [_P(artifact=Path("/a"), name="app")])
    host = _host()
    factory.apply_providers(host)
    assert [(p.name, p.owner) for p in host.products] == [("app", "acme")]


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


def test_apply_providers_runs_the_kmodcov_binding_check(monkeypatch):
    from otto.host import kmod_tool_kind

    seen = []
    monkeypatch.setattr(kmod_tool_kind, "check_kmodcov_bindings", lambda host: seen.append(host.id))
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
    assert isinstance(excinfo.value, StageCollisionError)
    assert isinstance(excinfo.value, OttoError)
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


@pytest.mark.parametrize("tool_kind", ["kmod", "kmodcov"])
def test_a_dev_tool_and_a_product_staging_one_basename_collide(tool_kind):
    """A REAL dev tool, so the production ``stages_artifact`` is what admits it.

    A hand-written double that set the flag itself would pass even with the
    flag missing from :class:`~otto.host.kmod_tool_kind.KmodTool` — and the
    check skips anything whose flag is falsy, so every real dev tool would
    have been silently exempt. Both concrete kinds are built here; ``KmodcovTool``
    inherits the flag rather than declaring its own.
    """
    from otto.host.kmod_tool_kind import KmodcovTool, KmodTool

    cls = KmodTool if tool_kind == "kmod" else KmodcovTool
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
            return Result(status=Status.Success)

        async def install(self, host):
            return Result(status=Status.Success)

        async def uninstall(self, host):
            return Result(status=Status.Success)

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


# ── the cov_dir path rule and one owner per dir (#777 §2) ────────────────────


def _declared_shell(name: str, cov_dir: str | None):
    """A ``kind = "shell"`` products entry, ingested by ``apply_declared_products``."""
    from otto.declared import DeclaredEntry

    params: dict = {"artifact": f"build/{name}"}
    if cov_dir is not None:
        params["cov_dir"] = cov_dir
    return DeclaredEntry(
        name=name,
        kind="shell",
        seam="products",
        owner="declrepo",
        base_dir=Path("/project"),
        match={},
        params=params,
    )


def _ingest(monkeypatch, *, declared=(), provided=(), host_id="h1"):
    """Run the one ingest chokepoint over declared and provider (name, cov_dir) pairs."""
    from otto.host import shell_kind  # noqa: F401 — import registers the kind

    entries = [_declared_shell(n, d) for n, d in declared]
    monkeypatch.setattr(product_mod, "declared_for_host", lambda host, attr: list(entries))
    if provided:
        made = [_P(artifact=Path(f"/{n}"), name=n, cov_dir=d) for n, d in provided]
        product_mod.register_product_provider(lambda host: list(made))
    host = SimpleNamespace(id=host_id, products=[], dev_tools=[], source_lab="")
    factory.apply_providers(host)
    return host


@pytest.mark.parametrize("seam", ["declared", "provided"])
@pytest.mark.parametrize(
    "bad", ["cov", "/tmp/../etc/x", "/tmp/x/", "//tmp/cov", "/", "/tmp", "/tmp/./x"]
)
def test_a_cov_dir_that_is_not_absolute_normalized_and_two_deep_is_refused(monkeypatch, seam, bad):
    with pytest.raises(ValueError, match=r"host h1: product 'app'") as caught:
        _ingest(monkeypatch, **{seam: [("app", bad)]})
    assert repr(bad) in str(caught.value)
    assert "cov_dir" in str(caught.value)


def test_a_relative_cov_dir_shares_the_absolute_path_grammar(monkeypatch):
    with pytest.raises(ValueError, match=r"'cov_dir' must be an absolute path on the host"):
        _ingest(monkeypatch, declared=[("app", "cov")])


@pytest.mark.parametrize("bad", ["/tmp/../etc/x", "//tmp/cov", "/tmp"])
def test_a_refused_cov_dir_names_the_rule(monkeypatch, bad):
    with pytest.raises(ValueError, match=r"absolute, normalized, at least two components"):
        _ingest(monkeypatch, declared=[("app", bad)])


@pytest.mark.parametrize("seam", ["declared", "provided"])
@pytest.mark.parametrize("good", ["/tmp/x", "/opt/My App/cov", None])
def test_an_absolute_normalized_cov_dir_is_accepted(monkeypatch, seam, good):
    host = _ingest(monkeypatch, **{seam: [("app", good)]})
    assert [p.cov_dir for p in host.products] == [good or "/tmp/app"]


def _place(monkeypatch, triples):
    """Ingest (seam, name, cov_dir) triples, keeping their order within each seam."""
    return _ingest(
        monkeypatch,
        declared=[(n, d) for s, n, d in triples if s == "declared"],
        provided=[(n, d) for s, n, d in triples if s == "provided"],
    )


def test_two_products_with_one_cov_dir_are_refused_naming_both(monkeypatch):
    with pytest.raises(ValueError, match=r"host h1: products 'a' and 'b' share cov_dir"):
        _place(monkeypatch, [("declared", "a", "/var/cov/a"), ("declared", "b", "/var/cov/a")])


@pytest.mark.parametrize(
    ("parent_seam", "child_seam"),
    [
        ("declared", "declared"),
        ("provided", "provided"),
        ("provided", "declared"),
        ("declared", "provided"),
    ],
)
@pytest.mark.parametrize("parent_first", [True, False], ids=["parent-first", "child-first"])
def test_nested_cov_dirs_are_refused_in_either_order(
    monkeypatch, parent_seam, child_seam, parent_first
):
    parent = (parent_seam, "outer", "/var/cov")
    child = (child_seam, "inner", "/var/cov/a")
    triples = [parent, child] if parent_first else [child, parent]
    with pytest.raises(ValueError, match=r"share cov_dir") as caught:
        _place(monkeypatch, triples)
    assert "'outer'" in str(caught.value)
    assert "'inner'" in str(caught.value)


@pytest.mark.parametrize("ref_first", [False, True], ids=["container-first", "ref-first"])
def test_siblings_that_share_a_string_prefix_are_accepted(monkeypatch, ref_first):
    # Review Focus 1: nesting is a path-component test, never startswith
    # (tests/project5 declares exactly this pair).
    triples = [
        ("declared", "cov_container", "/var/cov/cov_container"),
        ("declared", "cov_container_ref", "/var/cov/cov_container_ref"),
    ]
    host = _place(monkeypatch, triples[::-1] if ref_first else triples)
    assert len(host.products) == 2


def test_one_cov_dir_on_two_hosts_is_accepted(monkeypatch):
    for host_id in ("h1", "h2"):
        host = _ingest(monkeypatch, declared=[("app", "/var/cov/app")], host_id=host_id)
        assert [p.cov_dir for p in host.products] == ["/var/cov/app"]


# ── a declared dir equal to the login home another entry falls back to (#1251) ─


def _discovering_host(home: str = "/home/u", **attrs):
    """A host whose login home is unknown until ``login_home()`` asks, as on a real one.

    Like ``UnixHost.login_home``, the answer is cached on ``cached_login_home``
    by the first ask, so what the lab-load key sees changes at that moment.
    """
    host = _staging_host(default_dest_dir=Path(), cached_login_home=None, **attrs)
    host.probes = 0

    async def _login_home():
        if host.cached_login_home is None:
            host.probes += 1
            host.cached_login_home = Path(home)
        return host.cached_login_home

    host.login_home = _login_home
    return host


def _entry(kind: str, name: str, stage_dir: Path):
    """A product or a kmod dev tool staging ``app.bin`` into *stage_dir* (empty: the fallback)."""
    from otto.host.kmod_tool_kind import KmodTool

    artifact = Path(f"/builds/{name}/app.bin")
    if kind == "tool":
        return KmodTool(name=name, artifact=artifact, stage_dir=stage_dir)
    return _P(artifact=artifact, name=name, stage_dir=stage_dir)


def _load(host, *entries):
    """Ingest *entries* on *host* the way lab load does: products and tools through providers."""
    product_mod.register_product_provider(lambda h: [e for e in entries if isinstance(e, _P)])
    dev_tool_mod.register_dev_tool_provider(lambda h: [e for e in entries if not isinstance(e, _P)])
    factory.apply_providers(host)
    return host


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fallback_kind", "declared_kind"),
    [("product", "product"), ("product", "tool"), ("tool", "product")],
)
async def test_a_declared_dir_equal_to_the_discovered_home_is_refused_once_it_is_known(
    fallback_kind, declared_kind
):
    """Accepted at lab load (the home is a placeholder there), refused when it becomes known."""
    host = _discovering_host()
    fallback = _entry(fallback_kind, "alpha", Path())
    declared = _entry(declared_kind, "beta", Path("/home/u"))
    _load(host, fallback, declared)  # no refusal: nothing is connected
    with pytest.raises(ValueError, match="overwrite") as excinfo:
        await fallback.resolved_stage_dir(host)
    msg = str(excinfo.value)
    assert "h1" in msg
    assert f"{fallback_kind} 'alpha'" in msg
    assert f"{declared_kind} 'beta'" in msg
    assert "/home/u" in msg
    assert "app.bin" in msg


@pytest.mark.asyncio
async def test_the_refusal_repeats_on_every_attempt_not_only_the_first():
    # The home is cached after the first ask, so a check that lived in the
    # discovery step would let the second attempt through silently.
    host = _discovering_host()
    fallback = _entry("product", "alpha", Path())
    _load(host, fallback, _entry("product", "beta", Path("/home/u")))
    for _ in range(3):
        with pytest.raises(ValueError, match="overwrite"):
            await fallback.resolved_stage_dir(host)
    assert host.cached_login_home == Path("/home/u")


@pytest.mark.asyncio
@pytest.mark.parametrize("spelling", ["/home/u/", "/home/u//", "/home/u/."])
async def test_a_trailing_slash_spelling_of_the_home_is_still_the_home(spelling):
    host = _discovering_host()
    fallback = _entry("product", "alpha", Path())
    _load(host, fallback, _entry("product", "beta", Path(spelling)))
    with pytest.raises(ValueError, match="overwrite"):
        await fallback.resolved_stage_dir(host)


@pytest.mark.asyncio
async def test_a_declared_dir_that_differs_from_the_home_is_fine():
    host = _discovering_host()
    fallback = _entry("product", "alpha", Path())
    _load(host, fallback, _entry("product", "beta", Path("/home/other")))
    assert await fallback.resolved_stage_dir(host) == Path("/home/u")


@pytest.mark.asyncio
async def test_a_different_basename_in_the_home_is_fine():
    host = _discovering_host()
    fallback = _entry("product", "alpha", Path())
    other = _P(artifact=Path("/builds/beta/other.bin"), name="beta", stage_dir=Path("/home/u"))
    _load(host, fallback, other)
    assert await fallback.resolved_stage_dir(host) == Path("/home/u")


@pytest.mark.asyncio
async def test_two_entries_falling_back_to_the_home_are_still_refused_the_same_way_at_load():
    host = _discovering_host()
    with pytest.raises(ValueError, match="overwrite") as excinfo:
        _load(host, _entry("product", "alpha", Path()), _entry("product", "beta", Path()))
    assert product_mod.LOGIN_HOME in str(excinfo.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("declared_kind", ["product", "tool"])
async def test_the_declared_entry_resolved_first_is_refused_with_one_home_probe(declared_kind):
    """The declared side stages first (another seam, another run, another owner)."""
    host = _discovering_host()
    declared = _entry(declared_kind, "beta", Path("/home/u"))
    _load(host, _entry("product", "alpha", Path()), declared)
    for _ in range(2):
        with pytest.raises(ValueError, match=r"(?s)'alpha'.*'beta'.*overwrite") as excinfo:
            await declared.resolved_stage_dir(host)
        assert isinstance(excinfo.value, StageCollisionError)
        assert isinstance(excinfo.value, OttoError)
    assert host.probes == 1  # the home is cached; the check repeats without a second probe


@pytest.mark.asyncio
async def test_the_declared_entry_is_refused_though_the_fallback_entry_is_never_resolved():
    host = _discovering_host()
    declared = _entry("product", "beta", Path("/home/u"))
    fallback = _entry("tool", "alpha", Path())
    _load(host, fallback, declared)
    with pytest.raises(ValueError, match="overwrite"):
        await declared.resolved_stage_dir(host)  # `fallback` was skipped as already installed


@pytest.mark.asyncio
async def test_a_declared_entry_with_no_same_basename_fallback_entry_probes_nothing():
    """The ordinary path gains no host command: the question is answered in memory."""
    host = _discovering_host()
    declared = _entry("product", "beta", Path("/home/u"))
    fallback_other_name = _P(artifact=Path("/builds/x/other.bin"), name="x", stage_dir=Path())
    _load(host, declared, fallback_other_name)
    assert await declared.resolved_stage_dir(host) == Path("/home/u")
    assert host.probes == 0


@pytest.mark.asyncio
async def test_a_host_default_dir_with_the_same_declarations_probes_nothing_and_is_not_refused():
    host = _discovering_host(home="/home/u")
    host.default_dest_dir = Path("/srv/stage")
    first = _entry("product", "alpha", Path())  # falls to /srv/stage, not the home
    second = _entry("product", "beta", Path("/home/u"))
    _load(host, first, second)
    assert await second.resolved_stage_dir(host) == Path("/home/u")
    assert await first.resolved_stage_dir(host) == Path("/srv/stage")
    assert host.probes == 0


@pytest.mark.asyncio
async def test_an_ad_hoc_load_on_a_host_with_a_colliding_pair_is_not_refused_by_this_check():
    """``UnixHost.load`` with no ``dest_dir`` belongs to no entry: it resolves the host alone."""
    host = _discovering_host()
    _load(host, _entry("product", "alpha", Path()), _entry("product", "beta", Path("/home/u")))
    assert await product_mod.resolve_stage_dir(Path(), host, who="host h1") == Path("/home/u")


@pytest.mark.asyncio
@pytest.mark.parametrize("stage_dir", [Path(), Path("/srv/x")], ids=["host-default", "declared"])
async def test_a_stand_in_host_with_no_entries_of_its_own_still_resolves(stage_dir):
    """A product author's own test hands ``resolved_stage_dir`` a minimal stand-in host.

    Such a host has no ``products`` or ``dev_tools``: there is nothing to
    collide with, so the directory resolves as it did before the collision
    check existed (the getting-started example's test does exactly this).
    """

    class StandIn:
        id = "h1"
        os_type = "unix"
        default_dest_dir = "/opt/agent"
        cached_login_home = "/root"

    entry = _P(artifact=Path("/builds/a/app.bin"), name="a", stage_dir=stage_dir)
    expected = Path("/opt/agent") if stage_dir == Path() else stage_dir
    assert await entry.resolved_stage_dir(StandIn()) == expected
