"""``otto.session.build_lab`` — the lab the CLI builds, as a library call."""

import json
from pathlib import Path

import pytest

from otto.config.project import Project
from tests._fixtures.fake_project import fake_project
from tests._fixtures.labdata import write_lab_json
from tests._fixtures.project_scaffold import make_project

_SOURCES = '[[lab.sources]]\nbackend = "json"\npaths = ["lab"]\n'


def _host(element: str, ip: str) -> dict:
    return {
        "ip": ip,
        "element": element,
        "creds": [{"login": "u", "password": "p"}],
        "resources": [element],
        "labs": ["merged"],
    }


def _project(root: Path, name: str, sources_toml: str, labfiles: dict[str, list[dict]]) -> Project:
    sut = make_project(root, name=name, extra=sources_toml)
    for rel, hosts in labfiles.items():
        write_lab_json(root / rel, hosts)
    return Project(root=sut)


# `OTTO_HOME` is relocated in the inventory tests below: `build_inventory` reads
# `~/.otto/settings.toml` on EVERY call, even when a project declares its own
# table. The root autouse home is private per worker but shared across the
# tests on that worker, so a settings file another test leaves there could
# otherwise decide these.
# The broken-declaration test is anchored on the "inventory unavailable" banner:
# an unanchored match would also be satisfied by the "no inventory is
# configured" message the LOADER raises for a referenced entry, which is a
# different failure at a different layer.

_REFERENCED = {
    "inventory": "dut-1",
    "element": "dut",
    "creds": [{"login": "u", "password": "p"}],
    "labs": ["merged"],
    "resources": ["dut"],
}


def _inventory_project(root: Path, table: str) -> Project:
    """A project whose one lab entry is REFERENCED, plus the given ``[inventory]`` table."""
    project = _project(root, "r1", f"{_SOURCES}\n{table}", {"lab/lab.json": [_REFERENCED]})
    (root / "inv.json").write_text(json.dumps({"dut-1": {"ip": "10.0.0.7"}}))
    return project


def test_build_lab_resolves_referenced_entries(tmp_path, monkeypatch) -> None:
    """The inventory is built and handed to the load: a referenced entry has no address."""
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    from otto.session import build_lab

    project = _inventory_project(
        tmp_path / "r1", '[inventory]\nbackend = "json"\npath = "inv.json"\nsupplies = ["ip"]\n'
    )
    lab = build_lab([project], labs=["merged"])
    assert lab.hosts["dut"].ip == "10.0.0.7"
    assert lab.hosts["dut"].inventory_ref.key == "dut-1"


def test_a_broken_inventory_declaration_is_an_inventory_refusal(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    from otto.session import LabBuildError, build_lab

    project = _inventory_project(tmp_path / "r1", '[inventory]\nbackend = "no-such-backend"\n')
    with pytest.raises(LabBuildError) as exc:
        build_lab([project], labs=["merged"])
    assert exc.value.kind == "inventory"
    assert "inventory unavailable" in str(exc.value)


def test_no_labs_is_refused_before_any_project_is_read():
    from otto.session import LabBuildError, build_lab

    with pytest.raises(LabBuildError) as exc:
        build_lab(None, labs=[])  # ty: ignore[invalid-argument-type] — must not be touched
    assert (exc.value.kind, exc.value.field) == ("no_labs", "labs")


def test_an_unknown_lab_keeps_the_sources_own_text(tmp_path):
    from otto.labs import LabNotFoundError
    from otto.session import LabBuildError, build_lab

    project = _project(
        tmp_path / "r1", "r1", _SOURCES, {"lab/lab.json": [_host("alt1", "10.0.0.1")]}
    )
    with pytest.raises(LabBuildError) as exc:
        build_lab([project], labs=["nope"])
    assert (exc.value.kind, exc.value.field) == ("unknown_lab", "labs")
    assert isinstance(exc.value.__cause__, LabNotFoundError)
    assert str(exc.value) == str(exc.value.__cause__) == exc.value.detail


def test_an_unknown_backend_is_a_sources_refusal(tmp_path):
    from otto.session import LabBuildError, build_lab

    project = _project(tmp_path / "r1", "r1", '[[lab.sources]]\nbackend = "nope"\n', {})
    with pytest.raises(LabBuildError) as exc:
        build_lab([project], labs=["merged"])
    assert (exc.value.kind, exc.value.field) == ("sources", None)
    assert str(exc.value).startswith("host source unavailable: ")


def test_preferences_merge_lists_atomically_and_tables_per_key():
    from otto.session import merge_host_preferences

    a = fake_project(host_preferences={"*": {"transfer": ["scp"], "ssh": {"port": 22, "x": 1}}})
    b = fake_project(host_preferences={"*": {"transfer": ["nc"], "ssh": {"port": 2222}}})
    assert merge_host_preferences(projects=[a, b]) == {
        "*": {"transfer": ["nc"], "ssh": {"port": 2222, "x": 1}}
    }


def test_a_later_projects_source_overrides_an_earlier_ones_host(tmp_path):
    from otto.session import build_lab

    r1 = _project(tmp_path / "r1", "r1", _SOURCES, {"lab/lab.json": [_host("alt1", "10.0.0.1")]})
    r2 = _project(tmp_path / "r2", "r2", _SOURCES, {"lab/lab.json": [_host("alt1", "10.0.0.2")]})
    lab = build_lab([r1, r2], labs=["merged"])
    assert lab.hosts["alt1"].ip == "10.0.0.2"


def test_declared_containers_are_registered(tmp_path, monkeypatch):
    from otto.session import build_lab

    calls = []
    monkeypatch.setattr(
        "otto.docker.compose.register_declared_container_hosts",
        lambda lab, projects, **kw: calls.append((lab, list(projects), kw)) or 0,
    )
    project = _project(
        tmp_path / "r1", "r1", _SOURCES, {"lab/lab.json": [_host("alt1", "10.0.0.1")]}
    )
    lab = build_lab([project], labs=["merged"])
    assert calls == [(lab, [project], {"selection": None, "unscoped": False})]


def test_the_merged_host_preferences_reach_the_load(tmp_path):
    from otto.session import build_lab

    prefs = '[host_preferences.".*".ssh_options]\nport = 2222\n'
    project = _project(
        tmp_path / "r1", "r1", _SOURCES + prefs, {"lab/lab.json": [_host("alt1", "10.0.0.1")]}
    )
    lab = build_lab([project], labs=["merged"])
    assert lab.hosts["alt1"].ssh_options.port == 2222


def test_build_lab_and_view_reports_an_unknown_lab_through_the_load_step(tmp_path, monkeypatch):
    from otto.session import LabBuildError, build_lab_and_view
    from otto.session import lab as session_lab

    raised: "list[BaseException]" = []
    load = session_lab.load_lab_and_view

    def counted(*args, **kwargs):
        try:
            return load(*args, **kwargs)
        except LabBuildError as e:
            raised.append(e)
            raise

    monkeypatch.setattr(session_lab, "load_lab_and_view", counted)
    project = _project(
        tmp_path / "r1", "r1", _SOURCES, {"lab/lab.json": [_host("alt1", "10.0.0.1")]}
    )
    with pytest.raises(LabBuildError) as exc:
        build_lab_and_view([project], labs=["nope"])
    assert exc.value.kind == "unknown_lab"
    assert raised == [exc.value]


# ── declared-container placement (spec 2026-11-24 4.9, Q8, Q27) ──────────────

_COMPOSE = '[[docker.composes]]\nname = "core"\npath = "docker/compose.yml"\nservices = ["api"]\n'


def _scope(labs: "list[str]", hosts: "list[str]") -> str:
    """A ``[project]`` table declaring *labs* and *hosts*."""
    quoted = [", ".join(f"'{item}'" for item in items) for items in (labs, hosts)]
    return f"[project]\nlab_patterns = [{quoted[0]}]\nhost_patterns = [{quoted[1]}]\n"


def _docker_host(element: str, ip: str, *, priority: int = 0) -> dict:
    return {**_host(element, ip), "docker_capable": True, "docker_priority": priority}


def _docker_project(
    root: Path,
    name: str,
    *,
    scope: str,
    sources: str = "",
    labfiles: "dict[str, list[dict]] | None" = None,
    use_case: "str | None" = None,
) -> Project:
    """A real project declaring *scope* and one compose (service ``api``), plus a use-case."""
    extra = f"{sources}\n{scope}\n{_COMPOSE}"
    if use_case is not None:
        extra += f'\n[[docker.use_cases]]\nname = "{use_case}"\ncomposes = ["core"]\n'
    sut = make_project(root, name=name, extra=extra, files={"docker/compose.yml": "services: {}\n"})
    for rel, hosts in (labfiles or {}).items():
        write_lab_json(root / rel, hosts)
    return Project(root=sut)


def _containers(lab) -> "list[str]":
    from otto.host.docker_host import DockerContainerHost

    return [hid for hid, host in lab.hosts.items() if isinstance(host, DockerContainerHost)]


def test_default_build_lab_places_without_switches(tmp_path):
    """With no selection, a project whose lab_patterns miss the built lab gets no placeholder."""
    from otto.session import build_lab

    here = _docker_project(
        tmp_path / "here",
        "here",
        scope=_scope(["merged"], [".*"]),
        sources=_SOURCES,
        labfiles={"lab/lab.json": [_docker_host("alt1", "10.0.0.1")]},
    )
    away = _docker_project(tmp_path / "away", "away", scope=_scope(["elsewhere"], [".*"]))

    lab = build_lab([here, away], labs=["merged"])
    assert _containers(lab) == ["alt1.here.api"]


def test_docker_free_build_resolves_no_scopes(tmp_path, monkeypatch):
    """Regression: a build no project declares docker for resolves no scope, switches or not.

    It fails before this change only because ``selection=`` is new; the old
    build never resolved a scope here either.
    """
    from otto.host import builtin_hosts
    from otto.session import ScopeSelection, build_lab

    calls = []
    monkeypatch.setattr(builtin_hosts, "lab_scopes", lambda *a, **kw: calls.append(a) or {})
    project = _project(
        tmp_path / "r1", "r1", _SOURCES, {"lab/lab.json": [_docker_host("alt1", "10.0.0.1")]}
    )
    selection = ScopeSelection(labs=("merged",), include=(), exclude=("r1",))
    lab = build_lab([project], labs=["merged"], selection=selection)
    assert "alt1" in lab.hosts
    assert calls == []


@pytest.mark.asyncio
async def test_library_construction_with_switches(tmp_path, monkeypatch):
    """``build_lab(selection=...)`` and ``open_context(exclude_projects=...)`` match the CLI.

    ``keep`` targets ``alt1`` and ``drop`` targets ``alt2``, which ranks
    higher, so the whole lab's default parent is ``alt2``. Under ``-E drop``
    the placeholder, the context's default parent and the CLI's
    ``docker use-cases`` listing all name ``alt1``.
    """
    from typer.testing import CliRunner

    import otto
    from otto.cli.main import app
    from otto.docker.observe import default_docker_parent
    from otto.session import ScopeSelection, build_lab
    from tests._fixtures.bootstrap_seam import install_composed, patch_bootstrap

    hosts = [_docker_host("alt1", "10.0.0.1"), _docker_host("alt2", "10.0.0.2", priority=10)]
    keep = _docker_project(
        tmp_path / "keep",
        "keep",
        scope=_scope(["merged"], ["alt1"]),
        sources=_SOURCES,
        labfiles={"lab/lab.json": hosts},
        use_case="keepuc",
    )
    drop = _docker_project(
        tmp_path / "drop", "drop", scope=_scope(["merged"], ["alt2"]), use_case="dropuc"
    )
    install_composed(monkeypatch, patch_bootstrap(monkeypatch, [keep, drop]))

    selection = ScopeSelection(labs=("merged",), include=(), exclude=("drop",))
    lab = build_lab([keep, drop], labs=["merged"], selection=selection)
    assert _containers(lab) == ["alt1.keepuc.api"]
    async with otto.open_context(lab=lab, exclude_projects=["drop"]) as ctx:
        assert default_docker_parent(lab=ctx.lab).id == "alt1"

    result = CliRunner().invoke(
        app, ["--lab", "merged", "-E", "drop", "docker", "use-cases"], env={"COLUMNS": "200"}
    )
    assert result.exit_code == 0, result.output
    assert "dropuc" not in result.output
    table = result.output.split("use-case keepuc", 1)[1]  # after the scope's log line
    assert "alt1" in table
    assert "alt2" not in table
