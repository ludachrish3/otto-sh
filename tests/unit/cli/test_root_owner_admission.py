"""A non-participating project's root commands refuse at dispatch (§3.7, Q14, Q34).

A root command records the project whose init import registered it. When that
project does not take part, the command refuses with exit 1 before its body
runs, naming why, unless a project that does take part requires it. ``-I``
restores it. A lab-free command is judged before any lab, from ``-l``, ``-I``
and ``-E`` alone (tier P); a lab-bound one is judged with the lab (tier L).
otto's own commands have no owner and are never refused.

Every case goes through the real root dispatch (``otto.cli.main.app``) over
real projects parsed from real ``settings.toml`` files and a lab built by the
host factory, as ``test_empty_scope_refusal`` does.
"""

from typing import Any

import pytest
from typer.testing import CliRunner

from otto.cli.main import app
from otto.cli.registry import CLI_COMMANDS, register_cli_command
from otto.config.dependencies import ResolvedDependency
from otto.registry import registering_project
from tests._fixtures.bootstrap_seam import install_composed, patch_bootstrap
from tests._fixtures.fleet import _lab, _project, add_builtin_local

runner = CliRunner()

_CMD = "t12-cmd"


@pytest.fixture(autouse=True)
def _clean_registry():
    before = set(CLI_COMMANDS.names())
    yield
    for name in set(CLI_COMMANDS.names()) - before:
        CLI_COMMANDS.unregister(name=name)


@pytest.fixture(autouse=True)
def _wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """Render the refusal on one line, so a fold cannot read as a missing word."""
    monkeypatch.setenv("COLUMNS", "300")


@pytest.fixture
def projects(tmp_path) -> "list[Any]":
    """``inscope`` applies to lab ``alpha`` (``a-dut``); ``outscope`` applies to ``beta`` only."""
    return [
        _project(tmp_path, "inscope", labs=["alpha"], hosts=["a-dut.*"]),
        _project(tmp_path, "outscope", labs=["beta"], hosts=["b-dut.*"]),
    ]


def _alpha_lab() -> Any:
    """Lab ``alpha``: ``a-dut``, ``a-jump`` and the built-in runner."""
    return add_builtin_local(
        _lab(("a-dut", "alpha"), ("a-jump", "alpha"), component_names=["alpha"])
    )


def _dispatch(
    monkeypatch: pytest.MonkeyPatch, argv: "list[str]", *, projects: Any, lab_free: bool = False
):
    """Run ``otto --lab alpha <argv>`` through the root app, over *projects*.

    *lab_free* makes building the lab fail the test: a lab-free command is
    judged without one.
    """
    from otto.session.lab import BuiltLab

    lab = _alpha_lab()

    def build(_projects: Any, **_kw: Any) -> BuiltLab:
        if lab_free:
            pytest.fail("a lab-free command must not build the lab")
        return BuiltLab(lab=lab, view=None)  # ty: ignore[invalid-argument-type]

    install_composed(monkeypatch, patch_bootstrap(monkeypatch, projects))
    monkeypatch.setattr("otto.session.lab.build_lab_and_view", build)
    return runner.invoke(app, ["--lab", lab.name, *argv])


def _register(owner: "str | None", *, lab_free: bool, **registration: Any) -> "list[Any]":
    """Register root command ``_CMD`` from *owner*'s init import; the list records each run.

    *owner* ``None`` registers it outside any project's init import, as otto's
    own commands are.
    """
    ran: "list[Any]" = []

    def lab_free_leaf() -> None:
        ran.append(_CMD)

    async def lab_bound_leaf() -> None:
        from otto.run_context import get_context

        ran.append(get_context().scoped)

    leaf = lab_free_leaf if lab_free else lab_bound_leaf
    if owner is None:
        register_cli_command(_CMD, leaf, lab_free=lab_free, **registration)
    else:
        with registering_project(owner):
            register_cli_command(_CMD, leaf, lab_free=lab_free, **registration)
    return ran


def _require(project: Any, needs: str) -> None:
    """Record a satisfied required dependency of *project* on *needs*, as bootstrap would."""
    project.dependencies = [
        ResolvedDependency(
            name=needs,
            normalized=needs,
            constraint="",
            required=True,
            status="satisfied",
            provider_version=None,
        )
    ]


def _assert_refused(result: Any, ran: "list[Any]", *, detail: str, hint: str) -> None:
    assert result.exit_code == 1, result.output
    assert f"{detail}\n{hint}" in result.output
    assert ran == []


_SWITCHED_OFF = "which was switched off for this run (--exclude-projects {name})"
_SWITCHED_OFF_HINT = "  activate it: remove --exclude-projects {name}    or: -I {name}"


# ── Lab-free: judged before any lab (tier P) ─────────────────────────────────


def test_lab_free_root_command_refused_by_E(monkeypatch, projects):  # noqa: N802 — -E's own spelling
    """Fail-first: ``-E`` switches the owner off, and its lab-free command refuses."""
    ran = _register("inscope", lab_free=True)

    result = _dispatch(monkeypatch, ["-E", "inscope", _CMD], projects=projects, lab_free=True)

    _assert_refused(
        result,
        ran,
        detail=f"'{_CMD}' belongs to project 'inscope', " + _SWITCHED_OFF.format(name="inscope"),
        hint=_SWITCHED_OFF_HINT.format(name="inscope"),
    )


def test_lab_free_root_command_refused_by_lab_mismatch(monkeypatch, projects):
    """Fail-first: the owner's ``lab_patterns`` miss the ``-l`` lab, judged with no lab built."""
    ran = _register("outscope", lab_free=True)

    result = _dispatch(monkeypatch, [_CMD], projects=projects, lab_free=True)

    _assert_refused(
        result,
        ran,
        detail=(
            f"'{_CMD}' belongs to project 'outscope', which is inactive for the loaded "
            "lab(s) [alpha] (lab_patterns: beta)"
        ),
        hint="  activate it: -l beta    or: -I outscope",
    )


def test_lab_free_root_command_restored_by_I(monkeypatch, projects):  # noqa: N802 — -I's own spelling
    """Fail-first: ``-I`` brings back an owner its lab left out, and the command runs."""
    ran = _register("outscope", lab_free=True)
    refused = _dispatch(monkeypatch, [_CMD], projects=projects, lab_free=True)
    assert refused.exit_code == 1, refused.output

    result = _dispatch(monkeypatch, ["-I", "outscope", _CMD], projects=projects, lab_free=True)

    assert result.exit_code == 0, result.output
    assert ran == [_CMD]


def test_lab_free_root_command_required_owner_runs(monkeypatch, projects):
    """Fail-first: an owner outside the lab runs when a participating project requires it.

    The control comes first: with nobody requiring it, the same command refuses.
    """
    inscope, _outscope = projects
    ran = _register("outscope", lab_free=True)

    refused = _dispatch(monkeypatch, [_CMD], projects=projects, lab_free=True)
    assert refused.exit_code == 1, refused.output

    _require(inscope, "outscope")
    result = _dispatch(monkeypatch, [_CMD], projects=projects, lab_free=True)

    assert result.exit_code == 0, result.output
    assert ran == [_CMD]


# ── Lab-bound: judged with the lab (tier L) ──────────────────────────────────


def test_lab_bound_root_command_refused_by_E(monkeypatch, projects):  # noqa: N802 — -E's own spelling
    """Fail-first: a lab-bound command of a switched-off project refuses before its body."""
    ran = _register("outscope", lab_free=False)

    result = _dispatch(monkeypatch, ["-E", "outscope", _CMD], projects=projects)

    _assert_refused(
        result,
        ran,
        detail=f"'{_CMD}' belongs to project 'outscope', " + _SWITCHED_OFF.format(name="outscope"),
        hint=_SWITCHED_OFF_HINT.format(name="outscope"),
    )


def test_lab_bound_root_command_required_owner_runs(monkeypatch, projects):
    """Fail-first: an owner outside the loaded lab runs when an active project requires it.

    The control comes first: with nobody requiring it, the same command refuses.
    """
    inscope, _outscope = projects
    ran = _register("outscope", lab_free=False)

    refused = _dispatch(monkeypatch, [_CMD], projects=projects)
    assert refused.exit_code == 1, refused.output

    _require(inscope, "outscope")
    result = _dispatch(monkeypatch, [_CMD], projects=projects)

    assert result.exit_code == 0, result.output
    assert ran == [True]


def test_lab_bound_root_command_of_out_of_lab_owner_refused(monkeypatch, projects):
    """With nobody requiring it, the same owner's command refuses for its lab."""
    ran = _register("outscope", lab_free=False)

    result = _dispatch(monkeypatch, [_CMD], projects=projects)

    _assert_refused(
        result,
        ran,
        detail=(
            f"'{_CMD}' belongs to project 'outscope', which is inactive for the loaded "
            "lab(s) [alpha] (lab_patterns: beta)"
        ),
        hint="  activate it: -l beta    or: -I outscope",
    )


# ── Regression ───────────────────────────────────────────────────────────────


def test_first_party_command_never_refused(monkeypatch, projects, tmp_path):
    """A command no project registered has no owner: every project switched off, it still runs.

    Both tiers: otto's own lab-free ``schema export``, and a lab-bound command
    registered outside any project's init import.
    """
    out = tmp_path / "schemas"
    schema = _dispatch(
        monkeypatch,
        ["-E", "inscope,outscope", "schema", "export", "--out", str(out), "--builtins-only"],
        projects=projects,
    )
    assert schema.exit_code == 0, schema.output
    assert CLI_COMMANDS.project(name="schema") is None

    ran = _register(None, lab_free=False)
    result = _dispatch(monkeypatch, ["-E", "outscope", _CMD], projects=projects)
    assert result.exit_code == 0, result.output
    assert ran == [True]


def test_plugin_registration_scoped_by_omission(monkeypatch, projects):
    """A project's command registered without ``scoped=`` is scoped, and runs with its owner."""
    ran = _register("inscope", lab_free=False)

    assert CLI_COMMANDS.get(_CMD).scoped is True
    result = _dispatch(monkeypatch, [_CMD], projects=projects)

    assert result.exit_code == 0, result.output
    assert ran == [True]


def test_builtins_registered_inside_a_projects_init_import_are_never_refused(
    monkeypatch, projects, tmp_path
):
    """otto's commands have no owner whichever project's init import first registers them.

    In a real run nothing imports the command registry before bootstrap's
    init loop, so the first project whose init module imports it is running
    when the fifteen built-ins register. The registration is replayed in that
    order: the built-ins are removed, then registered again inside
    ``outscope``'s init import. ``outscope`` then takes no part, by ``-E`` or
    by its lab, and otto's own command still runs.
    """
    from otto.cli.builtin_commands import register_builtin_commands

    builtins = [
        name
        for name in CLI_COMMANDS.names()
        if CLI_COMMANDS.origin(name=name) == "otto.cli.builtin_commands"
    ]
    assert "schema" in builtins
    for name in builtins:
        CLI_COMMANDS.unregister(name=name)
    with registering_project("outscope"):
        register_builtin_commands()
    assert {name: CLI_COMMANDS.project(name=name) for name in builtins} == dict.fromkeys(builtins)

    for switches in (["-E", "outscope"], []):
        out = tmp_path / f"schemas{len(switches)}"
        result = _dispatch(
            monkeypatch,
            [*switches, "schema", "export", "--out", str(out), "--builtins-only"],
            projects=projects,
            lab_free=True,
        )
        assert result.exit_code == 0, (switches, result.output)
        assert "belongs to project" not in result.output
