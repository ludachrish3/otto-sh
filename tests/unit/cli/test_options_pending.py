"""The window before a command reads its flags: a read there says so (§6 item 11).

``pending_options`` decides whether a command marks: one that accepts
registered options does, whatever its name (§6 item 17). The root group
holds the dispatched command's mark over the dispatch, so a group callback's
read raises; ``ensure_lab_context`` holds it over the lab build and moves it
onto the context, and the lab-free leaf invoke holds it for the leaf.
``settle_options`` clears it for a leaf that records nothing (``otto host <id>
<command>``: defaults, §6 item 8), and for a group that runs with no child; a
command prepared when it is invoked records over it.
"""

import contextvars
import dataclasses
from types import SimpleNamespace
from typing import Any, cast

import pytest
import typer
from typer.core import TyperGroup
from typer.testing import CliRunner

import otto
from otto.cli import invoke
from otto.cli.invoke import (
    ACCEPTS_OPTIONS_ATTR,
    OWN_OPTIONS_ATTR,
    OwnOptions,
    ensure_lab_context,
    pending_options,
    settle_options,
    stash_command_spec,
    try_ensure_lab,
)
from otto.cli.registry import CLI_COMMANDS, CommandSpec, resolve_spec_command
from otto.labs.loader import Lab
from otto.params import OptionsNotAvailableError, RecordedOptions, command_options, register_options
from otto.run_context import record_options, reset_context, set_context
from tests._fixtures import plugin_commands
from tests._fixtures.bootstrap_seam import fake_bootstrap_result, install_composed, patch_bootstrap
from tests._fixtures.bootstrapstub import bootstrap_stub
from tests._fixtures.clickctx import chain
from tests._fixtures.fake_project import fake_project
from tests._fixtures.plugin_commands import Acme
from tests._fixtures.rootoptions import make_root_options

PENDING = "has not read its flags yet"


@dataclasses.dataclass
class Knobs:
    depth: int = 3


def _builtin_spec(name: str) -> CommandSpec:
    from otto.cli.builtin_commands import register_builtin_commands

    register_builtin_commands()
    return CLI_COMMANDS.get(name)


def _dispatched(*names: str) -> Any:
    """The leaf context of ``otto <names>`` under otto's root, its command's spec stashed.

    The leaf's callback is the real one the registration resolves to, found by
    walking *names* down its groups. A group that builds its commands as they
    are asked for (``run``'s instructions, ``host``'s verbs) holds none, and
    its leaf gets no callback; a name a static group does not hold is refused.
    """
    spec = _builtin_spec(names[0])
    node: Any = resolve_spec_command(spec)
    for name in names[1:]:
        static = getattr(node, "commands", None) or {}
        if static and name not in static:
            raise LookupError(f"otto {' '.join(names)} is not a command")
        node = static.get(name)
    ctx = chain("otto", *names, root_params={})
    ctx.command.callback = getattr(node, "callback", None)
    stash_command_spec(ctx, spec)
    return ctx


@pytest.mark.parametrize(
    ("argv", "marks"),
    [
        (["cov", "get"], True),
        (["docker", "ps"], True),
        (["host", "exec"], True),
        (["run", "x"], True),
        (["test"], True),
        (["link", "list"], False),
        (["tunnel", "list"], False),
        (["monitor"], False),
        (["cov", "check-stamp"], False),
    ],
)
def test_a_command_marks_when_it_accepts(argv, marks):
    recorded = pending_options(_dispatched(*argv))
    assert (recorded is not None) == marks
    if marks:
        assert recorded == RecordedOptions(group=argv[0], pending=True)


def test_a_plugin_command_marks_when_it_accepts():
    for accepts in (True, None):
        with plugin_commands.registered(accepts_options=accepts):
            spec = CLI_COMMANDS.get("flash")
            ctx = chain("otto", "flash", "erase", root_params={})
            ctx.command.callback = plugin_commands.erase
            stash_command_spec(ctx, spec)
            marked = pending_options(ctx)
            assert (marked is not None) == bool(accepts)


def test_a_context_with_no_command_spec_marks_only_a_command_that_says_what_is_its_own():
    assert pending_options(chain("otto", "cov", "get")) is None

    def leaf() -> None: ...

    setattr(leaf, OWN_OPTIONS_ATTR, OwnOptions())
    node = chain("otto", "acme", "go")
    node.command.callback = leaf
    assert pending_options(node) == RecordedOptions(group=None, pending=True)


def test_a_marked_context_refuses_a_read_until_its_command_records(make_otto_context):
    context = make_otto_context()
    recorded = pending_options(_dispatched("run", "x"))
    assert recorded is not None
    record_options(context, recorded)
    with pytest.raises(OptionsNotAvailableError, match=PENDING):
        context.options(Knobs)
    record_options(context, RecordedOptions(group="run", parsed={"depth": 5}, offered=(Knobs,)))
    assert context.options(Knobs).depth == 5


def _with_callback(node: Any, callback: Any) -> Any:
    node.command.callback = callback
    return node


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
        stash_command_spec(node, _builtin_spec(group))
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
        stash_command_spec(node, _builtin_spec("run"))
        built = ensure_lab_context(node)
        with pytest.raises(OptionsNotAvailableError, match=PENDING):
            built.options(Knobs)
    assert otto.get_options(Knobs) == Knobs()


def test_settling_clears_the_mark_and_keeps_its_group(make_otto_context):
    context = make_otto_context()
    host_leaf = _with_callback(_dispatched("host", "exec"), lambda: None)
    recorded = pending_options(host_leaf)
    assert recorded is not None
    record_options(context, recorded)
    binding = set_context(context)
    try:
        settle_options()
        assert otto.get_options(Knobs) == Knobs()
        assert context.verb == "host"
    finally:
        reset_context(binding)


def test_a_listing_still_reads_defaults():
    """``otto test --list-tests -l lab`` lists at parse time, before any preparation.

    The ``otto test`` leaf marks (it accepts); the listing's settle clears
    the mark and a module it collects reads defaults
    (``listing_participation``, driven end to end by
    ``tests/unit/cli/test_test_listings.py``).
    """
    test_leaf = _dispatched("test")
    marked = pending_options(test_leaf)
    assert marked == RecordedOptions(group="test", pending=True)
    with command_options(marked):
        with pytest.raises(OptionsNotAvailableError, match=PENDING):
            otto.get_options(Knobs)
        settle_options()
        assert otto.get_options(Knobs) == Knobs()


def test_the_preparation_runs_before_the_dry_run_seam(monkeypatch):
    """The preparation, which settles a leaf that records nothing, runs before the seam."""
    calls: list[str] = []
    monkeypatch.setattr(invoke, "validate_project_switches", lambda ctx: None)
    monkeypatch.setattr(invoke, "fail_loud_on_bootstrap_errors", lambda ctx: None)
    monkeypatch.setattr(invoke, "command_spec", lambda ctx: SimpleNamespace(lab_free=True))
    monkeypatch.setattr(invoke, "prepare_command_options", lambda ctx: calls.append("prepare"))
    monkeypatch.setattr(invoke, "stop_at_dry_run_seam", lambda ctx, spec: calls.append("seam"))
    leaf = _with_callback(chain("otto", "host", "ls"), lambda: None)
    invoke.command_preamble(leaf)
    assert calls == ["prepare", "seam"]


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


def test_a_lab_free_options_command_reads_its_values_for_its_run_only(monkeypatch):
    """Prepared when it is invoked: its body reads what was typed, and nothing outlives the run."""
    from otto.cli.invoke import prepare_command_target

    seen: list[object] = []
    app = typer.Typer()

    async def go(knobs: Knobs) -> None:
        seen.append(knobs)
        seen.append(otto.get_options(Knobs))

    app.command()(prepare_command_target(go, Knobs))
    app.command("other")(lambda: None)
    root = _root_with("acme", app, monkeypatch)
    _run_contextless(root, ["acme", "go", "--depth", "7"])
    assert seen == [Knobs(depth=7), Knobs(depth=7)]
    assert otto.get_options(Knobs) == Knobs()


def test_a_lab_free_options_command_that_opens_the_lab_keeps_its_flags(monkeypatch):
    """The ``extending-cli`` shape: a lab-free leaf records its flags, then its body opens the lab.

    The lab's context carries the leaf's record, not a fresh pending one, so a
    read after the opening returns the typed value.
    """
    from otto.cli.invoke import leaf_preparation
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
    own = OwnOptions(cls=Knobs)
    app = typer.Typer()

    @app.command()
    def go(depth: int = 3) -> None:
        prepared = leaf_preparation(go, {"depth": depth}, own)
        with prepared.scope():
            built = ensure_lab_context(cast("typer.Context", get_current_context()))
            seen.append(otto.get_options(Knobs).depth)
            seen.append(built.options(Knobs).depth)

    setattr(go, OWN_OPTIONS_ATTR, own)
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


# --- the mark follows the declaration, through the real dispatch ---------------------


BUILD_READS: "list[object]" = []
"""What each :class:`ReadingHost` the lab build made read: its :class:`Acme`, or the error."""


class ReadingHost:
    """A host class whose constructor reads an options class, as a project's host may."""

    def __init__(self) -> None:
        try:
            acme = otto.get_options(Acme)
        except OptionsNotAvailableError as e:
            BUILD_READS.append(e)
            raise
        BUILD_READS.append(acme)


@pytest.fixture
def reading_lab(monkeypatch) -> "Any":
    """A lab build that constructs a :class:`ReadingHost`, then stops the command (exit 0).

    The command's body never runs: what the constructor read is the result.
    """

    def build(projects: Any, labs: Any) -> Any:
        ReadingHost()
        raise typer.Exit(0)

    BUILD_READS.clear()
    install_composed(monkeypatch, fake_bootstrap_result([fake_project("a")]))
    monkeypatch.setattr("otto.session.lab.build_lab_and_view", build)
    monkeypatch.setattr("otto.cli.invoke.ensure_cli_session", lambda ctx: None)
    yield BUILD_READS
    BUILD_READS.clear()


@pytest.fixture
def lab_plugins() -> "Any":
    """The fixture plugin commands registered as accepting and using the lab."""
    with plugin_commands.registered(accepts_options=True, lab_free=False):
        yield plugin_commands


@pytest.mark.usefixtures("lab_plugins")
@pytest.mark.parametrize("argv", [["cov", "get"], ["docker", "ps"], ["flash", "erase"]])
@pytest.mark.parametrize("covered", [True, False])
def test_a_constructor_that_reads_an_option_fails_under_an_accepting_command(  # §6 item 17
    otto_cli, reading_lab, argv, covered
):
    if covered:
        register_options(Acme, verbs=[argv[0]])
    result = otto_cli(["--lab", "rig", *argv], ACME_DEBUG=None)
    assert result.code != 0, result
    assert PENDING in result.stdout + result.stderr, result
    [refused] = reading_lab
    assert isinstance(refused, OptionsNotAvailableError)


@pytest.mark.parametrize("argv", [["link", "list"], ["tunnel", "list"], ["monitor", "--live"]])
def test_the_same_constructor_reads_defaults_under_a_command_that_does_not_accept(
    otto_cli, reading_lab, argv
):
    result = otto_cli(["--lab", "rig", *argv], ACME_DEBUG=None)
    assert reading_lab == [Acme()], result
    assert result.code == 0, result


@pytest.mark.usefixtures("lab_plugins")
def test_a_leaf_that_does_not_accept_builds_its_lab_reading_defaults(
    otto_cli, reading_lab, monkeypatch
):
    """Under a top-level command that accepts, a leaf that says it does not reads defaults.

    For its whole run: the lab build, the reservation gate after it, and its body.
    """
    from otto.cli.invoke import ACCEPTS_OPTIONS_ATTR
    from otto.inventory import InventoryView
    from otto.session.lab import BuiltLab

    gate_reads: list[object] = []

    def build(projects: Any, labs: Any) -> Any:
        ReadingHost()
        return BuiltLab(lab=Lab(name="rig"), view=InventoryView.of_default(inventory=None))

    def gate(ctx: Any) -> None:
        gate_reads.append(otto.get_options(Acme))

    monkeypatch.setattr("otto.session.lab.build_lab_and_view", build)
    monkeypatch.setattr(
        "otto.reservations.factory.build_reservation_gate",
        lambda *args, **kwargs: SimpleNamespace(identity=None),
    )
    monkeypatch.setattr("otto.cli.invoke.present_reservation_gate", gate)
    monkeypatch.setattr(plugin_commands.erase, ACCEPTS_OPTIONS_ATTR, False, raising=False)
    result = otto_cli(["--lab", "rig", "flash", "erase"], ACME_DEBUG=None)
    assert result.code == 0, result
    assert reading_lab == [Acme()], result
    assert gate_reads == [Acme()], result
    [received] = plugin_commands.RECEIVED
    assert received.debug_level == 0


@pytest.mark.usefixtures("lab_plugins")
@pytest.mark.parametrize("listing", ["--list-hosts", "--show-lab"])
@pytest.mark.parametrize("argv", [["flash", "erase"], ["cov", "get"]])
def test_a_root_listing_builds_its_lab_reading_defaults(otto_cli, reading_lab, listing, argv):
    """A root listing never runs the command after it, so nothing marks: as in 0.20.0."""
    register_options(Acme, verbs=[argv[0]])
    result = otto_cli(["--lab", "rig", listing, *argv], ACME_DEBUG=None)
    assert reading_lab == [Acme()], result
    assert result.code == 0, result


def _cli(argv: "list[str]") -> Any:
    """Run ``otto <argv>`` on the real app in a fresh context, which nothing installed answers."""
    from otto.cli.main import app

    env = {"OTTO_LAB": "", "ACME_DEBUG": None, "COLUMNS": "200"}
    return contextvars.Context().run(CliRunner().invoke, app, argv, env=env, prog_name="otto")


@pytest.fixture
def one_project(monkeypatch) -> None:
    install_composed(monkeypatch, fake_bootstrap_result([fake_project("a")]))


@pytest.mark.usefixtures("one_project")
def test_the_body_of_an_accepting_command_reads_its_values_after_the_mark_clears():
    with plugin_commands.registered(accepts_options=True):
        register_options(Acme, verbs=["flash"])
        result = _cli(["flash", "erase", "--debug-level", "3"])
        assert result.exit_code == 0, (result.output, result.exception)
        [received] = plugin_commands.RECEIVED
        assert received.debug_level == 3


@pytest.mark.usefixtures("one_project")
def test_a_read_in_a_groups_callback_raises_when_a_child_follows():
    with plugin_commands.registered_board(accepts_options=True):
        result = _cli(["board", "status"])
        [refused] = plugin_commands.GROUP_READS
        assert isinstance(refused, OptionsNotAvailableError), result.output
        assert PENDING in str(refused)
        assert plugin_commands.RECEIVED == []


@pytest.mark.usefixtures("one_project")
def test_a_group_that_runs_with_no_child_reads_defaults_in_its_callback():
    register_options(Acme, verbs=["board"])
    with plugin_commands.registered_board(accepts_options=True):
        result = _cli(["board"])
        assert result.exit_code == 0, (result.output, result.exception)
        assert [Acme()] == plugin_commands.GROUP_READS
        refused = _cli(["board", "--debug-level", "3"])
        assert refused.exit_code == 2, refused.output
        assert "No such option" in refused.output


@pytest.mark.usefixtures("one_project")
def test_a_group_callback_under_a_command_that_does_not_accept_reads_defaults():
    with plugin_commands.registered_board(accepts_options=False):
        result = _cli(["board", "status"])
        assert result.exit_code == 0, (result.output, result.exception)
        assert [Acme()] == plugin_commands.GROUP_READS


@pytest.mark.usefixtures("one_project")
@pytest.mark.parametrize("registration", [True, False])
def test_a_groups_callback_is_judged_by_its_registration_not_by_the_child_that_follows(
    monkeypatch, registration
):
    """The group's registration decides its callback's read; the child's own marker does not.

    The root group holds the registration's mark until the child is invoked,
    so a child that says the opposite of its registration changes nothing
    in the group's callback.
    """
    monkeypatch.setattr(
        plugin_commands.status, ACCEPTS_OPTIONS_ATTR, not registration, raising=False
    )
    with plugin_commands.registered_board(accepts_options=registration):
        result = _cli(["board", "status"])
        [read] = plugin_commands.GROUP_READS
        if registration:
            assert isinstance(read, OptionsNotAvailableError), result.output
        else:
            assert read == Acme(), result.output


@pytest.mark.usefixtures("one_project")
def test_a_lab_free_command_under_an_accepting_group_reads_defaults(monkeypatch, tmp_path):
    seen: list[object] = []

    def verify(binary: Any, gcno_dir: Any) -> "list[object]":
        seen.append(otto.get_options(Acme))
        return []

    register_options(Acme, verbs=["cov"])
    monkeypatch.setattr("otto.coverage.stamp_guard.verify_stamps", verify)
    result = _cli(["cov", "check-stamp", str(tmp_path / "app"), "--gcno-dir", str(tmp_path)])
    assert result.exit_code == 0, (result.output, result.exception)
    assert seen == [Acme()]


def test_ctx_verb_is_the_top_level_command(monkeypatch):  # §6 item 8
    """``otto cov get``'s body runs under a context whose verb is ``"cov"``."""
    from otto.inventory import InventoryView
    from otto.session.lab import BuiltLab

    seen: list[object] = []

    async def got(**kwargs: Any) -> None:
        seen.append(otto.get_context().verb)

    patch_bootstrap(monkeypatch, [])
    monkeypatch.setattr(
        "otto.session.lab.build_lab_and_view",
        lambda projects, labs: BuiltLab(
            lab=Lab(name="rig"), view=InventoryView.of_default(inventory=None)
        ),
    )
    monkeypatch.setattr("otto.cli.invoke.ensure_cli_session", lambda ctx: None)
    monkeypatch.setattr("otto.coverage.get.get_coverage", got)
    monkeypatch.setattr("otto.cli.cov._render_get_report", lambda report: None)
    result = _cli(["--lab", "rig", "-R", "cov", "get"])
    assert seen == ["cov"], (result.output, result.exception)
