"""The cheap host enumeration must agree with full host construction, exactly.

``list_host_summaries()`` exists so completion and tunnel narrowing can name
hosts without building them. That is only safe while every id it offers is an
id ``load_lab()`` actually produces — an id that does not round-trip offers
the user a completion that cannot dispatch, which is worse than offering none.

These cases are not hypothetical: deriving ids by formatting the raw JSON
(the obvious cheap implementation) diverges on any ``os_profile`` that
defaults an identity field (``r`` vs ``r-cpu-0``) and on any element or board
the author spelled with case or punctuation (``Line Card`` vs ``line-card``),
because the raw dict cannot see the profile merge or the slug rule.
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
    {"ip": "10.0.0.1", "element": "router", "labs": ["e"], "creds": _CREDS},
    # a second host of the SAME element, told apart by board + slot
    {
        "ip": "10.0.0.3",
        "element": "router",
        "board": "io",
        "slot": 1,
        "labs": ["e"],
        "creds": _CREDS,
    },
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
    {"ip": "10.0.0.6", "element": "Router One", "labs": ["w"], "creds": _CREDS},
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
    assert summary.id == "r-cpu-0"
    assert summary.id in repo.load_lab("e").hosts
    # docker_capable likewise comes from the merged spec, not the raw dict.
    assert summary.docker_capable is True


def test_multi_lab_host_merges_into_one_summary(tmp_path):
    """A host listed in two labs is one summary carrying both lab names."""
    repo = _write_lab(tmp_path, _HOSTS)
    shared = [s for s in repo.list_host_summaries() if s.id == "shared"]
    assert len(shared) == 1
    assert sorted(shared[0].labs) == ["e", "w"]


def test_two_records_deriving_one_id_keep_the_first_with_its_whole_membership(tmp_path):
    """Two hosts of ONE element whose boards slug alike derive one id.

    Degenerate config (``load_lab`` refuses it as a host-id collision), but
    pinned: enumeration must keep the first record and its whole membership
    rather than silently prefer the later one. (Two DIFFERENT elements can
    also compose one id since every seam is ``-``; see
    ``test_two_elements_composing_one_id_record_a_drop_naming_both`` and
    ``test_two_elements_at_one_address_are_one_machine_in_both_labs``.)

    The ``ip`` is the half that discriminates keep-first from keep-last. The
    membership assertions pin that keeping the first record keeps the WHOLE
    membership: both records are the one element's, so the kept summary
    already carries every lab either of them could contribute.
    """
    repo = _write_lab(
        tmp_path,
        [
            {
                "ip": "10.0.0.1",
                "element": "dup",
                "board": "seed",
                "labs": ["e", "w"],
                "creds": _CREDS,
            },
            {
                "ip": "10.0.0.2",
                "element": "dup",
                "board": "SEED",
                "labs": ["e", "w"],
                "creds": _CREDS,
            },
        ],
    )

    (summary,) = repo.list_host_summaries()
    assert summary.id == "dup-seed", "both boards slug to 'seed'"
    assert summary.ip == "10.0.0.1", "first record wins"
    assert sorted(summary.labs) == ["e", "w"], "keeping the first keeps the whole membership"
    assert sorted(summary.lab_patterns) == ["e", "w"]


def _two_file_repo(tmp_path, first: dict, second: dict) -> JsonFileLabRepository:
    """One source holding two lab files: *first* in ``a/``, *second* in ``b/``."""
    a, b = tmp_path / "a", tmp_path / "b"
    for d in (a, b):
        d.mkdir()
    write_lab_json(a / "lab.json", [first])
    write_lab_json(b / "lab.json", [second])
    return JsonFileLabRepository(search_paths=[a, b])


_BARE_X_CPU_1 = {"ip": "10.0.0.1", "element": "x-cpu-1", "creds": _CREDS}
_X_CPU_SLOT_1 = {
    "ip": "10.0.0.2",
    "element": "x",
    "board": "cpu",
    "slot": 1,
    "creds": _CREDS,
}
_BARE_DESCRIBED = "element 'x-cpu-1' (10.0.0.1)"
_BOARDED_DESCRIBED = "element 'x' board 'cpu' slot 1 (10.0.0.2)"


@pytest.mark.parametrize(
    ("first", "second", "kept_described", "dropped_described"),
    [
        pytest.param(
            _BARE_X_CPU_1, _X_CPU_SLOT_1, _BARE_DESCRIBED, _BOARDED_DESCRIBED, id="bare-kept"
        ),
        pytest.param(
            _X_CPU_SLOT_1, _BARE_X_CPU_1, _BOARDED_DESCRIBED, _BARE_DESCRIBED, id="boarded-kept"
        ),
    ],
)
def test_two_elements_composing_one_id_record_a_drop_naming_both(
    tmp_path, first, second, kept_described, dropped_described
):
    """Elements ``x-cpu-1`` and ``x`` + board ``cpu`` slot 1 compose ONE id; the loser is SAID.

    Every portion of an id joins with ``-`` (spec 2026-10-03 §3), so two
    different elements can land on one id. Across LABS that is not a refusal
    (``load_lab`` of each lab succeeds), but the summary list carries one
    record per id: the first wins, and the other host — a different address —
    must not vanish silently (see otto.labs.drops). The drop names BOTH
    declarations by the fields that tell them apart (element, board, slot,
    ip), as ``Lab.add_host``'s refusal does. Run in both file orders so the
    KEPT side's board and slot are asserted too, not only the dropped side's.
    """
    from otto.labs.drops import collecting_drops

    repo = _two_file_repo(tmp_path, {**first, "labs": ["e"]}, {**second, "labs": ["w"]})
    assert "x-cpu-1" in repo.load_lab("e").hosts
    assert "x-cpu-1" in repo.load_lab("w").hosts

    with collecting_drops() as drops:
        (summary,) = repo.list_host_summaries()

    assert (summary.id, summary.ip, summary.labs) == ("x-cpu-1", first["ip"], ["e"])
    (drop,) = drops
    assert drop.where == f"{tmp_path / 'b' / 'lab.json'}: element {second['element']!r} hosts[0]"
    assert "host id 'x-cpu-1'" in drop.reason, "names the id"
    assert f"{kept_described} collides with {dropped_described}" in drop.reason, (
        "names the kept declaration, then the dropped one, each with its board, slot and address"
    )


def test_two_elements_at_one_address_are_one_machine_in_both_labs(tmp_path):
    """``a-b`` and ``a`` + board ``b`` at ONE address are the same machine.

    Same id + same ip is the rule ``Lab.__add__`` dedups on: one host. The
    second record's lab membership folds into the kept summary (order
    preserved) and nothing is dropped — lab ``w``'s membership must not vanish.
    """
    from otto.labs.drops import collecting_drops

    repo = _two_file_repo(
        tmp_path,
        {"ip": "10.0.0.1", "element": "a-b", "labs": ["e"], "creds": _CREDS},
        {"ip": "10.0.0.1", "element": "a", "board": "b", "labs": ["w"], "creds": _CREDS},
    )

    with collecting_drops() as drops:
        (summary,) = repo.list_host_summaries()

    assert (summary.id, summary.ip, summary.labs) == ("a-b", "10.0.0.1", ["e", "w"])
    assert summary.lab_patterns == ["e", "w"]
    assert drops == []


def test_a_same_lab_kebab_pair_at_one_address_folds_in_completion_but_is_refused_at_load(
    tmp_path,
):
    """The one shape where completion offers an id the lab will not load.

    ``a-b`` and ``a`` + board ``b`` at ONE address in ONE lab: the summary
    folds them (same id, same machine) while ``load_lab`` refuses the lab,
    naming both declarations. The refusal says ``in lab`` once — the loader's
    file/element/index prefix does not repeat what the refusal already says.
    """
    from otto.labs.errors import LabRepositoryError

    repo = _write_lab(
        tmp_path,
        [
            {"ip": "10.0.0.1", "element": "a-b", "labs": ["e"], "creds": _CREDS},
            {"ip": "10.0.0.1", "element": "a", "board": "b", "labs": ["e"], "creds": _CREDS},
        ],
    )

    assert [s.id for s in repo.list_host_summaries()] == ["a-b"]

    with pytest.raises(LabRepositoryError) as excinfo:
        repo.load_lab("e")
    message = str(excinfo.value)
    assert "element 'a' hosts[0]: host id 'a-b' in lab 'e':" in message
    assert message.count("in lab 'e'") == 1
    assert "element 'a-b' (10.0.0.1) collides with element 'a' board 'b' (10.0.0.1)" in message


def test_a_restated_host_with_the_same_address_folds_without_a_drop(tmp_path):
    """The same host written twice (same id, same address) is one host, not a collision.

    A second file restating an element is a duplicate-element error, so one
    element listing the host twice is the same-element route to an id met twice
    at one address (two ELEMENTS at one address: see
    ``test_two_elements_at_one_address_are_one_machine_in_both_labs``); the
    kept record carries the element's whole membership.
    """
    from otto.labs.drops import collecting_drops

    host = {
        "ip": "10.0.0.1",
        "element": "dup",
        "board": "seed",
        "labs": ["e", "w"],
        "creds": _CREDS,
    }
    repo = _write_lab(tmp_path, [host, dict(host)])

    with collecting_drops() as drops:
        (summary,) = repo.list_host_summaries()

    assert (summary.id, summary.ip, sorted(summary.labs)) == ("dup-seed", "10.0.0.1", ["e", "w"])
    assert drops == []


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
    assert ids == ["router"]


def test_an_element_the_runtime_refuses_is_dropped_and_said_not_raised(tmp_path, monkeypatch):
    """Enumeration feeds completion: a bad element is skipped and recorded, never raised.

    ``ElementSpec`` refuses today everything ``Element.__post_init__`` refuses,
    so no lab file reaches this branch — the hostile condition is injected by
    making the runtime object refuse one element the file layer accepted. The
    other element's host must still enumerate, and the drop must name the file
    and the element so the omission is visible (see otto.labs.drops).
    """
    from otto.labs.drops import collecting_drops
    from otto.models.lab import ElementSpec

    hosts = [
        {"ip": "10.0.0.1", "element": "bad", "labs": ["e"], "creds": _CREDS},
        {"ip": "10.0.0.2", "element": "good", "labs": ["e"], "creds": _CREDS},
    ]
    repo = _write_lab(tmp_path, hosts)
    assert sorted(s.id for s in repo.list_host_summaries()) == ["bad", "good"], "positive control"

    original = ElementSpec.to_element

    def _refuse_bad(self):
        if self.name == "bad":
            raise ValueError("injected: the runtime object refused this element")
        return original(self)

    monkeypatch.setattr(ElementSpec, "to_element", _refuse_bad)

    with collecting_drops() as drops:
        ids = [s.id for s in repo.list_host_summaries()]

    assert ids == ["good"]
    (drop,) = drops
    assert drop.where == f"{tmp_path / 'lab.json'}: element 'bad'"
    assert "injected" in drop.reason
