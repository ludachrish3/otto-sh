"""
Fixtures local to tests/integration/host/.

The parametrized ``host1`` / ``host1_kit`` fixtures live in
:mod:`tests.conftest` (shared with the unit tree). This conftest exists
only to populate the lab into otto's configModule so the embedded hosts'
hop resolution (``configmodule.get_host('basil_seed')`` inside
``RemoteHost._build_hop_transport``) can find the SSH hop.

The same wiring is done in :mod:`tests.unit.host.test_hop_integration` for
multi-hop UnixHost tests; both follow the pattern documented in
:func:`otto.configmodule.setConfigModule`.
"""

import pytest

from otto.configmodule import setConfigModule
from otto.configmodule.lab import Lab
from otto.host.unixHost import UnixHost

from tests.conftest import host_data


@pytest.fixture(autouse=True, scope="module")
def _load_lab():
    """Make the SSH hops resolvable by the embedded host transport.

    The Zephyr backends in ``host1`` carry ``hop="basil_seed"``, and
    :meth:`RemoteHost._build_hop_transport` calls ``get_host(hop_id)`` to
    resolve the hop's connection details. That lookup needs the configModule
    populated with at least the ``basil`` Unix host.

    Adding ``carrot`` / ``tomato`` / ``pepper`` too keeps the lab usable by
    any cross-OS / mixed-hop test that ends up in this directory.
    """
    lab = Lab(name="integration_host")
    for ne in ("carrot", "tomato", "pepper", "basil"):
        data = host_data(ne)
        lab.addHost(UnixHost(
            ip=data["ip"],
            ne=data["ne"],
            creds=data["creds"],
            board=data.get("board"),
            is_virtual=data.get("is_virtual", False),
            term=data.get("term", "ssh"),
            transfer=data.get("transfer", "scp"),
            log=False,
        ))
    setConfigModule(lab=lab, repos=[])
    yield
