"""The cheap host enumeration must agree with full host construction, exactly.

``list_host_summaries()`` exists so completion and tunnel narrowing can name
hosts without building them. That is only safe while every id it offers is an
id ``load_lab()`` actually produces — an id that does not round-trip offers
the user a completion that cannot dispatch, which is worse than offering none.

These cases are not hypothetical: deriving ids by formatting the raw JSON
(the obvious cheap implementation) diverges on a float ``element_id``
(``router3.0`` vs ``router3``) and on any ``os_profile`` that defaults an
identity field (``r`` vs ``r_cpu0``), because the raw dict cannot see the
profile merge or pydantic's coercion.
"""

from pathlib import Path

import pytest

from otto.host.builtin_hosts import builtin_host_ids
from otto.host.os_profile import OS_PROFILES, register_os_profile
from otto.labs.json_repository import JsonFileLabRepository
from tests._fixtures.labdata import write_lab_json

_CREDS = [{"login": "u", "password": "p"}]

# Each entry pairs a raw host dict with the identity hazard it pins.
_HOSTS = [
    # plain
    {"ip": "10.0.0.1", "element": "router", "element_id": 1, "labs": ["e"], "creds": _CREDS},
    # element_id as a JSON float — formats as "3.0", coerces to 3
    {"ip": "10.0.0.3", "element": "router", "element_id": 3.0, "labs": ["e"], "creds": _CREDS},
    # element_id as a JSON string — coerces to int
    {"ip": "10.0.0.4", "element": "router", "element_id": "4", "labs": ["e"], "creds": _CREDS},
    # board + slot with punctuation/case, exercising slug()
    {
        "ip": "10.0.0.5",
        "element": "R",
        "board": "Line Card",
        "slot": 2,
        "labs": ["e"],
        "creds": _CREDS,
    },
    # uppercase, punctuated element
    {"ip": "10.0.0.6", "element": "Router One", "element_id": 1, "labs": ["w"], "creds": _CREDS},
    # a host in two labs at once
    {"ip": "10.0.0.7", "element": "shared", "labs": ["e", "w"], "creds": _CREDS},
]


@pytest.fixture
def profile_defaulting_identity():
    """An os_profile whose defaults supply board/slot — invisible to the raw dict."""
    register_os_profile(
        "_equiv_profile",
        "unix",
        defaults={"board": "CPU", "slot": 0, "docker_capable": True},
    )
    try:
        yield "_equiv_profile"
    finally:
        OS_PROFILES.unregister("_equiv_profile")


@pytest.fixture
def profile_defaulting_ip():
    """An os_profile that supplies the ip — invisible to the raw dict."""
    register_os_profile("_equiv_ip_profile", "unix", defaults={"ip": "10.7.7.7"})
    try:
        yield "_equiv_ip_profile"
    finally:
        OS_PROFILES.unregister("_equiv_ip_profile")


def _write_lab(tmp_path: Path, hosts: list[dict]) -> JsonFileLabRepository:
    """Write *hosts* (flat v1-style dicts) as a v2 document and open it."""
    write_lab_json(tmp_path / "lab.json", hosts)
    return JsonFileLabRepository(search_paths=[tmp_path])


def test_summary_ids_match_constructed_host_ids(tmp_path):
    """Every summarized id is an id load_lab() produces, for every lab."""
    repo = _write_lab(tmp_path, _HOSTS)

    summaries = repo.list_host_summaries()
    assert summaries, "positive control: the fixture must produce summaries"

    builtins = set(builtin_host_ids())
    for lab in repo.list_labs():
        constructed = set(repo.load_lab(lab).hosts) - builtins
        summarized = {s.id for s in summaries if lab in s.labs}
        assert summarized == constructed, f"lab {lab!r} diverged"


def test_float_element_id_matches_the_constructed_id(tmp_path):
    """A JSON float element_id must render as the coerced int, not '3.0'."""
    repo = _write_lab(tmp_path, [_HOSTS[1]])
    (summary,) = repo.list_host_summaries()
    assert summary.id == "router3"
    assert summary.id in repo.load_lab("e").hosts


def test_profile_defaulted_identity_fields_reach_the_summary(tmp_path, profile_defaulting_identity):
    """board/slot supplied by an os_profile still shape the id and docker flag."""
    host = {
        "ip": "10.0.0.9",
        "element": "r",
        "os_type": profile_defaulting_identity,
        "labs": ["e"],
        "creds": _CREDS,
    }
    repo = _write_lab(tmp_path, [host])

    (summary,) = repo.list_host_summaries()
    # Raw derivation would say "r" — the profile's board/slot are invisible there.
    assert summary.id == "r_cpu0"
    assert summary.id in repo.load_lab("e").hosts
    # docker_capable likewise comes from the merged spec, not the raw dict.
    assert summary.docker_capable is True


def test_multi_lab_host_merges_into_one_summary(tmp_path):
    """A host listed in two labs is one summary carrying both lab names."""
    repo = _write_lab(tmp_path, _HOSTS)
    shared = [s for s in repo.list_host_summaries() if s.id == "shared"]
    assert len(shared) == 1
    assert sorted(shared[0].labs) == ["e", "w"]


def test_duplicate_id_across_lab_files_keeps_the_first_record(tmp_path):
    """Two files whose records derive the same id merge labs, first fields win.

    Degenerate config, but pinned: ``load_lab`` also keeps-first for
    addressing, so enumeration must not silently prefer the later file.

    The two records are DISTINCT elements — ``('dup', 1)`` with board ``seed``
    and ``dup1`` with board ``seed``, both ``dup1_seed`` — because
    that is the only v2 route to one id from two files of one source: the same
    element in two files is a duplicate-element error before enumeration ever
    reaches the ids (spec §2.4).
    """
    a, b = tmp_path / "a", tmp_path / "b"
    for d in (a, b):
        d.mkdir()
    write_lab_json(
        a / "lab.json",
        [
            {
                "ip": "10.0.0.1",
                "element": "dup",
                "element_id": 1,
                "board": "seed",
                "labs": ["e"],
                "creds": _CREDS,
            }
        ],
    )
    write_lab_json(
        b / "lab.json",
        [{"ip": "10.0.0.2", "element": "dup1", "board": "seed", "labs": ["w"], "creds": _CREDS}],
    )
    repo = JsonFileLabRepository(search_paths=[a, b])

    (summary,) = repo.list_host_summaries()
    assert summary.id == "dup1_seed"
    assert summary.ip == "10.0.0.1", "first record wins"
    assert sorted(summary.labs) == ["e", "w"], "membership unions"


def test_ip_comes_from_the_validated_spec_not_the_raw_dict(tmp_path, profile_defaulting_ip):
    """A profile-supplied ip must reach the summary, like every other field."""
    host = {"element": "profiled", "os_type": profile_defaulting_ip, "labs": ["e"], "creds": _CREDS}
    repo = _write_lab(tmp_path, [host])

    (summary,) = repo.list_host_summaries()
    assert summary.ip == "10.7.7.7"
    assert summary.ip == repo.load_lab("e").hosts["profiled"].ip


def test_malformed_entries_are_skipped_not_raised(tmp_path):
    """Enumeration is best-effort: completion must never crash on bad data.

    A bad HOST entry, which is what stays per-record in v2: a malformed
    ELEMENT (or a non-object entry) fails ``ElementSpec`` at parse and takes
    its whole file out of the enumeration — see
    ``test_json_repository.py`` for that layer.
    """
    repo = _write_lab(
        tmp_path,
        [
            _HOSTS[0],
            {"element": "bad", "slot": "not-an-int", "labs": ["e"], "creds": _CREDS},
        ],
    )
    ids = [s.id for s in repo.list_host_summaries()]
    assert ids == ["router1"]
