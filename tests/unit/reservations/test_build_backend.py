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

    def test_tilde_path_expands_via_home(self, tmp_path, monkeypatch):
        """A ``~``-prefixed path expands against ``HOME`` before repo-anchoring
        (path-resolution convention, docs/guide/configuration/settings.md).

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
        assert backend.get_reserved_resources("anyone") == set()

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
            def __init__(self, *, api_key: str = "", url=None, repo_dir=None):
                self.api_key = api_key
                self.url = url
                self.repo_dir = repo_dir

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
            assert backend.repo_dir == tmp_path
        finally:
            RESERVATION_BACKENDS.unregister("fake-test")

    def test_registered_name_without_url(self, tmp_path):
        from otto.reservations import register_reservation_backend
        from otto.reservations.registry import RESERVATION_BACKENDS

        class FakeBackend:
            def __init__(self, *, api_key: str = "", repo_dir=None):
                self.api_key = api_key
                self.repo_dir = repo_dir

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
            assert backend.repo_dir == tmp_path
        finally:
            RESERVATION_BACKENDS.unregister("fake-test-2")

    def test_unknown_backend_name_raises(self, tmp_path):
        with pytest.raises(ValueError, match="Unknown reservation backend"):
            build_backend({"backend": "mystery"}, tmp_path)


class TestCustomBackendRepoDir:
    def test_custom_backend_receives_repo_dir(self, tmp_path):
        """Custom reservation backends get repo_dir, like custom lab backends."""
        from otto.reservations import build_backend, register_reservation_backend
        from otto.reservations.registry import RESERVATION_BACKENDS

        seen: dict[str, object] = {}

        class RecordingBackend:
            def __init__(self, **kwargs):
                seen.update(kwargs)

            def get_reserved_resources(self, username):
                return set()

            def who_reserved(self, resource):
                return []

            def backend_name(self):
                return "recording"

        register_reservation_backend("recording-test", RecordingBackend)
        try:
            build_backend({"backend": "recording-test"}, tmp_path)
        finally:
            RESERVATION_BACKENDS.unregister("recording-test")

        assert seen["repo_dir"] == tmp_path


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
