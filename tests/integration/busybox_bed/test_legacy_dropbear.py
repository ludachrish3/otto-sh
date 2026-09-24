"""Wire proof: otto negotiates with a SHA-1-only sshd, dropbear 2012.55 on bb1350.

Four handshakes against the real 2012 daemon behind ``test1``, each under
the repo's ``filterwarnings``, which starts at ``error`` and carries exactly
one scoped exemption (the FFDH deprecation from ``asyncssh.crypto.dh``) — so
any *other* deprecation a legacy key exchange raises is a failure here, not
noise on a user's terminal:

1. stock ``ssh_options`` reach the SHA-1-era tuple the 2026-08-22 loopback
   measurement found (``diffie-hellman-group14-sha1`` / ``aes256-ctr`` /
   ``hmac-sha1``), so the docs' "no configuration needed" stays true;
2. a per-host ``kex_algs`` changes what the wire negotiates — the ask that
   started this work;
3. a per-host legacy cipher list still connects end to end;
4. the DEBUG lines a user reads (``otto.host.connections``, not asyncssh's
   own trace) show the same thing, and never the guest's password.

The key-exchange algorithm is read from asyncssh's own debug-2 log line,
because asyncssh exposes ciphers and MACs through ``get_extra_info`` but
never the kex after the exchange. The hop's handshake logs one too (a modern
OpenSSH picks curve25519), so the assertion reads the HIGHEST ``conn=N`` id:
the target connects after its tunnel, so its connection is the later one.
"""

import logging
import re

import asyncssh
import pytest
import pytest_asyncio

from scripts.build_busybox_guest_images import GUEST_TABLE
from tests._fixtures.labdata import host_data
from tests.integration.busybox_bed.conftest import _build_guest, _require_guest

pytestmark = pytest.mark.timeout(180)

SSHD_GUESTS = [g.element for g in GUEST_TABLE if g.sshd == "dropbear"]
assert SSHD_GUESTS, (
    "no bed guest declares an sshd in GUEST_TABLE; this module is parametrized over "
    "that roster and would empty-param-SKIP rather than fail"
)

STOCK_KEX = "diffie-hellman-group14-sha1"
STOCK_CIPHER = "aes256-ctr"
STOCK_MAC = "hmac-sha1"
SERVER_BANNER = "SSH-2.0-dropbear_2012.55"


@pytest_asyncio.fixture(params=SSHD_GUESTS)
async def ssh_guest(request):
    """A factory: ``ssh_guest(**ssh_options)`` -> a UnixHost on term=ssh.

    ``ssh_guest.ne`` names the guest this parametrization is building, so a
    test that needs the guest's own committed record (its cred, for the
    password-redaction assertion) can look it up without re-deriving it.
    """
    _require_guest(request.param)
    opened = []

    def factory(**ssh_options):
        host, _version = _build_guest(request.param, term="ssh", ssh_options=ssh_options)
        opened.append(host)
        return host

    factory.ne = request.param
    yield factory
    for host in opened:
        await host.close()


@pytest.fixture
def asyncssh_debug2(monkeypatch):
    """Arms ``OTTO_SSH_DEBUG=2``, the env var a user sets for asyncssh's own trace.

    Yields nothing: this only raises the process-global debug level, it does
    not capture anything — a caller still has to separately request
    ``caplog`` and arm it itself (see the long note below).

    ``apply_ssh_debug_level`` (``otto.host.connections``) reads
    ``OTTO_SSH_DEBUG`` on every connect and calls ``asyncssh.set_debug_level``
    from it — setting the var here, rather than calling
    ``asyncssh.set_debug_level`` directly, exercises the env-var half of the
    mechanism (asyncssh's own debug level). It does NOT prove otto's own
    ``asyncssh`` logger floor lifts under the CLI — this fixture and the
    tests below arm ``caplog`` on the ``asyncssh`` logger itself, bypassing
    that floor entirely; ``test_connections_asyncssh_seam.py`` (hostless)
    proves the floor lifts. Restored to the value it had before this fixture
    armed it: the debug level is a process-global class attribute
    (``asyncssh.logging.SSHLogger._debug_level``) the env var can only
    raise, never lower back down on its own. The attribute is private (no
    public getter), but readable — ``test_connections_asyncssh_seam.py``
    already reads it the same way — so the prior value is saved and restored
    rather than reset to the ``1`` default, which would be wrong if a caller
    stacks this fixture inside a wider debug-level scope.

    Deliberately does NOT touch ``caplog``: pytest re-wraps caplog's own
    handler in a fresh ``catching_logs(level=<ini log_level>)`` at the start
    of EVERY phase (setup/call/teardown — ``_pytest/logging.py``
    ``_runtest_for``), so a level set during a fixture's SETUP half (before
    its ``yield``) is silently reset back to the ini floor (``INFO``) the
    instant the CALL phase begins — before the test body's own connect ever
    runs, and before any DEBUG record it logs can be kept. Each test below
    calls ``caplog.set_level(logging.DEBUG, logger="asyncssh")`` itself, as
    its first line, so the override lands inside the CALL phase instead.

    Also restores the ``asyncssh`` LOGGER level and ``management``'s
    remembered override table: since ``e3da2acb``, the first connect under
    this fixture runs ``apply_ssh_debug_level``, which now ALSO lifts
    otto's ``asyncssh`` logger floor to DEBUG and remembers that on
    ``management._state.level_overrides`` — left alone, that leaks the
    DEBUG floor into every later test in this process.
    """
    from asyncssh.logging import SSHLogger

    from otto.logger import management

    # Mirrors tests/conftest.py's own snapshot/restore of management._state.
    saved_debug_level = SSHLogger._debug_level
    saved_asyncssh_logger_level = logging.getLogger("asyncssh").level
    saved_overrides = dict(management._state.level_overrides)
    monkeypatch.setenv("OTTO_SSH_DEBUG", "2")
    try:
        yield
    finally:
        asyncssh.set_debug_level(saved_debug_level)
        logging.getLogger("asyncssh").setLevel(saved_asyncssh_logger_level)
        management._state.level_overrides.clear()
        management._state.level_overrides.update(saved_overrides)
        # Re-apply so the floored loggers match the restored override table.
        management.apply_library_levels()


_KEX_LINE_RE = re.compile(r"\[conn=(\d+)\]\s+Key exchange alg: (\S+)")


def _kex_by_conn(caplog) -> dict[int, str]:
    """{conn id: negotiated algorithm} for every handshake in this capture.

    Matched on the ``[conn=N]``-prefixed form ``SSHLogger.process`` puts on
    every message, and keyed on that id rather than taken by position, so a
    second connection in the same capture cannot be mistaken for the first:
    a caller picks the connection it means (the target's, the highest id —
    see the module docstring) instead of trusting log order.
    """
    return {
        int(m.group(1)): m.group(2)
        for r in caplog.records
        if (m := _KEX_LINE_RE.search(r.getMessage()))
    }


@pytest.mark.asyncio
async def test_stock_options_negotiate_the_sha1_era_tuple(ssh_guest, asyncssh_debug2, caplog):
    caplog.set_level(logging.DEBUG, logger="asyncssh")
    host = ssh_guest()
    out = (await host.run("echo legacy-ok")).only
    assert out.retcode == 0, out
    assert "legacy-ok" in out.value
    conn = await host._connections.ssh()
    assert conn.get_extra_info("server_version") == SERVER_BANNER
    assert conn.get_extra_info("send_cipher") == STOCK_CIPHER
    assert conn.get_extra_info("recv_cipher") == STOCK_CIPHER
    assert conn.get_extra_info("send_mac") == STOCK_MAC
    by_conn = _kex_by_conn(caplog)
    assert len(by_conn) == 2, by_conn  # the hop's handshake and the target's
    assert by_conn[max(by_conn)] == STOCK_KEX, by_conn


@pytest.mark.asyncio
async def test_a_per_host_kex_list_reaches_the_wire(ssh_guest, asyncssh_debug2, caplog):
    """The one algorithm the stock handshake would never pick, chosen because
    the host record said so."""
    caplog.set_level(logging.DEBUG, logger="asyncssh")
    host = ssh_guest(kex_algs=["diffie-hellman-group1-sha1"])
    out = (await host.run("echo group1-ok")).only
    assert "group1-ok" in out.value, out
    by_conn = _kex_by_conn(caplog)
    assert len(by_conn) == 2, by_conn  # the hop's handshake and the target's
    assert by_conn[max(by_conn)] == "diffie-hellman-group1-sha1", by_conn


@pytest.mark.asyncio
async def test_a_per_host_legacy_cipher_list_still_connects(ssh_guest):
    host = ssh_guest(encryption_algs=["3des-cbc"])
    out = (await host.run("echo cbc-ok")).only
    assert "cbc-ok" in out.value, out
    conn = await host._connections.ssh()
    assert conn.get_extra_info("send_cipher") == "3des-cbc"


@pytest.mark.asyncio
async def test_otto_logs_what_it_sent_and_what_was_negotiated(ssh_guest, caplog):
    """The user-facing half: ``--log-level DEBUG`` answers "were my options
    respected" without asyncssh's own trace."""
    caplog.set_level(logging.DEBUG, logger="otto.host")
    host = ssh_guest(kex_algs=["diffie-hellman-group14-sha1"])
    await host.run("true")
    assert "'kex_algs': ['diffie-hellman-group14-sha1']" in caplog.text
    negotiated = (
        f"negotiated server={SERVER_BANNER!r} cipher={STOCK_CIPHER}/{STOCK_CIPHER} "
        f"mac={STOCK_MAC}/{STOCK_MAC}"
    )
    assert negotiated in caplog.text
    assert "'password': '***'" in caplog.text
    # Derived from the committed record, not a copy of it, so a cred rotation
    # cannot leave this assertion vacuously green.
    secret = host_data(ssh_guest.ne)["creds"][0]["password"]
    assert secret, (
        f"{ssh_guest.ne} has an empty password; this redaction assertion would be vacuous"
    )
    assert f"'password': {secret!r}" not in caplog.text  # the guest password never appears
