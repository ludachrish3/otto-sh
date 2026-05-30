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

import sys
from pathlib import Path

import pytest

from otto.configmodule import setConfigModule
from otto.configmodule.lab import Lab
from otto.host.command_frame import register_command_frame
from otto.host.unixHost import UnixHost
from tests.conftest import host_data

# Make repo1's custom Zephyr 2.7 dialect resolvable by the storage factory.
#
# The embedded integration tests build hosts via ``create_host_from_dict``
# directly (the raw factory path), which — unlike a full ``otto`` config load —
# does not import the SUT repo's init modules, so the ``"zephyr-inline"`` frame
# the 2.7 lab entries declare would be unregistered. Register it here by
# importing the very same class repo1's init module registers in production
# (``repo1_instructions/__init__.py``), adding the repo's pylib to the path the
# way ``Repo.addLibsToPythonpath`` does at config-load time.
_REPO1_PYLIB = Path(__file__).resolve().parents[2] / "repo1" / "pylib"
if str(_REPO1_PYLIB) not in sys.path:
    sys.path.insert(0, str(_REPO1_PYLIB))
from repo1_common.zephyr_inline import ZephyrInlineRetcodeFrame  # noqa: E402

register_command_frame(ZephyrInlineRetcodeFrame.type_name, ZephyrInlineRetcodeFrame)


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
