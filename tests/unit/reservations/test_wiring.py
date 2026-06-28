"""Unit tests for build_reservation_state — the callback's reservation assembly."""

import types

import pytest

import otto.reservations as r
from otto.reservations import (
    NullReservationBackend,
    ReservationBackendError,
    build_reservation_state,
)


def _repo(reservation_settings, sut_dir):
    return types.SimpleNamespace(reservation_settings=reservation_settings, sut_dir=sut_dir)


def test_skip_does_not_build_backend(tmp_path, monkeypatch):
    def _spy(settings, repo_dir):
        raise AssertionError("build_backend must not be called under -R")

    monkeypatch.setattr(r, "build_backend", _spy)
    state = build_reservation_state(
        [_repo({"backend": "none"}, tmp_path)],
        as_user=None,
        skip_reservation_check=True,
        cwd_fallback=tmp_path,
    )
    assert state.backend is None
    assert state.skip_check is True
    assert state.backend_factory is not None


def test_no_skip_builds_backend(tmp_path):
    state = build_reservation_state(
        [_repo({"backend": "none"}, tmp_path)],
        as_user=None,
        skip_reservation_check=False,
        cwd_fallback=tmp_path,
    )
    assert isinstance(state.backend, NullReservationBackend)
    assert state.skip_check is False


def test_factory_builds_on_demand(tmp_path):
    state = build_reservation_state(
        [_repo({"backend": "none"}, tmp_path)],
        as_user=None,
        skip_reservation_check=True,
        cwd_fallback=tmp_path,
    )
    assert isinstance(state.backend_factory(), NullReservationBackend)


def test_build_failure_propagates(tmp_path, monkeypatch):
    def _boom(settings, repo_dir):
        raise ReservationBackendError("unreachable")

    monkeypatch.setattr(r, "build_backend", _boom)
    with pytest.raises(ReservationBackendError):
        build_reservation_state(
            [_repo({"backend": "x"}, tmp_path)],
            as_user=None,
            skip_reservation_check=False,
            cwd_fallback=tmp_path,
        )


def test_as_user_sets_identity(tmp_path):
    state = build_reservation_state(
        [_repo({"backend": "none"}, tmp_path)],
        as_user="bob",
        skip_reservation_check=False,
        cwd_fallback=tmp_path,
    )
    assert state.identity.username == "bob"
    assert state.identity.source == "--as-user"
