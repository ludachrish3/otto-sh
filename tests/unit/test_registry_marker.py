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


def test_bootstrap_result_declares_an_ordered_repos_field():
    # Kills: the FIELD never landing on the carrier (dropped, or renamed out
    # from under `otto.config.get_ordered_repos`, which is nothing but this
    # attribute read) — every orchestrator walk would die on an AttributeError.
    #
    # IT DOES NOT KILL AN UNPOPULATED FIELD, though the shape invites the
    # claim: `ordered_repos` has a `default_factory=list`, so a bootstrap that
    # builds its result WITHOUT `ordered_repos=resolution.ordered` leaves it []
    # and this assertion passes unchanged. The VALUE is pinned where it can be
    # observed against a real resolution — tests/unit/bootstrap/
    # test_bootstrap_dependencies.py::test_required_dep_reorders_registration,
    # which asserts the resolved order both on the field and through
    # get_ordered_repos(), and ::test_missing_required_skips_registration,
    # which asserts a skipped repo is absent from it.
    import dataclasses

    from otto.bootstrap import BootstrapResult

    field_names = {f.name for f in dataclasses.fields(BootstrapResult)}
    assert "ordered_repos" in field_names
