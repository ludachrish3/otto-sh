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
import subprocess
import sys
import threading

import pytest

from scripts import lab_health
from scripts.lab_health import (
    _CONSOLE_PROBE,
    DEFAULT_HOSTS,
    _hop_index,
    _load_hosts,
    _print_report,
)


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
    """bb guests have creds AND a hop; SSHing 127.0.0.1 would probe the dev VM
    itself. Routing must send them down the console path."""
    guest = {
        "ip": "127.0.0.1",
        "element": "bb1161",
        "os_type": "unix",
        "hop": "carrot_seed",
        "creds": [{"login": "root", "password": "otto"}],
        "telnet_options": {"port": 2316},
    }
    assert lab_health._is_ssh_host(guest) is False
    hop = {
        "ip": "10.10.200.11",
        "element": "carrot",
        "board": "seed",
        "creds": [{"login": "vagrant", "password": "vagrant"}],
    }
    assert lab_health._is_ssh_host(hop) is True

    seen = {}

    def fake_run_ssh(ip, user, password, cmd, timeout=25.0):
        seen["ip"] = ip
        seen["cmd"] = cmd
        return 0, "OK login", ""

    monkeypatch.setattr(lab_health, "_run_ssh", fake_run_ssh)
    res = lab_health._check_embedded(guest, {"carrot_seed": hop})
    assert seen["ip"] == "10.10.200.11"  # probed FROM the hop
    assert " 2316" in seen["cmd"]  # honors telnet_options.port
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
    the zephyr units would restart nothing at all on carrot."""
    captured = {}

    def fake_run_ssh(ip, user, password, cmd, timeout=25.0):
        captured["cmd"] = cmd
        return 0, "", ""

    monkeypatch.setattr(lab_health, "_run_ssh", fake_run_ssh)
    hosts = [
        {
            "ip": "10.10.200.11",
            "element": "carrot",
            "board": "seed",
            "creds": [{"login": "vagrant", "password": "vagrant"}],
        },
        {
            "ip": "127.0.0.1",
            "element": "bb1161",
            "hop": "carrot_seed",
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
        "element": "carrot",
        "board": "seed",
        "creds": [{"login": "vagrant", "password": "vagrant"}],
    }
    hops = {"carrot_seed": hop, "basil_seed": hop}
    captured = {}

    def fake_run_ssh(ip, user, password, cmd, timeout=25.0):
        captured["cmd"] = cmd
        captured["timeout"] = timeout
        return 0, "OK login", ""

    monkeypatch.setattr(lab_health, "_run_ssh", fake_run_ssh)

    lab_health._check_embedded(
        {
            "ip": "127.0.0.1",
            "element": "bb1161",
            "hop": "carrot_seed",
            "creds": [{"login": "root", "password": "otto"}],
            "telnet_options": {"port": 2316},
        },
        hops,
    )
    bb_tail = captured["cmd"].split(" 127.0.0.1 ", 1)[1]
    assert bb_tail == "2316 root otto", f"BusyBox guest probe did not carry its creds: {bb_tail!r}"
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
            "element": "sprout",
            "hop": "basil_seed",
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


def _bb_entry(element="bb1161", port=2316):
    return {
        "ip": "127.0.0.1",
        "element": element,
        "hop": "carrot_seed",
        "creds": [{"login": "root", "password": "otto"}],
        "telnet_options": {"port": port},
    }


_HOP = {
    "ip": "10.10.200.11",
    "element": "carrot",
    "board": "seed",
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

    refused = lab_health._check_embedded(_bb_entry(), {"carrot_seed": _HOP})
    assert refused["ok"] is False, (
        f"a console that refused the guest's committed creds reported {refused!r} — "
        "vm-health stays green and the broken bake surfaces as the first bed test's "
        "login failure, which is the outcome spec §6 exists to prevent"
    )
    assert refused["status"] == "BAD-CREDS", refused
    assert "root" in refused["info"], (
        f"the failing row does not name the login that was refused: {refused['info']!r}"
    )

    silent = lab_health._check_embedded(_bb_entry(), {"carrot_seed": _HOP})
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
