"""Unit tests for build_reservation_gate — the callback's reservation assembly."""

import types

import pytest

import otto.reservations as r
from otto.reservations import (
    NullReservationBackend,
    ReservationBackendError,
    build_reservation_gate,
)


def _repo(reservation_settings, sut_dir):
    return types.SimpleNamespace(reservation_settings=reservation_settings, sut_dir=sut_dir)


def test_skip_does_not_build_backend(tmp_path, monkeypatch):
    def _spy(settings, repo_dir):
        raise AssertionError("build_backend must not be called under -R")

    monkeypatch.setattr(r, "build_backend", _spy)
    gate = build_reservation_gate(
        [_repo({"backend": "none"}, tmp_path)],
        as_user=None,
        skip_reservation_check=True,
        cwd_fallback=tmp_path,
    )
    assert gate.backend is None
    assert gate.skip_check is True
    assert gate.backend_factory is not None


def test_no_skip_builds_backend(tmp_path):
    gate = build_reservation_gate(
        [_repo({"backend": "none"}, tmp_path)],
        as_user=None,
        skip_reservation_check=False,
        cwd_fallback=tmp_path,
    )
    assert isinstance(gate.backend, NullReservationBackend)
    assert gate.skip_check is False


def test_factory_builds_on_demand(tmp_path):
    gate = build_reservation_gate(
        [_repo({"backend": "none"}, tmp_path)],
        as_user=None,
        skip_reservation_check=True,
        cwd_fallback=tmp_path,
    )
    assert isinstance(gate.backend_factory(), NullReservationBackend)


def test_build_failure_propagates(tmp_path, monkeypatch):
    def _boom(settings, repo_dir):
        raise ReservationBackendError("unreachable")

    monkeypatch.setattr(r, "build_backend", _boom)
    with pytest.raises(ReservationBackendError):
        build_reservation_gate(
            [_repo({"backend": "x"}, tmp_path)],
            as_user=None,
            skip_reservation_check=False,
            cwd_fallback=tmp_path,
        )


def test_as_user_sets_identity(tmp_path):
    gate = build_reservation_gate(
        [_repo({"backend": "none"}, tmp_path)],
        as_user="bob",
        skip_reservation_check=False,
        cwd_fallback=tmp_path,
    )
    assert gate.identity.username == "bob"
    assert gate.identity.source == "--as-user"


class TestAPresentTableMustNameItsBackend:
    """The settings load is where a half-written ``[reservations]`` is refused.

    Absent table → no checker (``reservation_settings`` is ``{}`` and the gate
    builds the null backend). A present table — even a bare header — must name
    ``backend``: before this pin the key defaulted to ``"none"``, so a url-only
    table passed every gated command with the held column reading ``n/a``.
    """

    def test_absent_table_means_no_checker(self, tmp_path):
        from otto.config.repo import Repo
        from tests._fixtures.sutrepo import make_sut_repo

        repo = Repo(sut_dir=make_sut_repo(tmp_path))
        assert repo.reservation_settings == {}

    def test_bare_header_refuses_the_load_naming_the_key(self, tmp_path):
        from pydantic import ValidationError

        from otto.config.repo import Repo
        from tests._fixtures.sutrepo import make_sut_repo

        sut = make_sut_repo(tmp_path, extra="[reservations]\n")
        with pytest.raises(ValidationError, match=r"reservations\.backend"):
            Repo(sut_dir=sut)

    def test_url_only_table_refuses_the_load_naming_the_key(self, tmp_path):
        from pydantic import ValidationError

        from otto.config.repo import Repo
        from tests._fixtures.sutrepo import make_sut_repo

        sut = make_sut_repo(tmp_path, extra='[reservations]\nurl = "https://sched.example"\n')
        with pytest.raises(ValidationError, match=r"reservations\.backend"):
            Repo(sut_dir=sut)

    def test_explicit_none_still_loads(self, tmp_path):
        from otto.config.repo import Repo
        from tests._fixtures.sutrepo import make_sut_repo

        repo = Repo(sut_dir=make_sut_repo(tmp_path, extra='[reservations]\nbackend = "none"\n'))
        assert repo.reservation_settings == {"backend": "none"}
