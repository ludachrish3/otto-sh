"""Generic declared-entry core: typed match table, kind registry, repo collection."""

import logging
from types import SimpleNamespace

import pytest

from otto import declared as declared_mod
from otto.declared import (
    MATCH_KEYS,
    host_matches,
    validate_match_table,
)


@pytest.fixture(autouse=True)
def _reset_warned_versions(monkeypatch):
    """Reset the module-level _warned_versions set before each test."""
    monkeypatch.setattr(declared_mod, "_warned_versions", set())


def _host(**attrs):
    """Minimal host double — the matcher reads plain attributes only."""
    attrs.setdefault("id", "h1")
    attrs.setdefault("source_lab", "")
    return SimpleNamespace(**attrs)


# ── host_matches: value typing ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("match", "attrs", "expected"),
    [
        # plain string = regex FULLmatch (a substring hit must not admit)
        ({"id": "bb.*_qemu"}, {"id": "bb7_qemu"}, True),
        ({"id": "bb"}, {"id": "bb-extra"}, False),
        # specifier-prefixed string = version comparison
        ({"os_version": ">=3.7"}, {"os_version": "3.7"}, True),
        ({"os_version": ">=3.7"}, {"os_version": "2.7"}, False),
        ({"os_version": "!=3.7"}, {"os_version": "3.8"}, True),
        # bool / number = equality
        ({"element_id": 3}, {"element_id": 3}, True),
        ({"element_id": 3}, {"element_id": 4}, False),
        # list = any-of, elements typed by the same rules
        ({"os_name": ["Linux", "Zephyr"]}, {"os_name": "Zephyr"}, True),
        ({"os_name": ["Linux", "Zephyr"]}, {"os_name": "VxWorks"}, False),
        ({"os_version": [">=4.0", "2.7"]}, {"os_version": "2.7"}, True),
        # AND across keys
        (
            {"id": "bb.*", "os_version": ">=3.7"},
            {"id": "bb1", "os_version": "3.6"},
            False,
        ),
        ({"id": "bb.*", "os_version": ">=3.7"}, {"id": "bb1", "os_version": "3.9"}, True),
        # empty match admits every host
        ({}, {"id": "anything"}, True),
        # a None attribute never matches (os_version defaults to None on hosts)
        ({"os_version": ">=1.0"}, {"os_version": None}, False),
        ({"os_name": "Linux"}, {"os_name": None}, False),
    ],
)
def test_value_semantics(match, attrs, expected):
    assert host_matches(match, _host(**attrs)) is expected


def test_dotted_metadata_paths():
    host = _host(
        metadata={"hw_version": "rev2", "nested": {"deep": "x"}},
        element_metadata={"site": "lab-a"},
    )
    assert host_matches({"metadata.hw_version": "rev2"}, host)
    assert host_matches({"metadata.nested.deep": "x"}, host)
    assert host_matches({"element_metadata.site": "lab-.*"}, host)
    # a missing metadata key is a no-match, never an error: presence varies per lab
    assert not host_matches({"metadata.absent": ".*"}, host)
    assert not host_matches({"metadata.nested.absent": ".*"}, host)


def test_unknown_key_raises_naming_the_key_and_the_valid_set():
    with pytest.raises(ValueError, match=r"is_virtual"):
        host_matches({"is_virtual": True}, _host())
    with pytest.raises(ValueError, match=r"metadata\."):
        # the error must teach the dotted escape hatch
        host_matches({"is_virtual": True}, _host())


def test_unparseable_host_version_is_a_no_match_with_one_warning(caplog):
    host = _host(os_version="release-candidate-fw")
    with caplog.at_level(logging.WARNING, logger="otto.declared"):
        assert not host_matches({"os_version": ">=3.7"}, host)
        assert not host_matches({"os_version": ">=3.7"}, host)
    warnings = [r for r in caplog.records if "release-candidate-fw" in r.getMessage()]
    assert len(warnings) == 1, "the warning must fire exactly once per (host, key, pattern)"


# ── validate_match_table: the parse-time half ────────────────────────────────


def test_validate_accepts_a_well_formed_table():
    validate_match_table(
        {"id": "bb.*", "os_version": ">=3.7", "element_id": 3, "metadata.hw": ["a", "b"]}
    )


@pytest.mark.parametrize(
    ("match", "fragment"),
    [
        ({"is_virtual": True}, "is_virtual"),  # unknown key
        ({"id": "bb["}, "bb["),  # regex that does not compile
        ({"os_version": ">=not.a.version"}, "not.a.version"),  # bad specifier
        ({"metadata": "x"}, "metadata"),  # bare metadata without a dotted path
    ],
)
def test_validate_rejects_malformed_tables(match, fragment):
    with pytest.raises(ValueError, match=fragment.replace("[", r"\[").replace(".", r"\.")):
        validate_match_table(match)


def test_match_keys_is_the_documented_provider_surface():
    assert (
        frozenset(
            {"id", "element", "element_id", "os_type", "os_name", "os_version", "ip", "source_lab"}
        )
        == MATCH_KEYS
    )


# ── KindRegistry.build ───────────────────────────────────────────────────────

from pathlib import Path

from otto.declared import DeclaredEntry, KindRegistry


def _entry(name, kind="toy", seam="products", owner="acme", match=None, **params):
    return DeclaredEntry(
        name=name,
        kind=kind,
        seam=seam,
        owner=owner,
        base_dir=Path("/repo"),
        match=match or {},
        params=params,
    )


def _toy_factory(entry, host):
    return SimpleNamespace(name=entry.name, owner=None, built_for=host.id, **entry.params)


@pytest.fixture
def kinds():
    reg: KindRegistry = KindRegistry("toy kind", register_hint="register_toy_kind()")
    reg.register("toy", _toy_factory, origin="tests")
    return reg


def test_build_first_match_wins_in_declaration_order(kinds):
    host = _host(id="bb1", os_version="3.9")
    entries = [
        _entry("fw", match={"os_version": ">=4.0"}, artifact="new.bin"),  # misses
        _entry("fw", match={"os_version": ">=3.0"}, artifact="mid.bin"),  # first hit
        _entry("fw", artifact="fallback.bin"),  # same name, already taken
        _entry("probe", artifact="probe.sh"),  # distinct name accumulates
    ]
    built = kinds.build(entries, host)
    assert [(b.name, b.artifact) for b in built] == [("fw", "mid.bin"), ("probe", "probe.sh")]


def test_build_stamps_owner_unless_the_factory_already_named_one(kinds):
    def opinionated(entry, host):
        return SimpleNamespace(name=entry.name, owner="handed-over")

    kinds.register("opinionated", opinionated, origin="tests")
    built = kinds.build([_entry("a"), _entry("b", kind="opinionated")], _host())
    assert [(b.name, b.owner) for b in built] == [("a", "acme"), ("b", "handed-over")]


def test_build_unknown_kind_fails_even_when_the_match_misses(kinds):
    # An entry whose kind is a typo must fail EVERY ingest loudly, not only on
    # the hosts it happens to match — otherwise the typo ships silently.
    entries = [_entry("fw", kind="fiel", match={"id": "matches-no-host"})]
    with pytest.raises(ValueError, match=r"fiel.*Did you mean 'toy'|Unknown toy kind"):
        kinds.build(entries, _host())


def test_build_runs_the_matcher_per_host(kinds):
    entries = [_entry("fw", match={"id": "bb.*"})]
    assert [b.name for b in kinds.build(entries, _host(id="bb1"))] == ["fw"]
    assert kinds.build(entries, _host(id="server1")) == []


# ── declared_for_host: repo collection + the §5 targeting gate ───────────────

from otto.declared import declared_for_host


def _repo(name="repo1", scope=None, products=(), dev_tools=()):
    return SimpleNamespace(
        name=name,
        project_scope=scope,
        declared_products=list(products),
        declared_dev_tools=list(dev_tools),
    )


def _scope(labs, hosts=(".*",)):
    """A compiled [project] declaration, without going through settings parse."""
    import re as _re

    from otto.config.scope import ProjectScopeConfig

    return ProjectScopeConfig(
        lab_patterns=[_re.compile(p) for p in labs],
        host_patterns=[_re.compile(p) for p in hosts],
    )


@pytest.fixture
def repos_table(monkeypatch):
    """Fake the bootstrap's repos — declared_for_host reads them via config.get_repos.

    Also pins the non-forcing probe as "bootstrapped": declared_for_host checks
    ``is_bootstrapped()`` before ever calling ``get_repos()``, so a fixture that
    patched only the latter would have every test below silently collect
    nothing — patching both keeps the simulated state honest.
    """
    import otto.config as config_mod

    table: list = []
    monkeypatch.setattr(config_mod, "get_repos", lambda: table)
    # Every faked repo survives the dependency pass unless a test says
    # otherwise — declared_for_host filters against the survivors' names.
    monkeypatch.setattr(config_mod, "get_ordered_repos", lambda: table)
    monkeypatch.setattr(config_mod, "is_bootstrapped", lambda: True)
    return table


def test_declared_for_host_collects_only_targeting_repos(repos_table):
    e1, e2, e3 = _entry("a"), _entry("b"), _entry("c")
    repos_table.extend(
        [
            _repo("in-scope", scope=_scope(["bench"]), products=[e1]),
            _repo("other-lab", scope=_scope(["floor"]), products=[e2]),
            _repo("undeclared", scope=None, products=[e3]),  # no [project] admits everything
        ]
    )
    host = _host(id="h1", source_lab="bench")
    assert declared_for_host(host, "declared_products") == [e1, e3]


def test_declared_for_host_skips_dependency_skipped_repos(repos_table, monkeypatch):
    """A repo the dependency pass dropped contributes no entries (Chris,
    2026-09-02): its init never ran, so applying its declared half while its
    provider half stays silent would split the two seams' view of the repo.
    Precedence among survivors stays get_repos() discovery order — the
    surviving list here is deliberately REVERSED to prove the filter never
    reorders."""
    import otto.config as config_mod

    e1, e2, e3 = _entry("a"), _entry("b"), _entry("c")
    r1 = _repo("first", products=[e1])
    r2 = _repo("dep-skipped", products=[e2])
    r3 = _repo("last", products=[e3])
    repos_table.extend([r1, r2, r3])
    monkeypatch.setattr(config_mod, "get_ordered_repos", lambda: [r3, r1])
    assert declared_for_host(_host(), "declared_products") == [e1, e3]


def test_declared_for_host_unstamped_host_is_not_judged(repos_table):
    # The provider loops' carve-out, identically: source_lab == "" means a host
    # built outside the loader, which predates scoping and admits everything.
    e = _entry("a")
    repos_table.append(_repo(scope=_scope(["bench"]), products=[e]))
    assert declared_for_host(_host(source_lab=""), "declared_products") == [e]


def test_declared_for_host_selects_the_named_seam(repos_table):
    p, t = _entry("p"), _entry("t", seam="dev_tools")
    repos_table.append(_repo(products=[p], dev_tools=[t]))
    host = _host()
    assert declared_for_host(host, "declared_products") == [p]
    assert declared_for_host(host, "declared_dev_tools") == [t]


def test_declared_for_host_unreachable_config_yields_nothing(monkeypatch, caplog):
    # Unlike scope_for_repo's admit-on-failure, "no config" here means the
    # entries themselves do not exist — the empty answer IS the true answer.
    # Bootstrapped is pinned True so this exercises get_repos() raising, not
    # the non-forcing probe short-circuit (that path is a separate test).
    import logging

    import otto.config as config_mod

    def boom():
        raise RuntimeError("no bootstrap")

    monkeypatch.setattr(config_mod, "get_repos", boom)
    monkeypatch.setattr(config_mod, "is_bootstrapped", lambda: True)
    with caplog.at_level(logging.DEBUG, logger="otto.declared"):
        assert declared_for_host(_host(), "declared_products") == []
    assert "config unreachable" in caplog.text


def test_declared_for_host_never_forces_bootstrap(monkeypatch):
    """Entry collection must PROBE, never FORCE: an un-bootstrapped process
    (bare-library, ``otto init``, a unit test constructing a host directly)
    must not pay discovery's cost or run repo init imports just to ask what
    entries exist yet — there are none until something else bootstraps.
    """
    import otto.config as config_mod

    called: list[bool] = []

    def boom():
        called.append(True)
        raise AssertionError("bootstrap forced")

    monkeypatch.setattr(config_mod, "get_repos", boom)
    monkeypatch.setattr(config_mod, "is_bootstrapped", lambda: False)
    assert declared_for_host(_host(), "declared_products") == []
    assert not called  # get_repos must never be reached when not bootstrapped


def test_repo_parse_settings_builds_declared_entries(tmp_path):
    from otto.config.repo import Repo
    from tests._fixtures.sutrepo import make_sut_repo

    sut = make_sut_repo(
        tmp_path / "repo",
        name="declrepo",
        extra="""
[[products]]
name = "fw"
kind = "file"
artifact = "build/fw.bin"
match = { id = "bb.*" }

[[dev_tools]]
name = "probe"
kind = "file"
artifact = "tools/probe.sh"
""",
    )
    repo = Repo(sut_dir=sut)
    (fw,) = repo.declared_products
    assert (fw.name, fw.kind, fw.seam, fw.owner) == ("fw", "file", "products", "declrepo")
    assert fw.base_dir == sut
    assert fw.params == {"artifact": "build/fw.bin"}  # raw: anchoring is the kind's job
    (probe,) = repo.declared_dev_tools
    assert (probe.seam, probe.owner) == ("dev_tools", "declrepo")
