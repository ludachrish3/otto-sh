"""The ten plumbing classes nothing reaches stay off the public surface.

Spec 2026-11-07-keyword-only-public-interfaces-design.md §6.8 demoted them: each
had both rights withdrawn, no public signature, field, constant or ``__init__``
annotation named it, and no docs page named it. A registry kind string (``"json"``,
``"ftp"``, ``"netbox"``, ``"command"``) promises the protocol or base its factory
returns, never the concrete class. The list is spelled literally: this test is the
record of the decision. A class that comes back takes the normal route (a public
name, an ``__all__`` entry, a reference page, a ``[symbols]`` entry), and this list
loses its key in the same change.
"""

import pytest
import tomli

from tests._fixtures.paths import CHECKOUT_ROOT

DEMOTED = [
    "otto.coverage:GcdaFetcher",
    "otto.creds:JsonCredsStore",
    "otto.host:CommandPowerController",
    "otto.host:HostFilter",
    "otto.host:SshHopTransport",
    "otto.host.transfer:FtpFileTransfer",
    "otto.host.transfer:TftpFileTransfer",
    "otto.inventory:NetBoxInventory",
    "otto.labs:JsonFileLabRepository",
    "otto.reservations:JsonReservationBackend",
]

_GOLDEN = CHECKOUT_ROOT / "tests" / "unit" / "api_snapshot" / "public_api.txt"
_MANIFEST = CHECKOUT_ROOT / "api" / "public.toml"


def _golden_names() -> set[str]:
    names = set()
    for line in _GOLDEN.read_text(encoding="utf-8").splitlines():
        fields = line.split("\t")
        if fields[0] == "name" and len(fields) > 1:
            names.add(fields[1])
    return names


@pytest.mark.parametrize("key", DEMOTED)
def test_demoted_key_has_no_golden_name_record(key: str) -> None:
    assert key not in _golden_names()


@pytest.mark.parametrize("key", DEMOTED)
def test_demoted_key_has_no_symbols_entry(key: str) -> None:
    symbols = tomli.loads(_MANIFEST.read_text(encoding="utf-8")).get("symbols", {})
    stale = sorted(k for k in symbols if k == key or k.startswith(key + "."))
    assert stale == []
