"""Unit tests for the reservation backend factory."""

import json
import sys
import types
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


class TestDottedPath:

    def test_dotted_path_resolved(self, tmp_path, monkeypatch):
        # Install a fake module on sys.modules that build_backend can import.
        fake = types.ModuleType("otto_fake_backend_mod")

        class FakeBackend:
            def __init__(self, *, api_key: str = "", url=None):
                self.api_key = api_key
                self.url = url

            def get_reserved_resources(self, username):
                return set()

            def who_reserved(self, resource):
                return None

            def backend_name(self):
                return "fake"

        fake.FakeBackend = FakeBackend  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "otto_fake_backend_mod", fake)

        backend = build_backend(
            {
                "backend": "otto_fake_backend_mod:FakeBackend",
                "url": "https://api.example",
                "otto_fake_backend_mod:FakeBackend": {"api_key": "secret"},
            },
            repo_dir=tmp_path,
        )
        assert isinstance(backend, FakeBackend)
        assert backend.api_key == "secret"
        assert backend.url == "https://api.example"

    def test_dotted_path_without_url(self, tmp_path, monkeypatch):
        fake = types.ModuleType("otto_fake_backend_mod2")

        class FakeBackend:
            def __init__(self, *, api_key: str = ""):
                self.api_key = api_key

            def get_reserved_resources(self, username):
                return set()

            def who_reserved(self, resource):
                return None

            def backend_name(self):
                return "fake"

        fake.FakeBackend = FakeBackend  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "otto_fake_backend_mod2", fake)

        backend = build_backend(
            {
                "backend": "otto_fake_backend_mod2:FakeBackend",
                "FakeBackend": {"api_key": "secret"},
            },
            repo_dir=tmp_path,
        )
        assert isinstance(backend, FakeBackend)
        assert backend.api_key == "secret"

    def test_invalid_backend_name_raises(self, tmp_path):
        with pytest.raises(ValueError, match="Unknown reservation backend"):
            build_backend({"backend": "mystery"}, tmp_path)

    def test_missing_module_raises(self, tmp_path):
        with pytest.raises(ValueError, match="Could not import"):
            build_backend({"backend": "nonexistent.module:Cls"}, tmp_path)

    def test_missing_class_raises(self, tmp_path, monkeypatch):
        fake = types.ModuleType("otto_fake_empty_mod")
        monkeypatch.setitem(sys.modules, "otto_fake_empty_mod", fake)
        with pytest.raises(ValueError, match="no attribute"):
            build_backend({"backend": "otto_fake_empty_mod:Missing"}, tmp_path)
