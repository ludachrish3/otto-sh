"""The composite's status report, contribution scope and per-source policy (lab templating #1176).

A lab's availability is decided once, over every source: the last declaring
source's status wins wholesale, a scoped source that is down fails exactly
the labs inside its scope (declared or not, and no later declaration clears
it), and an unscoped failure fails every lab, as it always has. Each source is
asked through the view as its own project sees it.
"""

from pathlib import Path

import pytest

from otto.examples.lab_repository import ExampleLabRepository
from otto.host.inventory_ref import InventoryRef
from otto.inventory import BareReferenceRefused, InventoryError, InventoryView, RefusedDefault
from otto.labs import (
    CompositeLabRepository,
    Diagnostic,
    LabNotFoundError,
    LabRepositoryError,
    LabScope,
    LabSource,
    LabStatus,
    LabStatusReport,
    LabUnavailableError,
    LoginSummary,
    SourceRef,
    SourceUnavailable,
)
from otto.labs.drops import HostDrop, collecting_drops
from otto.labs.loader import Lab
from otto.testing import assert_lab_repository_conforms
from tests._fixtures.named_inventory import FakeInventory, FakeViewSource

_CREDS = [{"login": "u", "password": "p"}]
_BENCH_SCOPE = LabScope(names=frozenset({"bench"}), prefixes=frozenset({"router-lab."}))
_ROOT_A = Path("/projects/a")
_ROOT_B = Path("/projects/b")
_ORIGIN_A = "/projects/a/.otto/settings.toml"


def _el(name: str, ip: str) -> dict:
    """One element dict with one inline host."""
    return {"name": name, "hosts": [{"ip": ip, "creds": _CREDS}]}


def _down(**kw) -> FakeViewSource:
    """A source that is down: every status, load and summary call raises."""
    return FakeViewSource(
        {"router-lab.east": [_el("dut", "10.1.0.1")]},
        fail=LabRepositoryError("db down"),
        fail_on="both",
        **kw,
    )


def _composite(*sources: "tuple[str, object]") -> CompositeLabRepository:
    return CompositeLabRepository([LabSource(label, backend) for label, backend in sources])


_VIEW = InventoryView.of_default(inventory=None)


def test_a_failed_unscoped_source_fails_every_lab() -> None:
    comp = _composite(
        ("r/ok", FakeViewSource({"site": [_el("alt1", "10.0.0.1")]})), ("r/down", _down())
    )
    with pytest.raises(LabRepositoryError, match="db down") as info:
        comp.load_lab_in_view("site", view=_VIEW)
    assert not isinstance(info.value, LabUnavailableError)
    with pytest.raises(LabRepositoryError, match="db down"):
        comp.lab_status_report(view=_VIEW)
    with pytest.raises(LabRepositoryError, match="db down"):
        comp.load_lab("site")


def test_a_failed_scoped_source_spares_labs_outside_its_scope() -> None:
    down = _down(scope=_BENCH_SCOPE)
    comp = _composite(
        ("r/ok", FakeViewSource({"other": [_el("alt1", "10.0.0.1")]})), ("r/down", down)
    )
    lab = comp.load_lab_in_view("other", view=_VIEW)
    assert sorted(lab.hosts) == ["alt1"]
    assert down.calls["load"] == 0
    report = comp.lab_status_report(view=_VIEW)
    assert report.unavailable_sources == [
        SourceUnavailable(
            source=SourceRef(label="r/down", project_root=None),
            scope=_BENCH_SCOPE,
            diagnostic=Diagnostic(where="r/down", reason="db down"),
        )
    ]
    # The down source still lists its lab, so it is shown, unavailable.
    assert report.labs == [
        LabStatus(name="other"),
        LabStatus(name="router-lab.east", unavailable=Diagnostic(where="r/down", reason="db down")),
    ]
    assert report.refusal(name="other") is None


def test_a_failed_scoped_source_fails_bench_it_never_declared() -> None:
    comp = _composite(
        ("r/ok", FakeViewSource({"bench": [_el("alt1", "10.0.0.1")]})),
        ("r/down", _down(scope=_BENCH_SCOPE)),
    )
    with pytest.raises(LabUnavailableError) as info:
        comp.load_lab_in_view("bench", view=_VIEW)
    assert str(info.value) == "Lab 'bench' is unavailable: source r/down failed: db down"
    assert info.value.declared is False
    assert info.value.diagnostic == Diagnostic(where="r/down", reason="db down")
    report = comp.lab_status_report(view=_VIEW)
    reason = Diagnostic(where="r/down", reason="db down")
    assert report.labs == [
        LabStatus(name="bench", unavailable=reason),
        LabStatus(name="router-lab.east", unavailable=reason),
    ]
    assert report.declared_by == {
        "bench": [SourceRef(label="r/ok", project_root=None)],
        "router-lab.east": [SourceRef(label="r/down", project_root=None)],
    }


def test_an_in_scope_name_nobody_declares_is_unavailable_not_unknown() -> None:
    comp = _composite(
        ("r/ok", FakeViewSource({"other": [_el("alt1", "10.0.0.1")]})),
        ("r/down", _down(scope=_BENCH_SCOPE)),
    )
    with pytest.raises(LabUnavailableError) as info:
        comp.load_lab_in_view("router-lab.west", view=_VIEW)
    assert not isinstance(info.value, LabNotFoundError)
    assert str(info.value) == "Lab 'router-lab.west' is unavailable: source r/down failed: db down"
    # Outside every failed scope, an unknown name stays unknown.
    with pytest.raises(LabNotFoundError):
        comp.load_lab_in_view("nowhere", view=_VIEW)


def test_a_scoped_source_whose_load_fails_after_its_status() -> None:
    flaky = FakeViewSource(
        {"bench": [_el("dut", "10.1.0.1")]},
        scope=_BENCH_SCOPE,
        fail=LabRepositoryError("timed out"),
        fail_on="load",
    )
    comp = _composite(
        ("r/ok", FakeViewSource({"bench": [_el("alt1", "10.0.0.1")]})), ("r/flaky", flaky)
    )
    report = comp.lab_status_report(view=_VIEW)
    before = (list(report.labs), dict(report.declared_by), list(report.unavailable_sources))
    assert report.refusal(name="bench") is None
    with pytest.raises(LabUnavailableError) as info:
        comp.load_lab_reported("bench", view=_VIEW, report=report)
    assert str(info.value) == "Lab 'bench' is unavailable: source r/flaky failed: timed out"
    assert info.value.declared is False
    assert isinstance(info.value.__cause__, LabRepositoryError)
    assert (report.labs, report.declared_by, report.unavailable_sources) == before
    assert report.unavailable_sources == []


@pytest.mark.parametrize("order", ["outage-first", "outage-last"])
def test_a_source_outage_is_never_cleared_by_an_available_declaration(order) -> None:
    outage = ("r/down", _down(scope=_BENCH_SCOPE))
    available = ("r/ok", FakeViewSource({"bench": [_el("alt1", "10.0.0.1")]}))
    comp = _composite(*([outage, available] if order == "outage-first" else [available, outage]))
    report = comp.lab_status_report(view=_VIEW)
    assert report.status(name="bench") == LabStatus(
        name="bench", unavailable=Diagnostic(where="r/down", reason="db down")
    )
    refusal = report.refusal(name="bench")
    assert isinstance(refusal, LabUnavailableError)
    assert refusal.declared is False
    with pytest.raises(LabUnavailableError, match="source r/down failed: db down"):
        comp.load_lab_in_view("bench", view=_VIEW)


@pytest.mark.parametrize("order", ["unavailable-first", "unavailable-last"])
def test_unavailable_follows_the_last_declaration(order) -> None:
    why = Diagnostic(where="/labs/a.json: lab 'bench'", reason="rewiring")
    closed = (
        "r/closed",
        FakeViewSource(
            {"bench": [_el("dut", "10.1.0.1")]},
            statuses=[LabStatus(name="bench", unavailable=why)],
        ),
    )
    opened = ("r/open", FakeViewSource({"bench": [_el("alt1", "10.0.0.1")]}))
    sources = [closed, opened] if order == "unavailable-first" else [opened, closed]
    comp = _composite(*sources)
    report = comp.lab_status_report(view=_VIEW)
    assert report.declared_by == {
        "bench": [SourceRef(label=s[0], project_root=None) for s in sources]
    }
    assert comp.list_lab_status(view=_VIEW) == report.labs
    if order == "unavailable-first":
        assert report.labs == [LabStatus(name="bench")]
        lab = comp.load_lab_in_view("bench", view=_VIEW)
        assert sorted(lab.hosts) == ["alt1", "dut"]
    else:
        assert report.labs == [LabStatus(name="bench", unavailable=why)]
        with pytest.raises(LabUnavailableError) as info:
            comp.load_lab_in_view("bench", view=_VIEW)
        assert str(info.value) == (
            "Lab 'bench' is unavailable: rewiring (/labs/a.json: lab 'bench')"
        )
        assert info.value.declared is True


def test_a_status_outside_the_declared_scope_is_refused() -> None:
    stray = FakeViewSource({"bench": [], "other": []}, scope=_BENCH_SCOPE)
    comp = _composite(("r/stray", stray))
    with pytest.raises(LabRepositoryError) as info:
        comp.lab_status_report(view=_VIEW)
    assert str(info.value) == (
        "source r/stray reports lab 'other', which is outside its declared scope"
    )


def _bare(element: str, key: str) -> dict:
    return {"name": element, "hosts": [{"inventory": key}]}


def _policy_view() -> InventoryView:
    """``default`` holds ``r1`` and ``r2``; project A refuses bare references, B does not."""
    inv = FakeInventory(
        {"r1": {"ip": "10.0.0.1", "creds": _CREDS}, "r2": {"ip": "10.0.0.2", "creds": _CREDS}}
    )
    return InventoryView(inventories={"default": inv}, bare_refused_by={_ROOT_A: _ORIGIN_A})


def test_each_source_sees_its_own_projects_policy() -> None:
    a = FakeViewSource({"site": [_bare("ra", "r1")]})
    b = FakeViewSource({"site": [_bare("rb", "r2")]})
    comp = CompositeLabRepository(
        [LabSource("a/x", a, project_root=_ROOT_A), LabSource("b/x", b, project_root=_ROOT_B)]
    )
    view = _policy_view()
    with collecting_drops() as drops:
        summaries = comp.list_host_summaries_in_view(view=view)
    assert [(s.id, s.ip) for s in summaries] == [("rb", "10.0.0.2")]
    assert [d.where for d in drops] == ["inventory reference 'r1'"]  # A's refusal, once
    assert a.views["summaries"][0].project_root == _ROOT_A
    assert b.views["summaries"][0].project_root == _ROOT_B
    report = comp.lab_status_report(view=view)
    assert report.labs_declared_in(project_root=_ROOT_A) == ["site"]
    # Twice each: the summaries' listing reads the labs through the status call, then the report.
    assert [v.project_root for v in a.views["status"]] == [_ROOT_A, _ROOT_A]
    assert [v.project_root for v in b.views["status"]] == [_ROOT_B, _ROOT_B]
    with pytest.raises(BareReferenceRefused):
        comp.load_lab_in_view("site", view=view)


def test_composite_summaries_are_todays_for_a_legacy_project() -> None:
    """Literals recorded from the pre-change composite (R17): one source overrides, one fails."""
    inv = FakeInventory(
        {"r1": {"ip": "10.0.0.1", "creds": _CREDS}, "s1": {"ip": "10.0.0.2", "creds": _CREDS}}
    )
    a = ExampleLabRepository(
        labs={
            "east": [_bare("router", "r1"), _el("shared", "10.0.0.2")],
            "west": [_el("probe", "10.0.0.9"), _bare("ghost", "missing")],
        }
    )
    b = FakeViewSource({"north": [_bare("shared", "s1"), _el("edge", "10.0.0.7")]})
    broken = FakeViewSource({"south": []}, fail=LabRepositoryError("db down"), fail_on="load")
    comp = _composite(("r/a", a), ("r/broken", broken), ("r/b", b))
    login = [LoginSummary(login="u", protocols=[], proxy=False)]
    for summarize in (
        lambda: comp.list_host_summaries(inventory=inv),
        lambda: comp.list_host_summaries_in_view(view=InventoryView.of_default(inventory=inv)),
    ):
        with collecting_drops() as drops:
            out = summarize()
        assert [(s.id, s.labs, s.lab_patterns, s.ip, s.os_type, s.logins) for s in out] == [
            ("edge", ["north"], [], "10.0.0.7", "unix", login),
            ("probe", ["west"], [], "10.0.0.9", "unix", login),
            ("router", ["east"], [], "10.0.0.1", "unix", login),
            ("shared", ["east", "north"], [], "10.0.0.2", "unix", login),
        ]
        assert all((s.docker_capable, s.docker_priority) == (False, 0) for s in out)
        assert drops == [HostDrop(where="source r/broken", reason="db down")]
    with collecting_drops() as drops:
        out = comp.list_host_summaries()
    assert [(s.id, s.labs, s.ip) for s in out] == [
        ("edge", ["north"], "10.0.0.7"),
        ("probe", ["west"], "10.0.0.9"),
        ("shared", ["east"], "10.0.0.2"),
    ]
    assert drops == [HostDrop(where="source r/broken", reason="db down")]


_QUALIFIED_REF = InventoryRef(
    key="k", backend="fake:mem", extra={"rack": "b"}, name="physical", qualified=True
)


@pytest.mark.parametrize(
    ("order", "ip", "labs", "ref"),
    [
        (["qualified"], "10.0.0.5", ["west"], _QUALIFIED_REF),
        (["inline", "qualified"], "10.0.0.5", ["east", "west"], _QUALIFIED_REF),
        # Later source wins inventory_ref with the other scalars: an inline
        # restatement replaces the qualified identity, it does not keep it.
        (["qualified", "inline"], "10.0.0.4", ["west", "east"], InventoryRef()),
    ],
)
def test_the_merge_carries_inventory_ref(order, ip, labs, ref) -> None:
    physical = FakeInventory({"k": {"ip": "10.0.0.5", "creds": _CREDS, "extra": {"rack": "b"}}})
    view = InventoryView(inventories={"physical": physical})
    sources = {
        "inline": FakeViewSource({"east": [_el("r", "10.0.0.4")]}),
        "qualified": FakeViewSource(
            {"west": [{"name": "r", "hosts": [{"inventory": {"name": "physical", "key": "k"}}]}]}
        ),
    }
    (s,) = _composite(*[(f"r/{k}", sources[k]) for k in order]).list_host_summaries_in_view(
        view=view
    )
    assert (s.ip, s.labs, s.inventory_ref) == (ip, labs, ref)


@pytest.mark.parametrize(
    "error",
    [LabRepositoryError("db down"), InventoryError("netbox unreachable")],
    ids=["lab-repository", "inventory"],
)
def test_status_and_load_classify_a_scoped_failure_alike(error) -> None:
    """One rule decides a scoped source failure, whether its status call or its load fails."""

    def comp(fail_on):
        source = FakeViewSource(
            {"bench": [_el("dut", "10.1.0.1")]}, scope=_BENCH_SCOPE, fail=error, fail_on=fail_on
        )
        return _composite(("r/s", source))

    (outage,) = comp("status").lab_status_report(view=_VIEW).unavailable_sources
    with pytest.raises(LabUnavailableError) as info:
        comp("load").load_lab_in_view("bench", view=_VIEW)
    expected = Diagnostic(where="r/s", reason=str(error))
    assert outage.diagnostic == info.value.diagnostic == expected
    assert info.value.__cause__ is error


@pytest.mark.parametrize(
    ("scoped", "error"),
    [
        (False, LabRepositoryError("db down")),
        (False, InventoryError("netbox unreachable")),
        (False, RuntimeError("bug")),
        (True, RuntimeError("bug")),
    ],
    ids=["unscoped-lab-repository", "unscoped-inventory", "unscoped-other", "scoped-other"],
)
def test_status_and_load_propagate_what_is_not_a_scoped_failure(scoped, error) -> None:
    """An unscoped source's failure, and any error outside the rule, propagate from both."""
    for fail_on in ("status", "load"):
        source = FakeViewSource(
            {"bench": [_el("dut", "10.1.0.1")]},
            scope=_BENCH_SCOPE if scoped else None,
            fail=error,
            fail_on=fail_on,
        )
        with pytest.raises(type(error)) as info:
            _composite(("r/s", source)).load_lab_in_view("bench", view=_VIEW)
        assert info.value is error


def test_a_scoped_not_found_is_never_an_outage() -> None:
    """``LabNotFoundError`` is a protocol signal on both routes, never a source failure."""
    missing = LabNotFoundError("no such lab")
    status_fails = FakeViewSource({"bench": []}, scope=_BENCH_SCOPE, fail=missing)
    with pytest.raises(LabNotFoundError) as info:
        _composite(("r/s", status_fails)).lab_status_report(view=_VIEW)
    assert info.value is missing
    load_fails = FakeViewSource({"bench": []}, scope=_BENCH_SCOPE, fail=missing, fail_on="load")
    with pytest.raises(LabNotFoundError) as info:
        _composite(("r/s", load_fails)).load_lab_in_view("bench", view=_VIEW)
    assert not isinstance(info.value, LabUnavailableError)
    assert "r/s: no such lab" in str(info.value)


def test_list_labs_never_calls_a_status_method() -> None:
    class NoStatus(FakeViewSource):
        def list_lab_status(self, *, view):
            raise AssertionError("list_labs must never ask for a status")

    a = NoStatus({"east": []})
    comp = _composite(("r/a", a), ("r/b", FakeViewSource({"west": [], "east": []})))
    assert comp.list_labs() == ["east", "west"]
    assert a.calls["status"] == 0


def test_status_and_load_see_the_sources_own_policy() -> None:
    class Legacy:
        """A source with no capability: it receives what a legacy backend can take."""

        def __init__(self) -> None:
            self.inventories: list[object] = []

        def list_labs(self) -> list[str]:
            return ["site"]

        def load_lab(self, name, *, preferences=None, inventory=None) -> Lab:
            self.inventories.append(inventory)
            return ExampleLabRepository(labs={"site": [_el("old", "10.0.0.8")]}).load_lab(name)

    modern = FakeViewSource({"site": [_el("new", "10.0.0.9")]})
    legacy = Legacy()
    comp = CompositeLabRepository(
        [
            LabSource("a/modern", modern, project_root=_ROOT_A),
            LabSource("a/legacy", legacy, project_root=_ROOT_A),
        ]
    )
    lab = comp.load_lab_in_view("site", view=_policy_view())
    assert sorted(lab.hosts) == ["new", "old"]
    for seen in (modern.views["status"][0], modern.views["load"][0]):
        refused = seen.bare_inventory
        assert isinstance(refused, RefusedDefault)
        assert refused.origin == _ORIGIN_A
    (given,) = legacy.inventories
    assert isinstance(given, RefusedDefault)
    assert given.origin == _ORIGIN_A


def test_the_report_is_a_public_record() -> None:
    report = LabStatusReport(labs=[], declared_by={}, unavailable_sources=[])
    assert report.status(name="x") is None
    assert report.refusal(name="x") is None
    assert report.labs_declared_in(project_root=_ROOT_A) == []


def test_a_down_scoped_sources_listed_labs_are_unavailable_in_the_statuses() -> None:
    """Spec §6.8: names a down scoped source still lists are unavailable, with its reason."""
    down = FakeViewSource(
        {"router-lab.east": [_el("dut", "10.1.0.1")], "bench": []},
        scope=_BENCH_SCOPE,
        fail=LabRepositoryError("db down"),
        fail_on="both",
    )
    comp = _composite(
        ("r/ok", FakeViewSource({"other": [_el("alt1", "10.0.0.1")]})), ("r/down", down)
    )
    reason = Diagnostic(where="r/down", reason="db down")
    report = comp.lab_status_report(view=_VIEW)
    assert report.labs == [
        LabStatus(name="bench", unavailable=reason),
        LabStatus(name="other"),
        LabStatus(name="router-lab.east", unavailable=reason),
    ]
    assert [s.name for s in comp.list_lab_status(view=_VIEW)] == comp.list_labs()
    assert report.declared_by["router-lab.east"] == [SourceRef(label="r/down", project_root=None)]
    assert_lab_repository_conforms(comp, expected_labs=["other"])
    assert_lab_repository_conforms(comp, view=_VIEW)


def test_a_down_scoped_sources_listing_failure_leaves_its_names_unknown() -> None:
    """When the down source cannot list its labs either, nothing of it is listed or reported."""

    class Mute(FakeViewSource):
        def list_labs(self):
            raise LabRepositoryError("db down")

    comp = _composite(
        ("r/ok", FakeViewSource({"other": [_el("alt1", "10.0.0.1")]})),
        ("r/down", Mute({"bench": []}, scope=_BENCH_SCOPE, fail=LabRepositoryError("db down"))),
    )
    report = comp.lab_status_report(view=_VIEW)
    assert report.labs == [LabStatus(name="other")]
    assert len(report.unavailable_sources) == 1


def test_a_down_source_without_statuses_is_asked_for_its_labs_once() -> None:
    """Its statuses are its listing, so the failed status call is not repeated as a listing."""

    class Statusless:
        """A scoped source with no status capability, whose listing is down."""

        def __init__(self) -> None:
            self.listings = 0

        def list_labs(self) -> list[str]:
            self.listings += 1
            raise LabRepositoryError("db down")

        def load_lab(self, name, *, preferences=None, inventory=None):
            raise LabRepositoryError("db down")

        def lab_scope(self) -> LabScope:
            return _BENCH_SCOPE

    down = Statusless()
    comp = _composite(
        ("r/ok", FakeViewSource({"other": [_el("alt1", "10.0.0.1")]})), ("r/down", down)
    )
    report = comp.lab_status_report(view=_VIEW)
    assert down.listings == 1
    assert report.labs == [LabStatus(name="other")]
    assert [o.diagnostic for o in report.unavailable_sources] == [
        Diagnostic(where="r/down", reason="db down")
    ]


def test_a_projects_lab_rows_read_the_statuses_once(monkeypatch) -> None:
    """The panel indexes the report once: no per-lab scan of every status (Astra minor)."""
    from otto.config.project import _lab_rows

    ref = SourceRef(label="json:a", project_root=_ROOT_A)
    names = [f"lab-{i:04d}" for i in range(500)]
    report = LabStatusReport(
        labs=[
            LabStatus(
                name=n, unavailable=Diagnostic(where="x", reason="off") if n == "lab-0007" else None
            )
            for n in names
        ],
        declared_by={n: [ref] for n in names},
        unavailable_sources=[],
    )

    def no_scan(self, *, name):
        raise AssertionError("status(name=...) scans every lab; the rows must not call it")

    monkeypatch.setattr(LabStatusReport, "status", no_scan)
    rows = str(
        _lab_rows(
            report=report,
            project_root=_ROOT_A,
            scope=None,
            listed_labs=frozenset(),
            absence_provable=False,
        )
    ).splitlines()
    assert len(rows) == 500
    assert rows[7] == "• lab-0007 — unavailable: off"
    assert report.statuses_declared_in(project_root=_ROOT_B) == []
