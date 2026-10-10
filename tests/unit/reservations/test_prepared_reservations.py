"""A ``[reservations]`` table is prepared by its config model and built by its registry."""

import pytest
from pydantic import model_validator

from otto.models.base import OttoModel
from otto.reservations import (
    ReservationConstructionError,
    ReservationEnv,
    register_reservation_backend,
)
from otto.reservations.factory import build_backend, gate_from_settings


class _Cfg(OttoModel, frozen=True):
    pass


class _Backend:
    def __init__(self, env: ReservationEnv) -> None:
        self.env = env

    def backend_name(self) -> str:
        return "spy"

    def fetch_reservations(self, username, start, end):
        return []


def test_url_reaches_a_custom_backend_through_the_env(tmp_path):
    register_reservation_backend("spy", config=_Cfg, factory=lambda c: _Backend(c.env))
    backend = build_backend({"backend": "spy", "url": "http://x"}, tmp_path, username="me")
    assert (backend.env.url, backend.env.username, backend.env.repo_dir) == (
        "http://x",
        "me",
        tmp_path,
    )


def test_an_absent_table_selects_none(tmp_path):
    assert type(build_backend({}, tmp_path)).__name__ == "NullReservationBackend"


def test_skip_check_prepares_and_builds_nothing_and_a_status_report_reuses_one_preparation(
    tmp_path,
):
    parses: list[int] = []
    builds: list[int] = []

    class Counted(OttoModel, frozen=True):
        @model_validator(mode="after")
        def _count(self):
            parses.append(1)
            return self

    register_reservation_backend(
        "spy", config=Counted, factory=lambda c: builds.append(1) or _Backend(c.env)
    )
    gate = gate_from_settings(
        {"backend": "spy"}, tmp_path, holder="me", skip_reservation_check=True
    )
    assert (parses, builds, gate.backend) == ([], [], None)
    gate.backend_factory()
    gate.backend_factory()
    assert (len(parses), len(builds)) == (1, 2)  # one Prepared, built twice


def test_a_backend_missing_fetch_reservations_is_a_result_error(tmp_path):
    register_reservation_backend("bad", config=_Cfg, factory=lambda c: object())
    with pytest.raises(ReservationConstructionError, match="result"):
        build_backend({"backend": "bad"}, tmp_path)


def test_an_unknown_backend_lists_names(tmp_path):
    with pytest.raises(ReservationConstructionError, match=r"lookup.*json"):
        build_backend({"backend": "jsno"}, tmp_path)
