"""Acceptance: NetBox and Jsonnet inventories render, compose and dispatch one lab.

The closing end-to-end check of lab templating (#1176): one project, driven
through the real ``otto`` CLI as a subprocess with an isolated ``OTTO_HOME``,
with nothing between the CLI and the backends but loopback.

- ``physical`` is the real ``netbox`` backend, pointed at a
  :class:`~tests.unit.inventory.netbox_stub.NetBoxStub` this process serves
  on ``127.0.0.1``. Its one device, ``edge-r1``, is the bed's test4
  (``10.10.200.14``, the Vagrant VM ``zephyr``, which is also its hostname),
  and its credentials come from the inventory's own ``creds`` sub-table.
- ``virtual`` is a ``.jsonnet`` inventory file: test1, test2 and test3.
- ``lab_data/layouts/router-lab.jsonnet`` groups both on ``site`` and
  ``rack``, with the DUT selected by ``inInventory('physical')`` and the
  traffic generator by ``allOf([inInventory('virtual'), fact('key',
  'test1')])``. NetBox records carry no ``groups`` or ``labels`` yet (#1192),
  so the roles select by inventory membership and facts. The source is
  scoped to ``router-lab.``.
- ``bench``, a hand-written lab outside that scope, holds test2.

A NetBox device always has a device type, which the backend maps to the
record's ``board``, and a host's id carries its board: the DUT's host is
``edge-r1-cx-4`` (``make_host_id``), while its element is ``edge-r1``.

Run 1 lists, renders and explains the lab and dispatches to its DUT. Run 2
stops NetBox and deletes the home's snapshot cache, so the backend's plain
``InventoryError`` has no snapshot to fall back on: the scoped lab fails
naming its source and the NetBox inventory, ``--list-labs`` still lists it,
unavailable, from the source's last-good names, and ``bench`` still loads
and dispatches. The typed-transient ``max_stale`` policy is not restated
here: phase 1's fake-backend tests in
``tests/unit/inventory/test_cache_staleness.py`` own it.

The steps that reach no host are :func:`test_templated_lab_lists_renders_explains_and_fails_scoped`,
``hostless``, so every CI run holds them. The dispatches are
:func:`test_templated_lab_dispatches_and_a_failed_source_spares_bench`,
``integration``: the bed half of ``make coverage`` (``make coverage-bed``,
which owns the bed lease) and the gate's bed lanes run it.
"""

import contextlib
import json
import re
import shutil
import string
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from otto.config.home import SNAPSHOT_CACHE_DIRNAME
from otto.host.remote_host import make_host_id
from tests._fixtures._host_pool import lease_unix_host
from tests._fixtures.project_scaffold import copy_sample_project
from tests.e2e._otto_subprocess import run_otto
from tests.unit.inventory.netbox_stub import TOKEN, NetBoxStub, device
from tests.unit.templating import conftest as _templating

# The binding proof every templated test file carries: the child ``otto`` loads
# the binding this process's venv loads.
jsonnet_binding_under_test = _templating.jsonnet_binding_under_test

_PROJECT = Path(__file__).parent / "_templating_project"
_ENTRY = "lab_data/layouts/router-lab.jsonnet"
_LAB = "router-lab.lab-a-1"
_SOURCE = "templating_e2e/jsonnet#2"
_BOARD = "cx-4"
_DUT = make_host_id("edge-r1", _BOARD, None)
_DUT_HOSTNAME = "zephyr"
"""The hostname of the machine behind the DUT: lab host test4 at ``10.10.200.14``
is the Vagrant VM ``zephyr``, and the bed names each VM's host after the VM."""
_SECRETS = ("Password1", TOKEN)
"""What no output may hold: the bed's second password (the first, ``vagrant``,
is also the dev VM's user, so it is in every path) and the NetBox token."""


@dataclass(frozen=True)
class _TemplatedProject:
    """The project copied into the test's directory, its NetBox stub and its otto home."""

    root: Path
    home: Path
    xdir: Path
    stub: NetBoxStub
    _serving: contextlib.ExitStack

    def otto(self, *argv: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
        """Run ``otto ARGV`` from the project, against this project and home only."""
        return run_otto(
            list(argv),
            xdir=self.xdir,
            roots=self.root,
            cwd=self.root,
            extra_env={"OTTO_HOME": str(self.home), "NETBOX_TOKEN": TOKEN},
            timeout=timeout,
        )

    def take_netbox_down(self) -> None:
        """Stop the stub and delete the snapshots, so NetBox's failure has no fallback."""
        assert self.stub.requests, "run 1 never reached the NetBox stub"
        self._serving.close()
        snapshots = self.home / SNAPSHOT_CACHE_DIRNAME
        assert snapshots.is_dir(), f"run 1 left no NetBox snapshot under {self.home}"
        shutil.rmtree(snapshots)


@pytest.fixture
def project(tmp_path: Path) -> Iterator[_TemplatedProject]:
    """The project with its settings pointed at a running NetBox stub that serves edge-r1."""
    stub = NetBoxStub(
        [device(1, "edge-r1", ip="10.10.200.14/24", site="lab-a", rack="1", model=_BOARD)]
    )
    with contextlib.ExitStack() as serving:
        serving.enter_context(stub)
        root = copy_sample_project(_PROJECT, tmp_path / "project")
        settings = root / ".otto" / "settings.toml"
        netbox = string.Template(settings.read_text()).substitute(netbox_url=stub.base)
        settings.write_text(netbox)  # scaffold-exempt: in-place edit of a copied sample project
        yield _TemplatedProject(
            root=root, home=tmp_path / "home", xdir=tmp_path / "xdir", stub=stub, _serving=serving
        )


def _output(result: subprocess.CompletedProcess[str]) -> str:
    return f"exit {result.returncode}\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"


def _assert_no_secret(result: subprocess.CompletedProcess[str]) -> None:
    for secret in _SECRETS:
        assert secret not in result.stdout + result.stderr, _output(result)


def _assert_fails_naming_netbox(
    result: subprocess.CompletedProcess[str], project: _TemplatedProject
) -> None:
    """The scoped lab's load fails with its source's reason: the NetBox inventory is down."""
    assert result.returncode != 0, _output(result)
    said = " ".join((result.stdout + result.stderr).split())
    assert f"Lab '{_LAB}' is unavailable: source {_SOURCE} failed" in said, _output(result)
    assert f"netbox inventory {project.stub.base}" in said, _output(result)
    _assert_no_secret(result)


def _assert_dispatched(result: subprocess.CompletedProcess[str], machine_hostname: str) -> None:
    """``exec hostname`` ran on the machine whose own hostname is *machine_hostname*.

    That is what the machine's ``hostname`` prints, not the lab host's id: the
    two differ wherever the bed's VM is named apart from its lab host.
    """
    assert result.returncode == 0, _output(result)
    assert re.search(rf"> \| {machine_hostname}\s*$", result.stdout, re.MULTILINE), _output(result)
    _assert_no_secret(result)


@pytest.mark.hostless
def test_templated_lab_lists_renders_explains_and_fails_scoped(project: _TemplatedProject) -> None:
    """Run 1 lists, renders and explains the templated lab; run 2 fails it and spares bench."""
    listed = project.otto("--list-labs")
    assert listed.returncode == 0, _output(listed)
    assert re.search(rf"• {re.escape(_LAB)}\s", listed.stdout), _output(listed)
    assert re.search(r"• bench\b(?! — unavailable)", listed.stdout), _output(listed)
    assert "unavailable" not in listed.stdout, _output(listed)

    rendered = project.otto("lab", "render", _ENTRY)
    assert rendered.returncode == 0, _output(rendered)
    _assert_no_secret(rendered)
    document = json.loads(rendered.stdout)
    assert document["labs"] == {_LAB: {}}
    assert {e["name"]: e["hosts"][0]["inventory"] for e in document["elements"]} == {
        "edge-r1": {"name": "physical", "key": "edge-r1"},
        "test1": {"name": "virtual", "key": "test1"},
    }
    assert "creds" not in json.dumps(document), "a rendered document carries credentials"
    assert {name: p["role"] for name, p in document["_otto"]["provenance"].items()} == {
        "edge-r1": "dut",
        "test1": "tgen",
    }

    explained = project.otto("-l", _LAB, "lab", "explain", _DUT)
    assert explained.returncode == 0, _output(explained)
    _assert_no_secret(explained)
    out = explained.stdout
    assert re.search(r"^│ ip\s+│ inventory record\s+│", out, re.MULTILINE), _output(explained)
    assert re.search(r"^│ os_type\s+│ template\s+│", out, re.MULTILINE), _output(explained)
    # physical's credentials come from its ``[inventories.creds]`` sub-table
    assert re.search(r"^│ creds\.password\s+│ creds store\s+│", out, re.MULTILINE), _output(
        explained
    )
    assert f"host: {_DUT}\n" in out, _output(explained)
    assert "(element 'edge-r1', hosts[0])" in out, _output(explained)
    assert "role: dut\n" in out, _output(explained)
    assert re.search(r"^inventory physical: ", out, re.MULTILINE), _output(explained)

    project.take_netbox_down()

    _assert_fails_naming_netbox(project.otto("-l", _LAB, "--list-hosts"), project)

    listed = project.otto("--list-labs")
    assert listed.returncode == 0, _output(listed)
    assert re.search(rf"• {re.escape(_LAB)} — unavailable: ", listed.stdout), _output(listed)
    assert re.search(r"• bench\b(?! — unavailable)", listed.stdout), _output(listed)

    bench = project.otto("-l", "bench", "--list-hosts")
    assert bench.returncode == 0, _output(bench)
    assert re.search(r"• test2\s", bench.stdout), _output(bench)


@pytest.mark.integration
def test_templated_lab_dispatches_and_a_failed_source_spares_bench(
    project: _TemplatedProject,
) -> None:
    """Run 1 dispatches to NetBox's edge-r1 (test4); run 2 still dispatches to bench's test2."""
    dut = project.otto("-R", "--lab", _LAB, "host", _DUT, "exec", "hostname", timeout=120)
    _assert_dispatched(dut, _DUT_HOSTNAME)

    project.take_netbox_down()

    failed = project.otto("-R", "--lab", _LAB, "host", _DUT, "exec", "hostname", timeout=120)
    _assert_fails_naming_netbox(failed, project)

    with lease_unix_host(("test2",)):
        bench = project.otto(
            "-R", "--lab", "bench", "host", "test2", "exec", "hostname", timeout=120
        )
    _assert_dispatched(bench, "test2")
