"""A ``verbs=`` entry is checked against the commands, where the commands are known.

:func:`~otto.cli.registry.entry_error` is the one judgement. The startup check
(:func:`~otto.cli.registry.check_option_entries`) runs it for every entry once
per bootstrap result and judges a first word, a one-word entry and a ``"run
<name>"``; a two-word entry under any other group is judged when the group is
resolved (:func:`~otto.cli.registry.check_group_entries`). Each error is a
``BootstrapError`` on the result, attributed to the registering project, so the
existing printer and gate judge it. A registration made after the startup check
is judged when it is made, by the checker ``entry()`` installs
(:func:`~otto.cli.registry.refuse_bad_entries`).
"""

import contextlib
import os
import sys
from collections.abc import Callable, Generator
from pathlib import Path
from typing import Any

import pytest
import typer

import otto
from otto.cli.invoke import ACCEPTS_OPTIONS_ATTR
from otto.cli.registry import (
    CLI_COMMANDS,
    check_option_entries,
    entry_error,
    register_cli_command,
)
from otto.config.env import is_otto_env_var
from otto.params import OptionsRegistrationError, Registration, register_options, verbs_for
from otto.registry import registering_project
from tests._fixtures import plugin_commands
from tests._fixtures.bootstrap_seam import fake_bootstrap_result, install_composed
from tests._fixtures.fake_project import fake_project


def _class(name: str = "C") -> type:
    return type(name, (), {"__module__": __name__})


def _register(verbs: "list[str]", *, project: "str | None" = "a", name: str = "C") -> type:
    """Register a plain class for *verbs*, attributed to *project*."""
    cls = _class(name)
    with registering_project(project) if project else contextlib.nullcontext():
        register_options(cls, verbs=verbs)
    return cls


def _messages(result: Any) -> "list[str]":
    return [str(err) for err in result.errors if getattr(err, "source", None) == "options"]


@pytest.fixture
def project_a(tmp_path: Path) -> Any:
    return fake_project("a", root=tmp_path / "a")


@pytest.fixture
def result(monkeypatch: pytest.MonkeyPatch, project_a: Any) -> Any:
    """The published bootstrap result every ``entry()`` here answers with."""
    return install_composed(monkeypatch, fake_bootstrap_result([project_a]))


@contextlib.contextmanager
def _hung_on_run(name: str, **marker: Any) -> "Generator[None]":
    """Hang a command *name* on ``otto run`` for the block, carrying *marker*."""
    from otto.cli.run import run_app

    def burn() -> None:  # pragma: no cover — only listed and judged
        """Burn in the board."""

    for attr, value in marker.items():
        setattr(burn, attr, value)
    before = list(run_app.registered_commands)
    run_app.command(name)(burn)
    try:
        yield
    finally:
        run_app.registered_commands[:] = before


# --- the judgement, at startup ----------------------------------------------------


@pytest.mark.parametrize("word", ["link", "tunnel", "monitor"])
def test_an_entry_naming_a_command_that_does_not_accept_is_refused_without_importing_it(
    monkeypatch, word
):
    module = f"otto.cli.{word}"
    monkeypatch.delitem(sys.modules, module, raising=False)
    registration = Registration(key="m:C", entries=(word,), project="a")
    message = entry_error(registration, word, group_of=lambda _: None)
    assert message is not None
    assert "does not accept registered options" in message
    assert "run" in message
    assert module not in sys.modules


def test_an_unknown_first_word_suggests_the_close_match():
    message = entry_error(
        Registration(key="m:C", entries=("tset",), project=None), "tset", group_of=lambda _: None
    )
    assert message is not None
    assert "'tset'" in message
    assert "test" in message


@pytest.mark.parametrize("entry", ["cov", "host", "host install"])
def test_cov_and_host_pass(entry):
    registration = Registration(key="m:C", entries=(entry,), project=None)
    assert entry_error(registration, entry, group_of=lambda _: None) is None


def test_a_lazily_registered_single_command_is_judged_from_its_registration():
    register_cli_command("lazyoff", "tests_no_such_module_off:command", lab_free=True)
    register_cli_command("lazyon", "tests_no_such_module_on:command")
    off = Registration(key="m:C", entries=("lazyoff",), project=None)
    on = Registration(key="m:D", entries=("lazyon",), project=None)
    message = entry_error(off, "lazyoff", group_of=lambda _: None)
    assert message is not None
    assert "does not accept registered options" in message
    assert entry_error(on, "lazyon", group_of=lambda _: None) is None
    assert "tests_no_such_module_off" not in sys.modules
    assert "tests_no_such_module_on" not in sys.modules


def test_an_unknown_run_command_is_an_error_of_the_registering_project(project_a):
    bootstrap = fake_bootstrap_result([project_a])
    _register(["run instal"], project="a")
    check_option_entries(bootstrap)
    errors = [err for err in bootstrap.errors if err.source == "options"]
    assert len(errors) == 1
    assert errors[0].root == project_a.root
    assert isinstance(errors[0].__cause__, OptionsRegistrationError)
    assert "run instal" in str(errors[0])
    assert "install" in str(errors[0]).replace("run instal'", "")


def test_a_run_entry_may_name_a_command_hung_on_the_group(result, otto_cli):
    with _hung_on_run("burn"):
        _register(["run burn"])
        check_option_entries(result)
        assert _messages(result) == []
        assert otto_cli(["run", "--help"]).code == 0
        assert _messages(result) == []


def test_a_run_entry_may_name_a_command_of_a_nameless_sub_app_hung_on_the_group():
    """``run_app.add_typer(sub)`` merges ``sub``'s commands into ``otto run``, as Typer does."""
    from otto.cli.run import run_app

    sub = typer.Typer()

    @sub.command("leaf")
    def leaf() -> None:  # pragma: no cover — only listed and judged
        """Hung on run through a nameless sub-app."""

    before = list(run_app.registered_groups)
    run_app.add_typer(sub)
    try:
        group = typer.main.get_group(run_app)
        assert "leaf" in group.list_commands(typer.Context(group))
        registration = Registration(key="m:C", entries=("run leaf",), project=None)
        assert entry_error(registration, "run leaf", group_of=lambda _: None) is None
    finally:
        run_app.registered_groups[:] = before


def test_a_run_entry_naming_a_command_that_opted_out_is_an_error_when_run_is_resolved(
    result, otto_cli
):
    with _hung_on_run("burn", **{ACCEPTS_OPTIONS_ATTR: False}):
        _register(["run burn"], name="Burn")
        check_option_entries(result)
        assert _messages(result) == []
        run = otto_cli(["run", "--help"])
        assert run.code == 0
        [message] = _messages(result)
        assert "otto run burn does not accept registered options" in message
        assert run.stderr.count("does not accept registered options") == 1
        assert f"warning: {result.errors[-1]}" in run.stderr


def test_a_run_entry_naming_an_instruction_passes_when_run_is_resolved(result, otto_cli):
    _register(["run install"])
    assert otto_cli(["run", "--help"]).code == 0
    assert _messages(result) == []


# --- through entry() --------------------------------------------------------------


def test_the_error_is_fatal_for_a_project_that_takes_part_and_one_warning_otherwise(
    result, otto_cli
):
    _register(["tset"])
    with plugin_commands.registered():
        fatal = otto_cli(["flash", "erase"])
        assert fatal.code == 1
        assert "failed to load" in fatal.stderr
        assert plugin_commands.CALLS == []
    # The same result: its error is reported once, and judged again by the gate.
    with plugin_commands.registered():
        demoted = otto_cli(["-E", "a", "flash", "erase"])
        assert demoted.code == 0, demoted.stderr
        assert demoted.stderr.count("failed to load, but is inactive") == 1
        assert len(plugin_commands.CALLS) == 1


@pytest.mark.parametrize(
    ("entry", "says"),
    [
        ("cov check-stamp", "otto cov check-stamp does not accept registered options"),
        ("cov kmodcov", "otto cov kmodcov does not accept registered options"),
    ],
)
def test_a_two_word_entry_is_judged_when_its_group_is_resolved(result, otto_cli, entry, says):
    _register([entry])
    check_option_entries(result)
    assert _messages(result) == []
    with plugin_commands.registered():
        assert otto_cli(["-E", "a", "flash", "erase"]).code == 0
    assert _messages(result) == []
    run = otto_cli(["cov", "--help"])
    assert run.code == 0
    [message] = _messages(result)
    assert says in message
    assert f"warning: {result.errors[-1]}" in run.stderr


def test_a_group_entry_passes_over_the_commands_that_do_not_accept(result, otto_cli):
    _register(["cov"])
    assert otto_cli(["cov", "--help"]).code == 0
    assert _messages(result) == []


def test_a_new_bootstrap_result_is_checked_again_at_the_same_registry_revision(
    monkeypatch, project_a, otto_cli
):
    _register(["tset"])
    first = install_composed(monkeypatch, fake_bootstrap_result([project_a]))
    otto_cli(["--version"])
    second = install_composed(monkeypatch, fake_bootstrap_result([project_a]))
    otto_cli(["--version"])
    assert len(_messages(first)) == 1
    assert len(_messages(second)) == 1


def test_two_entries_that_share_one_bootstrap_result_report_an_entry_once(result, otto_cli):
    _register(["tset"])
    otto_cli(["--version"])
    otto_cli(["--version"])
    assert len(_messages(result)) == 1


def test_a_registration_made_between_two_entries_on_one_result_is_judged_by_the_second(
    result, otto_cli
):
    _register(["tset"])
    otto_cli(["--version"])
    _register(["lnik"], name="D")  # no checker is installed between invocations
    otto_cli(["--version"])
    messages = _messages(result)
    assert len(messages) == 2
    assert "'tset'" in messages[0]
    assert "'lnik'" in messages[1]


def test_an_entry_is_reported_once_per_result(result, otto_cli):
    _register(["cov nope"])
    otto_cli(["cov", "--help"])
    otto_cli(["cov", "--help"])
    assert len(_messages(result)) == 1


# --- the late checker ---------------------------------------------------------------


@contextlib.contextmanager
def _late(body: "Callable[[], None]") -> "Generator[list[BaseException]]":
    """Register a lab-free plugin group ``board`` whose ``go`` runs *body*; yield its failures."""
    failures: list[BaseException] = []
    board = typer.Typer(name="board")

    @board.command("go")
    def go() -> None:
        try:
            body()
        except Exception as exc:  # noqa: BLE001 — the test asserts it
            failures.append(exc)

    @board.command("stop")
    def stop() -> None:  # pragma: no cover — keeps board a group
        pass

    register_cli_command("board", board, lab_free=True)
    try:
        yield failures
    finally:
        CLI_COMMANDS.unregister(name="board")


def test_a_bad_entry_registered_after_the_startup_check_raises_at_the_registration_call(
    result, otto_cli
):
    late = _class("Late")
    with _late(lambda: otto.register_options(late, verbs=["tset"])) as failures:
        assert otto_cli(["board", "go"]).code == 0
    [failure] = failures
    assert isinstance(failure, OptionsRegistrationError)
    assert "tset" in str(failure)
    assert "test" in str(failure).replace("tset", "")
    assert verbs_for(late) is None
    assert _messages(result) == []  # found after the findings were judged: not appended


def test_a_good_late_registration_is_recorded(result, otto_cli):
    late = _class("Late")
    with _late(lambda: otto.register_options(late, verbs=["run"])) as failures:
        assert otto_cli(["board", "go"]).code == 0
    assert failures == []
    assert verbs_for(late) == ["run"]


def test_a_late_two_word_entry_under_an_unresolved_group_passes_and_is_judged_at_resolution(
    result, otto_cli
):
    late = _class("Late")
    with _late(lambda: otto.register_options(late, verbs=["cov nope"])) as failures:
        assert otto_cli(["board", "go"]).code == 0
    assert failures == []
    assert verbs_for(late) == ["cov nope"]
    assert _messages(result) == []
    otto_cli(["cov", "--help"])
    [message] = _messages(result)
    assert "'nope'" in message


def test_a_late_two_word_entry_under_a_resolved_group_is_judged_at_once(result, otto_cli):
    late = _class("Late")
    with _late(lambda: otto.register_options(late, verbs=["board nope"])) as failures:
        assert otto_cli(["board", "go"]).code == 0
    [failure] = failures
    assert isinstance(failure, OptionsRegistrationError)
    assert "'nope'" in str(failure)
    assert verbs_for(late) is None


def test_the_checker_is_gone_when_entry_returns_and_when_it_raises(result, otto_cli):
    def boom() -> None:
        raise RuntimeError("the body failed")

    assert otto_cli(["--version"]).code == 0
    decorated = otto.options(verbs=["tset"])(_class("AfterReturn"))
    assert verbs_for(decorated) == ["tset"]

    board = typer.Typer(name="board")
    board.command("go")(boom)
    board.command("stop")(lambda: None)
    register_cli_command("board", board, lab_free=True)
    try:
        assert otto_cli(["board", "go"]).code == 1
    finally:
        CLI_COMMANDS.unregister(name="board")
    decorated = otto.options(verbs=["tset"])(_class("AfterRaise"))
    assert verbs_for(decorated) == ["tset"]


# --- fix round 1 ------------------------------------------------------------------


def test_a_run_entry_naming_an_instruction_that_opted_out_is_reported(result, otto_cli):
    from otto.cli.invoke import leaf_accepts_options
    from otto.cli.run import run_app
    from otto.instruction_catalog import instruction

    async def cool() -> None:  # pragma: no cover — only judged and built
        """Cool the board."""

    setattr(cool, ACCEPTS_OPTIONS_ATTR, False)
    instruction("cool")(cool)
    _register(["run cool"])

    startup = otto_cli(["--version"])
    [message] = _messages(result)
    assert "otto run cool does not accept registered options" in message
    assert f"warning: {result.errors[-1]}" in startup.stderr

    # `otto run --help` agrees: nothing new is reported, and the leaf it builds
    # does not offer registered flags by the same reader.
    assert otto_cli(["run", "--help"]).code == 0
    assert len(_messages(result)) == 1
    group = typer.main.get_group(run_app)
    leaf = group.get_command(typer.Context(group), "cool")
    spec = CLI_COMMANDS.peek(name="run")
    assert leaf_accepts_options(spec, leaf.callback, beneath_group=True) is False


def test_a_dispatch_beneath_a_group_is_stopped_by_the_error_its_resolution_found(result, otto_cli):
    _register(["flash nope"])
    with plugin_commands.registered():
        run = otto_cli(["flash", "erase"])
    assert run.code == 1
    [message] = _messages(result)
    assert "otto flash has no command 'nope'" in message
    assert f"warning: {result.errors[-1]}" in run.stderr
    assert "failed to load" in run.stderr
    assert plugin_commands.CALLS == []


def test_a_bad_late_registration_on_a_cache_walk_is_contained(monkeypatch, tmp_path):
    """The cache rebuild's walkers import lazy plugin modules with the checker installed."""
    from functools import partial

    from otto.cli.main import PENDING_SUBCMD_ARGS_KEY, app
    from otto.cli.registry import collect_cli_commands, refuse_bad_entries
    from otto.config.completion_tree import serialize_tree
    from otto.params import set_entry_checker

    module = "tests_late_bad_plugin"
    (tmp_path / f"{module}.py").write_text(
        "import typer\n"
        "import otto\n"
        "class Late:\n"
        "    pass\n"
        "otto.register_options(Late, verbs=['tset'])\n"
        "plug = typer.Typer()\n"
        "@plug.command('a')\n"
        "def a() -> None: ...\n"
        "@plug.command('b')\n"
        "def b() -> None: ...\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    register_cli_command("lateplug", f"{module}:plug", help="A plugin that fails to import.")
    set_entry_checker(partial(refuse_bad_entries, group_of=lambda _: None))

    [entry] = [e for e in collect_cli_commands() if e["name"] == "lateplug"]
    assert "commands" not in entry  # name and help only
    tree = serialize_tree(typer.main.get_command(app), pending_key=PENDING_SUBCMD_ARGS_KEY)
    assert tree.tree["commands"]["lateplug"]["commands"] == {}
    assert module not in sys.modules


def test_a_cold_tab_with_bad_entries_answers_and_prints_no_group_warning(tmp_path):
    import subprocess

    from tests._fixtures.generated_project import generate_project

    project = generate_project(tmp_path, files=1, dirs=1)
    (project / "pylib" / "genproject_instructions.py").write_text(
        "import otto\n\n\n@otto.options(verbs=['tset', 'cov nope'])\nclass Bad:\n    x: int = 0\n"
    )
    env = {k: v for k, v in os.environ.items() if not is_otto_env_var(k)}
    env.update(
        OTTOPATH=str(project),
        OTTO_HOME=str(tmp_path / "home"),
        PYTHONDONTWRITEBYTECODE="1",
        _OTTO_COMPLETE="complete_bash",
        COMP_WORDS="otto cov ",
        COMP_CWORD="2",
    )
    done = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.argv = ['otto']; from otto.cli.main import entry; entry()",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    words = done.stdout.split()
    assert "check-stamp" in words, done.stdout
    assert all(not w.startswith(("warning", "error")) for w in words), done.stdout
    assert "'tset'" in done.stderr  # the startup channel (accepted under a TAB)
    assert "'nope'" not in done.stderr  # no group check under the completion parse
