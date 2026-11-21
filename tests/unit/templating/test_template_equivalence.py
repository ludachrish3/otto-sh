"""A rendered fleet and its hand-expanded JSON give identical labs (spec section 8).

Two projects share everything but their fleet source: the templated one
renders ``router.jsonnet`` through a ``jsonnet`` source, the hand-written one
reads the same fleet expanded by hand into a ``lab.json``. Both carry the same
hand-written console server (joining every router lab by pattern) and the same
later override source, so the comparison runs through composition across
sources too. Every lab both declare loads with the same host ids, addresses,
console options, inventory references and links.
"""

from pathlib import Path
from typing import Any

from otto.config.project import Project

from ._lab_project import CREDS, inventory, json_source, jsonnet_source, operation, project, write

FLEET_TEMPLATE = """
local o = import 'otto.libsonnet';
local hosts = import 'hosts.libsonnet';

function(otto)
  local recs = otto.inventories.physical + otto.inventories.virtual;
  local instances = o.groupBy(recs, ['site', 'rack']);
  local site(i) = i.id.site + '-' + i.id.rack;
  local doc = o.layout({
    instances: instances,
    lab(i): 'router-lab.' + site(i),
    elementName(i, role, record, n): site(i) + '-' + role + n,
    labsEntry(i): { resources: ['bench-power'], metadata: { site: i.id.site } },
    roles: {
      dut: { match: o.inGroup('dut'), count: 1, host: { label: 'dut', fn: hosts.routerDut } },
      tgen: { match: o.inGroup('tgen'), count: 1, host: { label: 'tgen', fn: hosts.linuxTgen } },
    },
  });
  doc {
    links: [
      {
        name: site(i) + '-data',
        endpoints: [{ host: site(i) + '-dut0' }, { host: site(i) + '-tgen0' }],
      }
      for i in instances
      if !std.objectHas(doc.labs['router-lab.' + site(i)], 'unavailable')
    ],
  }
"""
"""One router lab per site and rack: a DUT from ``physical``, a traffic generator from ``virtual``.
"""

HOSTS_LIBRARY = """
{
  routerDut(r): {
    os_type: 'unix',
    valid_terms: ['ssh', 'console'],
    console_options: { server: 'consrv1', port: r.labels.console_port },
  },
  linuxTgen(r): { os_type: 'unix', valid_terms: ['ssh'] },
}
"""


def _element(name: str, lab: str, host: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, "labs": [lab], "hosts": [host]}


def _dut(key: str, port: int) -> dict[str, Any]:
    return {
        "os_type": "unix",
        "valid_terms": ["ssh", "console"],
        "console_options": {"server": "consrv1", "port": port},
        "inventory": {"name": "physical", "key": key},
    }


def _tgen(key: str) -> dict[str, Any]:
    return {"os_type": "unix", "valid_terms": ["ssh"], "inventory": {"name": "virtual", "key": key}}


FLEET_EXPANDED_JSON: dict[str, Any] = {
    "labs": {
        "router-lab.east-r7": {"resources": ["bench-power"], "metadata": {"site": "east"}},
        "router-lab.west-r8": {"resources": ["bench-power"], "metadata": {"site": "west"}},
        "router-lab.north-r9": {"unavailable": "tgen: expected 1, found 0 (none)"},
    },
    "elements": [
        _element("east-r7-dut0", r"router-lab\.east-r7", _dut("edge-17", 7017)),
        _element("east-r7-tgen0", r"router-lab\.east-r7", _tgen("ixia-9")),
        _element("west-r8-dut0", r"router-lab\.west-r8", _dut("edge-18", 7018)),
        _element("west-r8-tgen0", r"router-lab\.west-r8", _tgen("ixia-8")),
    ],
    "links": [
        {
            "name": "east-r7-data",
            "endpoints": [{"host": "east-r7-dut0"}, {"host": "east-r7-tgen0"}],
        },
        {
            "name": "west-r8-data",
            "endpoints": [{"host": "west-r8-dut0"}, {"host": "west-r8-tgen0"}],
        },
    ],
}
"""The fleet ``FLEET_TEMPLATE`` renders, expanded by hand."""

PHYSICAL = {
    "edge-17": {
        "ip": "10.1.0.17",
        "site": "east",
        "rack": "r7",
        "groups": ["dut"],
        "labels": {"console_port": 7017},
        "creds": CREDS,
    },
    "edge-18": {
        "ip": "10.1.0.18",
        "site": "west",
        "rack": "r8",
        "groups": ["dut"],
        "labels": {"console_port": 7018},
        "creds": CREDS,
    },
    "edge-19": {
        "ip": "10.1.0.19",
        "site": "north",
        "rack": "r9",
        "groups": ["dut"],
        "labels": {"console_port": 7019},
        "creds": CREDS,
    },
}
VIRTUAL = {
    "ixia-9": {"ip": "10.2.0.9", "site": "east", "rack": "r7", "groups": ["tgen"], "creds": CREDS},
    "ixia-8": {"ip": "10.2.0.8", "site": "west", "rack": "r8", "groups": ["tgen"], "creds": CREDS},
}

SHARED = {
    "elements": [
        {
            "name": "consrv1",
            "labs": [r"router-lab\..*"],
            "hosts": [{"ip": "10.3.0.1", "os_type": "unix", "creds": CREDS}],
        }
    ],
    "links": [{"endpoints": [{"host": "consrv1"}, {"host": "east-r7-dut0"}]}],
}
"""A hand-written console server joining every router lab, linked to one generated DUT."""

OVERRIDE = {
    "elements": [
        _element(
            "west-r8-tgen0",
            r"router-lab\.west-r8",
            {**_tgen("ixia-8"), "valid_terms": ["ssh", "telnet"]},
        )
    ]
}
"""A later source replacing one generated element with the same machine, other terms."""


def project_pair(
    tmp_path: Path, template: str, expanded: dict[str, Any]
) -> "tuple[Project, Project]":
    """Return the templated project and the hand-written one, over the same inventories."""
    built: list[Project] = []
    for kind in ("templated", "hand"):
        root = tmp_path / kind
        shared = write(root / "lab_data" / "shared" / "lab.json", SHARED)
        override = write(root / "lab_data" / "override" / "lab.json", OVERRIDE)
        if kind == "templated":
            write(root / "lab_data" / "lib" / "hosts.libsonnet", HOSTS_LIBRARY)
            entry = write(root / "lab_data" / "layouts" / "router.jsonnet", template)
            fleet = jsonnet_source(paths=[str(entry)], inventories=["physical", "virtual"])
        else:
            fleet = json_source(write(root / "lab_data" / "fleet" / "lab.json", expanded))
        settings = (
            inventory(root, "physical", PHYSICAL)
            + inventory(root, "virtual", VIRTUAL)
            + json_source(shared)
            + fleet
            + json_source(override)
        )
        built.append(project(root, settings))
    return built[0], built[1]


def hand_names(hand: Project) -> list[str]:
    """Every lab the hand-written project lists."""
    op = operation([hand])
    return [status.name for status in op.repository.lab_status_report(view=op.view).labs]


def lab_facts(sut: Project, name: str) -> dict[str, Any]:
    """Lab *name* of *sut*, loaded as ``--lab`` loads it, reduced to what must match."""
    from otto.session import build_lab

    lab = build_lab([sut], labs=[name])
    hosts = {
        host_id: {
            "ip": host.ip,
            "element": host.element.name,
            "valid_terms": list(host.valid_terms),
            "console": (host.console_options.server, host.console_options.port),
            "ref": (host.inventory_ref.name, host.inventory_ref.key, host.inventory_ref.qualified),
            "logins": [c.login for c in host.creds or []],
        }
        for host_id, host in lab.hosts.items()
        if host_id != "local"  # the controller's own host, added to every lab
    }
    links = sorted((link.id, link.a.host, link.b.host, link.a.ip, link.b.ip) for link in lab.links)
    return {
        "hosts": hosts,
        "links": links,
        "resources": sorted(lab.resources),
        "metadata": lab.metadata,
    }


def test_rendered_fleet_equals_its_hand_expanded_json(tmp_path, otto_home):
    templated, hand = project_pair(tmp_path, FLEET_TEMPLATE, FLEET_EXPANDED_JSON)
    names = hand_names(hand)
    assert names == ["router-lab.east-r7", "router-lab.north-r9", "router-lab.west-r8"]
    assert hand_names(templated) == names
    for name in names:
        if name == "router-lab.north-r9":
            continue  # unavailable in both: compared through the status report below
        assert lab_facts(templated, name) == lab_facts(
            hand, name
        )  # ids, console options, links, refs
    east = lab_facts(templated, "router-lab.east-r7")
    assert sorted(east["hosts"]) == ["consrv1", "east-r7-dut0", "east-r7-tgen0"]
    assert east["hosts"]["east-r7-dut0"]["console"] == ("consrv1", 7017)
    assert east["hosts"]["east-r7-dut0"]["ref"] == ("physical", "edge-17", True)
    assert len(east["links"]) == 2
    west = lab_facts(templated, "router-lab.west-r8")
    assert west["hosts"]["west-r8-tgen0"]["valid_terms"] == ["ssh", "telnet"]  # the override won
    statuses = {}
    for sut in (templated, hand):
        op = operation([sut])
        report = op.repository.lab_status_report(view=op.view)
        statuses[sut.root.name] = [
            (s.name, None if s.unavailable is None else s.unavailable.reason) for s in report.labs
        ]
    assert statuses["templated"] == statuses["hand"]
    assert ("router-lab.north-r9", "tgen: expected 1, found 0 (none)") in statuses["hand"]
