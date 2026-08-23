"""Leased live-bed target for the tier-3 chaos lane.

The lane's spine: lease ONE free unix host (flock, cross-worker), prove it
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

from otto.config.lab import Lab
from otto.context import OttoContext, _active, set_context
from otto.host.factory import create_host_from_dict
from otto.host.login_proxy import Cred
from otto.host.unix_host import UnixHost
from otto.link.derive import addressing_from_dict, resolve_declared_links
from otto.logger.mode import LogMode
from otto.result import Result
from tests._fixtures._host_pool import lease_unix_host
from tests._fixtures.bed_hygiene import _PROBE_TIMEOUT, check_probe_result
from tests._fixtures.labdata import host_data, lab_data_path
from tests._fixtures.tunnel_bed import assert_reachable, build_bed_host
from tests.e2e._otto_subprocess import REPO_E2E
from tests.integration.chaos._target import ChaosTarget, make_bed_target


@dataclasses.dataclass(frozen=True)
class ChaosBed:
    element: str  # lab element name, e.g. "test2"
    ip: str  # management ip from tech1/lab.json
    target: ChaosTarget  # aim the otto subprocess here


@contextlib.contextmanager
def leased_bed(lock_dir: Path) -> Iterator[ChaosBed]:
    """Lease a free unix host; probe reachability; yield the bed handle."""
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
    return (out.value or "").strip()


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
    # Skip records this process cannot resolve, mirroring JsonFileLabRepository:
    # addressing_from_dict validates through the profile/frame registries, and
    # tech1/lab.json's `zephyr27_fat` declares `command_frame: zephyr-inline`, which
    # only a SUT repo's init modules register — never the pytest process.
    hosts = {}
    for h in data["hosts"]:
        try:
            host_id, addressing = addressing_from_dict(h)
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


def unix_link_id() -> str:
    """The declared test1<->test2 eth2 link's id."""
    return declared_link_id("test1", "test2")


def tunnel_target(sut_dir) -> ChaosTarget:
    """ChaosTarget for CLI-driven tunnel commands against `cli_sut_dir`'s isolated SUT.

    Hoisted from two former per-module copies (``test_tunnel_link_chaos.py``,
    ``test_reboot_chaos.py`` — verified byte-identical before the hoist).
    ``spawn_otto`` only ever reads ``target.sut_dir``/``target.lab`` (see
    ``tests/integration/chaos/_driver.py::_otto_env``) — the ssh_* fields
    exist to feed the asyncssh oracle in ``tests.integration.chaos._target``,
    unused here since tunnel-process reconciliation goes through
    ``observe_tunnel_processes``/``run_probe`` instead. Populated from
    test1's real creds anyway, for shape-parity with ``make_bed_target``.
    """
    test1 = host_data("test1")
    cred = test1["creds"][0]
    return ChaosTarget(
        sut_dir=sut_dir,
        lab="unix",
        host_id="test1",
        ssh_host=test1["ip"],
        ssh_port=22,
        ssh_username=cred["login"],
        ssh_client_key=None,
        ssh_password=cred["password"],
    )


def assert_eth2_netem_free(what: str) -> None:
    """Assert both eth2 endpoints (test1, test2) carry no netem qdisc.

    Hoisted from two former per-module copies (``test_tunnel_link_chaos.py``,
    ``test_reboot_chaos.py`` — verified behaviorally identical before the
    hoist).
    """
    for elem in ("test1", "test2"):
        qdisc = probe_text(elem, "tc qdisc show dev eth2")
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

BUSYBOX_CHAOS_HOST_ID = f"{BUSYBOX_CHAOS_ELEMENT}_qemu"
"""The guest's HOST ID (``element_board``), which is what link endpoints,
``--from`` and ``otto host`` all name — as distinct from the element name the
probe helpers above take."""

BUSYBOX_GUEST_NETDEV = "eth0"
"""The guest's only netdev: its own end of the /30. Also, and this is the
point of naming it, the netdev its management address lives on."""

BUSYBOX_TAP_NETDEV = f"bbeth-{BUSYBOX_CHAOS_ELEMENT.removeprefix('bb')}"
"""test1's end of the same wire. Derived from the element rather than typed,
so the anchor and its TAP cannot drift apart."""


def busybox_link_id() -> str:
    """The declared ``test1:bbeth-1350 <-> bb1350_qemu:eth0`` link's id.

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

    The SUT is ``tests/repo_e2e`` unchanged — its one ``[[lab.sources]]``
    entry already compiles ``tech1/lab.json``, which is where the five guest
    records and their ``test1`` hop live, so the lab leg is a plain
    ``-l busybox`` and no generated SUT is needed (unlike the hop-routed
    ``chaosdrop`` and console targets, which exist only because they need lab
    data this repo does not commit).

    ``spawn_otto`` reads ``sut_dir``/``lab`` and nothing else (see
    ``tests/integration/chaos/_driver.py::_otto_env``); the ``ssh_*`` fields
    exist to feed the asyncssh oracle in ``tests.integration.chaos._target``,
    which CANNOT be used here — the guest has no sshd at all, by construction.
    They are populated from the HOP's real creds for shape parity with
    ``make_bed_target``, and the oracle for this target is
    :func:`busybox_probe_text`, which goes through otto's own telnet-over-hop
    path because that is the only path there is.
    """
    guest = host_data(BUSYBOX_CHAOS_ELEMENT)
    hop = host_data(BUSYBOX_HOP_ELEMENT)
    cred = hop["creds"][0]
    return ChaosTarget(
        sut_dir=REPO_E2E,
        lab="busybox",
        host_id=f"{guest['element']}_{guest['board']}",
        ssh_host=hop["ip"],
        ssh_port=22,
        ssh_username=cred["login"],
        ssh_client_key=None,
        ssh_password=cred["password"],
    )


@contextlib.contextmanager
def busybox_hop_context() -> Iterator[None]:
    """Install the hop host in the active context, then put it back.

    An in-process guest host built from lab data carries ``hop:
    test1``, and that id is resolved against the ACTIVE context's lab
    when the connection dials — a pytest process has no such lab unless one
    is installed. Mirrors the discipline in
    ``tests/integration/busybox_bed/conftest.py`` (snapshot the ContextVar,
    install, restore), with one deliberate difference: the scope is ONE
    probe, not one module. The chaos lane's other modules build their own
    hosts and spawn their own subprocesses against the unix bed, and a
    session-scoped context carrying a two-host lab would sit under all of
    them for the whole run. Nothing here is worth that blast radius, and the
    ContextVar is process-global state — the narrowest scope that works is
    the right one.
    """
    snapshot = _active.get()
    lab = Lab(name="busybox_chaos")
    data = host_data(BUSYBOX_HOP_ELEMENT)
    lab.add_host(
        UnixHost(
            ip=data["ip"],
            element=data["element"],
            creds=[Cred(**c) for c in data["creds"]],
            board=data.get("board"),
            is_virtual=True,
            log=LogMode.QUIET,
        )
    )
    set_context(OttoContext(lab=lab))
    try:
        yield
    finally:
        _active.set(snapshot)


def busybox_probe(coro_factory):
    """Run ``await coro_factory(guest)`` on a fresh guest host in a fresh loop.

    The guest twin of :func:`run_probe`, and it carries the same G5 contract:
    a non-ok :class:`~otto.result.Result` coming back RAISES (host-named,
    status-quoted) rather than being returned, because an oracle reading
    ``.value`` off a dead probe reports "clean bed" for exactly the failures
    chaos manufactures. Factories must return the ``Result`` itself (or use
    :func:`busybox_probe_text`), never unwrap ``.value`` first.

    The host is built by the FACTORY from the committed lab entry, never by
    hand: ``UnixHost(...)`` direct would default ``term="ssh"`` on a guest
    that has no sshd, and the point of driving the committed record is that
    the chaos arm exercises what an ``otto host`` user exercises.
    """

    async def _go():
        host = create_host_from_dict(host_data(BUSYBOX_CHAOS_ELEMENT), lab_name="busybox")
        try:
            out = await coro_factory(host)
        finally:
            await host.close()
        if isinstance(out, Result):
            check_probe_result(BUSYBOX_CHAOS_ELEMENT, out)
        return out

    with busybox_hop_context():
        return asyncio.run(_go())


def busybox_probe_text(cmd: str, *, timeout: float = _PROBE_TIMEOUT) -> str:
    """The one spelling for a checked text read off the guest.

    :func:`busybox_probe` + ``exec`` + the status check + the ``.value``
    unwrap, so guest-reading helpers cannot drift back into the unchecked
    ``(result.value or "")`` shape.

    ``exec`` on a ``term: telnet`` host has no stateless channel to open, so
    it routes through a pooled shell session — which means every probe here
    is also a fresh LOGIN through the hop, and its success is itself evidence
    the guest's console still serves a shell.
    """
    out = busybox_probe(lambda h: h.exec(cmd, timeout=timeout, log=LogMode.QUIET))
    return (out.value or "").strip()


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
    try:
        answer = busybox_probe_text("echo BUSYBOX-CHAOS-READY")
    except Exception as exc:
        raise RuntimeError(
            f"BusyBox bed guest {BUSYBOX_CHAOS_ELEMENT} "
            f"(via {BUSYBOX_HOP_ELEMENT} at {ip}:23) will not serve a shell: {exc!r}. "
            "Is the bed provisioned and up? Recover with `make qemu-restart`; "
            f"check with `scripts/lab_health.py`. (Chaos lane fails loud on a "
            "down bed -- it never skips.)"
        ) from exc
    assert "BUSYBOX-CHAOS-READY" in answer, (
        f"BusyBox bed guest {BUSYBOX_CHAOS_ELEMENT} (via {BUSYBOX_HOP_ELEMENT} at "
        f"{ip}:23) answered a login but not the marker: {answer!r} -- console wedged?"
    )
    yield BusyboxChaosBed(
        element=BUSYBOX_CHAOS_ELEMENT,
        version=guest["sw_version"],
        ip=ip,
        target=busybox_target(),
    )
