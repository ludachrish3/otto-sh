"""``otto lab render``: a Jsonnet entry's rendered document, before validation, never a secret.

:func:`~otto.labs.inspect.render_entry` is driven directly over real projects
(``_lab_project``); what the user sees (the printed document, the refusal and
every error) is driven through ``otto lab``'s Typer app, over a project
``OTTOPATH`` names.
"""

import json
import logging
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from otto import bootstrap
from otto.cli.lab import lab_app
from otto.config.project import Project
from otto.inventory.config import deferred_inventory_view
from otto.labs import LabRepositoryError
from otto.labs.inspect import render_entry
from otto.labs.jsonnet_source import _JsonnetLabRepository
from otto.labs.sources import build_lab_sources
from otto.templating.errors import RenderCancelled

from ._lab_project import CREDS, inventory, json_source, jsonnet_source, project, write
from ._render_helpers import count_evaluations

runner = CliRunner()

SENTINEL = "SENTINEL-d41d8cd9"
"""A literal secret no message, path or label could contain by accident."""

BARE = """
function(otto) {
  labs: { bench: {} },
  elements: [{ name: 'dut', labs: ['bench'], hosts: [{ inventory: 'edge-17' }] }],
}
"""
"""A document whose host names its inventory with a bare reference, which a load refuses."""

SECRET = (
    "function(otto) { labs: { bench: {} }, elements: [{ name: 'dut', labs: ['bench'], "
    "hosts: [{ ip: '10.0.0.9', os_type: 'unix', creds: [{ login: 'a' }, "
    f"{{ login: 'b', password: '{SENTINEL}' }}] }}] }}] }}"
)
"""A document holding a literal password, at ``elements[0].hosts[0].creds[1].password``."""

SIMPLE = "function(otto) { labs: { ['lab-' + otto.project]: {} } }"
"""An entry that declares one lab, named from its project."""

PHYSICAL = {"edge-17": {"ip": "10.0.0.17", "creds": CREDS}}


def _sut(root: Path, text: str, *, name: str = "sut", **source: Any):
    """A project whose one jsonnet source renders *text* as ``root/lab_data/lab.jsonnet``."""
    entry = write(root / "lab_data" / "lab.jsonnet", text)
    source.setdefault("paths", [str(entry)])
    settings = inventory(root, "physical", PHYSICAL) + jsonnet_source(
        inventories=["physical"], **source
    )
    return project(root, settings, name=name), entry


def _render(
    entry: Path, projects: "list[Project]", *, source: "str | None" = None, view: Any = None
):
    """:func:`render_entry` over *projects*, through the view the CLI builds by default."""
    return render_entry(
        entry=entry,
        source=source,
        projects=projects,
        view=deferred_inventory_view(projects=projects) if view is None else view,
    )


@pytest.fixture
def cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, otto_home: Path):
    """Run ``otto lab`` over the projects ``OTTOPATH`` names; return the runner call."""
    monkeypatch.setenv("COLUMNS", "600")  # no fold may split a path or the sentinel

    def invoke(roots: "list[Path]", *args: str):
        monkeypatch.setenv("OTTOPATH", ":".join(str(r) for r in roots))
        bootstrap._reset()  # the root conftest snapshot-restores bootstrap's caches
        return runner.invoke(lab_app, list(args))

    return invoke


# --------------------------------------------------------------------------- #
# What renders, and what prints.
# --------------------------------------------------------------------------- #


def test_render_prints_before_validation(tmp_path, cli):
    sut, entry = _sut(tmp_path / "sut", BARE)
    rendered = _render(entry, [sut])
    assert rendered.source_label == "sut/jsonnet#1"
    assert rendered.secret_at is None
    assert rendered.document == {
        "labs": {"bench": {}},
        "elements": [{"name": "dut", "labs": ["bench"], "hosts": [{"inventory": "edge-17"}]}],
    }
    # the load refuses the same document
    view = deferred_inventory_view(projects=[sut])
    [source] = build_lab_sources(projects=[sut]).sources
    assert isinstance(source.repository, _JsonnetLabRepository)
    with pytest.raises(LabRepositoryError, match="bare reference 'edge-17'"):
        source.repository.list_lab_status(view=view.for_project(root=sut.root))
    result = cli([sut.root], "render", str(entry))
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == rendered.document
    assert result.stdout == json.dumps(rendered.document, indent=2) + "\n"


def test_render_refuses_a_literal_secret_naming_its_path_not_value(tmp_path, cli):
    sut, entry = _sut(tmp_path / "sut", SECRET)
    rendered = _render(entry, [sut])
    assert rendered.secret_at == "elements[0].hosts[0].creds[1].password"
    assert SENTINEL not in repr(rendered)  # a traceback's locals never show the document
    result = cli([sut.root], "render", str(entry))
    assert result.exit_code == 1
    assert result.output == (
        f"refusing to print {entry}: it holds a literal secret at "
        "elements[0].hosts[0].creds[1].password; move it to the credentials store\n"
    )


@pytest.mark.parametrize(
    ("value", "shown"),
    [("1234", "1234"), (f"['{SENTINEL}']", SENTINEL), (f"{{ v: '{SENTINEL}' }}", SENTINEL)],
    ids=["int", "list", "table"],
)
def test_render_refuses_a_secret_that_is_not_a_string(value, shown, tmp_path, cli):
    """An off-type secret is one a load refuses, which is exactly when ``render`` is used."""
    text = (
        "function(otto) { labs: { bench: {} }, elements: [{ name: 'dut', labs: ['bench'], "
        f"hosts: [{{ ip: '10.0.0.9', ssh_options: {{ passphrase: {value} }} }}] }}] }}"
    )
    sut, entry = _sut(tmp_path / "sut", text)
    assert _render(entry, [sut]).secret_at == "elements[0].hosts[0].ssh_options.passphrase"
    result = cli([sut.root], "render", str(entry))
    assert result.exit_code == 1
    assert result.output == (
        f"refusing to print {entry}: it holds a literal secret at "
        "elements[0].hosts[0].ssh_options.passphrase; move it to the credentials store\n"
    )
    assert shown not in result.output


def test_render_error_output_holds_no_secret(tmp_path, cli, caplog, capfd):
    """No failure shows the document: a parse error on its line, a runtime error, a miss."""
    caplog.set_level(logging.DEBUG)
    parse = f"function(otto) {{ password: '{SENTINEL}' 'x' }}"
    runtime = f"function(otto) {{ creds: [{{ password: '{SENTINEL}' }}], n: 1 + {{}} }}"
    outputs = []
    for name, text in (("parse", parse), ("runtime", runtime)):
        sut, entry = _sut(tmp_path / name, text, name=name)
        result = cli([sut.root], "render", str(entry))
        assert result.exit_code == 1, result.output
        assert f"Lab file '{entry}' could not be rendered: {entry}:1: " in result.output
        outputs.append(result.output)
    stray = write(tmp_path / "stray.jsonnet", f"{{ password: '{SENTINEL}' }}")
    result = cli([tmp_path / "parse"], "render", str(stray))
    assert result.exit_code == 1
    assert "no jsonnet lab source reads this file" in result.output
    outputs.append(result.output)
    out, err = capfd.readouterr()
    for text in [*outputs, out, err, caplog.text]:
        assert SENTINEL not in text


LEAK_IMPORT = (
    "function(otto) { labs: { [std.strReplace(std.strReplace(importstr 'creds.json', "
    "'\"', ''), ' ', '')]: {} } }"
)
"""An entry that would name its lab after the bytes of ``creds.json`` in an import root."""


def _user_creds_file(otto_home: Path, creds: Path, *, valid: bool) -> Path:
    """Write the user settings file, its ``[creds]`` naming *creds*; invalid unless *valid*."""
    from otto.config.user_settings import clear_user_settings_memo

    text = f'[creds]\nbackend = "json"\npath = "{creds}"\n'
    if not valid:
        text += '\n[lab]\nname = "a project-only table"\n'  # refused: extra='forbid'
    clear_user_settings_memo()
    return write(otto_home / "settings.toml", text)


def test_an_invalid_user_file_renders_nothing_and_leaks_no_file_it_names(tmp_path, cli, otto_home):
    """A user file that cannot be read names files nobody can list: no template renders.

    A partial catalog would leave the creds file the user file names
    unprotected, so the entry that ``importstr``s it is refused whole, with a
    message naming the user file and never the read error or the creds.
    """
    creds = write(tmp_path / "secrets" / "creds.json", [{"login": "a", "password": SENTINEL}])
    entry = write(tmp_path / "sut" / "lab_data" / "lab.jsonnet", LEAK_IMPORT)
    sut = project(  # reads no inventory, so nothing but the catalog stands in the way
        tmp_path / "sut", jsonnet_source(paths=[str(entry)], import_paths=[str(creds.parent)])
    )
    _user_creds_file(otto_home, creds, valid=True)
    control = cli([sut.root], "render", str(entry))
    assert control.exit_code == 1
    assert "refused (a protected file)" in control.output, (
        control.output
    )  # the user file protects it
    user_file = _user_creds_file(otto_home, creds, valid=False)
    result = cli([sut.root], "render", str(entry))
    assert result.exit_code == 1, result.output
    assert SENTINEL not in result.output
    assert (
        f"no protected-file catalog: the user settings file {user_file} cannot be read"
        in result.output
    ), result.output
    assert "project-only table" not in result.output  # the read error is never quoted
    hand = write(tmp_path / "hand" / "lab_data" / "lab.json", {"labs": {"bench": {}}})
    hand_written = project(tmp_path / "hand", json_source(hand.parent), name="hand")
    assert build_lab_sources(projects=[hand_written]).list_labs() == ["bench"]


def test_a_render_failure_names_the_entry_once(tmp_path, cli):
    """The lab file leads the message; the render's own failure is not prefixed by it again."""
    sut, entry = _sut(tmp_path / "sut", "function(otto) importstr '/etc/hostname'")
    result = cli([sut.root], "render", str(entry))
    assert result.exit_code == 1, result.output
    assert result.output.startswith(
        f"Lab file '{entry}' could not be rendered: import '/etc/hostname'"
    ), result.output
    assert result.output.count(str(entry)) == 1, result.output
    located = _sut(tmp_path / "located", "function(otto) error 'boom'")
    shown = cli([located[0].root], "render", str(located[1])).output
    assert f"could not be rendered: {located[1]}:1: boom" in shown, "a location keeps its line"


def test_a_cancelled_render_is_the_timeout_never_a_document(monkeypatch, tmp_path, otto_home):
    sut, entry = _sut(tmp_path / "sut", SIMPLE)
    view = deferred_inventory_view(projects=[sut])
    view.render_memo.cancel(scope=sut.root, reason="the completion deadline passed")
    n = count_evaluations(monkeypatch)
    with pytest.raises(LabRepositoryError, match="the completion deadline passed") as caught:
        _render(entry, [sut], view=view)
    assert str(caught.value).startswith("source sut/jsonnet#1: ")
    assert isinstance(caught.value.__cause__, RenderCancelled)
    assert n() == 0


# --------------------------------------------------------------------------- #
# One evaluation, one source.
# --------------------------------------------------------------------------- #


def test_render_and_load_share_one_evaluation(monkeypatch, tmp_path, otto_home):
    sut, entry = _sut(tmp_path / "sut", SIMPLE)
    view = deferred_inventory_view(projects=[sut])
    n = count_evaluations(monkeypatch)
    # a relative spelling of the entry is the same file, and the same render
    monkeypatch.chdir(entry.parent)
    rendered = _render(Path(entry.name), [sut], view=view)
    assert rendered.document == {"labs": {"lab-sut": {}}}
    report = build_lab_sources(projects=[sut]).lab_status_report(view=view)
    assert [s.name for s in report.labs] == ["lab-sut"]
    assert n() == 1


def test_render_needs_source_when_two_sources_read_the_entry(tmp_path, otto_home):
    shared = write(tmp_path / "shared" / "lab.jsonnet", SIMPLE)
    projects = [
        project(tmp_path / name, jsonnet_source(paths=[str(shared)]), name=name)
        for name in ("alpha", "beta")
    ]
    with pytest.raises(LabRepositoryError) as caught:
        _render(shared, projects)
    assert str(caught.value) == (
        f"{shared}: 2 jsonnet lab sources read this file; choose one with --source: "
        "'alpha/jsonnet#1', 'beta/jsonnet#1'"
    )
    rendered = _render(shared, projects, source="beta/jsonnet#1")
    assert rendered.source_label == "beta/jsonnet#1"
    assert rendered.document == {"labs": {"lab-beta": {}}}  # beta's own render
    with pytest.raises(LabRepositoryError) as wrong:
        _render(shared, projects, source="alpha/other")
    assert str(wrong.value) == (
        f"{shared}: jsonnet lab source 'alpha/other' does not read this file; "
        "the ones that do: 'alpha/jsonnet#1', 'beta/jsonnet#1'"
    )


def test_render_of_a_file_no_source_reads_lists_the_sources(tmp_path, otto_home):
    sut, _ = _sut(tmp_path / "sut", SIMPLE, name="sut")
    stray = write(tmp_path / "stray.jsonnet", SIMPLE)
    with pytest.raises(LabRepositoryError) as caught:
        _render(stray, [sut])
    assert str(caught.value) == (
        f"{stray}: no jsonnet lab source reads this file; the jsonnet sources: 'sut/jsonnet#1'"
    )
    plain = project(tmp_path / "plain", "")
    with pytest.raises(LabRepositoryError) as none:
        _render(stray, [plain])
    assert str(none.value) == (
        f"{stray}: no project declares a jsonnet lab source, so no source reads this file"
    )
