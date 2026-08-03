"""Leased live-bed target for the tier-3 chaos lane.

The lane's spine: lease ONE free veggies host (flock, cross-worker), prove it
reachable (fail LOUD, host-named, never skip), and expose it both as a
``ChaosTarget`` (for the otto subprocess under test) and as fresh probe
``UnixHost``s (for oracle exec over an independent connection). The tier-2
driver (`tests/integration/chaos/_driver.py`) is reused unchanged — signals
only ever go to the LOCAL otto subprocess; the bed host just runs its remote
commands.
"""

import asyncio
import contextlib
import dataclasses
import json
from collections.abc import Iterator
from pathlib import Path

from otto.link.derive import addressing_from_dict, resolve_declared_links
from otto.logger.mode import LogMode
from tests._fixtures._host_pool import lease_unix_host
from tests._fixtures.labdata import host_data, lab_data_path
from tests._fixtures.tunnel_bed import assert_reachable, build_bed_host
from tests.integration.chaos._target import ChaosTarget, make_bed_target


@dataclasses.dataclass(frozen=True)
class ChaosBed:
    element: str  # lab element name, e.g. "tomato"
    ip: str  # management ip from tech1/lab.json
    target: ChaosTarget  # aim the otto subprocess here


@contextlib.contextmanager
def leased_bed(lock_dir: Path) -> Iterator[ChaosBed]:
    """Lease a free veggies host; probe reachability; yield the bed handle."""
    with lease_unix_host(lock_dir) as element:
        ip = host_data(element)["ip"]
        asyncio.run(assert_reachable(element, ip))
        yield ChaosBed(element=element, ip=ip, target=make_bed_target(element))


@contextlib.asynccontextmanager
async def probe_host(element: str):
    """Fresh, independent ``UnixHost`` for oracle exec; always closed."""
    host = build_bed_host(element)
    try:
        yield host
    finally:
        await host.close()


def run_probe(element: str, coro_factory):
    """Run ``await coro_factory(host)`` on a fresh probe host in a fresh loop.

    Sync bridge for subprocess-driving (sync) scenario tests — mirrors the
    tier-2 suite's ``probe()`` shape but with a full UnixHost, so oracles can
    use ``exec``/``run`` semantics (sudo, timeouts, QUIET logging) instead of
    raw asyncssh.
    """

    async def _go():
        async with probe_host(element) as host:
            return await coro_factory(host)

    return asyncio.run(_go())


def veggies_link_id() -> str:
    """The declared carrot_seed<->tomato_seed eth2 link's id.

    Hoisted from three former per-module copies (``test_connection_drop.py``,
    ``test_tunnel_link_chaos.py``, ``test_reboot_chaos.py`` — verified
    byte-identical before the hoist). The raw ``tech1/lab.json`` has no
    literal ``"id"`` key on its ``links`` entries -- ``Link.id`` is
    auto-derived at load time from the sorted endpoint host ids
    (``otto.link.model.make_static_link_id``), so this replicates the SAME
    load ``otto`` itself does (``otto.link.derive.resolve_declared_links``)
    rather than guessing the computed string.
    """
    data = json.loads(lab_data_path().read_text())
    hosts = dict(addressing_from_dict(h) for h in data["hosts"])
    loaded_ids = set(hosts)
    links = resolve_declared_links(data["links"], hosts, source="lab.json", loaded_ids=loaded_ids)
    link = links[0]  # tech1/lab.json declares exactly one link: carrot_seed:eth2<->tomato_seed:eth2
    assert {link.a.host, link.b.host} == {"carrot_seed", "tomato_seed"}, (
        f"expected the carrot<->tomato eth2 link at links[0], got {link!r} -- "
        "tech1/lab.json's declared links changed shape"
    )
    return link.id


def tunnel_target(sut_dir) -> ChaosTarget:
    """ChaosTarget for CLI-driven tunnel commands against `cli_sut_dir`'s isolated SUT.

    Hoisted from two former per-module copies (``test_tunnel_link_chaos.py``,
    ``test_reboot_chaos.py`` — verified byte-identical before the hoist).
    ``spawn_otto`` only ever reads ``target.sut_dir``/``target.lab`` (see
    ``tests/integration/chaos/_driver.py::_otto_env``) — the ssh_* fields
    exist to feed the asyncssh oracle in ``tests.integration.chaos._target``,
    unused here since tunnel-process reconciliation goes through
    ``observe_tunnel_processes``/``run_probe`` instead. Populated from
    carrot's real creds anyway, for shape-parity with ``make_bed_target``.
    """
    carrot = host_data("carrot")
    cred = carrot["creds"][0]
    return ChaosTarget(
        sut_dir=sut_dir,
        lab="veggies",
        host_id="carrot_seed",
        ssh_host=carrot["ip"],
        ssh_port=22,
        ssh_username=cred["login"],
        ssh_client_key=None,
        ssh_password=cred["password"],
    )


def assert_eth2_netem_free(what: str) -> None:
    """Assert both eth2 endpoints (carrot, tomato) carry no netem qdisc.

    Hoisted from two former per-module copies (``test_tunnel_link_chaos.py``,
    ``test_reboot_chaos.py`` — verified behaviorally identical before the
    hoist).
    """
    for elem in ("carrot", "tomato"):
        out = run_probe(
            elem, lambda h: h.exec("tc qdisc show dev eth2", timeout=30, log=LogMode.QUIET)
        )
        assert "netem" not in (out.value or ""), f"{elem}: netem survived {what}: {out.value!r}"
