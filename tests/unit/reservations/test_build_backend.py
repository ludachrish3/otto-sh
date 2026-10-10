"""Unit tests for the reservation backend factory."""

import json
from pathlib import Path

import pytest

from otto.models.base import OttoModel
from otto.reservations import (
    JsonReservationBackend,
    NullReservationBackend,
    ReservationConstructionError,
    build_backend,
    register_reservation_backend,
)
from otto.reservations.base import ReservationBackendBase


def _write_reservations(path: Path) -> Path:
    f = path / "reservations.json"
    f.write_text(json.dumps({"version": 1, "reservations": []}))
    return f


class TestNoneBackend:
    def test_explicit_none(self, tmp_path):
        backend = build_backend({"backend": "none"}, tmp_path)
        assert isinstance(backend, NullReservationBackend)

    def test_absent_section_is_the_null_backend(self, tmp_path):
        # {} is what build_reservation_gate passes when NO repo has a
        # [reservations] table — no checker specified, nothing to gate.
        backend = build_backend({}, tmp_path)
        assert isinstance(backend, NullReservationBackend)

    def test_present_section_without_backend_is_refused(self, tmp_path):
        # A table with keys but no `backend` is a specified checker missing
        # its one required key — never a silent allow-all.
        with pytest.raises(ValueError, match=r"(?s)Invalid \[reservations\] settings.*backend"):
            build_backend({"url": "https://sched.example"}, tmp_path)


class TestEnvelopeValidation:
    def test_non_string_backend_raises_contextual_value_error(self, tmp_path):
        # A malformed envelope is reported as a ValueError with context, not a
        # raw pydantic ValidationError dump.
        with pytest.raises(ValueError, match=r"Invalid \[reservations\] settings"):
            build_backend({"backend": 3}, tmp_path)

    def test_a_malformed_envelope_never_quotes_the_rejected_value(self, tmp_path):
        with pytest.raises(ValueError, match=r"Invalid \[reservations\] settings") as err:
            build_backend({"backend": ["s3cr3t"]}, tmp_path)
        assert "s3cr3t" not in str(err.value)


class TestJsonBackend:
    def test_absolute_path(self, tmp_path):
        f = _write_reservations(tmp_path)
        backend = build_backend(
            {"backend": "json", "json": {"path": str(f)}},
            repo_dir=tmp_path,
        )
        assert isinstance(backend, JsonReservationBackend)
        assert backend.fetch_reservations("anyone") == []

    def test_relative_path_resolved_against_repo_dir(self, tmp_path):
        _write_reservations(tmp_path)
        backend = build_backend(
            {"backend": "json", "json": {"path": "reservations.json"}},
            repo_dir=tmp_path,
        )
        assert isinstance(backend, JsonReservationBackend)
        assert backend.fetch_reservations("anyone") == []  # read from tmp_path, not the cwd

    def test_missing_path_raises(self, tmp_path):
        with pytest.raises(ReservationConstructionError, match=r"'json'.*parse failed.*path"):
            build_backend({"backend": "json", "json": {}}, tmp_path)

    def test_missing_json_subsection_raises(self, tmp_path):
        with pytest.raises(ReservationConstructionError, match=r"'json'.*parse failed.*path"):
            build_backend({"backend": "json"}, tmp_path)

    def test_an_empty_path_is_refused_not_read_as_the_repo_root(self, tmp_path):
        with pytest.raises(ReservationConstructionError, match="must name the reservation file"):
            build_backend({"backend": "json", "json": {"path": ""}}, tmp_path)

    def test_an_unknown_json_key_names_the_field_and_the_settings_file(self, tmp_path):
        with pytest.raises(ReservationConstructionError) as err:
            build_backend(
                {"backend": "json", "json": {"path": "r.json", "pth": "hunter2"}}, tmp_path
            )
        message = str(err.value)
        assert "parse failed" in message
        assert "pth" in message
        assert str(tmp_path / ".otto" / "settings.toml") in message
        assert "hunter2" not in message

    def test_tilde_path_expands_via_home(self, tmp_path, monkeypatch):
        """A ``~``-prefixed path expands against ``HOME`` before repo-anchoring
        (path-resolution convention, docs/configuration/settings.md).

        Without the fix, ``~`` is never expanded and the path is anchored
        literally under ``repo_dir / "~" / "reservations.json"``, which never
        exists — the backend raises on first read.
        """
        home = tmp_path / "home"
        home.mkdir()
        _write_reservations(home)
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        monkeypatch.setenv("HOME", str(home))

        backend = build_backend(
            {"backend": "json", "json": {"path": "~/reservations.json"}},
            repo_dir=repo_dir,
        )
        assert isinstance(backend, JsonReservationBackend)
        assert backend.fetch_reservations("anyone") == []

    def test_url_forwarded_and_ignored(self, tmp_path):
        """url= forwards cleanly; the JSON backend ignores it."""
        f = _write_reservations(tmp_path)
        backend = build_backend(
            {"backend": "json", "url": "https://example", "json": {"path": str(f)}},
            repo_dir=tmp_path,
        )
        assert isinstance(backend, JsonReservationBackend)


class _ApiKeyConfig(OttoModel, frozen=True):
    api_key: str = ""


class _Recorder(ReservationBackendBase):
    def fetch_reservations(self, username, start=None, end=None):
        return []

    def backend_name(self):
        return "recorder"


def _recorder(c):
    backend = _Recorder(url=c.env.url, repo_dir=c.env.repo_dir, username=c.env.username)
    backend.api_key = c.config.api_key
    return backend


@pytest.fixture
def recorder_registered():
    register_reservation_backend("recorder-test", config=_ApiKeyConfig, factory=_recorder)


class TestRegisteredBackend:
    def test_the_sub_table_url_and_repo_dir_reach_the_factory(self, tmp_path, recorder_registered):
        backend = build_backend(
            {
                "backend": "recorder-test",
                "url": "https://api.example",
                "recorder-test": {"api_key": "secret"},
            },
            repo_dir=tmp_path,
        )
        assert isinstance(backend, _Recorder)
        assert (backend.api_key, backend.url, backend.repo_dir) == (
            "secret",
            "https://api.example",
            tmp_path,
        )

    def test_without_url_the_env_carries_none(self, tmp_path, recorder_registered):
        backend = build_backend({"backend": "recorder-test"}, repo_dir=tmp_path)
        assert backend.url is None

    def test_unknown_backend_name_raises(self, tmp_path):
        with pytest.raises(
            ReservationConstructionError, match="lookup failed: Unknown reservation backend"
        ):
            build_backend({"backend": "mystery"}, tmp_path)

    def test_the_origin_is_named_when_given(self, tmp_path):
        with pytest.raises(ReservationConstructionError, match=r"configured in /etc/x\.toml"):
            build_backend({"backend": "mystery"}, tmp_path, origin="/etc/x.toml")


def test_username_reaches_a_custom_backend(tmp_path, recorder_registered):
    backend = build_backend({"backend": "recorder-test"}, tmp_path, username="alice")
    assert backend.username == "alice"


def test_json_backend_receives_username(tmp_path):
    path = tmp_path / "res.json"
    path.write_text('{"version": 1, "reservations": []}')
    backend = build_backend(
        {"backend": "json", "json": {"path": str(path)}}, tmp_path, username="alice"
    )
    assert backend.username == "alice"


def test_none_backend_receives_username(tmp_path):
    backend = build_backend({"backend": "none"}, tmp_path, username="alice")
    assert backend.username == "alice"


class TestConstructionStages:
    """Every failure past the envelope is a construction error naming its stage."""

    def test_a_backend_without_fetch_reservations_is_a_result_error(self, tmp_path):
        class Stale:
            def backend_name(self):
                return "stale"

        register_reservation_backend("stale-test", config=_ApiKeyConfig, factory=lambda c: Stale())
        with pytest.raises(
            ReservationConstructionError, match=r"result failed.*callable fetch_reservations"
        ):
            build_backend({"backend": "stale-test"}, tmp_path, username="alice")

    def test_a_backend_without_backend_name_is_a_result_error(self, tmp_path):
        class Nameless:
            def fetch_reservations(self, username, start=None, end=None):
                return []

        register_reservation_backend(
            "nameless-test", config=_ApiKeyConfig, factory=lambda c: Nameless()
        )
        with pytest.raises(
            ReservationConstructionError, match=r"result failed.*callable backend_name"
        ):
            build_backend({"backend": "nameless-test"}, tmp_path, username="alice")

    def test_a_factory_that_raises_is_a_construction_error(self, tmp_path):
        def _boom(c):
            raise TypeError("cannot connect")

        register_reservation_backend("boom-test", config=_ApiKeyConfig, factory=_boom)
        with pytest.raises(
            ReservationConstructionError, match=r"construction failed.*cannot connect"
        ):
            build_backend({"backend": "boom-test"}, tmp_path, username="alice")

    def test_a_typo_in_the_sub_table_is_a_parse_error(self, tmp_path, recorder_registered):
        with pytest.raises(ReservationConstructionError) as err:
            build_backend(
                {"backend": "recorder-test", "recorder-test": {"apikey": "secret-value"}},
                tmp_path,
                username="alice",
            )
        message = str(err.value)
        assert "parse failed" in message
        assert "apikey" in message
        assert "secret-value" not in message
