"""Routing regression tests for ``scripts/lab_health.py``.

Guards the os_type-literal rot exposed when commit 41cf70c renamed the lab
data's console hosts from ``os_type: "embedded"`` to ``os_type: "zephyr"``: the
health probe kept routing only ``os_type == "embedded"`` to the console check,
so every Zephyr host fell into the SSH check and crashed on the missing
``creds`` key. The contract under test is reach-path, not a literal: a host
without its own ``creds`` must be probed via the hop/console path, never the
direct-SSH path.

The BusyBox bed widened that contract without changing its nature — the reach
path is still read off the entry's shape, but a hop now outranks creds. Those
guests are ``os_type: "unix"`` WITH their own root login, yet their ``ip`` is
``127.0.0.1`` on their hop, so an SSH probe would silently interrogate whatever
machine runs this script and report a healthy bed no matter what the guests are
doing.
"""

import socket
import socketserver
import subprocess
import sys
import threading
import time
from typing import ClassVar

import pytest

from scripts import lab_health
from scripts.lab_health import (
    _CONSOLE_PROBE,
    _CONSOLE_STATE_PROBE,
    DEFAULT_HOSTS,
    _hop_index,
    _load_hosts,
    _print_report,
)

pytestmark = pytest.mark.interpreter_agnostic


def _route_probes(monkeypatch):
    """Run ``_print_report`` over the real lab data with both probes stubbed,
    recording which host each probe was asked to handle."""
    hosts = _load_hosts(DEFAULT_HOSTS)
    hops = _hop_index(hosts)
    seen: dict[str, list[str]] = {"unix": [], "embedded": []}

    def fake_unix(host):
        seen["unix"].append(host["element"])
        return {"ok": True, "status": "up", "info": ""}

    def fake_embedded(host, hops):
        seen["embedded"].append(host["element"])
        return {"ok": True, "status": "up", "info": ""}

    monkeypatch.setattr(lab_health, "_check_unix", fake_unix)
    monkeypatch.setattr(lab_health, "_check_embedded", fake_embedded)
    monkeypatch.setattr(
        lab_health,
        "_check_console",
        lambda host, hops, *, logout: {"ok": True, "status": "AT-LOGIN", "info": ""},
    )
    _print_report(hosts, hops)
    return hosts, seen


def test_no_credless_host_is_sent_to_the_ssh_probe(monkeypatch):
    """The SSH probe dereferences ``host['creds']``; routing a credential-less
    host there is exactly the 41cf70c crash. Such hosts must use the console
    probe instead."""
    hosts, seen = _route_probes(monkeypatch)
    assert any("creds" not in h for h in hosts), (
        "fixture sanity: expected some credential-less hosts in the lab data — "
        "with none, every assertion below is skipped and this guard cannot fail"
    )
    for host in hosts:
        if "creds" not in host:
            assert host["element"] not in seen["unix"], (
                f"{host['element']} (os_type={host.get('os_type')!r}) was routed to "
                "the SSH probe but carries no creds"
            )
            assert host["element"] in seen["embedded"]


def test_zephyr_hosts_route_to_console_probe(monkeypatch):
    hosts, seen = _route_probes(monkeypatch)
    zephyr = [h["element"] for h in hosts if h.get("os_type") == "zephyr"]
    assert zephyr, "fixture sanity: expected some zephyr hosts in the lab data"
    assert set(zephyr) <= set(seen["embedded"])
    assert not (set(zephyr) & set(seen["unix"]))


def test_unix_hosts_route_to_ssh_probe(monkeypatch):
    """Creds AND no hop is the direct-SSH shape. The `not h.get("hop")` half
    arrived with the BusyBox bed: those guests are ``os_type: "unix"`` and carry
    their own creds, but their ``ip`` is their hop's loopback, so the SSH path
    would probe the machine running this script. They are pinned to the console
    path by the companion assertion below, not merely excluded from this one."""
    hosts, seen = _route_probes(monkeypatch)
    ssh_hosts = {h["element"] for h in hosts if "creds" in h and not h.get("hop")}
    assert ssh_hosts, "fixture sanity: expected some credentialed hopless unix hosts"
    assert ssh_hosts <= set(seen["unix"])
    assert not (ssh_hosts & set(seen["embedded"]))

    hop_fronted = {h["element"] for h in hosts if "creds" in h and h.get("hop")}
    assert hop_fronted, (
        "fixture sanity: expected cred-carrying hop-fronted guests (the BusyBox "
        "bed) in the lab data — with none, the routing half this test guards is "
        "never exercised"
    )
    assert hop_fronted <= set(seen["embedded"])
    assert not (hop_fronted & set(seen["unix"]))


def test_a_cred_carrying_guest_behind_a_hop_is_probed_via_the_hop(monkeypatch):
    """bb guests have creds AND a hop, so routing must take the console path.

    Their addresses live on a /30 that exists only on the hop's own TAP, so
    ``_is_ssh_host`` treating them as ordinary Unix hosts would aim ssh at an
    address the machine running this script cannot reach at all.

    The entry declares NO ``telnet_options``, exactly as the committed bed
    entries do since the guests moved onto real NICs — so this also covers the
    default-23 branch of the port lookup, which is the branch the whole BusyBox
    bed now depends on. (Port HONORING is pinned separately, by the ARM serial
    console's 2323 in
    ``test_check_embedded_appends_creds_only_for_a_guest_that_has_its_own``.)
    """
    guest = {
        "ip": "198.51.100.1",
        "element": "bb1161",
        "os_type": "unix",
        "hop": "test1",
        "creds": [{"login": "root", "password": "otto"}],
    }
    assert lab_health._is_ssh_host(guest) is False
    hop = {
        "ip": "10.10.200.11",
        "element": "test1",
        "creds": [{"login": "vagrant", "password": "vagrant"}],
    }
    assert lab_health._is_ssh_host(hop) is True

    seen = {}

    def fake_run_ssh(ip, user, password, cmd, timeout=25.0):
        seen["ip"] = ip
        seen["cmd"] = cmd
        return 0, "OK login", ""

    monkeypatch.setattr(lab_health, "_run_ssh", fake_run_ssh)
    res = lab_health._check_embedded(guest, {"test1": hop})
    assert seen["ip"] == "10.10.200.11"  # probed FROM the hop
    assert " 198.51.100.1 23 " in seen["cmd"]  # the guest's own address, on :23
    assert res == {"ok": True, "status": "up", "info": "login prompt"}


def test_the_console_probe_reports_a_login_prompt_as_ok():
    """Drive the real probe script against a scripted telnet-ish server.

    The fake serves what a BusyBox guest's telnetd actually answers with — an
    IAC option negotiation byte run followed by the login banner — so the probe
    is exercised end to end, script text included, rather than through a stub
    of its own output.
    """
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def serve():
        conn, _ = srv.accept()
        with conn:
            conn.recv(64)
            conn.sendall(b"\xff\xfd\x18bb1161 login: ")

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _CONSOLE_PROBE, "127.0.0.1", str(port)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        # Bounded: the server has already answered by the time the probe exits,
        # so this only reaps the thread — it is not a wait FOR the answer.
        thread.join(timeout=5)
    finally:
        srv.close()
    assert proc.stdout.strip() == "OK login"


def test_restart_units_include_the_busybox_bed(monkeypatch):
    """``make qemu-restart`` is the bed's recovery command; a glob naming only
    the zephyr units would restart nothing at all on test1."""
    captured = {}

    def fake_run_ssh(ip, user, password, cmd, timeout=25.0):
        captured["cmd"] = cmd
        return 0, "", ""

    monkeypatch.setattr(lab_health, "_run_ssh", fake_run_ssh)
    hosts = [
        {
            "ip": "10.10.200.11",
            "element": "test1",
            "creds": [{"login": "vagrant", "password": "vagrant"}],
        },
        {
            "ip": "127.0.0.1",
            "element": "bb1161",
            "hop": "test1",
            "creds": [{"login": "root", "password": "otto"}],
        },
    ]
    rc = lab_health._restart_qemu(hosts, lab_health._hop_index(hosts))
    assert rc == 0
    assert "busybox-qemu-*.service" in captured["cmd"]
    assert "zephyr-qemu-*.service" in captured["cmd"]


# ---------------------------------------------------------------------------
# Authenticated uptime (spec §6 amendment)
#
# "OK login" is a real answer, but a thin one: it says a telnetd is listening
# and got as far as printing a prompt. It cannot distinguish a guest that has
# been serving that prompt happily for a day from one that panicked and was
# restarted by systemd forty seconds ago, and that restart loop is the failure
# mode the bed actually has (``Restart=always``, because a guest panic exits
# qemu with status 0 under ``-no-reboot``). The BusyBox entries carry their own
# root creds, so the probe can log in and read ``/proc/uptime`` — the same
# number the Zephyr consoles report, arriving through a different door.
#
# The Zephyr consoles carry NO creds of their own (they borrow the hop's), and
# that absence is what keeps them on the two-argument call. This is the half of
# the change with something to lose: the console probe is one script shared by
# both families, and a login attempt against a Zephyr shell would type a
# username into it. The tests below pin both directions — the bb shape gets
# creds appended, the Zephyr shape gets a command byte-identical to the one it
# got before this existed.
# ---------------------------------------------------------------------------


def _scripted_console(script, *, hold=False):
    """Serve one connection through *script*; return (port, server, thread, tail).

    *script* is a list of steps: a ``bytes`` step is written to the client, and
    ``None`` means "read whatever the client says next and discard it". That
    covers every console shape these tests need — banner, credential challenge,
    prompt, command echo — without a second fake server per case.

    *hold* keeps the connection open after the script runs out instead of
    closing it, which is what makes *tail* collectable — and it costs real
    seconds, so it is opt-in. A probe reading from a held-open silent socket
    has no way to know the conversation is over and waits out its whole budget;
    against a server that closes, the same read ends on EOF immediately. Two of
    the tests below want the first (a console that goes quiet IS the wedge they
    describe) and the rest only want their script delivered, so the default is
    to close. Measured on this file: the credential-less Zephyr case ran 5.25 s
    held open and 1.2 s closed, for an assertion that never looked at *tail*.

    *tail* is a ``bytearray`` collecting everything the probe says AFTER the
    script runs out, up to the moment it hangs up (empty unless *hold*). That is the observable the
    refusal guard needs: what the probe must not do at a console that never
    gave it a shell is TYPE AT IT, and typing is not visible in the probe's
    stdout — a bad-credential console produces "OK login" whether the probe
    stopped at the refusal or barrelled on and issued a command into the login
    prompt. Reading the wire, not the verdict, is what tells those two apart
    (measured: with only the stdout assertion, deleting the refusal guard
    outright left all eleven tests green).
    """
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    tail = bytearray()

    def serve():
        # One try around the whole conversation: any OSError here means the
        # probe hung up, which is a normal end for every script below and never
        # something a test asserts on. Per-step handlers would say the same
        # thing five times (and trip PERF203).
        try:
            conn, _ = srv.accept()
            with conn:
                for step in script:
                    if step is None:
                        conn.recv(256)
                    else:
                        conn.sendall(step)
                if not hold:
                    return
                # Hold the connection open until the probe hangs up, recording
                # whatever else it says. Closing instead hands the probe an EOF
                # it reads as "the console stopped talking" — a different
                # stimulus from a console that stays up and says nothing, which
                # is what the callers passing hold=True are asking about.
                while True:
                    chunk = conn.recv(256)
                    if not chunk:
                        return
                    tail.extend(chunk)
        except OSError:
            return

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return port, srv, thread, tail


def _run_probe(port, *creds, timeout=60):
    """Drive the real ``_CONSOLE_PROBE`` script as a subprocess, as the hop does."""
    proc = subprocess.run(
        [sys.executable, "-c", _CONSOLE_PROBE, "127.0.0.1", str(port), *creds],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return proc.stdout.strip()


def test_an_authenticated_console_probe_reports_the_guests_own_uptime():
    """Given creds, the probe logs in and reads ``/proc/uptime``.

    The scripted server is a BusyBox guest's actual login sequence: option
    negotiation, a login banner, a ``Password:`` challenge, the ash banner and
    prompt, then the two-float ``/proc/uptime`` line echoed after the command.
    ``12.345`` seconds is 12345 ms, which is the number ``_check_embedded``
    then renders as ``up 12s`` — so this pins the whole chain, not just the
    regex.
    """
    port, srv, thread, _tail = _scripted_console(
        [
            b"\xff\xfd\x18bb1161 login: ",
            None,  # the username
            b"\r\nPassword: ",
            None,  # the password
            b"\r\n\r\nBusyBox v1.35.0 built-in shell (ash)\r\n\r\n~ # ",
            None,  # `cat /proc/uptime`
            b"cat /proc/uptime\r\n12.345 98.765\r\n~ # ",
        ]
    )
    try:
        out = _run_probe(port, "root", "otto")
        thread.join(timeout=5)
    finally:
        srv.close()
    assert out == "OK 12345"


def test_a_console_that_never_challenges_for_a_password_gets_no_password():
    """A login that stalls degrades to the unauthenticated sighting — quietly.

    A guest whose telnetd answers ``login:`` and then goes silent is alive and
    reachable, which is exactly what ``OK login`` has always meant. Reporting it
    as WEDGED or DOWN because the *new* half of the probe could not finish would
    turn a working guest into a false alarm and make this amendment a regression
    for every case it cannot improve.

    The second assertion is the one that can fail. ``OK login`` is reached by
    more than one route — a read that times out, a read that hits EOF, and (if
    the timeout arm ever stopped reporting "nothing happened") a read that
    wrongly claims it saw the challenge — and all of them print the same word.
    What separates them is whether the probe went on to TYPE THE PASSWORD at a
    console that never asked for one. Positive control (2026-08-21): making
    ``read_until``'s recv-timeout arm return a match instead of ``None`` leaves
    the verdict assertion green and reds this one with ``b'otto\\r\\n'`` — a
    credential emitted onto a console that never challenged for it.

    Mutate the RECV-TIMEOUT arm, not the deadline check above it. A silent
    peer never gets as far as ``left <= 0``: ``recv`` blocks for the whole
    remaining budget and raises, so the ``except`` is the arm this scenario
    actually runs. Mutating the deadline check instead leaves all eleven tests
    green and looks like a hole in this guard — it is a hole in the mutation.

    ``hold=True`` is load-bearing for that: the server stays up and silent (a
    wedged guest holds its socket open, it does not close it), so the tail
    records anything the probe says next. The short step budget only shortens
    the wait — see the argv[5] note in ``_CONSOLE_PROBE``.
    """
    port, srv, thread, tail = _scripted_console([b"\xff\xfd\x18bb1161 login: ", None], hold=True)
    try:
        out = _run_probe(port, "root", "otto", "1.5")
        thread.join(timeout=5)
    finally:
        srv.close()
    assert out == "OK login"
    assert bytes(tail) == b"", (
        f"the probe sent {bytes(tail)!r} to a console that never challenged for a "
        "password — the credential went onto the wire on the strength of a read "
        "that returned nothing"
    )


@pytest.mark.parametrize(
    "announcement",
    [
        pytest.param(b"\r\nLogin incorrect\r\nbb1161 login: ", id="incorrect_then_reprompt"),
        pytest.param(b"\r\nbb1161 login: ", id="bare_reprompt"),
    ],
)
def test_a_refused_login_is_reported_as_refused_and_never_typed_at(announcement):
    """``Login incorrect`` is a live console, and it is a BROKEN CREDENTIAL BAKE.

    Two claims, and they fail for different reasons.

    The VERDICT is ``OK refused`` — spec §6's requirement that a bad
    ``/etc/shadow`` in an image fail ``vm-health`` by name rather than surface
    later as the first bed test's login failure. The creds this probe types are
    the ones committed in the guest's own lab entry, so a refusal is not an
    ambiguous sighting: the image and the lab data disagree, deterministically,
    and no amount of waiting will change it. That is what separates it from the
    silent console two tests below, which keeps ``OK login``. Positive control
    (2026-08-21): restoring the collapsed branch (``if hit != b"#": print("OK
    login")``) reds both parameters here, and ONLY here — every other test in
    this file stays green, which is exactly the hole this closes.

    Both announcement shapes are driven because the probe watches for two
    needles: BusyBox's ``Login incorrect`` and the bare re-prompt a console can
    answer with instead. A guard that only ever saw the first would leave the
    second needle unpinned and free to be deleted.

    The WIRE is the subtler half and is still asserted, because a correct
    verdict does not prove the probe behaved: ``cat /proc/uptime`` typed at a
    ``login:`` prompt is a bogus username and the ``exit`` after it a bogus
    password, i.e. this probe manufacturing failed-login records on a device it
    was only asked to look at. Positive control (2026-08-21), chosen so it
    separates the two claims: issuing the uptime command BEFORE reading the
    refusal needle — the optimistic-pipelining shape a refactor would reach for
    — leaves the verdict assertion green and reds this one with
    ``b'cat /proc/uptime\\r\\n'``.
    """
    port, srv, thread, tail = _scripted_console(
        [
            b"\xff\xfd\x18bb1161 login: ",
            None,
            b"\r\nPassword: ",
            None,
            announcement,
        ],
        hold=True,  # `tail` is the second assertion; without this there is nothing to collect
    )
    try:
        out = _run_probe(port, "root", "wrong")
        thread.join(timeout=5)
    finally:
        srv.close()
    assert out == "OK refused", (
        f"a console that refused the entry's own committed creds reported {out!r} — "
        "a broken credential bake must not read as the benign unauthenticated state"
    )
    assert bytes(tail) == b"", (
        f"the probe kept typing at a console that refused its login: {bytes(tail)!r}"
    )


def test_a_console_silent_after_the_password_keeps_the_login_fallback():
    """The other half of the refusal split, and the one with something to lose.

    A guest that challenges for a password, takes it, and then says nothing has
    told this probe nothing about its credentials: the shell may be slow, the
    console may be wedged, the read budget may simply be short. Reporting that
    as ``BAD-CREDS`` would invent a broken image bake out of a timeout — the
    false alarm the spec's own wording guards against by keeping the
    unauthenticated fallback for every unanswered login.

    So this is the discriminator for the branch above: it is the case a
    verdict split done carelessly (``if hit != b"#": print("OK refused")``)
    gets wrong, and it reds under exactly that mutation while the refusal test
    stays green.

    ``hold=True`` is load-bearing — a wedged guest holds its socket open rather
    than closing it, and closing would hand the probe an EOF, a different
    stimulus. The short step budget only shortens the wait (see the argv[5]
    note in ``_CONSOLE_PROBE``).
    """
    port, srv, thread, _tail = _scripted_console(
        [
            b"\xff\xfd\x18bb1161 login: ",
            None,
            b"\r\nPassword: ",
            None,
        ],
        hold=True,
    )
    try:
        out = _run_probe(port, "root", "otto", "1.5")
        thread.join(timeout=5)
    finally:
        srv.close()
    assert out == "OK login", (
        f"a console that went quiet after the password reported {out!r} — silence is "
        "not a credential verdict, and calling it one turns a slow guest into a false "
        "alarm about its image"
    )


def test_the_zephyr_uptime_path_is_unchanged_without_creds():
    """Two arguments, ``kernel uptime``, ``Uptime: N ms`` — exactly as before.

    This is the byte-identical half. The Zephyr consoles reach the same script
    the BusyBox guests do; if the login branch ever ran for them it would type
    a username at a shell that has no login to offer.
    """
    port, srv, thread, _tail = _scripted_console([b"\r\nUptime: 140860 ms\r\nuart:~$ "])
    try:
        out = _run_probe(port)
        thread.join(timeout=5)
    finally:
        srv.close()
    assert out == "OK 140860"


def test_check_embedded_appends_creds_only_for_a_guest_that_has_its_own(monkeypatch):
    """The routing half: creds on the ENTRY are what select the login branch.

    A Zephyr console has none (it borrows its hop's), so its probe command must
    carry exactly two arguments after the script — the shape it had before this
    amendment. A BusyBox guest has its own root login, so its command carries
    four. Asserted on the command text, because that string is the entire
    interface between ``_check_embedded`` and the script running on the hop.
    """
    hop = {
        "ip": "10.10.200.11",
        "element": "test1",
        "creds": [{"login": "vagrant", "password": "vagrant"}],
    }
    hops = {"test1": hop, "test4": hop}
    captured = {}

    def fake_run_ssh(ip, user, password, cmd, timeout=25.0):
        captured["cmd"] = cmd
        captured["timeout"] = timeout
        return 0, "OK login", ""

    monkeypatch.setattr(lab_health, "_run_ssh", fake_run_ssh)

    lab_health._check_embedded(
        {
            "ip": "198.51.100.1",
            "element": "bb1161",
            "hop": "test1",
            "creds": [{"login": "root", "password": "otto"}],
        },
        hops,
    )
    bb_tail = captured["cmd"].split(" 198.51.100.1 ", 1)[1]
    assert bb_tail == "23 root otto", f"BusyBox guest probe did not carry its creds: {bb_tail!r}"
    # The login path's four bounded reads can spend 30 s, so it must be given
    # more than `_run_ssh`'s 25 s default — otherwise ssh times out first and a
    # guest that is merely slow is reported as HOP-FAIL, the loudest verdict
    # for the mildest fault. Nothing else observes this wiring: drop the kwarg
    # and every other test here stays green while the false alarm only ever
    # appears against a live bed. Assert the value, not just "not the default",
    # so the constant and the budgets it is sized against move together.
    assert captured["timeout"] == 45.0, (
        f"login probe was given {captured['timeout']}s of ssh — its own reads can "
        "spend 30s, so anything at or below the 25s default reports HOP-FAIL for a "
        "slow-but-healthy guest"
    )

    lab_health._check_embedded(
        {
            "ip": "192.0.2.1",
            "element": "zephyr37_fat",
            "hop": "test4",
            "telnet_options": {"port": 2323},
        },
        hops,
    )
    zephyr_tail = captured["cmd"].split(" 192.0.2.1 ", 1)[1]
    assert zephyr_tail == "2323", (
        f"credential-less Zephyr console was handed a login: {zephyr_tail!r}"
    )
    # ...and is still called with no timeout of its own, so it keeps `_run_ssh`'s
    # default. The byte-identical claim covers the budget too, not just the argv.
    assert captured["timeout"] == 25.0, (
        f"the Zephyr console call grew a timeout of its own ({captured['timeout']}s); "
        "nothing about that path got slower"
    )


def _bb_entry(element="bb1161", ip="198.51.100.1"):
    return {
        "ip": ip,
        "element": element,
        "hop": "test1",
        "creds": [{"login": "root", "password": "otto"}],
    }


_HOP = {
    "ip": "10.10.200.11",
    "element": "test1",
    "creds": [{"login": "vagrant", "password": "vagrant"}],
}


def test_a_refused_verdict_becomes_a_not_ok_row_and_a_login_verdict_does_not(monkeypatch):
    """The mapping half of the credential-bake contract.

    ``OK refused`` and ``OK login`` differ by one word on the wire and by
    everything in the report: the first must make ``vm-health`` fail and name
    what to look at, the second must stay a healthy row. Both directions are
    asserted here because the failure this closes was the two collapsing into
    one — pinning only the new branch would leave a fix that reds the whole bed
    on every slow login looking correct.

    The info string is asserted for the LOGIN NAME, not just for not-ok: the
    row has to tell an operator which credential the image disagrees with, and
    "some guest is unhappy" is what the probe already said before this existed.
    """
    verdicts = iter(["OK refused", "OK login"])

    def fake_run_ssh(ip, user, password, cmd, timeout=25.0):
        return 0, next(verdicts), ""

    monkeypatch.setattr(lab_health, "_run_ssh", fake_run_ssh)

    refused = lab_health._check_embedded(_bb_entry(), {"test1": _HOP})
    assert refused["ok"] is False, (
        f"a console that refused the guest's committed creds reported {refused!r} — "
        "vm-health stays green and the broken bake surfaces as the first bed test's "
        "login failure, which is the outcome spec §6 exists to prevent"
    )
    assert refused["status"] == "BAD-CREDS", refused
    assert "root" in refused["info"], (
        f"the failing row does not name the login that was refused: {refused['info']!r}"
    )

    silent = lab_health._check_embedded(_bb_entry(), {"test1": _HOP})
    assert silent == {"ok": True, "status": "up", "info": "login prompt"}, (
        f"the unauthenticated fallback changed: {silent!r} — an unanswered login is a "
        "sighting, not a verdict about the image"
    )


def test_a_refused_guest_fails_the_whole_report_by_name(monkeypatch, capsys):
    """End to end over the real lab data: the exit status and the named row.

    ``vm-health`` is ``_print_report``'s return value (``main`` turns False into
    exit 1), so "fails vm-health by name" is two observations — the run is not
    green, and the table says which guest and why. Neither is visible from
    ``_check_embedded`` alone: an ``ok: False`` that some later aggregation
    swallowed would satisfy the unit test above and still ship a green bed.

    The stub answers by SHAPE, not by element: only an entry carrying its own
    creds gets the four-argument login probe, so only those commands can come
    back refused. The Zephyr consoles keep their uptime, the Unix VMs their
    stubbed clock — so what turns the report red is the bb rows and nothing
    else.
    """

    def fake_run_ssh(ip, user, password, cmd, timeout=25.0):
        return (0, "OK refused", "") if cmd.endswith(" root otto") else (0, "OK 140860", "")

    monkeypatch.setattr(lab_health, "_run_ssh", fake_run_ssh)
    monkeypatch.setattr(
        lab_health, "_check_unix", lambda host: {"ok": True, "status": "up", "info": ""}
    )

    hosts = _load_hosts(DEFAULT_HOSTS)
    hops = _hop_index(hosts)
    ok = _print_report(hosts, hops)
    table = capsys.readouterr().out

    assert ok is False, "a bed guest refusing its own committed creds left vm-health green"
    named = [
        line
        for line in table.splitlines()
        if "BAD-CREDS" in line and line.split()[0].startswith("bb")
    ]
    assert len(named) == 5, (
        f"the refusal did not surface as a named row per guest: {named!r}\n{table}"
    )
    assert all("root login refused" in line for line in named), named


# ---------------------------------------------------------------------------
# console_options (spec 2026-09-24 §7 / console-term)
#
# A host whose term is "console" carries console_options (server + port)
# instead of telnet_options: it dials the console-term's own telnet server
# (the zephyr hop, on the loopback port the ARM QEMU unit now binds to)
# rather than the guest's own address. The lab data migration to
# term: "console" is a later task (#14); until then _check_embedded must
# handle BOTH shapes, so this pins the new one without disturbing the
# telnet_options path pinned above.
# ---------------------------------------------------------------------------


def test_embedded_probe_dials_the_console_server_port_when_console_options_are_declared(
    monkeypatch,
):
    """``console_options`` routes the probe through ITS OWN server/port, not the
    guest's address — the console-term reaches the guest via the zephyr hop's
    own loopback telnet listener, not by dialing the guest directly."""
    from scripts import lab_health

    seen = {}

    def fake_ssh(ip, user, password, remote_cmd, timeout=25.0):
        seen["ip"], seen["cmd"] = ip, remote_cmd
        return 0, "OK 1234", ""

    monkeypatch.setattr(lab_health, "_run_ssh", fake_ssh)
    # `hop` is deliberately a THIRD host (test9), distinct from the console
    # server (test4): the console path must dial the server named in
    # `console_options`, never the entry's own `hop`, and a real host dict
    # with both pointing the same place (test4 == test4) would let a mutant
    # such as `hops.get(host.get("hop") or console["server"])` pass unnoticed.
    host = {
        "element": "zephyr37_nofs",
        "ip": "192.0.2.37",
        "os_type": "zephyr",
        "hop": "test9",
        "console_options": {"server": "test4", "port": 2325},
    }
    hops = {
        "test4": {
            "element": "test4",
            "ip": "10.10.200.14",
            "creds": [{"login": "vagrant", "password": "vagrant"}],
        },
        "test9": {
            "element": "test9",
            "ip": "10.10.200.19",
            "creds": [{"login": "vagrant", "password": "vagrant"}],
        },
    }
    res = lab_health._check_embedded(host, hops)
    assert res["ok"]
    assert seen["ip"] == "10.10.200.14"
    assert "127.0.0.1 2325" in seen["cmd"]
    assert "192.0.2.37" not in seen["cmd"], (
        f"the guest's own address leaked into the console probe: {seen['cmd']!r}"
    )


def test_a_creds_carrying_console_host_keeps_the_telnet_options_probe(monkeypatch):
    """``console_options`` alone must NOT take over a host that also has its own
    creds — the bb1350 shape Task 14 adds. bb1350 keeps `creds` + `hop: test1`
    (its in-guest telnetd is still its primary reach path) and separately
    grows `console_options` pointing at its ttyS1 serial getty; Task 15 adds
    a console row for it IN ADDITION to this one. If this probe silently
    switched to the console path once console_options showed up, the
    authenticated `/proc/uptime` read (and the in-guest telnetd check
    entirely) would be lost with no sign of it — the console UART0 getty was
    long past its login prompt by the time the probe connects (QEMU's
    `nowait` discards anything printed before the client attaches), so the
    swapped-in probe would report a false ``up`` instead."""
    from scripts import lab_health

    seen = {}

    def fake_ssh(ip, user, password, remote_cmd, timeout=25.0):
        seen["ip"], seen["cmd"] = ip, remote_cmd
        return 0, "OK login", ""

    monkeypatch.setattr(lab_health, "_run_ssh", fake_ssh)
    host = {
        "element": "bb1350",
        "ip": "198.51.100.17",
        "os_type": "unix",
        "hop": "test1",
        "creds": [{"login": "root", "password": "otto"}],
        "console_options": {"server": "test1", "port": 2450},
    }
    hops = {
        "test1": {
            "element": "test1",
            "ip": "10.10.200.11",
            "creds": [{"login": "vagrant", "password": "vagrant"}],
        }
    }
    res = lab_health._check_embedded(host, hops)
    assert res["ok"]
    assert seen["ip"] == "10.10.200.11"
    assert "198.51.100.17 23" in seen["cmd"], (
        f"a creds-carrying console host was probed on the console path instead of "
        f"its own telnetd: {seen['cmd']!r}"
    )
    assert seen["cmd"].endswith(" root otto"), (
        f"the login branch's own creds did not survive: {seen['cmd']!r}"
    )


# ---------------------------------------------------------------------------
# The console-state probe (spec 2026-09-24, "health probe")
#
# Every entry that offers `console` in its term menu gets one extra row: the
# state its serial console is in right now, read the way otto's ConsoleClient
# reads it (nudge CR, classify the ANSI-stripped tail). `--logout-consoles`
# additionally runs the bounded reset on lines left logged in.
# ---------------------------------------------------------------------------


class _Console(socketserver.BaseRequestHandler):
    reply: bytes = b""
    close_at_once: bool = False

    def handle(self):
        if self.close_at_once:
            return
        self.request.settimeout(2)
        try:
            while True:
                data = self.request.recv(64)
                if not data:
                    return
                if b"\r" in data and self.reply:
                    self.request.sendall(self.reply)
        except OSError:
            return


def _serve(reply: bytes, close_at_once: bool = False):
    handler = type("H", (_Console,), {"reply": reply, "close_at_once": close_at_once})
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _probe(port: int, *extra: str) -> str:
    out = subprocess.run(
        [sys.executable, "-c", _CONSOLE_STATE_PROBE, "127.0.0.1", str(port), "1.0", *extra],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    return out.stdout.strip()


@pytest.mark.parametrize(
    ("reply", "close_at_once", "expected"),
    [
        (b"\r\ntest2 login: ", False, "AT-LOGIN"),
        (b"Password: ", False, "AT-PASSWORD"),
        (b"\r\ntest@test2:~$ ", False, "LOGGED-IN"),
        (b"", False, "SILENT"),
        (b"", True, "BUSY"),
    ],
)
def test_console_probe_script_classifies_each_state(reply, close_at_once, expected):
    srv = _serve(reply, close_at_once)
    try:
        assert _probe(srv.server_address[1]) == expected
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        # A getty that paints its banner in colour: the ANSI has to go before
        # the tail is classified, or `login: ?$` never matches.
        (b"\r\n\x1b[1mtest2\x1b[0m login: \x1b[0m", "AT-LOGIN"),
        # A prompt followed by stray line ends is still that prompt.
        (b"\r\ntest2 login: \r\n", "AT-LOGIN"),
        # A MOTD's "Last login:" mid-buffer is not a login prompt: only the tail decides.
        (b"Last login: Mon\r\n# ", "LOGGED-IN"),
        # Zephyr's shell prompt: bytes, no prompt of either kind.
        (b"\r\nuart:~$ ", "LOGGED-IN"),
    ],
)
def test_console_probe_classifies_the_stripped_tail(reply, expected):
    srv = _serve(reply)
    try:
        assert _probe(srv.server_address[1]) == expected
    finally:
        srv.shutdown()
        srv.server_close()


def test_console_probe_reports_a_refused_connect():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.close()  # nothing listens here now
    assert _probe(port).startswith("CONNFAIL")


def test_console_probe_reset_ends_a_shell():
    # A shell that answers Ctrl-C/Ctrl-D/CR with a login prompt.
    class _Shell(_Console):
        def handle(self):
            self.request.settimeout(2)
            seen = b""
            while True:
                data = self.request.recv(64)
                if not data:
                    return
                seen += data
                self.request.sendall(
                    b"\r\ntest2 login: " if b"\x04" in seen else b"\r\ntest@test2:~$ "
                )

    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Shell)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert _probe(srv.server_address[1], "reset").splitlines()[-1] == "RESET-OK"
    finally:
        srv.shutdown()
        srv.server_close()


class _RecordingConsole:
    """One connection: answer each write with ``respond(everything_seen)``; record every byte."""

    def __init__(self, respond, idle=5.0):
        self.respond = respond
        self.idle = idle
        self.received = bytearray()
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        try:
            conn, _ = self.srv.accept()
            with conn:
                conn.settimeout(self.idle)
                while True:
                    chunk = conn.recv(64)
                    if not chunk:
                        return
                    self.received.extend(chunk)
                    reply = self.respond(bytes(self.received))
                    if reply:
                        conn.sendall(reply)
        except OSError:
            return

    def close(self):
        self.thread.join(timeout=5)
        self.srv.close()


def test_reset_sends_ctrl_c_ctrl_d_cr_in_order_and_stops_at_the_first_login_prompt():
    """One round is enough when the first Ctrl-D ends the shell: the probe must
    not keep typing at a line that is already back at ``login:``."""

    def shell(seen):
        return b"\r\ntest2 login: " if b"\x04" in seen else b"\r\nroot@test2:~# "

    con = _RecordingConsole(shell)
    try:
        out = _probe(con.port, "reset").splitlines()
    finally:
        con.close()
    assert out == ["LOGGED-IN", "RESET-OK"], out
    assert bytes(con.received) == b"\r" + b"\x03\x04\r", bytes(con.received)


def test_reset_writes_ctrl_d_apart_from_the_ctrl_c_whose_flush_would_eat_it():
    """A tty's ISIG handling of Ctrl-C flushes its input queue: a Ctrl-D that
    arrives in the same burst never reaches the shell (measured on test2's
    bash, where every burst round only redrew the prompt). The fake models
    it per received chunk, so a probe that sends the two together fails."""
    delivered = bytearray()
    consumed = [0]

    def tty(seen):
        chunk = seen[consumed[0] :]
        consumed[0] = len(seen)
        if b"\x03" in chunk:
            chunk = chunk[: chunk.index(b"\x03") + 1]
        delivered.extend(chunk)
        return b"\r\ntest2 login: " if b"\x04" in delivered else b"\r\ntest@test2:~$ "

    con = _RecordingConsole(tty)
    try:
        out = _probe(con.port, "reset").splitlines()
    finally:
        con.close()
    assert out == ["LOGGED-IN", "RESET-OK"], out


def test_reset_at_a_password_prompt_sends_only_ctrl_c():
    """At ``Password:`` the reset's first move is Ctrl-C: login(1) exits and
    getty respawns a clean prompt. It never types a password, and no Enter
    follows (an Enter would leave login(1)'s own retry prompt, which answers
    the next nudge with ``Password:``)."""

    def getty(seen):
        if seen.endswith(b"\x03"):
            return b"\r\r\nUbuntu test2 ttyV0\r\n\r\ntest2 login: "
        return b"Password: "

    con = _RecordingConsole(getty)
    try:
        out = _probe(con.port, "reset").splitlines()
    finally:
        con.close()
    assert out == ["AT-PASSWORD", "RESET-OK"], out
    assert bytes(con.received) == b"\r\x03", bytes(con.received)


def test_reset_at_a_password_prompt_does_not_stop_at_login_1s_retry_prompt():
    """Measured on the bed: an Enter at login(1)'s Password: lands at login(1)'s
    retry prompt, whose next nudge answers ``Password:`` again. The fake
    answers an Enter that way, so a probe that sends one reports a reset it
    did not do only if it stops there; Ctrl-C is the only way to getty."""

    def login1(seen):
        if seen.endswith(b"\x03"):
            return b"\r\r\nUbuntu test2 ttyV0\r\n\r\ntest2 login: "
        if seen == b"\r\r":
            return b"\r\nLogin incorrect\r\ntest2 login: "
        return b"\r\nPassword: "

    con = _RecordingConsole(login1)
    try:
        out = _probe(con.port, "reset").splitlines()
    finally:
        con.close()
    assert out == ["AT-PASSWORD", "RESET-OK"], out
    assert bytes(con.received) == b"\r\x03", "the password prompt got Ctrl-C, not Enter"


def test_reset_gives_up_after_three_rounds_and_quotes_the_tail():
    def stuck(seen):
        return b"\r\n(stuck)> "

    con = _RecordingConsole(stuck)
    try:
        out = _probe(con.port, "reset").splitlines()
    finally:
        con.close()
    assert out[0] == "LOGGED-IN"
    assert out[-1].startswith("RESET-FAILED"), out
    assert "(stuck)>" in out[-1]
    assert bytes(con.received) == b"\r" + b"\x03\x04\r" * 3, bytes(con.received)


def test_reset_is_never_attempted_on_a_line_already_at_login():
    con = _RecordingConsole(lambda seen: b"\r\ntest2 login: ")
    try:
        out = _probe(con.port, "reset").splitlines()
    finally:
        con.close()
    assert out == ["AT-LOGIN"], out
    assert bytes(con.received) == b"\r\r", "two nudges (the second confirms getty), no reset"


def test_reset_tells_login_1s_retry_prompt_from_gettys_and_ends_login_1():
    """login(1)'s own retry prompt looks like getty's but answers an Enter with
    Password: (an empty username). The confirming nudge sees that and the
    reset ends login(1) with Ctrl-C."""

    def login1(seen):
        if seen.endswith(b"\x03"):
            return b"\r\r\nUbuntu test2 ttyV0\r\n\r\ntest2 login: "
        if seen == b"\r":
            return b"\r\nLogin incorrect\r\ntest2 login: "
        return b"\r\nPassword: "

    con = _RecordingConsole(login1)
    try:
        out = _probe(con.port, "reset").splitlines()
    finally:
        con.close()
    assert out == ["AT-PASSWORD", "RESET-OK"], out
    assert bytes(con.received) == b"\r\r\x03", bytes(con.received)


def test_a_plain_health_probe_nudges_once():
    """Without ``reset`` the probe is read-only past its one Enter: a second
    Enter at login(1)'s retry prompt would move the line to Password:."""
    con = _RecordingConsole(lambda seen: b"\r\ntest2 login: ")
    try:
        out = _probe(con.port).splitlines()
    finally:
        con.close()
    assert out == ["AT-LOGIN"], out
    assert bytes(con.received) == b"\r"


class _ClosingSocket:
    """Wrap a real socket and record whether the probe closed it itself."""

    closed: ClassVar[list[bool]] = []

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    def close(self):
        _ClosingSocket.closed.append(True)
        self._real.close()


@pytest.mark.parametrize(
    ("reply", "close_at_once", "extra"),
    [
        (b"\r\ntest2 login: ", False, []),
        (b"\r\n# ", False, []),
        (b"", False, []),
        (b"", True, []),
        (b"\r\n# ", False, ["reset"]),
    ],
    ids=["at-login", "logged-in", "silent", "busy", "failed-reset"],
)
def test_the_probe_closes_its_socket_on_every_path(
    monkeypatch, capsys, reply, close_at_once, extra
):
    """A serial console serves ONE client: a probe that leaves its socket to
    the interpreter's exit is a probe that can wedge the line if it ever runs
    longer than planned. Run the script in-process and watch ``close()``."""
    real_connect = socket.create_connection
    _ClosingSocket.closed = []
    monkeypatch.setattr(
        socket, "create_connection", lambda *a, **k: _ClosingSocket(real_connect(*a, **k))
    )
    srv = _serve(reply, close_at_once)
    monkeypatch.setattr(sys, "argv", ["-c", "127.0.0.1", str(srv.server_address[1]), "0.3", *extra])
    try:
        with pytest.raises(SystemExit):
            exec(compile(_CONSOLE_STATE_PROBE, "<probe>", "exec"), {"__name__": "__main__"})  # noqa: S102
    finally:
        srv.shutdown()
        srv.server_close()
    assert capsys.readouterr().out.strip()
    assert _ClosingSocket.closed == [True]


# -- _check_console ----------------------------------------------------------

# Sentinels that must never leave the entry they sit in.
_GUEST_PASSWORD = "s3cr3t-guest-pw"
_HOP_PASSWORD = "s3cr3t-hop-pw"


def _console_entry(*, creds: bool, element="test2", server="test1", port=4001):
    host = {
        "element": element,
        "ip": "10.10.200.12",
        "valid_terms": ["telnet", "ssh", "console"] if creds else ["console"],
        "console_options": {"server": server, "port": port},
    }
    if creds:
        host["creds"] = [{"login": "test", "password": _GUEST_PASSWORD}]
    return host


_SERVER = {
    "element": "test1",
    "ip": "10.10.200.11",
    "creds": [{"login": "vagrant", "password": _HOP_PASSWORD}],
}


def _fake_ssh(monkeypatch, out, rc=0, err=""):
    calls = []

    def fake(ip, user, password, cmd, timeout=25.0):
        calls.append({"ip": ip, "user": user, "cmd": cmd, "timeout": timeout})
        return rc, out, err

    monkeypatch.setattr(lab_health, "_run_ssh", fake)
    return calls


def test_check_console_dials_the_servers_loopback_port_from_the_server(monkeypatch):
    calls = _fake_ssh(monkeypatch, "AT-LOGIN")
    res = lab_health._check_console(_console_entry(creds=True), {"test1": _SERVER}, logout=False)
    assert res["ok"] is True
    assert res["status"] == "AT-LOGIN"
    (call,) = calls
    assert call["ip"] == "10.10.200.11"
    assert call["user"] == "vagrant"
    assert " 127.0.0.1 4001 " in call["cmd"] + " "
    assert not call["cmd"].rstrip().endswith("reset")


@pytest.mark.parametrize(
    ("creds", "out", "ok"),
    [
        (True, "AT-LOGIN", True),
        (True, "LOGGED-IN", False),
        (True, "AT-PASSWORD", False),
        (True, "SILENT", False),
        (True, "BUSY", False),
        (False, "LOGGED-IN", True),  # a Zephyr shell showing its prompt is healthy
        (False, "AT-LOGIN", True),
        (False, "SILENT", False),
        (False, "BUSY", False),
    ],
)
def test_check_console_ok_rule_depends_on_whether_the_entry_logs_in(monkeypatch, creds, out, ok):
    _fake_ssh(monkeypatch, out)
    res = lab_health._check_console(_console_entry(creds=creds), {"test1": _SERVER}, logout=False)
    assert res["ok"] is ok, res
    assert res["status"] == out


def test_logout_consoles_resets_only_entries_that_log_in(monkeypatch):
    calls = _fake_ssh(monkeypatch, "LOGGED-IN")
    zephyr = lab_health._check_console(
        _console_entry(creds=False, element="zephyr37_llext", server="test1", port=2323),
        {"test1": _SERVER},
        logout=True,
    )
    assert zephyr["ok"] is True
    assert not calls[-1]["cmd"].rstrip().endswith("reset"), (
        "a credless (Zephyr) console was sent the reset: Ctrl-D at a Zephyr shell is not a logout"
    )

    calls = _fake_ssh(monkeypatch, "LOGGED-IN\nRESET-OK")
    res = lab_health._check_console(_console_entry(creds=True), {"test1": _SERVER}, logout=True)
    assert calls[-1]["cmd"].rstrip().endswith("reset")
    assert res["ok"] is True
    assert res["status"] == "AT-LOGIN"
    assert "LOGGED-IN" in res["info"]


def test_a_failed_reset_is_a_not_ok_row_that_keeps_the_state(monkeypatch):
    _fake_ssh(monkeypatch, "LOGGED-IN\nRESET-FAILED '(stuck)> '")
    res = lab_health._check_console(_console_entry(creds=True), {"test1": _SERVER}, logout=True)
    assert res["ok"] is False
    assert res["status"] == "LOGGED-IN"
    assert "reset failed" in res["info"]


@pytest.mark.parametrize(
    ("rc", "out", "status"),
    [(0, "CONNFAIL [Errno 111] Connection refused", "DOWN"), (255, "", "HOP-FAIL")],
)
def test_check_console_failures(monkeypatch, rc, out, status):
    _fake_ssh(monkeypatch, out, rc=rc, err="boom" if rc else "")
    res = lab_health._check_console(_console_entry(creds=True), {"test1": _SERVER}, logout=False)
    assert res["ok"] is False
    assert res["status"] == status


def test_check_console_names_a_missing_server(monkeypatch):
    _fake_ssh(monkeypatch, "AT-LOGIN")
    res = lab_health._check_console(_console_entry(creds=True), {}, logout=False)
    assert res == {"ok": False, "status": "NO-HOP", "info": "console server 'test1' not in lab"}


def test_no_password_reaches_the_probe_or_the_report(monkeypatch, capsys):
    assert _GUEST_PASSWORD not in _CONSOLE_STATE_PROBE
    calls = _fake_ssh(monkeypatch, "LOGGED-IN\nRESET-OK")
    monkeypatch.setattr(
        lab_health, "_check_unix", lambda host: {"ok": True, "status": "up", "info": ""}
    )
    host = _console_entry(creds=True)
    lab_health._print_report([host, {**_SERVER}], {"test1": _SERVER}, logout=True)
    table = capsys.readouterr().out
    for call in calls:
        assert _GUEST_PASSWORD not in call["cmd"]
        assert _HOP_PASSWORD not in call["cmd"]
    assert _GUEST_PASSWORD not in table
    assert _HOP_PASSWORD not in table


def _rows(table: str, element: str) -> list[str]:
    return [line for line in table.splitlines() if line.split()[:1] == [element]]


@pytest.mark.parametrize("element", ["bb1350", "zephyr37_llext", "test2"])
def test_a_console_capable_entry_gets_its_own_row_and_a_console_row(monkeypatch, capsys, element):
    """Over the real lab data: the console row is IN ADDITION to the entry's
    existing probe, never instead of it, and a host without `console` in its
    term menu gets no console row at all."""
    seen: list[str] = []
    monkeypatch.setattr(
        lab_health, "_check_unix", lambda host: {"ok": True, "status": "up", "info": ""}
    )
    monkeypatch.setattr(
        lab_health,
        "_check_embedded",
        lambda host, hops: {"ok": True, "status": "up", "info": ""},
    )

    def fake_console(host, hops, *, logout):
        seen.append(host["element"])
        return {"ok": True, "status": "AT-LOGIN", "info": "login prompt"}

    monkeypatch.setattr(lab_health, "_check_console", fake_console)
    hosts = _load_hosts(DEFAULT_HOSTS)
    assert _print_report(hosts, _hop_index(hosts)) is True
    rows = _rows(capsys.readouterr().out, element)
    assert len(rows) == 2, rows
    assert rows[0].split()[2] != "console"
    assert rows[1].split()[2] == "console"
    assert "AT-LOGIN" in rows[1]
    expected = {
        h["element"]
        for h in hosts
        if h.get("console_options") and "console" in h.get("valid_terms", [])
    }
    assert sorted(seen) == sorted(expected)
    assert "test1" not in seen


def test_a_not_ok_console_row_fails_the_report(monkeypatch, capsys):
    monkeypatch.setattr(
        lab_health, "_check_unix", lambda host: {"ok": True, "status": "up", "info": ""}
    )
    monkeypatch.setattr(
        lab_health,
        "_check_console",
        lambda host, hops, *, logout: {"ok": False, "status": "BUSY", "info": "port held"},
    )
    host = _console_entry(creds=True)
    assert lab_health._print_report([host, {**_SERVER}], {"test1": _SERVER}) is False


def test_main_passes_logout_consoles_through(monkeypatch, tmp_path):
    seen = {}
    lab = tmp_path / "lab.json"
    lab.write_text('{"elements": []}')
    monkeypatch.setattr(lab_health.shutil, "which", lambda name: "/usr/bin/sshpass")

    def fake_report(hosts, hops, *, logout=False):
        seen["logout"] = logout
        return True

    monkeypatch.setattr(lab_health, "_print_report", fake_report)
    assert lab_health.main(["--hosts", str(lab), "--logout-consoles"]) == 0
    assert seen["logout"] is True
    assert lab_health.main(["--hosts", str(lab)]) == 0
    assert seen["logout"] is False


def test_a_console_that_hangs_up_on_the_nudge_is_busy_not_silent():
    """The server accepts, stays quiet through the settle, then drops the line
    on the Enter: the EOF after the nudge is what BUSY means, not zero bytes."""

    class _HangUpOnEnter(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.settimeout(5)
            try:
                self.request.recv(64)
            except OSError:
                return

    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _HangUpOnEnter)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert _probe(srv.server_address[1]) == "BUSY"
    finally:
        srv.shutdown()
        srv.server_close()


def test_a_nested_shell_needing_two_rounds_resets_at_the_real_budget():
    """A shell nested in a shell needs two Ctrl-D rounds. At the REAL default
    budget the LOGGED-IN decision alone spends the full 10 s, and the reset
    rounds must still each get a real wait of their own afterwards, rather than
    being fired blind past a hold ceiling and reported as a failure."""

    consumed = [0]

    def nested(seen):
        chunk = seen[consumed[0] :]
        consumed[0] = len(seen)
        rounds = seen.count(b"\x04")
        if b"\x04" in chunk:
            # Each logout takes a moment to land, as a real shell exit + getty
            # respawn does: two of these do not fit in what a 15 s hold would
            # leave after the 10.5 s LOGGED-IN decision.
            time.sleep(2.5)
        return b"\r\ntest2 login: " if rounds >= 2 else b"\r\nroot@test2:~# "

    # The server must outwait the probe's 10 s LOGGED-IN decision.
    con = _RecordingConsole(nested, idle=20.0)
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _CONSOLE_STATE_PROBE, "127.0.0.1", str(con.port), "10", "reset"],
            capture_output=True,
            text=True,
            timeout=40,
            check=False,
        )
    finally:
        con.close()
    assert proc.stdout.split() == ["LOGGED-IN", "RESET-OK"], proc.stdout
    assert bytes(con.received) == b"\r" + b"\x03\x04\r" * 2, bytes(con.received)


def test_a_failed_reset_quotes_the_last_thing_the_line_said():
    """A line that answers the first round and then goes quiet: the failure
    must quote what it last said, not the empty read of the final round."""

    def goes_quiet(seen):
        rounds = seen.count(b"\x04")
        if rounds == 0:
            return b"\r\nroot@test2:~# "
        return b"\r\n(stuck)> " if rounds == 1 else b""

    con = _RecordingConsole(goes_quiet)
    try:
        out = _probe(con.port, "reset").splitlines()
    finally:
        con.close()
    assert out[-1].startswith("RESET-FAILED"), out
    assert "(stuck)>" in out[-1], out


def test_the_ssh_timeout_covers_the_probes_whole_hold():
    step = min(lab_health._CONSOLE_STATE_BUDGET_S, 3.0)
    hold = 0.5 + lab_health._CONSOLE_STATE_BUDGET_S + 2 * step + 3 * 2 * step
    assert 4 + hold + 10 <= lab_health._CONSOLE_STATE_SSH_TIMEOUT_S


def test_a_failed_reset_row_keeps_the_info_column_and_puts_the_tail_last(monkeypatch, capsys):
    _fake_ssh(monkeypatch, "LOGGED-IN\nRESET-FAILED '(stuck in a very long prompt)> '")
    monkeypatch.setattr(
        lab_health, "_check_unix", lambda host: {"ok": True, "status": "up", "info": ""}
    )
    lab_health._print_report(
        [_console_entry(creds=True), {**_SERVER}], {"test1": _SERVER}, logout=True
    )
    (row,) = [line for line in capsys.readouterr().out.splitlines() if " console " in line]
    info = row[16 + 16 + 10 + 13 :][:18]
    assert info.rstrip() == "reset failed", row
    assert row.endswith("'(stuck in a very long prompt)> '"), row
