"""The product / dev-tool listing library: what repos declare, what each host gets.

The listing reads facts the ingest chokepoint recorded (``kind``, ``origin``,
``shadowed_*``) and derives the rest from the declared entries and the lab, so
none of it may contact a host. The lab is built the way ingest builds it --
``apply_providers`` against host doubles -- so the rows are checked against
what the real loops attached, not against a second implementation of them.
"""

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from otto.config.scope import ProjectScopeConfig
from otto.declared import DeclaredEntry
from otto.host import dev_tool as dev_tool_mod
from otto.host import listing
from otto.host import product as product_mod
from otto.host.dev_tool import DevTool, register_dev_tool_provider
from otto.host.factory import apply_providers
from otto.host.product import Product, ShellProduct, register_product_provider
from otto.registry import registering_repo
from otto.result import Result
from otto.utils import Status

ROOT1 = Path("/work/repo1")
ROOT2 = Path("/work/repo2")


@pytest.fixture(autouse=True)
def _isolate_provider_registries():
    saved_p = list(product_mod._PRODUCT_PROVIDERS)
    saved_t = list(dev_tool_mod._DEV_TOOL_PROVIDERS)
    try:
        yield
    finally:
        product_mod._PRODUCT_PROVIDERS[:] = saved_p
        dev_tool_mod._DEV_TOOL_PROVIDERS[:] = saved_t


class ProbeProduct(Product):
    """A code product with no artifact."""

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


class ProbeTool(DevTool):
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


class ArtifactProduct(ShellProduct):
    """A code product that stages a file."""

    async def install(self, host):
        return Result(Status.Success)

    async def uninstall(self, host):
        return Result(Status.Success)

    async def is_installed(self, host):
        return True


def _entry(repo, name, kind="shell", *, seam="products", match=None, **params):
    root = ROOT1 if repo == "repo1" else ROOT2
    return DeclaredEntry(
        name=name,
        kind=kind,
        seam=seam,
        owner=repo,
        base_dir=root,
        match=match or {},
        params=params,
    )


def _repo(name, products=(), dev_tools=(), scope=None):
    return SimpleNamespace(
        name=name,
        sut_dir=ROOT1 if name == "repo1" else ROOT2,
        declared_products=list(products),
        declared_dev_tools=list(dev_tools),
        project_scope=scope,
    )


def _host(hid, *, lab="labA", default_dest_dir=None, **attrs):
    host = SimpleNamespace(
        id=hid,
        source_lab=lab,
        products=[],
        dev_tools=[],
        shadowed_products=[],
        shadowed_dev_tools=[],
        default_dest_dir=default_dest_dir,
        cached_login_home=None,
        ip="10.0.0.1",
        loader=object(),
    )
    for k, v in attrs.items():
        setattr(host, k, v)
    return host


@pytest.fixture
def ingest(monkeypatch):
    """Return a function that ingests *hosts* against *repos* the way a lab load does."""

    def go(repos, hosts):
        from otto import config

        monkeypatch.setattr(config, "is_bootstrapped", lambda: True)
        monkeypatch.setattr(config, "get_repos", lambda: list(repos))
        monkeypatch.setattr(config, "get_ordered_repos", lambda: list(repos))
        for host in hosts:
            apply_providers(host)
        return SimpleNamespace(name="labA", hosts={h.id: h for h in hosts})

    return go


# ── No lab: what the repos declare ────────────────────────────────────────────


def test_declared_rows_follow_repo_then_declaration_order():
    repos = [
        _repo(
            "repo1",
            products=[
                _entry("repo1", "agent", artifact="build/agent.tar.gz"),
                _entry("repo1", "kcov", "kmod", artifact="build/kcov.ko"),
            ],
        ),
        _repo("repo2", products=[_entry("repo2", "fw", "embedded", artifact="out/fw.o")]),
    ]
    rows = listing.declared_rows(repos, "products")
    assert [(r.name, r.kind, r.repo, r.artifact) for r in rows] == [
        ("agent", "shell", "repo1", "build/agent.tar.gz"),
        ("kcov", "kmod", "repo1", "build/kcov.ko"),
        ("fw", "embedded", "repo2", "out/fw.o"),
    ]


def test_empty_match_reads_any_host_and_a_selector_is_rendered_compactly():
    repos = [
        _repo(
            "repo1",
            products=[
                _entry("repo1", "a", artifact="a"),
                _entry(
                    "repo1",
                    "b",
                    artifact="b",
                    match={"id": "bb.*", "os_name": ["debian", "ubuntu"], "metadata.n": 3},
                ),
            ],
        )
    ]
    rows = listing.declared_rows(repos, "products")
    assert rows[0].match == "any host"
    assert rows[1].match == "id=bb.*, os_name=[debian, ubuntu], metadata.n=3"


def test_docker_image_artifact_is_the_image_and_other_kinds_without_one_are_empty():
    repos = [
        _repo(
            "repo1",
            products=[
                _entry("repo1", "img", "docker_image", image="registry/app:1"),
                _entry("repo1", "custom", "mykind"),
            ],
        )
    ]
    rows = listing.declared_rows(repos, "products")
    assert [r.artifact for r in rows] == ["registry/app:1", ""]


def test_dev_tools_seam_reads_the_dev_tool_entries():
    repos = [
        _repo(
            "repo1",
            products=[_entry("repo1", "p", artifact="p")],
            dev_tools=[_entry("repo1", "t", seam="dev_tools", artifact="tools/t.sh")],
        )
    ]
    assert [r.name for r in listing.declared_rows(repos, "dev_tools")] == ["t"]
    assert [r.name for r in listing.declared_rows(repos, "products")] == ["p"]


def test_provider_note_names_module_and_qualname_per_registering_repo():
    def supply(host):
        return None

    with registering_repo("repo2"):
        register_product_provider(supply)
    with registering_repo("repo1"):
        register_dev_tool_provider(supply)
    repos = [_repo("repo1"), _repo("repo2")]
    [note] = listing.provider_notes(repos, "products")
    assert note == (
        f"repo2 also registers a product provider ({supply.__module__}.{supply.__qualname__}); "
        "what it supplies depends on the host, so it is listed only with --lab."
    )
    [tool_note] = listing.provider_notes(repos, "dev_tools")
    assert tool_note.startswith("repo1 also registers a dev tool provider (")


def test_providers_of_repos_outside_the_listing_are_not_noted():
    def supply(host):
        return None

    with registering_repo("elsewhere"):
        register_product_provider(supply)
    assert listing.provider_notes([_repo("repo1")], "products") == []


def test_the_public_provider_accessors_are_read_only_snapshots():
    def supply(host):
        return None

    with registering_repo("repo1"):
        register_product_provider(supply)
        register_dev_tool_provider(supply)
    got = product_mod.registered_product_providers()
    assert (supply, "repo1") in got
    got.clear()
    assert (supply, "repo1") in product_mod.registered_product_providers()
    assert (supply, "repo1") in dev_tool_mod.registered_dev_tool_providers()


# ── Stamped facts ─────────────────────────────────────────────────────────────


def test_declared_instances_carry_kind_origin_and_their_source_entry(ingest):
    host = _host("test1")
    entry = _entry("repo1", "agent", artifact="a")
    ingest([_repo("repo1", products=[entry])], [host])
    [p] = host.products
    assert (p.kind, p.origin) == ("shell", "declared")
    assert p.source_entry is entry


def test_provider_instances_carry_code_kind_and_provider_origin(ingest):
    with registering_repo("repo2"):
        register_product_provider(lambda host: [ProbeProduct("probe")])
        register_dev_tool_provider(lambda host: [ProbeTool("tool")])
    host = _host("test1")
    ingest([_repo("repo2")], [host])
    assert (host.products[0].kind, host.products[0].origin) == ("code", "provider")
    assert (host.dev_tools[0].kind, host.dev_tools[0].origin) == ("code", "provider")
    assert host.products[0].source_entry is None


def test_a_provider_dropped_for_a_taken_name_is_recorded_on_the_host(ingest):
    with registering_repo("repo2"):
        register_product_provider(lambda host: [ProbeProduct("agent")])
        register_dev_tool_provider(lambda host: [ProbeTool("t")])
    host = _host("test1")
    ingest(
        [
            _repo(
                "repo1",
                products=[_entry("repo1", "agent", artifact="a")],
                dev_tools=[_entry("repo1", "t", seam="dev_tools", artifact="t")],
            ),
            _repo("repo2"),
        ],
        [host],
    )
    assert [(hid, p.name, p.owner) for hid, p in host.shadowed_products] == [
        ("test1", "agent", "repo2")
    ]
    assert [(hid, t.name) for hid, t in host.shadowed_dev_tools] == [("test1", "t")]
    assert [p.kind for p in host.products] == ["shell"]


# ── With a lab: what each host gets ───────────────────────────────────────────


def test_a_product_on_two_hosts_is_one_row_listing_both_in_lab_order(ingest):
    repos = [_repo("repo1", products=[_entry("repo1", "agent", artifact="build/agent.tar.gz")])]
    lab = ingest(repos, [_host("test2"), _host("test1")])
    result = listing.lab_rows(lab, repos, "products")
    [row] = result.rows
    assert (row.name, row.kind, row.repo, row.artifact) == (
        "agent",
        "shell",
        "repo1",
        "build/agent.tar.gz",
    )
    assert row.hosts == ["test2", "test1"]
    assert result.unused == []


def test_the_instance_artifact_is_shown_relative_to_its_repo_root(ingest):
    repos = [_repo("repo1", products=[_entry("repo1", "agent", artifact="build/agent.tar.gz")])]
    lab = ingest(repos, [_host("test1")])
    # The shell kind anchors the declared path under the repo root; the listing
    # shows it the way it was written.
    assert Path(lab.hosts["test1"].products[0].artifact).is_absolute()
    assert listing.lab_rows(lab, repos, "products").rows[0].artifact == "build/agent.tar.gz"


def test_a_provider_product_shows_code_and_its_class_name(ingest):
    with registering_repo("repo2"):
        register_product_provider(lambda host: [ProbeProduct("probe")])
    repos = [_repo("repo2")]
    lab = ingest(repos, [_host("test3")])
    [row] = listing.lab_rows(lab, repos, "products").rows
    assert (row.name, row.kind, row.repo, row.artifact, row.stage_dir) == (
        "probe",
        "code (ProbeProduct)",
        "repo2",
        "",
        "",
    )


def test_stage_dir_is_the_declared_dir_else_the_host_default_else_login_home(ingest):
    repos = [
        _repo(
            "repo1",
            products=[
                _entry("repo1", "declared", artifact="a", stage_dir="/opt/stage"),
                _entry("repo1", "defaulted", artifact="b", match={"id": "test2"}),
                _entry("repo1", "home", artifact="c", match={"id": "test1"}),
            ],
        )
    ]
    lab = ingest(
        repos,
        [_host("test1"), _host("test2", default_dest_dir=Path("/srv/drop"))],
    )
    rows = {r.name: r for r in listing.lab_rows(lab, repos, "products").rows}
    assert rows["declared"].stage_dir == "/opt/stage"
    assert rows["declared"].hosts == ["test1", "test2"]
    assert rows["defaulted"].stage_dir == "/srv/drop"
    assert rows["home"].stage_dir == "login home"


def test_a_kind_that_stages_nothing_leaves_the_stage_dir_empty(ingest):
    repos = [
        _repo(
            "repo1",
            products=[
                _entry("repo1", "img", "docker_image", image="registry/app:1"),
                _entry("repo1", "fw", "embedded", artifact="out/fw.o"),
            ],
        )
    ]
    lab = ingest(repos, [_host("test1")])
    rows = listing.lab_rows(lab, repos, "products").rows
    assert [(r.name, r.artifact, r.stage_dir) for r in rows] == [
        ("img", "registry/app:1", ""),
        ("fw", "out/fw.o", ""),
    ]


def test_one_product_with_different_stage_dirs_per_host_is_two_rows(ingest):
    repos = [_repo("repo1", products=[_entry("repo1", "agent", artifact="a")])]
    lab = ingest(
        repos,
        [
            _host("test1", default_dest_dir=Path("/one")),
            _host("test2", default_dest_dir=Path("/two")),
        ],
    )
    rows = listing.lab_rows(lab, repos, "products").rows
    assert [(r.hosts, r.stage_dir) for r in rows] == [(["test1"], "/one"), (["test2"], "/two")]


def test_dev_tools_seam_lists_the_dev_tool_rows(ingest):
    repos = [
        _repo(
            "repo1",
            products=[_entry("repo1", "p", artifact="p")],
            dev_tools=[_entry("repo1", "t", seam="dev_tools", artifact="tools/t.sh")],
        )
    ]
    lab = ingest(repos, [_host("test1")])
    assert [r.name for r in listing.lab_rows(lab, repos, "dev_tools").rows] == ["t"]


# ── The three "not used" reasons, and the shadowed provider ───────────────────


def test_an_entry_no_host_matches_is_not_used_with_that_reason(ingest):
    repos = [
        _repo(
            "repo2",
            products=[_entry("repo2", "fw", "embedded", artifact="o", match={"id": "nope"})],
        )
    ]
    lab = ingest(repos, [_host("test1")])
    result = listing.lab_rows(lab, repos, "products")
    assert result.rows == []
    assert [(u.name, u.kind, u.repo, u.reason) for u in result.unused] == [
        ("fw", "embedded", "repo2", "no host matches")
    ]


def test_an_entry_outside_the_repos_project_scope_says_so(ingest):
    scope = ProjectScopeConfig([re.compile("otherlab")], [re.compile(".*")])
    repos = [_repo("repo1", products=[_entry("repo1", "agent", artifact="a")], scope=scope)]
    lab = ingest(repos, [_host("test1"), _host("test2")])
    result = listing.lab_rows(lab, repos, "products")
    assert result.rows == []
    assert [(u.name, u.reason) for u in result.unused] == [
        ("agent", "outside repo1's [project] scope")
    ]


def test_scope_that_admits_a_host_is_not_reported_as_outside(ingest):
    scope = ProjectScopeConfig([re.compile("labA")], [re.compile("test1")])
    repos = [
        _repo(
            "repo1",
            products=[_entry("repo1", "agent", artifact="a", match={"id": "test2"})],
            scope=scope,
        )
    ]
    lab = ingest(repos, [_host("test1"), _host("test2")])
    [unused] = listing.lab_rows(lab, repos, "products").unused
    assert unused.reason == "no host matches"


def test_a_later_entry_with_a_taken_name_is_shadowed_and_listed_once(ingest):
    repos = [
        _repo(
            "repo1",
            products=[
                _entry("repo1", "probe", artifact="first"),
                _entry("repo1", "probe", artifact="second"),
            ],
        )
    ]
    lab = ingest(repos, [_host("test1"), _host("test2")])
    result = listing.lab_rows(lab, repos, "products")
    assert [r.artifact for r in result.rows] == ["first"]
    assert [(u.name, u.reason) for u in result.unused] == [
        ("probe", "shadowed by an earlier entry named 'probe'")
    ]


def test_a_fallback_entry_used_on_some_host_is_not_reported_unused(ingest):
    repos = [
        _repo(
            "repo1",
            products=[
                _entry("repo1", "fw", artifact="rev2", match={"id": "test1"}),
                _entry("repo1", "fw", artifact="generic"),
            ],
        )
    ]
    lab = ingest(repos, [_host("test1"), _host("test2")])
    result = listing.lab_rows(lab, repos, "products")
    assert [(r.artifact, r.hosts) for r in result.rows] == [
        ("rev2", ["test1"]),
        ("generic", ["test2"]),
    ]
    assert result.unused == []


def test_a_provider_product_dropped_for_a_declared_name_is_reported(ingest):
    with registering_repo("repo2"):
        register_product_provider(lambda host: [ProbeProduct("agent")])
    repos = [_repo("repo1", products=[_entry("repo1", "agent", artifact="a")]), _repo("repo2")]
    lab = ingest(repos, [_host("test1"), _host("test2")])
    result = listing.lab_rows(lab, repos, "products")
    assert [(u.name, u.kind, u.repo, u.reason) for u in result.unused] == [
        ("agent", "code (ProbeProduct)", "repo2", "shadowed by the declared entry named 'agent'")
    ]


def test_a_provider_product_dropped_for_an_earlier_provider_reads_an_earlier_entry(ingest):
    with registering_repo("repo1"):
        register_product_provider(lambda host: [ProbeProduct("probe")])
    with registering_repo("repo2"):
        register_product_provider(lambda host: [ProbeProduct("probe")])
    repos = [_repo("repo1"), _repo("repo2")]
    lab = ingest(repos, [_host("test1")])
    result = listing.lab_rows(lab, repos, "products")
    assert [(u.repo, u.reason) for u in result.unused] == [
        ("repo2", "shadowed by an earlier entry named 'probe'")
    ]


def test_a_provider_product_shadowed_twice_for_different_reasons_is_listed_once(ingest):
    with registering_repo("repo1"):
        register_product_provider(lambda host: [ProbeProduct("probe")])
    with registering_repo("repo2"):
        register_product_provider(lambda host: [ProbeProduct("probe")])
    repos = [
        _repo("repo1", products=[_entry("repo1", "probe", artifact="a", match={"id": "test1"})]),
        _repo("repo2"),
    ]
    lab = ingest(repos, [_host("test1"), _host("test2")])
    result = listing.lab_rows(lab, repos, "products")
    # repo1's provider lands on test2, so only repo2's product is unused; it was
    # dropped on test1 (declared holder) and on test2 (earlier provider).
    assert [(u.repo, u.reason) for u in result.unused] == [
        ("repo2", "shadowed by the declared entry named 'probe'")
    ]


def test_the_scope_verdict_comes_from_declared_for_host(ingest, monkeypatch):
    """A gate added to ``declared_for_host`` reaches the listing: no second copy of it."""
    scope = ProjectScopeConfig([re.compile(".*")], [re.compile(".*")])
    repos = [
        _repo(
            "repo1",
            products=[_entry("repo1", "agent", artifact="a", match={"id": "nope"})],
            scope=scope,
        )
    ]
    lab = ingest(repos, [_host("test1")])
    assert [u.reason for u in listing.lab_rows(lab, repos, "products").unused] == [
        "no host matches"
    ]
    monkeypatch.setattr(listing, "declared_for_host", lambda host, attr: [])
    assert [u.reason for u in listing.lab_rows(lab, repos, "products").unused] == [
        "outside repo1's [project] scope"
    ]


def test_an_entry_the_ingest_built_is_never_reported_whatever_the_gate_says(ingest, monkeypatch):
    """What built is read off the recorded stamp, not recomputed."""
    repos = [_repo("repo1", products=[_entry("repo1", "agent", artifact="a")])]
    lab = ingest(repos, [_host("test1")])
    monkeypatch.setattr(listing, "declared_for_host", lambda host, attr: [])
    assert listing.lab_rows(lab, repos, "products").unused == []


def test_a_provider_product_that_lands_elsewhere_is_not_reported_shadowed(ingest):
    with registering_repo("repo2"):
        register_product_provider(lambda host: [ProbeProduct("agent")])
    repos = [
        _repo("repo1", products=[_entry("repo1", "agent", artifact="a", match={"id": "test1"})]),
        _repo("repo2"),
    ]
    lab = ingest(repos, [_host("test1"), _host("test2")])
    result = listing.lab_rows(lab, repos, "products")
    assert sorted((r.repo, r.hosts[0]) for r in result.rows) == [
        ("repo1", "test1"),
        ("repo2", "test2"),
    ]
    assert result.unused == []


# ── No host contact ───────────────────────────────────────────────────────────


def _forbid(*_a, **_k):
    raise AssertionError("the listing contacted a host")


def test_listing_never_contacts_a_host(ingest):
    repos = [
        _repo(
            "repo1",
            products=[_entry("repo1", "agent", artifact="a")],
            dev_tools=[_entry("repo1", "t", seam="dev_tools", artifact="t")],
        )
    ]
    hosts = [
        _host("test1", exec=_forbid, run=_forbid, login_home=_forbid, put=_forbid, get=_forbid)
    ]
    lab = ingest(repos, hosts)
    for seam in ("products", "dev_tools"):
        assert listing.lab_rows(lab, repos, seam).rows
        assert listing.declared_rows(repos, seam)
