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

    with pytest.raises(
        pydantic.ValidationError, match=r"(?m)^count\n\s+Input should be greater than 0"
    ):
        _Opts(count=-1)


def test_build_options_valid_input_constructs_instance():
    import pydantic

    from otto import options
    from otto.params import build_options

    @options
    class _Opts:
        count: int = pydantic.Field(default=1, gt=0)

    assert build_options(_Opts, {"count": 3}).count == 3


def test_build_options_invalid_input_raises_options_validation_error():
    import pydantic
    import pytest

    from otto import options
    from otto.params import OptionsValidationError, build_options

    @options
    class _Opts:
        count: int = pydantic.Field(default=1, gt=0)

    with pytest.raises(OptionsValidationError, match="count"):
        build_options(_Opts, {"count": -1})


def test_build_options_raises_the_library_error():
    import pytest

    from otto import options
    from otto.errors import OttoError
    from otto.params import OptionsValidationError, build_options

    @options
    class _O:
        n: int = 0

    with pytest.raises(OptionsValidationError, match="n: Input should be a valid integer") as info:
        build_options(_O, {"n": "x"})
    assert isinstance(info.value, OttoError)
    assert isinstance(info.value, ValueError)
    assert "typer" not in type(info.value).__module__


def test_build_options_plain_dataclass_unaffected():
    from dataclasses import dataclass

    from otto.params import build_options

    @dataclass
    class _Plain:
        name: str = "x"

    assert build_options(_Plain, {"name": "y"}).name == "y"


# ── End-to-end through `otto test`'s test-verb options ───────────────────────

from typing import Annotated

import pydantic
import pytest
import typer
from typer.testing import CliRunner

from otto import options
from otto.context import OttoContext, reset_context, set_context
from otto.labs.loader import Lab
from otto.params import register_options


def _ok_result():
    """A zero-exit ``SuiteRunResult`` stub for a faked ``run_tests``."""
    from pathlib import Path

    from otto.suite.run import SuiteRunResult

    return SuiteRunResult(
        exit_code=0,
        junit_paths=[],
        stability_report=None,
        stability_unstable=False,
        output_dir=Path(),
    )


def _test_app():
    """``otto test``'s app, read at call time so it carries the registered verb flags."""
    from otto.cli import test as cli_test

    return cli_test.test_app


@pytest.fixture(autouse=True)
def _stub_cli_bootstrap(monkeypatch):
    """Patch management.create_output_dir and install a stub context.

    Tests in this module invoke ``otto test``'s app directly (not via the main
    callback), so ``init_cli_logging``/``create_output_dir`` have never run
    and there is no active OttoContext. Patch both so the command doesn't
    raise.
    """
    monkeypatch.setattr("otto.logger.management.create_output_dir", lambda *a, **k: None)
    ctx = OttoContext(lab=Lab(name="_test_stub"))
    token = set_context(ctx)
    yield
    reset_context(token)


def _capture_options(monkeypatch) -> dict:
    """Fake ``run_tests``; return the dict what a test reads of each ``test`` class lands in."""
    from otto.params import get_options, verb_option_classes

    seen: dict = {}

    def fake(names, **kw):
        del names, kw
        seen["options"] = [get_options(o.cls) for o in verb_option_classes("test")]
        return _ok_result()

    monkeypatch.setattr("otto.suite.run.run_tests", fake)
    return seen


def test_verb_pydantic_options_reject_bad_value(monkeypatch):
    @options
    class _ValOpts:
        count: Annotated[int, typer.Option(help="positive count")] = pydantic.Field(default=1, gt=0)

    register_options(_ValOpts, verbs=["test"])
    seen = _capture_options(monkeypatch)
    result = CliRunner().invoke(_test_app(), ["test_x", "--count", "-5"])
    assert result.exit_code == 2, result.output
    assert "count" in result.stderr
    assert seen == {}, "run_tests was reached although validation failed"


def test_verb_pydantic_options_accept_good_value(monkeypatch):
    @options
    class _OkOpts:
        count: Annotated[int, typer.Option(help="positive count")] = pydantic.Field(default=1, gt=0)

    register_options(_OkOpts, verbs=["test"])
    seen = _capture_options(monkeypatch)
    result = CliRunner().invoke(_test_app(), ["test_x", "--count", "5"])
    assert result.exit_code == 0, result.output
    assert seen["options"][0].count == 5


def test_verb_field_default_used_when_flag_omitted(monkeypatch):
    """A Field(default=N, constraint) option uses N when omitted — options_params
    must unwrap the FieldInfo, not pass it through as the Typer default.
    """

    @options
    class _DefOpts:
        count: Annotated[int, typer.Option()] = pydantic.Field(default=7, ge=0)

    register_options(_DefOpts, verbs=["test"])
    seen = _capture_options(monkeypatch)
    result = CliRunner().invoke(_test_app(), ["test_x"])  # no --count
    assert result.exit_code == 0, result.output
    assert seen["options"][0].count == 7


def test_verb_plain_dataclass_options_still_work(monkeypatch):
    """A plain @dataclass options class (no validation) still runs."""
    from dataclasses import dataclass

    @dataclass
    class _PlainOpts:
        label: Annotated[str, typer.Option()] = "x"

    register_options(_PlainOpts, verbs=["test"])
    seen = _capture_options(monkeypatch)
    result = CliRunner().invoke(_test_app(), ["test_x", "--label", "y"])
    assert result.exit_code == 0, result.output
    assert seen["options"][0].label == "y"


def test_typer_still_drops_annotated_pydantic_metadata():
    """Pins Typer's own behaviour, so a release that changes it fails here (#1119).

    Typer 0.29 keeps only the base type and its ``Option`` from an
    ``Annotated`` hint, so ``Field(gt=3)`` and ``AfterValidator`` never run on
    the command line. otto therefore validates options in one place, the
    options class, through ``build_options``. When this test fails, Typer has
    started honouring the metadata: decide whether the CLI should keep
    ``build_options`` as the only validator or let Typer refuse the value first,
    so the two never disagree on a message.
    """
    from typing import Annotated

    import pydantic
    import typer
    from typer.testing import CliRunner

    seen: list[object] = []
    app = typer.Typer()

    @app.command()
    def cmd(
        n: Annotated[int, pydantic.Field(gt=3), typer.Option()] = 5,
        a: Annotated[str, pydantic.AfterValidator(str.upper), typer.Option()] = "x",
    ) -> None:
        seen.extend([n, a])

    result = CliRunner().invoke(app, ["--n", "1", "--a", "hi"])
    assert result.exit_code == 0, result.output
    assert seen == [1, "hi"], "Typer now applies Annotated pydantic metadata: see the docstring"
