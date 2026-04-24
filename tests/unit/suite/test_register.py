"""
Unit tests for ``otto.suite.register``.

Tests verify:
  - ``@register_suite()`` adds a sub-Typer to ``_SUITE_REGISTRY`` and to ``testing_app``
  - The generated Typer command has only suite-specific parameters (runner-level
    options live on the ``otto test`` parent callback in ``otto.cli.test``)
  - Dataclass inheritance works: parent fields appear alongside child fields
  - The runner correctly constructs the ``Options`` instance and calls ``run_suite``
  - ``OttoOptionsPlugin`` stores options and is accessible by name via pluginmanager
"""

import inspect
from dataclasses import dataclass
from typing import Annotated, Optional
from unittest.mock import patch

import typer
from typer.testing import CliRunner

from otto.suite.register import (
    OttoOptionsPlugin,
    _SUITE_REGISTRY,
    _options_params,
    register_suite,
)

runner = CliRunner()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_app_with_suite(suite_class: type) -> typer.Typer:
    """Wrap a registered suite in a fresh Typer app for isolated CliRunner tests."""
    # Find the most-recently added entry in _SUITE_REGISTRY for this class
    for name, sub_app in reversed(_SUITE_REGISTRY):
        if name == suite_class.__name__:
            app = typer.Typer()
            app.add_typer(sub_app)
            return app
    raise LookupError(f'{suite_class.__name__} not found in _SUITE_REGISTRY')


# ── @register_suite() decorator ───────────────────────────────────────────────

class TestRegisterSuiteDecorator:

    def test_adds_entry_to_registry(self):
        initial = len(_SUITE_REGISTRY)

        @register_suite()
        class _SuiteA:
            pass

        assert len(_SUITE_REGISTRY) == initial + 1
        assert _SUITE_REGISTRY[-1][0] == '_SuiteA'
        assert isinstance(_SUITE_REGISTRY[-1][1], typer.Typer)

    def test_returns_class_unchanged(self):
        @register_suite()
        class _SuiteB:
            pass

        assert isinstance(_SuiteB, type)

    def test_suite_without_options_has_no_params(self):
        @register_suite()
        class _SuiteNoOpts:
            pass

        _, sub_app = _SUITE_REGISTRY[-1]
        cmd = sub_app.registered_commands[0]
        sig = inspect.signature(cmd.callback)
        assert set(sig.parameters) == set()

    def test_suite_with_options_includes_option_fields(self):
        @register_suite()
        class _SuiteWithOpts:
            @dataclass
            class Options:
                device_type: Annotated[str, typer.Option()] = "router"
                count: Annotated[int, typer.Option()] = 3

        _, sub_app = _SUITE_REGISTRY[-1]
        cmd = sub_app.registered_commands[0]
        sig = inspect.signature(cmd.callback)
        assert 'device_type' in sig.parameters
        assert 'count' in sig.parameters

    def test_suite_docstring_used_as_command_help(self):
        @register_suite()
        class _SuiteDocstring:
            """My suite docstring."""
            pass

        _, sub_app = _SUITE_REGISTRY[-1]
        cmd = sub_app.registered_commands[0]
        assert cmd.callback.__doc__ == 'My suite docstring.'


# ── Parameter types ───────────────────────────────────────────────────────────

class TestOptionsParamTypes:
    """Verify that _options_params() produces Parameters with the right annotations."""

    def test_str_field(self):
        @dataclass
        class Opts:
            name: Annotated[str, typer.Option()] = "default"

        params = _options_params(Opts)
        assert len(params) == 1
        p = params[0]
        assert p.name == 'name'
        assert p.default == 'default'
        # Annotation should be Annotated[str, ...]
        origin = getattr(p.annotation, '__args__', None)
        assert origin is not None and origin[0] is str

    def test_int_field(self):
        @dataclass
        class Opts:
            count: Annotated[int, typer.Option()] = 5

        params = _options_params(Opts)
        p = params[0]
        origin = getattr(p.annotation, '__args__', None)
        assert origin is not None and origin[0] is int

    def test_float_field(self):
        @dataclass
        class Opts:
            ratio: Annotated[float, typer.Option()] = 0.9

        params = _options_params(Opts)
        p = params[0]
        origin = getattr(p.annotation, '__args__', None)
        assert origin is not None and origin[0] is float

    def test_bool_field(self):
        @dataclass
        class Opts:
            enabled: Annotated[bool, typer.Option()] = True

        params = _options_params(Opts)
        p = params[0]
        origin = getattr(p.annotation, '__args__', None)
        assert origin is not None and origin[0] is bool

    def test_optional_field(self):
        @dataclass
        class Opts:
            name: Annotated[Optional[str], typer.Option()] = None

        params = _options_params(Opts)
        p = params[0]
        assert p.default is None


# ── Dataclass inheritance ─────────────────────────────────────────────────────

class TestInheritedOptions:

    def test_parent_fields_present(self):
        @dataclass
        class ParentOpts:
            device_type: Annotated[str, typer.Option()] = "router"

        @register_suite()
        class _SuiteInherited:
            @dataclass
            class Options(ParentOpts):
                firmware: Annotated[str, typer.Option()] = "latest"

        _, sub_app = _SUITE_REGISTRY[-1]
        cmd = sub_app.registered_commands[0]
        sig = inspect.signature(cmd.callback)
        assert 'device_type' in sig.parameters
        assert 'firmware' in sig.parameters

    def test_multiple_parent_fields_combined(self):
        @dataclass
        class NetOpts:
            vlan: Annotated[int, typer.Option()] = 100

        @dataclass
        class AuthOpts:
            username: Annotated[str, typer.Option()] = "admin"

        @dataclass
        class CombinedOpts(NetOpts, AuthOpts):
            extra: Annotated[str, typer.Option()] = "x"

        params = _options_params(CombinedOpts)
        names = {p.name for p in params}
        assert {'vlan', 'username', 'extra'} == names


# ── Runner invocation and Options construction ────────────────────────────────

class TestRunnerInvocation:

    def test_runner_calls_run_suite_with_options(self):
        """Invoking a suite command constructs the Options instance and calls run_suite."""

        @register_suite()
        class _SuiteRunner:
            @dataclass
            class Options:
                device_type: Annotated[str, typer.Option()] = "router"

        app = _make_app_with_suite(_SuiteRunner)

        captured: dict[str, object] = {}

        def fake_run_suite(suite_class, suite_file, opts_instance):
            captured['opts'] = opts_instance
            captured['suite_class'] = suite_class

        with patch('otto.cli.test.run_suite', fake_run_suite):
            result = runner.invoke(app, ['_SuiteRunner', '--device-type', 'switch'])

        assert result.exit_code == 0
        opts = captured.get('opts')
        assert opts is not None
        assert opts.device_type == 'switch'  # type: ignore[union-attr]

    def test_runner_uses_defaults_when_options_omitted(self):
        @register_suite()
        class _SuiteDefaults:
            @dataclass
            class Options:
                count: Annotated[int, typer.Option()] = 7

        app = _make_app_with_suite(_SuiteDefaults)
        captured: dict[str, object] = {}

        def fake_run_suite(suite_class, suite_file, opts_instance):
            captured['opts'] = opts_instance

        with patch('otto.cli.test.run_suite', fake_run_suite):
            result = runner.invoke(app, ['_SuiteDefaults'])

        assert result.exit_code == 0
        opts = captured.get('opts')
        assert opts is not None
        assert opts.count == 7  # type: ignore[union-attr]

    def test_runner_called_with_three_args(self):
        """The runner closure invokes run_suite with exactly (suite_cls, file, opts)."""
        @register_suite()
        class _SuiteArity:
            pass

        app = _make_app_with_suite(_SuiteArity)
        captured: dict[str, object] = {}

        def fake_run_suite(suite_class, suite_file, opts_instance):
            captured['args'] = (suite_class, suite_file, opts_instance)

        with patch('otto.cli.test.run_suite', fake_run_suite):
            result = runner.invoke(app, ['_SuiteArity'])

        assert result.exit_code == 0
        args = captured['args']
        assert isinstance(args, tuple) and len(args) == 3
        assert args[0] is _SuiteArity
        assert args[2] is None  # no Options dataclass


# ── OttoOptionsPlugin ─────────────────────────────────────────────────────────

class TestOttoOptionsPlugin:

    def test_stores_options(self):
        opts = object()
        plugin = OttoOptionsPlugin(opts)
        assert plugin.options is opts

    def test_stores_none_for_no_options(self):
        plugin = OttoOptionsPlugin(None)
        assert plugin.options is None

    def test_provides_suite_options_fixture(self):
        """OttoOptionsPlugin exposes a session-scoped suite_options fixture."""
        plugin = OttoOptionsPlugin({"key": "value"})
        assert hasattr(plugin, 'suite_options')
        # Verify it's a pytest fixture wrapper
        method = type(plugin).suite_options
        assert 'pytest_fixture' in repr(method)


# ── Annotated[T, typer.Option(...)] field help text ───────────────────────────

class TestTyperAnnotatedFields:
    """Verify that Annotated[T, typer.Option(help=...)] help text is preserved."""

    def test_help_text_preserved(self):
        @dataclass
        class _HelpAnnotatedOpts:
            device_type: Annotated[str, typer.Option(help="My help text.")] = "router"

        params = _options_params(_HelpAnnotatedOpts)
        opt_info = params[0].annotation.__metadata__[0]
        assert opt_info.help == "My help text."

    def test_no_help_when_omitted(self):
        @dataclass
        class _BareAnnotatedOpts:
            count: Annotated[int, typer.Option()] = 5

        params = _options_params(_BareAnnotatedOpts)
        opt_info = params[0].annotation.__metadata__[0]
        assert opt_info.help is None

    def test_mixed_annotated_fields(self):
        @dataclass
        class _MixedAnnotatedOpts:
            labeled: Annotated[str, typer.Option(help="Has help.")] = "x"
            unlabeled: Annotated[str, typer.Option()] = "y"

        params = _options_params(_MixedAnnotatedOpts)
        by_name = {p.name: p.annotation.__metadata__[0] for p in params}
        assert by_name['labeled'].help == "Has help."
        assert by_name['unlabeled'].help is None
