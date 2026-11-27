"""A click leaf-context stand-in for tests that drive ``command_preamble``.

One home for the fake that ``tests/unit/cli/test_preamble_reservation_gate.py``
and ``tests/unit/cli/test_reservation_gate_coverage.py`` both build. The fixture
that stubs the session around it is ``preamble_gate`` in ``tests/unit/cli/conftest.py``.
"""

from types import SimpleNamespace
from typing import Any

from otto.cli.registry import CommandSpec
from tests._fixtures.clickctx import ChainLeaf, chain
from tests._fixtures.rootoptions import make_root_options


class FakeLeafCtx(ChainLeaf):
    """Minimal stand-in for a click leaf context the preamble inspects.

    ``callback`` is the leaf's real callback when a test asks about a real leaf
    (its ``__cli_*__`` markers are what the preamble reads); absent, a bare
    namespace that only asks for an output directory.
    """

    def __init__(
        self, spec: CommandSpec, reservation: object | None = None, callback: Any = None
    ) -> None:
        if callback is None:
            callback = SimpleNamespace(__cli_output_dir__=True)
        self.command = SimpleNamespace(name=spec.name, callback=callback)
        self.info_name = spec.name
        # A real leaf context has a parent chain, and the preamble's activation
        # gate walks it before reaching the reservation gate.
        self.parent = chain("otto")
        self.meta: dict[str, Any] = {
            "_otto_command_spec": spec,
            # A real RootOptions, not a bare sentinel: the preamble reads the
            # -I/-E tuples off this object before it reaches the gate.
            "_otto_root_options": make_root_options(),
        }
        if reservation is not None:
            self.meta["otto_reservation"] = reservation
