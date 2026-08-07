"""Dispatch gate: errors block, warnings never do; entry renders both."""

import pytest
import typer

from otto import bootstrap as bs
from otto.cli.invoke import fail_loud_on_bootstrap_errors


@pytest.fixture(autouse=True)
def _fresh():
    bs._reset()
    yield
    bs._reset()


def _install_result(monkeypatch, *, errors=(), warnings=()):
    result = bs.BootstrapResult(env=None, repos=[], errors=list(errors), warnings=list(warnings))
    monkeypatch.setattr(bs, "_result", result)
    return result


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
