"""Unit tests for the reservation backend factory."""

import json
from pathlib import Path

import pytest

from otto.reservations import (
    JsonReservationBackend,
    NullReservationBackend,
    build_backend,
)


def _write_reservations(path: Path) -> Path:
    f = path / "reservations.json"
    f.write_text(json.dumps({"version": 1, "reservations": []}))
    return f


class TestNoneBackend:
    def test_explicit_none(self, tmp_path):
        backend = build_backend({"backend": "none"}, tmp_path)
        assert isinstance(backend, NullReservationBackend)

    def test_missing_backend_defaults_to_none(self, tmp_path):
        backend = build_backend({}, tmp_path)
        assert isinstance(backend, NullReservationBackend)


class TestEnvelopeValidation:
    def test_non_string_backend_raises_contextual_value_error(self, tmp_path):
        # A malformed envelope is reported as a ValueError with context, not a
        # raw pydantic ValidationError dump.
        with pytest.raises(ValueError, match=r"Invalid \[reservations\] settings"):
            build_backend({"backend": 3}, tmp_path)


class TestJsonBackend:
    def test_absolute_path(self, tmp_path):
        f = _write_reservations(tmp_path)
        backend = build_backend(
            {"backend": "json", "json": {"path": str(f)}},
            repo_dir=tmp_path,
        )
        assert isinstance(backend, JsonReservationBackend)
        assert backend.get_reserved_resources("anyone") == set()

    def test_relative_path_resolved_against_repo_dir(self, tmp_path):
        _write_reservations(tmp_path)
        backend = build_backend(
            {"backend": "json", "json": {"path": "reservations.json"}},
            repo_dir=tmp_path,
        )
        assert isinstance(backend, JsonReservationBackend)

    def test_missing_path_raises(self, tmp_path):
        with pytest.raises(ValueError, match="requires a 'path'"):
            build_backend({"backend": "json", "json": {}}, tmp_path)

    def test_missing_json_subsection_raises(self, tmp_path):
        with pytest.raises(ValueError, match="requires a 'path'"):
            build_backend({"backend": "json"}, tmp_path)

    def test_url_forwarded_and_ignored(self, tmp_path):
        """url= forwards cleanly; the JSON backend ignores it."""
        f = _write_reservations(tmp_path)
        backend = build_backend(
            {"backend": "json", "url": "https://example", "json": {"path": str(f)}},
            repo_dir=tmp_path,
        )
        assert isinstance(backend, JsonReservationBackend)


class TestRegisteredBackend:
    def test_registered_name_resolved_with_url_and_kwargs(self, tmp_path):
        from otto.reservations import register_reservation_backend
        from otto.reservations.registry import RESERVATION_BACKENDS

        class FakeBackend:
            def __init__(self, *, api_key: str = "", url=None):
                self.api_key = api_key
                self.url = url

            def get_reserved_resources(self, username):
                return set()

            def who_reserved(self, resource):
                return []

            def backend_name(self):
                return "fake"

        register_reservation_backend("fake-test", FakeBackend)
        try:
            backend = build_backend(
                {
                    "backend": "fake-test",
                    "url": "https://api.example",
                    "fake-test": {"api_key": "secret"},
                },
                repo_dir=tmp_path,
            )
            assert isinstance(backend, FakeBackend)
            assert backend.api_key == "secret"
            assert backend.url == "https://api.example"
        finally:
            RESERVATION_BACKENDS.unregister("fake-test")

    def test_registered_name_without_url(self, tmp_path):
        from otto.reservations import register_reservation_backend
        from otto.reservations.registry import RESERVATION_BACKENDS

        class FakeBackend:
            def __init__(self, *, api_key: str = ""):
                self.api_key = api_key

            def get_reserved_resources(self, username):
                return set()

            def who_reserved(self, resource):
                return []

            def backend_name(self):
                return "fake"

        register_reservation_backend("fake-test-2", FakeBackend)
        try:
            backend = build_backend(
                {"backend": "fake-test-2", "fake-test-2": {"api_key": "secret"}},
                repo_dir=tmp_path,
            )
            assert isinstance(backend, FakeBackend)
            assert backend.api_key == "secret"
        finally:
            RESERVATION_BACKENDS.unregister("fake-test-2")

    def test_unknown_backend_name_raises(self, tmp_path):
        with pytest.raises(ValueError, match="Unknown reservation backend"):
            build_backend({"backend": "mystery"}, tmp_path)


class TestBuiltinBypassFix:
    def test_reregistering_none_takes_effect(self, tmp_path):
        """build_backend resolves "none" through the registry, not a hardcoded
        NullReservationBackend() construction — re-registering "none"
        (overwrite=True) must be honored.
        """
        from otto.reservations import register_reservation_backend

        class ReplacementNoneBackend(NullReservationBackend):
            pass

        register_reservation_backend("none", ReplacementNoneBackend, overwrite=True)
        try:
            backend = build_backend({"backend": "none"}, tmp_path)
            assert isinstance(backend, ReplacementNoneBackend)
        finally:
            register_reservation_backend("none", NullReservationBackend, overwrite=True)

    def test_reregistering_json_takes_effect(self, tmp_path):
        """Same bypass fix for the "json" built-in."""
        from otto.reservations import register_reservation_backend

        class ReplacementJsonBackend(JsonReservationBackend):
            pass

        register_reservation_backend("json", ReplacementJsonBackend, overwrite=True)
        try:
            f = _write_reservations(tmp_path)
            backend = build_backend(
                {"backend": "json", "json": {"path": str(f)}}, repo_dir=tmp_path
            )
            assert isinstance(backend, ReplacementJsonBackend)
        finally:
            register_reservation_backend("json", JsonReservationBackend, overwrite=True)
