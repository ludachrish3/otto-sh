"""Unit tests for the reservation backend factory."""

import json
from pathlib import Path

import pytest

from otto.reservations import (
    JsonReservationBackend,
    NullReservationBackend,
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
        assert backend.fetch_reservations("anyone") == []

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
            def __init__(self, *, api_key: str = "", url=None, repo_dir=None, username=None):
                self.api_key = api_key
                self.url = url
                self.repo_dir = repo_dir
                self.username = username

            def fetch_reservations(self, username, start=None, end=None):
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
            def __init__(self, *, api_key: str = "", repo_dir=None, username=None):
                self.api_key = api_key
                self.repo_dir = repo_dir
                self.username = username

            def fetch_reservations(self, username, start=None, end=None):
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

            def fetch_reservations(self, username, start=None, end=None):
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


class _Recorder(ReservationBackendBase):
    def fetch_reservations(self, username, start=None, end=None):
        return []

    def backend_name(self):
        return "recorder"


@pytest.fixture
def recorder_registered():
    from otto.reservations.registry import RESERVATION_BACKENDS

    register_reservation_backend("recorder-test", _Recorder, overwrite=True)
    try:
        yield
    finally:
        RESERVATION_BACKENDS.unregister("recorder-test")


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


def test_half_ported_backend_warns(tmp_path, recorder_registered, caplog):
    """who_reserved without holders is the signature of an unfinished port."""
    from otto.reservations.registry import RESERVATION_BACKENDS

    class HalfPorted(_Recorder):
        def who_reserved(self, resource):
            return []

    register_reservation_backend("half-ported", HalfPorted, overwrite=True)
    try:
        with caplog.at_level("WARNING"):
            build_backend({"backend": "half-ported"}, tmp_path, username="alice")
        assert "who_reserved" in caplog.text
        assert "holders" in caplog.text
    finally:
        RESERVATION_BACKENDS.unregister("half-ported")


def test_half_ported_backend_warns_only_once_per_process(tmp_path, recorder_registered, caplog):
    """One command builds the backend more than once; the user hears it once.

    ``build_reservation_gate`` builds one, and the completion cache's
    ``collect_reservation_usernames`` builds another on the same invocation, so
    a bare ``logger.warning`` says the same thing twice per gated command.

    Mutation: drop the ``if name in _warned_half_ported: return`` guard and
    the second build warns again, so ``count == 2``.
    """
    from otto.reservations.registry import RESERVATION_BACKENDS

    class HalfPortedTwice(_Recorder):
        def who_reserved(self, resource):
            return []

    register_reservation_backend("half-ported-twice", HalfPortedTwice, overwrite=True)
    try:
        with caplog.at_level("WARNING"):
            build_backend({"backend": "half-ported-twice"}, tmp_path, username="alice")
            build_backend({"backend": "half-ported-twice"}, tmp_path, username="alice")
        count = sum("who_reserved" in r.getMessage() for r in caplog.records)
        assert count == 1, caplog.text
    finally:
        RESERVATION_BACKENDS.unregister("half-ported-twice")


def test_half_ported_backend_never_warns_into_shell_completion(
    tmp_path, recorder_registered, caplog, monkeypatch
):
    """Completion has no log handler, so a warning here lands in the user's TAB.

    ``otto --holder <TAB>`` reaches ``build_backend`` through the completion
    cache's username collection. The root callback returns before logging is
    configured, so ``logging.lastResort`` prints any WARNING straight to
    stderr, corrupting the completion stream (spec §6.1, §8).

    The payload is suppressed, never the announcement: the second half of this
    test proves the backend is NOT recorded as warned, so the next real
    invocation still says it.

    Mutation: drop the ``is_completion_mode()`` early return and the first
    assertion fails with the warning present.
    """
    from otto.config.completion_cache import COMPLETION_ENV_VAR
    from otto.reservations.registry import RESERVATION_BACKENDS

    class HalfPortedCompleting(_Recorder):
        def who_reserved(self, resource):
            return []

    register_reservation_backend("half-ported-tab", HalfPortedCompleting, overwrite=True)
    monkeypatch.setenv(COMPLETION_ENV_VAR, "complete_bash")
    try:
        with caplog.at_level("WARNING"):
            build_backend({"backend": "half-ported-tab"}, tmp_path, username="alice")
        assert "who_reserved" not in caplog.text

        # The announcement survives: the next real invocation still warns.
        monkeypatch.delenv(COMPLETION_ENV_VAR)
        with caplog.at_level("WARNING"):
            build_backend({"backend": "half-ported-tab"}, tmp_path, username="alice")
        assert "who_reserved" in caplog.text
    finally:
        RESERVATION_BACKENDS.unregister("half-ported-tab")


class TestProtocolConformance:
    """`build_backend` refuses a constructed backend that does not satisfy
    the `ReservationBackend` protocol, rather than handing the caller an
    object that fails later with a bare `AttributeError`.
    """

    def test_structural_backend_missing_fetch_reservations_raises(self, tmp_path):
        from otto.reservations import ReservationBackendError, register_reservation_backend
        from otto.reservations.registry import RESERVATION_BACKENDS

        class StaleBackend:
            """An old-style backend that never migrated off the removed API."""

            def __init__(self, **_kwargs):
                pass

            def get_reserved_resources(self, username):
                return set()

            def who_reserved(self, resource):
                return []

            def backend_name(self):
                return "stale"

        register_reservation_backend("stale-test", StaleBackend)
        try:
            with pytest.raises(ReservationBackendError, match="get_reserved_resources"):
                build_backend({"backend": "stale-test"}, tmp_path, username="alice")
        finally:
            RESERVATION_BACKENDS.unregister("stale-test")

    def test_structural_backend_with_no_removed_methods_names_the_protocol(self, tmp_path):
        from otto.reservations import ReservationBackendError, register_reservation_backend
        from otto.reservations.registry import RESERVATION_BACKENDS

        class EmptyBackend:
            """Satisfies neither the old nor the new contract."""

            def __init__(self, **_kwargs):
                pass

            def backend_name(self):
                return "empty"

        register_reservation_backend("empty-test", EmptyBackend)
        try:
            with pytest.raises(ReservationBackendError, match="ReservationBackend protocol"):
                build_backend({"backend": "empty-test"}, tmp_path, username="alice")
        finally:
            RESERVATION_BACKENDS.unregister("empty-test")

    def test_half_abstract_subclass_typeerror_wrapped(self, tmp_path):
        """A subclass of ReservationBackendBase that implements neither
        abstract method fails inside ``cls(...)`` itself with a Python
        ``TypeError`` -- that must also be turned into a
        ``ReservationBackendError`` naming the migration doc, not a raw
        traceback.
        """
        from otto.reservations import ReservationBackendError, register_reservation_backend
        from otto.reservations.registry import RESERVATION_BACKENDS

        class UnfinishedPort(ReservationBackendBase):
            """Inherits the base but never implemented either abstract method."""

        register_reservation_backend("unfinished-test", UnfinishedPort)
        try:
            with pytest.raises(ReservationBackendError, match="could not be constructed"):
                build_backend({"backend": "unfinished-test"}, tmp_path, username="alice")
        finally:
            RESERVATION_BACKENDS.unregister("unfinished-test")

    def test_config_typo_typeerror_is_not_misdiagnosed_as_unfinished_port(self, tmp_path):
        """A TypeError from a fully-ported, non-abstract backend -- e.g. a
        typo'd key in ``[reservations.<name>]`` landing in ``**extra_kwargs``
        -- must NOT get the "unfinished port" headline: that class has no
        abstract methods left unimplemented, so the likely cause is a config
        mistake, not a stale contract.
        """
        from otto.reservations import ReservationBackendError, register_reservation_backend
        from otto.reservations.registry import RESERVATION_BACKENDS

        class FullyPortedBackend(_Recorder):
            """Fully satisfies the new contract; rejects an unknown kwarg."""

            def __init__(self, *, username=None):
                super().__init__(username=username)

        register_reservation_backend("typo-test", FullyPortedBackend)
        try:
            with pytest.raises(ReservationBackendError) as exc_info:
                build_backend(
                    {"backend": "typo-test", "typo-test": {"apikey": "secret"}},
                    tmp_path,
                    username="alice",
                )
            message = str(exc_info.value)
            assert "could not be constructed" in message
            assert "unfinished port" not in message
        finally:
            RESERVATION_BACKENDS.unregister("typo-test")
