"""[creds] compilation and construction (spec 2026-09-06 creds-store §4.1, §5.1)."""

import json
from pathlib import Path

import pytest

from otto.creds import CredsError, JsonCredsStore
from otto.creds.config import (
    CompiledCreds,
    compile_creds,
    compile_creds_table,
    construct_creds_store,
)
from otto.creds.registry import CREDS_BACKENDS, register_creds_backend
from otto.models.settings import CredsConfigSpec


def test_json_requires_path_and_refuses_unknown_keys(tmp_path):
    with pytest.raises(CredsError, match=r"o: \[creds\] backend 'json' requires a 'path' string"):
        compile_creds(CredsConfigSpec(backend="json"), anchor_dir=tmp_path, origin="o")
    with pytest.raises(
        CredsError, match=r"o: \[creds\] unknown key\(s\) for the json backend: \['pathh'\]"
    ):
        compile_creds(
            CredsConfigSpec(backend="json", path="c.json", pathh="x"),
            anchor_dir=tmp_path,
            origin="o",
        )


def test_relative_path_anchors_to_the_declaring_dir_and_tilde_expands(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    out = compile_creds(
        CredsConfigSpec(backend="json", path="lab/c.json"), anchor_dir=tmp_path, origin="o"
    )
    assert out.kwargs == {"path": tmp_path / "lab" / "c.json"}
    home = compile_creds(
        CredsConfigSpec(backend="json", path="~/c.json"),
        anchor_dir=tmp_path / "elsewhere",
        origin="o",
    )
    assert home.kwargs == {"path": tmp_path / "c.json"}


def test_other_backends_keep_their_kwargs_verbatim(tmp_path):
    out = compile_creds(
        CredsConfigSpec(backend="vault", url="https://v", mount="lab"),
        anchor_dir=tmp_path,
        origin="o",
    )
    assert out == CompiledCreds(
        backend="vault",
        kwargs={"url": "https://v", "mount": "lab"},
        anchor_dir=tmp_path,
        origin="o",
    )


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
    assert out.backend == "json"
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
    with pytest.raises(CredsError, match=r"o: Unknown creds backend 'nope'"):
        construct_creds_store(
            CompiledCreds(backend="nope", kwargs={}, anchor_dir=tmp_path, origin="o")
        )


class _Vault:
    def __init__(self, repo_dir: Path, *, url: str) -> None:
        self.repo_dir = repo_dir
        self.label = f"vault:{url}"

    def lookup(self, key):
        return []

    def list_keys(self):
        return None

    def fingerprint(self):
        return None


def test_a_third_party_store_gets_repo_dir_plus_its_kwargs_and_bad_kwargs_name_both(tmp_path):
    register_creds_backend("vault-test", _Vault)
    try:
        store = construct_creds_store(
            CompiledCreds(
                backend="vault-test", kwargs={"url": "https://v"}, anchor_dir=tmp_path, origin="o"
            )
        )
        assert store.repo_dir == tmp_path
        assert store.label == "vault:https://v"
        with pytest.raises(
            CredsError,
            match=r"o: \[creds\] backend 'vault-test': .*unexpected keyword argument 'urll'",
        ):
            construct_creds_store(
                CompiledCreds(
                    backend="vault-test", kwargs={"urll": "x"}, anchor_dir=tmp_path, origin="o"
                )
            )
    finally:
        CREDS_BACKENDS.unregister("vault-test")
