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
    # The ingest gate reads `source_lab`, so the double must carry it or it
    # would pin a shape no real host has. Empty is BaseHost's own default:
    # a host built outside the loader is unattributed.
    attrs.setdefault("source_lab", "")
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


# --------------------------------------------------------------------------
# Ingest gating (spec §5): a repo's dev tools stop at its declared universe.
#
# The same admission rule the product seam carries, pinned separately because
# the two registries are deliberately separate: a gate added to one loop and
# forgotten in the other leaves a repo free to hang debug tooling on every host
# of every lab, and no product test would notice.
# --------------------------------------------------------------------------


def _scope(labs, hosts=(".*",)):
    """A compiled ``[project]`` declaration, without going through settings parse."""
    import re

    from otto.config.scope import ProjectScopeConfig

    return ProjectScopeConfig(
        lab_patterns=[re.compile(p) for p in labs],
        host_patterns=[re.compile(p) for p in hosts],
    )


@pytest.fixture
def scope_table(monkeypatch):
    """An owner -> declaration table the gate reads instead of bootstrap's repos.

    Patched on ``otto.config.scope`` because that is the module the gate looks
    the name up through. An owner absent from the table resolves to ``None``,
    which is exactly the real lookup's "no such repo / no ``[project]``" answer.
    """
    from otto.config import scope as scope_mod

    table = {}
    monkeypatch.setattr(scope_mod, "scope_for_repo", table.get)
    return table


def test_provider_not_invoked_outside_its_repos_universe(scope_table):
    # Kills: gating AFTER the call — filtering the returned tools instead of
    # skipping the provider. A provider that ran has already been handed a host
    # its repo never declared: providers probe host attributes and some keep
    # their own per-host registries, so "called and discarded" still leaks the
    # fleet. `host.dev_tools` alone cannot tell the two apart, which is why the
    # invocation list is asserted as well.
    from otto.registry import registering_repo

    scope_table["r1"] = _scope(["a"])
    invocations = []

    def provider(host):
        invocations.append(host.id)
        return [_tool("gdbserver")]

    with registering_repo("r1"):
        register_dev_tool_provider(provider)
    host = _host(source_lab="b")

    apply_dev_tool_providers(host)

    assert invocations == []
    assert host.dev_tools == []


def test_provider_invoked_inside_its_repos_universe(scope_table):
    # The other half: the gate must not be a blanket refusal. Same declaration,
    # same provider — only the host's lab differs.
    from otto.registry import registering_repo

    scope_table["r1"] = _scope(["a"])
    invocations = []

    def provider(host):
        invocations.append(host.id)
        return [_tool("gdbserver")]

    with registering_repo("r1"):
        register_dev_tool_provider(provider)
    host = _host(source_lab="a")

    apply_dev_tool_providers(host)

    assert invocations == ["h1"]
    assert [(t.name, t.owner) for t in host.dev_tools] == [("gdbserver", "r1")]


def test_the_host_axis_gates_as_well_as_the_lab(scope_table):
    # Kills: gating on the lab alone. A repo declared for lab `a` shares that
    # lab with every other repo's hosts, so the host axis is what keeps its
    # tooling off the gateways sitting next to its sensors.
    from otto.registry import registering_repo

    scope_table["r1"] = _scope(["a"], hosts=["sensor-.*"])
    invocations = []

    def provider(host):
        invocations.append(host.id)
        return [_tool("gdbserver")]

    with registering_repo("r1"):
        register_dev_tool_provider(provider)
    host = _host(source_lab="a", id="gw-1")

    apply_dev_tool_providers(host)

    assert invocations == []
    assert host.dev_tools == []


def test_one_repos_exclusion_does_not_gate_another(scope_table):
    # Kills: gating on ANY registered declaration rather than the registering
    # one — a single per-apply verdict, or reading the wrong element of the
    # (provider, owner) pair. Both providers run on the same host in the same
    # apply, and exactly one of them belongs there.
    from otto.registry import registering_repo

    scope_table["here"] = _scope(["a"])
    scope_table["elsewhere"] = _scope(["b"])
    with registering_repo("here"):
        register_dev_tool_provider(lambda host: [_tool("mine")])
    with registering_repo("elsewhere"):
        register_dev_tool_provider(lambda host: [_tool("theirs")])
    host = _host(source_lab="a")

    apply_dev_tool_providers(host)

    assert [t.name for t in host.dev_tools] == ["mine"]


def test_an_unstamped_host_admits_without_consulting_any_declaration(monkeypatch):
    # The carve-out, with the hostile condition injected rather than inherited:
    # the declaration on file EXCLUDES this host on both axes, and the host is
    # admitted anyway because it carries no lab attribution. Direct
    # `create_host_from_dict` use, container hosts and the built-in `local`
    # arrive this way, and they predate scoping.
    #
    # The lookup list is the second leg: an unattributed host must not even
    # reach the lookup, which bootstraps lazily — asking would drag a full
    # composition root into bare library use.
    from otto.config import scope as scope_mod
    from otto.registry import registering_repo

    lookups = []

    def _excluding(owner):
        lookups.append(owner)
        return _scope(["nowhere"], hosts=["nothing"])

    monkeypatch.setattr(scope_mod, "scope_for_repo", _excluding)
    with registering_repo("r1"):
        register_dev_tool_provider(lambda host: [_tool("gdbserver")])
    host = _host()  # no source_lab: BaseHost's unattributed default

    apply_dev_tool_providers(host)

    assert [t.name for t in host.dev_tools] == ["gdbserver"]
    assert lookups == [], "an unattributed host has no lab axis to be judged on"


def test_a_registration_made_outside_any_repo_admits(monkeypatch):
    # Owner None, through the REAL lookup: a provider registered by library
    # code (or by a test) has no declaration to be bounded by, and refusing it
    # would break every such use. `get_repos` is made hostile so a lookup that
    # consulted config for a None owner cannot pass this quietly.
    from otto import config as config_mod

    def _unavailable():
        raise RuntimeError("no bootstrap in this process")

    monkeypatch.setattr(config_mod, "get_repos", _unavailable)
    register_dev_tool_provider(lambda host: [_tool("gdbserver")])
    host = _host(source_lab="b")

    apply_dev_tool_providers(host)

    assert [t.name for t in host.dev_tools] == ["gdbserver"]


def test_an_owner_config_cannot_resolve_admits(monkeypatch):
    # REAL `scope_for_repo` here, with `get_repos` injected to raise: an ingest
    # that ran where no bootstrap is reachable must attach exactly what it
    # attached before this gate existed. A gate that let the lookup's failure
    # propagate would turn an unbootstrapped process into an ingest crash.
    from otto import config as config_mod
    from otto.registry import registering_repo

    def _unavailable():
        raise RuntimeError("no bootstrap in this process")

    monkeypatch.setattr(config_mod, "get_repos", _unavailable)
    with registering_repo("ghost"):
        register_dev_tool_provider(lambda host: [_tool("gdbserver")])
    host = _host(source_lab="b")

    apply_dev_tool_providers(host)

    assert [t.name for t in host.dev_tools] == ["gdbserver"]


def test_source_lab_is_stamped_before_the_providers_run():
    """The stamp must land before the apply, because the gate reads it.

    Kills: moving ``apply_dev_tool_providers`` above the ``source_lab``
    assignment in ``create_host_from_dict``. Every provider would then see
    ``""`` — the unattributed carve-out — and attach to every host of every
    lab, while every test of the gate above stayed green, because they call
    ``apply`` directly on an already-stamped double. The regression is silent
    admission, which is the failure this whole seam exists to prevent.

    Pinned in BOTH provider suites: the two applies are separate statements in
    the factory, so a reorder can strand one of them alone.
    """
    from otto.host.factory import create_host_from_dict

    seen = []

    def probe(host):
        # Records only; the implicit None return is the provider contract's
        # "no dev tools", so the pin cannot pass through the attach path.
        seen.append(host.source_lab)

    register_dev_tool_provider(probe)
    create_host_from_dict(
        {
            "element": "probe-box",
            "os_type": "unix",
            "ip": "10.0.0.9",
            "creds": [{"login": "admin", "password": "admin"}],
        },
        lab_name="somelab",
    )

    assert seen == ["somelab"], "the provider ran before the lab stamp landed"
