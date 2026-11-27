"""The completion cache records every options class a command may offer, and every flag.

``otto.cli.registry.collect_option_candidates`` describes each class on its
own, from the project instruction table and the registrations, never from a
command: one row list per class (each parameter's row is the one a tree node
gives it, ``otto.config.completion_tree.param_rows``) and one use per
occurrence. Each covered leaf of the shim's tree names the uses that cover it
(``otto.cli.invoke.covering_uses``). ``collect_cli_commands`` records a
command so a stub parses a line as the live command does.
"""

import contextlib
import enum
import inspect
from collections.abc import Generator
from typing import Annotated, Any

import pytest
import typer

import otto
from otto.cli.invoke import ACCEPTS_OPTIONS_ATTR
from otto.cli.main import PENDING_SUBCMD_ARGS_KEY, _completion_collectors, app
from otto.cli.registry import (
    CLI_COMMANDS,
    collect_cli_commands,
    collect_option_candidates,
    register_cli_command,
)
from otto.config.completion_tree import _context, _param, param_rows, serialize_tree
from otto.instructions import instruction
from otto.params import Registration, options_key, options_params, register_options
from otto.project import ProjectActions, register_project_actions
from otto.registry import registering_project
from tests._fixtures import plugin_commands
from tests._fixtures.bootstrap_seam import fake_bootstrap_result, install_composed
from tests._fixtures.fake_project import fake_project
from tests._fixtures.scope_worlds import B_FLAGS
from tests._fixtures.warm_tab import INSTALLERS


class Mode(enum.Enum):
    fast = "fast"
    slow = "slow"


def _declared(incomplete: str) -> "list[str]":  # pragma: no cover — never run by a write
    return []


_declared.__completion_source__ = {"kind": "payload", "key": "hosts"}  # ty: ignore[unresolved-attribute]


def _undeclared(incomplete: str) -> "list[str]":  # pragma: no cover — never run by a write
    return []


@otto.options
class Shape:
    """One field per row feature: an alias, an enum, a tuple and two completers."""

    debug: Annotated[int, typer.Option("--debug", "-d", help="Debug level.")] = 0
    mode: Annotated[Mode, typer.Option(help="How fast.")] = Mode.fast
    size: Annotated[tuple[int, int], typer.Option(help="Width and height.")] = (1, 1)
    source: Annotated[str, typer.Option(autocompletion=_declared, help="Declared.")] = ""
    undeclared: Annotated[str, typer.Option(autocompletion=_undeclared, help="Live.")] = ""


@otto.options
class Shared:
    reach: Annotated[int, typer.Option(help="How far.")] = 0


@otto.options
class Healthy:
    speed: Annotated[int, typer.Option(help="How fast.")] = 1


@otto.options
class OtherDebug:
    """Spells ``--debug-level``, as :class:`~tests._fixtures.plugin_commands.Acme` does."""

    debug_level: Annotated[int, typer.Option(help="Another debug level.")] = 0


def _register(cls: "type | str", verbs: "list[str]", *, project: "str | None") -> None:
    with registering_project(project) if project else contextlib.nullcontext():
        register_options(cls, verbs=verbs)


def _declare_body(project: str, name: str, options_cls: type) -> None:
    """Register, as *project*, actions whose one method is the body of *name* over *options_cls*."""

    @instruction(name, options=options_cls, help="h")
    async def body(self, opts: options_cls) -> None: ...  # ty: ignore[invalid-type-form]

    cls = type(f"{project.title()}Actions", (ProjectActions,), {name: body})
    with registering_project(project):
        register_project_actions(cls)


@pytest.fixture
def plugins(monkeypatch) -> "Generator[Any]":
    """The fixture plugin commands registered as accepting, with projects ``a`` and ``b``."""
    install_composed(monkeypatch, fake_bootstrap_result([fake_project("a"), fake_project("b")]))
    with plugin_commands.registered(accepts_options=True):
        yield plugin_commands


def _uses(found: "dict[str, Any]", *classes: "type | str") -> "list[tuple[str, Any, list[str]]]":
    """The uses of *classes*, in order, as ``(key, project, entries)``."""
    keys = {options_key(cls) for cls in classes}
    return [(u["key"], u["project"], u["entries"]) for u in found["uses"] if u["key"] in keys]


def _walk() -> "tuple[dict[str, Any], dict[str, Any]]":
    """Serialise otto's tree as a cache write does; return it with the candidates it indexes."""
    cli = _completion_collectors()
    tree = serialize_tree(
        typer.main.get_command(app),
        pending_key=PENDING_SUBCMD_ARGS_KEY,
        leaf_candidates=cli.leaf_candidates,
    ).tree
    return tree, cli.option_candidates()


def _named(node: "dict[str, Any]", found: "dict[str, Any]") -> "list[tuple[str, Any, list[str]]]":
    """The uses *node* names, as ``(key, project, entries)``."""
    uses = found["uses"]
    return [
        (uses[i]["key"], uses[i]["project"], uses[i]["entries"]) for i in node.get("candidates", [])
    ]


def _node(tree: "dict[str, Any]", *path: str) -> "dict[str, Any]":
    node = tree
    for name in path:
        node = node["commands"][name]
    return node


def _assert_names_its_bodies(
    node: "dict[str, Any]", found: "dict[str, Any]", *bodies: "tuple[str, Any, list[str]]"
) -> None:
    """A project instruction's node: no flag of its own, its body classes' uses as candidates.

    The cache answers as the live offer does, which attaches a project
    instruction's body classes (``OwnOptions.bodies``) for each invocation.
    Typer's completion installers are left out: a ``run`` leaf carries them
    whatever its options (Refs #1383).
    """
    own = [p["name"] for p in node["params"] if not set(p["flags"]) & set(INSTALLERS)]
    assert own == ["help"]
    named = _named(node, found)
    assert [use for use in bodies if use in named] == list(bodies)


# --- the uses ----------------------------------------------------------------------------


def test_each_registration_and_each_body_is_one_use_with_its_project(plugins):
    _declare_body("a", "provision", Healthy)
    _register(Shape, ["flash"], project="b")
    _register(OtherDebug, ["burn"], project=None)

    found = collect_option_candidates()
    assert _uses(found, Healthy, Shape, OtherDebug) == [
        (options_key(Healthy), "a", ["run provision"]),
        (options_key(Shape), "b", ["flash"]),
        (options_key(OtherDebug), None, ["burn"]),
    ]


def test_one_class_on_two_instruction_bodies_of_different_projects_keeps_both_uses(plugins):
    _declare_body("a", "provision", Shared)
    _declare_body("b", "deploy", Shared)

    tree, found = _walk()
    key = options_key(Shared)
    assert list(found["rows"]).count(key) == 1
    assert _uses(found, Shared) == [(key, "a", ["run provision"]), (key, "b", ["run deploy"])]
    _assert_names_its_bodies(_node(tree, "run", "provision"), found, (key, "a", ["run provision"]))
    _assert_names_its_bodies(_node(tree, "run", "deploy"), found, (key, "b", ["run deploy"]))


def test_a_body_class_that_is_also_registered_for_a_plugin_command_keeps_both_uses(plugins):
    _declare_body("a", "provision", Shared)
    _register(Shared, ["flash"], project="b")

    tree, found = _walk()
    key = options_key(Shared)
    assert [row["name"] for row in found["rows"][key]] == ["reach"]
    assert _uses(found, Shared) == [(key, "a", ["run provision"]), (key, "b", ["flash"])]
    provision = _node(tree, "run", "provision")
    _assert_names_its_bodies(provision, found, (key, "a", ["run provision"]))
    assert (key, "b", ["flash"]) not in _named(provision, found)
    assert _named(_node(tree, "flash", "erase"), found) == [(key, "b", ["flash"])]


def test_one_class_that_cannot_be_imported_costs_no_other_class_its_record(plugins):
    _register("nomod:Cls", ["flash"], project="a")
    _register(Healthy, ["flash"], project="b")

    found = collect_option_candidates()
    assert _uses(found, "nomod:Cls", Healthy) == [(options_key(Healthy), "b", ["flash"])]
    assert options_key("nomod:Cls") not in found["rows"]
    assert [row["name"] for row in found["rows"][options_key(Healthy)]] == ["speed"]


def test_two_colliding_classes_are_both_recorded(plugins):
    _register(plugin_commands.Acme, ["flash"], project="a")
    _register(OtherDebug, ["flash erase"], project="b")

    tree, found = _walk()
    assert _named(_node(tree, "flash", "erase"), found) == [
        (options_key(plugin_commands.Acme), "a", ["flash"]),
        (options_key(OtherDebug), "b", ["flash erase"]),
    ]


# --- one row form ----------------------------------------------------------------------


def _shape_rows(plugins_fixture: Any) -> "dict[str, dict[str, Any]]":
    _register(Shape, ["flash"], project="a")
    return {row["name"]: row for row in collect_option_candidates()["rows"][options_key(Shape)]}


def test_a_row_is_the_row_the_tree_gives_the_same_parameter(plugins):
    rows = _shape_rows(plugins)

    def plain(**_: Any) -> None: ...

    params = options_params(Shape)
    plain.__signature__ = inspect.Signature(params)  # ty: ignore[unresolved-attribute]
    plain.__annotations__ = {p.name: p.annotation for p in params}
    holder = typer.Typer()
    holder.command()(plain)
    command = typer.main.get_command(holder)
    ctx = _context(command, "plain", None)
    built = [p for p in command.params if p.name in rows]
    assert [p.name for p in built] == list(rows)
    for param in built:
        assert rows[param.name] == _param(param, ctx, "plain", []), param.name


def test_a_tuple_option_records_its_arity(plugins):
    size = _shape_rows(plugins)["size"]
    assert (size["nargs"], size["takes_value"]) == (2, True)


def test_aliases_are_recorded(plugins):
    assert _shape_rows(plugins)["debug"]["flags"] == ["--debug", "-d"]


def test_an_enum_option_records_its_choices(plugins):
    source = _shape_rows(plugins)["mode"]["source"]
    assert source["kind"] == "static"
    assert source["values"] == ["fast", "slow"]


def test_a_declared_completion_source_is_recorded_and_an_undeclared_function_is_live(plugins):
    rows = _shape_rows(plugins)
    assert rows["source"]["source"] == {"kind": "payload", "key": "hosts"}
    assert rows["undeclared"]["source"] == {"kind": "live"}


# --- the tree --------------------------------------------------------------------------


def test_every_accepting_leaf_names_its_candidates_and_others_name_none(plugins):
    _register(Healthy, ["cov"], project="a")
    _declare_body("b", "provision", Shared)

    tree, found = _walk()
    healthy = (options_key(Healthy), "a", ["cov"])
    assert healthy in _named(_node(tree, "cov", "get"), found)
    for path in (["cov", "check-stamp"], ["link"], ["monitor"]):
        node = _node(tree, *path)
        leaves = [node] if not node["commands"] else list(node["commands"].values())
        assert all("candidates" not in leaf for leaf in leaves), path
    _assert_names_its_bodies(
        _node(tree, "run", "provision"), found, (options_key(Shared), "b", ["run provision"])
    )


def test_a_node_holds_no_candidate_row(plugins):
    _register(Shape, ["flash"], project="a")

    tree, found = _walk()
    erase = _node(tree, "flash", "erase")
    assert [p["name"] for p in erase["params"]] == ["help"]
    assert all(isinstance(i, int) for i in erase["candidates"])
    assert [row["name"] for row in found["rows"][options_key(Shape)]][:2] == ["debug", "mode"]


def test_the_writer_reads_no_selection(scope_world, otto_cli, monkeypatch):
    stored: "list[dict[str, Any]]" = []
    monkeypatch.setattr("otto.cli.main._cache_is_writable", lambda projects: True)
    monkeypatch.setattr("otto.config.completion_cache.cache_is_stale", lambda *a, **k: True)
    monkeypatch.setattr(
        "otto.config.completion_cache.write_sections",
        lambda projects, payloads, **k: stored.append(payloads["names"]["option_candidates"]),
    )

    # Control: under this selection the live path drops project b's flags.
    live = otto_cli(["flash", "erase", "--help"], OTTO_LAB="alpha1")
    assert live.code == 0, live
    assert "--a-level" in live.text
    assert "--b-level" not in live.text
    # Root help is the rebuild; its lab selection comes from OTTO_LAB, the -l option's variable.
    run = otto_cli(["--help"], OTTO_LAB="alpha1")
    assert run.code == 0, run
    assert stored, "root help did not rebuild the cache"
    assert ("b", ["flash"]) in [
        (u["project"], u["entries"]) for u in stored[-1]["uses"] if u["key"] == B_FLAGS
    ]


# --- the stub records ------------------------------------------------------------------


def _record(name: str) -> "dict[str, Any]":
    return next(c for c in collect_cli_commands() if c["name"] == name)


def test_a_function_loader_command_records_its_options_and_its_accepts_bit(plugins):
    for name, option in (("burn", "loud"), ("lazyburn", "count")):
        record = _record(name)
        assert record["accepts_options"] is True, name
        assert [o["name"] for o in record["options"]] == [option], name


zgroup = typer.Typer(name="zgroup", help="A group with its own flag.")


@zgroup.callback()
def _zgroup(verbose: bool = False) -> None:
    """The group."""


@zgroup.command("one")
def _one(target: str) -> None:
    """Takes an argument."""


@zgroup.command("two")
def _two() -> None:
    """Takes nothing."""


accepting_group = typer.Typer(name="zaccepts", help="A lab-free group with one accepting leaf.")


@accepting_group.command("loud")
def _loud() -> None:
    """Accepts, by its own marker."""


setattr(_loud, ACCEPTS_OPTIONS_ATTR, True)

sub_group = typer.Typer(name="nested", help="Holds the accepting leaf.")
sub_group.command("leaf")(_loud)


@sub_group.command("quiet")
def _quiet() -> None:
    """Says nothing, so the sub-group stays a group."""


accepting_group.add_typer(sub_group)


def test_a_group_record_accepts_when_a_leaf_beneath_it_does():
    register_cli_command("zaccepts", accepting_group, lab_free=True)
    try:
        record = _record("zaccepts")
    finally:
        CLI_COMMANDS.unregister(name="zaccepts")
    nested = next(c for c in record["commands"] if c["name"] == "nested")
    assert nested["accepts_options"] is True


def test_a_leaf_with_bodies_that_does_not_accept_gets_the_same_answer_from_the_cache_and_live():
    """One gate answers both: a leaf that does not accept is offered its own bodies only.

    The leaf is ``flash erase`` with a body class of its own and its marker
    saying it does not accept; ``flash`` itself accepts, and a class is
    registered for it. The cache's walk (``covering_uses``) and a live TAB
    (``live_candidates``) both offer the body and not the registration.
    """
    from otto.cli.invoke import (
        ACCEPTS_OPTIONS_ATTR,
        OWN_OPTIONS_ATTR,
        OwnOptions,
        covering_uses,
        live_candidates,
        offer_gate,
    )
    from otto.params import OptionsOrigin

    holder = typer.Typer(add_completion=False)

    @holder.command("erase")
    def erase() -> None:
        """Has a body class of its own, and does not accept."""

    @holder.command("verify")
    def verify() -> None:
        """Makes the holder a group."""

    setattr(erase, OWN_OPTIONS_ATTR, OwnOptions(bodies=lambda: [OptionsOrigin(Healthy, "a")]))
    setattr(erase, ACCEPTS_OPTIONS_ATTR, False)
    _register(Shape, ["flash"], project="b")
    with plugin_commands.registered(accepts_options=True):
        group = typer.main.get_command(holder)
        leaf = group.commands["erase"]
        root_ctx = _context(typer.main.get_command(app), "otto", None)
        group_ctx = _context(group, "flash", root_ctx)
        ctx = _context(leaf, "erase", group_ctx)
        gate = offer_gate(leaf, ctx, CLI_COMMANDS.get("flash"))
        assert (gate.path, gate.accepts) == (["flash", "erase"], False)
        uses = [
            Registration(key=options_key(Healthy), entries=("flash erase",), project="a"),
            Registration(key=options_key(Shape), entries=("flash",), project="b"),
        ]
        cached = [uses[i].key for i in covering_uses(leaf, ctx, uses)]
        live = [c.key for c in live_candidates(gate.path, gate.own, covered=gate.accepts)]
    assert cached == live == [options_key(Healthy)]


def test_a_stub_record_holds_arguments_and_a_groups_own_parameters():
    register_cli_command("zgroup", zgroup, lab_free=True)
    try:
        record = _record("zgroup")
    finally:
        CLI_COMMANDS.unregister(name="zgroup")
    assert [o["name"] for o in record["options"]] == ["verbose"]
    assert record["accepts_options"] is False
    one = next(c for c in record["commands"] if c["name"] == "one")
    [target] = one["options"]
    assert target["name"] == "target"
    assert target["row"]["flags"] == []
    assert target["row"]["takes_value"] is True
    assert one["accepts_options"] is False


# --- the shared readings -----------------------------------------------------------------


def test_a_built_parameters_spellings_are_read_once_for_the_tree_and_the_offer():
    """``built_spellings`` is the one reading: a row's flags are it, and an argument has none."""
    from otto.config._typer import built_spellings

    holder = typer.Typer(add_completion=False)

    @holder.command()
    def cmd(target: str, loud: Annotated[bool, typer.Option("--loud/--quiet", "-L")] = False):
        """A command with an argument and a two-way flag."""

    command = typer.main.get_command(holder)
    target, loud = command.params
    assert built_spellings(target) == []
    assert built_spellings(loud) == ["--loud", "-L", "--quiet"]
    rows = param_rows(command.params)
    assert rows["loud"]["flags"] == built_spellings(loud)
    assert rows["target"]["flags"] == []


def test_a_body_use_names_the_command_its_instruction_is_hung_under():
    """The instructions' group is the registered command whose loader is ``run_app``."""
    from otto.cli.run import run_app

    name = run_app.info.name
    assert name in CLI_COMMANDS
    assert CLI_COMMANDS.peek(name=name).loader == "otto.cli.run:run_app"
