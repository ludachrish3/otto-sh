"""[creds] compilation and construction (spec 2026-09-06 creds-store §4.1, §5.1)."""

import json

import pytest

from otto.creds import CredsConstructionError, CredsEnv, CredsError, JsonCredsStore
from otto.creds.config import (
    JsonCredsConfig,
    compile_creds,
    compile_creds_table,
    construct_creds_store,
)
from otto.creds.registry import register_creds_backend
from otto.models.base import OttoModel
from otto.models.settings import CredsConfigSpec


def test_json_requires_path_and_refuses_unknown_keys(tmp_path):
    with pytest.raises(
        CredsConstructionError, match=r"'json' \(.*configured in o\): parse failed: path"
    ):
        compile_creds(CredsConfigSpec(backend="json"), anchor_dir=tmp_path, origin="o")
    with pytest.raises(CredsConstructionError, match=r"parse failed: pathh: Extra inputs"):
        compile_creds(
            CredsConfigSpec(backend="json", path="c.json", pathh="x"),
            anchor_dir=tmp_path,
            origin="o",
        )


@pytest.mark.parametrize("path", ["", 3], ids=["empty", "non-str"])
def test_json_refuses_an_empty_or_non_string_path(tmp_path, path):
    """An empty or non-string ``path`` fails the store's config model, naming the field."""
    with pytest.raises(CredsConstructionError, match=r"parse failed: path"):
        compile_creds(CredsConfigSpec(backend="json", path=path), anchor_dir=tmp_path, origin="o")


def test_relative_path_anchors_to_the_declaring_dir_and_tilde_expands(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    out = compile_creds(
        CredsConfigSpec(backend="json", path="lab/c.json"), anchor_dir=tmp_path, origin="o"
    )
    assert out.prepared.config == JsonCredsConfig(path=tmp_path / "lab" / "c.json")
    home = compile_creds(
        CredsConfigSpec(backend="json", path="~/c.json"),
        anchor_dir=tmp_path / "elsewhere",
        origin="o",
    )
    assert home.prepared.normalized == {"path": str(tmp_path / "c.json")}


class _VaultConfig(OttoModel, frozen=True):
    url: str
    mount: str = "secret"


def test_another_backend_s_keys_are_parsed_by_its_own_model(tmp_path):
    register_creds_backend("vault-test", config=_VaultConfig, factory=_Vault)
    out = compile_creds(
        CredsConfigSpec(backend="vault-test", url="https://v", mount="lab"),
        anchor_dir=tmp_path,
        origin="o",
    )
    assert out.prepared.config == _VaultConfig(url="https://v", mount="lab")
    assert (out.anchor_dir, out.origin) == (tmp_path, "o")


def test_same_as_ignores_origin_and_anchor_but_not_kwargs(tmp_path):
    cfg = CredsConfigSpec(backend="json", path="c.json")
    a = compile_creds(cfg, anchor_dir=tmp_path, origin="r1")
    b = compile_creds(cfg, anchor_dir=tmp_path, origin="r2")
    c = compile_creds(cfg, anchor_dir=tmp_path / "elsewhere", origin="r3")
    assert a.same_as(b)
    assert not a.same_as(c)  # anchored to a different file


def test_compile_creds_table_validates_the_raw_table_naming_the_origin(tmp_path):
    out = compile_creds_table(
        {"backend": "json", "path": "c.json"}, anchor_dir=tmp_path, origin="o"
    )
    assert out.prepared.backend == "json"
    with pytest.raises(CredsError, match=r"o: \[creds\] [\s\S]*backend\n\s+Field required"):
        compile_creds_table({"path": "c.json"}, anchor_dir=tmp_path, origin="o")


def test_construct_builds_the_json_store_on_the_resolved_path_without_io(tmp_path):
    compiled = compile_creds(
        CredsConfigSpec(backend="json", path="./nested/../creds.json"),
        anchor_dir=tmp_path,
        origin="o",
    )
    store = construct_creds_store(compiled)  # no file exists; no raise
    assert isinstance(store, JsonCredsStore)
    assert store.path == (tmp_path / "creds.json").resolve()
    (tmp_path / "creds.json").write_text(json.dumps({"k": [{"login": "u"}]}))
    assert [c.login for c in store.lookup("k")] == ["u"]


def test_unknown_backend_names_the_origin(tmp_path):
    with pytest.raises(
        CredsConstructionError, match=r"'nope' \(not registered; configured in o\): lookup failed"
    ):
        compile_creds(CredsConfigSpec(backend="nope"), anchor_dir=tmp_path, origin="o")


class _Vault:
    def __init__(self, c) -> None:
        self.env = c.env
        self.label = f"vault:{c.config.url}"

    def lookup(self, key):
        return []

    def list_keys(self):
        return None

    def fingerprint(self):
        return None


def test_a_third_party_store_gets_its_config_and_env_and_bad_keys_name_both(tmp_path):
    register_creds_backend("vault-test", config=_VaultConfig, factory=_Vault)
    store = construct_creds_store(
        compile_creds(
            CredsConfigSpec(backend="vault-test", url="https://v"), anchor_dir=tmp_path, origin="o"
        )
    )
    assert store.env == CredsEnv(tmp_path, "o")
    assert store.label == "vault:https://v"
    with pytest.raises(
        CredsConstructionError,
        match=r"'vault-test' \(registered by .*; configured in o\): parse failed: .*urll",
    ):
        compile_creds(
            CredsConfigSpec(backend="vault-test", urll="x"), anchor_dir=tmp_path, origin="o"
        )
