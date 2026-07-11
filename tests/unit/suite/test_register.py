"""
Unit tests for ``otto.suite.register``.

Tests verify:
  - ``register_suite_class()`` registers a sub-Typer into the ``SUITES`` registry,
    resolved lazily by ``suite_app``'s ``RegistryBackedGroup``
  - The generated Typer command has only suite-specific parameters (runner-level
    options live on the ``otto test`` parent callback in ``otto.cli.test``)
  - Dataclass inheritance works: parent fields appear alongside child fields
  - The runner correctly constructs the ``Options`` instance and calls ``run_suite``
  - ``OttoOptionsPlugin`` stores options and is accessible by name via pluginmanager

(``Test*``-prefixed-subclass auto-registration itself is covered by
``test_auto_registration.py``; this file exercises ``register_suite_class()``
directly against non-``Test*``-named probe classes so registration can be
triggered explicitly, independent of ``OttoSuite.__init_subclass__``.)
"""

import inspect
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated
from unittest.mock import patch

import pytest
import typer
from typer.testing import CliRunner

from otto.suite.pytest_plugin import OttoOptionsPlugin
from otto.suite.register import (
    SUITES,
    _options_params,
    register_suite_class,
)

runner = CliRunner()


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_app_with_suite(suite_class: type) -> typer.Typer:
    """Wrap a registered suite in a fresh Typer app for isolated CliRunner tests."""
    if suite_class.__name__ not in SUITES:
        raise LookupError(f"{suite_class.__name__} not found in SUITES")
    app = typer.Typer()
    app.add_typer(SUITES.get(suite_class.__name__).sub_app)
    return app


def _ok_result(exit_code: int = 0):
    """A SuiteRunResult stub for faked library ``run_suite`` calls.

    The runner consumes the library ``run_suite`` (returning a
    ``SuiteRunResult``) and raises ``typer.Exit`` on a non-zero exit code, so a
    fake must return a result carrying an ``exit_code``.
    """
    from otto.suite.run import SuiteRunResult

    return SuiteRunResult(
        exit_code=exit_code,
        junit_paths=[],
        stability_report=None,
        stability_unstable=False,
        output_dir=Path(),
    )


# ── register_suite_class() ──────────────────────────────────────────────────


class TestRegisterSuiteClass:
    def test_register_suite_class_records_source_file(self):
        class _SuiteFileProbe:
            pass

        register_suite_class(_SuiteFileProbe)

        assert SUITES.get("_SuiteFileProbe").file == __file__

    def test_adds_entry_to_registry(self):
        initial = len(SUITES)

        class _SuiteA:
            pass

        register_suite_class(_SuiteA)

        assert len(SUITES) == initial + 1
        entry = SUITES.get("_SuiteA")
        assert entry.name == "_SuiteA"
        assert isinstance(entry.sub_app, typer.Typer)

    @staticmethod
    def _exec_suite_file(suite_file, mod_name: str) -> None:
        """Load and execute *suite_file* as *mod_name*, registered in sys.modules.

        Mirrors ``Repo.import_test_file``'s ``spec_from_file_location`` +
        ``sys.modules[mod_name] = mod`` shape — needed so ``inspect.getfile``
        (called by ``register_suite_class``) can resolve the class's
        source file via ``sys.modules[cls.__module__].__file__``.
        """
        import importlib.util

        spec = importlib.util.spec_from_file_location(mod_name, suite_file)
        assert spec is not None
        assert spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        try:
            spec.loader.exec_module(mod)
        except Exception:
            sys.modules.pop(mod_name, None)
            raise

    def test_duplicate_suite_name_from_different_file_fails_loudly(self, tmp_path):
        """Two DIFFERENT source files registering the same class name collide.

        (Re-registration of the same class from the SAME file — the
        ``pytest.main([suite_file, ...])`` re-import pattern — is a
        deliberate exception to this rule; see
        ``test_reregistration_from_same_file_is_idempotent``.)
        """

        def _write(dirname: str) -> Path:
            suite_file = tmp_path / dirname / "test_dup_probe.py"
            suite_file.parent.mkdir(parents=True, exist_ok=True)
            suite_file.write_text(
                "from otto.suite.register import register_suite_class\n\n\n"
                "class _DupFileSuite:\n"
                "    pass\n\n\n"
                "register_suite_class(_DupFileSuite)\n"
            )
            return suite_file

        try:
            self._exec_suite_file(_write("a"), "_otto_suite_dup_a")
            with pytest.raises(ValueError, match="_DupFileSuite"):
                self._exec_suite_file(_write("b"), "_otto_suite_dup_b")
        finally:
            if "_DupFileSuite" in SUITES:
                SUITES.unregister("_DupFileSuite")
            sys.modules.pop("_otto_suite_dup_a", None)
            sys.modules.pop("_otto_suite_dup_b", None)

    def test_reregistration_from_same_file_is_idempotent(self, tmp_path):
        """Re-registering the SAME class from the SAME source file overwrites silently.

        This is the expected re-import pattern: ``run_suite()`` executes a
        suite via ``pytest.main([suite_file, ...])``, which makes pytest
        re-import the suite file under its own module name — a second,
        expected execution of ``register_suite_class()`` for the same class
        from the same file within one process. It must NOT be treated as a
        collision (unlike two genuinely different files registering the
        same class name, which does raise).
        """
        suite_file = tmp_path / "test_reimport_probe.py"
        suite_file.write_text(
            "from otto.suite.register import register_suite_class\n\n\n"
            "class _ReimportProbe:\n"
            "    pass\n\n\n"
            "register_suite_class(_ReimportProbe)\n"
        )

        try:
            self._exec_suite_file(suite_file, "_otto_suite_test_reimport_probe")
            first_entry = SUITES.get("_ReimportProbe")
            # Re-importing under a DIFFERENT module name (mirrors pytest's own
            # collection giving it a plain "test_reimport_probe" name) must not
            # raise — same file, same class.
            self._exec_suite_file(suite_file, "test_reimport_probe")
            second_entry = SUITES.get("_ReimportProbe")
            assert first_entry.file == second_entry.file == str(suite_file)
        finally:
            if "_ReimportProbe" in SUITES:
                SUITES.unregister("_ReimportProbe")
            sys.modules.pop("_otto_suite_test_reimport_probe", None)
            sys.modules.pop("test_reimport_probe", None)

    def test_suite_without_options_has_only_injected_ctx(self):
        class _SuiteNoOpts:
            pass

        register_suite_class(_SuiteNoOpts)

        sub_app = SUITES.get("_SuiteNoOpts").sub_app
        cmd = sub_app.registered_commands[0]
        sig = inspect.signature(cmd.callback)
        # Only the Typer-injected context param remains; no CLI option params.
        assert set(sig.parameters) == {"ctx"}
        assert sig.parameters["ctx"].annotation is typer.Context

    def test_suite_with_options_includes_option_fields(self):
        class _SuiteWithOpts:
            @dataclass
            class Options:
                device_type: Annotated[str, typer.Option()] = "router"
                count: Annotated[int, typer.Option()] = 3

        register_suite_class(_SuiteWithOpts)

        sub_app = SUITES.get("_SuiteWithOpts").sub_app
        cmd = sub_app.registered_commands[0]
        sig = inspect.signature(cmd.callback)
        assert "device_type" in sig.parameters
        assert "count" in sig.parameters

    def test_suite_docstring_used_as_command_help(self):
        class _SuiteDocstring:
            """My suite docstring."""

        register_suite_class(_SuiteDocstring)

        sub_app = SUITES.get("_SuiteDocstring").sub_app
        cmd = sub_app.registered_commands[0]
        assert cmd.callback.__doc__ == "My suite docstring."


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
        assert p.name == "name"
        assert p.default == "default"
        # Annotation should be Annotated[str, ...]
        origin = getattr(p.annotation, "__args__", None)
        assert origin is not None
        assert origin[0] is str

    def test_int_field(self):
        @dataclass
        class Opts:
            count: Annotated[int, typer.Option()] = 5

        params = _options_params(Opts)
        p = params[0]
        origin = getattr(p.annotation, "__args__", None)
        assert origin is not None
        assert origin[0] is int

    def test_float_field(self):
        @dataclass
        class Opts:
            ratio: Annotated[float, typer.Option()] = 0.9

        params = _options_params(Opts)
        p = params[0]
        origin = getattr(p.annotation, "__args__", None)
        assert origin is not None
        assert origin[0] is float

    def test_bool_field(self):
        @dataclass
        class Opts:
            enabled: Annotated[bool, typer.Option()] = True

        params = _options_params(Opts)
        p = params[0]
        origin = getattr(p.annotation, "__args__", None)
        assert origin is not None
        assert origin[0] is bool

    def test_optional_field(self):
        @dataclass
        class Opts:
            name: Annotated[str | None, typer.Option()] = None

        params = _options_params(Opts)
        p = params[0]
        assert p.default is None


# ── Dataclass inheritance ─────────────────────────────────────────────────────


class TestInheritedOptions:
    def test_parent_fields_present(self):
        @dataclass
        class ParentOpts:
            device_type: Annotated[str, typer.Option()] = "router"

        class _SuiteInherited:
            @dataclass
            class Options(ParentOpts):
                firmware: Annotated[str, typer.Option()] = "latest"

        register_suite_class(_SuiteInherited)

        sub_app = SUITES.get("_SuiteInherited").sub_app
        cmd = sub_app.registered_commands[0]
        sig = inspect.signature(cmd.callback)
        assert "device_type" in sig.parameters
        assert "firmware" in sig.parameters

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
        assert {"vlan", "username", "extra"} == names


# ── Runner invocation and Options construction ────────────────────────────────


class TestRunnerInvocation:
    """The runner constructs the Options instance and calls the library run_suite.

    Since Task 12 the suite runner consumes ``otto.suite.run.run_suite``
    directly (class-first) — ``run_suite(suite, *, options, run_options,
    output_dir)`` — rather than the old CLI wrapper, so these tests patch the
    library entrypoint and assert on the new keyword call convention. The
    runner reads ``output_dir`` from the active context, so a stub context is
    installed for the class.
    """

    @pytest.fixture(autouse=True)
    def _stub_context(self):
        from otto.config.lab import Lab
        from otto.context import OttoContext, reset_context, set_context

        token = set_context(OttoContext(lab=Lab(name="_test_stub")))
        yield
        reset_context(token)

    def test_runner_calls_run_suite_with_options(self):
        """Invoking a suite command constructs the Options instance and calls run_suite."""

        class _SuiteRunner:
            @dataclass
            class Options:
                device_type: Annotated[str, typer.Option()] = "router"

        register_suite_class(_SuiteRunner)

        app = _make_app_with_suite(_SuiteRunner)

        captured: dict[str, object] = {}

        def fake_run_suite(suite, **kw):
            captured["opts"] = kw["options"]
            captured["suite_class"] = suite
            return _ok_result()

        with patch("otto.suite.run.run_suite", fake_run_suite):
            result = runner.invoke(app, ["_SuiteRunner", "--device-type", "switch"])

        assert result.exit_code == 0
        opts = captured.get("opts")
        assert opts is not None
        assert opts.device_type == "switch"  # type: ignore[union-attr]

    def test_runner_uses_defaults_when_options_omitted(self):
        class _SuiteDefaults:
            @dataclass
            class Options:
                count: Annotated[int, typer.Option()] = 7

        register_suite_class(_SuiteDefaults)

        app = _make_app_with_suite(_SuiteDefaults)
        captured: dict[str, object] = {}

        def fake_run_suite(suite, **kw):
            captured["opts"] = kw["options"]
            return _ok_result()

        with patch("otto.suite.run.run_suite", fake_run_suite):
            result = runner.invoke(app, ["_SuiteDefaults"])

        assert result.exit_code == 0
        opts = captured.get("opts")
        assert opts is not None
        assert opts.count == 7  # type: ignore[union-attr]

    def test_runner_calls_library_run_suite_class_first(self):
        """The runner invokes run_suite(suite, *, options, run_options, output_dir)."""
        from otto.suite.run import RunOptions

        class _SuiteArity:
            pass

        register_suite_class(_SuiteArity)

        app = _make_app_with_suite(_SuiteArity)
        captured: dict[str, object] = {}

        def fake_run_suite(suite, **kw):
            captured["suite"] = suite
            captured["kw"] = kw
            return _ok_result()

        with patch("otto.suite.run.run_suite", fake_run_suite):
            result = runner.invoke(app, ["_SuiteArity"])

        assert result.exit_code == 0
        # Suite class is passed positionally (class-first); no CLI ctx / file arg.
        assert captured["suite"] is _SuiteArity
        kw = captured["kw"]
        assert kw["options"] is None  # no Options dataclass
        assert isinstance(kw["run_options"], RunOptions)
        assert "output_dir" in kw

    def test_runner_propagates_nonzero_exit_code(self):
        """A non-zero SuiteRunResult.exit_code becomes a typer.Exit from the runner."""

        class _SuiteFails:
            pass

        register_suite_class(_SuiteFails)

        app = _make_app_with_suite(_SuiteFails)

        def fake_run_suite(suite, **kw):
            return _ok_result(exit_code=5)

        with patch("otto.suite.run.run_suite", fake_run_suite):
            result = runner.invoke(app, ["_SuiteFails"])

        assert result.exit_code == 5


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
        """OttoOptionsPlugin exposes a class-scoped suite_options fixture."""
        plugin = OttoOptionsPlugin({"key": "value"})
        assert hasattr(plugin, "suite_options")
        # Verify it's a pytest fixture wrapper
        method = type(plugin).suite_options
        assert "pytest_fixture" in repr(method)

    def test_provides_ctx_fixture(self):
        """OttoOptionsPlugin exposes a ctx fixture."""
        plugin = OttoOptionsPlugin(None)
        assert hasattr(plugin, "ctx")
        method = type(plugin).ctx
        assert "pytest_fixture" in repr(method)

    def test_ctx_fixture_returns_active_context(self):
        """The ctx fixture body returns the active OttoContext."""
        from otto.config.lab import Lab
        from otto.context import OttoContext, reset_context, set_context

        plugin = OttoOptionsPlugin(None)
        ctx = OttoContext(lab=Lab(name="test"))
        token = set_context(ctx)
        try:
            # Call the underlying fixture function directly (bypassing pytest machinery)
            result = OttoOptionsPlugin.ctx.__wrapped__(plugin)
            assert result is ctx
        finally:
            reset_context(token)


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
        assert by_name["labeled"].help == "Has help."
        assert by_name["unlabeled"].help is None
