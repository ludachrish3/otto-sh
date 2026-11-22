"""A built lab projected host by host, as ``tests/_fixtures/unchanged_loading.json`` records it.

:func:`lab_projection` reduces what :func:`otto.session.build_lab` returns to
plain JSON: each host's identity, address, element, inventory reference,
logins, console and interfaces, then the lab's links, metadata and resources.
The unchanged-loading golden was written through it, so every test that
compares a lab with that golden projects through this one function.
:func:`register_fixture_host_extensions` registers what the fixture labs'
init modules register, which a lab needs before it builds.
"""

import dataclasses
import json
from typing import Any

from otto.host.builtin_hosts import BUILTIN_LOCAL_HOST_ID
from tests._fixtures.labdata import register_zephyr_inline_frame
from tests._fixtures.paths import CHECKOUT_ROOT, ensure_custom_hosts_on_path
from tests._fixtures.portable import portable


def register_fixture_host_extensions() -> None:
    """What the fixture labs' init modules register: ``tech1``'s frame, the example's proxy."""
    from tests._fixtures.gs_example import import_gs_example

    ensure_custom_hosts_on_path()
    register_zephyr_inline_frame()
    import_gs_example()


def _jsonable(value: Any) -> Any:
    """A dataclass or mapping as plain JSON, via a round trip that stringifies the rest."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        value = dataclasses.asdict(value)
    return json.loads(json.dumps(value, sort_keys=True, default=str))


def _remote_host(host: Any) -> dict[str, Any]:
    ref = host.inventory_ref
    return {
        "id": host.id,
        "class": type(host).__name__,
        "os_type": host.os_type,
        "ip": host.ip,
        "element": host.element.slug,
        "source_lab": host.source_lab,
        "lab_info": host.lab_info.name,
        "resources": sorted(host.resources),
        "inventory_key": ref.key,
        "inventory_backend": portable(ref.backend, places={"<root>": CHECKOUT_ROOT}),
        "logins": sorted(c.login for c in host.creds),
        "console_options": _jsonable(host.console_options),
        "interfaces": {name: _jsonable(i) for name, i in sorted(host.interfaces.items())},
        "hop": host.hop,
    }


def _local_host(host: Any) -> dict[str, Any]:
    return {
        "id": host.id,
        "class": type(host).__name__,
        "source_lab": host.source_lab,
        "logins": sorted(c.login for c in host.creds or []),
    }


def lab_projection(lab: Any) -> dict[str, Any]:
    """*lab*, a built lab, as the plain JSON the unchanged-loading golden records."""
    return {
        "hosts": {
            host_id: (_local_host if host_id == BUILTIN_LOCAL_HOST_ID else _remote_host)(host)
            for host_id, host in sorted(lab.hosts.items())
        },
        "links": {
            link.id: {"a": _jsonable(link.a), "b": _jsonable(link.b)}
            for link in sorted(lab.static_links(), key=lambda link: link.id)
        },
        "metadata": _jsonable(lab.metadata),
        "resources": sorted(lab.resources),
    }
