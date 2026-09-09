"""
Unix-host integration tests (formerly tests/unit/host/test_host_integration.py).

These exercise behavior that is **specific to a POSIX shell** — bash builtins
(``echo``, ``cd``, ``export``), Linux commands (``uname -s``, ``ls``), and
transfer protocols (scp/sftp/ftp/nc). The contract these tests assert is
"this is what a Unix host does", not "this is what every otto host does";
the cross-OS contract lives in :mod:`test_host_contract` and is parametrized
over Unix and Zephyr backends both.

The file was renamed from ``test_host_integration`` and moved out of the
unit tree because it has no unit-test scope (every test carries
``@pytest.mark.integration`` and requires a Vagrant test VM). The
parametrized ``host1`` fixture lives in :mod:`tests.conftest` and resolves
``ssh`` / ``telnet`` / ``local`` to :class:`UnixHost` / :class:`LocalHost`.
"""

import hashlib
import logging
import time
import uuid
from pathlib import Path

import pytest

from otto.host.connections import teardown_step
from otto.host.host import Host
from otto.host.login_proxy import Cred
from otto.host.session import ShellSession
from otto.host.unix_host import UnixHost
from otto.utils import Status
from tests._fixtures.labdata import element_for
from tests.conftest import BUSYBOX_GUEST_NES, host_data, make_host
from tests.integration.host._transfer_retry import transfer_with_retry

pytestmark = pytest.mark.timeout(30)

_ALL_HOSTS = pytest.mark.parametrize("host1", ["ssh", "telnet", "local"], indirect=True)
_REMOTE_ONLY = pytest.mark.parametrize("host1", ["ssh", "telnet"], indirect=True)
_ALL_TRANSFERS = pytest.mark.parametrize(
    "transfer_host",
    [
        "scp",
        "sftp",
        "ftp",
        "nc",
        pytest.param(("nc", "telnet"), id="nc-telnet"),
        # The BusyBox bed's five pinned userlands, on the transfer their lab
        # entries resolve to — `shell`, over the telnet console, through the
        # test1 hop. This is where transfer parity across userland versions
        # is asserted: the same two round trips every other transfer takes.
        #
        # NO `nc` ROWS, and that is still a ruling rather than an omission
        # (Chris, 2026-08-21) — but NOT FOR THE ORIGINAL REASON, which expired
        # on 2026-08-25. It used to be incapability: otto's nc backend could
        # not round-trip with any BusyBox userland, GET refused up front and
        # PUT spawning an OpenBSD-spelling listener the applet does not parse.
        # The universal `nc -l -p PORT` spelling closed that gap, the refusal
        # and its test are deleted, and otto's nc now round-trips to these
        # guests like any other host.
        #
        # WHAT KEEPS THE ROWS OUT IS DIVISION OF LABOUR. This phase asks each
        # backend for DEPTH over ONE transport apiece — the two round trips
        # below, on the transfer each host's lab entry resolves to, which for
        # every BusyBox guest is `shell`. Crossing a guest's transports
        # against its transfers is the conformance bed venue's job, and it
        # already draws all five `bed-busybox[*:telnet:nc]` cells and puts a
        # byte-tripwire payload and a mode through each
        # (`tests/conformance/test_transfer_contract.py`). Each guest's entry
        # lists both — `valid_transfers: ["shell", "nc"]` — and `shell` is the
        # one it resolves to; an `nc` row here would force the other and take a
        # second copy of a measurement the crossing already owns, which is the
        # split that module's `applicable_cell` note describes from its side.
        *(pytest.param(("shell", ne), id=f"shell-{ne}") for ne in BUSYBOX_GUEST_NES),
    ],
    indirect=True,
)


# ---------------------------------------------------------------------------
# Basic command execution
# ---------------------------------------------------------------------------


@_ALL_HOSTS
class TestBasicCommands:
    @pytest.mark.asyncio
    async def test_echo(self, host1: Host):
        result = (await host1.run("echo hello")).only
        assert result.status == Status.Success
        assert "hello" in result.value

    @pytest.mark.asyncio
    async def test_multiple_commands_run_in_order(self, host1: Host):
        result = await host1.run(["echo first", "echo second"])
        assert result.status == Status.Success
        assert len(result) == 2
        assert "first" in result[0].value
        assert "second" in result[1].value

    @pytest.mark.asyncio
    async def test_uname_returns_linux(self, host1: Host):
        result = (await host1.run("uname -s")).only
        assert result.status == Status.Success
        assert "Linux" in result.value

    @pytest.mark.asyncio
    async def test_multiline_output(self, host1: Host):
        result = (await host1.run("echo -e 'line1\\nline2\\nline3'")).only
        assert result.status == Status.Success
        lines = result.value.strip().splitlines()
        assert len(lines) == 3

    @pytest.mark.asyncio
    async def test_failing_command_returns_failed_status(self, host1: Host):
        result = (await host1.run("ls /nonexistent_dir_otto_test")).only
        assert result.status == Status.Failed
        # GNU `ls` returns 2 for a missing path on every backend (ssh/telnet/local).
        assert result.retcode == 2

    @pytest.mark.asyncio
    async def test_unexpected_eof_returns_error(self, host1: Host):
        result = (await host1.run("exit 42")).only
        assert result.status == Status.Error
        assert result.retcode == -1

    @pytest.mark.asyncio
    async def test_overall_status_reflects_failure(self, host1: Host):
        result = await host1.run(["echo ok", "ls /nonexistent_dir_otto_test"])
        assert result.status == Status.Failed
        assert result[0].status == Status.Success
        assert result[1].status == Status.Failed


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------


@_ALL_HOSTS
class TestStatePersistence:
    @pytest.mark.asyncio
    async def test_cd_persists_between_commands(self, host1: Host):
        await host1.run("cd /")
        await host1.run("cd tmp")
        result = (await host1.run("pwd")).only
        assert result.status == Status.Success
        assert result.value.strip() == "/tmp"

    @pytest.mark.asyncio
    async def test_env_var_persists(self, host1: Host):
        await host1.run("export OTTO_TEST_VAR=hello123")
        result = (await host1.run("echo $OTTO_TEST_VAR")).only
        assert result.status == Status.Success
        assert "hello123" in result.value


# ---------------------------------------------------------------------------
# Timeout and recovery
# ---------------------------------------------------------------------------


@_ALL_HOSTS
class TestTimeout:
    @pytest.mark.asyncio
    async def test_timeout_returns_error(self, host1: Host):
        result = (await host1.run("sleep 999", timeout=0.1)).only
        assert result.status == Status.Error, (
            f"expected Status.Error, got {result.status!r}; "
            f"retcode={result.retcode!r} output={result.value!r}"
        )
        assert "timed out" in result.value, (
            f"expected 'timed out' in output; "
            f"status={result.status!r} retcode={result.retcode!r} output={result.value!r}"
        )
        # The SSH process is killed on timeout, surfacing the sentinel retcode.
        # ``LocalHost`` carries no ``term``; the assertion is ssh-scoped.
        if getattr(host1, "term", None) == "ssh":
            assert result.retcode == -1

    @pytest.mark.asyncio
    async def test_session_recovers_after_timeout(self, host1: Host):
        await host1.run("sleep 999", timeout=0.1)
        result = (await host1.run("echo recovered")).only
        assert result.status == Status.Success
        assert "recovered" in result.value


# ---------------------------------------------------------------------------
# Send / Expect
# ---------------------------------------------------------------------------


@_ALL_HOSTS
class TestSendExpect:
    @pytest.mark.asyncio
    async def test_python_repl(self, host1: Host):
        # Use -i to force interactive mode (local sessions use PIPE, not PTY)
        await host1.send("python3 -i -c ''\n")
        await host1.expect(r">>> ", timeout=5.0)
        await host1.send("print('otto_test')\n")
        output = await host1.expect(r">>> ", timeout=5.0)
        assert "otto_test" in output
        await host1.send("exit()\n")


# ---------------------------------------------------------------------------
# Named sessions
# ---------------------------------------------------------------------------


@_ALL_HOSTS
class TestNamedSessionIntegration:
    @pytest.mark.asyncio
    async def test_named_session_runs_command(self, host1: Host):
        mon = await host1.open_session("monitor")
        result = (await mon.run("echo hello")).only
        assert result.status == Status.Success
        assert "hello" in result.value
        await mon.close()

    @pytest.mark.asyncio
    async def test_two_sessions_have_independent_state(self, host1: Host):
        s1 = await host1.open_session("s1")
        s2 = await host1.open_session("s2")
        await s1.run("cd /tmp")
        await s2.run("cd /home")
        r1 = (await s1.run("pwd")).only
        r2 = (await s2.run("pwd")).only
        assert r1.value.strip() == "/tmp"
        assert "/home" in r2.value.strip()
        await s1.close()
        await s2.close()

    @pytest.mark.asyncio
    async def test_context_manager_removes_session_from_registry(self, host1: Host):
        async with await host1.open_session("monitor") as mon:
            assert "monitor" in host1._session_mgr._named_sessions
            result = (await mon.run("echo hi")).only
            assert result.status == Status.Success
        assert "monitor" not in host1._session_mgr._named_sessions

    @pytest.mark.asyncio
    async def test_host_close_closes_all_named_sessions(self, host1: Host):
        s1 = await host1.open_session("s1")
        s2 = await host1.open_session("s2")
        # Sessions initialize lazily on first I/O — run a command to make them alive
        await s1.run("echo init")
        await s2.run("echo init")
        assert s1.alive
        assert s2.alive
        await host1.close()
        assert not s1.alive
        assert not s2.alive
        assert host1._session_mgr._named_sessions == {}


# ---------------------------------------------------------------------------
# Incremental output logging
# ---------------------------------------------------------------------------


@_ALL_HOSTS
class TestIncrementalLogging:
    """Verify that command output is logged line-by-line as it arrives."""

    @pytest.mark.asyncio
    async def test_multiline_output_logged_incrementally(self, host1: Host, caplog):
        """Each output line appears as a separate log record with distinct timestamps."""
        with caplog.at_level(logging.INFO):
            caplog.clear()
            result = (
                await host1.run(
                    "for i in 1 2 3; do echo line_$i; sleep 0.05; done",
                    timeout=10.0,
                )
            ).only

        assert result.status == Status.Success
        # Each line should have been logged individually
        output_records = [
            r for r in caplog.records if hasattr(r, "host") and "line_" in r.getMessage()
        ]
        assert len(output_records) >= 3
        # Timestamps should be spread out (not all batched at the end)
        if len(output_records) >= 2:
            span = output_records[-1].created - output_records[0].created
            assert span > 0.05, "Log records should arrive incrementally, not all at once"

    @pytest.mark.asyncio
    async def test_long_running_command_logs_before_completion(self, host1: Host, caplog):
        """Output produced early is logged before the command finishes."""
        with caplog.at_level(logging.INFO):
            caplog.clear()
            start = time.time()
            result = (
                await host1.run(
                    "echo early_output; sleep 0.1; echo late_output",
                    timeout=10.0,
                )
            ).only
            end = time.time()

        assert result.status == Status.Success
        early_records = [
            r
            for r in caplog.records
            if hasattr(r, "host") and "early_output" in r.getMessage() and "> |" in r.getMessage()
        ]
        assert len(early_records) >= 1, "'early_output' should have been logged"
        # early_output should have been logged well before the command finished
        assert early_records[0].created - start < end - start, (
            "early_output should be logged before command completion"
        )

    @pytest.mark.asyncio
    async def test_no_sentinels_in_logs(self, host1: Host, caplog):
        """Sentinel markers should never appear in log output."""
        with caplog.at_level(logging.INFO):
            caplog.clear()
            result = (await host1.run("echo sentinel_test", timeout=10.0)).only

        assert result.status == Status.Success
        for record in caplog.records:
            msg = record.getMessage()
            assert "__OTTO_" not in msg, f"Sentinel found in log: {msg}"


# ---------------------------------------------------------------------------
# Multi-host reachability
# ---------------------------------------------------------------------------


@_REMOTE_ONLY
class TestReachability:
    """A second host on the bed is independently reachable alongside host1.

    Remote-only: the test builds a second :class:`UnixHost` matching host1's
    term, which has no ``local`` analogue (``LocalHost`` carries no ``term``).
    """

    @pytest.mark.asyncio
    async def test_both_hosts_reachable(self, host1: Host):
        kwargs: dict[str, str] = {"term": host1.term}
        if host1.term == "telnet":
            kwargs["transfer"] = "ftp"
        host2 = make_host("test2", **kwargs)
        try:
            for host in (host1, host2):
                result = (await host.run("echo ping")).only
                assert result.status == Status.Success
                assert "ping" in result.value
        finally:
            await host2.close()


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


class TestCredentials:
    @pytest.mark.asyncio
    async def test_second_credential_works(self):
        """Verify the non-default (test) user can log in and run commands."""
        data = host_data("test2")
        second_user = data["creds"][1]["login"]
        host = UnixHost(
            ip=data["ip"],
            user=second_user,
            element=element_for("test2"),
            creds=[Cred(**c) for c in data["creds"]],
            board=data.get("board"),
        )
        try:
            result = (await host.run("whoami")).only
            assert result.status == Status.Success
            assert second_user in result.value
        finally:
            await host.close()

    @pytest.mark.serial_timing
    @pytest.mark.asyncio
    async def test_telnet_bad_credentials_fails_fast(self, monkeypatch):
        """A telnet login with a wrong password must raise a clear error
        promptly — not hang.

        Telnet ``login()`` no longer drains to silence; the bounded marker
        handshake in ``_ensure_initialized`` is what catches a failed login
        (the device stays in its login-prompt loop, so the READY marker can
        never appear). The timeout is shrunk here so the test stays fast.
        """
        monkeypatch.setattr(ShellSession, "_init_timeout", 3.0)

        data = host_data("test1")
        user = data["creds"][0]["login"]
        host = UnixHost(
            ip=data["ip"],
            user=user,
            element=element_for("test1"),
            creds=[Cred(login=user, password="definitely-the-wrong-password")],
            board=data.get("board"),
            term="telnet",
            transfer="ftp",
        )
        try:
            start = time.monotonic()
            with pytest.raises(ConnectionError):
                await host.run("echo hello")
            elapsed = time.monotonic() - start
            # Bounded by the shrunk handshake timeout — proves it did not hang.
            assert elapsed < 15
        finally:
            await host.close()


# ---------------------------------------------------------------------------
# user= authenticates as that user
# ---------------------------------------------------------------------------


@pytest.mark.timeout(60)
class TestUserAuthenticates:
    """``exec``/``put``/``get`` with ``user=`` really AUTHENTICATE as that user.

    The unix family declares ``exec_user``/``put_user``/``get_user`` as
    :attr:`~otto.host.capability_grid.UserSupport.authenticate` — the verb rides
    a connection opened as that user, not an elevation from the login user — and
    :func:`otto.testing.assert_host_conforms` only probes that declaration under
    a dry run, where nothing authenticates. These rows are the live half: each
    one carries a discriminator that a decoration (a chown, a ``sudo``, an
    ignored kwarg) could not produce.

    One host per row, built as the DEFAULT login with BOTH bed credentials in
    ``creds``, so the second login is a direct-cred user and ``user=`` has a
    credential to authenticate with. Both halves of a row — the ``user=`` call
    and its no-``user=`` control — are made against that one host, so they
    differ in nothing but the kwarg.

    SCOPE: the default ``scp`` backend only. ``ftp`` is excluded by the API
    itself (``put``/``get`` refuse ``user=`` there), and ``sftp``/``nc``, which
    ``test2`` also lists, stay mock-only — a reader should not take these rows
    as evidence about those two.
    """

    @staticmethod
    def _host_and_logins() -> tuple[UnixHost, str, str]:
        """Build the bed host plus its (default, second) logins.

        Fails — never skips — when the bed's two credentials are the same
        login, because then no assertion below could tell the two identities
        apart and a green would mean nothing.
        """
        data = host_data("test2")
        default_login = data["creds"][0]["login"]
        second = data["creds"][1]["login"]
        assert second != default_login, (
            f"bed data for test2 lists the same login twice ({second!r}); "
            f"user= cannot be discriminated from the default identity"
        )
        host = UnixHost(
            ip=data["ip"],
            user=default_login,
            element=element_for("test2"),
            creds=[Cred(**c) for c in data["creds"]],
            board=data.get("board"),
        )
        return host, default_login, second

    @staticmethod
    async def _cleanup(host: UnixHost, cmd: str, second: str) -> None:
        """Best-effort remote cleanup as both identities, then close the host.

        Each ``rm`` runs under :func:`~otto.host.connections.teardown_step`, so a
        cleanup that fails against an already-broken transport logs instead of
        replacing the assertion that diagnosed the real bug; ``close()`` sits in
        its own ``finally`` arm so it runs whatever the removals did. This is the
        shape ``.ast-grep/rules/no-awaited-exec-in-finally.yml`` mandates in
        ``src/``; the hazard is identical here.
        """
        try:
            with teardown_step(host.name, "rm-as-second"):
                await host.exec(cmd, user=second)
            with teardown_step(host.name, "rm-as-login"):
                await host.exec(cmd)
        finally:
            await host.close()

    @pytest.mark.asyncio
    async def test_exec_user_runs_as_that_user(self):
        """``id -un`` is the discriminator: the kernel's answer for the
        identity the exec channel authenticated as.

        A decoration could not move it — the same host, one call apart,
        must report two different logins.
        """
        host, default_login, second = self._host_and_logins()
        try:
            as_second = await host.exec("id -un", user=second)
            assert as_second.status == Status.Success, (
                f"exec as {second} failed: {as_second.value!r}"
            )
            assert as_second.value.strip() == second, (
                f"exec(user={second!r}) ran as {as_second.value.strip()!r}"
            )

            as_default = await host.exec("id -un")
            assert as_default.status == Status.Success, f"exec failed: {as_default.value!r}"
            assert as_default.value.strip() == default_login, (
                f"plain exec ran as {as_default.value.strip()!r}, not the login user"
            )
        finally:
            await host.close()

    @pytest.mark.asyncio
    async def test_put_user_lands_files_owned_by_that_user(self, tmp_path: Path):
        """Ownership of the landed bytes is the discriminator.

        The transfer rides the second user's own connection, so the file lands
        owned by them with no chown step; the control ``put`` in the same
        directory, same run, lands owned by the login user. A world-writable
        scratch directory keeps the two halves symmetric — neither write is
        privileged by the directory. Size is read alongside owner so an
        authenticated-but-empty landing cannot pass for a transfer.
        """
        host, default_login, second = self._host_and_logins()
        remote_dir = f"/tmp/otto_put_user_{uuid.uuid4().hex}"
        try:
            mk = await host.exec(f"mkdir -m 777 {remote_dir}")
            assert mk.status == Status.Success, f"could not make {remote_dir}: {mk.value!r}"

            by_second = tmp_path / "by_second.txt"
            by_second.write_text("as the second user")
            by_default = tmp_path / "by_default.txt"
            by_default.write_text("as the login user")

            res = await transfer_with_retry(
                lambda: host.put([by_second], Path(remote_dir), user=second)
            )
            assert res.status == Status.Success, f"put(user={second!r}) failed: {res.msg}"
            landed = await host.exec(f"stat -c %U:%s {remote_dir}/{by_second.name}")
            assert landed.status == Status.Success, f"stat failed: {landed.value!r}"
            assert landed.value.strip() == f"{second}:{len(by_second.read_bytes())}", (
                f"put(user={second!r}) landed {landed.value.strip()!r}, expected "
                f"{second}:{len(by_second.read_bytes())} (owner:size)"
            )

            res = await transfer_with_retry(lambda: host.put([by_default], Path(remote_dir)))
            assert res.status == Status.Success, f"control put failed: {res.msg}"
            landed = await host.exec(f"stat -c %U:%s {remote_dir}/{by_default.name}")
            assert landed.status == Status.Success, f"stat failed: {landed.value!r}"
            assert landed.value.strip() == f"{default_login}:{len(by_default.read_bytes())}", (
                f"control put landed {landed.value.strip()!r}, expected "
                f"{default_login}:{len(by_default.read_bytes())} (owner:size)"
            )
        finally:
            await self._cleanup(host, f"rm -rf {remote_dir}", second)

    @pytest.mark.asyncio
    async def test_get_user_reads_with_that_users_permissions(self, tmp_path: Path):
        """A file only the second user may read is the discriminator.

        ``get(user=second)`` must bring the bytes back; the identical ``get``
        without ``user=`` must FAIL, because the login user cannot open a
        0600 file owned by somebody else. A ``user=`` that merely decorated
        the call would make both halves succeed. The refusal is read where the
        API documents it — the per-file ``dict[Path, Result]`` entry — and the
        refused destination is asserted EMPTY, so a backend that creates the
        local file and only then fails cannot pass as a clean refusal.
        """
        host, default_login, second = self._host_and_logins()
        remote_path = f"/tmp/otto_get_user_{uuid.uuid4().hex}"
        try:
            uid = await host.exec("id -u")
            assert uid.status == Status.Success, f"id -u failed: {uid.value!r}"
            assert uid.value.strip() != "0", (
                f"the bed's default login for test2 ({default_login!r}) is root; "
                f"file permissions cannot discriminate an authenticated read, so "
                f"this test needs an unprivileged default user"
            )

            made = await host.exec(f"umask 077 && printf secret > {remote_path}", user=second)
            assert made.status == Status.Success, f"could not write {remote_path}: {made.value!r}"
            perms = await host.exec(f"stat -c %U:%a {remote_path}")
            assert perms.status == Status.Success, f"stat failed: {perms.value!r}"
            assert perms.value.strip() == f"{second}:600", (
                f"{remote_path} is {perms.value.strip()!r}; the test needs it owned "
                f"by {second!r} and unreadable by anyone else"
            )

            allowed_dir = tmp_path / "as_second"
            allowed_dir.mkdir()
            res = await transfer_with_retry(
                lambda: host.get([Path(remote_path)], allowed_dir, user=second)
            )
            assert res.status == Status.Success, f"get(user={second!r}) failed: {res.msg}"
            got = allowed_dir / Path(remote_path).name
            assert got.read_bytes() == b"secret", (
                f"{got} came back as {got.read_bytes()!r}, not the bytes written as {second!r}"
            )

            refused_dir = tmp_path / "as_default"
            refused_dir.mkdir()
            res = await transfer_with_retry(lambda: host.get([Path(remote_path)], refused_dir))
            assert not res.is_ok, (
                f"get without user= read a file only {second!r} may read "
                f"(status {res.status}); the transfer did not run as the login user"
            )
            entry = res.value[Path(remote_path)]
            assert not entry.is_ok, (
                f"the per-file entry for {remote_path} is {entry.status}; the aggregate "
                f"failed for some other reason than this path being refused"
            )
            assert "denied" in (entry.msg or "").lower(), (
                f"the refused get failed for the wrong reason: {entry.msg!r}"
            )
            assert list(refused_dir.iterdir()) == [], (
                f"the refused get still left {[p.name for p in refused_dir.iterdir()]} behind"
            )
        finally:
            await self._cleanup(host, f"rm -f {remote_path}", second)


# ---------------------------------------------------------------------------
# File transfer (SCP, SFTP, FTP, nc — and `shell` on the five BusyBox guests)
# ---------------------------------------------------------------------------


# NOTE: Transfers go through asyncssh/scp/sftp and have been observed to hang
# indefinitely when the remote SSH daemon stalls mid-protocol. get/put are
# wrapped in ``transfer_with_retry`` so an individual transfer is bounded
# and retried once, preventing the whole suite from blocking on a single flake.
@_ALL_TRANSFERS
class TestFileTransfer:
    @pytest.mark.asyncio
    async def test_get_file(self, transfer_host: UnixHost, tmp_path: Path):
        """Download /etc/passwd and verify the bytes against the host's own md5sum.

        ``/etc/passwd``, not ``/etc/hostname``, because the busybox rows added
        with the bed have no ``/etc/hostname``: their whole rootfs is the
        initramfs ``scripts/build_busybox_guest_images.py`` builds, and it sets
        the hostname with the ``hostname`` applet rather than by shipping the
        file. ``/etc/passwd`` is on every backend this fixture can produce —
        it is a member of that initramfs and of every unix pool host — so the
        row asserts otto's transfer instead of a rootfs layout.

        Verified by checksum rather than by text so the assertion is
        byte-exact, and so the only thing crossing the console is 32 hex
        characters: the ``nc-telnet`` row reads its truth through a telnet
        session, where ``cat``-ing a multi-kilobyte file back would be
        asserting the console's fidelity, not the transfer's.
        """
        digest = (await transfer_host.run("md5sum /etc/passwd")).only
        assert digest.status == Status.Success, f"md5sum failed: {digest.value!r}"
        expected = digest.value.split()[0]

        res = await transfer_with_retry(lambda: transfer_host.get([Path("/etc/passwd")], tmp_path))
        assert res.status == Status.Success, f"get failed: {res.msg}"

        landed = (tmp_path / "passwd").read_bytes()
        actual = hashlib.md5(landed, usedforsecurity=False).hexdigest()
        assert actual == expected, (
            f"/etc/passwd came back with a different checksum "
            f"({actual} local vs {expected} on the host); {len(landed)} bytes landed"
        )

    @pytest.mark.asyncio
    async def test_put_file(self, transfer_host: UnixHost, tmp_path: Path):
        """Upload a file, verify it arrived, clean up."""
        content = "file transfer test"
        src = tmp_path / f"otto_{transfer_host.transfer}_{transfer_host.term}_upload.txt"
        src.write_text(content)
        remote_path = f"/tmp/otto_{transfer_host.transfer}_{transfer_host.term}_upload.txt"

        res = await transfer_with_retry(lambda: transfer_host.put([src], Path("/tmp")))
        assert res.status == Status.Success, f"put failed: {res.msg}"

        result = (await transfer_host.run(f"cat {remote_path}")).only
        assert content in result.value

        await transfer_host.run(f"rm -f {remote_path}")
