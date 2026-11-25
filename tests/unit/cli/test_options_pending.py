"""The window before a command reads its flags: a read there says so (§6 item 11).

``pending_options`` decides whether a command marks; ``ensure_lab_context``
holds the mark over the lab build and moves it onto the context, and the
lab-free leaf invoke holds it for the leaf. ``settle_options`` clears it for
a leaf that records nothing (``otto host <id> <command>``: defaults, §6 item
8); a leaf that records clears it by recording.
"""

import contextvars
import dataclasses
from types import SimpleNamespace
from typing import Any, cast

import pytest
import typer
from typer.core import TyperGroup

import otto
from otto.cli import invoke
from otto.cli.invoke import (
    RECORDS_OPTIONS_ATTR,
    ensure_lab_context,
    option_group,
    pending_options,
    settle_options,
    try_ensure_lab,
)
from otto.labs.loader import Lab
from otto.params import OptionsNotAvailableError, RecordedOptions, command_options
from otto.run_context import record_options, reset_context, set_context
from tests._fixtures.bootstrap_seam import patch_bootstrap
from tests._fixtures.bootstrapstub import bootstrap_stub
from tests._fixtures.clickctx import chain
from tests._fixtures.rootoptions import make_root_options

PENDING = "has not read its flags yet"


@dataclasses.dataclass
class Knobs:
    depth: int = 3


def test_option_group_names_the_three_groups():
    assert option_group(chain("otto", "run", "x")) == "run"
    assert option_group(chain("otto", "test", "suite")) == "test"
    assert option_group(chain("otto", "host", "ls")) == "host"
    assert option_group(chain("otto", "cov", "get")) is None
    assert option_group(chain("otto")) is None


def test_a_marked_context_refuses_a_read_until_its_command_records(make_otto_context):
    context = make_otto_context()
    recorded = pending_options(chain("otto", "run", "x"))
    assert recorded is not None
    record_options(context, recorded)
    with pytest.raises(OptionsNotAvailableError, match=PENDING):
        context.options(Knobs)
    record_options(context, RecordedOptions(group="run", parsed={"depth": 5}, offered=(Knobs,)))
    assert context.options(Knobs).depth == 5


def _with_callback(node: Any, callback: Any) -> Any:
    node.command.callback = callback
    return node


def test_a_command_that_declares_options_is_marked_whatever_its_group():
    def leaf() -> None: ...

    setattr(leaf, RECORDS_OPTIONS_ATTR, True)
    recorded = pending_options(_with_callback(chain("otto", "acme", "go"), leaf))
    assert recorded == RecordedOptions(group=None, pending=True)


def test_a_command_outside_the_groups_is_never_marked():
    assert pending_options(chain("otto", "cov", "get")) is None
    assert pending_options(_with_callback(chain("otto", "cov", "get"), lambda: None)) is None


def _lab_reading_options(monkeypatch):
    """A lab build whose host constructor reads ``otto.get_options(Knobs)``."""
    from otto.inventory import InventoryView
    from otto.session.lab import BuiltLab

    class ReadingHost:
        def __init__(self) -> None:
            self.knobs = otto.get_options(Knobs)

    def build(projects, labs):
        ReadingHost()
        return BuiltLab(lab=Lab(name="rig"), view=InventoryView.of_default(inventory=None))

    patch_bootstrap(monkeypatch, [])
    monkeypatch.setattr("otto.session.lab.build_lab_and_view", build)
    monkeypatch.setattr(
        "otto.reservations.factory.build_reservation_gate",
        lambda *args, **kwargs: SimpleNamespace(identity=None),
    )


@pytest.mark.parametrize("group", ["run", "host"])
def test_a_constructor_run_by_the_lab_build_cannot_read_an_option(monkeypatch, group):
    _lab_reading_options(monkeypatch)
    with typer.Context(TyperGroup(name="otto")) as root:
        root.meta["_otto_root_options"] = make_root_options(labs=["rig"])
        node = typer.Context(TyperGroup(name=group), parent=root, info_name=group)
        entry = ensure_lab_context if group == "run" else try_ensure_lab
        with pytest.raises(OptionsNotAvailableError, match=PENDING):
            entry(node)
    assert otto.get_options(Knobs) == Knobs()  # nothing leaks past the build


def test_a_built_context_keeps_the_mark_and_nothing_leaks_after(monkeypatch):
    from otto.inventory import InventoryView
    from otto.session.lab import BuiltLab

    patch_bootstrap(monkeypatch, [])
    monkeypatch.setattr(
        "otto.session.lab.build_lab_and_view",
        lambda projects, labs: BuiltLab(
            lab=Lab(name="rig"), view=InventoryView.of_default(inventory=None)
        ),
    )
    monkeypatch.setattr(
        "otto.reservations.factory.build_reservation_gate",
        lambda *args, **kwargs: SimpleNamespace(identity=None),
    )
    with typer.Context(TyperGroup(name="otto")) as root:
        root.meta["_otto_root_options"] = make_root_options(labs=["rig"])
        node = typer.Context(TyperGroup(name="run"), parent=root, info_name="run")
        built = ensure_lab_context(node)
        with pytest.raises(OptionsNotAvailableError, match=PENDING):
            built.options(Knobs)
    assert otto.get_options(Knobs) == Knobs()


def test_settling_clears_a_leaf_that_does_not_record_and_leaves_one_that_does(
    make_otto_context,
):
    context = make_otto_context()
    host_leaf = _with_callback(chain("otto", "host", "ls"), lambda: None)
    recorded = pending_options(host_leaf)
    assert recorded is not None
    record_options(context, recorded)
    binding = set_context(context)
    try:
        settle_options(host_leaf)
        assert otto.get_options(Knobs) == Knobs()
        assert context.verb == "host"
    finally:
        reset_context(binding)

    def run_leaf() -> None: ...

    setattr(run_leaf, RECORDS_OPTIONS_ATTR, True)
    stamped = _with_callback(chain("otto", "run", "x"), run_leaf)
    marked = pending_options(stamped)
    assert marked is not None
    with command_options(marked):
        settle_options(stamped)
        with pytest.raises(OptionsNotAvailableError, match=PENDING):
            otto.get_options(Knobs)


def test_settling_runs_before_the_dry_run_seam(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(invoke, "validate_project_switches", lambda ctx: None)
    monkeypatch.setattr(invoke, "fail_loud_on_bootstrap_errors", lambda ctx: None)
    monkeypatch.setattr(invoke, "command_spec", lambda ctx: SimpleNamespace(lab_free=True))
    monkeypatch.setattr(invoke, "settle_options", lambda ctx: calls.append("settle"))
    monkeypatch.setattr(invoke, "stop_at_dry_run_seam", lambda ctx, spec: calls.append("seam"))
    leaf = _with_callback(chain("otto", "host", "ls"), lambda: None)
    leaf.meta = {}
    invoke.command_preamble(leaf)
    assert calls == ["settle", "seam"]


def test_the_pending_error_passes_both_catch_alls(monkeypatch):
    from otto.cli.expose import host_class_for_id

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise OptionsNotAvailableError(f"Knobs cannot be read: the command {PENDING}")

    monkeypatch.setattr(invoke, "ensure_lab_context", refuse)
    with pytest.raises(OptionsNotAvailableError, match=PENDING):
        try_ensure_lab(chain("otto", "host"))
    monkeypatch.setattr("otto.fleet.get_host", refuse)
    with pytest.raises(OptionsNotAvailableError, match=PENDING):
        host_class_for_id("router1")


def _root_with(spec_name: str, app: typer.Typer, monkeypatch) -> Any:
    from otto.cli.invoke import wrap_leaf_callbacks
    from otto.cli.registry import CommandSpec, resolve_spec_command

    monkeypatch.setattr("otto.bootstrap.bootstrap", bootstrap_stub)
    spec = CommandSpec(name=spec_name, loader=app, lab_free=True, output_dir=False)
    cmd = wrap_leaf_callbacks(resolve_spec_command(spec), spec)
    return TyperGroup(name="otto", commands={spec_name: cmd})


def _run_contextless(root: Any, args: list[str]) -> None:
    """Run *root* as a lab-free command runs: with no context installed (a fresh one)."""
    contextvars.Context().run(root.main, args, prog_name="otto", standalone_mode=False)


def test_a_lab_free_options_command_is_pending_until_it_records(monkeypatch):
    from otto.cli.invoke import LeafOptionSpec, prepare_leaf_options, stamp_options_leaf

    seen: list[object] = []
    spec = LeafOptionSpec(group=None, build=(Knobs,))
    app = typer.Typer()

    @app.command()
    def go(depth: int = 3) -> None:
        try:
            otto.get_options(Knobs)
        except OptionsNotAvailableError as e:
            seen.append(e)
        prepared = prepare_leaf_options({"depth": depth}, spec, leaf=go)
        with prepared.scope():
            seen.append(otto.get_options(Knobs))

    stamp_options_leaf(go, spec)
    app.command("other")(lambda: None)
    root = _root_with("acme", app, monkeypatch)
    _run_contextless(root, ["acme", "go", "--depth", "7"])
    assert isinstance(seen[0], OptionsNotAvailableError)
    assert seen[1] == Knobs(depth=7)
    assert otto.get_options(Knobs) == Knobs()


def test_a_lab_free_options_command_that_opens_the_lab_keeps_its_flags(monkeypatch):
    """The ``extending-cli`` shape: a lab-free leaf records its flags, then its body opens the lab.

    The lab's context carries the leaf's record, not a fresh pending one, so a
    read after the opening returns the typed value.
    """
    from otto.cli.invoke import LeafOptionSpec, prepare_leaf_options, stamp_options_leaf
    from otto.config._typer import get_current_context
    from otto.inventory import InventoryView
    from otto.session.lab import BuiltLab

    monkeypatch.setattr(
        "otto.session.lab.build_lab_and_view",
        lambda projects, labs: BuiltLab(
            lab=Lab(name="rig"), view=InventoryView.of_default(inventory=None)
        ),
    )
    monkeypatch.setattr(
        "otto.reservations.factory.build_reservation_gate",
        lambda *args, **kwargs: SimpleNamespace(identity=None),
    )
    monkeypatch.setattr(invoke, "root_options", lambda ctx: make_root_options(labs=["rig"]))
    seen: list[object] = []
    spec = LeafOptionSpec(group=None, build=(Knobs,))
    app = typer.Typer()

    @app.command()
    def go(depth: int = 3) -> None:
        prepared = prepare_leaf_options({"depth": depth}, spec, leaf=go)
        with prepared.scope():
            built = ensure_lab_context(cast("typer.Context", get_current_context()))
            seen.append(otto.get_options(Knobs).depth)
            seen.append(built.options(Knobs).depth)

    stamp_options_leaf(go, spec)
    app.command("other")(lambda: None)
    root = _root_with("acme", app, monkeypatch)
    _run_contextless(root, ["acme", "go", "--depth", "7"])
    assert seen == [7, 7]


def test_a_lab_free_leaf_in_a_group_that_records_nothing_reads_defaults_in_its_body(monkeypatch):
    seen: list[object] = []
    app = typer.Typer()

    @app.command()
    def show() -> None:
        seen.append(otto.get_options(Knobs))

    app.command("other")(lambda: None)
    root = _root_with("test", app, monkeypatch)
    _run_contextless(root, ["test", "show"])
    assert seen == [Knobs()]


def test_a_host_command_body_reads_defaults(monkeypatch):
    """Through the dispatch ``tests/unit/cli/test_host.py`` drives, with the real lab context."""
    from unittest.mock import patch

    from typer.testing import CliRunner

    from otto.cli.main import app
    from otto.inventory import InventoryView
    from otto.session.lab import BuiltLab
    from tests.unit.cli.test_host import _make_host_with_session

    mock_host = _make_host_with_session([("", 0)])
    seen: list[object] = []

    async def exec_reading(*args: Any, **kwargs: Any) -> Any:
        seen.append(otto.get_options(Knobs))
        seen.append(otto.get_context().verb)

    mock_host.exec = exec_reading  # type: ignore[method-assign]
    patch_bootstrap(monkeypatch, [])
    monkeypatch.setattr(
        "otto.session.lab.build_lab_and_view",
        lambda projects, labs: BuiltLab(
            lab=Lab(name="x"), view=InventoryView.of_default(inventory=None)
        ),
    )
    with (
        patch("otto.cli.invoke.ensure_cli_session"),
        patch("otto.logger.management.create_output_dir"),
        patch("otto.fleet.get_host", return_value=mock_host),
    ):
        result = CliRunner().invoke(app, ["--lab", "x", "-R", "host", "router1", "exec", "ls"])
    assert seen == [Knobs(), "host"], (result.output, result.exception)
