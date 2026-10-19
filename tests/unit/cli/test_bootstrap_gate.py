"""Dispatch gate: errors block, warnings never do; entry renders both."""

import re
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer

from otto import bootstrap as bs
from otto.cli.invoke import fail_loud_on_bootstrap_errors
from otto.config.scope import ProjectScopeConfig
from tests._fixtures.bootstrap_seam import install_composed
from tests._fixtures.fake_project import fake_project
from tests._fixtures.rootoptions import make_root_options


@pytest.fixture(autouse=True)
def _fresh():
    bs._reset()
    yield
    bs._reset()


def _install_result(monkeypatch, *, errors=(), warnings=(), projects=()):
    result = bs.BootstrapResult(
        env=None, projects=list(projects), errors=list(errors), warnings=list(warnings)
    )
    return install_composed(monkeypatch, result)


def _fake_ctx(*, labs=None, include=(), exclude=()):
    """The one slice of a click ctx the gate reads: meta['_otto_root_options']."""
    opts = make_root_options(
        labs=labs,
        include_projects=tuple(include),
        exclude_projects=tuple(exclude),
    )
    return SimpleNamespace(meta={"_otto_root_options": opts})


def _broken_project(name, *, lab_patterns=None):
    """A discovered project whose init failed: settings parsed, so [project] is known."""
    scope = (
        None
        if lab_patterns is None
        else ProjectScopeConfig(
            lab_patterns=[re.compile(p) for p in lab_patterns], host_patterns=[]
        )
    )
    return fake_project(name, root=Path(f"/projects/{name}"), project_scope=scope)


def test_gate_ignores_warnings(monkeypatch):
    _install_result(
        monkeypatch,
        warnings=[
            bs.BootstrapWarning(root="x", message="project root x: optional dependency down")
        ],
    )
    fail_loud_on_bootstrap_errors()  # must not raise


def test_gate_still_blocks_on_errors(monkeypatch):
    _install_result(
        monkeypatch, errors=[bs.DependencyError("x", "dependency 'y' is not satisfied")]
    )
    with pytest.raises(typer.Exit) as excinfo:
        fail_loud_on_bootstrap_errors()
    assert excinfo.value.exit_code == 1  # the gate's documented Exit(1)


def test_dependency_error_framing():
    err = bs.DependencyError("/suts/a", "dependency 'b >= 2' is not satisfied: found b 1.0.0")
    assert str(err) == "project root /suts/a: dependency 'b >= 2' is not satisfied: found b 1.0.0"
    assert err.source == "dependencies"


def test_emit_renders_errors_then_warnings(capsys):
    from otto.cli.main import _emit_bootstrap_findings

    result = bs.BootstrapResult(
        env=None,
        projects=[],
        errors=[bs.DependencyError("/suts/a", "dependency 'b' is not satisfied")],
        warnings=[
            bs.BootstrapWarning(
                root="/suts/c",
                message=(
                    "project root /suts/c: optional dependency 'm >= 2' not satisfied "
                    "(found 1.0.0) — feature disabled"
                ),
            )
        ],
    )
    _emit_bootstrap_findings(result)
    err_out = capsys.readouterr().err
    assert "warning: project root /suts/a: dependency 'b' is not satisfied\n" in err_out
    assert (
        "warning: project root /suts/c: optional dependency 'm >= 2' not satisfied "
        "(found 1.0.0) — feature disabled\n"
    ) in err_out
    assert err_out.index("/suts/a") < err_out.index("/suts/c")  # errors first


class TestDemotion:
    """A project that is inactive for THIS invocation cannot fail the invocation.

    The gate runs before the lab is built, so activation here is the pre-lab
    projection: the explicit -I/-E switches plus lab-name inference. Each arm
    below is a separate decision in the loop, so each gets its own test.

    The demotion assertions read ``capsys``, never ``caplog``. Since the root
    callback installs the console handler (spec 2026-08-30 §3.1) a record here
    WOULD reach a handler — but this site deliberately prints instead, for two
    reasons that have nothing to do with when logging starts: ``entry()``
    already wrote ``warning: <err>`` for the same error to STDERR before Typer
    parsed argv, and otto's console handler renders to stdout (the two tests
    below pin both halves — the stream, and the bracketed lab list that a
    rich-rendered route would delete). Asserting on records would therefore pin
    a line the operator never sees. Output is the exit criterion.
    """

    def test_excluded_projects_errors_demote_to_warnings(self, monkeypatch, capsys):
        project = _broken_project("project2")
        _install_result(
            monkeypatch,
            errors=[bs.BootstrapError(project.root, "project2_init", ImportError("paramiko"))],
            projects=[project],
        )
        fail_loud_on_bootstrap_errors(_fake_ctx(exclude=("project2",)))  # must not raise
        err_out = capsys.readouterr().err
        assert "inactive for this run" in err_out
        assert "project2" in err_out

    def test_lab_inactive_projects_errors_demote(self, monkeypatch, capsys):
        project = _broken_project("project2", lab_patterns=["unix_alt"])
        _install_result(
            monkeypatch,
            errors=[bs.BootstrapError(project.root, "project2_init", ImportError("paramiko"))],
            projects=[project],
        )
        fail_loud_on_bootstrap_errors(_fake_ctx(labs=["unix"]))  # must not raise
        assert "inactive for this run" in capsys.readouterr().err

    def test_the_demotion_line_goes_to_stderr_not_stdout(self, monkeypatch, capsys):
        """Not stdout: the run CONTINUES, so this must not enter command output.

        ``entry()`` already printed ``warning: project root <dir>: failed to load …``
        to stderr for the same error; the explanation belongs on the same
        stream, or a redirect separates the scary line from its retraction.
        """
        project = _broken_project("project2")
        _install_result(
            monkeypatch,
            errors=[bs.BootstrapError(project.root, "project2_init", ImportError("paramiko"))],
            projects=[project],
        )
        fail_loud_on_bootstrap_errors(_fake_ctx(exclude=("project2",)))
        captured = capsys.readouterr()
        assert "inactive for this run" in captured.err
        assert captured.out == ""

    def test_the_lab_list_survives_rendering(self, monkeypatch, capsys):
        """``[unix]`` must reach the user intact — rich would delete it.

        The lab-inferred reason interpolates the selection in SQUARE BRACKETS,
        which ``rich.print`` reads as a style tag and drops, rendering
        ``not applicable to lab(s) )`` — a sentence that has lost the only
        detail the operator needs. Pins the plain-text render against a
        well-meaning "make it pretty" edit.
        """
        project = _broken_project("project2", lab_patterns=["unix_alt"])
        _install_result(
            monkeypatch,
            errors=[bs.BootstrapError(project.root, "project2_init", ImportError("paramiko"))],
            projects=[project],
        )
        fail_loud_on_bootstrap_errors(_fake_ctx(labs=["unix"]))
        assert "not applicable to lab(s) [unix]" in capsys.readouterr().err

    def test_active_projects_errors_stay_fatal(self, monkeypatch):
        project = _broken_project("project2", lab_patterns=["unix"])
        _install_result(
            monkeypatch,
            errors=[bs.BootstrapError(project.root, "project2_init", ImportError("paramiko"))],
            projects=[project],
        )
        with pytest.raises(typer.Exit) as excinfo:
            fail_loud_on_bootstrap_errors(_fake_ctx(labs=["unix"]))
        assert excinfo.value.exit_code == 1

    def test_include_forces_the_error_fatal(self, monkeypatch):
        project = _broken_project("project2", lab_patterns=["unix_alt"])
        _install_result(
            monkeypatch,
            errors=[bs.BootstrapError(project.root, "project2_init", ImportError("paramiko"))],
            projects=[project],
        )
        with pytest.raises(typer.Exit) as excinfo:
            fail_loud_on_bootstrap_errors(_fake_ctx(labs=["unix"], include=("project2",)))
        assert excinfo.value.exit_code == 1

    def test_discovery_errors_are_never_demoted(self, monkeypatch):
        # No Project object exists for a settings.toml that failed to parse —
        # there is nothing to attribute activation to.
        _install_result(
            monkeypatch,
            errors=[bs.BootstrapError(Path("/projects/ghost"), "settings.toml", ValueError("bad"))],
            projects=[],
        )
        with pytest.raises(typer.Exit) as excinfo:
            fail_loud_on_bootstrap_errors(_fake_ctx(exclude=("ghost",)))
        assert excinfo.value.exit_code == 1

    def test_no_ctx_keeps_the_strict_gate(self, monkeypatch):
        project = _broken_project("project2")
        _install_result(
            monkeypatch,
            errors=[bs.BootstrapError(project.root, "project2_init", ImportError("x"))],
            projects=[project],
        )
        with pytest.raises(typer.Exit) as excinfo:
            fail_loud_on_bootstrap_errors()
        assert excinfo.value.exit_code == 1

    def test_the_project_name_is_normalized_before_the_switch_is_matched(self, monkeypatch, capsys):
        """``-E proj-2`` must demote a project whose declared name is ``Proj_2``.

        The switch values are PEP-503-normalized at parse time, so a raw
        ``project.name`` on the other side of the comparison would never match
        the only spelling the CLI can hand this gate.
        """
        project = _broken_project("Proj_2")
        _install_result(
            monkeypatch,
            errors=[bs.BootstrapError(project.root, "project2_init", ImportError("paramiko"))],
            projects=[project],
        )
        fail_loud_on_bootstrap_errors(_fake_ctx(exclude=("proj-2",)))  # must not raise
        assert "inactive for this run" in capsys.readouterr().err

    def test_one_active_projects_error_is_still_fatal_beside_a_demoted_one(self, monkeypatch):
        """Demotion is per error, not per run: one surviving fatal still exits."""
        dead = _broken_project("project2")
        alive = _broken_project("project3")
        _install_result(
            monkeypatch,
            errors=[
                bs.BootstrapError(dead.root, "project2_init", ImportError("paramiko")),
                bs.BootstrapError(alive.root, "project3_init", ImportError("asyncssh")),
            ],
            projects=[dead, alive],
        )
        with pytest.raises(typer.Exit) as excinfo:
            fail_loud_on_bootstrap_errors(_fake_ctx(exclude=("project2",)))
        assert excinfo.value.exit_code == 1


def _requiring(name, *, needs):
    """A healthy discovered project with a satisfied required dependency on *needs*."""
    from otto.config.dependencies import ResolvedDependency
    from otto.config.version import Version

    dep = ResolvedDependency(
        name=needs,
        normalized=needs,
        constraint="",
        required=True,
        status="satisfied",
        provider_version=Version("1.0.0"),
    )
    return fake_project(name, root=Path(f"/projects/{name}"), dependencies=[dep])


def test_the_refusal_says_why_a_switched_off_project_still_counts(monkeypatch, capsys):
    """#630: switching off fw does not excuse it while active app requires it."""
    app, fw = _requiring("app", needs="fw"), _broken_project("fw")
    _install_result(
        monkeypatch,
        errors=[bs.BootstrapError(fw.root, "fw_init", ImportError("paramiko"))],
        projects=[app, fw],
    )
    with pytest.raises(typer.Exit) as excinfo:
        fail_loud_on_bootstrap_errors(_fake_ctx(exclude=("fw",)))
    assert excinfo.value.exit_code == 1
    err_out = capsys.readouterr().err
    assert (
        "error: project 'fw' failed to load and is inactive for this run "
        "(--exclude-projects fw), but active project 'app' requires it — "
        "switch 'app' off too with --exclude-projects, or fix 'fw'\n"
    ) in err_out
    assert "continuing without it" not in err_out


def test_the_refusal_names_every_requirer_of_an_out_of_scope_project(monkeypatch, capsys):
    """#630: several active requirers are named, and the lab scope is the reason."""
    app, cli = _requiring("app", needs="fw"), _requiring("cli", needs="fw")
    fw = _broken_project("fw", lab_patterns=["elsewhere"])
    _install_result(
        monkeypatch,
        errors=[bs.BootstrapError(fw.root, "fw_init", ImportError("paramiko"))],
        projects=[app, cli, fw],
    )
    with pytest.raises(typer.Exit) as excinfo:
        fail_loud_on_bootstrap_errors(_fake_ctx(labs=["rig"]))
    assert excinfo.value.exit_code == 1
    assert (
        "error: project 'fw' failed to load and is inactive for this run "
        "(not applicable to lab(s) [rig]), but active projects 'app', 'cli' require it"
        " — switch them off too with --exclude-projects, or fix 'fw'\n"
    ) in capsys.readouterr().err
