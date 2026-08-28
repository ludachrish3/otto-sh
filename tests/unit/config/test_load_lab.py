"""Unit tests for config.load_lab repository routing."""

import json
from pathlib import Path

import pytest

from otto.config.lab import Lab, load_lab
from otto.labs.json_repository import JsonFileLabRepository
from tests._fixtures.labdata import lab_data_dir, write_lab_json


def _hosts_file(path: Path, hosts: list[dict]) -> Path:
    """Write *hosts* (flat v1-style dicts) as a v2 ``lab.json`` under *path*."""
    return write_lab_json(path / "lab.json", hosts)


def test_load_lab_default_repository_uses_search_paths(tmp_path):
    """With no repository given, load_lab builds a json backend over search_paths."""
    _hosts_file(
        tmp_path,
        [
            {
                "ip": "10.10.200.11",
                "element": "alt1",
                "creds": [{"login": "vagrant", "password": "vagrant"}],
                "resources": ["alt1"],
                "labs": ["testlab"],
            },
        ],
    )
    lab = load_lab("testlab", search_paths=[tmp_path])
    assert isinstance(lab, Lab)
    assert lab.name == "testlab"
    # `local` is the built-in host load_lab injects into every lab.
    assert set(lab.hosts) == {"alt1", "local"}


def test_load_lab_uses_injected_repository(tmp_path):
    """A passed repository is used instead of the default json backend."""
    _hosts_file(
        tmp_path,
        [
            {
                "ip": "10.10.200.11",
                "element": "alt1",
                "creds": [{"login": "vagrant", "password": "vagrant"}],
                "resources": ["alt1"],
                "labs": ["injected"],
            },
        ],
    )
    repo = JsonFileLabRepository([tmp_path])
    lab = load_lab("injected", repository=repo)
    assert lab.name == "injected"
    assert set(lab.hosts) == {"alt1", "local"}  # `local` = built-in injection


def test_missing_search_path_still_names_the_paths_it_searched(tmp_path):
    """The composite keeps each source's not-found reason, not just its own verdict.

    R15 routes EVERY load through the composite, so the json backend's "no lab
    data found in any search path: ..." — the only message that names the paths
    actually consulted — reaches the user solely because the composite carries
    it forward. Without that, a typo'd ``paths`` entry (the commonest first-run
    mistake) is answered with "add a 'labs' table", pointing at a file otto
    never found.
    """
    from otto.labs.errors import LabNotFoundError

    with pytest.raises(LabNotFoundError) as err:
        load_lab("unix", search_paths=[tmp_path / "nowhere"])

    message = str(err.value)
    assert "not declared" in message  # the composite's own verdict survives
    assert "nowhere" in message, f"the searched path is missing from: {message}"


def test_load_lab_merges_multiple_names(tmp_path):
    """`+`-joined names merge into one lab."""
    _hosts_file(
        tmp_path,
        [
            {
                "ip": "10.10.200.11",
                "element": "alt1",
                "creds": [{"login": "vagrant", "password": "vagrant"}],
                "resources": ["alt1"],
                "labs": ["lab_a"],
            },
            {
                "ip": "10.10.200.12",
                "element": "test2",
                "creds": [{"login": "vagrant", "password": "vagrant"}],
                "resources": ["test2"],
                "labs": ["lab_b"],
            },
        ],
    )
    repo = JsonFileLabRepository([tmp_path])
    lab = load_lab("lab_a+lab_b", repository=repo)
    assert set(lab.hosts) == {"alt1", "test2", "local"}  # `local` = built-in injection


def test_load_lab_comma_is_not_a_separator(tmp_path):
    """The comma is an ordinary character: `lab_a,lab_b` is one (unknown) lab name.

    Both `lab_a` and `lab_b` are registered so this test genuinely discriminates:
    under the old comma-splitting behavior, `load_lab("lab_a,lab_b")` would
    SUCCEED and merge both labs (since both exist); under the current `+`-only
    grammar, `lab_a,lab_b` is a single unknown lab name and this must raise.
    """
    from otto.labs.errors import LabNotFoundError

    _hosts_file(
        tmp_path,
        [
            {
                "ip": "10.10.200.11",
                "element": "alt1",
                "creds": [{"login": "vagrant", "password": "vagrant"}],
                "resources": ["alt1"],
                "labs": ["lab_a"],
            },
            {
                "ip": "10.10.200.12",
                "element": "test2",
                "creds": [{"login": "vagrant", "password": "vagrant"}],
                "resources": ["test2"],
                "labs": ["lab_b"],
            },
        ],
    )
    repo = JsonFileLabRepository([tmp_path])

    with pytest.raises(LabNotFoundError):
        load_lab("lab_a,lab_b", repository=repo)


def test_merged_lab_name_uses_the_plus_separator(tmp_path):
    """A merged lab's name is written in the same grammar that selects it."""
    _hosts_file(
        tmp_path,
        [
            {
                "ip": "10.10.200.11",
                "element": "alt1",
                "creds": [{"login": "vagrant", "password": "vagrant"}],
                "resources": ["alt1"],
                "labs": ["lab_a"],
            },
            {
                "ip": "10.10.200.12",
                "element": "test2",
                "creds": [{"login": "vagrant", "password": "vagrant"}],
                "resources": ["test2"],
                "labs": ["lab_b"],
            },
        ],
    )
    repo = JsonFileLabRepository([tmp_path])

    assert load_lab("lab_a+lab_b", repository=repo).name == "lab_a+lab_b"


def test_load_lab_round_trips_declared_link_from_fixture():
    """The tech1 fixture's test1<->test2 udp link survives load_lab end to end."""
    lab = load_lab("unix", search_paths=[lab_data_dir() / "tech1"])

    by_pair = {frozenset({link.a.host, link.b.host}): link for link in lab.links}
    link = by_pair[frozenset({"test1", "test2"})]
    assert link.protocol == "udp"
    assert {link.a.ip, link.b.ip} == {"192.168.1.11", "192.168.1.12"}


def test_loading_one_lab_also_loads_a_link_reaching_out_of_it():
    """``unix`` loads the bb1350 TAP link too, and that is the containment
    rule working rather than a leak.

    ``resolve_declared_links`` keeps any entry with at least ONE endpoint in
    the loaded lab, so a link is loaded by BOTH labs it straddles: test1
    belongs to ``unix`` and ``busybox``, so ``unix`` sees the
    ``test1:bbeth-1350 <-> bb1350_qemu:eth0`` link and ``busybox``
    already saw the ``test1:eth2 <-> test2:eth2`` one. Dropping
    half-outside links instead would silently hide every cross-lab route.

    Pinned as its own case because the count is load-bearing and easy to
    "fix" the wrong way: the sibling above used to assert ``len(lab.links)
    == 1`` and went red the moment a second link was declared, and bumping
    that number to 2 would have recorded the arithmetic while losing the
    reason. It also documents a sharp edge -- ``otto -l unix`` can NAME a
    link whose far end it never loaded, which ``link list`` reports as not
    impairable, but which ``link impair --from test1`` will act on
    while ``link repair`` refuses it for exactly that reason.
    """
    lab = load_lab("unix", search_paths=[lab_data_dir() / "tech1"])

    pairs = {frozenset({link.a.host, link.b.host}) for link in lab.links}
    assert frozenset({"test1", "bb1350_qemu"}) in pairs, pairs
    assert "bb1350_qemu" not in lab.hosts, "the guest is not a unix host, only its link is"


def test_lab_info_stamped_per_component(tmp_path) -> None:
    """Two labs in one file, loaded as a+b: each host names ITS component."""
    creds = [{"login": "u", "password": "p"}]
    write_lab_json(
        tmp_path / "lab.json",
        [
            {
                "ip": "10.10.200.11",
                "element": "alt1",
                "creds": creds,
                "resources": ["ra"],
                "labs": ["lab_a"],
            },
            {
                "ip": "10.10.200.12",
                "element": "test2",
                "creds": creds,
                "resources": ["rb"],
                "labs": ["lab_b"],
            },
        ],
    )
    doc = json.loads((tmp_path / "lab.json").read_text())
    doc["labs"]["lab_a"]["metadata"] = {"m": 1}
    (tmp_path / "lab.json").write_text(json.dumps(doc))

    lab = load_lab("lab_a+lab_b", search_paths=[tmp_path])

    assert lab.hosts["alt1"].lab_info.name == "lab_a"
    assert lab.hosts["alt1"].lab_info.resources == frozenset({"ra"})
    assert lab.hosts["alt1"].lab_info.metadata == {"m": 1}
    assert lab.hosts["test2"].lab_info.name == "lab_b"
    assert lab.metadata == {"lab_a": {"m": 1}, "lab_b": {}}
    assert lab.resources == {"ra", "rb"}
    lab.hosts["alt1"].lab_info.metadata["m"] = 2  # a per-host copy
    assert lab.metadata["lab_a"] == {"m": 1}


def test_builtin_local_carries_the_first_components_lab_info(tmp_path) -> None:
    """``local`` is attributed to the FIRST component, on BOTH channels (R16).

    It is injected after the merge, so the pre-merge sweep never sees it —
    leaving ``lab_info`` at its empty default while ``source_lab`` named a real
    component, i.e. the one host present in every lab disagreeing with itself
    about which lab it is in.
    """
    creds = [{"login": "u", "password": "p"}]
    write_lab_json(
        tmp_path / "lab.json",
        [
            {
                "ip": "10.10.200.11",
                "element": "alt1",
                "creds": creds,
                "resources": ["ra"],
                "labs": ["lab_a"],
            },
            {
                "ip": "10.10.200.12",
                "element": "test2",
                "creds": creds,
                "resources": ["rb"],
                "labs": ["lab_b"],
            },
        ],
    )
    doc = json.loads((tmp_path / "lab.json").read_text())
    doc["labs"]["lab_a"]["metadata"] = {"m": 1}
    (tmp_path / "lab.json").write_text(json.dumps(doc))

    local = load_lab("lab_a+lab_b", search_paths=[tmp_path]).hosts["local"]

    assert local.source_lab == "lab_a"
    assert local.lab_info.name == "lab_a", "the two attribution channels must agree"
    assert local.lab_info.resources == frozenset({"ra"})
    assert local.lab_info.metadata == {"m": 1}
