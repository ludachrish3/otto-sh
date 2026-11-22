"""``load_lab(search_paths=)`` reads ``.jsonnet`` entries, under one view decision.

:func:`otto.labs.loader.paths_backend` decides the backend a bare path list
is, and :func:`otto.labs.loader.search_paths_view` the view a ``.jsonnet``
search path renders under when the caller gives none: no inventory, and the
protected-file catalog of the user settings file alone, which refuses when
that file cannot be read. The json route keeps its own default view.
"""

import json
from pathlib import Path

import pytest

from otto.config.project import Project
from otto.config.user_settings import clear_user_settings_memo
from otto.labs.errors import LabRepositoryError
from otto.labs.loader import load_lab, paths_backend
from tests.unit.templating.conftest import jsonnet_binding_under_test, otto_home  # noqa: F401

_ENTRY = """\
function(otto) {
  labs: { small: { metadata: { project: otto.project } } },
  elements: [{
    name: 'h1',
    labs: ['small'],
    hosts: [{ ip: '10.0.0.1', os_type: 'unix', creds: [{ login: 'u', password: 'p' }] }],
  }],
}
"""
"""A one-host lab whose ``small`` lab records the ``otto.project`` it rendered with."""

_SECRET = "s3cret-value"


def _entry(cwd: Path, body: str = _ENTRY) -> Path:
    entry = cwd / "small.jsonnet"
    entry.write_text(body)
    return entry


@pytest.fixture
def home(otto_home: Path) -> Path:  # noqa: F811
    """This test's private, empty ``OTTO_HOME``: where ``~/.otto/settings.toml`` is read."""
    return otto_home


@pytest.fixture
def cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, home: Path) -> Path:
    """A working directory of its own, holding the source's import root ``lab_data/lib``."""
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    return work


@pytest.mark.parametrize(
    ("paths", "backend"),
    [
        (["lab.jsonnet"], "jsonnet"),
        (["labs/*.jsonnet"], "jsonnet"),
        (["a.jsonnet", "b.jsonnet"], "jsonnet"),
        (["lab.json"], "json"),
        (["lab_data/tech1"], "json"),
        (["labs/*.json"], "json"),
        (["labs/*"], "json"),
        ([], "json"),
    ],
    ids=lambda v: v if isinstance(v, str) else "+".join(v) or "none",
)
def test_paths_backend_is_jsonnet_for_jsonnet_entries_and_json_for_the_rest(paths, backend):
    assert paths_backend([Path(p) for p in paths]) == backend
    # A str entry is a path, as ``load_lab(search_paths=)`` has always read one.
    assert paths_backend(paths) == backend


@pytest.mark.parametrize("mixed", [["lab.jsonnet", "lab_data/tech1"], ["labs/*.json", "x.jsonnet"]])
def test_paths_backend_refuses_mixed_kinds_naming_every_path(mixed):
    with pytest.raises(
        LabRepositoryError, match=r"mix \.jsonnet entries with other lab paths"
    ) as e:
        paths_backend([Path(p) for p in mixed])
    for path in mixed:
        assert path in str(e.value)


def test_a_bare_load_of_a_jsonnet_search_path_renders(cwd):
    """No ``view=``: the entry renders under ``search_paths_view``, and loads."""
    lab = load_lab("small", search_paths=[_entry(cwd)])
    assert sorted(lab.hosts) == ["h1", "local"]
    assert lab.hosts["h1"].ip == "10.0.0.1"


def test_a_str_jsonnet_search_path_loads(cwd):
    """A ``str`` entry is read as a path: ``search_paths=["small.jsonnet"]`` renders the entry."""
    _entry(cwd)
    lab = load_lab("small", search_paths=["small.jsonnet"])
    assert sorted(lab.hosts) == ["h1", "local"]


def test_a_str_json_search_path_loads(cwd):
    """A ``str`` directory entry reads its ``lab.json``, as it did before ``.jsonnet`` entries."""
    lab_data = cwd / "lab_data"
    lab_data.mkdir()
    (lab_data / "lab.json").write_text(
        json.dumps({"labs": {"small": {}}, "elements": [_json_element()]})
    )
    lab = load_lab("small", search_paths=["lab_data"])
    assert sorted(lab.hosts) == ["h1", "local"]


def test_a_search_path_render_sees_an_empty_project_name(cwd):
    """No project declares the source, so ``otto.project`` is ``""``: never invented, never null."""
    lab = load_lab("small", search_paths=[_entry(cwd)])
    assert lab.metadata["small"] == {"project": ""}


def test_a_directory_holding_only_lab_jsonnet_says_to_name_the_file(cwd):
    """A directory entry reads ``lab.json``; the failure says a ``.jsonnet`` entry is the file."""
    lab_data = cwd / "lab_data"
    lab_data.mkdir()
    (lab_data / "lab.jsonnet").write_text(_ENTRY)
    with pytest.raises(LabRepositoryError, match="No lab data found") as refused:
        load_lab("small", search_paths=[Path("lab_data")])
    message = str(refused.value)
    assert f"{lab_data / 'lab.jsonnet'} is a Jsonnet template" in message
    assert "in load_lab(search_paths=), name the .jsonnet file" in message
    # The hint is the remedy: naming the file loads the lab.
    assert "h1" in load_lab("small", search_paths=[Path("lab_data/lab.jsonnet")]).hosts


@pytest.mark.parametrize("paths", [["lab_data"], ["lab_data/lab.jsonnet"]])
def test_a_project_json_source_over_a_template_says_to_declare_a_jsonnet_source(cwd, paths):
    """A project's json source never reads the template, named or not; the hint's remedy loads it.

    Naming the file is ``load_lab``'s remedy, not a json source's: there the
    json rule reads ``lab_data/lab.jsonnet`` as a directory. The remedy that
    holds is a ``backend = "jsonnet"`` source, which then loads the lab.
    """
    from otto.session import LabBuildError, build_lab
    from tests.unit.templating._lab_project import json_source, jsonnet_source, project

    def holding_the_template(root: Path) -> Path:
        (root / "lab_data").mkdir(parents=True)
        (root / "lab_data" / "lab.jsonnet").write_text(_ENTRY)
        return root

    root = holding_the_template(cwd / "sut")
    sut = project(root, json_source(*(Path(p) for p in paths)))
    with pytest.raises(LabBuildError, match="No lab data found") as refused:
        build_lab([sut], labs=["small"])
    message = str(refused.value)
    assert f"{root / 'lab_data' / 'lab.jsonnet'} is a Jsonnet template" in message
    assert 'in a project, declare a backend = "jsonnet" [[lab.sources]] entry' in message
    fixed = project(holding_the_template(cwd / "fixed"), jsonnet_source(paths=paths))
    assert "h1" in build_lab([fixed], labs=["small"]).hosts


def test_an_inventory_argument_cannot_render_a_template_and_the_refusal_says_so(cwd):
    """``inventory=`` carries no protected-file catalog; ``load_lab`` names its own remedy.

    The general statement is ``NO_CATALOG``'s; the guidance after it is the
    caller's: no inventory reaches a bare search path's template, even with
    ``view=``, so a template that reads one goes through a configured project.
    """
    from otto.inventory.config import NO_CATALOG
    from otto.inventory.json_backend import JsonInventory

    records = cwd / "inventory.json"
    records.write_text("{}")
    with pytest.raises(LabRepositoryError, match="no protected-file catalog") as refused:
        load_lab("small", search_paths=[_entry(cwd)], inventory=JsonInventory(path=records))
    message = str(refused.value)
    assert NO_CATALOG.refusal is not None
    assert NO_CATALOG.refusal in message
    assert "declares no inventories, even with view=" in message
    assert "otto.session.build_lab" in message


_READS_INVENTORY = """\
function(otto) {
  labs: { bench: {} },
  elements: [{
    name: 'dut',
    labs: ['bench'],
    hosts: [{ inventory: { name: 'physical', key: 'edge-17' } }],
  }],
}
"""
"""A lab whose one host is the ``physical`` inventory's ``edge-17`` record."""


def _inventory_project(root: Path) -> "tuple[Project, Path]":
    """A project with a ``physical`` inventory and a jsonnet source declaring it."""
    from tests.unit.templating._lab_project import CREDS, inventory, jsonnet_source, project

    entry = root / "lab_data" / "lab.jsonnet"
    entry.parent.mkdir(parents=True)
    entry.write_text(_READS_INVENTORY)
    settings = inventory(root, "physical", {"edge-17": {"ip": "10.0.0.17", "creds": CREDS}})
    settings += jsonnet_source(paths=[str(entry)], inventories=["physical"])
    return project(root, settings), entry


def test_a_view_gives_a_bare_jsonnet_search_path_no_inventory(cwd):
    """``view=`` brings its catalog, not a declaration: the bare source declares no inventory."""
    from otto.inventory import build_inventory_view

    sut, entry = _inventory_project(cwd / "sut")
    with pytest.raises(LabRepositoryError, match="its inventories: none"):
        load_lab("bench", search_paths=[entry], view=build_inventory_view(projects=[sut]))


def test_the_refusals_remedy_loads_a_template_that_reads_an_inventory(cwd):
    """Both remedies ``load_lab`` names load it: ``build_lab``, and the source with its view."""
    from otto.inventory import build_inventory_view
    from otto.labs import build_lab_sources
    from otto.session import build_lab

    sut, _entry_path = _inventory_project(cwd / "sut")
    assert build_lab([sut], labs=["bench"]).hosts["dut"].ip == "10.0.0.17"
    lab = load_lab(
        "bench",
        repository=build_lab_sources(projects=[sut]),
        view=build_inventory_view(projects=[sut]),
    )
    assert lab.hosts["dut"].ip == "10.0.0.17"


def _user_settings(home: Path, protected: Path) -> None:
    """Write a user settings file whose inventory's creds file is *protected*."""
    records = home / "usr.json"
    records.write_text(json.dumps({"usr1": {"ip": "10.9.9.9"}}))
    (home / "settings.toml").write_text(  # scaffold-exempt: the USER file is the subject
        "[[inventories]]\n"
        'name = "usr"\nbackend = "json"\n'
        f'path = "{records}"\n'
        f'creds = {{ backend = "json", path = "{protected}" }}\n'
    )


def test_a_file_the_user_settings_name_is_protected_on_that_path(cwd, home):
    """The default view's catalog holds the files ``~/.otto/settings.toml`` names.

    The same import reads the file while no settings name it (the control),
    and is refused as a protected file once the user file names it.
    """
    lib = cwd / "lab_data" / "lib"
    lib.mkdir(parents=True)
    creds = lib / "creds.json"
    creds.write_text(json.dumps({"usr1": [{"login": "u", "password": _SECRET}]}))
    body = _ENTRY.replace(
        "project: otto.project", "project: otto.project, c: importstr 'creds.json'"
    )
    entry = _entry(cwd, body)
    assert _SECRET in load_lab("small", search_paths=[entry]).metadata["small"]["c"]
    _user_settings(home, creds)
    clear_user_settings_memo()  # the process read the user file once, at the control render
    with pytest.raises(LabRepositoryError, match=r"refused \(a protected file\)") as refused:
        load_lab("small", search_paths=[entry])
    assert _SECRET not in str(refused.value)


def test_the_catalog_refuses_when_the_user_settings_cannot_be_read(cwd, home):
    """An unreadable user file leaves no complete catalog: a ``.jsonnet`` load renders nothing."""
    settings = home / "settings.toml"
    # invalid: [lab] is a project table
    settings.write_text('[lab]\nname = "s3cret"\n')  # scaffold-exempt: the unreadable USER file
    with pytest.raises(LabRepositoryError, match="cannot be read") as refused:
        load_lab("small", search_paths=[_entry(cwd)])
    assert str(settings) in str(refused.value)
    assert "s3cret" not in str(refused.value)


def test_a_json_search_path_never_reads_the_user_settings(cwd, home):
    """The json route keeps its default view: an unreadable user file does not touch it."""
    (home / "settings.toml").write_text('[lab]\nname = "x"\n')  # scaffold-exempt: as above
    lab_file = cwd / "lab.json"
    lab_file.write_text(json.dumps({"labs": {"small": {}}, "elements": [_json_element()]}))
    lab = load_lab("small", search_paths=[lab_file])
    assert "h1" in lab.hosts


def _json_element() -> dict[str, object]:
    return {
        "name": "h1",
        "labs": ["small"],
        "hosts": [
            {"ip": "10.0.0.1", "os_type": "unix", "creds": [{"login": "u", "password": "p"}]}
        ],
    }
