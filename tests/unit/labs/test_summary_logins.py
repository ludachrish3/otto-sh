"""HostSummary carries each host's login identities so --user can complete without a host."""

from pathlib import Path
from types import SimpleNamespace

from otto.config import completion_cache as cc
from otto.host.element import Element
from otto.labs import HostSummary, LoginSummary
from tests._fixtures.labdata import json_lab_sources, write_lab_json

_CREDS = [
    {"login": "u", "password": "p"},
    {"login": "root", "password": "r", "proxy": "su"},
    {"login": "tel", "password": "t", "protocols": ["telnet"]},
]


def _repo(tmp_path: Path, hosts: list[dict]) -> SimpleNamespace:
    lab = tmp_path / "lab"
    lab.mkdir()
    write_lab_json(lab / "lab.json", hosts)
    return SimpleNamespace(
        lab_sources=json_lab_sources(tmp_path, [lab]),
        sut_dir=tmp_path,
        inventory_settings={},
        creds_settings={},
    )


def _summaries(repo) -> dict[str, HostSummary]:
    return {s.id: s for s in cc.repo_host_summaries(repo, cc.resolve_process_inventory([repo]))}


EXPECTED = [
    LoginSummary(login="u", protocols=[], proxy=False),
    LoginSummary(login="root", protocols=[], proxy=True),
    LoginSummary(login="tel", protocols=["telnet"], proxy=False),
]


def test_json_backend_records_login_identities_only(tmp_path):
    repo = _repo(tmp_path, [{"ip": "1.1.1.1", "element": "u1", "labs": ["e"], "creds": _CREDS}])
    assert _summaries(repo)["u1"].logins == EXPECTED


def test_no_secret_field_exists_on_a_login_summary():
    fields = set(LoginSummary.__dataclass_fields__)
    assert fields == {"login", "protocols", "proxy"}


def test_the_field_defaults_empty_for_backends_that_do_not_know():
    assert HostSummary(id="x").logins == []


def test_composite_carries_logins_across_sources(tmp_path):
    from otto.labs import build_lab_sources

    repo = _repo(tmp_path, [{"ip": "1.1.1.1", "element": "u1", "labs": ["e"], "creds": _CREDS}])
    composite = build_lab_sources([repo])
    got = {s.id: s.logins for s in composite.list_host_summaries()}
    assert got == {"u1": EXPECTED}


def test_load_lab_fallback_records_the_built_hosts_logins():
    from otto.config.lab import Lab
    from otto.host.factory import create_host_from_dict
    from otto.labs import host_summaries

    class _MinimalRepo:
        def load_lab(self, name, preferences=None, inventory=None):
            lab = Lab(name=name)
            lab.add_host(
                create_host_from_dict({"ip": "1.1.1.1", "creds": _CREDS}, element=Element("u1"))
            )
            return lab

        def list_labs(self):
            return ["only"]

    summaries = {s.id: s for s in host_summaries(_MinimalRepo())}
    assert summaries["u1"].logins == EXPECTED


def test_a_host_without_creds_summarises_to_no_logins():
    from otto.config.lab import Lab
    from otto.host.local_host import LocalHost
    from otto.labs import host_summaries

    class _LocalOnly:
        def load_lab(self, name, preferences=None, inventory=None):
            lab = Lab(name=name)
            lab.add_host(LocalHost())
            return lab

        def list_labs(self):
            return ["only"]

    assert {s.id: s.logins for s in host_summaries(_LocalOnly())} == {"local": []}


def test_logins_of_host_data_reads_only_identity_fields():
    from otto.labs import logins_of_host_data

    assert logins_of_host_data({"ip": "1.1.1.1", "creds": _CREDS}) == EXPECTED
    assert logins_of_host_data({"ip": "1.1.1.1"}) == []
    assert logins_of_host_data({"creds": [{"password": "no-login"}, "junk"]}) == []
