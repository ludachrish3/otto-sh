"""The temporary session-setup hook owns its own registration."""

import pytest

from otto.host.session_setup import SESSION_SETUPS
from tests._fixtures.session_setup_hooks import noop_session_setup


def _registered(name: str) -> bool:
    return name in SESSION_SETUPS.names()


def test_overlapping_contexts_never_remove_each_others_hook():
    """Closing the first of two live contexts leaves the second's hook registered.

    A host resolves its hook when the hook EXECUTES, so the second host would
    fail its next session open if the first context's exit removed its entry.
    """
    with noop_session_setup("overlap") as first:
        with noop_session_setup("overlap") as second:
            assert first != second
            assert _registered(first)
            assert _registered(second)
        assert _registered(first)
        assert not _registered(second)
    assert not _registered(first)


def _raise_inside(names: "list[str]") -> None:
    with noop_session_setup("raises") as name:
        names.append(name)
        raise RuntimeError("boom")


def test_the_hook_is_removed_when_the_context_raises():
    names: list[str] = []
    with pytest.raises(RuntimeError, match="boom"):
        _raise_inside(names)
    assert names
    assert not _registered(names[0])
