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


def test_a_repos_collision_reaches_the_user_as_a_framed_error_not_a_crash(
    tmp_path, monkeypatch, registered
):
    """END TO END: the guard's ValueError is CONTAINED, and its advice survives.

    The two decorator tests above catch the raise at the decoration site, where
    ``pytest.raises`` sees the exception object directly. Nobody runs otto that
    way. On the real path the raise happens inside bootstrap's init-import
    loop, which frames ANY containable failure as a ``BootstrapError`` and
    keeps going — so what the user is actually told is bootstrap's framing of
    it, and a message that does not survive that framing is a message that does
    not exist.

    Kills a framing that drops the cause (``failed to load acme_init`` with no
    reason, which is the shape a caller reads when the repo is skipped), and
    kills a containment seam that lets this one through and bricks the process
    over a repo naming its instruction badly.
    """
    from otto import bootstrap as bs
    from otto.bootstrap import BootstrapError

    # A MODULE NAME OF ITS OWN: init modules are imported by bare name into the
    # one process-wide sys.modules, so reusing another test's name here would
    # hand this bootstrap that test's already-cached module and import nothing.
    repo = make_sut_repo(
        tmp_path / "acme",
        name="acme",
        extra='libs = ["lib"]\ninit = ["acme_collision_init"]\n',
        files={
            "lib/acme_collision_init.py": (
                "from otto.cli.run import instruction\n\n\n"
                "@instruction()\n"
                "async def install() -> None: ...\n"
            )
        },
    )
    monkeypatch.setenv("OTTO_SUT_DIRS", str(repo))
    bs._reset()
    try:
        result = bs.bootstrap()  # must NOT raise — that is half the claim
    finally:
        bs._reset()

    (err,) = result.errors
    assert isinstance(err, BootstrapError)
    assert isinstance(err.__cause__, ValueError)  # the guard's, framed — not re-typed
    assert "acme_collision_init" in str(err), "the framing still names the file that failed"
    # …and the guard's whole point survives it: WHERE to put the override.
    assert "ProjectActions" in str(err)
    assert "docs/guide/run/defaults.md" in str(err)
    # otto keeps the name; the repo did not shadow it on the way past.
    assert INSTRUCTIONS.get("install").module == "otto.project.instructions"


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
async def test_cleanup_forwards_all_four_flags(monkeypatch, registered):
    """Both log flags AND the two lab-infrastructure ones.

    Asserted as an exact mapping, defaults included: a flag the wrapper accepts
    and drops is a `--no-remove-tunnels` that reaps the tunnels anyway, and it
    would pass any assertion that only checked the flags it bothered to pass.
    """
    rec = _Recorder()
    monkeypatch.setattr(orchestrator, "cleanup", rec)

    await registered.cleanup(product_logs=True, debug_logs=False, remove_tunnels=False)
    await registered.cleanup(reset_impairments=False)

    assert rec.calls == [
        {
            "get_product_logs": True,
            "get_debug_logs": False,
            "reset_impairments": True,
            "remove_tunnels": False,
        },
        {
            "get_product_logs": True,
            "get_debug_logs": True,
            "reset_impairments": False,
            "remove_tunnels": True,
        },
    ]


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


def _state_rows(out: str) -> dict[str, str]:
    """Parse the status table into ``{repo: state}``, one entry per printed row.

    The table is ``repo_name`` then the state, so the FIRST token names the
    repo and the rest is its state ("partially installed" is two words).
    """
    rows = {}
    for line in out.splitlines():
        if not line.strip():
            continue
        name, _, state = line.strip().partition(" ")
        rows[name] = state.strip()
    return rows


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

    # ROW-WISE, because "installed" is a SUBSTRING of "uninstalled": a renderer
    # that printed every repo as uninstalled — or as installed — satisfies both
    # bare `in out` checks, and this table exists precisely to tell the two
    # apart per repo. Pairing each name with its own cell is the only form that
    # can fail. Kills a row built from the wrong side of the mapping, a state
    # column read off the aggregate, and the substring accident itself.
    assert _state_rows(capsys.readouterr().out) == {
        "acme": "installed",
        "widgets": "uninstalled",
    }
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


def test_list_instructions_shows_the_defaults_with_no_repos_configured(registered):
    from unittest.mock import patch

    from otto.cli.run import run_app

    with patch("otto.config.get_repos", return_value=[]):
        result = runner.invoke(run_app, ["--list-instructions"])

    assert result.exit_code == 0, result.output
    assert "otto defaults" in result.output
    assert "install-tools" in result.output


class _PanelRepo:
    """Repo double for the listing: contributes one panel with a known title."""

    def __init__(self, title: str) -> None:
        self.title = title

    def get_instructions_panel(self):
        from rich.panel import Panel
        from rich.text import Text

        return Panel(Text("• deploy"), title=Text(self.title), expand=True)


def test_list_instructions_puts_the_defaults_ahead_of_the_repo_panels(registered):
    """The ``otto defaults`` panel is the FIRST column, before every repo's.

    ORDERING NEEDS A REPO TO BE AHEAD OF. With ``get_repos`` patched to ``[]``
    the panel list has one element, so ``insert(0, …)`` and ``append(…)`` are
    the same program — and "ahead of the repos" is asserted by a test that
    configured no repos. Kills the append, which is the natural edit (the
    first-party panel is built last) and which buries the six verbs every lab
    has behind however many repos the lab happens to configure.

    The panels are columns of a single-row table, so both titles land on the
    same output line and their column order IS their character order there.
    """
    from unittest.mock import patch

    from otto.cli.run import run_app

    repos = [_PanelRepo("first-repo 1.0"), _PanelRepo("second-repo 1.0")]
    with patch("otto.config.get_repos", return_value=repos):
        result = runner.invoke(run_app, ["--list-instructions"])

    assert result.exit_code == 0, result.output
    (titles,) = [ln for ln in result.output.splitlines() if "otto defaults" in ln]
    assert "first-repo" in titles, "the repo panels share the title line with otto's"
    assert titles.index("otto defaults") < titles.index("first-repo") < titles.index("second-repo")
