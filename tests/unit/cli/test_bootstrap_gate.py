"""Dispatch gate: errors block, warnings never do; entry renders both."""

import re
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer

from otto import bootstrap as bs
from otto.cli.invoke import fail_loud_on_bootstrap_errors
from otto.config.scope import ProjectScopeConfig
from tests._fixtures.rootoptions import make_root_options


@pytest.fixture(autouse=True)
def _fresh():
    bs._reset()
    yield
    bs._reset()


def _install_result(monkeypatch, *, errors=(), warnings=(), repos=()):
    result = bs.BootstrapResult(
        env=None, repos=list(repos), errors=list(errors), warnings=list(warnings)
    )
    monkeypatch.setattr(bs, "_result", result)
    return result


def _fake_ctx(*, labs=None, include=(), exclude=()):
    """The one slice of a click ctx the gate reads: meta['_otto_root_options']."""
    opts = make_root_options(
        labs=labs,
        include_projects=tuple(include),
        exclude_projects=tuple(exclude),
    )
    return SimpleNamespace(meta={"_otto_root_options": opts})


def _broken_repo(name, *, lab_patterns=None):
    """A discovered repo whose init failed: settings parsed, so [project] is known."""
    scope = (
        None
        if lab_patterns is None
        else ProjectScopeConfig(
            lab_patterns=[re.compile(p) for p in lab_patterns], host_patterns=[]
        )
    )
    return SimpleNamespace(name=name, sut_dir=Path(f"/repos/{name}"), project_scope=scope)


def test_gate_ignores_warnings(monkeypatch):
    _install_result(
        monkeypatch,
        warnings=[bs.BootstrapWarning(sut_dir="x", message="repo x: optional dependency down")],
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
    assert str(err) == "repo /suts/a: dependency 'b >= 2' is not satisfied: found b 1.0.0"
    assert err.source == "dependencies"


def test_emit_renders_errors_then_warnings(capsys):
    from otto.cli.main import _emit_bootstrap_findings

    result = bs.BootstrapResult(
        env=None,
        repos=[],
        errors=[bs.DependencyError("/suts/a", "dependency 'b' is not satisfied")],
        warnings=[
            bs.BootstrapWarning(
                sut_dir="/suts/c",
                message=(
                    "repo /suts/c: optional dependency 'm >= 2' not satisfied "
                    "(found 1.0.0) — feature disabled"
                ),
            )
        ],
    )
    _emit_bootstrap_findings(result)
    err_out = capsys.readouterr().err
    assert "warning: repo /suts/a: dependency 'b' is not satisfied\n" in err_out
    assert (
        "warning: repo /suts/c: optional dependency 'm >= 2' not satisfied "
        "(found 1.0.0) — feature disabled\n"
    ) in err_out
    assert err_out.index("/suts/a") < err_out.index("/suts/c")  # errors first


class TestDemotion:
    """A repo that is inactive for THIS invocation cannot fail the invocation.

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

    def test_excluded_repos_errors_demote_to_warnings(self, monkeypatch, capsys):
        repo = _broken_repo("repo2")
        _install_result(
            monkeypatch,
            errors=[bs.BootstrapError(repo.sut_dir, "repo2_init", ImportError("paramiko"))],
            repos=[repo],
        )
        fail_loud_on_bootstrap_errors(_fake_ctx(exclude=("repo2",)))  # must not raise
        err_out = capsys.readouterr().err
        assert "inactive for this run" in err_out
        assert "repo2" in err_out

    def test_lab_inactive_repos_errors_demote(self, monkeypatch, capsys):
        repo = _broken_repo("repo2", lab_patterns=["unix_alt"])
        _install_result(
            monkeypatch,
            errors=[bs.BootstrapError(repo.sut_dir, "repo2_init", ImportError("paramiko"))],
            repos=[repo],
        )
        fail_loud_on_bootstrap_errors(_fake_ctx(labs=["unix"]))  # must not raise
        assert "inactive for this run" in capsys.readouterr().err

    def test_the_demotion_line_goes_to_stderr_not_stdout(self, monkeypatch, capsys):
        """Not stdout: the run CONTINUES, so this must not enter command output.

        ``entry()`` already printed ``warning: repo <dir>: failed to load …``
        to stderr for the same error; the explanation belongs on the same
        stream, or a redirect separates the scary line from its retraction.
        """
        repo = _broken_repo("repo2")
        _install_result(
            monkeypatch,
            errors=[bs.BootstrapError(repo.sut_dir, "repo2_init", ImportError("paramiko"))],
            repos=[repo],
        )
        fail_loud_on_bootstrap_errors(_fake_ctx(exclude=("repo2",)))
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
        repo = _broken_repo("repo2", lab_patterns=["unix_alt"])
        _install_result(
            monkeypatch,
            errors=[bs.BootstrapError(repo.sut_dir, "repo2_init", ImportError("paramiko"))],
            repos=[repo],
        )
        fail_loud_on_bootstrap_errors(_fake_ctx(labs=["unix"]))
        assert "not applicable to lab(s) [unix]" in capsys.readouterr().err

    def test_active_repos_errors_stay_fatal(self, monkeypatch):
        repo = _broken_repo("repo2", lab_patterns=["unix"])
        _install_result(
            monkeypatch,
            errors=[bs.BootstrapError(repo.sut_dir, "repo2_init", ImportError("paramiko"))],
            repos=[repo],
        )
        with pytest.raises(typer.Exit) as excinfo:
            fail_loud_on_bootstrap_errors(_fake_ctx(labs=["unix"]))
        assert excinfo.value.exit_code == 1

    def test_include_forces_the_error_fatal(self, monkeypatch):
        repo = _broken_repo("repo2", lab_patterns=["unix_alt"])
        _install_result(
            monkeypatch,
            errors=[bs.BootstrapError(repo.sut_dir, "repo2_init", ImportError("paramiko"))],
            repos=[repo],
        )
        with pytest.raises(typer.Exit) as excinfo:
            fail_loud_on_bootstrap_errors(_fake_ctx(labs=["unix"], include=("repo2",)))
        assert excinfo.value.exit_code == 1

    def test_discovery_errors_are_never_demoted(self, monkeypatch):
        # No Repo object exists for a settings.toml that failed to parse —
        # there is nothing to attribute activation to.
        _install_result(
            monkeypatch,
            errors=[bs.BootstrapError(Path("/repos/ghost"), "settings.toml", ValueError("bad"))],
            repos=[],
        )
        with pytest.raises(typer.Exit) as excinfo:
            fail_loud_on_bootstrap_errors(_fake_ctx(exclude=("ghost",)))
        assert excinfo.value.exit_code == 1

    def test_no_ctx_keeps_the_strict_gate(self, monkeypatch):
        repo = _broken_repo("repo2")
        _install_result(
            monkeypatch,
            errors=[bs.BootstrapError(repo.sut_dir, "repo2_init", ImportError("x"))],
            repos=[repo],
        )
        with pytest.raises(typer.Exit) as excinfo:
            fail_loud_on_bootstrap_errors()
        assert excinfo.value.exit_code == 1

    def test_the_repo_name_is_normalized_before_the_switch_is_matched(self, monkeypatch, capsys):
        """``-E repo-2`` must demote a repo whose declared name is ``Repo_2``.

        The switch values are PEP-503-normalized at parse time, so a raw
        ``repo.name`` on the other side of the comparison would never match
        the only spelling the CLI can hand this gate.
        """
        repo = _broken_repo("Repo_2")
        _install_result(
            monkeypatch,
            errors=[bs.BootstrapError(repo.sut_dir, "repo2_init", ImportError("paramiko"))],
            repos=[repo],
        )
        fail_loud_on_bootstrap_errors(_fake_ctx(exclude=("repo-2",)))  # must not raise
        assert "inactive for this run" in capsys.readouterr().err

    def test_one_active_repos_error_is_still_fatal_beside_a_demoted_one(self, monkeypatch):
        """Demotion is per error, not per run: one surviving fatal still exits."""
        dead = _broken_repo("repo2")
        alive = _broken_repo("repo3")
        _install_result(
            monkeypatch,
            errors=[
                bs.BootstrapError(dead.sut_dir, "repo2_init", ImportError("paramiko")),
                bs.BootstrapError(alive.sut_dir, "repo3_init", ImportError("asyncssh")),
            ],
            repos=[dead, alive],
        )
        with pytest.raises(typer.Exit) as excinfo:
            fail_loud_on_bootstrap_errors(_fake_ctx(exclude=("repo2",)))
        assert excinfo.value.exit_code == 1
