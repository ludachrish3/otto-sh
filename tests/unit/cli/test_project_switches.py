"""The -I/-E parse layer: normalization, comma-splitting, and the conflict rule."""

from types import SimpleNamespace

import pytest
import typer

from otto.cli.invoke import validate_project_switches
from otto.cli.main import _project_completer, parse_project_list
from tests._fixtures.fake_project import fake_project
from tests.unit.cli.test_bootstrap_gate import _fake_ctx, _install_result


class TestParseProjectList:
    def test_none_passes_through(self):
        assert parse_project_list(None) is None

    def test_each_occurrence_splits_on_commas(self):
        assert parse_project_list(["a,b", "c"]) == ["a", "b", "c"]

    def test_values_are_pep503_normalized(self):
        assert parse_project_list(["Proj.A,proj_B"]) == ["proj-a", "proj-b"]

    def test_empty_segments_are_dropped(self):
        assert parse_project_list(["a,,b,"]) == ["a", "b"]

    def test_segments_are_stripped_before_normalizing(self):
        # normalize_name collapses [-_.] runs but leaves whitespace alone, so a
        # segment that is only *tested* for emptiness after stripping still gets
        # STORED with its spaces — a name that matches no project.
        assert parse_project_list(["proj-a, proj-b"]) == ["proj-a", "proj-b"]

    def test_a_whitespace_only_segment_is_dropped_not_stored(self):
        assert parse_project_list(["a,   ,b"]) == ["a", "b"]


class TestConflict:
    def test_same_name_in_both_is_a_usage_error(self):
        from otto.cli.main import _refuse_contradictory_switches

        with pytest.raises(typer.BadParameter, match="proj-a"):
            _refuse_contradictory_switches(["proj-a"], ["proj-a", "proj-b"])

    def test_disjoint_switches_pass(self):
        from otto.cli.main import _refuse_contradictory_switches

        _refuse_contradictory_switches(["proj-a"], ["proj-b"])  # must not raise

    def test_whitespace_cannot_smuggle_a_contradiction_past_the_check(self):
        """`-I "proj-a" -E " proj-a"` is one name twice — exit 2, not accepted.

        The check compares the PARSED values, so this is only true while
        parse_project_list strips. Driven through the parse layer deliberately:
        handing pre-normalized input would test the check against a shape the
        real CLI can no longer produce.
        """
        from otto.cli.main import _refuse_contradictory_switches

        with pytest.raises(typer.BadParameter, match="proj-a"):
            _refuse_contradictory_switches(
                parse_project_list(["proj-a"]), parse_project_list([" proj-a"])
            )


class TestProjectCompleter:
    def test_offers_discovered_project_names(self, monkeypatch):
        monkeypatch.setattr(
            "otto.config.project.discover",
            lambda: SimpleNamespace(projects=[fake_project("project1"), fake_project("project2")]),
        )
        assert _project_completer(None, "proj") == ["project1", "project2"]
        assert _project_completer(None, "project2") == ["project2"]

    def test_swallows_a_broken_world(self, monkeypatch):
        def _boom():
            raise RuntimeError("half-registered")

        monkeypatch.setattr("otto.config.project.discover", _boom)
        assert _project_completer(None, "") == []

    def test_completion_never_runs_user_code(self, monkeypatch):
        """Completion reads phase 1 only — `bootstrap()` imports sibling project inits.

        A project whose init is slow or opens a socket must not be able to hang
        `otto -I <TAB>`, and the bare except above would hide the reason. So
        assert the phase-2 entry points are never touched, rather than trusting
        the import line to stay put.
        """
        monkeypatch.setattr(
            "otto.config.project.discover",
            lambda: SimpleNamespace(projects=[fake_project("project1")]),
        )

        def _forbidden(*args, **kwargs):
            raise AssertionError("completion ran phase 2 (user code)")

        monkeypatch.setattr("otto.bootstrap.bootstrap", _forbidden)
        monkeypatch.setattr("otto.declared.get_projects", _forbidden)

        assert _project_completer(None, "proj") == ["project1"]


class TestUnknownName:
    """A ``-I``/``-E`` name discovery never found is a usage error — exit 2.

    Checked at FIRST USE rather than at parse: the check needs the discovered
    project set, and bootstrapping inside the root callback would put it on every
    ``--help`` and completion path. The fake bootstrap result comes from
    ``test_bootstrap_gate``'s helper so both gates are pinned against one
    patching idiom — the cached-result attribute is spelled in one place.
    """

    def test_unknown_name_exits_2_with_a_suggestion(self, monkeypatch, capsys):
        _install_result(monkeypatch, projects=[fake_project("project2")])
        with pytest.raises(typer.Exit) as excinfo:
            validate_project_switches(_fake_ctx(exclude=("projectt2",)))
        assert excinfo.value.exit_code == 2
        assert "did you mean 'project2'" in capsys.readouterr().out

    def test_an_unknown_include_name_is_caught_too(self, monkeypatch, capsys):
        """Both switches are validated; -I is not the untested half."""
        _install_result(monkeypatch, projects=[fake_project("project2")])
        with pytest.raises(typer.Exit) as excinfo:
            validate_project_switches(_fake_ctx(include=("ghost",)))
        assert excinfo.value.exit_code == 2
        assert "no project 'ghost'" in capsys.readouterr().out

    def test_known_names_pass(self, monkeypatch):
        _install_result(monkeypatch, projects=[fake_project("project2")])
        validate_project_switches(_fake_ctx(exclude=("project2",)))  # must not raise

    def test_a_project_name_is_normalized_before_comparison(self, monkeypatch):
        """``-E proj-2`` names the project declared as ``Proj_2``.

        Parse normalizes the switch value; the project side has to be normalized
        too or the only spelling the CLI can produce is rejected as unknown.
        """
        _install_result(monkeypatch, projects=[fake_project("Proj_2")])
        validate_project_switches(_fake_ctx(exclude=("proj-2",)))  # must not raise

    def test_no_switches_never_bootstraps(self, monkeypatch):
        """With nothing to validate, the validator looks nothing up.

        Not a user-code saving: ``entry()`` bootstraps before argv parsing and
        the gate on the next line of ``command_preamble`` bootstraps again, so
        by here the project set is built either way. What this pins is that the
        function is a no-op when both tuples are empty — the branch every
        switch-less invocation takes.
        """

        def _forbidden():
            raise AssertionError("validate_project_switches bootstrapped with no switch given")

        monkeypatch.setattr("otto.bootstrap.bootstrap", _forbidden)
        validate_project_switches(_fake_ctx())  # must not raise
