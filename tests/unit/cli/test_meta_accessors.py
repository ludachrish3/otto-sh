"""The typed ``ctx.meta`` accessors in :mod:`otto.cli.invoke`.

``ctx.meta`` is click's untyped per-invocation stash (``dict[str, Any]``); the
accessors are the single seam that narrows what the root callback stored back
to real types. The wrong-type cases matter as much as the happy paths: the
isinstance check is what stands between a drifted stash (or a drifted test
double) and an ``AttributeError`` three frames later.
"""

from types import SimpleNamespace

import pytest

from otto.cli.invoke import (
    command_spec,
    maybe_root_options,
    root_options,
)
from otto.cli.registry import CommandSpec
from tests._fixtures.rootoptions import make_root_options


def _ctx(meta):
    """The one slice of a click ctx the accessors read."""
    return SimpleNamespace(meta=meta)


class TestRootOptions:
    def test_returns_the_stashed_instance(self):
        opts = make_root_options()
        assert root_options(_ctx({"_otto_root_options": opts})) is opts

    def test_foreign_object_in_stash_is_refused(self):
        with pytest.raises(AssertionError, match="RootOptions"):
            root_options(_ctx({"_otto_root_options": SimpleNamespace()}))

    def test_missing_key_raises_key_error(self):
        # Strict variant: absence is a caller bug, exactly as it was when the
        # call sites indexed ``meta["_otto_root_options"]`` themselves.
        with pytest.raises(KeyError):
            root_options(_ctx({}))


class TestMaybeRootOptions:
    def test_none_ctx_is_none(self):
        assert maybe_root_options(None) is None

    def test_missing_key_is_none(self):
        assert maybe_root_options(_ctx({})) is None

    def test_returns_the_stashed_instance(self):
        opts = make_root_options()
        assert maybe_root_options(_ctx({"_otto_root_options": opts})) is opts

    def test_foreign_object_in_stash_is_refused(self):
        with pytest.raises(AssertionError, match="RootOptions"):
            maybe_root_options(_ctx({"_otto_root_options": object()}))


class TestCommandSpec:
    def test_returns_the_stashed_instance(self):
        spec = CommandSpec(name="probe", loader=lambda: None)
        assert command_spec(_ctx({"_otto_command_spec": spec})) is spec

    def test_foreign_object_in_stash_is_refused(self):
        with pytest.raises(AssertionError, match="CommandSpec"):
            command_spec(_ctx({"_otto_command_spec": object()}))
