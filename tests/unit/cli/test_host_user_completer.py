"""The --user completer: logins the typed host accepts for this verb, cache-then-live."""

from types import SimpleNamespace

import pytest

from otto.cli.completers import (
    HostGroupRequest,
    filter_logins,
    host_group_request,
    host_user_completer,
)

ENTRIES = [
    {"login": "root", "protocols": [], "proxy": True},
    {"login": "tel", "protocols": ["telnet"], "proxy": False},
    {"login": "u", "protocols": [], "proxy": False},
    {"login": "ssh_only", "protocols": ["ssh"], "proxy": False},
    {"login": "u", "protocols": ["telnet"], "proxy": False},
]
"""`u` appears twice — once unscoped, once telnet-scoped — a login scoped to
several protocols is still offered once; `--term ssh` keeps it via the
unscoped entry, `--term telnet` via both (deduped to one)."""


def _ctx(host_id="dut1", term=None):
    """A leaf context under the host group, as Typer builds it during completion."""
    root = SimpleNamespace(params={"labs": []}, parent=None)
    group = SimpleNamespace(params={"host_id": host_id, "term": term, "hop": ""}, parent=root)
    return SimpleNamespace(params={}, parent=group)


@pytest.mark.parametrize(
    ("flavour", "term", "incomplete", "expected"),
    [
        ("any", None, "", ["root", "ssh_only", "tel", "u"]),
        ("direct", None, "", ["ssh_only", "tel", "u"]),
        ("any", "ssh", "", ["root", "ssh_only", "u"]),
        ("any", "telnet", "", ["root", "tel", "u"]),
        ("direct", "ssh", "", ["ssh_only", "u"]),
        ("any", None, "r", ["root"]),
        ("any", None, "zz", []),
    ],
)
def test_filter_logins(flavour, term, incomplete, expected):
    assert filter_logins(ENTRIES, flavour=flavour, term=term, incomplete=incomplete) == expected


def test_host_group_request_walks_to_the_host_group():
    assert host_group_request(_ctx("dut2", "telnet")) == HostGroupRequest("dut2", "telnet")
    assert host_group_request(SimpleNamespace(params={}, parent=None)) == HostGroupRequest("", None)


def test_completer_prefers_the_cache(monkeypatch):
    import otto.config as cm

    monkeypatch.setattr(cm, "get_completion_names", lambda: {"logins_by_host": {"dut1": ENTRIES}})
    assert host_user_completer("direct")(_ctx(), "") == ["ssh_only", "tel", "u"]


def test_completer_offers_nothing_for_an_unknown_host(monkeypatch):
    import otto.config as cm

    monkeypatch.setattr(cm, "get_completion_names", lambda: {"logins_by_host": {"dut1": ENTRIES}})
    assert host_user_completer("any")(_ctx("ghost"), "") == []


def test_completer_falls_back_to_live(monkeypatch):
    import otto.config as cm
    import otto.config.completion_cache as cc

    monkeypatch.setattr(cm, "get_completion_names", lambda: None)
    monkeypatch.setattr(cm, "get_repos", list)
    monkeypatch.setattr(cc, "collect_logins_by_host", lambda repos: {"dut1": ENTRIES})
    assert host_user_completer("any")(_ctx(term="telnet"), "") == ["root", "tel", "u"]


def test_completer_declares_its_source():
    src = host_user_completer("direct").__completion_source__
    assert src == {
        "kind": "payload",
        "key": "logins_by_host",
        "host_scoped": True,
        "term_scoped": True,
        "flavour": "direct",
        "sort": True,
    }
