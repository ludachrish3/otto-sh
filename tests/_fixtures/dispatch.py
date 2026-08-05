"""``DispatchRunner`` — drive a sub-app through otto's production dispatch seam.

Registered commands execute through the root dispatch's leaf-invoke wrapper
(:func:`otto.cli.invoke.wrap_leaf_callbacks`), whose lifecycle bridge runs a
plain ``async def`` leaf under :func:`otto.lifecycle.run_command`. A bare
``typer.testing.CliRunner`` invocation of a sub-app bypasses that wrapper, so
an async leaf would return its un-awaited coroutine and never run — the
documented loud-failure mode (see ``docs/guide/extending-cli.md``), not the
contract sub-app unit tests mean to exercise.

``DispatchRunner`` is typer's ``CliRunner`` with one substitution: the app is
resolved and wrapped exactly the way the root dispatch composes it
(``resolve_spec_command`` + ``wrap_leaf_callbacks`` on a ``lab_free`` spec), so
command bodies run under the real bridge while the runner's isolation, input
feeding, and ``Result`` API stay untouched. ``lab_free=True``/``output_dir=False``
keep the preamble to its bootstrap gate — which is stubbed clean here, since
these tests exercise command bodies, not repo discovery (the gate has its own
tests in ``tests/unit/cli/test_bootstrap_gate.py``).
"""

from collections.abc import Mapping, Sequence
from types import SimpleNamespace
from typing import Any
from unittest import mock

import typer
import typer.testing
from typer.testing import CliRunner, Result


def _clean_bootstrap() -> SimpleNamespace:
    """A bootstrap result with no repo errors — pass the preamble's loud gate."""
    return SimpleNamespace(errors=[])


class DispatchRunner(CliRunner):
    """``CliRunner`` that invokes through the leaf-invoke wrapper (the bridge)."""

    def invoke(
        self,
        app: Any,
        args: str | Sequence[str] | None = None,
        input: bytes | str | None = None,  # noqa: A002 — CliRunner.invoke's own name
        env: Mapping[str, str | None] | None = None,
        catch_exceptions: bool = True,
        color: bool = False,
        *,
        spec_name: str | None = None,
        async_leaves: bool = False,
        **extra: Any,
    ) -> Result:
        """Invoke *app* (a Typer app or plain/async function loader) dispatched.

        *async_leaves* mirrors the real ``run`` registration, whose leaves must
        all be coroutines.

        *spec_name* names the ``CommandSpec`` (and so the resolved command);
        it defaults to the Typer app's own name. Function loaders (which have
        no app name) must pass it.
        """
        from otto.cli.invoke import wrap_leaf_callbacks
        from otto.cli.registry import CommandSpec, resolve_spec_command

        name = spec_name or (app.info.name if isinstance(app, typer.Typer) else None)
        if name is None:
            raise TypeError("spec_name= is required for a non-Typer (function) loader")
        spec = CommandSpec(
            name=name,
            loader=app,
            lab_free=True,
            output_dir=False,
            async_leaves=async_leaves,
        )
        cmd = wrap_leaf_callbacks(resolve_spec_command(spec), spec)
        with (
            # CliRunner.invoke's only use of `app` is `_get_command(app)`;
            # substituting the dispatched command there keeps every other
            # runner behavior (isolation, stdin, Result) byte-identical.
            mock.patch.object(typer.testing, "_get_command", return_value=cmd),
            mock.patch("otto.bootstrap.bootstrap", _clean_bootstrap),
        ):
            return super().invoke(
                app,
                args,
                input=input,
                env=env,
                catch_exceptions=catch_exceptions,
                color=color,
                **extra,
            )
