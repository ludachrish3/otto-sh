"""The typed ``ctx.meta`` accessors in :mod:`otto.cli.invoke`.

``ctx.meta`` is click's untyped per-invocation stash (``dict[str, Any]``); the
accessors are the single seam that narrows what the root callback stored back
to real types. The wrong-type cases matter as much as the happy paths: the
isinstance check is what stands between a drifted stash (or a drifted test
double) and an ``AttributeError`` three frames later.
"""

from types import SimpleNamespace

import pytest

from otto.cli.invoke import (
    command_spec,
    maybe_root_options,
    root_options,
)
from otto.cli.registry import CommandSpec
from tests._fixtures.fake_project import fake_project
from tests._fixtures.rootoptions import make_root_options


def _ctx(meta):
    """The one slice of a click ctx the accessors read."""
    return SimpleNamespace(meta=meta)


class TestRootOptions:
    def test_returns_the_stashed_instance(self):
        opts = make_root_options()
        assert root_options(_ctx({"_otto_root_options": opts})) is opts

    def test_foreign_object_in_stash_is_refused(self):
        with pytest.raises(AssertionError, match="RootOptions"):
            root_options(_ctx({"_otto_root_options": SimpleNamespace()}))

    def test_missing_key_raises_key_error(self):
        # Strict variant: absence is a caller bug, exactly as it was when the
        # call sites indexed ``meta["_otto_root_options"]`` themselves.
        with pytest.raises(KeyError):
            root_options(_ctx({}))


class TestMaybeRootOptions:
    def test_none_ctx_is_none(self):
        assert maybe_root_options(None) is None

    def test_missing_key_is_none(self):
        assert maybe_root_options(_ctx({})) is None

    def test_returns_the_stashed_instance(self):
        opts = make_root_options()
        assert maybe_root_options(_ctx({"_otto_root_options": opts})) is opts

    def test_foreign_object_in_stash_is_refused(self):
        with pytest.raises(AssertionError, match="RootOptions"):
            maybe_root_options(_ctx({"_otto_root_options": object()}))


class TestCommandSpec:
    def test_returns_the_stashed_instance(self):
        spec = CommandSpec(name="probe", loader=lambda: None)
        assert command_spec(_ctx({"_otto_command_spec": spec})) is spec

    def test_foreign_object_in_stash_is_refused(self):
        with pytest.raises(AssertionError, match="CommandSpec"):
            command_spec(_ctx({"_otto_command_spec": object()}))


class TestScopeSelection:
    def test_reads_root_options_without_splitting(self):
        from otto.cli.invoke import scope_selection

        opts = make_root_options(
            labs=["a", "b+c"], include_projects=("x",), exclude_projects=("y", "z")
        )
        sel = scope_selection(_ctx({"_otto_root_options": opts}))
        assert (sel.labs, sel.include, sel.exclude) == (("a", "b+c"), ("x",), ("y", "z"))

    def test_no_lab_is_empty(self):
        from otto.cli.invoke import scope_selection

        sel = scope_selection(_ctx({"_otto_root_options": make_root_options()}))
        assert sel.labs == ()

    def test_otto_lab_env_flows_through_the_root_callback(self, monkeypatch, real_main_mocks):
        """``OTTO_LAB=a+b -I x -E y`` reaches ``scope_selection`` through the real root callback."""
        from typer.testing import CliRunner

        from otto.cli import listing
        from otto.cli.invoke import scope_selection
        from otto.cli.main import app

        seen = []
        monkeypatch.setattr(
            listing, "show_listing", lambda ctx, seam: seen.append(scope_selection(ctx))
        )
        monkeypatch.setenv("OTTO_LAB", "a+b")
        result = CliRunner().invoke(app, ["-I", "x", "-E", "y", "--list-products"])
        assert result.exit_code == 0, result.output
        (sel,) = seen
        assert (sel.labs, sel.include, sel.exclude) == (("a", "b"), ("x",), ("y",))


class TestListingParticipation:
    """The one listing preamble: switches, bootstrap gate, then lab tier or pre-lab tier."""

    def _wire(self, monkeypatch, *, calls):
        from otto.cli import invoke

        monkeypatch.setattr(
            invoke, "validate_project_switches", lambda ctx: calls.append("validate")
        )
        monkeypatch.setattr(
            invoke, "fail_loud_on_bootstrap_errors", lambda ctx=None: calls.append("gate")
        )
        monkeypatch.setattr(invoke, "ensure_inline_lab", lambda ctx: calls.append("lab"))

    def test_validates_switches_first_and_a_typo_exits(self, monkeypatch):
        import typer

        from otto.cli import invoke

        def refuse(ctx):
            raise typer.Exit(2)

        monkeypatch.setattr(invoke, "validate_project_switches", refuse)
        ctx = _ctx({"_otto_root_options": make_root_options(exclude_projects=("typo",))})
        with pytest.raises(typer.Exit) as excinfo:
            invoke.listing_participation(ctx, [])
        assert excinfo.value.exit_code == 2

    def test_with_lab_is_lab_tier(self, monkeypatch):
        from otto.cli import invoke
        from otto.cli import listing as listing_mod

        calls = []
        self._wire(monkeypatch, calls=calls)
        monkeypatch.setattr(listing_mod, "print_no_lab_cue", lambda: calls.append("cue"))
        sentinel = object()
        monkeypatch.setattr("otto.run_context.get_context", lambda: sentinel)
        monkeypatch.setattr(
            "otto.config.scope.participating",
            lambda projects, **kw: calls.append(("participating", kw)) or "out",
        )
        ctx = _ctx({"_otto_root_options": make_root_options(labs=["bench"])})
        assert invoke.listing_participation(ctx, []) == "out"
        # The gates are the inline lab load's own (stubbed here): not run twice.
        assert calls == ["lab", ("participating", {"ctx": sentinel})]

    def test_with_lab_gates_once(self, monkeypatch, capsys):
        """A demoted project's warning prints once: the inline lab load runs both gates."""
        from contextlib import nullcontext

        import typer

        from otto.cli import invoke

        calls = []
        monkeypatch.setattr(
            invoke, "validate_project_switches", lambda ctx: calls.append("validate")
        )

        def gate(ctx=None):
            calls.append("gate")
            typer.echo("warning: project 'broken' failed to load, but is inactive", err=True)

        monkeypatch.setattr(invoke, "fail_loud_on_bootstrap_errors", gate)
        monkeypatch.setattr(invoke, "ensure_cli_session", lambda ctx: calls.append("session"))
        monkeypatch.setattr(invoke, "lab_context_refusals", nullcontext)
        monkeypatch.setattr(invoke, "ensure_lab_context", lambda ctx: calls.append("lab"))
        monkeypatch.setattr("otto.run_context.get_context", object)
        monkeypatch.setattr("otto.config.scope.participating", lambda projects, **kw: "out")
        ctx = _ctx({"_otto_root_options": make_root_options(labs=["bench"])})

        assert invoke.listing_participation(ctx, []) == "out"
        assert calls == ["validate", "gate", "session", "lab"]
        assert capsys.readouterr().err.count("failed to load, but is inactive") == 1

    def test_no_lab_prints_cue_and_is_pre_lab_tier(self, monkeypatch, capsys):
        from otto.cli import invoke

        calls = []
        self._wire(monkeypatch, calls=calls)
        ctx = _ctx({"_otto_root_options": make_root_options(exclude_projects=("acme",))})
        projects = [fake_project("acme"), fake_project("other")]
        out = invoke.listing_participation(ctx, projects)
        assert calls == ["validate", "gate"]  # no inline lab load
        assert out.tier == "pre_lab"
        assert [p.name for p in out.kept] == ["other"]
        captured = capsys.readouterr()
        assert "no lab selected, showing every project; -l <lab> narrows" in captured.err
        assert captured.out == ""
