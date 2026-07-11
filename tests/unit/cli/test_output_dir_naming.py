"""The leaf-invoke preamble names each command's output dir correctly.

A flattened single-command group (``monitor``) is the group-level command
itself, so it must keep the base ``monitor/<TS>`` layout (no ``_monitor``
suffix). Real sub-groups (``run``/``test``/``host``) keep ``<name>/<TS>_<sub>``.
"""

from types import SimpleNamespace
from typing import Any

import pytest

from otto.cli import invoke
from otto.cli.registry import CommandSpec


class _FakeCtx:
    """Minimal stand-in for a click leaf context the preamble inspects."""

    def __init__(self, spec: CommandSpec, command_name: str) -> None:
        # `callback` carries the per-verb output-dir opt-out marker (default on).
        callback = SimpleNamespace(__cli_output_dir__=True)
        self.command = SimpleNamespace(name=command_name, callback=callback)
        self.meta: dict[str, Any] = {
            "_otto_command_spec": spec,
            "_otto_root_options": object(),
        }


@pytest.fixture
def preamble_naming(monkeypatch):
    """Isolate ``command_preamble`` down to its output-dir naming call.

    Stubs bootstrap (no errors), session/lab setup, the reservation gate, and
    the runtime context so the only observable effect is the
    ``create_output_dir(command, sub)`` call, whose args we capture and return.
    """
    calls: list[tuple[str, str | None]] = []

    def _create_output_dir(command: str, sub: str | None = None) -> None:
        calls.append((command, sub))

    def _noop(ctx: Any) -> None:
        pass

    monkeypatch.setattr(invoke, "ensure_cli_session", _noop)
    monkeypatch.setattr(invoke, "ensure_lab_context", _noop)
    monkeypatch.setattr("otto.bootstrap.bootstrap", lambda: SimpleNamespace(errors=[], repos=[]))
    monkeypatch.setattr("otto.logger.management.create_output_dir", _create_output_dir)
    monkeypatch.setattr("otto.context.get_context", lambda: SimpleNamespace(output_dir=None))
    return calls


def test_flat_group_command_omits_sub(preamble_naming):
    # monitor: flattened leaf; ctx.command.name == spec.name -> no sub suffix.
    spec = CommandSpec(name="monitor", loader=None, gate=False)
    invoke.command_preamble(_FakeCtx(spec, command_name="monitor"))  # ty: ignore[invalid-argument-type]
    assert preamble_naming == [("monitor", None)]


def test_subgroup_command_keeps_sub_suffix(preamble_naming):
    # run: real sub-group; leaf name ('smoke') differs from spec.name ('run').
    spec = CommandSpec(name="run", loader=None, gate=False)
    invoke.command_preamble(_FakeCtx(spec, command_name="smoke"))  # ty: ignore[invalid-argument-type]
    assert preamble_naming == [("run", "smoke")]
