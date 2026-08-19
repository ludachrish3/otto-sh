"""The ``--dry-run`` seam: validate, print what would run, and stop before the body.

Spec: ``docs/superpowers/specs/2026-08-15-dry-run-contract-design.md`` §1-2.

Every "the body did not run" assertion in this file carries its POSITIVE
CONTROL in the same test — the same command, the same seam, without ``-n`` —
because a guard that only proves absence passes just as happily against a
command that does nothing at all.
"""

import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
import typer

from otto.cli.expose import HostGroup
from otto.cli.invoke import LabReference, render_leaf_value
from otto.cli.registry import CommandSpec, cli_command, register_cli_command
from otto.result import CommandResult, NotRunResult, Result
from otto.utils import DRY_RUN_HEADLINE, Status, cli_exposed
from tests._fixtures.dispatch import DispatchRunner
from tests.conftest import active_context

runner = DispatchRunner()


def flat(text: str) -> str:
    """Collapse rich's wrapping/padding so a line can be matched as one string."""
    return " ".join(text.split())


# ---------------------------------------------------------------------------
# The flag, threaded exactly the way `output_dir` is threaded
# ---------------------------------------------------------------------------


class TestRegistrationFlagThreading:
    """``dry_run_preview`` reaches the spec from all three registration seams."""

    def test_spec_defaults_to_the_safe_answer(self) -> None:
        assert CommandSpec(name="x", loader=None).dry_run_preview is False

    def test_register_cli_command_threads_the_flag(self) -> None:
        from otto.cli.registry import CLI_COMMANDS

        register_cli_command("_seam_optin", lambda: None, dry_run_preview=True)
        register_cli_command("_seam_default", lambda: None)
        try:
            assert CLI_COMMANDS.get("_seam_optin").dry_run_preview is True
            # positive control: the same call without the kwarg stays safe
            assert CLI_COMMANDS.get("_seam_default").dry_run_preview is False
        finally:
            CLI_COMMANDS.unregister("_seam_optin")
            CLI_COMMANDS.unregister("_seam_default")

    def test_cli_command_decorator_threads_the_flag(self) -> None:
        from otto.cli.registry import CLI_COMMANDS

        @cli_command(name="_seam_deco", lab_free=True, dry_run_preview=True)
        def _leaf() -> None: ...

        try:
            assert CLI_COMMANDS.get("_seam_deco").dry_run_preview is True
        finally:
            CLI_COMMANDS.unregister("_seam_deco")

    def test_cli_exposed_stamps_the_flag_and_defaults_false(self) -> None:
        @cli_exposed(dry_run_preview=True)
        async def previewing(self) -> None: ...

        @cli_exposed
        async def ordinary(self) -> None: ...

        assert previewing.__cli_dry_run_preview__ is True
        assert ordinary.__cli_dry_run_preview__ is False


# ---------------------------------------------------------------------------
# The SHIPPED registrations — not the harness's private copy of them
# ---------------------------------------------------------------------------


def _shipped(name: str) -> Any:
    """The production ``CommandSpec`` for *name*, from otto's own composition list."""
    from otto.cli.builtin_commands import register_builtin_commands
    from otto.cli.registry import CLI_COMMANDS

    register_builtin_commands()  # idempotent
    return CLI_COMMANDS.get(name)


class TestTheShippedRegistrationCarriesTheFlag:
    """Pin the registration `otto` actually installs, not one a test declared.

    `tests/_fixtures/dispatch.DispatchRunner` builds its OWN `CommandSpec`, so
    every `tests/unit/link/test_cli.py` and `tests/unit/tunnel/test_cli.py`
    assertion is made against a spec the HARNESS wrote. That is the
    mirrored-default drift this codebase keeps getting bitten by: drop
    `dry_run_preview=True` from `otto/cli/builtin_commands.py` and production
    `otto link impair -n` silently seam-stops, its whole deep preview replaced
    by the generic block, while the harness tests notice nothing.

    Two independent guards, because either alone is escapable:

    * this class, which reads the production registry directly, and
    * `DispatchRunner` reading the same registry (see
      `shipped_dry_run_preview`) so the link/tunnel CLI tests are DRIVEN by
      the shipped flag and go red as a set if it disappears.
    """

    @pytest.mark.parametrize("name", ["link", "tunnel"])
    def test_link_and_tunnel_ship_the_opt_in(self, name: str) -> None:
        assert _shipped(name).dry_run_preview is True, (
            f"`otto {name}` no longer declares dry_run_preview=True, so `otto {name} "
            f"... -n` now stops at the seam and prints the generic block instead of "
            f"the plan/refusals/gaps preview it ships"
        )

    @pytest.mark.parametrize("name", ["host", "test", "run", "cov", "docker"])
    def test_everything_else_keeps_the_safe_default(self, name: str) -> None:
        """POSITIVE CONTROL for the two above: the flag is not simply universal.

        `otto host <id> exec 'uptime' -n` deliberately does NOT opt in — the
        echoed command IS the whole announcement, so there is nothing a body
        run could add — and `test`'s opt-in lives on the suite LEAF, not here.
        """
        assert _shipped(name).dry_run_preview is False

    def test_the_harness_reads_the_shipped_flag_instead_of_declaring_one(self) -> None:
        from tests._fixtures.dispatch import shipped_dry_run_preview

        assert shipped_dry_run_preview("link") is True
        assert shipped_dry_run_preview("tunnel") is True
        # POSITIVE CONTROLS: a registered command without the flag, and a name
        # that has no shipped registration at all (an app built inside a test),
        # both get the safe default rather than an inherited True.
        assert shipped_dry_run_preview("host") is False
        assert shipped_dry_run_preview("seamdemo") is False


class TestTheHostVerbsThatOwnTheirDryRun:
    """Which `otto host` verbs opt out of the seam, pinned as a SET.

    A verb opts in exactly when its body already produces something the
    generic block cannot: `write_file` announces the action, byte count and
    destination without the payload; every transfer verb renders
    `_dry_run_transfer`'s per-file plan (and parses `--mode`, so a typo'd
    `--mode 789` is still caught). Each short-circuits on `is_dry_run()` above
    any device contact — that is the precondition for the flag, not a
    consequence of it.

    The negative half is the interesting one and is asserted in the same test:
    the command verb deliberately does NOT opt in (the echoed command IS the
    whole announcement, so a body run adds nothing and only widens the
    surface), and neither do `exists`/`ls`, which have no preview to give and
    now decline at the library layer instead. `UnixHost.load` is the third:
    its body would read a `put` decline and report `staging <file> failed`, a
    fabricated failure, so the seam stop is the honest answer there.

    `reboot`, `shutdown` and `power` are the fourth kind and the reason the
    rule is not simply "opt in when the preview is richer": they own a
    library-layer arm AND keep the stop, because they are the verbs that touch
    power. See the comment on the negative loop.

    (The verb is `run`, not `exec` — `BaseHost.exec` carries no `@cli_exposed`
    stamp and reaches no CLI surface, whatever the plan text calls it.)
    """

    def test_the_opted_in_verbs_are_exactly_the_ones_with_a_shipped_preview(self) -> None:
        from otto.host.docker_host import DockerContainerHost
        from otto.host.embedded_host import EmbeddedHost, ZephyrHost
        from otto.host.file_ops import PosixFileOps
        from otto.host.host import BaseHost
        from otto.host.local_host import LocalHost
        from otto.host.unix_host import UnixHost

        # EVERY class that overrides a transfer verb, not just the one the
        # brief named. `put`/`get` are re-stamped on all four host classes and
        # the seam reads the stamp on the class that RESOLVES, so a list that
        # covers three of them leaves the fourth free to lose its flag in
        # silence — which is the drift this whole class exists to catch.
        opted = [
            PosixFileOps.write_file,
            UnixHost.get,
            UnixHost.put,
            LocalHost.get,
            LocalHost.put,
            DockerContainerHost.get,
            DockerContainerHost.put,
            EmbeddedHost.get,
            EmbeddedHost.put,
            ZephyrHost.load,
            ZephyrHost.unload,
        ]
        for verb in opted:
            assert verb.__cli_dry_run_preview__ is True, (
                f"{verb.__qualname__} lost its opt-in, so `--dry-run` now stops "
                f"before the body and the preview it ships is gone"
            )

        # POSITIVE CONTROL — the same attribute on the verbs that must STAY at
        # the safe default. Without this the loop above passes just as happily
        # against a `cli_exposed` that stamps True unconditionally.
        #
        # THE THREE POWER VERBS ARE HERE ON PURPOSE, and `reboot` is the one
        # that argues for itself. All three now own a library-layer
        # `is_dry_run()` arm that announces and acts on nothing, and all three
        # still keep the seam default — a DELIBERATE DOUBLE GUARD rather than
        # an oversight. The stakes are asymmetric: a regressed transfer preview
        # mutates a file, a regressed reboot arm power-cycles hardware, on the
        # very flag that means "I am not sure". The two guards fail
        # independently (the seam reads the typed root options via
        # `dry_run_requested`, the arm reads the active context via
        # `is_dry_run`), so a context-plumbing regression takes out one and
        # leaves the other standing.
        #
        # What the stop costs, recorded so it is a trade and not an oversight:
        # `reboot`'s block loses the controller name and the resolved wait
        # bounds, and a controller-less `--hard` exits 0 printing "would run"
        # instead of raising. Both are reclaimable seam-side later (see
        # `todo/dry-run-followups-2026-08-15.md`).
        for verb in (
            UnixHost.run,
            PosixFileOps.exists,
            PosixFileOps.ls,
            UnixHost.load,
            BaseHost.reboot,
            UnixHost.shutdown,
            BaseHost.power,
        ):
            assert verb.__cli_dry_run_preview__ is False, (
                f"{verb.__qualname__} opted into running its body under a dry run"
            )


# ---------------------------------------------------------------------------
# The default: the body never runs
# ---------------------------------------------------------------------------


def _recording_app(calls: list[str]) -> typer.Typer:
    """A two-leaf app whose body is observable, standing in for any command.

    Two commands, not one: Typer flattens a single-command, callback-free app
    into a bare leaf (``registry._typer_app_flattens``), which would make this
    stand in for ``otto monitor`` rather than for a group like ``otto link``.
    """
    app = typer.Typer(name="seamdemo")

    @app.command("go")
    async def go(  # ty: ignore[unused-function]
        target: str,
        delay: int = typer.Option(0, "--delay"),
    ) -> None:
        calls.append(f"{target}:{delay}")

    @app.command("idle")
    async def idle() -> None:  # ty: ignore[unused-function]
        calls.append("idle")

    return app


class TestSeamDefaultStopsTheBody:
    def test_body_does_not_run_under_n_but_does_without_it(self) -> None:
        calls: list[str] = []
        app = _recording_app(calls)

        with active_context(dry_run=True):
            dry = runner.invoke(app, ["go", "edge", "--delay", "50"])
        assert dry.exit_code == 0, dry.output
        assert dry.exception is None, dry.exception
        assert calls == [], f"the seam let the command body run under --dry-run: {calls}"

        # POSITIVE CONTROL, same command, same seam: without -n the body runs.
        # Without this the assertion above would pass against a leaf that never
        # does anything at all.
        real = runner.invoke(app, ["go", "edge", "--delay", "50"])
        assert real.exit_code == 0, real.output
        assert calls == ["edge:50"], "the body did not run even WITHOUT --dry-run"
        assert DRY_RUN_HEADLINE not in real.output

    def test_the_block_names_the_command_the_lab_and_says_nothing_ran(self) -> None:
        calls: list[str] = []
        app = _recording_app(calls)
        with active_context(dry_run=True):
            dry = runner.invoke(app, ["go", "edge", "--delay", "50"])
        out = flat(dry.output)
        assert DRY_RUN_HEADLINE in out
        assert "would run: seamdemo go edge --delay 50" in out
        assert "lab: " in out
        # SUPPRESS THE PAYLOAD, NEVER THE ANNOUNCEMENT: an empty dry run is a bug.
        assert dry.output.strip(), "the dry run printed nothing at all"

    def test_the_announcement_survives_a_silenced_logger(self) -> None:
        """The block is the product, so no log mode or level may fold it away."""
        calls: list[str] = []
        app = _recording_app(calls)
        logging.disable(logging.CRITICAL)
        try:
            with active_context(dry_run=True):
                dry = runner.invoke(app, ["go", "edge"])
        finally:
            logging.disable(logging.NOTSET)
        assert DRY_RUN_HEADLINE in dry.output, (
            "silencing logging silenced the dry run's only output"
        )
        assert calls == []


class TestTheRealRootFlagReachesTheSeam:
    """End to end through ``otto``'s own root callback, not an injected context.

    Everything above installs the dry-run state with ``active_context``. This
    drives the shipped root app so the path the USER takes —
    ``--dry-run`` → ``RootOptions`` on ``ctx.meta`` → the seam — is the one
    under test, and so a ``lab_free`` command (which loads no lab and so has
    no context to inherit a flag from) is covered rather than assumed.
    """

    def test_the_flag_stops_a_lab_free_command_and_it_writes_nothing(self, tmp_path: Any) -> None:
        from typer.testing import CliRunner

        from otto.cli.main import app

        cli = CliRunner()
        out = tmp_path / "schemas"
        args = ["schema", "export", "--out", str(out), "--builtins-only"]

        # prog_name: click derives the root context's own name from argv, which
        # under a runner is "root". Naming it here pins the shape the installed
        # console script actually prints.
        dry = cli.invoke(app, ["--dry-run", *args], env={"OTTO_LAB": ""}, prog_name="otto")
        assert dry.exit_code == 0, dry.output
        assert DRY_RUN_HEADLINE in flat(dry.output)
        assert "would run: otto schema export --out" in flat(dry.output)
        assert "--builtins-only" in flat(dry.output)
        assert not out.exists(), "the dry run wrote the schema files anyway"

        # POSITIVE CONTROL, same command, same seam: without -n the files land.
        real = cli.invoke(app, args, env={"OTTO_LAB": ""}, prog_name="otto")
        assert real.exit_code == 0, real.output
        assert (out / "lab.schema.json").is_file()
        assert DRY_RUN_HEADLINE not in real.output


# ---------------------------------------------------------------------------
# The opt-in: a declared preview runs its own body
# ---------------------------------------------------------------------------


class TestOptInRunsTheBody:
    def test_group_level_flag_lets_the_body_run_under_n(self) -> None:
        calls: list[str] = []
        app = _recording_app(calls)
        with active_context(dry_run=True):
            opted = runner.invoke(app, ["go", "edge"], dry_run_preview=True)
        assert opted.exit_code == 0, opted.output
        assert calls == ["edge:0"], "dry_run_preview=True did not reach the body"

        # POSITIVE CONTROL for the flag itself, same app, same seam: without it
        # the identical invocation is stopped.
        calls.clear()
        with active_context(dry_run=True):
            default = runner.invoke(app, ["go", "edge"])
        assert default.exit_code == 0, default.output
        assert calls == [], "the default registration ran the body"

    def test_leaf_level_stamp_lets_the_body_run_under_n(self) -> None:
        calls: list[str] = []

        def _app(stamp: bool) -> typer.Typer:
            app = typer.Typer(name="seamdemo")

            async def go(target: str) -> None:
                calls.append(target)

            if stamp:
                go.__cli_dry_run_preview__ = True
            app.command("go")(go)
            app.command("idle")(lambda: None)
            return app

        with active_context(dry_run=True):
            opted = runner.invoke(_app(stamp=True), ["go", "edge"])
        assert opted.exit_code == 0, opted.output
        assert calls == ["edge"], "the leaf stamp did not reach the seam"

        # POSITIVE CONTROL: the identical leaf without the stamp is stopped.
        calls.clear()
        with active_context(dry_run=True):
            stopped = runner.invoke(_app(stamp=False), ["go", "edge"])
        assert stopped.exit_code == 0, stopped.output
        assert calls == []


# ---------------------------------------------------------------------------
# `otto host` — real validation, and the verb coroutine that is never awaited
# ---------------------------------------------------------------------------


class SpyHost:
    """A host class whose verbs record that they were actually awaited."""

    def __init__(self, host_id: str = "dut1") -> None:
        self.id = host_id
        self.awaited: list[str] = []
        self.closed = 0

    @cli_exposed
    async def probe(self, deep: bool = False) -> Result:
        self.awaited.append("probe")
        return Result(Status.Success, value="probed", msg="")

    @cli_exposed(name="preview-verb", dry_run_preview=True)
    async def preview_verb(self) -> Result:
        self.awaited.append("preview-verb")
        return NotRunResult(status=Status.NotRun, command="uptime", retcode=-1, host_name=self.id)

    async def close(self) -> None:
        self.closed += 1


def _host_app(monkeypatch: pytest.MonkeyPatch, host: SpyHost) -> typer.Typer:
    """An ``otto host``-shaped group over *host*, resolved the production way.

    Only the LAB is faked (``get_host``): the group class, verb synthesis and
    :func:`otto.cli.host.resolve_cli_host` are the shipped ones, so the seam's
    reference resolution is exercised rather than imitated.
    """
    import otto.host.os_profile as op

    known = {host.id: host}

    def fake_get_host(host_id: str, **_kw: Any) -> SpyHost:
        if host_id in known:
            return known[host_id]
        raise KeyError(f"No host {host_id!r} in lab 'test'. Available: {sorted(known)}")

    monkeypatch.setattr(op, "HOST_CLASSES", {"spy": SpyHost})
    monkeypatch.setattr("otto.cli.expose.host_class_for_id", lambda _hid: SpyHost)
    monkeypatch.setattr("otto.cli.host.get_host", fake_get_host)
    # `_resolve_host`'s "Available hosts" listing reads the active context's
    # lab mapping directly — explicit `otto host <id>` targeting is unscoped,
    # so it deliberately does NOT go through the fleet generator. Stubbed for
    # the same reason its `all_hosts` predecessor was: these tests install no
    # context, and the listing is not what they are about.
    empty_lab = SimpleNamespace(lab=SimpleNamespace(hosts={}))
    monkeypatch.setattr("otto.cli.host.get_context", lambda: empty_lab)

    app = typer.Typer(name="host", cls=HostGroup)

    @app.callback(invoke_without_command=True)
    def main(  # ty: ignore[unused-function]
        ctx: typer.Context,
        host_id: str = typer.Argument(""),
    ) -> None:
        if ctx.resilient_parsing:
            return
        ctx.meta["_otto_host_request"] = {
            "host_id": host_id,
            "hop": "",
            "term": None,
            "transfer": None,
        }

    return app


class TestHostVerbsAtTheSeam:
    def test_verb_coroutine_is_never_awaited_under_n_but_is_without_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        host = SpyHost()
        app = _host_app(monkeypatch, host)

        with active_context(dry_run=True):
            dry = runner.invoke(app, ["dut1", "probe"])
        assert dry.exit_code == 0, dry.output
        assert host.awaited == [], "the host verb ran under --dry-run"
        assert DRY_RUN_HEADLINE in dry.output

        # POSITIVE CONTROL, same verb, same seam: without -n it is awaited.
        real = runner.invoke(app, ["dut1", "probe"])
        assert real.exit_code == 0, real.output
        assert host.awaited == ["probe"], "the verb did not run even WITHOUT --dry-run"

    def test_a_default_host_verb_exits_0_and_does_not_traceback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The class Task 2's review flagged: a NotRunResult at the renderer.

        Before the seam, an ordinary ``otto host <id> <verb> -n`` reached
        ``render_leaf_value`` with a ``NotRunResult`` — whose ``.value`` raises
        and whose ``exit_code`` is 255 — so the whole default host surface
        either tracebacked or exited non-zero. The seam eliminates the class
        rather than hiding it: the body never runs, so nothing is produced to
        misrender.
        """
        host = SpyHost()
        app = _host_app(monkeypatch, host)
        with active_context(dry_run=True):
            dry = runner.invoke(app, ["dut1", "probe"])
        assert dry.exception is None, f"the dry run tracebacked: {dry.exception!r}"
        assert dry.exit_code == 0, dry.output
        assert "Traceback" not in dry.output

    def test_an_unknown_host_still_fails_under_n(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Validation is REAL, not print-and-exit."""
        host = SpyHost()
        app = _host_app(monkeypatch, host)

        with active_context(dry_run=True):
            bad = runner.invoke(app, ["nosuchbox", "probe"])
        assert bad.exit_code != 0, bad.output
        assert "nosuchbox" in bad.output
        assert DRY_RUN_HEADLINE not in bad.output, (
            "the seam claimed a command would run against a host that does not exist"
        )

        # POSITIVE CONTROL, same seam: the resolvable host DOES reach the block
        # and is named in it. Without this, a seam that refused everything
        # would satisfy the assertion above.
        with active_context(dry_run=True):
            good = runner.invoke(app, ["dut1", "probe"])
        assert good.exit_code == 0, good.output
        assert "references resolve: host 'dut1'" in flat(good.output)

    def test_the_resolved_reference_carries_the_host_ids_probe_will_dial(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--probe`` (spec §3) dials the reference set the seam resolved."""
        from otto.cli.expose import host_dry_run_references

        monkeypatch.setattr("otto.cli.host.get_host", lambda _hid, **_kw: SpyHost())

        class _Ctx:
            obj = None
            meta = {  # noqa: RUF012 — a throwaway ctx stand-in, not a model
                "_otto_host_request": {
                    "host_id": "dut1",
                    "hop": "",
                    "term": None,
                    "transfer": None,
                }
            }

        assert host_dry_run_references(_Ctx()) == [
            LabReference(kind="host", name="dut1", host_ids=["dut1"])
        ]

    def test_an_opted_in_verb_runs_and_its_decline_renders_at_exit_0(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        host = SpyHost()
        app = _host_app(monkeypatch, host)
        with active_context(dry_run=True):
            res = runner.invoke(app, ["dut1", "preview-verb"])
        assert res.exception is None, f"a rendered decline tracebacked: {res.exception!r}"
        assert res.exit_code == 0, res.output
        assert host.awaited == ["preview-verb"], "the opt-in did not reach the verb"


# ---------------------------------------------------------------------------
# The renderer: a decline is announced, never parsed, and never exit 255
# ---------------------------------------------------------------------------


class TestRendererHandlesADecline:
    def test_a_not_run_result_neither_raises_nor_exits_nonzero(self) -> None:
        declined = NotRunResult(status=Status.NotRun, command="uptime", retcode=-1, host_name="box")
        # The fact that makes this test necessary, pinned here rather than
        # assumed: the library-facing exit code of a decline is 255.
        assert declined.exit_code == 255

        render_leaf_value(declined)  # must not raise CommandNotRunError, must not Exit

        # POSITIVE CONTROL, same renderer: a real failure still exits with its
        # own code, so the branch above cannot have swallowed failures wholesale.
        with pytest.raises(typer.Exit) as exc:
            render_leaf_value(CommandResult(Status.Failed, value="", command="false", retcode=3))
        assert exc.value.exit_code == 3

    def test_a_declines_message_is_what_gets_printed(self, capsys: Any) -> None:
        render_leaf_value(Result(Status.NotRun, msg="[DRY RUN] WRITE: 38 bytes -> /tmp/x"))
        assert "[DRY RUN] WRITE: 38 bytes -> /tmp/x" in capsys.readouterr().out

    def test_a_msgless_decline_makes_no_claim_about_device_contact(self, capsys: Any) -> None:
        """A per-RESULT announcement may not speak for the whole invocation.

        This printer is reached on the PREVIEW path, which under
        ``--dry-run --probe`` runs after the probe may have dialed. Reusing the
        run-level headline here would print "no device was contacted" over a
        connection that was just opened — the unsafe direction of the one lie
        this contract exists to remove.
        """
        from otto.cli.invoke import DRY_RUN_DECLINE

        render_leaf_value(
            NotRunResult(status=Status.NotRun, command="uptime", retcode=-1, host_name="box")
        )
        out = flat(capsys.readouterr().out)
        assert DRY_RUN_HEADLINE not in out, (
            "a single declined result claimed no device was contacted"
        )
        assert DRY_RUN_DECLINE in out
        assert "uptime" in out, "the decline stopped naming the command it declined"


# ---------------------------------------------------------------------------
# `otto test` — the suite imports, the steps bind, no step body runs
# ---------------------------------------------------------------------------


class SeamPreviewSuite:
    """A suite-shaped class with two bound tests."""

    def test_alpha(self) -> None: ...
    def test_beta(self) -> None: ...
    def helper(self) -> None: ...


@pytest.fixture
def registered_suite() -> Any:
    from otto.suite.register import SUITES, register_suite_class

    register_suite_class(SeamPreviewSuite)
    yield SeamPreviewSuite
    SUITES.unregister(SeamPreviewSuite.__name__)


class TestSuitePreview:
    def test_dry_run_lists_the_bound_tests_and_never_reaches_run_suite(
        self, registered_suite: Any
    ) -> None:
        from otto.cli.test import suite_app

        ran: list[Any] = []

        def spy_run_suite(suite: Any, **kw: Any) -> Any:
            ran.append(suite)
            raise AssertionError("run_suite must not be reached under --dry-run")

        with (
            active_context(dry_run=True),
            patch("otto.suite.run.run_suite", spy_run_suite),
        ):
            dry = runner.invoke(suite_app, ["SeamPreviewSuite"], spec_name="test")
        assert dry.exit_code == 0, dry.output
        out = flat(dry.output)
        assert DRY_RUN_HEADLINE in out
        assert "2 test(s), no test body will run" in out
        assert "- test_alpha" in out
        assert "- test_beta" in out
        assert "helper" not in out
        assert ran == []

        # POSITIVE CONTROL, same command, same seam: without -n the suite runs.
        from pathlib import Path

        from otto.suite.run import SuiteRunResult

        def real_run_suite(suite: Any, **kw: Any) -> SuiteRunResult:
            ran.append(suite)
            return SuiteRunResult(
                exit_code=0,
                junit_paths=[],
                stability_report=None,
                stability_unstable=False,
                output_dir=Path(),
            )

        with active_context(), patch("otto.suite.run.run_suite", real_run_suite):
            real = runner.invoke(suite_app, ["SeamPreviewSuite"], spec_name="test")
        assert real.exit_code == 0, real.output
        assert ran == [SeamPreviewSuite], "the suite did not run even WITHOUT --dry-run"
        assert DRY_RUN_HEADLINE not in real.output

    def test_bound_test_names_reads_the_class_not_a_collection(self) -> None:
        from otto.suite.register import bound_test_names

        assert bound_test_names(SeamPreviewSuite) == ["test_alpha", "test_beta"]


class TestTheSuitelessSelectionPathKeepsTheSafeDefault:
    """`otto test --tests foo -n` stops at the seam and runs no pytest.

    WHY THIS IS A SEPARATE PATH, and why the guard has to exist. `otto test`'s
    opt-in is stamped on the SUITE LEAF (`otto/suite/register.py`), not on the
    `test` `CommandSpec`. That placement is load-bearing, not incidental: the
    `--tests` / `-m` selection has NO leaf at all — the group callback
    (`otto/cli/test.py`) handles it inline, stamps the `test` spec on
    `ctx.meta` itself, calls `command_preamble`, and then runs the selection
    for real. Move the opt-in up to the spec (the obvious simplification: "one
    flag for the whole command") and the seam waves this path through, so
    `otto test --tests foo -n` invokes pytest against real hardware.

    Nothing else covers it: `TestSuitePreview` above drives the suite leaf,
    which is the arm that must NOT stop.
    """

    def _preamble_without_a_lab(self) -> "list[Any]":
        """Neutralise the lab/gate halves of the preamble; the SEAM stays real.

        `command_preamble` is `lab load → gate → seam`, and the selection path
        reaches it under the real `test` spec (`lab_free=False`). A sub-app
        unit test has no lab and no reservation backend, so those two are
        stubbed — the line under test, `stop_at_dry_run_seam`, is untouched,
        and both controls below run through the same stubs.
        """
        return [
            patch("otto.cli.invoke.ensure_lab_session", lambda *_a, **_k: None),
            patch("otto.cli.invoke.present_reservation_gate", lambda *_a, **_k: None),
        ]

    def test_a_tests_selection_stops_at_the_seam_while_a_suite_still_previews(
        self, registered_suite: Any
    ) -> None:
        from otto.cli.test import suite_app

        selected: list[Any] = []
        lab, gate = self._preamble_without_a_lab()

        with (
            active_context(dry_run=True),
            lab,
            gate,
            patch("otto.cli.test.run_selection", selected.append),
        ):
            dry = runner.invoke(suite_app, ["--tests", "test_alpha"], spec_name="test")

        assert dry.exception is None, f"the selection path tracebacked: {dry.exception!r}"
        assert dry.exit_code == 0, dry.output
        assert selected == [], (
            "`otto test --tests ... -n` ran the suite-less selection for real; "
            "the opt-in has leaked from the suite leaf onto the `test` CommandSpec"
        )
        out = flat(dry.output)
        assert DRY_RUN_HEADLINE in out, "the stop produced no announcement at all"
        # `would run: test`, not `otto test --tests test_alpha`: `would_run_line`
        # omits the ROOT context's own options, and under this harness the
        # group IS the root (the real root is `otto`, which makes `test` a
        # child and echoes its flags). The claim under test is that the stop
        # announced itself — a dry run whose output is empty is a bug.
        assert "would run: test" in out
        assert dry.output.strip()

        # POSITIVE CONTROL 1, same seam, same command group: the SUITE path
        # still runs its body and prints its own deeper preview. Without this
        # the assertion above is satisfied by a seam that stops everything —
        # which is precisely the regression the leaf stamp exists to prevent.
        lab, gate = self._preamble_without_a_lab()
        with active_context(dry_run=True), lab, gate:
            suite = runner.invoke(suite_app, ["SeamPreviewSuite"], spec_name="test")
        assert suite.exit_code == 0, suite.output
        assert "2 test(s), no test body will run" in flat(suite.output), (
            "the suite leaf's own preview is gone, so `--tests` stopping proves nothing"
        )

        # POSITIVE CONTROL 2, same selection, same stubs, without -n: the
        # selection IS reached. Without this, a `--tests` path that was simply
        # broken would satisfy `selected == []` above.
        lab, gate = self._preamble_without_a_lab()
        with (
            active_context(),
            lab,
            gate,
            patch("otto.cli.test.run_selection", selected.append),
        ):
            real = runner.invoke(suite_app, ["--tests", "test_alpha"], spec_name="test")
        assert real.exit_code == 0, real.output
        assert len(selected) == 1, "the selection did not run even WITHOUT --dry-run"
        assert DRY_RUN_HEADLINE not in real.output

    def test_the_opt_in_is_on_the_leaf_and_not_on_the_test_command_spec(
        self, registered_suite: Any
    ) -> None:
        """The structural half, pinned so the drift is named and not inferred."""
        from otto.suite.register import SUITES

        assert _shipped("test").dry_run_preview is False, (
            "moving `otto test`'s opt-in onto the CommandSpec opts the suite-less "
            "`--tests` selection in too, and that path runs pytest for real"
        )
        # POSITIVE CONTROL: the opt-in DOES exist — one level down, on the leaf
        # the suite registration generates. Without it, "the spec says False"
        # would be satisfied by an `otto test` with no preview at all.
        leaf = SUITES.get(registered_suite.__name__).sub_app.registered_commands[0].callback
        assert leaf.__cli_dry_run_preview__ is True


# ---------------------------------------------------------------------------
# `--probe`: THE FLAG PERMITS A CONNECTION, NEVER A COMMAND (spec §3)
# ---------------------------------------------------------------------------


def _root_options(*, dry_run: bool, probe: bool) -> Any:
    """The real ``RootOptions`` the root callback stashes, not a stand-in.

    The production type in the production ``ctx.meta`` slot: a rename of
    either field turns these tests red instead of silently answering "no
    probe" through ``getattr``'s default.
    """
    from pathlib import Path

    from otto.cli.invoke import RootOptions

    return RootOptions(
        labs=None,
        xdir=Path(),
        log_days=30,
        log_level="INFO",
        rich_log_file=False,
        show_time=False,
        dry_run=dry_run,
        as_user=None,
        skip_reservation_check=False,
        probe=probe,
    )


def _dialable_host(element: str = "dut1", ip: str = "198.51.100.1") -> Any:
    """A REAL ``UnixHost`` — the family whose ``is_reachable`` is under test.

    Belt and braces on the address, because this file's whole subject is
    dialing: every transport primitive is spied out so no socket is ever
    created, AND the ip is TEST-NET-2 (198.51.100.0/24, RFC 5737 — reserved for
    documentation, guaranteed unroutable) rather than anything that could be a
    real machine. Nothing here may touch the 10.10.200.0/24 lab.
    """
    from otto.host.login_proxy import Cred
    from otto.host.unix_host import UnixHost

    return UnixHost(ip=ip, creds=[Cred(login="root", password="x")], element=element)


def _lab_with(*hosts: Any) -> Any:
    from otto.config.lab import Lab

    lab = Lab(name="probelab")
    for host in hosts:
        lab.add_host(host)
    return lab


def _probe_app(host_ids: "list[str]", *, probe: bool, calls: "list[str]") -> typer.Typer:
    """A leaf that lends the seam a reference set, driven under root options.

    The refs hook is stamped exactly the way ``otto host`` stamps it
    (``__otto_dry_run_refs__`` on the callback), so the seam derives its host
    set the production way rather than through a test-only door.
    """
    from otto.cli.invoke import DRY_RUN_REFS_ATTR

    app = typer.Typer(name="probedemo")

    @app.callback()
    def _root(ctx: typer.Context) -> None:  # ty: ignore[unused-function]
        if ctx.resilient_parsing:
            return
        ctx.meta["_otto_root_options"] = _root_options(dry_run=True, probe=probe)

    async def go() -> None:
        calls.append("go")

    setattr(
        go,
        DRY_RUN_REFS_ATTR,
        lambda _ctx: [LabReference(kind="host", name=host_ids[0], host_ids=list(host_ids))],
    )
    app.command("go")(go)
    app.command("idle")(lambda: None)
    return app


class _TransportSpy:
    """Counts every attempt to OPEN a transport, at the real ``ssh_connect`` seam."""

    def __init__(self, *, fail: bool = False) -> None:
        self.opens: list[Any] = []
        self.fail = fail

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        from unittest.mock import MagicMock

        from asyncssh import SSHClientConnection

        self.opens.append(args)
        if self.fail:
            raise OSError("[Errno 111] Connection refused")
        return MagicMock(spec=SSHClientConnection)


def _spy_transport_and_commands(
    monkeypatch: pytest.MonkeyPatch, *, fail: bool = False
) -> "tuple[_TransportSpy, list[str]]":
    """Spy BOTH seams: the transport open, and every command entrypoint.

    ``exec``/``run`` are REPLACED (not wrapped) so a test can fire them
    directly as its own positive control — proving the recorder is wired to
    the attributes the probe would have had to use, rather than asserting an
    empty list against a spy that could never have filled.

    ``functools.wraps`` is load-bearing, not tidiness: ``run`` is a
    ``@cli_exposed`` verb, and a bare replacement drops the marker so
    ``HostGroup`` stops offering ``otto host <id> run`` at all — the spy would
    silently change the surface it is supposed to be watching.
    """
    import functools

    from otto.host.unix_host import UnixHost

    opened = _TransportSpy(fail=fail)
    ran: list[str] = []
    monkeypatch.setattr("otto.host.connections.ssh_connect", opened)

    @functools.wraps(UnixHost.exec)
    async def _rec_exec(self: Any, cmd: Any = "", *a: Any, **kw: Any) -> Any:
        ran.append(f"exec:{cmd}")
        return CommandResult(status=Status.Success, value="", command=str(cmd), retcode=0)

    @functools.wraps(UnixHost.run)
    async def _rec_run(self: Any, cmd: Any = "", *a: Any, **kw: Any) -> Any:
        ran.append(f"run:{cmd}")
        return CommandResult(status=Status.Success, value="", command=str(cmd), retcode=0)

    monkeypatch.setattr(UnixHost, "exec", _rec_exec, raising=True)
    monkeypatch.setattr(UnixHost, "run", _rec_run, raising=True)
    return opened, ran


class TestProbeDialsAndNeverCommands:
    def test_probe_opens_one_transport_and_issues_no_command(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The flag's whole promise, both halves, at the real seams.

        ``-n --probe`` on a command naming one host: exactly one transport
        open, and ZERO commands. Both spies carry their positive control in
        this test — the transport spy's is the ``--probe``-off invocation
        below (which must open nothing), and the command spy's is the direct
        call at the end (which must record).
        """
        import asyncio

        host = _dialable_host()
        lab = _lab_with(host)
        opened, ran = _spy_transport_and_commands(monkeypatch)
        calls: list[str] = []

        with active_context(lab=lab, dry_run=True):
            probed = runner.invoke(_probe_app(["dut1"], probe=True, calls=calls), ["go"])

        assert probed.exit_code == 0, probed.output
        assert probed.exception is None, f"the probe tracebacked: {probed.exception!r}"
        assert len(opened.opens) == 1, (
            f"--probe opened {len(opened.opens)} transports, expected exactly one"
        )
        assert ran == [], f"--probe RAN A COMMAND: {ran} — the flag permits a connection only"
        assert calls == [], "the seam let the body run under --dry-run --probe"
        assert "dut1: reachable" in flat(probed.output)

        # POSITIVE CONTROL for the transport spy, same command, same seam:
        # without --probe the identical invocation dials nothing at all. This
        # is the half that keeps every "a dry run contacts no device" guard
        # Tasks 5-5c added true by default.
        opened.opens.clear()
        with active_context(lab=lab, dry_run=True):
            unprobed = runner.invoke(_probe_app(["dut1"], probe=False, calls=calls), ["go"])
        assert unprobed.exit_code == 0, unprobed.output
        assert opened.opens == [], (
            f"a dry run WITHOUT --probe opened {len(opened.opens)} transports"
        )
        assert "probe:" not in flat(unprobed.output)

        # POSITIVE CONTROL for the command spy, on the same attributes the
        # probe would have had to go through: they DO record when a command is
        # issued. Without this, `ran == []` above would pass against a spy
        # wired to nothing.
        asyncio.run(host.exec("uptime"))
        asyncio.run(host.run("uname -a"))
        assert ran == ["exec:uptime", "run:uname -a"], (
            "the command spy cannot observe a command, so proving none ran proves nothing"
        )

    def test_an_unreachable_host_is_reported_and_the_dry_run_still_exits_0(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reachability is information, not a gate."""
        host = _dialable_host()
        lab = _lab_with(host)
        opened, ran = _spy_transport_and_commands(monkeypatch, fail=True)
        calls: list[str] = []

        with active_context(lab=lab, dry_run=True):
            down = runner.invoke(_probe_app(["dut1"], probe=True, calls=calls), ["go"])

        assert down.exit_code == 0, f"an unreachable host failed the dry run: {down.output}"
        assert "dut1: unreachable" in flat(down.output)
        assert len(opened.opens) == 1, "the unreachable host was never dialed"
        assert ran == [], f"a failed probe fell back to a command: {ran}"

        # POSITIVE CONTROL, same host, same seam: when the dial succeeds the
        # same table says `reachable`. Without it, a probe hard-coded to report
        # every host down would satisfy the assertion above.
        opened.fail = False
        opened.opens.clear()
        with active_context(lab=lab, dry_run=True):
            up = runner.invoke(_probe_app(["dut1"], probe=True, calls=calls), ["go"])
        assert up.exit_code == 0, up.output
        assert "dut1: reachable" in flat(up.output)

    def test_a_family_with_no_connection_probe_is_not_called_unreachable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ "We could not ask" and "we asked and it said no" are different facts.

        ``DockerContainerHost`` reaches its shell through its parent and has no
        transport of its own, so ``BaseHost.is_reachable`` raises. Reporting
        that as ``unreachable`` would be a measurement nobody took.
        """
        from otto.host.unix_host import UnixHost

        host = _dialable_host()
        lab = _lab_with(host)
        _spy_transport_and_commands(monkeypatch)

        async def _no_probe(self: Any, timeout: float = 10.0) -> bool:
            raise NotImplementedError("is_reachable is not supported on 'DockerContainerHost'")

        monkeypatch.setattr(UnixHost, "is_reachable", _no_probe, raising=True)
        calls: list[str] = []
        with active_context(lab=lab, dry_run=True):
            res = runner.invoke(_probe_app(["dut1"], probe=True, calls=calls), ["go"])

        assert res.exit_code == 0, res.output
        out = flat(res.output)
        assert "dut1: not probed" in out
        assert "dut1: unreachable" not in out, "a family with no probe was reported down"
        assert "DockerContainerHost" in out, "the table did not say WHY it could not ask"

    def test_the_block_stops_claiming_no_contact_once_probe_dialed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The headline is a claim about device contact, so --probe must change it."""
        from otto.utils import DRY_RUN_HEADLINE_PROBED

        host = _dialable_host()
        lab = _lab_with(host)
        _spy_transport_and_commands(monkeypatch)
        calls: list[str] = []

        with active_context(lab=lab, dry_run=True):
            probed = runner.invoke(_probe_app(["dut1"], probe=True, calls=calls), ["go"])
        assert DRY_RUN_HEADLINE not in flat(probed.output), (
            "the block said no device was contacted immediately after dialing one"
        )
        assert DRY_RUN_HEADLINE_PROBED in flat(probed.output)

        # POSITIVE CONTROL, same app, same seam: without --probe the default
        # headline — the one that DOES claim no contact — is what prints.
        with active_context(lab=lab, dry_run=True):
            plain = runner.invoke(_probe_app(["dut1"], probe=False, calls=calls), ["go"])
        assert DRY_RUN_HEADLINE in flat(plain.output)
        assert DRY_RUN_HEADLINE_PROBED not in flat(plain.output)

    def test_a_previewing_command_gets_the_table_and_still_previews(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The table prints ahead of the seam block AND ahead of a preview.

        ``link``/``tunnel`` opt out of the seam stop and run their own body, so
        the probe has to sit above that fork or the whole previewing half of
        the CLI would silently ignore ``--probe``.
        """
        host = _dialable_host()
        lab = _lab_with(host)
        opened, ran = _spy_transport_and_commands(monkeypatch)
        calls: list[str] = []

        with active_context(lab=lab, dry_run=True):
            res = runner.invoke(
                _probe_app(["dut1"], probe=True, calls=calls), ["go"], dry_run_preview=True
            )
        assert res.exit_code == 0, res.output
        assert "dut1: reachable" in flat(res.output), "a previewing command ignored --probe"
        assert calls == ["go"], "the probe swallowed the preview body"
        assert len(opened.opens) == 1
        assert ran == [], f"the previewing path issued a command under --probe: {ran}"

        # POSITIVE CONTROL, same previewing app, same seam: without --probe the
        # body still runs and nothing is dialed — so the assertions above are
        # about the flag, not about the opt-in.
        calls.clear()
        opened.opens.clear()
        with active_context(lab=lab, dry_run=True):
            plain = runner.invoke(
                _probe_app(["dut1"], probe=False, calls=calls), ["go"], dry_run_preview=True
            )
        assert plain.exit_code == 0, plain.output
        assert calls == ["go"]
        assert opened.opens == []

    def test_a_host_answered_without_a_socket_does_not_license_a_contact_claim(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``local`` is reachable, and reaching it contacted no device.

        ``LocalHost.is_reachable`` returns ``True`` unconditionally with no
        transport. Deriving the headline from "there are results" would print
        "--probe opened a connection" for a run that opened no socket at all.
        """
        from otto.host.local_host import LocalHost
        from otto.utils import DRY_RUN_HEADLINE_PROBED

        local = LocalHost()
        lab = _lab_with(local)
        opened, _ran = _spy_transport_and_commands(monkeypatch)
        calls: list[str] = []

        with active_context(lab=lab, dry_run=True):
            res = runner.invoke(_probe_app([local.id], probe=True, calls=calls), ["go"])

        assert res.exit_code == 0, res.output
        out = flat(res.output)
        assert f"{local.id}: reachable -- no transport to open" in out, (
            "the row hid that this answer cost no connection"
        )
        assert opened.opens == [], "probing the local host opened a transport"
        assert DRY_RUN_HEADLINE_PROBED not in out, (
            "the block claimed --probe opened a connection when no socket was opened"
        )
        assert DRY_RUN_HEADLINE in out

        # POSITIVE CONTROL, same seam, same block: add a host that IS dialed and
        # the headline does flip — so the assertion above is about the socket,
        # not about --probe being inert.
        remote = _dialable_host()
        both = _lab_with(LocalHost(), remote)
        with active_context(lab=both, dry_run=True):
            mixed = runner.invoke(_probe_app([local.id, "dut1"], probe=True, calls=calls), ["go"])
        assert mixed.exit_code == 0, mixed.output
        assert DRY_RUN_HEADLINE_PROBED in flat(mixed.output)
        assert len(opened.opens) == 1

    def test_a_family_with_no_probe_alone_does_not_license_a_contact_claim(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An all-``not probed`` set attempted nothing, so nothing was contacted."""
        from otto.host.unix_host import UnixHost
        from otto.utils import DRY_RUN_HEADLINE_PROBED

        lab = _lab_with(_dialable_host())
        _spy_transport_and_commands(monkeypatch)

        async def _no_probe(self: Any, timeout: float = 10.0) -> bool:
            raise NotImplementedError("is_reachable is not supported on 'DockerContainerHost'")

        monkeypatch.setattr(UnixHost, "is_reachable", _no_probe, raising=True)
        calls: list[str] = []
        with active_context(lab=lab, dry_run=True):
            res = runner.invoke(_probe_app(["dut1"], probe=True, calls=calls), ["go"])

        assert res.exit_code == 0, res.output
        assert "dut1: not probed" in flat(res.output)
        assert DRY_RUN_HEADLINE_PROBED not in flat(res.output), (
            "an all-'not probed' set claimed a connection was opened"
        )

        # POSITIVE CONTROL, same app, same seam: with the family's probe
        # restored the identical run DOES flip the headline.
        monkeypatch.undo()
        opened, _ran = _spy_transport_and_commands(monkeypatch)
        with active_context(lab=_lab_with(_dialable_host()), dry_run=True):
            ok = runner.invoke(_probe_app(["dut1"], probe=True, calls=calls), ["go"])
        assert DRY_RUN_HEADLINE_PROBED in flat(ok.output)
        assert len(opened.opens) == 1

    def test_the_probe_dials_the_transport_the_invocation_chose(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--term telnet`` must probe TELNET, not the host's configured ssh.

        The override is applied by ``resolve_cli_host`` as a COPY on ``ctx.obj``
        and never reaches the lab, so a probe that re-fetched the bare id would
        dial ssh and report a reachability this invocation would never get.
        """
        import otto.host.os_profile as op
        from otto.host.unix_host import UnixHost

        host = _dialable_host()
        lab = _lab_with(host)
        opened, ran = _spy_transport_and_commands(monkeypatch)
        telnet_opens: list[str] = []

        class _FakeTelnet:
            alive = True

            def __init__(self, hostname: str, **_kw: Any) -> None:
                self.hostname = hostname

            async def connect(self, interactive: bool = False) -> None:
                telnet_opens.append(self.hostname)

            async def close(self) -> None:
                return None

        monkeypatch.setattr("otto.host.connections.TelnetClient", _FakeTelnet)
        monkeypatch.setattr(op, "HOST_CLASSES", {"unix": UnixHost})
        monkeypatch.setattr("otto.cli.expose.host_class_for_id", lambda _hid: UnixHost)
        monkeypatch.setattr("otto.cli.host.get_host", lambda hid, **_kw: lab.hosts[hid])
        # `_resolve_host`'s "Available hosts" listing now reads the active
        # context's lab directly (explicit targeting is unscoped); stubbed
        # for the same reason its `all_hosts` predecessor was.
        monkeypatch.setattr(
            "otto.cli.host.get_context", lambda: SimpleNamespace(lab=SimpleNamespace(hosts={}))
        )

        def _app(term: "str | None") -> typer.Typer:
            app = typer.Typer(name="host", cls=HostGroup)

            @app.callback(invoke_without_command=True)
            def main(ctx: typer.Context, host_id: str = typer.Argument("")) -> None:  # ty: ignore[unused-function]
                if ctx.resilient_parsing:
                    return
                ctx.meta["_otto_root_options"] = _root_options(dry_run=True, probe=True)
                ctx.meta["_otto_host_request"] = {
                    "host_id": host_id,
                    "hop": "",
                    "term": term,
                    "transfer": None,
                }

            return app

        with active_context(lab=lab, dry_run=True):
            overridden = runner.invoke(_app("telnet"), ["dut1", "run", "uptime"])
        assert overridden.exit_code == 0, overridden.output
        assert telnet_opens == [host.ip], f"--term telnet did not dial telnet: {telnet_opens}"
        assert opened.opens == [], "--term telnet dialed SSH — the override was dropped"
        assert "dut1: reachable" in flat(overridden.output)
        assert ran == []

        # POSITIVE CONTROL, same host, same seam: with no override the SAME
        # invocation dials the host's configured ssh. Without this, an
        # implementation that always dialed telnet would pass the assertions
        # above.
        telnet_opens.clear()
        with active_context(lab=lab, dry_run=True):
            plain = runner.invoke(_app(None), ["dut1", "run", "uptime"])
        assert plain.exit_code == 0, plain.output
        assert len(opened.opens) == 1, "the un-overridden probe did not dial ssh"
        assert telnet_opens == [], "the un-overridden probe dialed telnet"

    def test_otto_host_derives_the_dialed_set_from_its_own_shipped_resolver(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End to end through ``otto host``: one resolver, not a parallel one.

        The other tests in this class lend the seam a reference from a
        synthetic leaf. This one drives the SHIPPED group — ``HostGroup``, verb
        synthesis, ``host_dry_run_references`` → ``resolve_cli_host`` — so the
        host set ``--probe`` dials is the one the verb body would have used,
        rather than a lookalike derived a second way.
        """
        import otto.host.os_profile as op
        from otto.host.unix_host import UnixHost

        host = _dialable_host()
        lab = _lab_with(host)
        opened, ran = _spy_transport_and_commands(monkeypatch)

        monkeypatch.setattr(op, "HOST_CLASSES", {"unix": UnixHost})
        monkeypatch.setattr("otto.cli.expose.host_class_for_id", lambda _hid: UnixHost)
        monkeypatch.setattr("otto.cli.host.get_host", lambda hid, **_kw: lab.hosts[hid])
        # `_resolve_host`'s "Available hosts" listing now reads the active
        # context's lab directly (explicit targeting is unscoped); stubbed
        # for the same reason its `all_hosts` predecessor was.
        monkeypatch.setattr(
            "otto.cli.host.get_context", lambda: SimpleNamespace(lab=SimpleNamespace(hosts={}))
        )

        def _app(probe: bool) -> typer.Typer:
            app = typer.Typer(name="host", cls=HostGroup)

            @app.callback(invoke_without_command=True)
            def main(ctx: typer.Context, host_id: str = typer.Argument("")) -> None:  # ty: ignore[unused-function]
                if ctx.resilient_parsing:
                    return
                ctx.meta["_otto_root_options"] = _root_options(dry_run=True, probe=probe)
                ctx.meta["_otto_host_request"] = {
                    "host_id": host_id,
                    "hop": "",
                    "term": None,
                    "transfer": None,
                }

            return app

        with active_context(lab=lab, dry_run=True):
            probed = runner.invoke(_app(probe=True), ["dut1", "run", "uptime"])
        assert probed.exit_code == 0, probed.output
        assert "dut1: reachable" in flat(probed.output), (
            "otto host's own reference did not reach the probe"
        )
        assert len(opened.opens) == 1
        assert ran == [], f"the shipped host path issued a command under --probe: {ran}"

        # POSITIVE CONTROL, same shipped group, same verb: without --probe the
        # identical invocation dials nothing.
        opened.opens.clear()
        with active_context(lab=lab, dry_run=True):
            plain = runner.invoke(_app(probe=False), ["dut1", "run", "uptime"])
        assert plain.exit_code == 0, plain.output
        assert opened.opens == []


class TestProbeRequiresDryRun:
    def test_bare_probe_is_a_usage_error_naming_the_dependency(self, tmp_path: Any) -> None:
        """Driven through the shipped root app: this is a ROOT-option contract."""
        from typer.testing import CliRunner

        from otto.cli.main import app

        cli = CliRunner()
        out = tmp_path / "schemas"
        args = ["schema", "export", "--out", str(out), "--builtins-only"]

        bare = cli.invoke(app, ["--probe", *args], env={"OTTO_LAB": ""}, prog_name="otto")
        assert bare.exit_code == 2, f"bare --probe was not a usage error: {bare.output}"
        assert "--dry-run" in flat(bare.output), (
            "the usage error did not name the option --probe depends on"
        )
        assert not out.exists(), "the refused invocation ran the command anyway"

        # POSITIVE CONTROL, same flag, same root app: WITH -n it is accepted,
        # exits 0, and announces the probe. Without this the assertion above
        # would pass against a --probe that is simply always rejected.
        paired = cli.invoke(
            app, ["--dry-run", "--probe", *args], env={"OTTO_LAB": ""}, prog_name="otto"
        )
        assert paired.exit_code == 0, paired.output
        assert "probe:" in flat(paired.output), "the accepted --probe announced nothing"
        assert not out.exists(), "the dry run wrote the schema files anyway"


class TestProbeHostSetAndTable:
    def test_the_dial_list_is_every_reference_kind_deduplicated_in_order(self) -> None:
        """Host args directly, a link's endpoints, a tunnel's chain — one rule."""
        from otto.cli.probe import ProbeTarget, probe_targets

        assert probe_targets(
            [
                LabReference(kind="host", name="dut1", host_ids=["dut1"]),
                LabReference(kind="link", name="lan", host_ids=["dut1", "dut2"]),
                LabReference(kind="tunnel", name="t1", host_ids=["dut2", "hop1"]),
                LabReference(kind="host", name="nohosts"),
            ]
        ) == [ProbeTarget("dut1"), ProbeTarget("dut2"), ProbeTarget("hop1")]

    def test_the_dial_list_carries_the_invocations_protocol_overrides(self) -> None:
        """``--term``/``--transfer`` ride the reference through to the dial."""
        from otto.cli.probe import ProbeTarget, probe_targets

        assert probe_targets(
            [
                LabReference(
                    kind="host", name="dut1", host_ids=["dut1"], term="telnet", transfer="ftp"
                )
            ]
        ) == [ProbeTarget("dut1", term="telnet", transfer="ftp")]

    def test_the_table_reports_each_state_without_consulting_a_clock(self) -> None:
        """The renderer is pure, so no assertion here is wall-clock bound."""
        from otto.cli.probe import (
            NO_TRANSPORT,
            NOT_PROBED,
            REACHABLE,
            UNREACHABLE,
            ProbeResult,
            probe_report_lines,
        )

        assert probe_report_lines(
            [
                ProbeResult("dut1", REACHABLE, connect_ms=12.4, dialed=True),
                ProbeResult("dut2", UNREACHABLE, dialed=True),
                ProbeResult("cont1", NOT_PROBED, detail="no probe for 'DockerContainerHost'"),
                ProbeResult("local", REACHABLE, detail=NO_TRANSPORT),
            ]
        ) == [
            "dut1: reachable (connect 12 ms)",
            "dut2: unreachable",
            "cont1: not probed -- no probe for 'DockerContainerHost'",
            "local: reachable -- no transport to open",
        ]

    def test_only_a_real_dial_counts_as_device_contact(self) -> None:
        """The headline predicate counts SOCKETS, not rows.

        The two ways a row can exist without a socket — a family with no probe,
        and a host answered without one — must not license the block to claim a
        connection was opened.
        """
        from otto.cli.probe import (
            NO_TRANSPORT,
            NOT_PROBED,
            REACHABLE,
            UNREACHABLE,
            ProbeResult,
            probe_contacted,
        )

        assert probe_contacted([]) is False
        assert probe_contacted([ProbeResult("cont1", NOT_PROBED, detail="no probe")]) is False
        assert probe_contacted([ProbeResult("local", REACHABLE, detail=NO_TRANSPORT)]) is False

        # POSITIVE CONTROL, same predicate: a host that WAS dialed flips it —
        # reachable or not, because a refused connection is still contact.
        assert probe_contacted([ProbeResult("dut1", REACHABLE, dialed=True)]) is True
        assert probe_contacted([ProbeResult("dut1", UNREACHABLE, dialed=True)]) is True
        assert (
            probe_contacted(
                [
                    ProbeResult("local", REACHABLE, detail=NO_TRANSPORT),
                    ProbeResult("dut1", REACHABLE, dialed=True),
                ]
            )
            is True
        )

    def test_a_command_naming_no_host_says_so_rather_than_printing_nothing(self) -> None:
        """SUPPRESS THE PAYLOAD, NEVER THE ANNOUNCEMENT."""
        from otto.cli.probe import PROBE_NO_HOSTS, print_probe_report

        with patch("rich.print") as rprint:
            print_probe_report([])
        assert rprint.call_count == 1
        assert PROBE_NO_HOSTS in rprint.call_args[0][0]
