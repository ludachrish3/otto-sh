"""Generic declared-entry core: typed match table, kind registry, project collection."""

import contextlib
import logging
import re
import sys
from types import SimpleNamespace

import pytest

from otto import declared as declared_mod
from otto.declared import (
    MATCH_KEYS,
    host_matches,
    validate_match_table,
)
from otto.host.element import Element
from tests._fixtures.fake_project import fake_project


@pytest.fixture(autouse=True)
def _reset_warned_versions(monkeypatch):
    """Reset the module-level _warned_versions set before each test."""
    monkeypatch.setattr(declared_mod, "_warned_versions", set())


def _host(**attrs):
    """Minimal host double — plain attributes, ``element`` defaulting to None.

    ``element`` defaults to ``None`` (the ``LocalHost``/``DockerContainerHost``
    case) so a case exercising element paths passes its own
    ``element=Element(...)`` override explicitly.
    """
    attrs.setdefault("id", "h1")
    attrs.setdefault("source_lab", "")
    attrs.setdefault("element", None)
    return SimpleNamespace(**attrs)


# ── host_matches: value typing ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("match", "attrs", "expected"),
    [
        # plain string = regex FULLmatch (a substring hit must not admit)
        ({"id": "bb.*-qemu"}, {"id": "bb7-qemu"}, True),
        ({"id": "bb"}, {"id": "bb-extra"}, False),
        # specifier-prefixed string = version comparison
        ({"os_version": ">=3.7"}, {"os_version": "3.7"}, True),
        ({"os_version": ">=3.7"}, {"os_version": "2.7"}, False),
        ({"os_version": "!=3.7"}, {"os_version": "3.8"}, True),
        # bool / number = equality
        ({"element.id": 3}, {"element": Element("e", id=3)}, True),
        ({"element.id": 3}, {"element": Element("e", id=4)}, False),
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
        element=Element("e", metadata={"site": "lab-a"}),
    )
    assert host_matches({"metadata.hw_version": "rev2"}, host)
    assert host_matches({"metadata.nested.deep": "x"}, host)
    assert host_matches({"element.metadata.site": "lab-.*"}, host)
    # a missing metadata key is a no-match, never an error: presence varies per lab
    assert not host_matches({"metadata.absent": ".*"}, host)
    assert not host_matches({"metadata.nested.absent": ".*"}, host)


def test_metadata_path_never_falls_through_to_python_attributes():
    # A non-dict leaf under a metadata root must be a no-match, not a further
    # ATTRIBUTE lookup — an int's `.denominator` (== 1) or a str's
    # `.__class__` are real Python attributes that would otherwise leak
    # through and silently "match" a path the lab data never declared.
    host = _host(metadata={"count": 3})
    assert not host_matches({"metadata.count.denominator": 1}, host)


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
        {"id": "bb.*", "os_version": ">=3.7", "element.id": 3, "metadata.hw": ["a", "b"]}
    )


@pytest.mark.parametrize(
    ("match", "fragment"),
    [
        ({"is_virtual": True}, "is_virtual"),  # unknown key
        ({"id": "bb["}, "bb["),  # regex that does not compile
        ({"os_version": ">=not.a.version"}, "not.a.version"),  # bad specifier
        ({"metadata": "x"}, "metadata"),  # bare metadata without a dotted path
        ({"element.metadata": "x"}, "element.metadata"),  # bare element.metadata, same rule
    ],
)
def test_validate_rejects_malformed_tables(match, fragment):
    with pytest.raises(ValueError, match=fragment.replace("[", r"\[").replace(".", r"\.")):
        validate_match_table(match)


def test_match_keys_is_the_documented_provider_surface():
    assert (
        frozenset(
            {
                "id",
                "element.name",
                "element.id",
                "os_type",
                "os_name",
                "os_version",
                "ip",
                "source_lab",
            }
        )
        == MATCH_KEYS
    )


def _element_host(**overrides):
    """A host double with an Element; ``overrides`` replace any attribute
    (``element=None`` included).

    Distinct from this module's existing ``_host`` helper, which stays as is.
    """
    attrs: dict = {
        "id": "dut-cpu-1",
        "element": Element("dut", id=7, metadata={"rev": "B", "site": {"row": 3}}),
        "metadata": {"tag": "x"},
        "os_type": "unix",
        "os_name": "Linux",
        "os_version": "6.1",
        "ip": "10.0.0.1",
        "source_lab": "bench",
    }
    attrs.update(overrides)
    return SimpleNamespace(**attrs)


@pytest.mark.parametrize(
    ("match", "expected"),
    [
        ({"element.name": "dut"}, True),
        ({"element.name": "server"}, False),
        ({"element.id": 7}, True),
        ({"element.id": "7"}, True),
        ({"element.metadata.rev": "B"}, True),
        ({"element.metadata.site.row": 3}, True),
        ({"element.metadata.missing": ".*"}, False),
        ({"metadata.tag": "x"}, True),
    ],
)
def test_element_paths_match(match, expected):
    validate_match_table(match)
    assert host_matches(match, _element_host()) is expected


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("element", "element.name"),
        ("element_id", "element.id"),
        ("element_metadata.rev", "element.metadata.rev"),
    ],
)
def test_retired_keys_name_their_replacement(old, new):
    # Anchored on the retired-spelling PHRASE, not just the substrings: the
    # generic "unknown match key" message also lists MATCH_KEYS, which
    # contains the literal 'element.name' / 'element.id' strings — a loose
    # ``old.*new`` pattern would pass even if the retired-key naming were
    # deleted and every key fell through to the generic message.
    with pytest.raises(ValueError, match=rf"{re.escape(old)}.*is now spelled '{re.escape(new)}'"):
        validate_match_table({old: "x"})


def test_bare_element_object_is_never_a_match_value():
    with pytest.raises(ValueError, match=r"is now spelled 'element\.name'"):
        host_matches({"element": "dut"}, _element_host())


def test_element_less_host_never_matches_element_keys():
    assert host_matches({"element.name": ".*"}, _element_host(element=None)) is False


def test_unknown_element_subpath_is_not_a_retired_key():
    with pytest.raises(ValueError, match=r"unknown match key 'element\.resources'"):
        validate_match_table({"element.resources": "x"})


# ── KindBuilder.build ────────────────────────────────────────────────────────

from pathlib import Path

from otto.declared import DeclaredEntry, KindBuilder, KindEntry
from otto.registry import Registry


def _entry(
    name, kind="toy", seam="products", owner="acme", match=None, cls=None, variant=None, **params
):
    return DeclaredEntry(
        name=name,
        kind=kind,
        seam=seam,
        owner=owner,
        base_dir=Path("/project"),
        match=match or {},
        params=params,
        cls=cls,
        variant=variant,
    )


def _toy_factory(entry, host):
    return SimpleNamespace(name=entry.name, owner=None, built_for=host.id, **entry.params)


def _toy_resolver(entry):
    return object


def _boom_resolver(entry):
    raise ValueError("boom")


def _toy_kinds() -> Registry:
    """A toy seam's kind registry, empty."""
    return Registry("toy kind", entry=KindEntry, register_hint="register_toy_kind()")


@pytest.fixture
def kinds():
    table = _toy_kinds()
    table.register("toy", KindEntry(_toy_factory))
    return KindBuilder(table)


def test_kind_builder_is_not_a_registry():
    """The builder reads a plain kind registry; it is not one itself."""
    from otto.host.dev_tool import DEV_TOOL_KIND_BUILDER, DEV_TOOL_KINDS
    from otto.host.product import PRODUCT_KIND_BUILDER, PRODUCT_KINDS

    assert not isinstance(PRODUCT_KIND_BUILDER, Registry)
    assert not isinstance(DEV_TOOL_KIND_BUILDER, Registry)
    assert type(PRODUCT_KINDS) is Registry and type(DEV_TOOL_KINDS) is Registry  # noqa: PT018
    assert PRODUCT_KIND_BUILDER.kinds is PRODUCT_KINDS
    assert DEV_TOOL_KIND_BUILDER.kinds is DEV_TOOL_KINDS


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

    kinds.kinds.register("opinionated", KindEntry(opinionated))
    built = kinds.build([_entry("a"), _entry("b", kind="opinionated")], _host())
    assert [(b.name, b.owner) for b in built] == [("a", "acme"), ("b", "handed-over")]


def test_build_unknown_kind_fails_even_when_the_match_misses(kinds):
    # An entry whose kind is a typo must fail EVERY ingest loudly, not only on
    # the hosts it happens to match — otherwise the typo ships silently.
    entries = [_entry("fw", kind="fiel", match={"id": "matches-no-host"})]
    with pytest.raises(ValueError, match=r"fiel.*Did you mean 'toy'|Unknown toy kind"):
        kinds.build(entries, _host())


def test_build_refuses_the_retired_file_kind_naming_shell(kinds):
    # The rename is a hard cutover: an entry still spelling the old name must
    # fail every ingest with the new name in the message, before its match
    # is consulted — exactly as an unknown kind does.
    entries = [_entry("fw", kind="file", match={"id": "matches-no-host"})]
    with pytest.raises(ValueError, match=r"'fw': kind 'file' was renamed 'shell'"):
        kinds.build(entries, _host())


def test_build_runs_the_matcher_per_host(kinds):
    entries = [_entry("fw", match={"id": "bb.*"})]
    assert [b.name for b in kinds.build(entries, _host(id="bb1"))] == ["fw"]
    assert kinds.build(entries, _host(id="server1")) == []


def _variant_entry(name, variant, **params):
    return _entry(name, variant=variant, **params)


@contextlib.contextmanager
def _run_under(run):
    """Install a run policy choosing the *run* variant for the block: what ingest reads."""
    from otto.invocation import RunPolicy, install_policy, reset_binding

    binding = install_policy(RunPolicy(variant=run))
    try:
        yield
    finally:
        reset_binding(binding)


def test_an_entry_without_a_variant_matches_any_run(kinds):
    entries = [_entry("fw", artifact="any.bin")]
    for run in ("debug", "field"):
        with _run_under(run):
            assert [b.artifact for b in kinds.build(entries, _host())] == ["any.bin"]


def test_a_variant_entry_is_used_only_on_its_run(kinds):
    entries = [_variant_entry("fw", "field", artifact="field.bin")]
    with _run_under("field"):
        assert [b.artifact for b in kinds.build(entries, _host())] == ["field.bin"]
    with _run_under("debug"):
        assert kinds.build(entries, _host()) == []


def test_variant_entry_order_is_the_authors(kinds):
    # Same-name entries resolve first-match-wins in declaration order; a
    # variant entry is a more specific entry and goes FIRST, like a match.
    specific_first = [
        _variant_entry("fw", "field", artifact="field.bin"),
        _entry("fw", artifact="any.bin"),
    ]
    fallback_first = list(reversed(specific_first))
    with _run_under("field"):
        assert [b.artifact for b in kinds.build(specific_first, _host())] == ["field.bin"]
        assert [b.artifact for b in kinds.build(fallback_first, _host())] == ["any.bin"]


def test_a_variant_skipped_entry_does_not_take_the_name(kinds):
    entries = [
        _variant_entry("fw", "field", artifact="field.bin"),
        _entry("fw", artifact="debug.bin"),
    ]
    with _run_under("debug"):
        assert [b.artifact for b in kinds.build(entries, _host())] == ["debug.bin"]


def test_a_variant_entry_with_an_unknown_kind_still_fails_every_ingest(kinds):
    entries = [_variant_entry("fw", "field", kind="fiel")]
    with _run_under("debug"), pytest.raises(ValueError, match=r"fiel"):
        kinds.build(entries, _host())


def test_the_registry_reads_the_real_variant_when_nothing_patches_it(kinds):
    from otto import context

    token = context.set_variant("field")
    try:
        entries = [_variant_entry("fw", "field", artifact="field.bin")]
        assert [b.artifact for b in kinds.build(entries, _host())] == ["field.bin"]
    finally:
        context.reset_variant(token)


def test_a_class_entry_is_built_by_the_registrys_class_factory():
    from otto.registry import Ref

    reg = KindBuilder(
        _toy_kinds(),
        class_factory=Ref("tests.unit.test_declared:_toy_factory"),
        class_resolver=Ref("tests.unit.test_declared:_toy_resolver"),
    )
    entry = _entry("fw", kind=None, cls="acme.products:Firmware", artifact="x.bin")
    (built,) = reg.build([entry], _host(id="h1"))
    assert (built.name, built.built_for, built.kind, built.origin) == (
        "fw",
        "h1",
        "acme.products:Firmware",
        "declared",
    )


@pytest.mark.parametrize("skipped_by", ["match", "variant", "taken"])
def test_a_class_entry_is_resolved_before_match_variant_and_name_checks(skipped_by):
    from otto.registry import Ref

    reg = KindBuilder(
        _toy_kinds(),
        class_factory=Ref("tests.unit.test_declared:_toy_factory"),
        class_resolver=Ref("tests.unit.test_declared:_boom_resolver"),
    )
    entry = _entry(
        "fw",
        kind=None,
        cls="acme.products:Firmware",
        match={"os_version": ">=99"} if skipped_by == "match" else {},
        variant="field" if skipped_by == "variant" else None,
    )
    entries = [entry]
    if skipped_by == "taken":
        reg.kinds.register("toy", KindEntry(_toy_factory))
        entries.insert(0, _entry("fw", artifact="first.bin"))
    with _run_under("debug"), pytest.raises(ValueError, match="boom"):
        reg.build(entries, _host(os_version="1.0"))


@pytest.mark.parametrize(
    "hooks",
    [{"class_factory": "_toy_factory"}, {"class_resolver": "_toy_resolver"}],
)
def test_a_builder_with_only_one_class_hook_is_refused(hooks):
    from otto.registry import Ref

    refs = {k: Ref(f"tests.unit.test_declared:{v}") for k, v in hooks.items()}
    with pytest.raises(ValueError, match=r"`class_factory` and `class_resolver`"):
        KindBuilder(_toy_kinds(), **refs)


def test_a_class_entry_without_a_class_factory_fails_even_when_taken_and_variant_skipped(
    kinds,
):
    entries = [
        _entry("fw", artifact="first.bin"),
        _entry("fw", kind=None, cls="acme.products:Firmware", variant="field"),
    ]
    with (
        _run_under("debug"),
        pytest.raises(ValueError, match=r"toy kind registry builds no `class =` entries"),
    ):
        kinds.build(entries, _host())


def test_a_registry_without_a_class_factory_refuses_a_class_entry(kinds):
    entry = _entry("fw", kind=None, cls="acme.products:Firmware")
    with pytest.raises(
        ValueError, match=r"\[\[products\]\] 'fw': toy kind registry builds no `class =` entries"
    ):
        kinds.build([entry], _host())


# ── declared_for_host: project collection from the catalog + the §5 targeting gate

from otto.declared import (
    clear_composed,
    declared_for_host,
    install_initiator,
    publish_composed,
    scope_for_project,
    surviving_projects,
)


def _project(name="project1", scope=None, products=(), dev_tools=()):
    return fake_project(
        name,
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
def projects_table():
    """The projects a finished composition published; ``declared_for_host`` reads them there.

    One list is both ``projects`` and ``ordered_projects``, so every project a test adds
    survives the dependency pass. The root conftest withdraws the publication
    after the test.
    """
    table: list = []
    publish_composed(_composed(table, table), complete=True)
    return table


def _composed(projects, ordered):
    """A stand-in for a composition result: the two fields the catalog's readers take."""
    return SimpleNamespace(projects=projects, ordered_projects=ordered)


def test_declared_for_host_collects_only_targeting_projects(projects_table):
    e1, e2, e3 = _entry("a"), _entry("b"), _entry("c")
    projects_table.extend(
        [
            _project("in-scope", scope=_scope(["bench"]), products=[e1]),
            _project("other-lab", scope=_scope(["floor"]), products=[e2]),
            _project("undeclared", scope=None, products=[e3]),  # no [project] admits everything
        ]
    )
    host = _host(id="h1", source_lab="bench")
    assert declared_for_host(host, "declared_products") == [e1, e3]


def test_declared_for_host_skips_dependency_skipped_projects():
    """A project the dependency pass dropped contributes no entries (Chris,
    2026-09-02): its init never ran, so applying its declared half while its
    provider half stays silent would split the two seams' view of the project.
    Precedence among survivors stays the published ``projects`` (discovery)
    order — the surviving list here is deliberately REVERSED to prove the
    filter never reorders."""

    e1, e2, e3 = _entry("a"), _entry("b"), _entry("c")
    r1 = _project("first", products=[e1])
    r2 = _project("dep-skipped", products=[e2])
    r3 = _project("last", products=[e3])
    publish_composed(_composed([r1, r2, r3], [r3, r1]), complete=True)
    assert declared_for_host(_host(), "declared_products") == [e1, e3]


def test_declared_for_host_unstamped_host_is_not_judged(projects_table):
    # The provider loops' carve-out, identically: source_lab == "" means a host
    # built outside the loader, which predates scoping and admits everything.
    e = _entry("a")
    projects_table.append(_project(scope=_scope(["bench"]), products=[e]))
    assert declared_for_host(_host(source_lab=""), "declared_products") == [e]


def test_declared_for_host_selects_the_named_seam(projects_table):
    p, t = _entry("p"), _entry("t", seam="dev_tools")
    projects_table.append(_project(products=[p], dev_tools=[t]))
    host = _host()
    assert declared_for_host(host, "declared_products") == [p]
    assert declared_for_host(host, "declared_dev_tools") == [t]


def test_declared_for_host_reads_the_result_a_running_composition_published():
    """An init module that builds a host runs while its composition is still in progress.

    The root publishes the in-progress result before the init modules run, and
    its ``projects`` and ``ordered_projects`` are final by then: a reader that took
    only the complete result would drop the entries of every host built in
    that window while its providers still applied.
    """
    e = _entry("a")
    clear_composed()
    publish_composed(_composed([_project(products=[e])], [_project(products=[e])]), complete=False)
    assert declared_for_host(_host(), "declared_products") == [e]


def test_the_complete_result_replaces_the_in_progress_one():
    early, final = _entry("early"), _entry("final")
    clear_composed()
    publish_composed(_composed([_project(products=[early])], [_project()]), complete=False)
    publish_composed(_composed([_project(products=[final])], [_project()]), complete=True)
    assert declared_for_host(_host(), "declared_products") == [final]
    assert declared_mod._composition.in_progress is None


def test_declared_for_host_with_nothing_published_yields_nothing(caplog):
    # Unlike scope_for_project's admit-on-failure, "nothing composed" here means the
    # entries themselves do not exist yet: the empty answer IS the true answer.
    clear_composed()
    with caplog.at_level(logging.DEBUG, logger="otto.declared"):
        assert declared_for_host(_host(), "declared_products") == []
    assert "nothing composed yet" in caplog.text


def test_declared_for_host_never_starts_composition(monkeypatch):
    """Entry collection reads, never composes: an uncomposed process (bare
    library, ``otto init``, a unit test constructing a host directly) must not
    pay discovery's cost or run project init imports just to ask what entries
    exist yet — there are none until something else composes.
    """
    called: list[bool] = []

    def _initiator():
        called.append(True)
        raise AssertionError("composition started by declared_for_host")

    clear_composed()
    monkeypatch.setattr(declared_mod._composition, "initiator", _initiator)
    assert declared_for_host(_host(), "declared_products") == []
    assert surviving_projects() == []
    assert not called, "declared_for_host must never call the initiator"


def test_clear_composed_withdraws_both_results_and_keeps_the_initiator(monkeypatch):
    def _initiator():
        raise AssertionError("not called here")

    monkeypatch.setattr(declared_mod._composition, "initiator", _initiator)
    publish_composed(_composed([_project()], [_project()]), complete=False)
    publish_composed(_composed([_project()], [_project()]), complete=True)
    clear_composed()
    assert declared_mod._composition.result is None
    assert declared_mod._composition.in_progress is None
    assert declared_mod._composition.initiator is _initiator


def test_install_initiator_fills_the_slot(monkeypatch):
    def _initiator():
        raise AssertionError("installing must not run it")

    monkeypatch.setattr(declared_mod._composition, "initiator", None)
    install_initiator(_initiator)
    assert declared_mod._composition.initiator is _initiator


# ── scope_for_project: the NAME -> declaration hop the ingest gate needs ─────────
#
# Provider registrations carry the registering project's name; project_targets judges
# a compiled config. The projects come from the catalog: the complete published
# result, else the initiator's answer. Every "cannot answer" answers None,
# which project_targets reads as ADMITS, because the seam that asks runs in bare
# library use and in every factory unit test, where there is no composition
# to ask at all.


def _initiating(monkeypatch, *results):
    """Install an initiator that answers *results* in turn; return the list of its calls."""
    calls: list[int] = []
    answers = iter(results)

    def _initiator():
        calls.append(1)
        answer = next(answers)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(declared_mod._composition, "initiator", _initiator)
    return calls


def test_scope_for_project_returns_the_named_projects_declaration(monkeypatch):
    wanted = _project("sensors", scope=_scope(["bench"], hosts=["sensor-.*"]))
    other = _project("gateways", scope=_scope(["bench"], hosts=["gw-.*"]))
    # `other` first, so a lookup that returned the FIRST declaration it saw
    # rather than the matching one answers with the wrong project's fleet.
    publish_composed(_composed([other, wanted], [other, wanted]), complete=True)
    _initiating(monkeypatch)  # an initiator that would fail if it were asked

    assert scope_for_project("sensors") is wanted.project_scope


def test_scope_for_project_asks_the_initiator_when_nothing_is_complete(monkeypatch):
    """No complete result: the initiator composes (or, inside a running
    composition, answers with the in-progress result) and its projects decide."""
    wanted = _project("sensors", scope=_scope(["bench"]))
    clear_composed()
    calls = _initiating(monkeypatch, _composed([wanted], [wanted]))

    assert scope_for_project("sensors") is wanted.project_scope
    assert calls == [1]


def test_scope_for_project_reads_a_complete_result_before_the_initiator(monkeypatch):
    wanted = _project("sensors", scope=_scope(["bench"]))
    publish_composed(_composed([wanted], [wanted]), complete=True)
    calls = _initiating(monkeypatch, AssertionError("the initiator was asked"))

    assert scope_for_project("sensors") is wanted.project_scope
    assert calls == []


def test_scope_for_project_admits_when_no_root_is_loaded(no_composition_root):
    """Decision D2 (spec 2026-10-11 placement-and-phases §3.5): with no
    initiator installed, ``otto.bootstrap`` was never imported, and the gate
    admits rather than start composition from below."""
    clear_composed()
    assert declared_mod._composition.initiator is None
    assert scope_for_project("sensors") is None


def test_scope_for_project_admits_in_a_process_that_never_loaded_the_root():
    """The cold path the in-process test can only imitate: a fresh interpreter
    that imports ``otto.declared`` alone answers ``None`` and loads no root."""
    import subprocess

    probe = (
        "import sys, otto.declared as d; "
        "print(d.scope_for_project('sensors') is None, 'otto.bootstrap' in sys.modules)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split() == ["True", "False"]


def test_scope_for_project_admits_an_owner_no_project_claims(monkeypatch):
    # An owner otto cannot place is NOT the unknown-owner refusal scoped_ids
    # raises. There the caller named a project to be bounded BY; here a provider
    # arrived carrying a name this process cannot resolve (a stale marker, a
    # project bootstrap skipped), and refusing ingest over it would take a working
    # lab offline. The gate is a narrowing, so its failure mode is to narrow
    # nothing.
    known = _project("sensors", scope=_scope(["bench"], hosts=["sensor-.*"]))
    publish_composed(_composed([known], [known]), complete=True)

    assert scope_for_project("ghost") is None


def test_scope_for_project_of_an_undeclared_project_is_none():
    # A known project with no `[project]` block: None again, and for the same
    # reason project_targets reads None as everything — it scoped nothing out.
    plain = _project("toolsproject", scope=None)
    publish_composed(_composed([plain], [plain]), complete=True)

    assert scope_for_project("toolsproject") is None


def test_scope_for_project_never_composes_for_an_unowned_registration(monkeypatch):
    # Kills: looking the owner up before testing it for None. The initiator
    # composes, so a needless call here would drag a full composition root
    # into every library import that registers a provider outside a project.
    clear_composed()
    calls = _initiating(monkeypatch, AssertionError("composed for an unowned registration"))

    assert scope_for_project(None) is None
    assert calls == [], "a registration made outside any project has nothing to look up"


def test_scope_for_project_admits_when_composition_fails(monkeypatch):
    # The hostile condition is INJECTED, not inherited: an initiator that
    # raises is what a broken project or an unreadable environment looks like from
    # here, and a lookup that let it propagate would turn every such use into
    # an ingest crash.
    clear_composed()
    calls = _initiating(monkeypatch, RuntimeError("composition failed"))

    assert scope_for_project("sensors") is None
    assert calls == [1]


def test_scope_for_project_admits_when_composition_answers_with_no_projects(monkeypatch):
    # The quieter half of "cannot be reached": a composition that came back
    # with nothing to iterate rather than one that blew up. Same verdict, and
    # it must not become a TypeError on the way there — the lookup has to be
    # TOTAL, or the carve-out it exists to provide is conditional on config's
    # mood.
    clear_composed()
    _initiating(monkeypatch, _composed(None, None), _composed([], []))
    assert scope_for_project("sensors") is None
    assert scope_for_project("sensors") is None


def test_project_parse_settings_builds_declared_entries(tmp_path):
    from otto.config.project import Project
    from tests._fixtures.project_scaffold import make_project

    sut = make_project(
        tmp_path / "project",
        name="declproject",
        extra="""
[[products]]
name = "fw"
kind = "shell"
artifact = "build/fw.bin"
match = { id = "bb.*" }

[[dev_tools]]
name = "probe"
kind = "shell"
artifact = "tools/probe.sh"
""",
    )
    project = Project(root=sut)
    (fw,) = project.declared_products
    assert (fw.name, fw.kind, fw.seam, fw.owner) == ("fw", "shell", "products", "declproject")
    assert fw.base_dir == sut
    assert fw.params == {"artifact": "build/fw.bin"}  # raw: anchoring is the kind's job
    (probe,) = project.declared_dev_tools
    assert (probe.seam, probe.owner) == ("dev_tools", "declproject")
