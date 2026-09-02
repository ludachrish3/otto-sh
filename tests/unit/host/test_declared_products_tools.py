"""Declared products/dev tools: seam adapters, the file kind, factory wiring.

Parametrized over BOTH seams wherever the behavior must be identical — the
spec's "products and tools are handled the same way" enforced structurally.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from otto.declared import DeclaredEntry
from otto.host import dev_tool as dev_tool_mod
from otto.host import product as product_mod


@pytest.fixture(autouse=True)
def _isolate_provider_registries():
    saved_p = list(product_mod._PRODUCT_PROVIDERS)
    saved_t = list(dev_tool_mod._DEV_TOOL_PROVIDERS)
    try:
        yield
    finally:
        product_mod._PRODUCT_PROVIDERS[:] = saved_p
        dev_tool_mod._DEV_TOOL_PROVIDERS[:] = saved_t


SEAMS = [
    pytest.param(
        SimpleNamespace(
            mod=product_mod,
            kinds=product_mod.PRODUCT_KINDS,
            register_kind=product_mod.register_product_kind,
            apply_declared=product_mod.apply_declared_products,
            apply_providers=product_mod.apply_product_providers,
            register_provider=product_mod.register_product_provider,
            attr="products",
            seam="products",
            entries_attr="declared_products",
        ),
        id="products",
    ),
    pytest.param(
        SimpleNamespace(
            mod=dev_tool_mod,
            kinds=dev_tool_mod.DEV_TOOL_KINDS,
            register_kind=dev_tool_mod.register_dev_tool_kind,
            apply_declared=dev_tool_mod.apply_declared_dev_tools,
            apply_providers=dev_tool_mod.apply_dev_tool_providers,
            register_provider=dev_tool_mod.register_dev_tool_provider,
            attr="dev_tools",
            seam="dev_tools",
            entries_attr="declared_dev_tools",
        ),
        id="dev_tools",
    ),
]


def _host(**attrs):
    attrs.setdefault("products", [])
    attrs.setdefault("dev_tools", [])
    attrs.setdefault("id", "h1")
    attrs.setdefault("source_lab", "")
    return SimpleNamespace(**attrs)


def _entry(name, seam, kind="toy", match=None, **params):
    return DeclaredEntry(
        name=name,
        kind=kind,
        seam=seam,
        owner="declrepo",
        base_dir=Path("/repo"),
        match=match or {},
        params=params,
    )


def _toy(entry, host):
    return SimpleNamespace(name=entry.name, owner=None, params=entry.params)


@pytest.fixture(params=SEAMS)
def seam(request, monkeypatch):
    s = request.param
    # Through the public wrapper, not s.kinds.register(...) directly — a
    # wrapper that registered into the WRONG seam's registry must be caught
    # here. Dropped by _isolate_provider_registries regardless (KindRegistry
    # isolation is a suite-wide autouse fixture elsewhere).
    s.register_kind("toy", _toy)
    s.declared = []
    s.seam_attr_calls = []

    def _stub(host, seam_attr):
        s.seam_attr_calls.append(seam_attr)
        return list(s.declared)

    # apply_declared_* reads declared_for_host through its own module binding.
    monkeypatch.setattr(s.mod, "declared_for_host", _stub)
    return s


def test_declared_entry_attaches_with_owner(seam):
    seam.declared.append(_entry("fw", seam.seam))
    host = _host()
    seam.apply_declared(host)
    attached = getattr(host, seam.attr)
    assert [(a.name, a.owner) for a in attached] == [("fw", "declrepo")]
    # Pins the seam_attr literal each apply loop passes to declared_for_host —
    # swapping "declared_products"/"declared_dev_tools" between the two loops
    # must fail here (the stub ignores the value, so nothing else catches it).
    assert seam.seam_attr_calls == [seam.entries_attr]


def test_declared_never_touches_the_other_seams_list(seam):
    seam.declared.append(_entry("fw", seam.seam))
    host = _host()
    seam.apply_declared(host)
    other = "dev_tools" if seam.attr == "products" else "products"
    assert getattr(host, other) == []


def test_preexisting_name_blocks_a_declared_entry(seam):
    pre = SimpleNamespace(name="fw", owner=None)
    seam.declared.append(_entry("fw", seam.seam))
    host = _host(**{seam.attr: [pre]})
    seam.apply_declared(host)
    attached = getattr(host, seam.attr)
    assert attached == [pre]
    assert pre.owner is None


def test_declared_beats_a_same_name_provider_in_chokepoint_order(seam):
    # The fallback contract: declared applies FIRST at the chokepoint, so the
    # provider loop's own seen-dedup skips its same-name instance.
    from otto.registry import registering_repo

    seam.declared.append(_entry("fw", seam.seam))
    with registering_repo("coderepo"):
        seam.register_provider(
            lambda host: [
                SimpleNamespace(name="fw", owner=None),
                SimpleNamespace(name="extra", owner=None),
            ]
        )
    host = _host()
    seam.apply_declared(host)
    seam.apply_providers(host)
    attached = getattr(host, seam.attr)
    assert [(a.name, a.owner) for a in attached] == [("fw", "declrepo"), ("extra", "coderepo")]


def test_two_declared_entries_same_name_first_in_order_wins(seam):
    # Cross-repo ordering is load-bearing: declared_for_host concatenates
    # repos in a fixed order, and KindRegistry.build's first-match-wins must
    # respect that order, not e.g. registration/kind order or last-wins.
    first = _entry("fw", seam.seam)
    second = DeclaredEntry(
        name="fw",
        kind="toy",
        seam=seam.seam,
        owner="otherrepo",
        base_dir=Path("/other"),
        match={},
        params={},
    )
    seam.declared.append(first)
    seam.declared.append(second)
    host = _host()
    seam.apply_declared(host)
    attached = getattr(host, seam.attr)
    assert [(a.name, a.owner) for a in attached] == [("fw", "declrepo")]


# ── the built-in "file" kind ─────────────────────────────────────────────────

from otto.host import file_kind  # noqa: F401 — import registers the kind
from otto.result import Result
from otto.utils import Status


def _file_entry(seam="products", **params):
    params.setdefault("artifact", "build/fw.bin")
    return _entry("fw", seam, kind="file", **params)


class _RunHost(SimpleNamespace):
    """Host double recording run()/put() calls; run returns a canned Result."""

    def __init__(self, *, run_status=Status.Success, **attrs):
        super().__init__(**attrs)
        self.run_calls: list = []
        self.put_calls: list = []
        self._run_status = run_status

    async def run(self, cmds, **kwargs):
        self.run_calls.append(cmds)
        return Result(self._run_status)

    async def put(self, src_files, dest_dir, mode=None, user=None):
        self.put_calls.append((src_files, dest_dir))
        return Result(Status.Success)


@pytest.mark.parametrize("registry", [product_mod.PRODUCT_KINDS, dev_tool_mod.DEV_TOOL_KINDS])
def test_file_kind_is_registered_in_both_seams(registry):
    assert "file" in registry


def test_file_kind_anchors_the_artifact_and_names_the_entry():
    built = product_mod.PRODUCT_KINDS.get("file")(_file_entry(), _host())
    assert built.artifact == Path("/repo/build/fw.bin")  # base_dir-anchored
    assert built.name == "fw"
    assert built.dest_dir == Path()


def test_file_kind_absolute_artifact_passes_through():
    built = product_mod.PRODUCT_KINDS.get("file")(
        _file_entry(artifact="/abs/fw.bin", dest_dir="/opt/fw"), _host()
    )
    assert built.artifact == Path("/abs/fw.bin")
    assert built.dest_dir == Path("/opt/fw")


@pytest.mark.parametrize(
    ("params", "fragment"),
    [
        ({}, "artifact"),  # required param missing
        ({"artifact": "a", "artefact": "b"}, "artefact"),  # typo'd param named loudly
        ({"artifact": 7}, "artifact"),  # wrong type
    ],
)
def test_file_kind_rejects_bad_params_naming_entry_and_seam(params, fragment):
    entry = _entry("fw", "products", kind="file", **params)
    with pytest.raises(ValueError, match=rf"(?s)\[\[products\]\].*'fw'.*{fragment}"):
        product_mod.PRODUCT_KINDS.get("file")(entry, _host())


@pytest.mark.asyncio
async def test_file_kind_stage_puts_the_artifact():
    built = product_mod.PRODUCT_KINDS.get("file")(_file_entry(dest_dir="/opt"), _host())
    host = _RunHost()
    result = await built.stage(host)
    assert result.status is Status.Success
    assert host.put_calls == [(Path("/repo/build/fw.bin"), Path("/opt"))]


@pytest.mark.asyncio
async def test_file_kind_command_defaults_are_honest():
    built = product_mod.PRODUCT_KINDS.get("file")(_file_entry(), _host())
    host = _RunHost()
    assert (await built.install(host)).status is Status.Success  # no-op success
    assert (await built.uninstall(host)).status is Status.Success  # no-op success
    assert await built.is_installed(host) is False  # no check => assume absent
    assert host.run_calls == []


@pytest.mark.asyncio
async def test_file_kind_commands_run_on_the_host():
    built = product_mod.PRODUCT_KINDS.get("file")(
        _file_entry(install="opkg install fw", uninstall="opkg remove fw", check="test -f /opt/fw"),
        _host(),
    )
    ok = _RunHost(run_status=Status.Success)
    assert (await built.install(ok)).status is Status.Success
    assert await built.is_installed(ok) is True
    assert (await built.uninstall(ok)).status is Status.Success
    assert ok.run_calls == ["opkg install fw", "test -f /opt/fw", "opkg remove fw"]
    failing = _RunHost(run_status=Status.Failed)
    assert await built.is_installed(failing) is False
    # The command's Result comes back UNCHANGED, not re-wrapped as success —
    # a mutation that swallows run()'s status and always returns Success
    # must fail here.
    assert (await built.install(_RunHost(run_status=Status.Failed))).status is Status.Failed
    assert (await built.uninstall(_RunHost(run_status=Status.Failed))).status is Status.Failed


# ── factory chokepoint ───────────────────────────────────────────────────────


def test_factory_applies_declared_before_providers_after_the_lab_stamp(monkeypatch):
    """End to end: a fake repo's [[products]]/[[dev_tools]] entries land on a
    factory-built host, beat same-name providers on BOTH seams, and the gate
    saw the stamped lab.

    Kills: (a) omitting either apply_declared_* call; (b) placing either after
    its provider apply (the provider would win the name); (c) placing them
    above the source_lab stamp (the gate would judge "" and this scoped repo
    would be skipped, attaching nothing). Symmetric over both seams — a fix
    that pins only apply_declared_products would leave apply_declared_dev_tools
    droppable/reorderable with the whole suite green.
    """
    import otto.config as config_mod
    from otto.host.factory import create_host_from_dict
    from otto.registry import registering_repo

    product_entry = DeclaredEntry(
        name="fw",
        kind="file",
        seam="products",
        owner="declrepo",
        base_dir=Path("/repo"),
        match={"id": "probe-box.*"},
        params={"artifact": "build/fw.bin"},
    )
    dev_tool_entry = DeclaredEntry(
        name="probe",
        kind="file",
        seam="dev_tools",
        owner="declrepo",
        base_dir=Path("/repo"),
        match={"id": "probe-box.*"},
        params={"artifact": "tools/probe.sh"},
    )
    repo = SimpleNamespace(
        name="declrepo",
        project_scope=_scope_for_factory(["somelab"]),
        declared_products=[product_entry],
        declared_dev_tools=[dev_tool_entry],
    )
    monkeypatch.setattr(config_mod, "get_repos", lambda: [repo])
    monkeypatch.setattr(config_mod, "get_ordered_repos", lambda: [repo])
    monkeypatch.setattr(config_mod, "is_bootstrapped", lambda: True)
    with registering_repo("coderepo"):
        product_mod.register_product_provider(lambda host: [SimpleNamespace(name="fw", owner=None)])
        dev_tool_mod.register_dev_tool_provider(
            lambda host: [SimpleNamespace(name="probe", owner=None)]
        )

    host = create_host_from_dict(
        {
            "element": "probe-box",
            "os_type": "unix",
            "ip": "10.0.0.9",
            "creds": [{"login": "admin", "password": "admin"}],
        },
        lab_name="somelab",
    )

    (fw,) = host.products
    assert (fw.name, fw.owner) == ("fw", "declrepo"), "declared must win over the provider"
    assert fw.artifact == Path("/repo/build/fw.bin")

    (probe,) = host.dev_tools
    assert (probe.name, probe.owner) == ("probe", "declrepo"), "declared must win over the provider"
    assert probe.artifact == Path("/repo/tools/probe.sh")


def _scope_for_factory(labs):
    import re as _re

    from otto.config.scope import ProjectScopeConfig

    return ProjectScopeConfig(
        lab_patterns=[_re.compile(p) for p in labs],
        host_patterns=[_re.compile(".*")],
    )
