"""A command's options are prepared once, when it is invoked, before its body.

:func:`~otto.cli.invoke.prepare_command_options` runs in the preamble: the
values of the classes covering the command (and its own class, for a
``@cli_command(options=...)``) are converted as a function declaring them
receives them, recorded, built and validated, so a hook reads them, a bad
value is a usage error before the body, ``--dry-run`` shows and masks them,
and the command's function receives only its own parameters.
"""

import contextlib
import dataclasses
import enum
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import pydantic
import pytest
import typer
from typer.testing import CliRunner

import otto
from otto.cli.invoke import prepared_options
from otto.cli.main import app
from otto.cli.registry import CLI_COMMANDS, cli_command
from otto.config._typer import get_current_context
from otto.params import register_options
from otto.registry import registering_project
from tests._fixtures import plugin_commands
from tests._fixtures.bootstrap_seam import fake_bootstrap_result, install_composed
from tests._fixtures.dispatch import DispatchRunner
from tests._fixtures.entry_runner import complete
from tests._fixtures.fake_project import fake_project
from tests._fixtures.plugin_commands import Acme

runner = CliRunner()

ENV = {"OTTO_LAB": "", "ACME_DEBUG": None, "COLUMNS": "200"}


class Mode(enum.Enum):
    fast = "fast"
    slow = "slow"


@dataclasses.dataclass
class Shape:
    """A plain dataclass: nothing coerces its values, so their shape is the parser's work."""

    out: Path = Path("unset")
    mode: Mode = Mode.fast
    tags: Annotated["list[str]", typer.Option()] = dataclasses.field(default_factory=list)
    size: "tuple[int, int]" = (0, 0)


@dataclasses.dataclass
class OtherLevel:
    """Unregistered, and shares ``debug_level`` with :class:`Acme` by name."""

    debug_level: int = 9


@otto.options
class Bounded:
    depth: Annotated[int, pydantic.Field(ge=0), typer.Option(help="How deep.")] = 0


@otto.options
class Tok:
    token: Annotated[pydantic.SecretStr, typer.Option(help="A token.")] = pydantic.SecretStr("")


@otto.options
class Variant:
    variant: Annotated[str, typer.Option(help="Which board variant.")] = "a"


BUILT: "list[int]" = []


@otto.options
class Counted:
    n: Annotated[int, typer.Option(help="A count.")] = 0

    def __post_init__(self) -> None:
        BUILT.append(self.n)


def _register(cls: type, verbs: "list[str]", *, project: str = "a") -> None:
    with registering_project(project):
        register_options(cls, verbs=verbs)


@pytest.fixture
def plugins(monkeypatch):
    """The fixture plugin commands registered as accepting, with two projects discovered."""
    install_composed(monkeypatch, fake_bootstrap_result([fake_project("a"), fake_project("b")]))
    before = set(CLI_COMMANDS.names())
    BUILT.clear()
    with plugin_commands.registered(accepts_options=True):
        yield plugin_commands
    for name in set(CLI_COMMANDS.names()) - before:
        CLI_COMMANDS.unregister(name=name)


@pytest.fixture
def reads(monkeypatch) -> "Callable[..., list[dict[type, Any]]]":
    """``reads(*classes)``: each fixture body also reads ``otto.get_options`` of *classes*."""

    def reading(*classes: type) -> "list[dict[type, Any]]":
        seen: list[dict[type, Any]] = []
        record = plugin_commands._record_call

        def recorded(ctx: typer.Context, command: str, **kwargs: Any) -> None:
            seen.append({cls: otto.get_options(cls) for cls in classes})
            record(ctx, command, **kwargs)

        monkeypatch.setattr(plugin_commands, "_record_call", recorded)
        return seen

    return reading


def _otto(argv: "list[str]", **env: "str | None") -> Any:
    return runner.invoke(app, argv, env={**ENV, **env}, prog_name="otto")


def _ok(argv: "list[str]", **env: "str | None") -> Any:
    result = _otto(argv, **env)
    assert result.exit_code == 0, (result.output, result.exception)
    return result


# --- a plugin command ---------------------------------------------------------------


def test_the_hook_reads_the_value_given_on_a_plugin_command(plugins):
    _register(Acme, ["flash"])
    _ok(["flash", "image", "write", "--force", "--debug-level", "3"])
    [received] = plugin_commands.RECEIVED
    assert received.kwargs == {"force": True}
    assert received.debug_level == 3


def test_a_bad_value_is_a_usage_error_before_the_body(plugins):
    _register(Bounded, ["flash"])
    result = _otto(["flash", "erase", "--depth", "-1"])
    assert result.exit_code == 2, result.output
    assert plugin_commands.RECEIVED == []


def test_the_dry_run_shows_and_masks_the_covering_options(plugins):
    _register(Acme, ["flash"])
    _register(Tok, ["flash"])
    result = _ok(["-n", "flash", "erase", "--debug-level", "3", "--token", "s3cret"])
    lines = result.output.splitlines()
    [would_run] = [line for line in lines if "would run:" in line]
    assert "--debug-level 3" in would_run
    assert "--token <hidden>" in would_run
    assert any("Acme: debug_level=3" in line for line in lines), result.output
    assert any("Tok: token=<hidden>" in line for line in lines), result.output
    assert "s3cret" not in result.output
    assert plugin_commands.RECEIVED == []


def test_attached_values_are_normalised_as_the_commands_own(plugins, reads):  # Review Focus 1
    _register(Shape, ["burn"])
    seen = reads(Shape)
    _ok(["burn", "--out", "x", "--mode", "slow", "--tags", "a", "--tags", "b", "--size", "1", "2"])
    assert seen == [{Shape: Shape(out=Path("x"), mode=Mode.slow, tags=["a", "b"], size=(1, 2))}]
    assert plugin_commands.RECEIVED[0].kwargs == {"loud": False}


def test_a_flag_given_by_its_environment_variable_counts_as_given(plugins, reads):  # Review Focus 2
    _register(Acme, ["flash"])
    seen = reads(OtherLevel)
    _ok(["flash", "erase"], ACME_DEBUG="5")
    assert seen == [{OtherLevel: OtherLevel(debug_level=5)}]
    assert plugin_commands.RECEIVED[0].debug_level == 5


def test_an_unset_covering_flag_is_given_to_nobody(plugins, reads):
    _register(Acme, ["flash"])
    seen = reads(OtherLevel)
    _ok(["flash", "erase"])
    assert seen == [{OtherLevel: OtherLevel(debug_level=9)}]


def test_a_lab_free_command_that_says_it_accepts_holds_its_values_for_one_run(plugins):
    _register(Acme, ["flash"])
    _ok(["flash", "erase", "--debug-level", "3"])
    _ok(["flash", "erase"])
    assert [r.debug_level for r in plugin_commands.RECEIVED] == [3, 0]
    assert otto.get_options(Acme).debug_level == 0


def test_a_class_covering_one_command_reads_defaults_under_another(  # §9 item 2
    monkeypatch, plugins
):
    from otto.cli.cov import cov_app

    _register(Acme, ["flash"])
    _register(Variant, ["cov"])
    seen: list[tuple[int, str]] = []

    async def got(**kwargs: Any) -> None:
        seen.append((otto.get_options(Acme).debug_level, otto.get_options(Variant).variant))

    monkeypatch.setattr("otto.coverage.get.get_coverage", got)
    monkeypatch.setattr("otto.cli.cov._render_get_report", lambda report: None)
    result = DispatchRunner().invoke(
        cov_app,
        ["get", "--variant", "b"],
        env={**ENV, "ACME_DEBUG": "5"},
        spec_name="cov",
        accepts_options=True,
    )
    assert result.exit_code == 0, (result.output, result.exception)
    assert seen == [(0, "b")]

    monkeypatch.setenv("ACME_DEBUG", "5")
    assert otto.get_options(Acme).debug_level == 0


def test_completion_and_help_prepare_nothing(plugins):
    _register(Counted, ["flash"])
    assert "--n" in _ok(["flash", "erase", "--help"]).output
    assert "--n" in complete("otto flash erase --", ENV)
    assert BUILT == []


def test_one_validator_run_per_command(plugins, reads):
    _register(Counted, ["flash"])
    seen = reads(Counted)
    _ok(["flash", "erase", "--n", "2"])
    assert BUILT == [2]
    assert seen[0][Counted].n == 2


# --- @cli_command(options=...) ------------------------------------------------------


@dataclasses.dataclass
class OwnShape:
    mode: Mode = Mode.fast
    tags: Annotated["list[str]", typer.Option()] = dataclasses.field(default_factory=list)


HANDED: "list[dict[str, Any]]" = []


@contextlib.contextmanager
def _shaped_command():
    """``otto shaped``: a lab-free ``@cli_command(options=OwnShape)`` that accepts."""
    HANDED.clear()

    @cli_command(name="shaped", options=OwnShape, lab_free=True, accepts_options=True)
    async def shaped(opts: OwnShape, count: int = 1) -> None:
        """A command with its own options class."""
        ctx = get_current_context()
        HANDED.append(
            {
                "opts": opts,
                "count": count,
                "prepared": prepared_options(ctx),
                "root_prepared": prepared_options(ctx.find_root()),
                "debug_level": otto.get_options(Acme).debug_level,
            }
        )

    yield


def test_an_own_class_field_reaches_the_prepared_instance_as_the_function_receives_it(plugins):
    _register(Acme, ["shaped"])
    with _shaped_command():
        _ok(["shaped", "--mode", "slow", "--tags", "a", "--tags", "b"])
    [handed] = HANDED
    prepared = handed["prepared"]
    assert prepared is not None
    assert prepared.instances[OwnShape] == OwnShape(mode=Mode.slow, tags=["a", "b"])
    assert prepared.instances[OwnShape] is handed["opts"]
    assert list(prepared.instances) == [OwnShape, Acme]


def test_a_cli_command_with_its_own_class_also_takes_a_covering_class(plugins):  # §5.5 row
    _register(Acme, ["shaped"])
    with _shaped_command():
        assert "--debug-level" in _ok(["shaped", "--help"]).output
        _ok(["shaped", "--mode", "slow", "--tags", "x", "--count", "2", "--debug-level", "3"])
    [handed] = HANDED
    assert handed["opts"] == OwnShape(mode=Mode.slow, tags=["x"])
    assert handed["count"] == 2
    assert handed["debug_level"] == 3
    assert handed["prepared"].recorded.given == frozenset({"mode", "tags", "debug_level"})


def test_only_the_invoked_commands_context_reads_its_preparation(plugins):
    _register(Acme, ["flash", "shaped"])
    _ok(["flash", "erase", "--debug-level", "3"])
    erase = plugin_commands.CONTEXTS[-1]
    assert erase.parent is not None
    prepared = prepared_options(erase)
    assert prepared is not None
    assert prepared.instances[Acme].debug_level == 3
    assert prepared_options(erase.parent) is None
    assert prepared_options(erase.find_root()) is None

    with _shaped_command():  # a top-level leaf: the root's path is the leaf's
        _ok(["shaped", "--tags", "x"])
    [handed] = HANDED
    assert handed["prepared"] is not None
    assert handed["root_prepared"] is None
