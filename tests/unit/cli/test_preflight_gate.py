"""The preamble gate: whose unsatisfied dependency stops a run, and whose warns.

Three things this module is deliberate about:

* **The evaluator is REAL.** Only ``bootstrap`` and the runtime context are
  stubbed; the repos are written to disk with real pyprojects and checked
  against the live interpreter. A fake ``preflight`` would let the gate agree
  with a verdict nothing produced.
* **Both arms, every time.** A gate that refused unconditionally passes every
  refusal cell in this file; ``TestSeverity`` pairs each one with the world
  where nothing is refused.
* **The gate has to be REACHED.** ``TestPreambleWiring`` drives the real
  :func:`~otto.cli.invoke.command_preamble` and watches the refusal come out of
  *it* -- and watches a ``lab_free`` command come out unrefused, which is what
  keeps ``otto env sync`` runnable while the condition it fixes holds.

``ABSENT`` is the distribution the negative cases lean on. It is otto's own
test fixture package (plan 2), deliberately absent from the developer's
environment; ``test_the_absent_package_really_is_absent`` refuses to let that
premise rot.
"""

import types
from typing import Any

import pytest
import typer

from otto.cli import invoke
from otto.cli.invoke import refuse_unsatisfied_dependencies
from otto.cli.registry import CommandSpec
from tests._fixtures.clickctx import chain
from tests._fixtures.rootoptions import make_root_options
from tests._fixtures.scoping import verdict

ABSENT = "otto-fixture-beetroot"
"""A distribution that is NOT installed in the developer's environment.

otto's own fixture package, published nowhere -- so "absent" is a property of
the package rather than a hope about this machine."""

PRESENT = "rich"
"""A distribution otto declares and therefore always has."""


@pytest.fixture(autouse=True)
def _wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin rich's width so a WRAP cannot masquerade as a missing word.

    Same pin, same reason, as ``tests/unit/cli/test_instruction_ownership.py``.
    """
    monkeypatch.setenv("COLUMNS", "300")


def _repo(tmp_path, name: str, requirement: str) -> Any:
    """A repo on disk declaring exactly one Python requirement."""
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "otto-sample-{name}"\nversion = "0.1.0"\n'
        f"dependencies = ['{requirement}']\n"
    )
    return types.SimpleNamespace(name=name, sut_dir=root)


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    repos: "list[Any]",
    *,
    include: "tuple[str, ...]" = (),
    exclude: "tuple[str, ...]" = (),
    scopes: "dict[str, Any] | None" = None,
) -> None:
    """Install *repos* as the discovered set and a context with these switches."""
    monkeypatch.setattr(
        "otto.bootstrap.bootstrap",
        lambda: types.SimpleNamespace(ordered_repos=repos, errors=[], repos=repos),
    )
    monkeypatch.setattr(
        "otto.context.get_context",
        lambda: types.SimpleNamespace(
            include_projects=tuple(include),
            exclude_projects=tuple(exclude),
            scopes=dict(scopes or {}),
        ),
    )


def test_the_absent_package_really_is_absent() -> None:
    """Every negative case here rests on this, so it is asserted, not assumed.

    A fixture package that quietly became installable would turn every refusal
    cell below green for the wrong reason -- silently, because a satisfied
    requirement produces no output to notice.
    """
    from importlib import metadata

    with pytest.raises(metadata.PackageNotFoundError):
        metadata.version(ABSENT)


class TestSeverity:
    """Active refuses, inactive warns -- and each is paired with its opposite."""

    def test_an_active_repo_refuses_with_all_three_lines(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path
    ) -> None:
        _wire(monkeypatch, [_repo(tmp_path, "repo4", f"{ABSENT} >= 0.1")])
        with pytest.raises(typer.Exit) as excinfo:
            refuse_unsatisfied_dependencies()
        assert excinfo.value.exit_code == 1
        out = capsys.readouterr().out
        assert f"error: repo 'repo4' requires '{ABSENT} >= 0.1'" in out
        assert "not satisfied in this environment (found: none)" in out
        assert "fix: otto env sync" in out
        assert f"or:  uv pip install '{ABSENT} >= 0.1'" in out

    def test_a_satisfied_repo_is_silent(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path
    ) -> None:
        """The other arm. Without it every cell above passes against a gate
        that refuses unconditionally."""
        _wire(monkeypatch, [_repo(tmp_path, "repo4", f"{PRESENT} >= 1")])
        refuse_unsatisfied_dependencies()  # must not raise
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_a_switched_off_repo_warns_instead_of_refusing(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path
    ) -> None:
        _wire(monkeypatch, [_repo(tmp_path, "repo4", f"{ABSENT} >= 0.1")], exclude=("repo4",))
        refuse_unsatisfied_dependencies()  # must not raise
        captured = capsys.readouterr()
        assert "repo4 is inactive for this run" in captured.err
        assert "error:" not in captured.out

    def test_a_lab_excluded_repo_warns_instead_of_refusing(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path
    ) -> None:
        _wire(
            monkeypatch,
            [_repo(tmp_path, "repo4", f"{ABSENT} >= 0.1")],
            scopes={"repo4": verdict("repo4", excluded=True)},
        )
        refuse_unsatisfied_dependencies()  # must not raise
        assert "continuing without it" in capsys.readouterr().err

    def test_a_host_starved_repo_warns_because_the_gate_runs_after_the_lab(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path
    ) -> None:
        """F7's whole dividend, in one cell.

        A repo whose labs match but whose ``host_patterns`` select no host is
        inactive -- and that shape is INVISIBLE before the lab is built, so the
        pre-lab projection ``inactive_before_lab`` would have called this repo
        active and refused the run over a dependency nobody was going to use.
        Running after the lab session is what buys the correct answer, and this
        is the cell that goes red if the gate is ever hoisted above it.
        """
        _wire(
            monkeypatch,
            [_repo(tmp_path, "repo4", f"{ABSENT} >= 0.1")],
            scopes={"repo4": verdict("repo4", universe=())},
        )
        refuse_unsatisfied_dependencies()  # must not raise
        assert "repo4 is inactive for this run" in capsys.readouterr().err

    def test_include_beats_a_lab_verdict_and_refuses(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """``-I`` says the repo IS part of this run, so its dependency is too."""
        _wire(
            monkeypatch,
            [_repo(tmp_path, "repo4", f"{ABSENT} >= 0.1")],
            include=("repo4",),
            scopes={"repo4": verdict("repo4", excluded=True)},
        )
        with pytest.raises(typer.Exit) as excinfo:
            refuse_unsatisfied_dependencies()
        assert excinfo.value.exit_code == 1

    def test_an_inactive_repo_does_not_suppress_an_active_one(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path
    ) -> None:
        """Mixed worlds are the ordinary case: one repo warns, the other stops
        the run, and neither decides for the other."""
        _wire(
            monkeypatch,
            [
                _repo(tmp_path, "repo3", f"{ABSENT} >= 0.1"),
                _repo(tmp_path, "repo4", f"{ABSENT} >= 0.2"),
            ],
            exclude=("repo3",),
        )
        with pytest.raises(typer.Exit) as excinfo:
            refuse_unsatisfied_dependencies()
        assert excinfo.value.exit_code == 1
        captured = capsys.readouterr()
        assert "repo3 is inactive for this run" in captured.err
        assert "error: repo 'repo4'" in captured.out
        assert "error: repo 'repo3'" not in captured.out


class TestRendering:
    """The sentence has to reach a user intact, and its commands have to paste."""

    def test_a_bracketed_extra_survives_rich_markup(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path
    ) -> None:
        """Rich reads ``[word]`` as a style tag and DELETES it, so an unescaped
        ``uv pip install 'otto-sh[monitor]'`` hands the reader a command for the
        WRONG package. ``fail`` escapes; a hand-rolled ``[red]`` f-string would
        not -- which is what the ``error-render-through-helper`` rule enforces.
        """
        _wire(monkeypatch, [_repo(tmp_path, "repo4", f"{ABSENT}[labjack] >= 0.1")])
        with pytest.raises(typer.Exit) as excinfo:
            refuse_unsatisfied_dependencies()
        assert excinfo.value.exit_code == 1
        assert "[labjack]" in capsys.readouterr().out

    def test_several_findings_share_one_installable_fix_line(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path
    ) -> None:
        """One error line per requirement, one PASTEABLE install for all of them."""
        _wire(
            monkeypatch,
            [
                _repo(tmp_path, "repo3", f"{ABSENT} >= 0.1"),
                _repo(tmp_path, "repo4", f"{ABSENT} >= 0.2"),
            ],
        )
        with pytest.raises(typer.Exit) as excinfo:
            refuse_unsatisfied_dependencies()
        assert excinfo.value.exit_code == 1
        out = capsys.readouterr().out
        assert "error: repo 'repo3'" in out
        assert "error: repo 'repo4'" in out
        assert out.count("fix: otto env sync") == 1
        assert f"or:  uv pip install '{ABSENT} >= 0.1' '{ABSENT} >= 0.2'" in out

    def test_the_refusal_lands_on_stdout(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path
    ) -> None:
        """``fail`` renders through rich to STDOUT, like every other otto
        refusal. Pinned here so a stream change is a decision, not a surprise."""
        _wire(monkeypatch, [_repo(tmp_path, "repo4", f"{ABSENT} >= 0.1")])
        with pytest.raises(typer.Exit) as excinfo:
            refuse_unsatisfied_dependencies()
        assert excinfo.value.exit_code == 1
        captured = capsys.readouterr()
        assert "error: repo 'repo4'" in captured.out
        assert captured.err == ""

    def test_an_uncheckable_repo_warns_on_stderr_and_does_not_refuse(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path
    ) -> None:
        """A dynamic, uninstalled pyproject says the check could not be MADE.

        That is not a verdict about the repo, so it warns whatever the repo's
        activation -- and it never stops a run over a requirement nobody
        declared.
        """
        root = tmp_path / "repo4"
        root.mkdir()
        (root / "pyproject.toml").write_text(
            '[project]\nname = "nothing-installed-anywhere"\n'
            'version = "0.1.0"\ndynamic = ["dependencies"]\n'
        )
        _wire(monkeypatch, [types.SimpleNamespace(name="repo4", sut_dir=root)])
        refuse_unsatisfied_dependencies()  # must not raise
        captured = capsys.readouterr()
        assert "cannot preflight repo4" in captured.err
        assert captured.out == ""


class _PreambleCtx:
    """A leaf ctx the real ``command_preamble`` accepts."""

    def __init__(self, name: str, spec: CommandSpec, parent_name: str = "run") -> None:
        self.command = types.SimpleNamespace(
            name=name, callback=types.SimpleNamespace(__cli_output_dir__=False)
        )
        self.info_name = name
        self.parent = chain("otto", parent_name)
        self.meta: "dict[str, Any]" = {
            "_otto_command_spec": spec,
            "_otto_root_options": make_root_options(),
        }


class TestPreambleWiring:
    """The gate is CALLED, from the right branch."""

    @pytest.fixture(autouse=True)
    def _quiet_preamble(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Everything the preamble does EXCEPT this gate, stubbed to nothing."""
        monkeypatch.setattr(invoke, "ensure_lab_session", lambda ctx, spec: None)
        monkeypatch.setattr(invoke, "refuse_inactive_instruction", lambda ctx: None)
        monkeypatch.setattr(invoke, "stop_at_dry_run_seam", lambda ctx, spec: None)

    def test_the_preamble_refuses_an_unsatisfied_active_repo(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path
    ) -> None:
        """Delete the call from ``command_preamble`` and this cell goes red."""
        _wire(monkeypatch, [_repo(tmp_path, "repo4", f"{ABSENT} >= 0.1")])
        spec = CommandSpec(name="run", loader=None, gate=False)
        with pytest.raises(typer.Exit) as excinfo:
            invoke.command_preamble(_PreambleCtx("flash", spec))  # ty: ignore[invalid-argument-type]
        assert excinfo.value.exit_code == 1
        assert "fix: otto env sync" in capsys.readouterr().out

    def test_the_gate_runs_after_the_lab_session_installs_the_context(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        request: pytest.FixtureRequest,
        tmp_path,
    ) -> None:
        """F7 as a GUARD rather than a comment.

        Nothing here stubs ``get_context``; instead ``ensure_lab_session`` is
        stubbed to INSTALL the runtime context, which is what the real one
        does. Hoist the gate above it -- or put it back in ``bootstrap()``,
        where the spec asked for it -- and this cell raises ``RuntimeError: No
        active OttoContext``. That is the whole reason the spec's placement and
        its ``active()`` requirement could not both be honoured.
        """
        from otto.context import reset_cli_context, set_cli_context

        request.addfinalizer(reset_cli_context)
        repo = _repo(tmp_path, "repo4", f"{ABSENT} >= 0.1")
        monkeypatch.setattr(
            "otto.bootstrap.bootstrap",
            lambda: types.SimpleNamespace(ordered_repos=[repo], errors=[], repos=[repo]),
        )
        monkeypatch.setattr(
            invoke,
            "ensure_lab_session",
            lambda ctx, spec: set_cli_context(
                types.SimpleNamespace(include_projects=(), exclude_projects=("repo4",), scopes={})  # ty: ignore[invalid-argument-type]
            ),
        )
        spec = CommandSpec(name="run", loader=None, gate=False)
        invoke.command_preamble(_PreambleCtx("flash", spec))  # ty: ignore[invalid-argument-type]
        assert "repo4 is inactive for this run" in capsys.readouterr().err

    def test_a_lab_free_command_is_not_refused(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path
    ) -> None:
        """``otto env sync`` IS the fix the refusal names, so it has to run
        while the condition holds. ``lab_free`` is what keeps it outside the
        gate -- structurally, rather than by a hand-maintained exemption list
        that a future command would have to remember to join.
        """
        _wire(monkeypatch, [_repo(tmp_path, "repo4", f"{ABSENT} >= 0.1")])
        spec = CommandSpec(name="env", loader=None, lab_free=True, gate=False)
        invoke.command_preamble(_PreambleCtx("sync", spec, parent_name="env"))  # ty: ignore[invalid-argument-type]
        assert "error:" not in capsys.readouterr().out
