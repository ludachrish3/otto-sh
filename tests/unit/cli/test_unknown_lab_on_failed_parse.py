"""A command line that names an unknown lab and fails its parse reports the lab.

The root group's ``No such option`` handler calls
:func:`~otto.cli.invoke.unknown_lab_on_failed_parse` first: with a lab selected
it runs the load step of the lab build (``otto.session.lab.load_lab_and_view``),
and only an unknown lab replaces the parser's error. A line that parses never
reaches it, so it asks the lab sources no earlier than before. The world is
``tests/_fixtures/scope_worlds.py``'s, with ``flash`` using the lab; its source
declares ``alpha1`` and ``beta1``, and every run goes through ``entry()``.
"""

from typing import Any

import pytest
import typer
from typer.core import TyperCommand
from typer.testing import CliRunner

from otto.cli import invoke
from otto.cli.registry import CLI_COMMANDS, register_cli_command
from otto.labs.drops import Diagnostic
from otto.labs.errors import LabRepositoryError, LabUnavailableError
from otto.session import lab as session_lab
from tests._fixtures import plugin_commands
from tests._fixtures.entry_runner import complete
from tests._fixtures.scope_worlds import ENV, UNKNOWN_LAB

LABB = UNKNOWN_LAB.format(name="labb")
"""What the world's source says of ``labb``, which it does not declare."""


@pytest.fixture
def lab_world(scope_world):
    """The scope world, with ``flash`` using the lab, so a line that parses loads it."""
    CLI_COMMANDS.unregister(name="flash")
    register_cli_command("flash", plugin_commands.flash_app, lab_free=False, accepts_options=True)
    return scope_world


def _watch(monkeypatch, owner: Any, name: str) -> "list[Any]":
    """Record each call of ``owner.<name>``, which still answers."""
    calls: "list[Any]" = []
    function = getattr(owner, name)

    def watched(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        return function(*args, **kwargs)

    monkeypatch.setattr(owner, name, watched)
    return calls


def _reports_labb(run: Any) -> None:
    assert run.code == 1, run
    assert LABB in run.text, run
    assert run.text.count("error:") == 1, run
    assert "No such option" not in run.text, run
    assert plugin_commands.RECEIVED == []


def _without_the_check(monkeypatch, otto_cli, argv: "list[str]") -> Any:
    """``otto <argv>`` with the lab check taken out: the parser's bare error."""
    with monkeypatch.context() as patched:
        patched.setattr(invoke, "unknown_lab_on_failed_parse", lambda ctx: None)
        return otto_cli(argv)


# --- an unknown lab replaces the parser's error ------------------------------------


def test_an_unknown_lab_replaces_no_such_option_with_the_lab_loads_own_error(lab_world, otto_cli):
    bad = otto_cli(["-l", "labb", "flash", "erase", "--a-level", "1"])
    plain = otto_cli(["-l", "labb", "flash", "erase"])
    assert (bad.code, bad.stdout, bad.stderr) == (plain.code, plain.stdout, plain.stderr)
    _reports_labb(bad)


@pytest.mark.parametrize(
    "argv",
    [
        # The first error decides: alpha1 loads, labb does not. Neither puts b in scope.
        ["-l", "alpha1+labb", "flash", "erase", "--b-level", "1"],
        ["-l", "labb", "flash", "erase", "--a-level", "1", "--help"],
        ["-l", "labb", "flash", "erase", "--typo"],
        ["-l", "labb", "link", "list", "--typo"],
    ],
    ids=[
        "a_loading_lab_then_an_unknown_one",
        "with_help",
        "an_option_no_command_has",
        "not_accepting",
    ],
)
def test_the_lab_is_reported_whichever_option_failed(argv, lab_world, otto_cli, monkeypatch):
    checked = _watch(monkeypatch, invoke, "unknown_lab_on_failed_parse")
    _reports_labb(otto_cli(argv))
    assert len(checked) == 1


def test_otto_lab_with_an_unknown_name_is_reported_too(lab_world, otto_cli):
    bad = otto_cli(["flash", "erase", "--a-level", "1"], OTTO_LAB="labb")
    plain = otto_cli(["flash", "erase"], OTTO_LAB="labb")
    assert (bad.code, bad.stdout, bad.stderr) == (plain.code, plain.stdout, plain.stderr)
    _reports_labb(bad)


# --- every other outcome leaves the parser's error ----------------------------------


def test_a_lab_that_exists_and_does_not_match_is_the_bare_parser_error(
    lab_world, otto_cli, recording_repository, monkeypatch
):
    argv = ["-l", "beta1", "flash", "erase", "--a-level", "1"]
    run = otto_cli(argv)
    assert recording_repository.reads == ["build", "load_lab beta1"]
    assert run.code == 2, run
    assert "No such option" in run.text
    assert "lab" not in run.text.lower()
    assert "'a'" not in run.text
    assert "project" not in run.text
    bare = _without_the_check(monkeypatch, otto_cli, argv)
    assert (run.code, run.stdout, run.stderr) == (bare.code, bare.stdout, bare.stderr)


def _refuse_on_load(monkeypatch, repository: Any, refusals: "dict[str, Exception]") -> None:
    """Make *repository*'s load of each name in *refusals* raise its error."""
    load = repository.load_lab

    def load_lab(name: Any, **kwargs: Any) -> Any:
        if name in refusals:
            repository.reads.append(f"load_lab {name}")
            raise refusals[name]
        return load(name, **kwargs)

    monkeypatch.setattr(repository, "load_lab", load_lab)


@pytest.mark.parametrize(
    "case",
    [
        "out_of_reach",
        "source_unreadable",
        "first_out_of_reach_second_unknown",
        "anything_else",
        "no_lab",
    ],
)
def test_every_other_outcome_leaves_the_parser_error(
    case, lab_world, otto_cli, recording_repository, monkeypatch
):
    gone = LabUnavailableError(
        name="gone", diagnostic=Diagnostic(where="source world", reason="down"), declared=False
    )
    labs = {
        "out_of_reach": "gone",
        "source_unreadable": "labb",
        "first_out_of_reach_second_unknown": "gone+labb",
        "anything_else": "labb",
        "no_lab": None,
    }[case]
    if case == "source_unreadable":

        def unreadable(**_: Any) -> Any:
            raise LabRepositoryError("the world's source cannot be read")

        monkeypatch.setattr("otto.labs.sources.build_lab_sources", unreadable)
    elif case == "anything_else":
        _refuse_on_load(monkeypatch, recording_repository, {"labb": RuntimeError("broken")})
    else:
        _refuse_on_load(monkeypatch, recording_repository, {"gone": gone})
    argv = [*(["-l", labs] if labs else []), "flash", "erase", "--typo"]
    loads = _watch(monkeypatch, session_lab, "load_lab_and_view")

    run = otto_cli(argv)
    assert len(loads) == (0 if labs is None else 1)
    if case == "first_out_of_reach_second_unknown":
        assert recording_repository.reads == ["build", "load_lab gone"]
    assert run.code == 2, run
    assert "No such option: --typo" in run.text
    bare = _without_the_check(monkeypatch, otto_cli, argv)
    assert (run.code, run.stdout, run.stderr) == (bare.code, bare.stdout, bare.stderr)


# --- the failure path's cost -------------------------------------------------------


def test_the_failure_path_binds_nothing(lab_world, otto_cli, monkeypatch):
    bound: "list[str]" = []
    for target in (
        "otto.run_context.set_context",
        "otto.reservations.factory.build_reservation_gate",
        "otto.docker.compose.register_declared_container_hosts",
    ):
        monkeypatch.setattr(target, lambda *a, _t=target, **k: bound.append(_t))
    loads = _watch(monkeypatch, session_lab, "load_lab_and_view")

    # beta1 loads (a build would register its container hosts next); labb is unknown.
    assert otto_cli(["-l", "beta1", "flash", "erase", "--typo"]).code == 2
    _reports_labb(otto_cli(["-l", "labb", "flash", "erase", "--typo"]))
    assert len(loads) == 2
    assert bound == []


def test_a_sub_app_without_ottos_root_gives_the_parser_error(monkeypatch):
    loads = _watch(monkeypatch, session_lab, "load_lab_and_view")
    result = CliRunner().invoke(plugin_commands.flash_app, ["erase", "--typo"])
    assert result.exit_code == 2, result.output
    assert "No such option" in result.output

    # A root that is not otto's selects no lab, whatever its parameters hold.
    foreign = typer.Context(TyperCommand("sub"))
    foreign.params["labs"] = ["labb"]
    assert invoke.unknown_lab_on_failed_parse(foreign) is None
    assert loads == []


def test_a_line_that_parses_asks_the_lab_sources_no_earlier(
    lab_world, otto_cli, recording_repository, monkeypatch
):
    checked = _watch(monkeypatch, invoke, "unknown_lab_on_failed_parse")
    reads_at_offer: "list[list[str]]" = []
    offer = invoke.offered_options

    def watched(ctx: Any, spec: Any) -> Any:
        reads_at_offer.append(list(recording_repository.reads))
        return offer(ctx, spec)

    monkeypatch.setattr(invoke, "offered_options", watched)

    shown = otto_cli(["-l", "alpha1", "flash", "erase", "--a-level", "1", "--help"])
    assert shown.code == 0, shown
    assert recording_repository.reads == []

    assert "--a-level" in complete("otto -l alpha1 flash erase --a", ENV)
    assert recording_repository.reads == []

    dispatched = otto_cli(["-l", "alpha1", "flash", "erase", "--a-level", "1"])
    assert "No such option" not in dispatched.text, dispatched
    # The preamble's one load. Every offer the parse made came before it; the last
    # offer is the preamble preparing the options, after the load.
    assert recording_repository.reads == ["build", "load_lab alpha1"]
    *parsing, preparing = reads_at_offer
    assert parsing
    assert all(reads == [] for reads in parsing), reads_at_offer
    assert preparing == ["build", "load_lab alpha1"]
    assert checked == []
