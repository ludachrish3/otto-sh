"""
Unit tests for the ``otto run`` subcommand.

The run subcommand is a dynamic registry: instructions register themselves via
the ``@instruction()`` decorator exported from ``otto.cli.run``.  Tests verify:
  - The subcommand shows help when invoked with no arguments
  - Its callback calls management.create_output_dir based on the invoked subcommand
  - The ``@instruction()`` decorator registers a callable on ``run_app``
  - Decorated instruction bodies actually execute and can interact with hosts
  - The ``options=`` parameter enables dataclass-based option inheritance
"""

from dataclasses import dataclass
from typing import Annotated
from unittest.mock import AsyncMock, patch

import pytest
import typer
from typer.testing import CliRunner

from otto.cli.run import instruction, run_app
from otto.host.unix_host import UnixHost
from otto.utils import CommandStatus, Status

runner = CliRunner()


# ── Help / no-args behaviour ──────────────────────────────────────────────────

class TestRunHelp:
    def test_no_args_shows_usage(self):
        result = runner.invoke(run_app, [])
        # When no subcommands are registered, Typer shows usage (exit_code=2)
        # rather than a clean help page.  Verify the output is usage-style text,
        # not a traceback.
        assert 'Usage' in result.output or 'usage' in result.output.lower()

    def test_help_flag(self):
        result = runner.invoke(run_app, ['--help'])
        assert result.exit_code == 0

    def test_help_short_flag(self):
        result = runner.invoke(run_app, ['-h'])
        assert result.exit_code == 0


# ── Callback behaviour ────────────────────────────────────────────────────────

class TestRunCallback:
    """The run_app callback calls management.create_output_dir when a subcommand runs."""

    def test_log_dir_set_for_subcommand(self):
        """When a subcommand named '_test_cmd' is invoked, the callback should
        call management.create_output_dir('run', '_test_cmd').

        The no_logger_output_dir autouse fixture patches create_output_dir globally;
        we access that mock via the patch target directly.
        """
        from unittest.mock import patch

        @run_app.command('_test_cmd_cb')
        def _test_cmd_cb():
            pass

        with patch('otto.logger.management.create_output_dir') as p_create:
            runner.invoke(run_app, ['_test_cmd_cb'])

        p_create.assert_called_once_with('run', '_test_cmd_cb')


# ── @instruction() decorator ──────────────────────────────────────────────────

class TestInstructionDecorator:
    """The @instruction() helper wraps async functions and registers them on run_app."""

    def test_decorator_registers_instruction(self):
        """A function decorated with @instruction() must appear in run_app's sub-apps."""
        from otto.utils import CommandStatus, Status

        initial_count = len(run_app.registered_groups)

        @instruction('_unit_test_instruction')
        async def _my_instruction() -> CommandStatus:
            return CommandStatus(
                command='echo ok',
                output='ok',
                status=Status.Success,
                retcode=0,
            )

        # The decorator adds a new sub-Typer to run_app
        assert len(run_app.registered_groups) == initial_count + 1

    def test_decorated_instruction_is_invocable(self):
        """A decorated async instruction can be invoked synchronously via CliRunner."""
        from otto.utils import CommandStatus, Status

        @instruction('_unit_test_noop')
        async def _noop() -> CommandStatus:
            return CommandStatus(
                command='true',
                output='',
                status=Status.Success,
                retcode=0,
            )

        result = runner.invoke(run_app, ['_unit_test_noop'])
        assert result.exit_code == 0


# ── Instruction execution ────────────────────────────────────────────────────

class TestInstructionExecution:
    """Verify that instruction bodies run end-to-end, not just register.

    Mock boundary: management.create_output_dir (filesystem I/O, patched by
    no_logger_output_dir autouse fixture) and UnixHost methods (network I/O).
    The @instruction decorator, async_typer_command wrapper, and Typer argument
    parsing all run for real.
    """

    def test_instruction_body_executes(self):
        """The async function body must actually run, not just be registered."""
        execution_log: list[str] = []

        @instruction('_unit_test_exec')
        async def _exec_test() -> CommandStatus:
            execution_log.append('ran')
            return CommandStatus(
                command='test',
                output='executed',
                status=Status.Success,
                retcode=0,
            )

        result = runner.invoke(run_app, ['_unit_test_exec'])

        assert result.exit_code == 0
        assert execution_log == ['ran']

    def test_instruction_receives_typer_arguments(self):
        """Typer argument parsing must work through the @instruction decorator."""
        captured: dict[str, str] = {}

        @instruction('_unit_test_args')
        async def _args_test(
            target: Annotated[str, typer.Argument()],
        ) -> CommandStatus:
            captured['target'] = target
            return CommandStatus(
                command='test',
                output='',
                status=Status.Success,
                retcode=0,
            )

        result = runner.invoke(run_app, ['_unit_test_args', 'my-device'])

        assert result.exit_code == 0
        assert captured['target'] == 'my-device'

    def test_instruction_calls_host_method(self):
        """An instruction that calls host.run() must have that call awaited.

        Mock boundary is at the host method level — acceptable for
        instructions which are thin wrappers around host calls.
        """
        mock_host = AsyncMock(spec=UnixHost)
        mock_host.run.return_value = CommandStatus(
            command='echo hello',
            output='hello',
            status=Status.Success,
            retcode=0,
        )

        @instruction('_unit_test_host')
        async def _host_test() -> CommandStatus:
            return await mock_host.run('echo hello')

        result = runner.invoke(run_app, ['_unit_test_host'])

        assert result.exit_code == 0
        mock_host.run.assert_awaited_once_with('echo hello')


# ── @instruction(options=...) — dataclass option inheritance ─────────────────

class TestInstructionOptions:
    """The ``options=`` parameter on ``@instruction()`` enables dataclass-based
    option inheritance, mirroring the suite pattern.
    """

    def test_instruction_with_options_dataclass(self):
        """Dataclass fields become CLI options on the instruction."""
        @dataclass
        class _Opts:
            name: Annotated[str, typer.Option(help='A name.')] = 'default'

        captured: dict[str, object] = {}

        @instruction('_unit_test_opts_dc', options=_Opts)
        async def _opts_dc(opts: _Opts) -> CommandStatus:
            captured['opts'] = opts
            return CommandStatus('test', '', Status.Success, 0)

        result = runner.invoke(run_app, ['_unit_test_opts_dc', '--name', 'hello'])

        assert result.exit_code == 0
        assert isinstance(captured['opts'], _Opts)
        assert captured['opts'].name == 'hello'

    def test_instruction_pydantic_options_reject_bad_value(self):
        """An @options instruction surfaces a validation failure as a clean CLI
        error (exit 2 + field name), via the same build_options helper the suite
        path uses — confirming the instruction wiring catches typer.BadParameter.
        """
        import pydantic

        from otto import options

        @options
        class _ValOpts:
            count: Annotated[int, typer.Option(help='positive')] = \
                pydantic.Field(default=1, gt=0)

        @instruction('_unit_test_opts_validate', options=_ValOpts)
        async def _opts_validate(opts: _ValOpts) -> CommandStatus:
            return CommandStatus('test', '', Status.Success, 0)

        result = runner.invoke(
            run_app, ['_unit_test_opts_validate', '--count', '-5'],
        )

        assert result.exit_code == 2, result.output
        assert 'count' in result.stderr

    def test_instruction_with_inherited_options(self):
        """Parent + child dataclass fields both appear as CLI options."""
        @dataclass
        class _Parent:
            device: Annotated[str, typer.Option(help='Device.')] = 'router'

        @dataclass
        class _Child(_Parent):
            firmware: Annotated[str, typer.Option(help='Firmware.')] = 'latest'

        captured: dict[str, object] = {}

        @instruction('_unit_test_opts_inherit', options=_Child)
        async def _opts_inherit(opts: _Child) -> CommandStatus:
            captured['opts'] = opts
            return CommandStatus('test', '', Status.Success, 0)

        result = runner.invoke(run_app, [
            '_unit_test_opts_inherit',
            '--device', 'switch',
            '--firmware', 'v2.0',
        ])

        assert result.exit_code == 0
        opts = captured['opts']
        assert isinstance(opts, _Child)
        assert opts.device == 'switch'
        assert opts.firmware == 'v2.0'

    def test_instruction_options_defaults(self):
        """When no CLI flags are passed, dataclass defaults are used."""
        @dataclass
        class _Defaults:
            color: Annotated[str, typer.Option(help='Color.')] = 'blue'

        captured: dict[str, object] = {}

        @instruction('_unit_test_opts_defaults', options=_Defaults)
        async def _opts_defaults(opts: _Defaults) -> CommandStatus:
            captured['opts'] = opts
            return CommandStatus('test', '', Status.Success, 0)

        result = runner.invoke(run_app, ['_unit_test_opts_defaults'])

        assert result.exit_code == 0
        assert captured['opts'].color == 'blue'

    def test_instruction_options_mixed_with_inline_params(self):
        """An instruction can combine an options dataclass with inline params."""
        @dataclass
        class _MixOpts:
            level: Annotated[int, typer.Option(help='Level.')] = 1

        captured: dict[str, object] = {}

        @instruction('_unit_test_opts_mixed', options=_MixOpts)
        async def _opts_mixed(
            opts: _MixOpts,
            verbose: Annotated[bool, typer.Option('--verbose/--quiet')] = False,
        ) -> CommandStatus:
            captured['opts'] = opts
            captured['verbose'] = verbose
            return CommandStatus('test', '', Status.Success, 0)

        result = runner.invoke(run_app, [
            '_unit_test_opts_mixed',
            '--level', '5',
            '--verbose',
        ])

        assert result.exit_code == 0
        assert captured['opts'].level == 5
        assert captured['verbose'] is True

    def test_instruction_options_help_shows_all_fields(self):
        """Both inherited and child fields appear in --help output."""
        @dataclass
        class _HelpParent:
            region: Annotated[str, typer.Option(help='AWS region.')] = 'us-east-1'

        @dataclass
        class _HelpChild(_HelpParent):
            tag: Annotated[str, typer.Option(help='Resource tag.')] = 'dev'

        @instruction('_unit_test_opts_help', options=_HelpChild)
        async def _opts_help(opts: _HelpChild) -> CommandStatus:
            return CommandStatus('test', '', Status.Success, 0)

        result = runner.invoke(run_app, ['_unit_test_opts_help', '--help'])
        assert result.exit_code == 0
        assert '--region' in result.output
        assert '--tag' in result.output

    def test_instruction_without_options_still_works(self):
        """Existing instructions without options= are unaffected."""
        captured: list[str] = []

        @instruction('_unit_test_no_opts')
        async def _no_opts(
            msg: Annotated[str, typer.Option(help='Message.')] = 'hi',
        ) -> CommandStatus:
            captured.append(msg)
            return CommandStatus('test', '', Status.Success, 0)

        result = runner.invoke(run_app, ['_unit_test_no_opts', '--msg', 'bye'])

        assert result.exit_code == 0
        assert captured == ['bye']

    def test_instruction_options_missing_param_raises(self):
        """Passing options= without a matching parameter annotation is an error."""
        @dataclass
        class _Orphan:
            x: Annotated[int, typer.Option()] = 0

        import pytest
        with pytest.raises(TypeError, match='no parameter annotated'):
            @instruction('_unit_test_opts_orphan', options=_Orphan)
            async def _orphan() -> CommandStatus:
                return CommandStatus('test', '', Status.Success, 0)


# ── OttoContext injection ────────────────────────────────────────────────────

class TestInstructionCtxInjection:
    """The ``ctx: OttoContext`` parameter is stripped from the CLI signature and
    injected at call time from the active context.
    """

    def test_instruction_ctx_param_excluded_from_signature(self):
        """A handler declaring ctx: OttoContext must not expose it as a CLI param."""
        from otto.context import OttoContext

        @instruction(name='probe_ctx')
        async def probe(ctx: OttoContext) -> CommandStatus:
            return CommandStatus(command='probe', output='', status=Status.Success, retcode=0)

        import inspect
        assert 'ctx' not in inspect.signature(probe).parameters

    def test_instruction_ctx_and_options_compose(self):
        """An @instruction with both options= and ctx: OttoContext registers without
        raising, exposing the options field but not ctx.
        """
        from otto.context import OttoContext

        @dataclass
        class _CtxOpts:
            level: Annotated[int, typer.Option(help='Level.')] = 1

        import inspect
        # Should not raise:
        @instruction('_unit_test_ctx_opts_compose', options=_CtxOpts)
        async def _ctx_opts_handler(ctx: OttoContext, opts: _CtxOpts) -> CommandStatus:
            return CommandStatus('test', '', Status.Success, 0)

        sig = inspect.signature(_ctx_opts_handler)
        assert 'ctx' not in sig.parameters
        assert 'level' in sig.parameters


@pytest.mark.asyncio
async def test_inject_ctx_supplies_active_context():
    """_inject_ctx wraps a handler so the ctx param is filled from the active context."""
    import inspect

    from otto.cli.run import _inject_ctx
    from otto.configmodule.lab import Lab
    from otto.context import OttoContext, reset_context, set_context

    seen: dict[str, object] = {}

    async def handler(*, ctx: OttoContext, value: int = 0) -> str:
        seen['ctx'] = ctx
        seen['value'] = value
        return 'ok'

    wrapped = _inject_ctx(handler, 'ctx')
    assert 'ctx' not in inspect.signature(wrapped).parameters
    assert 'value' in inspect.signature(wrapped).parameters

    ctx = OttoContext(lab=Lab(name='t'))
    token = set_context(ctx)
    try:
        result = await wrapped(value=5)
        assert result == 'ok'
        assert seen['ctx'] is ctx
        assert seen['value'] == 5
    finally:
        reset_context(token)
