"""The registering-repo marker: bootstrap's attribution channel for init-module imports."""

import pytest

from otto.registry import get_registering_repo, registering_repo


def test_marker_defaults_to_none():
    # Kills: a marker that leaks a default repo name and silently owns
    # otto-internal registrations.
    assert get_registering_repo() is None


def test_marker_set_inside_context_and_restored_after():
    with registering_repo("acme"):
        assert get_registering_repo() == "acme"
    assert get_registering_repo() is None


def test_marker_restores_on_exception():
    # Kills: a plain set/unset pair (no try/finally) that leaves the marker
    # stuck on "acme" after a failed init import, mis-attributing every
    # later repo's registrations.
    # `pytest.raises` outside, so the marker's __exit__ runs first: a bare
    # try/except here is the same guard but trips TRY301.
    with pytest.raises(RuntimeError, match="boom"), registering_repo("acme"):
        raise RuntimeError("boom")
    assert get_registering_repo() is None


def test_marker_nests():
    with registering_repo("outer"):
        with registering_repo("inner"):
            assert get_registering_repo() == "inner"
        assert get_registering_repo() == "outer"


def test_bootstrap_result_carries_ordered_repos():
    # Kills: forgetting to store resolution.ordered — get_ordered_repos()
    # would return [] and the orchestrator would silently walk zero repos.
    import dataclasses

    from otto.bootstrap import BootstrapResult

    field_names = {f.name for f in dataclasses.fields(BootstrapResult)}
    assert "ordered_repos" in field_names
