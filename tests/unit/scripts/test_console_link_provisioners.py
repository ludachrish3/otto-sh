"""Unit guards for the Vagrantfile's test2 serial-console provisioners.

test2's serial console is a virtual null-modem built inside the guests:
VirtualBox on arm64 exposes no UART to them. On test2 (the console HOST) a
socat unit holds a pty at ``/dev/ttyV0`` and listens for its far end on the
lab address; a getty answers that pty. On test1 (the console SERVER) a socat
unit dials test2 and holds the other pty, and ser2net serves it on port 4001
single-client. The lab data (``console_options: {"server": "test1", "port":
4001}``) is what otto sees.

The provisioners are shell inside Ruby heredocs, provisioned only by hand,
so these hostless guards are the one thing between a transposed address or a
swapped role and a bed that silently offers no console.
"""

import re

import yaml

from tests._fixtures.paths import PROJECT_ROOT

_TEST2_LAB_IP = "10.10.200.12"
_LINK_PORT = 4102
_KEEPALIVE = "keepalive,keepidle=10,keepintvl=5,keepcnt=3"
"""TCP keepalives on both ends of the link.

An idle console sends nothing, so a hard-stopped peer never produces a RST:
without keepalives the surviving socat holds a dead socket forever and the
console goes silent. With them the peer is declared dead ~25 s into the
silence, socat exits, and its unit restarts.
"""


def _unit(body: str, path: str) -> str:
    """One ``cat > <path> <<'EOF'`` heredoc from a provisioner body."""
    match = re.search(rf"cat > {re.escape(path)} <<'EOF'\n(.*?)\nEOF\n", body, re.DOTALL)
    assert match, f"no {path} heredoc"
    return match.group(1)


def _vagrantfile() -> str:
    return (PROJECT_ROOT / "Vagrantfile").read_text()


def _helper_body(signature: str) -> str:
    """The body of one ``def`` in the Vagrantfile, up to its closing ``end``."""
    text = _vagrantfile()
    start = text.index(f"def {signature}")
    return text[start : text.index("\n    end\n", start)]


def _host_body() -> str:
    return _helper_body("provision_console_host(vm)")


def _server_body() -> str:
    return _helper_body("provision_console_server(vm)")


def _test_vm_loop() -> str:
    """The ``test1``/``test2``/``test3`` define loop, as committed."""
    text = _vagrantfile()
    start = text.index('"test3" => "10.10.200.13",')
    return text[start : text.index("\n    end\n", start)]


def _ser2net_config() -> dict:
    body = _server_body()
    match = re.search(r"cat > /etc/ser2net\.yaml <<'EOF'\n(.*?)\nEOF\n", body, re.DOTALL)
    assert match, "provision_console_server writes no /etc/ser2net.yaml heredoc"
    return yaml.safe_load(match.group(1))


# --- test2: the console host --------------------------------------------------


def test_the_host_socat_listens_on_the_lab_address_only():
    """test2's pty is reachable from test1 over the lab wire and nowhere else.

    ``bind=`` pins the listener to test2's lab address: without it (or with
    ``0.0.0.0``) the console is also offered on the NAT and data-plane NICs.
    ``reuseaddr`` lets a restart rebind at once.

    NO ``fork``: a serial line has one peer. With ``fork`` the pty is opened
    once in the parent and each connection's child unlinks ``/dev/ttyV0`` on
    exit, so the next getty respawn waits for a link that never returns.
    Without it socat exits when test1 disconnects, and the unit's restart
    makes a new pty and link that the getty follows.
    """
    body = _host_body()
    expected = (
        "ExecStart=/usr/bin/socat -d -d pty,raw,echo=0,link=/dev/ttyV0 "
        f"tcp-listen:{_LINK_PORT},bind={_TEST2_LAB_IP},reuseaddr,{_KEEPALIVE}\n"
    )
    assert expected in body
    assert ",fork" not in body


def test_the_host_socat_unit_restarts_without_limit():
    """socat exits on every test1 disconnect; systemd must never give up on it."""
    unit = _unit(_host_body(), "/etc/systemd/system/console-link.service")
    assert "\nRestart=always\n" in unit
    assert "\nStartLimitIntervalSec=0\n" in unit


def test_the_host_answers_its_pty_with_a_getty():
    """A getty on ttyV0 is what puts ``test2 login:`` on the line."""
    body = _host_body()
    assert "cat > /etc/systemd/system/serial-getty@ttyV0.service <<'EOF'" in body
    assert "systemctl enable serial-getty@ttyV0.service" in body
    assert re.search(r"^ExecStart=-/sbin/agetty -L .*\bttyV0\b", body, re.MULTILINE)


def test_the_host_getty_follows_the_socat_unit_across_restarts():
    """The getty's pty is REPLACED each time socat restarts.

    Survival is a ``Restart=`` policy, not ``BindsTo``: agetty exits on the
    hangup of the vanished pty, ``Restart=always`` brings it back, and its
    ``ExecStartPre`` waits for the new link. ``BindsTo`` would stop the getty
    with socat and leave it stopped after socat's own automatic restart.
    """
    unit = _unit(_host_body(), "/etc/systemd/system/serial-getty@ttyV0.service")
    for line in [
        "Wants=console-link.service",
        "After=console-link.service",
        "StartLimitIntervalSec=0",
        "Restart=always",
        # The stock serial-getty ordering: no login before user sessions are
        # allowed (pam_nologin), and none in rescue mode.
        "After=systemd-user-sessions.service",
        "Before=getty.target",
        "Conflicts=rescue.service",
    ]:
        assert f"\n{line}\n" in unit, line
    assert "BindsTo=" not in unit
    assert re.search(r"^ExecStartPre=.*until \[ -e /dev/ttyV0 \]", unit, re.MULTILINE)
    # The template's udev binding would wait forever: a socat pty link is not
    # a udev device, so dev-ttyV0.device never appears.
    assert "dev-%i.device" not in unit
    assert "dev-ttyV0.device" not in unit


def test_the_host_runs_no_ser2net_and_dials_nobody():
    """Role guard: the console HOST neither serves ser2net nor dials test2."""
    body = _host_body()
    assert "ser2net" not in body
    assert f"tcp:{_TEST2_LAB_IP}" not in body


# --- test1: the console server ------------------------------------------------


def test_the_server_socat_dials_test2_and_retries():
    """test1's end of the null-modem dials test2's listener and never gives up.

    test2 may boot after test1 (or be halted): the dial fails, socat exits,
    and ``Restart=always`` with a short ``RestartSec`` tries again, with no
    start limit: five failed dials in ten seconds must not stop the retries.
    """
    unit = _unit(_server_body(), "/etc/systemd/system/console-link.service")
    expected = (
        "ExecStart=/usr/bin/socat pty,raw,echo=0,link=/dev/ttyV0 "
        f"tcp:{_TEST2_LAB_IP}:{_LINK_PORT},{_KEEPALIVE}\n"
    )
    assert expected in unit
    assert "\nRestart=always\n" in unit
    assert "\nStartLimitIntervalSec=0\n" in unit
    restart_sec = re.search(r"^RestartSec=(\d+)$", unit, re.MULTILINE)
    assert restart_sec
    assert 0 < int(restart_sec.group(1)) <= 5


def test_ser2net_serves_the_pty_single_client_on_4001():
    """The one telnet listener both console dial modes reach.

    No host in the accepter: bound on every address, so ``dial: ssh``
    (localhost:4001 inside test1) and ``dial: direct`` (10.10.200.11:4001)
    meet the same listener. A serial line is single-client.
    """
    config = _ser2net_config()
    [connection] = [value for key, value in config.items() if key.startswith("connection")]
    assert connection["accepter"] == "telnet(rfc2217),tcp,4001"
    assert connection["connector"] == "serialdev,/dev/ttyV0,115200n81,local"
    assert connection["options"]["max-connections"] == 1


def test_the_server_runs_no_getty_and_listens_for_nobody():
    """Role guard: the console SERVER answers no login and opens no link port."""
    body = _server_body()
    assert "getty" not in body
    assert "tcp-listen" not in body


# --- both --------------------------------------------------------------------


def test_each_role_is_provisioned_on_exactly_its_host():
    """test1 serves, test2 hosts, test3 does neither."""
    loop = _test_vm_loop()
    assert re.findall(r"provision_console_\w+\(node\).*", loop) == [
        'provision_console_server(node) if name == "test1"',
        'provision_console_host(node) if name == "test2"',
    ]
    text = _vagrantfile()
    # One definition plus the one call above; no other VM is wired to either.
    assert text.count("provision_console_server(") == 2
    assert text.count("provision_console_host(") == 2


def test_both_provisioners_are_non_interactive_and_idempotent():
    """Re-provisioning either host is safe and never stops at a prompt.

    Every file is rewritten whole (``cat >``, never appended to), and each
    unit is enabled rather than started once, then restarted so an edited
    unit takes effect on a re-provision.
    """
    for body in [_host_body(), _server_body()]:
        assert "export DEBIAN_FRONTEND=noninteractive" in body
        # A conffile we overwrite (/etc/ser2net.yaml) would otherwise make a
        # later package upgrade stop at dpkg's prompt; DEBIAN_FRONTEND does
        # not cover conffile prompts.
        assert (
            "apt-get -y -o Dpkg::Options::=--force-confdef "
            "-o Dpkg::Options::=--force-confold install" in body
        )
        assert ">>" not in body
        assert "systemctl daemon-reload" in body
        assert "systemctl restart console-link.service" in body
