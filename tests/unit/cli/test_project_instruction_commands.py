"""Published project-instruction commands: the merge, the dispatch, the panel.

``otto run install`` and its five siblings are no longer hand-written wrappers.
:func:`otto.project.commands.publish_project_instructions` builds ONE command
per name in :data:`~otto.instructions.PROJECT_INSTRUCTIONS`, whose flags are the
union of every registered body's options class, and whose body forwards to
:func:`otto.project.orchestrator.run_project_instruction`. This file covers the
publish (what lands in ``INSTRUCTIONS``), the merge (whose flags land on the
command, and what a cross-repo field collision does), the dispatch (every flag
reaching the orchestrator as an options instance), ``status``' rendering and its
three exit codes, and the ``otto defaults`` panel.

PUBLISHING IS A CALL, NOT AN IMPORT, which is the whole point of this task: the
flag set is only known once every repo's init has spoken, so bootstrap publishes
AFTER the repo loop. The root conftest's ``_isolate_registries`` rolls every
registry back after each test, so tests that need the six present go through the
``registered`` fixture, which re-declares otto's bodies and publishes them --
in any order, whatever ran in this process before.

THE ORCHESTRATOR IS PATCHED AT ``orchestrator.<name>``, never at
``otto.project.<name>``: :data:`otto.project.orchestrator._ENTRIES` resolves the
six public functions BY NAME on that module at call time, so the package's
re-exported objects are not what the dispatcher consults.

Real invocations use :class:`~tests._fixtures.dispatch.DispatchRunner` rather
than a bare ``CliRunner``: the published leaves are ``async def``, so they only
run under the leaf-invoke bridge the root dispatch installs. ``--help`` and
``--list-instructions`` exit during parse and use the plain runner.
"""

import io
import re
from typing import Annotated

import pytest
import typer
from rich.console import Console
from typer.testing import CliRunner

from otto import options
from otto.cli.run import instruction, run_app
from otto.instructions import FIRST_PARTY_INSTRUCTIONS, INSTRUCTIONS, PROJECT_INSTRUCTIONS
from otto.project import (
    Cleanliness,
    CleanlinessItem,
    CleanlinessKind,
    CleanlinessReport,
    CleanupOptions,
    GetLogsOptions,
    InstallOptions,
    InstallState,
    InstallToolsOptions,
    ProjectActions,
    ProjectStatus,
    RepoScope,
    UninstallOptions,
    orchestrator,
    register_project_actions,
)
from otto.project.commands import (
    OptionsCollisionError,
    merged_option_params,
    publish_project_instructions,
)
from otto.registry import registering_repo
from otto.result import Result
from otto.utils import Status
from tests._fixtures.dispatch import DispatchRunner
from tests._fixtures.sutrepo import make_sut_repo

SIX = ["install", "uninstall", "cleanup", "get-logs", "install-tools", "status"]

ACTIONS_MODULE = "otto.project.actions"
"""The module every first-party entry is attributed to -- where the bodies live."""

runner = CliRunner()
dispatch = DispatchRunner()


def _clear_first_party() -> None:
    """Drop whatever first-party entries this process already holds."""
    for name in FIRST_PARTY_INSTRUCTIONS:
        if name in INSTRUCTIONS:
            INSTRUCTIONS.unregister(name)


def _publish_the_six():
    """Re-declare otto's own bodies and publish every project instruction."""
    from otto.project import actions as mod

    _clear_first_party()
    mod.register_project_instruction_bodies(ProjectActions, None)
    publish_project_instructions()


@pytest.fixture
def registered():
    """The six, published, whatever ran in this process before."""
    _publish_the_six()


def _render(renderable) -> str:
    """Render a Rich renderable to a plain string for assertion."""
    buf = io.StringIO()
    Console(file=buf, width=300, highlight=False).print(renderable)
    return buf.getvalue()


def _leaf(name: str):
    """The published command's callback for *name* -- what ``otto run <name>`` runs."""
    (command,) = INSTRUCTIONS.get(name).sub_app.registered_commands
    return command.callback


async def _run(name: str, **flags):
    """Await the published command's body for *name* with *flags*, and return its value.

    The CLI's own path minus click's parsing: the leaf takes the parsed flags
    as keywords and hands them to the orchestrator, so a flag the test omits
    defaults exactly where the real invocation's would -- in the options class
    the dispatcher builds, not in a signature this call had to restate.
    """
    return await _leaf(name)(**flags)


class _Recorder:
    """Async double for an orchestrator verb: records the ``OptionsSource`` it got.

    POSITIONAL, because that is the whole orchestrator signature now: every
    public verb takes ONE options instance (or an ``OptionsSource``), so a
    double that only accepted keywords would certify a dispatcher that still
    spelled the old flat flags.
    """

    def __init__(self, result=None):
        self.result = Result(Status.Success) if result is None else result
        self.calls: list = []

    async def __call__(self, *args, **kwargs):
        self.calls.append(args[0] if args and not kwargs else kwargs)
        return self.result

    def built(self, opts_cls: type) -> list:
        """Every recorded source, built into *opts_cls* -- the instance the body sees."""
        return [source.build(opts_cls) for source in self.calls]


# ── Registration ─────────────────────────────────────────────────────────────


def test_publishing_registers_all_six(registered):
    for name in SIX:
        assert name in INSTRUCTIONS, name
        entry = INSTRUCTIONS.get(name)
        assert entry.module == ACTIONS_MODULE
        # First-party whoever declared the body: the dispatch gate refuses an
        # instruction whose OWNER is inactive, and a project instruction has a
        # body per repo rather than one owner.
        assert entry.registered_by is None
    assert frozenset(SIX) == FIRST_PARTY_INSTRUCTIONS


def test_the_registered_set_is_exactly_the_declared_set(registered):
    """No drift between the frozenset the guard reads and what actually publishes.

    The guard refuses a repo instruction by NAME, from
    ``FIRST_PARTY_INSTRUCTIONS`` — a name in that set with no instruction
    behind it forbids a repo a name otto never took, and an instruction absent
    from it is one a repo can still shadow.
    """
    registered_here = {
        name for name, entry in INSTRUCTIONS.items() if entry.module == ACTIONS_MODULE
    }
    assert registered_here == FIRST_PARTY_INSTRUCTIONS


# ── Collision policy ─────────────────────────────────────────────────────────


def test_repo_defining_a_first_party_name_fails_loud_with_migration_message():
    # THE collision policy: fail loud, point at ProjectActions. Kills silent
    # shadowing (the CLI/fixture split-brain the design exists to prevent).
    with registering_repo("acme"), pytest.raises(ValueError, match="project instruction"):

        @instruction()
        async def install() -> None: ...


def test_the_refusal_beats_the_registry_and_leaves_ottos_entry_intact(registered):
    """The guard fires BEFORE ``INSTRUCTIONS.register``, with otto's own entry present.

    Two failures hide here if it fires after: the repo's sub-app would already
    be in the registry (the duplicate check would raise the generic
    "already registered" error, which says nothing about ProjectActions), and
    a first registration ORDER where the repo goes first would win outright.
    """
    with registering_repo("acme"), pytest.raises(ValueError, match="project instruction"):

        @instruction()
        async def cleanup() -> None: ...

    assert INSTRUCTIONS.get("cleanup").module == ACTIONS_MODULE


def test_otto_itself_may_register_first_party_names():
    # The guard keys on the registering-repo marker, NOT on the name alone —
    # otherwise otto's own publish trips it (order-independence).
    _publish_the_six()  # no registering-repo marker is set: must not raise
    assert set(SIX) <= set(INSTRUCTIONS.names())


def test_repo_instruction_with_novel_name_is_unaffected():
    with registering_repo("acme"):

        @instruction()
        async def totally_custom() -> None: ...

    assert "totally-custom" in INSTRUCTIONS


# ── Bootstrap wiring ─────────────────────────────────────────────────────────


def test_bootstrap_declares_before_any_repo_init_and_publishes_after(tmp_path, monkeypatch):
    """Phase 2 imports the BODIES first and publishes the COMMANDS last.

    The two halves are ordered against opposite ends of the repo loop and both
    orderings are load-bearing. ``otto.project.actions`` must be imported
    first, so the decorator's guard knows the project-instruction names before
    a repo can claim one and so the walk shapes are fixed; the publish must
    come last, because a repo's override is what ADDS flags to the merged
    command and a publish taken before the loop would ship the union of
    nothing.

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

    def _publish_spy() -> None:
        seen.append("<publish>")

    repo = make_sut_repo(
        tmp_path / "acme",
        name="acme",
        extra='libs = ["lib"]\ninit = ["acme_init"]\n',
        files={"lib/acme_init.py": "X = 1\n"},
    )
    monkeypatch.setenv("OTTO_SUT_DIRS", str(repo))
    monkeypatch.setattr(bs.importlib, "import_module", _spy)
    monkeypatch.setattr("otto.project.commands.publish_project_instructions", _publish_spy)
    bs._reset()
    try:
        result = bs.bootstrap()
    finally:
        bs._reset()

    assert result.errors == []
    assert ACTIONS_MODULE in seen
    assert seen.index(ACTIONS_MODULE) < seen.index("acme_init")
    assert seen.index("acme_init") < seen.index("<publish>")


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
    assert "docs/guide/cli/run/defaults.md" in str(err)
    # otto keeps the name; the repo did not shadow it on the way past.
    assert INSTRUCTIONS.get("install").module == ACTIONS_MODULE


# ── Dispatch: every flag reaches the orchestrator ────────────────────────────


def test_install_forwards_the_converge_flags(monkeypatch, registered):
    """``--ensure``/``--recover-partial`` are the orchestrator's, not the command's.

    The branch between a flat install and the converge lives in
    ``orchestrator.install`` so the ensure_* fixtures take the same one; a
    published command that re-decided it would be a second place to get it
    wrong. Asserted on the OPTIONS INSTANCE the source builds, which is what
    the body is handed.
    """
    rec = _Recorder()
    monkeypatch.setattr(orchestrator, "install", rec)

    first = dispatch.invoke(
        run_app, ["install", "--ensure", "--no-recover-partial"], async_leaves=True
    )
    second = dispatch.invoke(run_app, ["install"], async_leaves=True)

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert rec.built(InstallOptions) == [
        InstallOptions(ensure=True, recover_partial=False),
        InstallOptions(ensure=False, recover_partial=True),
    ]


def test_uninstall_forwards_both_log_flags(monkeypatch, registered):
    rec = _Recorder()
    monkeypatch.setattr(orchestrator, "uninstall", rec)

    result = dispatch.invoke(
        run_app, ["uninstall", "--no-product-logs", "--debug-logs"], async_leaves=True
    )

    assert result.exit_code == 0, result.output
    assert rec.built(UninstallOptions) == [UninstallOptions(product_logs=False, debug_logs=True)]


def test_cleanup_forwards_all_four_flags(monkeypatch, registered):
    """Both log flags AND the two lab-infrastructure ones.

    Asserted as an exact mapping, defaults included: a flag the command accepts
    and drops is a `--no-remove-tunnels` that reaps the tunnels anyway, and it
    would pass any assertion that only checked the flags it bothered to pass.
    """
    rec = _Recorder()
    monkeypatch.setattr(orchestrator, "cleanup", rec)

    first = dispatch.invoke(
        run_app,
        ["cleanup", "--product-logs", "--no-debug-logs", "--no-remove-tunnels"],
        async_leaves=True,
    )
    second = dispatch.invoke(run_app, ["cleanup", "--no-reset-impairments"], async_leaves=True)

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert rec.built(CleanupOptions) == [
        CleanupOptions(
            product_logs=True,
            debug_logs=False,
            reset_impairments=True,
            remove_tunnels=False,
        ),
        CleanupOptions(
            product_logs=True,
            debug_logs=True,
            reset_impairments=False,
            remove_tunnels=True,
        ),
    ]


def test_get_logs_forwards_all_three_flags(monkeypatch, registered):
    rec = _Recorder()
    monkeypatch.setattr(orchestrator, "get_logs", rec)

    result = dispatch.invoke(
        run_app,
        ["get-logs", "--product-logs", "--no-debug-logs", "--require-product-logs"],
        async_leaves=True,
    )

    assert result.exit_code == 0, result.output
    assert rec.built(GetLogsOptions) == [
        GetLogsOptions(product_logs=True, debug_logs=False, require_product_logs=True)
    ]


def test_install_tools_forwards_dev_and_toolchain(monkeypatch, registered):
    rec = _Recorder()
    monkeypatch.setattr(orchestrator, "install_tools", rec)

    result = dispatch.invoke(
        run_app, ["install-tools", "--no-dev", "--toolchain"], async_leaves=True
    )

    assert result.exit_code == 0, result.output
    assert rec.built(InstallToolsOptions) == [InstallToolsOptions(dev=False, toolchain=True)]


@pytest.mark.asyncio
async def test_the_orchestrators_result_is_returned_unchanged(monkeypatch, registered):
    """Including a ``Skipped`` one — the converge layer's no-op arm.

    ``Result.is_ok`` is true for Skipped, so the render seam exits 0; a
    dispatcher that repacked (or worse, tested ``is Status.Success``) would
    turn "already installed" into a failure.
    """
    skipped = Result(Status.Skipped, msg="already installed")
    monkeypatch.setattr(orchestrator, "install", _Recorder(skipped))

    returned = await _run("install", ensure=True)

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

    assert (await _run("status")).exit_code == code


@pytest.mark.parametrize(
    ("state", "code"), [(InstallState.UNINSTALLED, 1), (InstallState.PARTIAL, 2)]
)
def test_the_status_answer_reaches_the_process_exit_code(monkeypatch, registered, state, code):
    """The returned carrier is the WHOLE mechanism — nothing else sets the code.

    ``exit_code`` on the value proves the mapping; this proves the dispatch
    honors it, through the real command. A carrier the leaf bridge shrugged at
    (a bare int, a dataclass of otto's own) would pass the test above and still
    exit 0.
    """
    monkeypatch.setattr(
        orchestrator, "status", _Recorder(ProjectStatus(overall=state, repos={"acme": state}))
    )

    result = dispatch.invoke(run_app, ["status"], async_leaves=True)

    assert result.exit_code == code, result.output


@pytest.mark.asyncio
async def test_status_prints_every_repos_state(monkeypatch, registered, capsys):
    report = ProjectStatus(
        overall=InstallState.PARTIAL,
        repos={"acme": InstallState.INSTALLED, "widgets": InstallState.UNINSTALLED},
    )
    monkeypatch.setattr(orchestrator, "status", _Recorder(report))

    answer = await _run("status")

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


# ── status --full: the cleanliness axis ──────────────────────────────────────


def _report(*rows):
    """A cleanliness report of *rows*, in the order the orchestrator hands them over."""
    return CleanlinessReport(items=list(rows))


def _clean_row(kind, name, state, detail="", error=None):
    """One row; UNKNOWN rows must carry the error is_clean would have raised."""
    return CleanlinessItem(kind=kind, name=name, state=state, detail=detail, error=error)


def _wire_status(monkeypatch, overall, repos=None, report=None, scoping=None):
    """Point both orchestrator questions at doubles; return the cleanliness one."""
    monkeypatch.setattr(
        orchestrator,
        "status",
        _Recorder(ProjectStatus(overall=overall, repos=repos or {}, scoping=scoping or {})),
    )
    probe = _Recorder(_report() if report is None else report)
    monkeypatch.setattr(orchestrator, "cleanliness", probe)
    return probe


_SECTIONS = ("products & dev tools", "toolchain tools", "impairments", "tunnels")


def _row_line(out: str, name: str) -> str:
    """The one line of the CLEANLINESS section whose NAME column is *name*.

    Sliced from the first section heading down, because the install table
    prints first and names the same repos: a parser over the whole output finds
    two lines for one repo and cannot say which axis it just read. Matched
    line-wise within that slice for the reason the install table's own parser
    is -- "clean" is a substring of the summary line below it, so a renderer
    that painted every row alike satisfies a bare ``in out`` check and fails
    the only assertion that matters, that THIS row says THIS.
    """
    lines = out.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(_SECTIONS))
    (row,) = [line for line in lines[start:] if name in line.split()]
    return row


@pytest.mark.asyncio
async def test_bare_status_asks_for_no_cleanliness_at_all(monkeypatch, registered):
    """`otto run status` stays exactly as cheap as it was: no new device reads.

    Asserted as "the probe was never CALLED", not as "the output has no
    cleanliness rows": a status that ran the link reads and the process scan
    and then printed none of it passes the output check and charges every
    caller the device work anyway.
    """
    probe = _wire_status(monkeypatch, InstallState.INSTALLED, {"acme": InstallState.INSTALLED})

    await _run("status")

    assert probe.calls == []


@pytest.mark.asyncio
async def test_status_full_leaves_the_exit_code_to_the_install_axis(monkeypatch, registered):
    """A fully installed but filthy lab still exits 0.

    THE CONTRACT SCRIPTS BRANCH ON: `--full` changes what is DISPLAYED, never
    what is returned. Dev tools left behind and a tunnel still up are real dirt
    and the rows below say so, while the code keeps meaning "are the products
    on?" -- fold cleanliness into it and every script that reads `otto run
    status` starts getting a different answer to the question it asked.
    """
    _wire_status(
        monkeypatch,
        InstallState.INSTALLED,
        {"acme": InstallState.INSTALLED},
        _report(
            _clean_row(CleanlinessKind.REPO, "acme", Cleanliness.DIRTY),
            _clean_row(CleanlinessKind.TUNNEL, "h0-h1-tcp-5201", Cleanliness.DIRTY),
        ),
    )

    answer = await _run("status", full=True)

    assert answer.exit_code == 0
    assert answer is await _run("status")  # the same carrier the bare run returns


def test_status_full_is_parsed_off_the_command_line(monkeypatch, registered):
    """THE FLAG ITSELF, through click: `--full` is a merged parameter, not a keyword.

    Every other `--full` test here calls the leaf with ``full=True``, which
    proves the renderer and skips the half this task owns -- that the flag is
    on the published signature at all and that its parsed value reaches the
    dispatcher. A leaf whose signature lost the parameter would fail to parse
    `--full` (exit 2) while every keyword-driven test stayed green.
    """
    probe = _wire_status(
        monkeypatch,
        InstallState.INSTALLED,
        {"acme": InstallState.INSTALLED},
        _report(_clean_row(CleanlinessKind.TUNNEL, "lab", Cleanliness.DIRTY)),
    )

    result = dispatch.invoke(run_app, ["status", "--full"], async_leaves=True)

    assert result.exit_code == 0, result.output
    assert len(probe.calls) == 1  # --full is what asks the cleanliness question
    assert "tunnels" in result.output
    assert "lab is dirty" in result.output


@pytest.mark.asyncio
async def test_status_full_prints_a_row_per_thing_and_heads_each_section_once(
    monkeypatch, registered, capsys
):
    _wire_status(
        monkeypatch,
        InstallState.INSTALLED,
        {"acme": InstallState.INSTALLED},
        _report(
            _clean_row(CleanlinessKind.REPO, "acme", Cleanliness.CLEAN),
            _clean_row(CleanlinessKind.REPO, "widgets", Cleanliness.DIRTY),
            _clean_row(CleanlinessKind.TUNNEL, "lab", Cleanliness.CLEAN),
        ),
    )

    await _run("status", full=True)
    out = capsys.readouterr().out

    # Row-wise, because "clean" is a substring of nothing here by luck alone:
    # a renderer reading the state off the report's AGGREGATE would paint both
    # repo rows the same and still contain both words somewhere.
    assert "clean" in _row_line(out, "acme")
    assert "dirty" in _row_line(out, "widgets")
    assert out.count("products & dev tools") == 1  # heading on the row the kind changes
    assert out.count("tunnels") == 1
    assert "lab is dirty" in out  # the aggregate, which no exit code carries


@pytest.mark.asyncio
async def test_status_full_renders_a_cell_for_what_could_not_be_read(
    monkeypatch, registered, capsys
):
    """A DISPLAY'S DUTY ON A NON-FACT is the opposite of a converge's.

    `is_clean` raises on an unread state, deliberately -- a cleanup decided on
    something nobody measured is not a decision. A `--full` built on it would
    die on one unreachable host and show nothing about the hosts that answered,
    so the row is printed and marked instead.
    """
    _wire_status(
        monkeypatch,
        InstallState.UNINSTALLED,
        {"acme": InstallState.UNINSTALLED},
        _report(
            _clean_row(CleanlinessKind.TOOLCHAIN, "h0", Cleanliness.CLEAN),
            _clean_row(
                CleanlinessKind.TOOLCHAIN,
                "h9",
                Cleanliness.UNKNOWN,
                detail="host unreachable",
                error=RuntimeError("h9 never answered"),
            ),
        ),
    )

    answer = await _run("status", full=True)
    out = capsys.readouterr().out

    assert "clean" in _row_line(out, "h0")
    assert "unknown" in _row_line(out, "h9")
    assert "host unreachable" in _row_line(out, "h9")
    assert answer.exit_code == 1  # still the install axis, untouched


@pytest.mark.asyncio
async def test_status_full_does_not_read_a_device_message_as_markup(
    monkeypatch, registered, capsys
):
    """The detail column carries text off a device, not words otto wrote.

    A `tc` error or an exception repr can hold square brackets, and a cell
    built by markup interpolation would read `[no such file]` as a style tag --
    swallowing it and everything after it, or dying on the unknown tag.
    """
    detail = "read failed: [no such file]"
    _wire_status(
        monkeypatch,
        InstallState.INSTALLED,
        {"acme": InstallState.INSTALLED},
        _report(
            _clean_row(
                CleanlinessKind.IMPAIRMENT,
                "core",
                Cleanliness.UNKNOWN,
                detail=detail,
                error=RuntimeError(detail),
            )
        ),
    )

    await _run("status", full=True)

    assert detail in _row_line(capsys.readouterr().out, "core")


# ── status: the repos this run does not apply to ─────────────────────────────


def _cells(line: str) -> list[str]:
    """One table row split back into its cells (the columns are two spaces apart)."""
    return [cell.strip() for cell in re.split(r"\s{2,}", line.strip()) if cell.strip()]


def _fleet_line(out: str, name: str) -> str:
    """The one FLEET-OF-INTEREST line whose first cell is *name*.

    Sliced between its own heading and the first cleanliness heading, for the
    reason ``_row_line`` is sliced: the install table above names the same
    repos, so a parser over the whole output finds two lines for one repo and
    cannot say which of them it just read.
    """
    lines = out.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("fleet of interest"))
    stop = next((i for i, line in enumerate(lines) if line.startswith(_SECTIONS)), len(lines))
    (row,) = [line for line in lines[start:stop] if _cells(line)[:1] == [name]]
    return row


@pytest.mark.asyncio
async def test_status_prints_a_not_applicable_row_for_the_repo_the_walks_skipped(
    monkeypatch, registered, capsys
):
    """A repo no loaded lab applies to gets a row that SAYS SO (D3).

    Whole-cell equality, never ``in``: "applicable" is a substring of "not
    applicable" exactly as "installed" is of "uninstalled", so a renderer that
    painted every scoped-out repo as in-scope — or every in-scope repo as
    skipped — passes a containment check and fails the only question the row
    is asked. The labs are IN the cell because "not applicable" without them
    reads as a bug in otto rather than as the wrong ``-l``.
    """
    monkeypatch.setattr(
        orchestrator,
        "status",
        _Recorder(
            ProjectStatus(
                overall=InstallState.INSTALLED,
                repos={"acme": InstallState.INSTALLED},
                scoping={
                    "acme": RepoScope(
                        applicable=True,
                        loaded_labs=("bench", "floor"),
                        applicable_labs=("bench",),
                        universe=("h0",),
                    ),
                    "widgets": RepoScope(
                        applicable=False, usable=False, loaded_labs=("bench", "floor")
                    ),
                },
            )
        ),
    )

    await _run("status")

    assert _state_rows(capsys.readouterr().out) == {
        "acme": "installed",
        "widgets": "not applicable (labs: bench, floor)",
    }


@pytest.mark.asyncio
async def test_status_prints_a_no_matching_hosts_row_for_the_host_starved_repo(
    monkeypatch, registered, capsys
):
    """The OTHER skipped shape gets the OTHER text — the axis that actually failed.

    This repo's labs DO apply; its ``host_patterns`` match nothing in them. The
    "not applicable (labs: ...)" cell would be a lie here and would send the
    reader to change ``-l``. Whole-cell equality for the reason its twin uses
    it: one text printed for both conditions passes every containment check.
    """
    _wire_status(
        monkeypatch,
        InstallState.INSTALLED,
        {"acme": InstallState.INSTALLED},
        scoping={
            "acme": RepoScope(
                applicable=True,
                usable=True,
                loaded_labs=("bench", "floor"),
                applicable_labs=("bench",),
                universe=("h0",),
            ),
            "widgets": RepoScope(
                applicable=True,
                usable=False,
                loaded_labs=("bench", "floor"),
                applicable_labs=("bench",),
                host_patterns=("sensor-.*",),
            ),
        },
    )

    await _run("status")

    assert _state_rows(capsys.readouterr().out) == {
        "acme": "installed",
        "widgets": "no matching hosts (host_patterns: sensor-.*)",
    }


@pytest.mark.asyncio
async def test_status_full_lists_each_repos_labs_and_hosts(monkeypatch, registered, capsys):
    """``--full`` shows the fleet each repo actually resolved, cell for cell.

    Cells rather than substrings, because the two lab columns are prefixes of
    each other in the commonest lab: a section that printed the LOADED labs
    where it promised the APPLICABLE ones renders "labs: bench, floor" where
    "labs: bench" belongs, and every containment check passes.
    """
    _wire_status(
        monkeypatch,
        InstallState.INSTALLED,
        {"acme": InstallState.INSTALLED},
        scoping={
            "acme": RepoScope(
                applicable=True,
                loaded_labs=("bench", "floor"),
                applicable_labs=("bench",),
                universe=("h0", "h1"),
            ),
            "widgets": RepoScope(applicable=False, usable=False, loaded_labs=("bench", "floor")),
        },
    )

    await _run("status", full=True)
    out = capsys.readouterr().out

    assert _cells(_fleet_line(out, "acme")) == ["acme", "labs: bench", "hosts: h0, h1"]
    assert _cells(_fleet_line(out, "widgets")) == ["widgets", "labs: (none)", "hosts: (none)"]


@pytest.mark.asyncio
async def test_status_full_prints_no_section_for_an_empty_scoping_mapping(
    monkeypatch, registered, capsys
):
    """An empty mapping renders no section — a heading over an empty table is worse.

    A RENDERER-LEVEL pin, and the title says which emptiness it is about. An
    empty ``scoping`` means nothing was RESOLVED — a library context, an
    unavailable bootstrap — and it is the only input that reaches the early
    return. It is emphatically NOT the whole-lab fallback: an undeclared repo
    still gets a verdict, so a lab where nobody wrote ``[project]`` arrives
    here with a row per repo and prints one. This test used to claim it proved
    that fallback case, while wiring the empty mapping by hand — pinning its
    own premise. ``test_status_full_renders_a_row_per_repo_on_an_undeclared_lab``
    asks the real pipeline instead.
    """
    _wire_status(monkeypatch, InstallState.INSTALLED, {"acme": InstallState.INSTALLED})

    await _run("status", full=True)

    assert "fleet of interest" not in capsys.readouterr().out


@pytest.mark.asyncio
async def test_status_full_renders_a_row_per_repo_on_an_undeclared_lab(
    monkeypatch, registered, capsys
):
    """The whole-lab fallback still gets a fleet-of-interest row per repo.

    THE PIPELINE, NOT A HAND-WIRED REPORT. Every other test in this section
    hands the renderer a ``ProjectStatus`` it built itself, which can only ever
    prove what the renderer does with an input someone chose. The question here
    is which inputs ``orchestrator.status()`` actually PRODUCES, and the answer
    for a lab where no repo declared ``[project]`` is a verdict per repo — the
    fallback resolves to "applies to every loaded lab, targets every host",
    which is a fact about the run and not an absence of one. So the section
    renders, and each row names the whole lab.

    ``_counts`` is stubbed False so no repo is asked for an install state:
    that keeps the test on the one thing it is about (what ``scoping`` gets
    filled with) instead of dragging in fleet walks that would need hosts.
    ``_lab`` is stubbed with a context carrying REAL verdicts from
    :func:`~otto.config.scope.resolve_scopes` — the fallback has to come out of
    the resolver, not out of the test, or the pin is worth nothing.

    Cell equality, matching this section's other assertions: a renderer that
    printed the applicable labs where the universe belongs, or "(none)" for a
    fallback repo, passes any containment check.
    """
    import types

    from otto.config.scope import resolve_scopes

    repos = [
        types.SimpleNamespace(name="acme", sut_dir="/repos/acme", project_scope=None),
        types.SimpleNamespace(name="widgets", sut_dir="/repos/widgets", project_scope=None),
    ]
    hosts = {
        "h0": types.SimpleNamespace(source_lab="bench"),
        "h1": types.SimpleNamespace(source_lab="floor"),
    }
    ctx = types.SimpleNamespace(
        scopes=resolve_scopes(repos, ["bench", "floor"], hosts),
        # ``status`` asks ``otto.config.scope.active``, which reads the switch
        # tuples as well as the verdicts. Empty, as the real ``OttoContext``
        # defaults them: this lab is decided by declarations alone.
        include_projects=(),
        exclude_projects=(),
    )
    ctx.for_repo = lambda name: types.SimpleNamespace(_repo=name)

    monkeypatch.setattr(orchestrator, "_lab", lambda: (ctx, repos))
    monkeypatch.setattr(orchestrator, "_counts", lambda actions: False)
    monkeypatch.setattr(orchestrator, "cleanliness", _Recorder(_report()))

    await _run("status", full=True)
    out = capsys.readouterr().out

    # Asserted before the row parser runs: `_fleet_line` locates its slice with
    # a bare `next()`, so a missing section raises StopIteration out of this
    # coroutine and reports as a RuntimeError naming asyncio internals. The
    # regression this pins is exactly "the section is absent", and it has to say
    # so.
    assert "fleet of interest" in out
    assert _cells(_fleet_line(out, "acme")) == ["acme", "labs: bench, floor", "hosts: h0, h1"]
    assert _cells(_fleet_line(out, "widgets")) == [
        "widgets",
        "labs: bench, floor",
        "hosts: h0, h1",
    ]


@pytest.mark.asyncio
async def test_status_full_renders_both_rows_for_a_host_starved_repo(
    monkeypatch, registered, capsys
):
    """THE PIPELINE, not a hand-built report: real verdicts, real ``status()``, real cells.

    The regression this pins crashed ``status()`` outright — a declared repo
    whose labs apply while its ``host_patterns`` match nothing raised out of its
    own fleet walk — so the pin has to come from
    :func:`~otto.project.orchestrator.status` over verdicts
    :func:`~otto.config.scope.resolve_scopes` produced, not from a
    ``ProjectStatus`` the test wrote. A hand-wired renderer test hid exactly
    this kind of divergence once already (056b1032).

    BOTH surfaces, because they answer different questions: the status table
    says why the repo has no state, and ``--full``'s fleet-of-interest row says
    what it resolved to (its labs DO apply; the host column is empty). Cell
    equality throughout.
    """
    import re as _re
    import types

    from otto.config.scope import ProjectScopeConfig, resolve_scopes

    repos = [
        types.SimpleNamespace(name="acme", sut_dir="/repos/acme", project_scope=None),
        types.SimpleNamespace(
            name="widgets",
            sut_dir="/repos/widgets",
            project_scope=ProjectScopeConfig([_re.compile("bench")], [_re.compile("sensor-.*")]),
        ),
    ]
    hosts = {
        "h0": types.SimpleNamespace(source_lab="bench"),
        "h1": types.SimpleNamespace(source_lab="floor"),
    }
    ctx = types.SimpleNamespace(
        scopes=resolve_scopes(repos, ["bench", "floor"], hosts),
        # ``status`` asks ``otto.config.scope.active``, which reads the switch
        # tuples as well as the verdicts. Empty, as the real ``OttoContext``
        # defaults them: this lab is decided by declarations alone.
        include_projects=(),
        exclude_projects=(),
    )
    ctx.for_repo = lambda name: types.SimpleNamespace(_repo=name)

    monkeypatch.setattr(orchestrator, "_lab", lambda: (ctx, repos))
    monkeypatch.setattr(orchestrator, "_counts", lambda actions: False)
    monkeypatch.setattr(orchestrator, "cleanliness", _Recorder(_report()))

    await _run("status", full=True)
    out = capsys.readouterr().out

    assert "fleet of interest" in out
    assert _cells(_fleet_line(out, "widgets")) == ["widgets", "labs: bench", "hosts: (none)"]
    # The status table is everything ABOVE the fleet-of-interest heading: both
    # sections print a row per repo, so a parser over the whole output finds
    # two lines for `widgets` and cannot say which one it just read.
    lines = out.splitlines()
    heading = next(i for i, line in enumerate(lines) if line.startswith("fleet of interest"))
    (row,) = [line for line in lines[:heading] if _cells(line)[:1] == ["widgets"]]
    assert _cells(row) == ["widgets", "no matching hosts (host_patterns: sensor-.*)"]


@pytest.mark.asyncio
async def test_bare_status_prints_no_fleet_of_interest_section(monkeypatch, registered, capsys):
    """The scoping detail is ``--full``'s, so a plain status stays one line per repo."""
    _wire_status(
        monkeypatch,
        InstallState.INSTALLED,
        {"acme": InstallState.INSTALLED},
        scoping={
            "acme": RepoScope(
                applicable=True,
                loaded_labs=("bench",),
                applicable_labs=("bench",),
                universe=("h0",),
            )
        },
    )

    await _run("status")

    assert "fleet of interest" not in capsys.readouterr().out


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

    repos = [_PanelRepo("first-repo 1.0"), _PanelRepo("second-repo 1.0")]
    with patch("otto.config.get_repos", return_value=repos):
        result = runner.invoke(run_app, ["--list-instructions"])

    assert result.exit_code == 0, result.output
    (titles,) = [ln for ln in result.output.splitlines() if "otto defaults" in ln]
    assert "first-repo" in titles, "the repo panels share the title line with otto's"
    assert titles.index("otto defaults") < titles.index("first-repo") < titles.index("second-repo")


# ── The merge: one command, every body's flags ───────────────────────────────


@options
class _Shared:
    """A base two repos can agree on: one declaring class, so one flag."""

    lab_env: Annotated[str, typer.Option(help="Lab environment name.")] = "bench"


@options
class _DeployOptions:
    """A repo-added instruction's own flags."""

    target: Annotated[str, typer.Option(help="Where to deploy.")] = "bench"


class TestMergedFlags:
    def test_a_repos_fields_join_the_first_party_flags(self, registered) -> None:
        @options
        class WidgetInstall(InstallOptions):
            variant: Annotated[str, typer.Option(help="Firmware variant.")] = "field"

        with registering_repo("widget"):

            @register_project_actions
            class Widget(ProjectActions):
                @instruction(options=WidgetInstall)
                async def install(self, opts: WidgetInstall):
                    return await super().install(opts)

        publish_project_instructions()
        out = runner.invoke(run_app, ["install", "--help"]).output

        assert "--ensure" in out
        assert "--recover-partial" in out
        assert "--variant" in out

    def test_two_repos_sharing_a_base_get_one_flag(self, registered) -> None:
        """One BASE CLASS, one flag — identity of the declaring class decides.

        Two repos that agree on a field by inheriting it from one class are
        describing one flag; merging by NAME instead would either refuse this
        (the collision below) or silently keep whichever copy came first.
        """

        @options
        class AInstall(_Shared, InstallOptions):
            pass

        @options
        class BInstall(_Shared, InstallOptions):
            pass

        with registering_repo("a"):

            @register_project_actions
            class A(ProjectActions):
                @instruction(options=AInstall)
                async def install(self, opts: AInstall):
                    return await super().install(opts)

        with registering_repo("b"):

            @register_project_actions
            class B(ProjectActions):
                @instruction(options=BInstall)
                async def install(self, opts: BInstall):
                    return await super().install(opts)

        publish_project_instructions()
        out = runner.invoke(run_app, ["install", "--help"]).output

        assert out.count("--lab-env") == 1, out
        params = merged_option_params(PROJECT_INSTRUCTIONS.get("install"))
        assert [p.name for p in params].count("lab_env") == 1

    def test_two_repos_each_declaring_the_same_field_collide_loud(self, registered) -> None:
        """The same NAME from two classes is two flags, and otto refuses to guess.

        Silently keeping the first would hand repo ``b``'s body repo ``a``'s
        value — a flag that reads as its own and is not — so the publish fails
        and the message says how to share one.
        """

        @options
        class AInstall(InstallOptions):
            lab_env: Annotated[str, typer.Option(help="A's lab environment.")] = "bench"

        @options
        class BInstall(InstallOptions):
            lab_env: Annotated[str, typer.Option(help="B's lab environment.")] = "floor"

        with registering_repo("a"):

            @register_project_actions
            class A(ProjectActions):
                @instruction(options=AInstall)
                async def install(self, opts: AInstall):
                    return await super().install(opts)

        with registering_repo("b"):

            @register_project_actions
            class B(ProjectActions):
                @instruction(options=BInstall)
                async def install(self, opts: BInstall):
                    return await super().install(opts)

        with pytest.raises(OptionsCollisionError) as excinfo:
            publish_project_instructions()

        message = str(excinfo.value)
        assert "'install'" in message
        assert "'lab_env'" in message
        assert "repo 'a'" in message
        assert "repo 'b'" in message
        assert "share one base class" in message

    def test_a_repo_added_instruction_is_published_under_its_module(self, registered) -> None:
        """A name otto never declared is published too, attributed to ITS declarer.

        ``module`` is what ``--list-instructions`` attributes panels by, so a
        repo's own project instruction has to carry the repo's module or it
        lands in otto's panel.
        """
        with registering_repo("widget"):

            @register_project_actions
            class Widget(ProjectActions):
                @instruction(options=_DeployOptions)
                async def deploy(self, opts: _DeployOptions) -> Result:
                    """Deploy this repo's firmware."""
                    return Result(Status.Success, value=opts.target)

        publish_project_instructions()

        entry = INSTRUCTIONS.get("deploy")
        assert entry.registered_by is None
        assert entry.module == Widget.__module__
        assert "--target" in runner.invoke(run_app, ["deploy", "--help"]).output

    def test_the_published_command_serialises_for_the_completion_cache(self, registered) -> None:
        """TAB completion reads the command's SIGNATURE, which this one builds by hand.

        ``_serialize_options`` refuses a parameter whose annotation is not
        ``Annotated[..., typer.Option(...)]`` and skips the whole command, so a
        leaf that carried only ``**kwargs`` — or annotations that did not match
        its signature — would silently lose its flags from every completion.
        """
        import typer.main

        from otto.config.completion_cache import _serialize_options

        cmd = typer.main.get_command(INSTRUCTIONS.get("install").sub_app)
        leaf = cmd.commands["install"] if hasattr(cmd, "commands") else cmd
        serialised = _serialize_options(leaf.callback, command_name="install")

        assert serialised is not None
        assert {o["name"] for o in serialised} >= {"ensure", "recover_partial"}

    def test_the_published_help_is_the_lab_wide_summary(self, registered) -> None:
        """`--help` describes the command, which walks EVERY repo.

        The bodies' docstrings are per-repo ("this repo's products"), so the
        published summary comes from each declaration's ``help=`` instead --
        and ``status`` keeps the exit-code contract a script branches on, which
        no other surface states.
        """
        install = runner.invoke(run_app, ["install", "--help"]).output
        status = runner.invoke(run_app, ["status", "--help"]).output

        assert "every repo" in install
        assert "exit 0" in status

    def test_a_hand_built_repo_added_entry_is_refused_at_publish(self, registered) -> None:
        """The backstop holds for a REPO's own name, where the modules match.

        A repo declares ``deploy`` on its actions class and hand-registers an
        ``InstructionEntry`` under the same name from the same init module.
        Keying the republish on the entry's ``module`` would read that as "mine,
        overwrite it" and silently take the name; the registry's own attribution
        (``origin``) does not, so the second registration is refused.
        """
        with registering_repo("widget"):

            @register_project_actions
            class Widget(ProjectActions):
                @instruction(options=_DeployOptions, help="Deploy the lab's firmware.")
                async def deploy(self, opts: _DeployOptions) -> Result:
                    """Deploy this repo's firmware."""
                    return Result(Status.Success, value=opts.target)

        from otto.instructions import InstructionEntry

        INSTRUCTIONS.register(
            "deploy",
            InstructionEntry(
                name="deploy",
                sub_app=typer.Typer(),
                module=Widget.__module__,
                registered_by="widget",
            ),
            origin=Widget.__module__,
        )

        with pytest.raises(ValueError, match="already registered"):
            publish_project_instructions()

    def test_publishing_twice_republishes_its_own_entries(self, registered) -> None:
        """The republish path still works -- the backstop must not refuse otto's own.

        ``registered`` has already published once, so this second call is the
        one a re-entrant bootstrap (or a test) makes; it must overwrite rather
        than collide, and the entry it leaves must still be a usable command.
        """
        publish_project_instructions()  # must not raise

        entry = INSTRUCTIONS.get("install")
        assert entry.module == ACTIONS_MODULE
        assert "--ensure" in runner.invoke(run_app, ["install", "--help"]).output

    def test_a_hand_built_first_party_entry_is_refused_at_publish(self, registered) -> None:
        """The registry-order backstop: a repo cannot pre-empt a published name.

        Only an entry THIS module published is overwritten. Anything else is
        left for ``INSTRUCTIONS.register`` to refuse, which is what stops a repo
        that hand-built an ``InstructionEntry`` under a project instruction's
        name from shadowing it — the route the decorator's guard never sees.
        """
        from otto.instructions import InstructionEntry

        INSTRUCTIONS.unregister("install")
        INSTRUCTIONS.register(
            "install",
            InstructionEntry(
                name="install", sub_app=typer.Typer(), module="repo.init", registered_by="repo"
            ),
            origin="repo.init",
        )

        with pytest.raises(ValueError, match="already registered"):
            publish_project_instructions()
