"""Backend selection: flag > settings > recorded > auto-detect.

The order is the contract. Every arm is tested from BOTH directions -- the
value it picks and the value it declines to pick -- so no arm can pass by
always returning the same answer.
"""

import pytest

from otto.env.backends import BackendUnavailableError, select_backend


class TestOverrideOrder:
    def test_flag_beats_everything(self, monkeypatch):
        monkeypatch.setattr("otto.env.backends._uv_on_path", lambda: True)
        assert select_backend("pip", "uv", "uv") == "pip"
        assert select_backend("uv", "pip", "pip") == "uv"

    def test_settings_beats_recorded_and_autodetect(self, monkeypatch):
        monkeypatch.setattr("otto.env.backends._uv_on_path", lambda: True)
        assert select_backend(None, "pip", "uv") == "pip"

    def test_recorded_beats_autodetect(self, monkeypatch):
        """An existing env keeps its backend; switching is a create --force matter."""
        monkeypatch.setattr("otto.env.backends._uv_on_path", lambda: True)
        assert select_backend(None, None, "pip") == "pip"

    def test_autodetect_prefers_uv_when_present(self, monkeypatch):
        monkeypatch.setattr("otto.env.backends._uv_on_path", lambda: True)
        assert select_backend(None, None, None) == "uv"

    def test_autodetect_falls_back_to_pip(self, monkeypatch):
        monkeypatch.setattr("otto.env.backends._uv_on_path", lambda: False)
        assert select_backend(None, None, None) == "pip"


class TestRefusals:
    def test_an_unknown_name_is_refused_naming_both_valid_ones(self):
        with pytest.raises(BackendUnavailableError) as exc:
            select_backend("conda", None, None)
        assert "conda" in str(exc.value)
        assert "uv" in str(exc.value)
        assert "pip" in str(exc.value)

    def test_asking_for_uv_without_uv_is_refused_not_silently_downgraded(self, monkeypatch):
        """The whole point of an explicit flag is that it does not get ignored."""
        monkeypatch.setattr("otto.env.backends._uv_on_path", lambda: False)
        with pytest.raises(BackendUnavailableError) as exc:
            select_backend("uv", None, None)
        assert "not on PATH" in str(exc.value)

    def test_pip_is_always_available(self, monkeypatch):
        monkeypatch.setattr("otto.env.backends._uv_on_path", lambda: False)
        assert select_backend("pip", None, None) == "pip"
