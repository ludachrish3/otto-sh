"""Leased live-bed target for the tier-3 chaos lane.

The lane's spine: lease ONE free unix host (flock, cross-session), prove it
reachable (fail LOUD, host-named, never skip), and expose it both as a
``ChaosTarget`` (for the otto subprocess under test) and as fresh probe
``UnixHost``s (for oracle exec over an independent connection). The tier-2
driver (`tests/integration/chaos/_driver.py`) is reused unchanged — signals
only ever go to the LOCAL otto subprocess; the bed host just runs its remote
commands.

A SECOND, UNLEASED TARGET lives at the bottom of this module: the BusyBox
bed's ``bb1350`` guest, reached over telnet through the ``test1`` hop.
It is deliberately NOT part of the ``UNIX_POOL`` lease — a QEMU guest is not
a unix host, nothing else in the lane competes for it, and the flock's
whole job is to keep two pool consumers off one VM. See
:data:`BUSYBOX_CHAOS_ELEMENT` for why the anchor is one fixed version rather
than the bed's five.
"""

import asyncio
import contextlib
import dataclasses
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from otto.context import OttoContext, set_context
from otto.host.factory import create_host_from_dict
from otto.host.login_proxy import Cred
from otto.host.remote_host import make_host_id
from otto.host.unix_host import UnixHost
from otto.labs.loader import Lab
from otto.link.derive import addressing_from_dict, raw_endpoint_host_ids, resolve_declared_links
from otto.logger.mode import LogMode
from otto.result import Result
from otto.tunnel.discovery import DISCOVERY_PS_COMMAND, Observation, parse_process_discovery
from otto.utils import wait_for
from scripts.render_chaos_guest_seed import CHAOS_GUEST
from tests._fixtures._host_pool import DOCKER_HOST, UNIX_POOL, lease_unix_host
from tests._fixtures.bed_hygiene import _PROBE_TIMEOUT, check_probe_result
from tests._fixtures.labdata import (
    element_for,
    element_of,
    entry_of,
    flatten_lab_doc,
    host_data,
    lab_data_path,
)
from tests._fixtures.run_state import preserved_run_state
from tests._fixtures.tunnel_bed import assert_reachable, build_bed_host, cli_project_root
from tests.e2e._otto_subprocess import PROJECT_E2E
from tests.integration.chaos._target import ChaosTarget, make_bed_target


@dataclasses.dataclass(frozen=True)
class ChaosBed:
    element: str  # lab element name, e.g. "test2"
    ip: str  # management ip from tech1/lab.json
    target: ChaosTarget  # aim the otto subprocess here


# The bed's pool leaves out the docker host. The lane's docker scenarios lease
# it later in the same process while the bed is still held, and a bed that had
# drawn it would wait on its own lease.
CHAOS_POOL: tuple[str, ...] = tuple(h for h in UNIX_POOL if h != DOCKER_HOST)


@contextlib.contextmanager
def leased_bed() -> Iterator[ChaosBed]:
    """Lease a free unix host, not the docker host; probe reachability; yield the bed handle."""
    with lease_unix_host(CHAOS_POOL) as element:
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

    A non-ok :class:`~otto.result.Result` coming back from the factory RAISES
    (host-named, status-quoted) instead of being returned — an oracle reading
    ``.value`` off a dead probe would report "clean bed" for the exact
    failures chaos manufactures (G5). The check can only vet what it sees:
    factories must return the ``Result`` itself (or use :func:`probe_text`),
    never unwrap ``.value`` before returning.
    """

    async def _go():
        async with probe_host(element) as host:
            out = await coro_factory(host)
        if isinstance(out, Result):
            check_probe_result(element, out)
        return out

    return asyncio.run(_go())


def probe_text(element: str, cmd: str, *, timeout: float = _PROBE_TIMEOUT) -> str:
    """The one spelling for a checked text read off the bed.

    ``run_probe`` + ``exec`` + the status check + ``.value`` unwrap in one
    call, so probe-reading helpers cannot drift back to the unchecked
    ``(result.value or "")`` shape that turned probe failures into clean
    oracle answers.
    """
    out = run_probe(element, lambda h: h.exec(cmd, timeout=timeout, log=LogMode.QUIET))
    return out.value


def declared_link_id(host_a: str, host_b: str) -> str:
    """The id of the declared ``tech1/lab.json`` link joining *host_a* and *host_b*.

    Hoisted from three former per-module copies of ``unix_link_id``
    (``test_connection_drop.py``, ``test_tunnel_link_chaos.py``,
    ``test_reboot_chaos.py`` — verified byte-identical before the hoist). The
    raw ``tech1/lab.json`` has no literal ``"id"`` key on its ``links``
    entries -- ``Link.id`` is auto-derived at load time from the sorted
    endpoint host ids (``otto.link.model.make_static_link_id``), so this
    replicates the SAME load ``otto`` itself does
    (``otto.link.derive.resolve_declared_links``) rather than guessing the
    computed string.

    Selected BY ENDPOINT PAIR, never by list index. The index shortcut was
    correct only while the file declared exactly one link, and it was written
    as ``links[0]`` with that assumption in a comment; the file now declares
    two (the unix eth2 route and the bb1350 TAP route), and a third would
    have silently handed one module the other's link. The endpoint pair is
    what every caller actually means.
    """
    data = json.loads(lab_data_path().read_text())
    # Skip records this process cannot resolve, mirroring _JsonFileLabRepository:
    # addressing_from_dict validates through the profile/frame registries, and
    # tech1/lab.json's `zephyr27_fat` declares `command_frame: zephyr-inline`, which
    # only a project's init modules register — never the pytest process.
    hosts = {}
    for h in flatten_lab_doc(data):
        try:
            host_id, addressing = addressing_from_dict(entry_of(h), element_of(h))
        except ValueError:
            # ValueError (incl. pydantic.ValidationError) is host_identity's
            # documented "profile/frame not registered here" contract; anything
            # else is fixture-file corruption and must propagate.
            continue
        hosts[host_id] = addressing
    loaded_ids = set(hosts)
    links = resolve_declared_links(data["links"], hosts, source="lab.json", loaded_ids=loaded_ids)
    want = {host_a, host_b}
    matches = [link for link in links if {link.a.host, link.b.host} == want]
    assert len(matches) == 1, (
        f"expected exactly one declared {host_a}<->{host_b} link in tech1/lab.json, "
        f"found {len(matches)}: {matches!r} -- the declared links changed shape"
    )
    return matches[0].id


UNIX_PAIR = ("test1", "test2")
"""The lab VM pair the unix chaos scenarios dirty (element names; id == element)."""


def host_id_of(element: str) -> str:
    """The host id lab data derives for *element*; the one place a pair's ids are computed.

    The same rule ``make_bed_target`` applies (``make_host_id(element, board, slot)``
    off the tech1 record), so a ``("test4", "ubu2404")`` pair names ``ubu2404-qemu``
    exactly where a link endpoint spells it.
    """
    data = host_data(element)
    return make_host_id(element_for(element).name, data.get("board"), data.get("slot"))


def unix_link_id(pair: tuple[str, str] = UNIX_PAIR) -> str:
    """The declared link id joining *pair* (element names; default test1<->test2)."""
    return declared_link_id(host_id_of(pair[0]), host_id_of(pair[1]))


def tunnel_target(root, *, lab: str = "unix", host: str = "test1") -> ChaosTarget:
    """ChaosTarget for CLI-driven tunnel commands against `cli_project_root`'s isolated SUT.

    Hoisted from two former per-module copies (``test_tunnel_link_chaos.py``,
    ``test_reboot_chaos.py`` — verified byte-identical before the hoist).
    ``spawn_otto`` only ever reads ``target.root``/``target.lab`` (see
    ``tests/integration/chaos/_driver.py::_otto_env``) — the ssh_* fields
    exist to feed the asyncssh oracle in ``tests.integration.chaos._target``,
    unused here since tunnel-process reconciliation goes through
    ``observe_tunnel_processes``/``run_probe`` instead. Populated from
    test1's real creds anyway, for shape-parity with ``make_bed_target``.
    """
    data = host_data(host)
    cred = data["creds"][0]
    return ChaosTarget(
        root=root,
        lab=lab,
        host_id=host_id_of(host),
        ssh_host=data["ip"],
        ssh_port=22,
        ssh_username=cred["login"],
        ssh_client_key=None,
        ssh_password=cred["password"],
    )


def unix_link_entries(pair: tuple[str, str] = UNIX_PAIR) -> list[dict[str, Any]]:
    """tech1's raw link entries whose BOTH ends are *pair* (default: the unix pair).

    Filtered by endpoint pair, never by list index (see ``declared_link_id``
    for why): the bb1350 TAP link also touches test1, and a lab that carried
    it would be exactly the half-outside shape ``repair --all`` is documented
    to fail on. Raw entries, not resolved ``Link``s, because the consumer is
    ``lab_json_v2``, which writes a document for otto to load.
    """
    data = json.loads(lab_data_path().read_text())
    want = {host_id_of(element) for element in pair}
    return [e for e in data["links"] if set(raw_endpoint_host_ids(e)) == want]


def closed_unix_link_target(
    tmp_path: Path, *, pair: tuple[str, str] = UNIX_PAIR, lab: str = "unix"
) -> ChaosTarget:
    """ChaosTarget for CLI-driven link commands on a CLOSED unix lab.

    ``make_bed_target("test1")`` loads tech1's ``unix`` verbatim, which (by the
    derived-membership rule, lab-config.md "Lab membership is derived") also
    loads the ``bb1350-qemu--test1`` link with its far end absent, so an honest
    ``link repair --all`` exits 1 there by design (docs/cli/link/repair.md,
    "When one end is out of scope"). This SUT loads test1+test2 and only the
    link between them, so the sweep's exit code answers the question the
    link chaos scenarios ask. Pinned hostless by
    ``tests/unit/test_chaos_link_lab_scope.py``.
    """
    return tunnel_target(
        cli_project_root(tmp_path, links=unix_link_entries(pair), elements=pair),
        lab=lab,
        host=pair[0],
    )


def end_probe_text(element: str, cmd: str, *, timeout: float = _PROBE_TIMEOUT) -> str:
    """Read *cmd*'s output off one link end: the ONE place that decides how an end is read.

    A lab VM is reached directly (:func:`probe_text`); the chaos guest only
    through its hop (:func:`chaos_guest_probe_text`).
    """
    if element == CHAOS_GUEST_ELEMENT:
        return chaos_guest_probe_text(cmd, timeout=timeout)
    return probe_text(element, cmd, timeout=timeout)


def assert_eth2_netem_free(
    what: str, *, ends: tuple[tuple[str, str], ...] = (("test1", "eth2"), ("test2", "eth2"))
) -> None:
    """Assert every ``(element, netdev)`` end (default: test1/test2 eth2) has no netem qdisc.

    Hoisted from two former per-module copies (``test_tunnel_link_chaos.py``,
    ``test_reboot_chaos.py`` — verified behaviorally identical before the
    hoist).
    """
    for elem, netdev in ends:
        qdisc = end_probe_text(elem, f"tc qdisc show dev {netdev}")
        assert "netem" not in qdisc, f"{elem}: netem survived {what}: {qdisc!r}"


# ---------------------------------------------------------------------------
# The BusyBox bed guest — the lane's second, unleased target
# ---------------------------------------------------------------------------

BUSYBOX_CHAOS_ELEMENT = "bb1350"
"""The ONE BusyBox guest the chaos lane aims at, fixed rather than parametrized.

The bed carries five guests (1.16.1 … 1.35.0) and the version matrix is
exercised, guest by guest, in ``tests/integration/busybox_bed`` — that suite
owns the question "does this userland differ". Chaos asks a different one:
does otto's machinery hold up under adversity. Running the adversity five
times over would multiply the lane's runtime (and its bed-hostility) to
re-answer a question another suite already answers, and every failure would
first have to be triaged as machinery-vs-userland before it meant anything.

1.35.0 is the anchor because it is the newest pin and the least
userland-constrained of the five (``base64`` applet present with a real
``-d`` flag, ``md5sum`` for integrity, ``stat`` for sizing), so a red arm
here is a machinery finding and not an old-applet gap. What is version-
specific about the guests is pinned in ``userland_options`` and tested where
those pins live, not here.
"""

BUSYBOX_HOP_ELEMENT = "test1"
"""The guest's hop. Each guest owns a /30 out of TEST-NET-2 whose other end is
a TAP device on test1, and the guests configure no default route — so the
address in the entry's ``ip`` is a real address that is routable from test1
and from nowhere else, which is why nothing reaches it without
``hop: test1`` resolving first (see :func:`busybox_hop_context`).
"""

BUSYBOX_CHAOS_HOST_ID = host_id_of(BUSYBOX_CHAOS_ELEMENT)
"""The guest's HOST ID (``element-board``), which is what link endpoints,
``--from`` and ``otto host`` all name — as distinct from the element name the
probe helpers above take."""

BUSYBOX_GUEST_NETDEV = "eth0"
"""The guest's only netdev: its own end of the /30. Also, and this is the
point of naming it, the netdev its management address lives on."""

BUSYBOX_TAP_NETDEV = f"bbeth-{BUSYBOX_CHAOS_ELEMENT.removeprefix('bb')}"
"""test1's end of the same wire. Derived from the element rather than typed,
so the anchor and its TAP cannot drift apart."""

CHAOS_GUEST_ELEMENT = CHAOS_GUEST.element
"""The Ubuntu 24.04 QEMU reboot guest's element name. Every constant in this
block is read off ``CHAOS_GUEST`` (``scripts/render_chaos_guest_seed.py``), the
one definition the provisioner also renders from; none is a second spelling."""

CHAOS_GUEST_HOST_ID = host_id_of(CHAOS_GUEST_ELEMENT)
"""The guest's HOST ID (``element-board``), as link endpoints and ``--from`` name
it. ``test_chaos_guest_lab_entries`` asserts it equals ``CHAOS_GUEST.host_id``."""

CHAOS_GUEST_HOP = CHAOS_GUEST.hop
"""The lab VM the guest is reachable through (its QEMU host)."""

CHAOS_GUEST_UNIT = CHAOS_GUEST.unit
"""The hop's systemd unit that runs the guest's QEMU process."""

CHAOS_LAB = CHAOS_GUEST.lab
"""The lab holding exactly the hop and the guest, so no sweep is aimed at
``embedded`` (which holds the hop but not the guest)."""

CHAOS_GUEST_DATA_NETDEV = CHAOS_GUEST.data.netdev
"""The guest's data-plane netdev: the one end of the declared link. The
management netdev is never impaired."""

CHAOS_HOP_DATA_TAP = CHAOS_GUEST.data.tap
"""The hop's TAP device for the data plane: the declared link's other end."""

CHAOS_PAIR = (CHAOS_GUEST_HOP, CHAOS_GUEST_ELEMENT)
"""The reboot lane's pair: the hop and the QEMU guest behind it (element names)."""


def busybox_link_id() -> str:
    """The declared ``test1:bbeth-1350 <-> bb1350-qemu:eth0`` link's id.

    The guest's ONLY wire, declared in ``tech1/lab.json`` since the bed moved
    onto real TAPs. What otto will and will not do with it is measured in
    ``test_connection_drop.py``'s guest arm — read that before assuming this
    id is impairable.
    """
    return declared_link_id("test1", BUSYBOX_CHAOS_HOST_ID)


@dataclasses.dataclass(frozen=True)
class BusyboxChaosBed:
    element: str  # lab element name, always BUSYBOX_CHAOS_ELEMENT
    version: str  # the guest's pinned BusyBox version, e.g. "1.35.0"
    ip: str  # the guest's own address on its /30, reachable from test1 only
    target: ChaosTarget  # aim the otto subprocess here


def busybox_target() -> ChaosTarget:
    """``ChaosTarget`` aiming an otto subprocess at the bed's BusyBox guest.

    The SUT is ``tests/project_e2e`` unchanged — its one ``[[lab.sources]]``
    entry already compiles ``tech1/lab.json``, which is where the five guest
    records and their ``test1`` hop live, so the lab leg is a plain
    ``-l busybox`` and no generated SUT is needed (unlike the hop-routed
    ``chaosdrop`` and console targets, which exist only because they need lab
    data this repository does not commit).

    ``spawn_otto`` reads ``root``/``lab`` and nothing else (see
    ``tests/integration/chaos/_driver.py::_otto_env``); the ``ssh_*`` fields
    exist to feed the asyncssh oracle in ``tests.integration.chaos._target``,
    which is not used here: the chaos target drives the guest over telnet
    through the hop, the path every guest has, even though bb1350 also
    carries a 2012 dropbear for the legacy-crypto cell
    (`tests/integration/busybox_bed/test_legacy_dropbear.py`).
    They are populated from the HOP's real creds for shape parity with
    ``make_bed_target``, and the oracle for this target is
    :func:`busybox_probe_text`, which goes through otto's own telnet-over-hop
    path because that is the only path there is.
    """
    return _hopped_target("busybox", BUSYBOX_CHAOS_ELEMENT, BUSYBOX_HOP_ELEMENT)


def _hopped_target(lab: str, guest_element: str, hop_element: str) -> ChaosTarget:
    """The one ``ChaosTarget`` builder for a hopped guest: ``tests/project_e2e``, lab *lab*.

    The host id is the guest's (:func:`host_id_of`); the ``ssh_*`` fields are the
    HOP's first cred, for shape parity with ``make_bed_target`` (``spawn_otto``
    reads ``root``/``lab`` only).
    """
    hop = host_data(hop_element)
    cred = hop["creds"][0]
    return ChaosTarget(
        root=PROJECT_E2E,
        lab=lab,
        host_id=host_id_of(guest_element),
        ssh_host=hop["ip"],
        ssh_port=22,
        ssh_username=cred["login"],
        ssh_client_key=None,
        ssh_password=cred["password"],
    )


@contextlib.contextmanager
def _hop_context(hop_element: str, lab_name: str) -> Iterator[None]:
    """Install the hop host in the active context, then put it back.

    An in-process guest host built from lab data carries ``hop: <element>``,
    and that id is resolved against the ACTIVE context's lab when the
    connection dials -- a pytest process has no such lab unless one is
    installed. Mirrors the discipline in
    ``tests/integration/busybox_bed/conftest.py`` (install, then restore the
    whole run state the install set), with one deliberate difference: the
    scope is ONE probe, not one module. The chaos lane's other modules build their own
    hosts and spawn their own subprocesses against the unix bed, and a
    session-scoped context carrying a two-host lab would sit under all of
    them for the whole run. Nothing here is worth that blast radius, and the
    ContextVar is process-global state -- the narrowest scope that works is
    the right one.
    """
    lab = Lab(name=lab_name)
    data = host_data(hop_element)
    lab.add_host(
        UnixHost(
            ip=data["ip"],
            element=element_for(hop_element),
            creds=[Cred(**c) for c in data["creds"]],
            board=data.get("board"),
            is_virtual=True,
            log=LogMode.QUIET,
        )
    )
    with preserved_run_state():
        set_context(OttoContext(lab=lab))
        yield


def _hopped_probe(guest_element: str, lab_name: str, coro_factory):
    """Run ``await coro_factory(guest)`` on a fresh guest host in a fresh loop, inside its hop.

    Carries the same G5 contract as :func:`run_probe`: a non-ok
    :class:`~otto.result.Result` coming back RAISES (host-named, status-quoted)
    rather than being returned, because an oracle reading ``.value`` off a dead
    probe reports "clean bed" for exactly the failures chaos manufactures.
    Factories must return the ``Result`` itself, never unwrap ``.value`` first.

    The host is built by the FACTORY from the committed lab entry, never by
    hand: ``UnixHost(...)`` direct would default ``term="ssh"`` on a guest
    whose committed record puts ``telnet`` first, and the point of driving the
    committed record is that the chaos arm exercises what an ``otto host`` user
    exercises. The hop is the record's own ``hop`` field.
    """
    data = host_data(guest_element)

    async def _go():
        host = create_host_from_dict(data, lab_name=lab_name, element=element_for(guest_element))
        try:
            out = await coro_factory(host)
        finally:
            await host.close()
        if isinstance(out, Result):
            check_probe_result(guest_element, out)
        return out

    with _hop_context(data["hop"], lab_name):
        return asyncio.run(_go())


def _exec_factory(cmd: str, timeout: float):
    """The ``coro_factory`` of every checked guest text read: ``exec`` *cmd* quietly.

    Returns the ``Result`` itself, so the probe's status check vets it before
    the twins unwrap ``.value`` (``busybox_probe_text``, ``chaos_guest_probe_text``).
    """
    return lambda h: h.exec(cmd, timeout=timeout, log=LogMode.QUIET)


@contextlib.contextmanager
def busybox_hop_context() -> Iterator[None]:
    """:func:`_hop_context` for the BusyBox guest: install ``test1`` as the active lab's host."""
    with _hop_context(BUSYBOX_HOP_ELEMENT, "busybox"):
        yield


def busybox_probe(coro_factory):
    """Run ``await coro_factory(guest)`` on a fresh bb1350 host; see :func:`_hopped_probe`."""
    return _hopped_probe(BUSYBOX_CHAOS_ELEMENT, "busybox", coro_factory)


def busybox_probe_text(cmd: str, *, timeout: float = _PROBE_TIMEOUT) -> str:
    """The one spelling for a checked text read off the BusyBox guest.

    ``exec`` on a ``term: telnet`` host has no stateless channel to open, so
    it routes through a pooled shell session -- which means every probe here
    is also a fresh LOGIN through the hop, and its success is itself evidence
    the guest's console still serves a shell.
    """
    return busybox_probe(_exec_factory(cmd, timeout)).value


def _prove_guest_shell(probe_text, *, marker: str, where: str, remedy: str) -> None:
    """Fail LOUD, guest-named and with the operator remedy, unless the guest echoes *marker*.

    Reachability is proven by asking for a shell, not by a TCP connect: a
    connect proves only that a daemon bound its port, and says nothing about
    the login path. *where* names the guest, its hop and the address; *remedy*
    names the operator recovery.
    """
    try:
        answer = probe_text(f"echo {marker}")
    except Exception as exc:
        raise RuntimeError(
            f"{where} will not serve a shell: {exc!r}. {remedy} "
            "(Chaos lane fails loud on a down bed -- it never skips.)"
        ) from exc
    assert marker in answer, (
        f"{where} answered a login but not the marker: {answer!r} -- console wedged?"
    )


@contextlib.contextmanager
def busybox_bed() -> Iterator[BusyboxChaosBed]:
    """Prove the guest serves a shell, then yield the bed handle.

    NO LEASE, deliberately: ``lease_unix_host`` serializes pool consumers off
    one unix VM, and the guest is not in that pool. What could contend for
    it is ``tests/integration/busybox_bed``'s own rows, and nothing brings
    those two together — every catch-all lane excludes ``chaos`` by marker
    (``M_UNIX`` in the Makefile, the ``tests_*`` nox sessions) and the chaos
    lane itself is path-scoped to ``tests/e2e/chaos`` (``nox -s chaos``), so
    the two suites cannot co-run against ``bb1350``.

    Reachability is proven by asking for a shell, not by a TCP connect. A
    connect proves only that ``telnetd`` has bound 23; it says nothing about
    the login path, and a guest that has just been restarted by
    ``Restart=always`` serves the same banner as a healthy one. Fails LOUD and
    guest-named on a down bed, never skips, and names the operator remedy.
    """
    guest = host_data(BUSYBOX_CHAOS_ELEMENT)
    ip = guest["ip"]
    _prove_guest_shell(
        busybox_probe_text,
        marker="BUSYBOX-CHAOS-READY",
        where=f"BusyBox bed guest {BUSYBOX_CHAOS_ELEMENT} (via {BUSYBOX_HOP_ELEMENT} at {ip}:23)",
        remedy=(
            "Is the bed provisioned and up? Recover with `make qemu-restart`; "
            "check with `scripts/lab_health.py`."
        ),
    )
    yield BusyboxChaosBed(
        element=BUSYBOX_CHAOS_ELEMENT,
        version=guest["sw_version"],
        ip=ip,
        target=busybox_target(),
    )


# ---------------------------------------------------------------------------
# The chaos reboot guest -- the lane's third target, hopped through test4
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ChaosGuestBed:
    element: str  # lab element name, always CHAOS_GUEST_ELEMENT
    host_id: str  # CHAOS_GUEST_HOST_ID, as link endpoints and --from spell it
    hop: str  # the lab VM it is reached through, CHAOS_GUEST_HOP
    ip: str  # the guest's address on the hop's /30, routable from the hop only
    target: ChaosTarget  # aim the otto subprocess here


def chaos_guest_target() -> ChaosTarget:
    """``ChaosTarget`` aiming an otto subprocess at the reboot guest, lab ``chaos``.

    The SUT is ``tests/project_e2e`` unchanged (its lab source compiles
    ``tech1/lab.json``). The ``ssh_*`` fields are the HOP's, for shape parity
    with :func:`busybox_target`: ``spawn_otto`` reads ``root``/``lab`` only, and
    the oracle for this target is :func:`chaos_guest_probe_text`.
    """
    return _hopped_target(CHAOS_LAB, CHAOS_GUEST_ELEMENT, CHAOS_GUEST_HOP)


def chaos_guest_probe(coro_factory):
    """Run ``await coro_factory(guest)`` on a fresh guest host inside the hop context."""
    return _hopped_probe(CHAOS_GUEST_ELEMENT, CHAOS_LAB, coro_factory)


def chaos_guest_probe_text(cmd: str, *, timeout: float = _PROBE_TIMEOUT) -> str:
    """The one spelling for a checked text read off the reboot guest."""
    return chaos_guest_probe(_exec_factory(cmd, timeout)).value


def observe_guest_tunnel_processes() -> list[Observation]:
    """The guest's tagged tunnel processes, by the sentinel oracle (never ``pgrep``).

    ``observe_tunnel_processes`` cannot list a hopped guest (``build_bed_host``
    makes a bare ``UnixHost`` with no lab), so this reads the same
    ``DISCOVERY_PS_COMMAND`` through the hop and decodes it with the production
    ``parse_process_discovery``. Plain, not ``async``: the hopped probe runs its
    own loop, so a caller must not be inside one (``asyncio.run`` would nest).
    """
    return parse_process_discovery(chaos_guest_probe_text(DISCOVERY_PS_COMMAND))


def chaos_guest_boot_id() -> str:
    """The guest kernel's boot id: it changes exactly when the guest rebooted."""
    return chaos_guest_probe_text("cat /proc/sys/kernel/random/boot_id").strip()


_BOOT_POLL_S = 10.0


def wait_for_new_boot(old: str, *, timeout: float = 600.0) -> str:
    """Wait until the guest's boot id differs from *old*; return the new one.

    The 600 s default bounds a new boot id after a guest REBOOT, which may first
    sit out a shutdown hold of up to 90 s (the guest's ``reboot.target`` bound)
    before the TCG boot; ``scripts/lab_health.py``'s ``_GUEST_BOOT_S`` (300 s)
    bounds only sshd answering after a unit restart, so it is the shorter one.

    The guest is unreachable for part of every reboot, so a failing probe is
    "not yet", never an error (``wait_for`` propagates predicate exceptions, so
    the predicate swallows its own). Expiry raises ``WaitTimeoutError``.
    """
    seen: list[str] = []

    def _rebooted() -> bool:
        try:
            current = chaos_guest_boot_id()
        except Exception:  # noqa: BLE001 -- a guest mid-reboot refuses the probe
            return False
        if current == old:
            return False
        seen.append(current)
        return True

    wait_for(
        _rebooted,
        timeout,
        interval=_BOOT_POLL_S,
        on_timeout=f"{CHAOS_GUEST_ELEMENT} boot id still {old!r} after {timeout:.0f}s",
    )
    return seen[-1]


def set_shutdown_hold(seconds: int | None) -> None:
    """Make the guest's shutdown hold for *seconds* (``None`` removes the hold).

    No sudo: the hold unit creates the directory 1777, so the login writes and
    removes its own hold file, and the bed reap can remove a leftover one.
    """
    hold = CHAOS_GUEST.hold_file
    if seconds is None:
        chaos_guest_probe_text(f"rm -f {hold}")
    else:
        chaos_guest_probe_text(f"echo {int(seconds)} > {hold}")


@contextlib.contextmanager
def chaos_guest_bed() -> Iterator[ChaosGuestBed]:
    """Prove the guest serves a shell and carries its tools, then yield the bed handle.

    NO LEASE and NO REAP of its own, for :func:`busybox_bed`'s reasons: the guest
    is not in the unix pool, and the lane's ``$(call bed_reap)`` at its start
    reaches the guest through its hop -- the one reap there is.
    """
    ip = host_data(CHAOS_GUEST_ELEMENT)["ip"]
    _prove_guest_shell(
        chaos_guest_probe_text,
        marker="CHAOS-GUEST-READY",
        where=(
            f"Chaos guest {CHAOS_GUEST_ELEMENT} (via {CHAOS_GUEST_HOP} at {ip}, "
            f"unit {CHAOS_GUEST.unit})"
        ),
        remedy=(
            "Is the bed provisioned and up? Recover with `make qemu-restart`; "
            "check with `scripts/lab_health.py`."
        ),
    )
    tools = chaos_guest_probe_text("command -v socat bash tc").split()
    assert len(tools) == 3, (
        f"Chaos guest {CHAOS_GUEST_ELEMENT} is missing a tool of `socat bash tc`: found {tools!r}"
    )
    yield ChaosGuestBed(
        element=CHAOS_GUEST_ELEMENT,
        host_id=CHAOS_GUEST_HOST_ID,
        hop=CHAOS_GUEST_HOP,
        ip=ip,
        target=chaos_guest_target(),
    )
