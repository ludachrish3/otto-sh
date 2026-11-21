"""``secret_locations``: the one walk of a parsed document's secrets, and what wraps them."""

from typing import Any

from pydantic import SecretStr

from otto.models import base
from otto.models.base import secret_locations, wrap_secret_values

SENTINEL = "SENTINEL-p1-7f3a"


def _doc() -> dict:
    return {
        "elements": [
            {
                "name": "dut",
                "hosts": [
                    {"creds": [{"login": "a"}, {"login": "b", "password": SENTINEL}]},
                    {"ssh_options": {"passphrase": "p2"}, "password": None},
                ],
            }
        ],
        "password": {"password": "p3"},
        "passphrase": 7,
        "tail": ["password", [{"passphrase": ["p4"]}]],
    }


def test_every_non_null_value_under_a_secret_key_is_located_in_document_order():
    paths = [loc.path for loc in secret_locations(_doc())]
    assert paths == [
        "elements[0].hosts[0].creds[1].password",
        "elements[0].hosts[1].ssh_options.passphrase",
        "password",  # a table under a secret key is located, then walked into
        "password.password",
        "passphrase",  # a number
        "tail[1][0].passphrase",  # a list
    ]


def test_a_location_names_its_table_and_key_and_its_repr_never_shows_the_value():
    doc = _doc()
    first = next(secret_locations(doc))
    assert first.parent is doc["elements"][0]["hosts"][0]["creds"][1]
    assert first.key == "password"
    assert SENTINEL not in repr(first)
    assert "elements[0].hosts[0].creds[1].password" in repr(first)


def test_wrap_wraps_exactly_the_string_values():
    doc = _doc()
    wrap_secret_values(doc)
    host0, host1 = doc["elements"][0]["hosts"]
    assert host0["creds"][1]["password"] == SecretStr(SENTINEL)
    assert host1["ssh_options"]["passphrase"] == SecretStr("p2")
    assert host1["password"] is None
    assert doc["password"]["password"] == SecretStr("p3")
    assert doc["passphrase"] == 7  # not a string: left for the models to refuse
    assert doc["tail"][0] == "password"
    assert doc["tail"][1][0]["passphrase"] == ["p4"]
    assert host0["creds"][0] == {"login": "a"}
    again = _doc()
    wrap_secret_values(again)
    wrap_secret_values(again)  # a wrapped value is left as it is
    assert again == doc


def test_the_walk_builds_a_path_only_for_a_location_it_reports(monkeypatch):
    """Wrapping builds no path and no location, and locating builds one per secret.

    The walk allocates nothing per key or item: the path is the one per-secret
    cost, and wrapping does not pay it.
    """
    built: list[Any] = []
    real_path, real_location = base._path, base.SecretLocation
    monkeypatch.setattr(base, "_path", lambda slot: built.append("path") or real_path(slot))
    monkeypatch.setattr(
        base, "SecretLocation", lambda **kw: built.append("location") or real_location(**kw)
    )
    records = {
        f"h{i}": {"ip": "10.0.0.1", "tags": ["a", "b"], "extra": {"k": i}} for i in range(500)
    }
    records["h7"]["creds"] = [{"login": "root", "password": "pw"}]
    wrap_secret_values(records)
    assert built == []
    assert records["h7"]["creds"][0]["password"] == SecretStr("pw")
    records["h9"]["passphrase"] = "pp"
    assert [loc.path for loc in secret_locations(records)] == [
        "h7.creds[0].password",
        "h9.passphrase",
    ]
    assert built == ["path", "location", "path", "location"]


def test_a_document_deeper_than_the_recursion_limit_is_walked():
    depth = 5000
    doc: object = {"password": "deep"}
    for _ in range(depth):
        doc = [doc]
    [location] = secret_locations(doc)
    assert location.path == "[0]" * depth + ".password"
    wrap_secret_values(doc)
    assert isinstance(location.parent["password"], SecretStr)


def test_a_scalar_or_empty_document_has_no_secret():
    for doc in ("password", 3, None, [], {}, {"password": None}):
        assert list(secret_locations(doc)) == []
