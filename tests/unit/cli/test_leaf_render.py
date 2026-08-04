"""The leaf-invoke render seam: return values become output + exit codes.

Pins the documented "Return values" contract (``docs/guide/extending-cli.md``)
at the wrapper: a registered command or instruction returns an ``otto.result``
family value and the process exit code derives from its own ``exit_code``; a
plain non-``None`` value is printed as-is with exit 0; ``None`` renders
NOTHING unless the leaf installs a :class:`~otto.cli.invoke.RenderPolicy`
(``ctx.meta``) opting into a completion message — every side-effect-only
first-party leaf returns ``None``, so a default "done" line here would leak
into every otto command.

Wrapper-level tests drive the PRODUCTION seam through
``tests/_fixtures/dispatch.DispatchRunner`` (``resolve_spec_command`` +
``wrap_leaf_callbacks``, exactly what the root dispatch composes) — the
cleanest driver for exit-code assertions, since click's standalone main
converts the render's ``typer.Exit`` into the process exit code. The direct
``render_leaf_value`` tests below cover the branch matrix (migrated from the
retired ``expose._render_result`` unit tests).
"""

from pathlib import Path
from unittest import mock

import pytest
import typer

from otto.cli.invoke import RENDER_POLICY_KEY, RenderPolicy, render_leaf_value
from otto.result import CommandResult, Result, Results
from otto.utils import Status
from tests._fixtures.dispatch import DispatchRunner

# ---------------------------------------------------------------------------
# The production seam: return values rendered by the leaf-invoke wrapper
# ---------------------------------------------------------------------------


def test_async_leaf_failing_result_exits_nonzero_with_message():
    """A failing Result from a plain async leaf → red msg + its exit code."""
    app = typer.Typer(name="fail-tool")

    @app.command()
    async def flaky() -> Result:
        return Result(Status.Failed, msg="did not converge")

    r = DispatchRunner().invoke(app, [])
    assert r.exit_code == 1, r.output
    assert "did not converge" in r.output


def test_results_retcode_passes_through_as_exit_code():
    """A Results carrying a shell retcode surfaces it ssh-like, unchanged."""
    app = typer.Typer(name="retcode-tool")

    @app.command()
    async def run42() -> Results:
        return Results.collect(
            [CommandResult(Status.Failed, value="", command="exit 42", retcode=42)]
        )

    r = DispatchRunner().invoke(app, [])
    assert r.exit_code == 42, r.output


def test_never_ran_command_result_exits_255():
    """retcode -1 (command never ran) maps to ssh's connection-error 255."""
    app = typer.Typer(name="noran-tool")

    @app.command()
    async def broken() -> CommandResult:
        return CommandResult(Status.Error, command="x", retcode=-1)

    r = DispatchRunner().invoke(app, [])
    assert r.exit_code == 255, r.output


def test_plain_value_is_printed_with_exit_zero():
    """The documented third-party fallback: non-Result values print as-is."""
    app = typer.Typer(name="facts-tool")

    @app.command()
    async def facts() -> list:
        return ["third", "party"]

    r = DispatchRunner().invoke(app, [])
    assert r.exit_code == 0, r.output
    assert "third" in r.output


def test_none_return_is_silent_by_default():
    """Every first-party leaf returns None — the seam MUST NOT print for it.

    Exact-stdout assertion: a spurious "done" line here would appear on every
    otto command (the hazard the None-silent default exists to prevent).
    """
    app = typer.Typer(name="quiet-tool")

    @app.command()
    async def quiet() -> None:
        pass

    r = DispatchRunner().invoke(app, [])
    assert r.exit_code == 0, r.output
    assert r.output == ""


def test_none_message_policy_opts_into_a_completion_line():
    """A leaf that wants a completion message says so via RenderPolicy."""
    app = typer.Typer(name="verb-tool")

    @app.command()
    async def verb(ctx: typer.Context) -> None:
        ctx.meta[RENDER_POLICY_KEY] = RenderPolicy(none_message="done")

    r = DispatchRunner().invoke(app, [])
    assert r.exit_code == 0, r.output
    assert "done" in r.output


def test_suite_runner_exit_code_rides_the_renderer_silently():
    """`otto test <Suite>`'s rc reaches the process without a printed line.

    The suite runner returns a CommandResult instead of raising typer.Exit
    (the last suppressed ast-grep violation). Two halves of that contract:
    pytest's own rc survives verbatim — NOT flattened to 1, which a bare
    ``Result`` would do — and nothing is printed, because pytest already
    reported everything the user needs. The no-typer.Exit half is the
    ``typer-exit-outside-cli`` rule's job, not this test's.
    """
    from otto.suite.register import SUITES, register_suite_class
    from otto.suite.run import SuiteRunResult

    class _SuiteRcFive:
        pass

    register_suite_class(_SuiteRcFive)
    app = typer.Typer(name="test")
    app.add_typer(SUITES.get("_SuiteRcFive").sub_app)

    def fake_run_suite(suite, **kw):
        return SuiteRunResult(
            exit_code=5,
            junit_paths=[],
            stability_report=None,
            stability_unstable=False,
            output_dir=Path(),
        )

    with mock.patch("otto.suite.run.run_suite", fake_run_suite):
        r = DispatchRunner().invoke(app, ["_SuiteRcFive"], spec_name="test")

    assert r.exit_code == 5, r.output
    assert r.output == ""


def test_success_message_policy_renders_on_ok_result():
    """RenderPolicy.success replaces value-rendering for an ok, non-command Result."""
    app = typer.Typer(name="xfer-tool")

    @app.command()
    async def xfer(ctx: typer.Context) -> Result:
        ctx.meta[RENDER_POLICY_KEY] = RenderPolicy(success="Transfer complete.")
        return Result(Status.Success)

    r = DispatchRunner().invoke(app, [])
    assert r.exit_code == 0, r.output
    assert "Transfer complete." in r.output


def test_sync_leaf_return_value_shares_the_render_path():
    """Sync and async leaves render through the same seam (one path after the bridge)."""
    app = typer.Typer(name="sync-tool")

    @app.command()
    def status() -> Result:
        return Result(Status.Error, msg="backend down")

    r = DispatchRunner().invoke(app, [])
    assert r.exit_code == 2, r.output
    assert "backend down" in r.output


# ---------------------------------------------------------------------------
# render_leaf_value branch matrix (direct calls)
# ---------------------------------------------------------------------------


def _exit_code(value, policy=None):
    try:
        render_leaf_value(value, policy)
    except typer.Exit as e:
        return e.exit_code
    return 0


def test_command_retcode_passthrough():
    res = Results.collect([CommandResult(Status.Failed, value="", command="exit 42", retcode=42)])
    assert _exit_code(res) == 42


def test_command_never_ran_exits_255():
    assert _exit_code(CommandResult(Status.Error, command="x", retcode=-1)) == 255


def test_status_mapping_for_plain_results():
    assert _exit_code(Result(Status.Error, msg="boom")) == 2
    assert _exit_code(Result(Status.Failed, msg="no")) == 1
    assert _exit_code(Result(Status.Skipped)) == 0


def test_ok_result_prints_success_message(capsys):
    render_leaf_value(Result(Status.Success), RenderPolicy(success="Transfer complete."))
    assert "Transfer complete." in capsys.readouterr().out


def test_ok_transfer_mapping_prints_per_file_lines(capsys):
    per_file = {Path("a.bin"): Result(Status.Success, value=Path("/dst/a.bin"))}
    render_leaf_value(Result(Status.Success, value=per_file))
    out = capsys.readouterr().out
    assert "a.bin" in out
    assert "/dst/a.bin" in out


def test_failed_mapping_prints_per_entry_diagnostics(capsys):
    per_file = {Path("b.bin"): Result(Status.Error, msg="b.bin: reset")}
    with pytest.raises(typer.Exit):
        render_leaf_value(Result(Status.Error, value=per_file, msg="1 file failed"))
    assert "b.bin: reset" in capsys.readouterr().out


def test_command_results_print_nothing_on_ok(capsys):
    render_leaf_value(Results.collect([CommandResult(Status.Success, retcode=0)]))
    assert capsys.readouterr().out == ""


def test_command_results_print_per_entry_diagnostics_on_failure(capsys):
    res = Results.collect(
        [CommandResult(Status.Error, value="", command="x", retcode=3, msg="boom")]
    )
    with pytest.raises(typer.Exit):
        render_leaf_value(res)
    assert "boom" in capsys.readouterr().out


def test_plain_value_fallback(capsys):
    assert _exit_code(["third", "party"]) == 0
    assert "third" in capsys.readouterr().out


def test_none_is_silent_without_a_policy(capsys):
    render_leaf_value(None)
    assert capsys.readouterr().out == ""


def test_none_prints_the_policy_none_message(capsys):
    render_leaf_value(None, RenderPolicy(none_message="done"))
    assert "done" in capsys.readouterr().out
