"""One view and one status report through ``--lab`` and ``--list-labs`` (lab templating, #1176).

Dispatch builds one inventory view and one status report over every project's
sources, so ``--lab`` and ``--list-labs`` agree about which labs can be
loaded. Listing builds each source in its own project's profiles and defers
the view, so a configuration that never names a broken inventory lists as
before; when the shared listing cannot be built, each project's panel falls
back to today's own listing.
"""

import os
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from typer.testing import CliRunner

from otto.config.env import is_otto_env_var
from otto.labs import Diagnostic, LabRepositoryError, LabScope, LabStatus
from otto.labs.json_repository import _JsonFileLabRepository
from otto.labs.loader import Lab, load_lab
from otto.labs.sources import PendingLabSource
from otto.models.base import OttoModel
from otto.registry import FrozenMap
from tests._fixtures.bootstrap_seam import fake_bootstrap_result
from tests._fixtures.fake_project import fake_project
from tests._fixtures.labdata import lab_data_dir, write_lab_json
from tests._fixtures.named_inventory import FakeInventory, FakeViewSource
from tests.unit.labs.test_unchanged_loading import (
    _COLUMNS,
    LISTING_GOLDEN,
    _entered,
    _env,
    _json_project,
    _listing_cases,
    _register_host_extensions,
    _Setup,
)

_BACKEND = "lab-view-fake"
_CREDS = [{"login": "u", "password": "p"}]
_REASON = "bench is being recabled"


class _Which(OttoModel):
    """The fake backend's one option: which test-held source a declaration builds."""

    which: str


@pytest.fixture
def sources() -> dict[str, Any]:
    """Register the fake backend; a declaration's ``which`` picks the source from this dict.

    A value that is an exception is raised by the factory instead. The root
    conftest's registry isolation unregisters the backend after the test.
    """
    from otto.labs import register_lab_repository

    held: dict[str, Any] = {}

    def factory(configured: Any) -> Any:
        source = held[configured.config.which]
        if isinstance(source, Exception):
            raise source
        return source

    register_lab_repository(_BACKEND, config=_Which, factory=factory)
    return held


def _project(name: str, root: Path) -> Any:
    """A project whose one ``[[lab.sources]]`` entry is the fake backend's source *name*."""
    pending = PendingLabSource(
        backend=_BACKEND,
        label=f"{name}/fake",
        raw=FrozenMap.freeze_json(value={"which": name}),
        origin=str(root / ".otto" / "settings.toml"),
        project_root=root,
    )
    return fake_project(name, root=root, lab_sources=[pending])


def _el(name: str, ip: str) -> dict[str, Any]:
    return {"name": name, "hosts": [{"ip": ip, "creds": _CREDS}]}


def _clean_env(tmp_path: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not is_otto_env_var(k)}
    env["OTTO_XDIR"] = str(tmp_path / "xdir")
    env["COLUMNS"] = "200"
    return env


def _invoke(tmp_path: Path, projects: list[Any], args: list[str]) -> Any:
    """``otto <args>`` through the console-script ``entry()``, *projects* the bootstrap's.

    Through ``entry()``, so a refusal that the boundary frame prints (an
    unknown or unavailable lab) reaches the output as the user sees it.
    """
    import typer

    from otto import bootstrap as bs
    from otto.cli.main import entry

    wrapper = typer.Typer()

    @wrapper.command()
    def otto_entry() -> None:
        entry()

    bs._reset()
    try:
        with (
            mock.patch.dict(os.environ, _clean_env(tmp_path), clear=True),
            mock.patch("sys.argv", ["otto", *args]),
            mock.patch("otto.declared.get_projects", return_value=projects),
            mock.patch("otto.bootstrap.bootstrap", return_value=fake_bootstrap_result(projects)),
            mock.patch("rich._console", None),
        ):
            return CliRunner().invoke(wrapper, [])
    finally:
        bs._reset()


def _flat(output: str) -> str:
    """*output* with rich's panel borders and wrapping removed, for substring checks."""
    return " ".join(output.replace("│", " ").replace("╭", " ").replace("╰", " ").split())


def _panel(project: Any, projects: list[Any]) -> str:
    """The text of *project*'s panel as ``--list-labs`` over *projects* renders it."""
    from otto.inventory.config import deferred_inventory_view
    from otto.labs.sources import listing_sources

    report = listing_sources(projects=projects).lab_status_report(
        view=deferred_inventory_view(projects=projects)
    )
    return _render(project.get_lab_panel(report=report))


def _render(panel: Any) -> str:
    import io

    from rich.console import Console

    buf = io.StringIO()
    Console(file=buf, width=300, highlight=False).print(panel)
    return _flat(buf.getvalue())


# --------------------------------------------------------------------------- #
# One report, the later declaration deciding, for --lab and --list-labs alike.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("order", ["a-first", "b-first"])
def test_the_only_host_in_a_and_the_declaration_in_b(tmp_path, sources, order):
    """A supplies bench's only host and declares it unavailable; B redeclares it available."""
    sources["a"] = FakeViewSource(
        {"bench": [_el("dut", "10.0.0.1")]},
        statuses=[
            LabStatus(name="bench", unavailable=Diagnostic(where="a's file", reason=_REASON))
        ],
    )
    sources["b"] = FakeViewSource({"bench": []})
    a, b = _project("a", tmp_path / "a"), _project("b", tmp_path / "b")
    projects = [a, b] if order == "a-first" else [b, a]

    dispatched = _invoke(tmp_path, projects, ["--lab", "bench", "--show-lab"])
    listed = _flat(_invoke(tmp_path, projects, ["--list-labs"]).output)
    rows = [_panel(p, projects) for p in (a, b)]

    if order == "a-first":  # B declares last: bench is available, with A's host
        assert dispatched.exit_code == 0, dispatched.output
        assert "dut" in dispatched.output
        assert "unavailable" not in listed
        assert all("• bench" in r and "unavailable" not in r for r in rows)
    else:  # A declares last: bench is unavailable, whoever supplies its hosts
        assert dispatched.exit_code == 1, dispatched.output
        assert f"error: Lab 'bench' is unavailable: {_REASON} (a's file)" in _flat(
            dispatched.output
        )
        assert "Lab unavailable:" not in dispatched.output
        assert listed.count(f"• bench — unavailable: {_REASON}") == 2
        assert all(f"• bench — unavailable: {_REASON}" in r for r in rows)


def test_lab_a_plus_bench_names_bench_and_evaluates_status_once(tmp_path, sources):
    sources["a"] = FakeViewSource({"a": [_el("alt1", "10.0.0.1")]})
    sources["b"] = FakeViewSource(
        {"bench": [_el("dut", "10.0.0.2")]},
        statuses=[
            LabStatus(name="bench", unavailable=Diagnostic(where="b's file", reason=_REASON))
        ],
    )
    projects = [_project("a", tmp_path / "a"), _project("b", tmp_path / "b")]

    result = _invoke(tmp_path, projects, ["--lab", "a+bench", "--show-lab"])

    assert result.exit_code == 1, result.output
    flat = _flat(result.output)
    assert "Lab 'bench' is unavailable" in flat
    assert _REASON in flat
    # One report for both components: each source is asked for its statuses once.
    assert [sources["a"].calls["status"], sources["b"].calls["status"]] == [1, 1]


def test_a_load_only_failure_lists_the_lab_and_fails_the_load(tmp_path, sources):
    sources["a"] = FakeViewSource(
        {"bench": [_el("dut", "10.0.0.1")]},
        scope=LabScope(names=frozenset({"bench"})),
        fail=LabRepositoryError("db down"),
        fail_on="load",
    )
    projects = [_project("a", tmp_path / "a")]

    listed = _invoke(tmp_path, projects, ["--list-labs"])
    dispatched = _invoke(tmp_path, projects, ["--lab", "bench", "--show-lab"])

    assert listed.exit_code == 0, listed.output
    assert "• bench" in _flat(listed.output)
    assert "unavailable" not in listed.output
    assert dispatched.exit_code == 1, dispatched.output
    assert "error: Lab 'bench' is unavailable: source a/fake failed: db down" in _flat(
        dispatched.output
    )


# --------------------------------------------------------------------------- #
# Listing: each source in its own profiles, a deferred view, today's fallback.
# --------------------------------------------------------------------------- #


def _golden(case: str) -> str:
    """The ``--list-labs`` chunk Task 0 recorded for *case*, header included."""
    text = LISTING_GOLDEN.read_text()
    start = text.index(f"== {case} (")
    end = text.find("\n== ", start + 1)
    return text[start : len(text) if end == -1 else end + 1]


def _listed(case: str, setup: _Setup, workdir: Path) -> str:
    """``otto --list-labs`` over *setup*, under the header Task 0's golden uses for *case*."""
    from otto.cli.main import app

    _register_host_extensions()
    with (
        _env(COLUMNS=_COLUMNS, FORCE_COLOR=None, TTY_COMPATIBLE=None),
        _entered(setup, home=workdir / "home"),
        mock.patch("otto.declared.get_projects", return_value=setup.projects),
        mock.patch("rich._console", None),
    ):
        result = CliRunner().invoke(app, ["--list-labs"])
    return f"== {case} (exit {result.exit_code}) ==\n{result.output}"


def test_list_labs_lists_each_source_in_its_own_profiles(tmp_path):
    """B declares profile ``x``; A's panel omits ``needs-x``, as Task 0 recorded it."""
    case = "profile-declared-by-another-project"
    got = _listed(case, _listing_cases(tmp_path)[case], tmp_path)
    assert got == _golden(case)
    assert "needs-x" not in got


def test_a_malformed_user_settings_file_leaves_an_inline_list_labs_unchanged(tmp_path):
    case = "malformed-user-settings"
    assert _listed(case, _listing_cases(tmp_path)[case], tmp_path) == _golden(case)


def test_a_broken_unused_inventory_leaves_list_labs_unchanged(tmp_path, monkeypatch):
    """An inventory backend whose factory explodes is never built by an inline listing."""
    from otto.inventory import register_inventory_backend

    built: list[Any] = []

    def explode(configured: Any) -> Any:
        built.append(configured)
        raise AssertionError("listing built an inventory backend")

    register_inventory_backend(
        "lab-view-explode", config=_NoOptions, factory=explode, snapshot_cache=False
    )
    tech2 = lab_data_dir() / "tech2"
    case = "unregistered-inventory-inline-lab"
    for backend in ("lab-view-explode", "lab-view-unregistered"):
        setup = _Setup([_json_project("inline", tech2, [tech2], inventory={"backend": backend})])
        assert _listed(case, setup, tmp_path / backend) == _golden(case)
    assert built == []


class _NoOptions(OttoModel):
    """An inventory backend that takes no options."""


def test_a_bad_source_factory_falls_back_to_per_project_panels(tmp_path, sources):
    sources["a"] = FakeViewSource({"alpha": [_el("alt1", "10.0.0.1")]})
    sources["b"] = RuntimeError("factory broke")
    projects = [_project("a", tmp_path / "a"), _project("b", tmp_path / "b")]

    a, b = projects
    result = _invoke(tmp_path, projects, ["--list-labs"])

    assert result.exit_code == 0, result.output
    assert "• alpha" in _flat(result.output)
    assert "factory broke" in _flat(result.output)
    # The fallback attributes the failure to B's panel alone, as each panel builds itself.
    from otto.inventory.config import deferred_inventory_view

    view = deferred_inventory_view(projects=projects)
    panel_a, panel_b = (_render(p.get_lab_panel(view=view)) for p in (a, b))
    assert "• alpha" in panel_a
    assert "⚠" not in panel_a
    assert "⚠ host source unavailable:" in panel_b
    assert "factory broke" in panel_b
    assert "alpha" not in panel_b


def test_a_bad_unscoped_status_call_falls_back(tmp_path, sources):
    sources["a"] = FakeViewSource({"alpha": [_el("alt1", "10.0.0.1")]})
    sources["b"] = FakeViewSource(
        {"beta": [_el("alt2", "10.0.0.2")]}, fail=LabRepositoryError("db down"), fail_on="status"
    )
    projects = [_project("a", tmp_path / "a"), _project("b", tmp_path / "b")]

    result = _invoke(tmp_path, projects, ["--list-labs"])

    assert result.exit_code == 0, result.output
    flat = _flat(result.output)
    assert "• alpha" in flat
    assert "⚠ host source unavailable: db down" in flat
    assert "beta" not in flat


def test_a_scoped_source_outage_is_a_row_of_its_projects_panel(tmp_path, sources):
    sources["a"] = FakeViewSource({"alpha": [_el("alt1", "10.0.0.1")]})
    sources["b"] = FakeViewSource(
        {"bench": [_el("dut", "10.0.0.2")]},
        scope=LabScope(names=frozenset({"bench"})),
        fail=LabRepositoryError("db down"),
        fail_on="both",
    )
    a, b = _project("a", tmp_path / "a"), _project("b", tmp_path / "b")

    assert _panel(a, [a, b]).count("• alpha") == 1
    assert "⚠" not in _panel(a, [a, b])
    assert "⚠ source b/fake unavailable: db down" in _panel(b, [a, b])
    # The down source still lists its lab, so the panel shows it with the reason.
    assert "• bench — unavailable: db down" in _panel(b, [a, b])
    assert "bench" not in _panel(a, [a, b])


def test_a_project_without_sources_keeps_its_row_beside_a_report(tmp_path, sources):
    sources["a"] = FakeViewSource({"alpha": [_el("alt1", "10.0.0.1")]})
    a, bare = _project("a", tmp_path / "a"), fake_project("bare", root=tmp_path / "bare")
    assert "no [[lab.sources]] declared" in _panel(bare, [a, bare])


# --------------------------------------------------------------------------- #
# load_lab: inventory= or view=, a report for a composite, today's route otherwise.
# --------------------------------------------------------------------------- #


def test_load_lab_refuses_inventory_and_view_together():
    from otto.inventory import InventoryView

    with pytest.raises(ValueError, match="not both"):
        load_lab(
            "a",
            repository=_Plain({"a": []}),
            inventory=FakeInventory({}),
            view=InventoryView.of_default(inventory=None),
        )


class _Plain:
    """A legacy source: ``list_labs`` and ``load_lab`` only, recording what each load got."""

    def __init__(self, labs: dict[str, list[tuple[str, str]]]) -> None:
        self._labs = labs
        self.loads: list[tuple[str, Any]] = []

    def list_labs(self) -> list[str]:
        return sorted(self._labs)

    def load_lab(self, name: str, *, preferences: Any = None, inventory: Any = None) -> Lab:
        from otto.host.element import Element
        from otto.host.factory import create_host_from_dict

        del preferences
        self.loads.append((name, inventory))
        lab = Lab(name=name)
        for element, ip in self._labs[name]:
            lab.add_host(
                create_host_from_dict({"ip": ip, "creds": _CREDS}, element=Element(name=element))
            )
        return lab


def test_load_lab_a_plus_b_over_a_plain_repository():
    plain = _Plain({"a": [("alt1", "10.0.0.1")], "b": [("alt2", "10.0.0.2")]})

    lab = load_lab("a+b", repository=plain)

    assert lab.name == "a+b"
    assert lab.component_names == ["a", "b"]
    assert {h: lab.hosts[h].source_lab for h in ("alt1", "alt2")} == {"alt1": "a", "alt2": "b"}
    assert {h: lab.hosts[h].lab_info.name for h in ("alt1", "alt2")} == {"alt1": "a", "alt2": "b"}
    assert plain.loads == [("a", None), ("b", None)]


def test_load_lab_with_inventory_only_builds_the_default_view(tmp_path):
    """``inventory=`` alone is ``default``: every route resolves through it, as today."""
    import otto

    write_lab_json(
        tmp_path / "lab.json",
        [{"inventory": "k", "element": "dut", "creds": _CREDS, "labs": ["unix"]}],
    )
    inventory = FakeInventory({"k": {"ip": "10.0.0.7"}})

    for loaded in (
        otto.load_lab("unix", inventory=inventory, search_paths=[tmp_path]),
        load_lab("unix", inventory=inventory, repository=_JsonFileLabRepository([tmp_path])),
    ):
        assert loaded.hosts["dut"].ip == "10.0.0.7"
        assert loaded.hosts["dut"].source_lab == "unix"
        assert loaded.hosts["dut"].lab_info.name == "unix"

    plain = _Plain({"unix": [("dut", "10.0.0.8")]})
    loaded = load_lab("unix", inventory=inventory, repository=plain)
    assert plain.loads == [("unix", inventory)]
    assert loaded.hosts["dut"].source_lab == "unix"
    assert loaded.hosts["local"].source_lab == "unix"
