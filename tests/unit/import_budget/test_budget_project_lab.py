"""The budget project's lab (#1289): what it holds, and why a bad record cannot hide in it.

The import-budget delta generates the budget project with the tip's harness
under the previous release's otto, so the lab it reads must load there too
(:data:`tests._fixtures.paths.BUDGET_PROJECT_LAB` states the rule). This module
is the tip's half. The base's half is the delta itself: its canary dispatches
with ``OTTO_LAB=unix``, and a canary that fails at the base is a stop.

The canary only proves what it makes the base read. A lab validates the
members of the selected lab strictly and skips an unreadable record anywhere
else, so every element here is a member of ``unix``.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from otto.labs.errors import LabRepositoryError
from tests._fixtures.labdata import built_json_source
from tests._fixtures.paths import BUDGET_PROJECT_LAB

LAB = "unix"


def _load(root: Path, lab_file: Path) -> Any:
    """Load ``unix`` from *lab_file* through the json source a configuration builds."""
    return built_json_source(root, "budget", lab_file).repository.load_lab(LAB)


def test_it_loads_at_the_tip(tmp_path: Path):
    lab = _load(tmp_path, BUDGET_PROJECT_LAB)
    # what the budget surfaces' expected answers name, plus one host of each other kind
    assert {"test1", "test2", "test3", "test4", "bb1310-qemu", "zephyr37-fat"} == set(lab.hosts)


def test_every_element_is_in_the_one_lab():
    """Every record is a member of the lab the canary selects, so the base reads them all."""
    doc = json.loads(BUDGET_PROJECT_LAB.read_text())
    assert set(doc["labs"]) == {LAB}
    assert [e["labs"] for e in doc["elements"]] == [[LAB]] * len(doc["elements"])
    # `completion_remote_path_hit_reserved` derives what it must reserve from this
    assert doc["labs"][LAB]["resources"]


def test_the_one_link_joins_two_hosts_the_file_holds():
    doc = json.loads(BUDGET_PROJECT_LAB.read_text())
    [link] = doc["links"]
    ends = {(ep["host"], ep["interface"]) for ep in link["endpoints"]}
    assert ends == {("test1", "eth2"), ("test2", "eth2")}
    interfaces = {
        e["name"]: set(h.get("interfaces", {})) for e in doc["elements"] for h in e["hosts"]
    }
    for host, interface in ends:
        assert interface in interfaces[host]


@pytest.mark.parametrize("element", ["bb1310", "zephyr37_fat"])
def test_a_record_the_reader_rejects_fails_the_unix_load(tmp_path: Path, element: str):
    """The property the delta's canary relies on, shown at the tip.

    A host that is not one of the Ubuntu VMs still stops the ``unix`` load when
    its record cannot be read, because it is a member. Were it a member of
    another lab only, the load would skip it and the canary would pass.
    """
    doc = json.loads(BUDGET_PROJECT_LAB.read_text())
    [record] = [e for e in doc["elements"] if e["name"] == element]
    record["hosts"][0]["a_field_no_release_reads"] = True
    bad = tmp_path / "lab.json"
    bad.write_text(json.dumps(doc))
    with pytest.raises(LabRepositoryError, match="a_field_no_release_reads"):
        _load(tmp_path, bad)
