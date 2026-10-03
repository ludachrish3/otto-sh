"""``otto.session.build_lab`` — the lab the CLI builds, as a library call."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from otto.config.repo import Repo
from tests._fixtures.labdata import write_lab_json
from tests._fixtures.sutrepo import make_sut_repo

_SOURCES = '[[lab.sources]]\nbackend = "json"\npaths = ["lab"]\n'


def _host(element: str, ip: str) -> dict:
    return {
        "ip": ip,
        "element": element,
        "creds": [{"login": "u", "password": "p"}],
        "resources": [element],
        "labs": ["merged"],
    }


def _repo(root: Path, name: str, sources_toml: str, labfiles: dict[str, list[dict]]) -> Repo:
    sut = make_sut_repo(root, name=name, extra=sources_toml)
    for rel, hosts in labfiles.items():
        write_lab_json(root / rel, hosts)
    return Repo(sut)


# `OTTO_HOME` is relocated in the inventory tests below: `build_inventory` reads
# `~/.otto/settings.toml` on EVERY call, even when a repo declares its own
# table. The root autouse home is private per worker but shared across the
# tests on that worker, so a settings file another test leaves there could
# otherwise decide these.
# The broken-declaration test is anchored on the "inventory unavailable" banner:
# an unanchored match would also be satisfied by the "no inventory is
# configured" message the LOADER raises for a referenced entry, which is a
# different failure at a different layer.

_REFERENCED = {
    "inventory": "dut-1",
    "element": "dut",
    "creds": [{"login": "u", "password": "p"}],
    "labs": ["merged"],
    "resources": ["dut"],
}


def _inventory_repo(root: Path, table: str) -> Repo:
    """A repo whose one lab entry is REFERENCED, plus the given ``[inventory]`` table."""
    repo = _repo(root, "r1", f"{_SOURCES}\n{table}", {"lab/lab.json": [_REFERENCED]})
    (root / "inv.json").write_text(json.dumps({"dut-1": {"ip": "10.0.0.7"}}))
    return repo


def test_build_lab_resolves_referenced_entries(tmp_path, monkeypatch) -> None:
    """The inventory is built and handed to the load: a referenced entry has no address."""
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    from otto.session import build_lab

    repo = _inventory_repo(
        tmp_path / "r1", '[inventory]\nbackend = "json"\npath = "inv.json"\nsupplies = ["ip"]\n'
    )
    lab = build_lab([repo], ["merged"])
    assert lab.hosts["dut"].ip == "10.0.0.7"
    assert lab.hosts["dut"].inventory_ref.key == "dut-1"


def test_a_broken_inventory_declaration_is_an_inventory_refusal(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    from otto.session import LabBuildError, build_lab

    repo = _inventory_repo(tmp_path / "r1", '[inventory]\nbackend = "no-such-backend"\n')
    with pytest.raises(LabBuildError) as exc:
        build_lab([repo], ["merged"])
    assert exc.value.kind == "inventory"
    assert "inventory unavailable" in str(exc.value)


def test_no_labs_is_refused_before_any_repo_is_read():
    from otto.session import LabBuildError, build_lab

    with pytest.raises(LabBuildError) as exc:
        build_lab(None, [])  # ty: ignore[invalid-argument-type] — must not be touched
    assert (exc.value.kind, exc.value.field) == ("no_labs", "labs")


def test_an_unknown_lab_keeps_the_sources_own_text(tmp_path):
    from otto.labs import LabNotFoundError
    from otto.session import LabBuildError, build_lab

    repo = _repo(tmp_path / "r1", "r1", _SOURCES, {"lab/lab.json": [_host("alt1", "10.0.0.1")]})
    with pytest.raises(LabBuildError) as exc:
        build_lab([repo], ["nope"])
    assert (exc.value.kind, exc.value.field) == ("unknown_lab", "labs")
    assert isinstance(exc.value.__cause__, LabNotFoundError)
    assert str(exc.value) == str(exc.value.__cause__) == exc.value.detail


def test_an_unknown_backend_is_a_sources_refusal(tmp_path):
    from otto.session import LabBuildError, build_lab

    repo = _repo(tmp_path / "r1", "r1", '[[lab.sources]]\nbackend = "nope"\n', {})
    with pytest.raises(LabBuildError) as exc:
        build_lab([repo], ["merged"])
    assert (exc.value.kind, exc.value.field) == ("sources", None)
    assert str(exc.value).startswith("host source unavailable: ")


def test_preferences_merge_lists_atomically_and_tables_per_key():
    from otto.session import merge_host_preferences

    a = SimpleNamespace(host_preferences={"*": {"transfer": ["scp"], "ssh": {"port": 22, "x": 1}}})
    b = SimpleNamespace(host_preferences={"*": {"transfer": ["nc"], "ssh": {"port": 2222}}})
    assert merge_host_preferences([a, b]) == {
        "*": {"transfer": ["nc"], "ssh": {"port": 2222, "x": 1}}
    }


def test_a_later_repos_source_overrides_an_earlier_ones_host(tmp_path):
    from otto.session import build_lab

    r1 = _repo(tmp_path / "r1", "r1", _SOURCES, {"lab/lab.json": [_host("alt1", "10.0.0.1")]})
    r2 = _repo(tmp_path / "r2", "r2", _SOURCES, {"lab/lab.json": [_host("alt1", "10.0.0.2")]})
    lab = build_lab([r1, r2], ["merged"])
    assert lab.hosts["alt1"].ip == "10.0.0.2"


def test_declared_containers_are_registered(tmp_path, monkeypatch):
    from otto.session import build_lab

    calls = []
    monkeypatch.setattr(
        "otto.docker.compose.register_declared_container_hosts",
        lambda lab, repos: calls.append((lab, list(repos))) or 0,
    )
    repo = _repo(tmp_path / "r1", "r1", _SOURCES, {"lab/lab.json": [_host("alt1", "10.0.0.1")]})
    lab = build_lab([repo], ["merged"])
    assert calls == [(lab, [repo])]


def test_the_merged_host_preferences_reach_the_load(tmp_path):
    from otto.session import build_lab

    prefs = '[host_preferences.".*".ssh_options]\nport = 2222\n'
    repo = _repo(
        tmp_path / "r1", "r1", _SOURCES + prefs, {"lab/lab.json": [_host("alt1", "10.0.0.1")]}
    )
    lab = build_lab([repo], ["merged"])
    assert lab.hosts["alt1"].ssh_options.port == 2222
