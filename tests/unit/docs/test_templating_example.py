"""The templated-lab worked example is a real project, and the page shows what otto prints for it.

``docs/configuration/lab/templating.md`` literalincludes every file of
``docs/examples/templating/routers/`` and, for the commands it walks through,
the output files beside it under ``docs/examples/templating/output/``. Each
test copies the project to a scratch directory, runs the command through the
root app over the copy (``OTTOPATH`` naming it, as the page says), and holds
the result to the file the page shows: the composed lab, its listing, the
rendered document, the explanation of one host, and the refusal of a
generated element that would replace another source's machines (the page's
``override/`` files added to the copy). The copy's path is shown as
``/home/me/routers``, as the page writes it.
"""

import json
import logging
import re
import shutil
import sys
from pathlib import Path

import pytest
import rich
from typer.testing import CliRunner

from otto import bootstrap
from otto.cli.main import app, entry
from tests._fixtures.paths import CHECKOUT_ROOT
from tests.unit.templating import conftest as _templating

# The templating conftest's fixtures: the binding proof every templated test
# file carries, and a private OTTO_HOME.
jsonnet_binding_under_test = _templating.jsonnet_binding_under_test
otto_home = _templating.otto_home

EXAMPLE = CHECKOUT_ROOT / "docs" / "examples" / "templating"
PROJECT = EXAMPLE / "routers"
OUTPUT = EXAMPLE / "output"
OVERRIDE = EXAMPLE / "override"
PAGE_ROOT = "/home/me/routers"
"""Where the page shows the project, in place of the scratch copy's path."""
ENTRY = "lab_data/layouts/router-lab.jsonnet"

runner = CliRunner()


@pytest.fixture
def routers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, otto_home: Path) -> Path:
    """A scratch copy of the example, the only project ``OTTOPATH`` names, as the cwd."""
    root = tmp_path / "routers"
    shutil.copytree(PROJECT, root)
    monkeypatch.setenv("OTTOPATH", str(root))
    monkeypatch.setenv("COLUMNS", "80")
    monkeypatch.delenv("OTTO_LAB", raising=False)
    monkeypatch.chdir(root)
    return root


def _page_text(root: Path, text: str) -> str:
    """Return *text* as the page shows it: the copy's path replaced, trailing spaces and
    leading and trailing blank lines dropped."""
    text = text.replace(str(root), PAGE_ROOT)
    return "\n".join(line.rstrip() for line in text.splitlines()).strip("\n")


def _otto(root: Path, *args: str) -> tuple[int, str]:
    """Run ``otto ARGS`` over a fresh composition root; return its exit code and its output."""
    bootstrap._reset()  # the root conftest snapshot-restores bootstrap's caches
    result = runner.invoke(app, list(args))
    return result.exit_code, _page_text(root, result.output)


def _shown(name: str) -> str:
    """Return the output file *name* the page includes, as :func:`_otto` returns output."""
    return (OUTPUT / name).read_text(encoding="utf-8").strip("\n")


def test_the_layout_lists_one_lab_per_site_and_west_is_unavailable(routers: Path) -> None:
    code, output = _otto(routers, "--list-labs")
    assert code == 0, output
    assert output == _shown("list-labs.txt")


def test_the_east_lab_holds_the_dut_and_the_traffic_generator(routers: Path) -> None:
    code, output = _otto(routers, "-l", "router-lab.east", "--list-hosts")
    assert code == 0, output
    assert output == _shown("list-hosts.txt")


def _refused(
    root: Path,
    *args: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> str:
    """Run ``otto ARGS`` through the console script's ``entry()``; return the refusal it prints.

    A root option's refusal is printed by ``entry()``'s boundary frame, which
    the runner never reaches. Wide, so the one-line error the page shows is
    not folded. The command must exit 1.
    """
    monkeypatch.setenv("COLUMNS", "300")
    monkeypatch.setattr(sys, "argv", ["otto", *args])
    monkeypatch.setattr(rich, "_console", None)  # rich builds its console anew, over the capture
    bootstrap._reset()
    with pytest.raises(SystemExit) as exited:
        entry()
    output = _page_text(root, capsys.readouterr().out)
    assert exited.value.code == 1, output
    return output


def test_the_west_lab_refuses_to_load_naming_its_reason(
    routers: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    output = _refused(
        routers, "-l", "router-lab.west", "--list-hosts", monkeypatch=monkeypatch, capsys=capsys
    )
    assert output == _shown("west.txt")


def _with_bench_source(root: Path, *, overrides: "list[str] | None" = None) -> None:
    """Give the copy the page's hand-written ``bench`` source, read before the template's.

    The page's ``override/sources.toml`` replaces the example's one source
    table, which it repeats last; its ``bench/lab.json`` goes to the copy's
    ``bench/``. With *overrides*, the template's source declares them.
    """
    sources = (OVERRIDE / "sources.toml").read_text(encoding="utf-8")
    template = sources[sources.index('[[lab.sources]]\nbackend = "jsonnet"') :]
    settings = root / ".otto" / "settings.toml"
    text = settings.read_text(encoding="utf-8")
    assert template in text, "the page's sources.toml repeats the example's source table"
    if overrides is not None:
        sources = sources.rstrip("\n") + f"\noverrides = {json.dumps(overrides)}\n"
    settings.write_text(  # scaffold-exempt: the page's in-place edit of the copied example
        text.replace(template, sources), encoding="utf-8"
    )
    shutil.copytree(OVERRIDE / "bench", root / "bench")


def test_a_generated_element_replacing_other_machines_fails_as_the_page_shows(
    routers: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _with_bench_source(routers)
    output = _refused(
        routers, "-l", "router-lab.east", "--list-hosts", monkeypatch=monkeypatch, capsys=capsys
    )
    assert output == _shown("override.txt")


def test_declaring_the_override_lets_the_generated_element_replace_it(
    routers: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """It replaces ``bench``'s element, with the override warning the page promises."""
    _with_bench_source(routers, overrides=["east-dut0"])
    with caplog.at_level(logging.WARNING, logger="otto.labs.composite"):
        code, output = _otto(routers, "-l", "router-lab.east", "--show-lab")
    assert code == 0, output
    assert "address: 10.1.0.1" in output
    assert "10.1.0.9" not in output
    warnings = [r.getMessage() for r in caplog.records if r.name == "otto.labs.composite"]
    assert (
        "element 'east-dut0' in lab 'router-lab.east': routers/jsonnet#2 overrides routers/bench"
        in warnings
    ), warnings


def test_render_prints_the_document_the_page_shows(routers: Path) -> None:
    code, output = _otto(routers, "lab", "render", ENTRY)
    assert code == 0, output
    assert output == _shown("render.json")
    document = json.loads(output)
    assert document["labs"] == {
        "router-lab.east": {},
        "router-lab.west": {"unavailable": "tgen: expected 1, found 0 (none)"},
    }
    assert [
        (element["name"], element["hosts"][0]["inventory"]) for element in document["elements"]
    ] == [
        ("east-dut0", {"name": "physical", "key": "east-r1"}),
        ("east-tgen0", {"name": "virtual", "key": "east-tgen"}),
    ]


def test_explain_names_a_layer_for_every_value_as_the_page_shows(routers: Path) -> None:
    code, output = _otto(routers, "-l", "router-lab.east", "lab", "explain", "east-dut0")
    assert code == 0, output
    assert output == _shown("explain.txt")
    for layer in ("creds store", "inventory record", "template", "host preferences", "os profile"):
        assert f" {layer} " in output, layer


def test_the_page_includes_every_file_of_the_project_and_every_output() -> None:
    """A reader builds the project from the page alone, so the page shows every file."""
    page = (CHECKOUT_ROOT / "docs" / "configuration" / "lab" / "templating.md").read_text()
    included = set(
        re.findall(
            r"^```\{literalinclude\} \.\./\.\./examples/templating/(\S+)$", page, re.MULTILINE
        )
    )
    shown = {
        path.relative_to(EXAMPLE).as_posix()
        for path in EXAMPLE.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }
    assert included == shown
