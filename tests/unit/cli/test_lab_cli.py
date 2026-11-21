"""``otto lab``: a lab-free, ungated group, documented, and dispatched from the root.

``render`` needs no lab. ``explain`` takes its lab from the root ``-l``, as
every verb does, builds it once through the session and never presents the
reservation gate.
"""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from otto import bootstrap
from otto.cli.main import app
from otto.cli.registry import CLI_COMMANDS
from tests._fixtures.cli_registry import builtin_command_names
from tests._fixtures.paths import CHECKOUT_ROOT
from tests.unit.templating import conftest as _templating
from tests.unit.templating._lab_project import (
    CREDS,
    json_source,
    jsonnet_source,
    project,
    write,
)

# The templating conftest's fixtures: the binding proof every templated test
# file carries, and a private OTTO_HOME.
jsonnet_binding_under_test = _templating.jsonnet_binding_under_test
otto_home = _templating.otto_home

runner = CliRunner()


def test_lab_group_is_registered_and_documented():
    assert "lab" in builtin_command_names()
    spec = CLI_COMMANDS.get("lab")
    assert (spec.lab_free, spec.output_dir, spec.gate) == (True, False, False)
    page = (CHECKOUT_ROOT / "docs" / "cli" / "lab" / "index.md").read_text()
    assert page.startswith("# otto lab\n")
    assert ":file: ../../_static/generated/termynal/help-lab.html" in page
    assert "otto lab render ENTRY [--source NAME]" in page
    assert "otto -l NAME lab explain HOST_ID" in page
    result = runner.invoke(app, ["lab", "--help"])
    assert result.exit_code == 0, result.output
    assert "render" in result.output
    assert "explain" in result.output


def test_lab_render_needs_no_lab(tmp_path, monkeypatch, otto_home):
    entry = write(tmp_path / "sut" / "lab.jsonnet", "function(otto) { labs: { bench: {} } }")
    sut = project(tmp_path / "sut", jsonnet_source(paths=[str(entry)]))
    monkeypatch.setenv("OTTOPATH", str(sut.root))
    monkeypatch.delenv("OTTO_LAB", raising=False)
    bootstrap._reset()  # the root conftest snapshot-restores bootstrap's caches
    result = runner.invoke(app, ["lab", "render", str(entry)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"labs": {"bench": {}}}


# --------------------------------------------------------------------------- #
# explain: the lab is the root -l's, the session's, and ungated.
# --------------------------------------------------------------------------- #


@pytest.fixture
def bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, otto_home: Path) -> Path:
    """A project ``OTTOPATH`` names whose plain lab file declares lab ``bench``, host ``dut``."""
    root = tmp_path / "sut"
    lab_file = write(
        root / "lab_data" / "lab.json",
        {
            "labs": {"bench": {}},
            "elements": [
                {"name": "dut", "labs": ["bench"], "hosts": [{"ip": "10.0.0.9", "creds": CREDS}]}
            ],
        },
    )
    sut = project(root, json_source(lab_file))
    monkeypatch.setenv("OTTOPATH", str(sut.root))
    monkeypatch.setenv("COLUMNS", "300")
    monkeypatch.delenv("OTTO_LAB", raising=False)
    return sut.root


def _root(*args: str):
    """Invoke the root app as ``otto`` with *args*, over a fresh composition root."""
    bootstrap._reset()  # the root conftest snapshot-restores bootstrap's caches
    return runner.invoke(app, list(args))


def test_global_lab_option_explains_a_host(bench):
    result = _root("-l", "bench", "lab", "explain", "dut")
    assert result.exit_code == 0, result.output
    assert "lab file" in result.stdout
    assert "source: sut/json#1" in result.stdout.splitlines()


def test_explain_presents_no_reservation_gate(bench, monkeypatch):
    """A gate that would refuse is never evaluated: explain touches no host."""
    from otto.cli import invoke
    from otto.reservations import MissingReservationError

    def refuse() -> None:
        raise MissingReservationError("bench is reserved by somebody else")

    gate = SimpleNamespace(identity=None, evaluate=refuse)
    monkeypatch.setattr(
        "otto.reservations.factory.build_reservation_gate", lambda *args, **kwargs: gate
    )
    presented: list[Any] = []
    monkeypatch.setattr(invoke, "present_reservation_gate", presented.append)
    result = _root("-l", "bench", "lab", "explain", "dut")
    assert result.exit_code == 0, result.output
    assert presented == []
    # The gate it would have presented is the session's, and it refuses.
    with pytest.raises(MissingReservationError):
        gate.evaluate()


def test_unknown_lab_fails_like_other_verbs(bench):
    explained = _root("-l", "nosuch", "lab", "explain", "dut")
    other = _root("-l", "nosuch", "host", "dut", "exec", "true")
    assert explained.exit_code == other.exit_code == 1
    assert type(explained.exception) is type(other.exception)
    assert str(explained.exception) == str(other.exception)
    assert "nosuch" in str(explained.exception)
    assert (explained.output, other.output) == ("", "")
    missing = _root("lab", "explain", "dut")
    assert missing.exit_code == 2
    assert "Missing option '--lab' / '-l'" in missing.output


def test_explain_lab_option_is_unknown(bench):
    result = _root("lab", "explain", "dut", "--lab", "bench")
    assert result.exit_code == 2
    assert "No such option: --lab" in result.output


def test_explain_reuses_the_session_lab_and_view(bench, monkeypatch):
    """One build per invocation, and explain_host reads exactly the session's lab and view."""
    from otto.labs import inspect
    from otto.session import lab as session_lab

    builds: list[Any] = []
    real_build = session_lab.build_lab_and_view

    def counting_build(projects: Any, *, labs: Any, view: Any = None) -> Any:
        builds.append(real_build(projects, labs=labs, view=view))
        return builds[-1]

    explained: list[dict[str, Any]] = []
    real_explain = inspect.explain_host

    def spy_explain(**kwargs: Any) -> Any:
        explained.append(kwargs)
        return real_explain(**kwargs)

    monkeypatch.setattr(session_lab, "build_lab_and_view", counting_build)
    monkeypatch.setattr(inspect, "explain_host", spy_explain)
    result = _root("-l", "bench", "lab", "explain", "dut")
    assert result.exit_code == 0, result.output
    [built] = builds
    [call] = explained
    assert call["lab"] is built.lab
    assert call["view"] is built.view
    assert call["trace"].entry_for(host_id="dut") is not None
