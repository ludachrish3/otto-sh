from pathlib import Path

import pytest

from otto.configmodule.lab import Lab


def test_lab_default() -> None:

    lab = Lab("lab1")

    assert lab.name == "lab1"
    assert lab.resources == set()
    assert lab.hosts == dict()


def test_load_lab_forwards_preferences(monkeypatch):
    import otto.configmodule.lab as lab_mod
    from otto.configmodule.lab import Lab

    captured: dict[str, object] = {}

    class FakeRepo:
        def __init__(self, search_paths=None):
            pass

        def load_lab(self, name, preferences=None):
            captured["preferences"] = preferences
            return Lab(name=name)

    monkeypatch.setattr(lab_mod, "JsonFileLabRepository", FakeRepo)
    lab_mod.load_lab("x", [], preferences={".*": {"transfer": ["scp"]}})
    assert captured["preferences"] == {".*": {"transfer": ["scp"]}}
