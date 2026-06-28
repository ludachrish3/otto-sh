"""The @options alias and build_options validation surfacing."""

import dataclasses


def test_options_is_pydantic_dataclass_decorator():
    from otto import options

    @options
    class _Opts:
        count: int = 1

    # Behaves as a dataclass (otto's introspection contract).
    assert dataclasses.is_dataclass(_Opts)
    assert {f.name for f in dataclasses.fields(_Opts)} == {"count"}
    assert _Opts(count=4).count == 4


def test_options_validates_constraints():
    import pydantic
    import pytest

    from otto import options

    @options
    class _Opts:
        count: int = pydantic.Field(default=1, gt=0)

    with pytest.raises(pydantic.ValidationError):
        _Opts(count=-1)


def test_build_options_valid_input_constructs_instance():
    import pydantic

    from otto import options
    from otto.params import build_options

    @options
    class _Opts:
        count: int = pydantic.Field(default=1, gt=0)

    assert build_options(_Opts, {"count": 3}).count == 3


def test_build_options_invalid_input_raises_bad_parameter():
    import pydantic
    import pytest
    import typer

    from otto import options
    from otto.params import build_options

    # typer.BadParameter (not click's) so Typer 0.26's vendored handler catches it.
    @options
    class _Opts:
        count: int = pydantic.Field(default=1, gt=0)

    with pytest.raises(typer.BadParameter) as exc:
        build_options(_Opts, {"count": -1})
    # The pydantic message (field + reason) is surfaced, not a raw traceback.
    assert "count" in str(exc.value)


def test_build_options_plain_dataclass_unaffected():
    from dataclasses import dataclass

    from otto.params import build_options

    @dataclass
    class _Plain:
        name: str = "x"

    assert build_options(_Plain, {"name": "y"}).name == "y"


# ── End-to-end through the suite CLI path ─────────────────────────────────────

from typing import Annotated

import pydantic
import pytest
import typer
from typer.testing import CliRunner

from otto import options
from otto.cli.test import suite_app
from otto.configmodule.lab import Lab
from otto.context import OttoContext, reset_context, set_context
from otto.suite.register import _SUITE_REGISTRY, register_suite


def _attach(suite_cls) -> None:
    """Attach a freshly @register_suite()'d class's sub-app to suite_app."""
    for name, sub_app in reversed(_SUITE_REGISTRY):
        if name == suite_cls.__name__:
            suite_app.add_typer(sub_app)
            return


@pytest.fixture(autouse=True)
def _stub_cli_bootstrap(monkeypatch):
    """Patch management.create_output_dir and install a stub context.

    Tests in this module invoke ``suite_app`` directly (not via the main
    callback), so ``init_cli_logging``/``create_output_dir`` have never run
    and there is no active OttoContext. Patch both so the callback doesn't
    raise.
    """
    monkeypatch.setattr("otto.logger.management.create_output_dir", lambda *a, **k: None)
    ctx = OttoContext(lab=Lab(name="_test_stub"))
    token = set_context(ctx)
    yield
    reset_context(token)


def test_suite_pydantic_options_reject_bad_value(monkeypatch):
    @register_suite()
    class _ValSuite:
        @options
        class Options:
            count: Annotated[int, typer.Option(help="positive count")] = pydantic.Field(
                default=1, gt=0
            )

    _attach(_ValSuite)

    # run_suite is never reached — validation fails first at construction.
    monkeypatch.setattr("otto.cli.test.run_suite", lambda *a, **k: None)
    result = CliRunner().invoke(suite_app, ["_ValSuite", "--count", "-5"])
    assert result.exit_code == 2, result.output
    assert "count" in result.stderr


def test_suite_pydantic_options_accept_good_value(monkeypatch):
    seen: dict = {}

    @register_suite()
    class _OkSuite:
        @options
        class Options:
            count: Annotated[int, typer.Option(help="positive count")] = pydantic.Field(
                default=1, gt=0
            )

    _attach(_OkSuite)

    # run_suite signature: (suite_cls, suite_file, opts_instance, ctx).
    monkeypatch.setattr(
        "otto.cli.test.run_suite",
        lambda cls, f, opts, ctx: seen.update(count=opts.count),
    )
    result = CliRunner().invoke(suite_app, ["_OkSuite", "--count", "5"])
    assert result.exit_code == 0, result.output
    assert seen["count"] == 5


def test_suite_field_default_used_when_flag_omitted(monkeypatch):
    """A Field(default=N, constraint) option uses N when omitted — options_params
    must unwrap the FieldInfo, not pass it through as the Typer default.
    """
    seen: dict = {}

    @register_suite()
    class _DefSuite:
        @options
        class Options:
            count: Annotated[int, typer.Option()] = pydantic.Field(default=7, ge=0)

    _attach(_DefSuite)

    monkeypatch.setattr(
        "otto.cli.test.run_suite",
        lambda cls, f, opts, ctx: seen.update(count=opts.count),
    )
    result = CliRunner().invoke(suite_app, ["_DefSuite"])  # no --count
    assert result.exit_code == 0, result.output
    assert seen["count"] == 7


def test_suite_plain_dataclass_options_still_work(monkeypatch):
    """Back-compat: a plain @dataclass Options (no validation) still runs."""
    from dataclasses import dataclass

    seen: dict = {}

    @register_suite()
    class _PlainSuite:
        @dataclass
        class Options:
            label: Annotated[str, typer.Option()] = "x"

    _attach(_PlainSuite)

    monkeypatch.setattr(
        "otto.cli.test.run_suite",
        lambda cls, f, opts, ctx: seen.update(label=opts.label),
    )
    result = CliRunner().invoke(suite_app, ["_PlainSuite", "--label", "y"])
    assert result.exit_code == 0, result.output
    assert seen["label"] == "y"
