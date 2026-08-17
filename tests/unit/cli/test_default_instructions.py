"""First-party default instructions: registration, dispatch, and collision policy.

The six ``otto run`` verbs that wrap :mod:`otto.project.orchestrator`, the guard
that stops a repo shadowing one of their names, and the ``otto defaults`` panel
that ``--list-instructions`` puts them in.

REGISTRATION IS AN IMPORT-ONCE, PROCESS-WIDE SIDE EFFECT, while the root
conftest's ``_isolate_registries`` rolls every registry back after each test —
so a later test that merely imports the (already cached) module gets nothing,
and any assertion about the six would then depend on what ran before it. Tests
that need them registered go through the ``registered`` fixture, which clears
the names and RE-EXECUTES the module body: a real import, on demand, in any
order.
"""

import io

import pytest
import typer
from rich.console import Console
from typer.testing import CliRunner

from otto.instructions import FIRST_PARTY_INSTRUCTIONS, INSTRUCTIONS
from otto.project import InstallState, ProjectStatus, orchestrator
from otto.result import Result
from otto.utils import Status
from tests._fixtures.sutrepo import make_sut_repo

SIX = ["install", "uninstall", "cleanup", "get-logs", "install-tools", "status"]

runner = CliRunner()


def _clear_first_party() -> None:
    """Drop whatever first-party entries this process already holds."""
    for name in FIRST_PARTY_INSTRUCTIONS:
        if name in INSTRUCTIONS:
            INSTRUCTIONS.unregister(name)


def _reimport():
    """Re-execute ``otto.project.instructions``' body and return the module.

    ``importlib.reload`` rather than a ``sys.modules`` eviction on purpose: it
    updates the EXISTING module object in place, so the package attribute and
    the ``sys.modules`` entry stay one object (the desync behind issue #108).
    """
    import importlib

    from otto.project import instructions as mod

    _clear_first_party()
    return importlib.reload(mod)


@pytest.fixture
def registered():
    """The six, registered, whatever ran in this process before."""
    return _reimport()


def _render(renderable) -> str:
    """Render a Rich renderable to a plain string for assertion."""
    buf = io.StringIO()
    Console(file=buf, width=300, highlight=False).print(renderable)
    return buf.getvalue()


class _Recorder:
    """Async double for an orchestrator verb: records kwargs, returns *result*."""

    def __init__(self, result=None):
        self.result = Result(Status.Success) if result is None else result
        self.calls: list[dict] = []

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


# ── Registration ─────────────────────────────────────────────────────────────


def test_importing_the_module_registers_all_six(registered):
    for name in SIX:
        assert name in INSTRUCTIONS, name
        assert INSTRUCTIONS.get(name).module == "otto.project.instructions"
    assert frozenset(SIX) == FIRST_PARTY_INSTRUCTIONS


def test_the_registered_set_is_exactly_the_declared_set(registered):
    """No drift between the frozenset the guard reads and what actually registers.

    The guard refuses a repo instruction by NAME, from
    ``FIRST_PARTY_INSTRUCTIONS`` — a name in that set with no instruction
    behind it forbids a repo a name otto never took, and an instruction absent
    from it is one a repo can still shadow.
    """
    registered_here = {
        name for name, entry in INSTRUCTIONS.items() if entry.module == registered.__name__
    }
    assert registered_here == FIRST_PARTY_INSTRUCTIONS


# ── Collision policy ─────────────────────────────────────────────────────────


def test_repo_defining_a_first_party_name_fails_loud_with_migration_message():
    # THE collision policy: fail loud, point at ProjectActions. Kills silent
    # shadowing (the CLI/fixture split-brain the design exists to prevent).
    from otto.cli.run import instruction
    from otto.registry import registering_repo

    with registering_repo("acme"), pytest.raises(ValueError, match="ProjectActions"):

        @instruction()
        async def install() -> None: ...


def test_the_refusal_beats_the_registry_and_leaves_ottos_entry_intact(registered):
    """The guard fires BEFORE ``INSTRUCTIONS.register``, with otto's own entry present.

    Two failures hide here if it fires after: the repo's sub-app would already
    be in the registry (the duplicate check would raise the generic
    "already registered" error, which says nothing about ProjectActions), and
    a first registration ORDER where the repo goes first would win outright.
    """
    from otto.cli.run import instruction
    from otto.registry import registering_repo

    with registering_repo("acme"), pytest.raises(ValueError, match="ProjectActions"):

        @instruction()
        async def cleanup() -> None: ...

    assert INSTRUCTIONS.get("cleanup").module == "otto.project.instructions"


def test_otto_itself_may_register_first_party_names():
    # The guard keys on the registering-repo marker, NOT on the name alone —
    # otherwise otto's own registration trips it (order-independence).
    _reimport()  # no registering-repo marker is set: must not raise
    assert set(SIX) <= set(INSTRUCTIONS.names())


def test_repo_instruction_with_novel_name_is_unaffected():
    from otto.cli.run import instruction
    from otto.registry import registering_repo

    with registering_repo("acme"):

        @instruction()
        async def totally_custom() -> None: ...

    assert "totally-custom" in INSTRUCTIONS


# ── Bootstrap wiring ─────────────────────────────────────────────────────────


def test_bootstrap_imports_the_defaults_before_any_repo_init(tmp_path, monkeypatch):
    """Phase 2 registers otto's defaults, and does it before the repo loop.

    Observed as the CALL, not as ``sys.modules`` afterwards: the module is
    imported once per process, so an "is it imported" assertion passes on the
    strength of any earlier test having imported it and would stay green with
    the bootstrap line deleted.
    """
    import importlib

    from otto import bootstrap as bs

    seen: list[str] = []
    real_import = importlib.import_module

    def _spy(name, *args, **kwargs):
        seen.append(name)
        return real_import(name, *args, **kwargs)

    repo = make_sut_repo(
        tmp_path / "acme",
        name="acme",
        extra='libs = ["lib"]\ninit = ["acme_init"]\n',
        files={"lib/acme_init.py": "X = 1\n"},
    )
    monkeypatch.setenv("OTTO_SUT_DIRS", str(repo))
    monkeypatch.setattr(bs.importlib, "import_module", _spy)
    bs._reset()
    try:
        result = bs.bootstrap()
    finally:
        bs._reset()

    assert result.errors == []
    assert "otto.project.instructions" in seen
    assert seen.index("otto.project.instructions") < seen.index("acme_init")


# ── Dispatch: every flag reaches the orchestrator ────────────────────────────


@pytest.mark.asyncio
async def test_install_forwards_the_converge_flags(monkeypatch, registered):
    """``--ensure``/``--recover-partial`` are the orchestrator's, not the wrapper's.

    The branch between a flat install and the converge lives in
    ``orchestrator.install`` so the ensure_* fixtures take the same one; a
    wrapper that re-decided it here would be a second place to get it wrong.
    """
    rec = _Recorder()
    monkeypatch.setattr(orchestrator, "install", rec)

    await registered.install(ensure=True, recover_partial=False)
    await registered.install()

    assert rec.calls == [
        {"ensure": True, "recover_partial": False},
        {"ensure": False, "recover_partial": True},
    ]


@pytest.mark.asyncio
async def test_uninstall_forwards_both_log_flags(monkeypatch, registered):
    rec = _Recorder()
    monkeypatch.setattr(orchestrator, "uninstall", rec)

    await registered.uninstall(product_logs=False, debug_logs=True)

    assert rec.calls == [{"get_product_logs": False, "get_debug_logs": True}]


@pytest.mark.asyncio
async def test_cleanup_forwards_both_log_flags(monkeypatch, registered):
    rec = _Recorder()
    monkeypatch.setattr(orchestrator, "cleanup", rec)

    await registered.cleanup(product_logs=True, debug_logs=False)

    assert rec.calls == [{"get_product_logs": True, "get_debug_logs": False}]


@pytest.mark.asyncio
async def test_get_logs_forwards_all_three_flags(monkeypatch, registered):
    rec = _Recorder()
    monkeypatch.setattr(orchestrator, "get_logs", rec)

    await registered.get_logs(product_logs=True, debug_logs=False, require_product_logs=True)

    assert rec.calls == [{"product": True, "debug": False, "require_product_logs": True}]


@pytest.mark.asyncio
async def test_install_tools_forwards_dev_and_toolchain(monkeypatch, registered):
    rec = _Recorder()
    monkeypatch.setattr(orchestrator, "install_tools", rec)

    await registered.install_tools(dev=False, toolchain=True)

    assert rec.calls == [{"dev": False, "toolchain": True}]


@pytest.mark.asyncio
async def test_the_orchestrators_result_is_returned_unchanged(monkeypatch, registered):
    """Including a ``Skipped`` one — the converge layer's no-op arm.

    ``Result.is_ok`` is true for Skipped, so the render seam exits 0; a wrapper
    that repacked (or worse, tested ``is Status.Success``) would turn "already
    installed" into a failure.
    """
    skipped = Result(Status.Skipped, msg="already installed")
    monkeypatch.setattr(orchestrator, "install", _Recorder(skipped))

    returned = await registered.install(ensure=True)

    assert returned is skipped


def test_a_skipped_result_renders_as_success():
    """The exit-code half of the rule above, at the seam that decides it."""
    from otto.cli.invoke import render_leaf_value

    render_leaf_value(Result(Status.Skipped, msg="already installed"))  # must not raise


# ── status: the table and the three exit codes ───────────────────────────────


@pytest.mark.parametrize(
    ("state", "code"),
    [(InstallState.INSTALLED, 0), (InstallState.UNINSTALLED, 1), (InstallState.PARTIAL, 2)],
)
@pytest.mark.asyncio
async def test_status_exit_code_is_the_answer(monkeypatch, registered, state, code):
    """Scripts branch on the code, so all three are distinct and pinned."""
    monkeypatch.setattr(
        orchestrator, "status", _Recorder(ProjectStatus(overall=state, repos={"acme": state}))
    )

    assert (await registered.status()).exit_code == code


@pytest.mark.parametrize(
    ("state", "code"), [(InstallState.UNINSTALLED, 1), (InstallState.PARTIAL, 2)]
)
@pytest.mark.asyncio
async def test_the_status_answer_reaches_the_process_exit_code(
    monkeypatch, registered, state, code
):
    """The returned carrier is the WHOLE mechanism — nothing else sets the code.

    ``exit_code`` on the value proves the mapping; this proves the renderer
    honors it. A carrier the leaf bridge shrugged at (a bare int, a dataclass
    of otto's own) would pass the test above and still exit 0.
    """
    from otto.cli.invoke import render_leaf_value

    monkeypatch.setattr(
        orchestrator, "status", _Recorder(ProjectStatus(overall=state, repos={"acme": state}))
    )

    with pytest.raises(typer.Exit) as excinfo:
        render_leaf_value(await registered.status())

    assert excinfo.value.exit_code == code


@pytest.mark.asyncio
async def test_status_prints_every_repos_state(monkeypatch, registered, capsys):
    report = ProjectStatus(
        overall=InstallState.PARTIAL,
        repos={"acme": InstallState.INSTALLED, "widgets": InstallState.UNINSTALLED},
    )
    monkeypatch.setattr(orchestrator, "status", _Recorder(report))

    answer = await registered.status()

    out = capsys.readouterr().out
    assert "acme" in out
    assert "widgets" in out
    assert "installed" in out
    assert "uninstalled" in out
    assert "partially installed" in answer.msg  # the lab-level aggregate the renderer prints


# ── --list-instructions: the first-party panel ───────────────────────────────


def test_first_party_panel_lists_the_six(registered):
    from otto.cli.run import first_party_instructions_panel

    text = _render(first_party_instructions_panel())

    assert "otto defaults" in text
    for name in SIX:
        assert name in text, name


def test_no_panel_when_otto_registered_nothing():
    """An empty panel is worse than none: it advertises a section with no content."""
    from otto.cli.run import first_party_instructions_panel

    _clear_first_party()

    assert first_party_instructions_panel() is None


def test_list_instructions_shows_the_defaults_ahead_of_the_repos(registered):
    from unittest.mock import patch

    from otto.cli.run import run_app

    with patch("otto.config.get_repos", return_value=[]):
        result = runner.invoke(run_app, ["--list-instructions"])

    assert result.exit_code == 0, result.output
    assert "otto defaults" in result.output
    assert "install-tools" in result.output
