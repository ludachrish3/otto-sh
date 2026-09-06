"""Link endpoints must name hosts by the id ``lab.hosts`` is actually keyed by.

``addressing_from_dict`` builds the map declared links resolve against. It
derived ids by formatting the raw record, while the hosts themselves are keyed
by the validated, profile-merged id — two id systems over one map. The two
agree for simple records, which is why this survived, and diverge exactly where
host ids diverge:

- an ``os_profile`` that defaults ``board``/``slot`` makes the real id
  ``dut_lc2`` while the raw derivation says ``dut``. A link authored against
  the id otto advertises then fails to resolve and **the whole lab fails to
  load**; authored the raw way, the link's endpoint names a host that is not
  in ``lab.hosts`` — a phantom edge — unless the load refuses it.
"""

import pytest

from otto.host.element import Element
from otto.host.factory import host_identity
from otto.host.os_profile import OS_PROFILES, register_os_profile
from otto.labs import LabRepositoryError
from otto.labs.json_repository import JsonFileLabRepository
from otto.link.derive import addressing_from_dict
from tests._fixtures.labdata import write_lab_json

_CREDS = [{"login": "u", "password": "p"}]


@pytest.fixture
def carded_profile():
    """A profile supplying board/slot — identity fields absent from the record."""
    register_os_profile("_carded", "unix", defaults={"board": "lc", "slot": 2})
    try:
        yield "_carded"
    finally:
        OS_PROFILES.unregister("_carded")


def test_addressing_id_matches_host_identity_under_a_profile(carded_profile):
    """The map's key is the id the host will report, not a raw rendering."""
    host = {
        "ip": "10.0.0.1",
        "os_type": carded_profile,
        "creds": _CREDS,
    }

    host_id, _addressing = addressing_from_dict(host, Element("dut"))

    assert host_id == host_identity(host, Element("dut")).id
    assert host_id == "dut_lc2", "raw derivation would have said 'dut'"


def test_a_link_authored_against_the_raw_id_fails_loudly(tmp_path, carded_profile):
    """The one behaviour that REGRESSES — deliberately, and with a hint.

    A profile-defaulted board/slot is invisible in the raw record, so a link
    authored against ``dut`` resolved... to an endpoint naming a host absent
    from lab.hosts (which is keyed ``dut_lc2``). Every consumer mapping
    link.a.host to a host silently missed. It now fails the load instead,
    naming the near miss so the fix is obvious rather than archaeological.
    """
    write_lab_json(
        tmp_path / "lab.json",
        [
            {
                "ip": "10.0.0.3",
                "element": "dut",
                "os_type": carded_profile,
                "labs": ["e"],
                "creds": _CREDS,
            },
            {"ip": "10.0.0.2", "element": "srv", "labs": ["e"], "creds": _CREDS},
        ],
        [{"endpoints": [{"host": "dut"}, {"host": "srv"}]}],
    )
    repo = JsonFileLabRepository(search_paths=[tmp_path])

    with pytest.raises(LabRepositoryError) as excinfo:
        repo.load_lab("e")

    message = str(excinfo.value)
    assert "unknown host 'dut'" in message
    assert "did you mean 'dut_lc2'" in message, "the near-miss hint is the whole point"


def test_a_record_naming_an_unregistered_profile_is_skipped_not_fatal(tmp_path):
    """A record this process cannot resolve must not deny the rest of the lab.

    Exactly the shape of a lab file whose hosts use a profile or command frame
    registered by a SUT repo's init modules — absent in any other process.
    """
    write_lab_json(
        tmp_path / "lab.json",
        [
            {"ip": "10.0.0.1", "element": "fine", "labs": ["e"], "creds": _CREDS},
            {"ip": "10.0.0.2", "element": "srv", "labs": ["e"], "creds": _CREDS},
            {
                "ip": "10.0.0.9",
                "element": "exotic",
                "os_type": "_not_a_registered_profile",
                "labs": ["other"],
                "creds": _CREDS,
            },
        ],
        [{"endpoints": [{"host": "fine"}, {"host": "srv"}]}],
    )
    repo = JsonFileLabRepository(search_paths=[tmp_path])

    lab = repo.load_lab("e")

    assert sorted(x.id for x in lab.links) == ["fine--srv"]


def test_a_lab_declaring_a_link_by_the_advertised_id_loads(tmp_path, carded_profile):
    """The bug in one shot: this lab used to fail to load entirely.

    The link names ``dut_lc2`` — the id ``otto host <TAB>`` offers and
    ``lab.hosts`` is keyed by — but the endpoint map was keyed by the raw
    ``dut``, so resolution raised LabRepositoryError and the lab was
    unusable.
    """
    write_lab_json(
        tmp_path / "lab.json",
        [
            {
                "ip": "10.0.0.1",
                "element": "dut",
                "os_type": carded_profile,
                "labs": ["e"],
                "creds": _CREDS,
            },
            {"ip": "10.0.0.2", "element": "srv", "labs": ["e"], "creds": _CREDS},
        ],
        [{"endpoints": [{"host": "dut_lc2"}, {"host": "srv"}]}],
    )
    repo = JsonFileLabRepository(search_paths=[tmp_path])

    lab = repo.load_lab("e")

    assert "dut_lc2" in lab.hosts, "positive control: the profile shapes the host id"
    (link,) = [x for x in lab.links if x.id == "dut_lc2--srv"]
    # Every endpoint must name a host that actually exists in the lab.
    assert link.a.host in lab.hosts
    assert link.b.host in lab.hosts
