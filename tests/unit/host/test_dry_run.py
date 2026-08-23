"""Tests for dry-run mode on hosts (via OttoContext)."""

import base64
import copy
import dataclasses
import json
import logging
import re
from contextlib import contextmanager
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from otto.host import userland as userland_module
from otto.host.app_shell import AppShell
from otto.host.docker_host import DockerContainerHost
from otto.host.file_ops import refuse_if_base64_is_absent
from otto.host.host import HostFilter, is_dry_run
from otto.host.local_host import LocalHost
from otto.host.login_proxy import Cred
from otto.host.power import PowerController, PowerState
from otto.host.session import DeclinedSession, HostSession, SessionManager, ShellSession
from otto.host.transfer.base import aggregate_transfer
from otto.host.unix_host import UnixHost
from otto.logger import management
from otto.logger.mode import LogMode
from otto.result import CommandNotRunError, CommandResult, NotRunResult, Result, Results
from otto.utils import Status
from tests.conftest import active_context


class TestGlobalDryRun:
    def test_global_flag_defaults_to_false(self):
        assert is_dry_run() is False

    # ── LocalHost ─────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_localhost_run_declines(self):
        with active_context(dry_run=True):
            host = LocalHost()
            result = (await host.run("echo hello")).only

            assert result.status == Status.NotRun
            assert result.retcode == -1
            assert result.command == "echo hello"
            # The banner used to sit in `.value`, where it read as the
            # command's output; reading a decline's answer now raises.
            with pytest.raises(CommandNotRunError):
                _ = result.value

    @pytest.mark.asyncio
    async def test_localhost_run_does_not_spawn_subprocess(self):
        with active_context(dry_run=True):
            host = LocalHost()
            result = (await host.run("exit 1")).only

            assert result.status == Status.NotRun
            assert result.retcode == -1

    @pytest.mark.asyncio
    async def test_localhost_run_list_declines_every_command(self):
        with active_context(dry_run=True):
            host = LocalHost()
            result = await host.run(["cmd1", "cmd2", "cmd3"])

            assert len(result) == 3
            for r in result:
                assert r.status == Status.NotRun
            # The aggregate folded to Success while the entries were Skipped;
            # a NotRun entry makes it non-ok, which is the point.
            assert result.status == Status.NotRun

    @pytest.mark.asyncio
    async def test_localhost_exec_declines(self):
        with active_context(dry_run=True):
            host = LocalHost()
            result = await host.exec("echo hello")

            assert result.status == Status.NotRun
            assert result.retcode == -1

    @pytest.mark.asyncio
    async def test_localhost_send_is_noop(self):
        with active_context(dry_run=True):
            host = LocalHost()
            await host.send("some text")

    @pytest.mark.asyncio
    async def test_localhost_expect_declines_instead_of_reporting_no_match(self):
        # Was `assert result == ""`. The empty string is not "nothing
        # happened", it is a MATCH RESULT — `if "READY" in await
        # host.expect(...)` reads it as "the pattern did not appear".
        with active_context(dry_run=True), pytest.raises(CommandNotRunError) as exc:
            await LocalHost().expect("some_pattern")
        assert "some_pattern" in str(exc.value)

    @pytest.mark.asyncio
    async def test_localhost_put_declines(self):
        with active_context(dry_run=True):
            host = LocalHost()
            files = [Path("/tmp/file1.txt"), Path("/tmp/file2.txt")]
            dest = Path("/tmp/dest")

            result = await host.put(files, dest)

            assert result.status == Status.NotRun
            assert "[DRY RUN]" in result.msg
            assert "PUT" in result.msg

    @pytest.mark.asyncio
    async def test_localhost_put_dry_run_banner_shows_normalized_mode(self):
        with active_context(dry_run=True):
            host = LocalHost()

            result = await host.put([Path("/tmp/app.bin")], Path("/opt/bin"), mode="755")

            assert result.status == Status.NotRun
            # Normalized to octal in the banner, so a dry run shows the value
            # that would actually be applied rather than echoing the input.
            assert "(mode 0o755)" in result.msg

    @pytest.mark.asyncio
    async def test_localhost_put_dry_run_without_mode_has_no_mode_suffix(self):
        with active_context(dry_run=True):
            host = LocalHost()

            result = await host.put([Path("/tmp/app.bin")], Path("/opt/bin"))

            assert result.status == Status.NotRun
            assert "mode" not in result.msg

    @pytest.mark.asyncio
    async def test_localhost_put_dry_run_rejects_bad_octal(self):
        # A typo'd mode is the caller's own input and needs no host contact,
        # so a dry run must catch it rather than reporting a clean plan.
        with active_context(dry_run=True):
            host = LocalHost()
            src = Path("/tmp/app.bin")

            result = await host.put([src], Path("/opt/bin"), mode="789")

            assert result.status == Status.Error
            assert "789" in result.msg
            # Keeps the documented per-file mapping shape on the failure path.
            assert result.value[src].status == Status.Error

    @pytest.mark.asyncio
    async def test_localhost_get_declines(self):
        with active_context(dry_run=True):
            host = LocalHost()
            files = [Path("/tmp/file.bin")]
            dest = Path("/tmp/dest")

            result = await host.get(files, dest)

            assert result.status == Status.NotRun
            assert "[DRY RUN]" in result.msg
            assert "GET" in result.msg

    # ── UnixHost ──────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_remotehost_run_declines(self):
        with active_context(dry_run=True):
            host = UnixHost(
                ip="10.0.0.1",
                element="box",
                creds=[Cred(login="user", password="pass")],
                log=LogMode.QUIET,
            )
            result = (await host.run("ls -la")).only

            assert result.status == Status.NotRun
            assert result.retcode == -1
            assert result.command == "ls -la"
            with pytest.raises(CommandNotRunError):
                _ = result.value
            assert host._connections._ssh_conn is None

    @pytest.mark.asyncio
    async def test_remotehost_exec_declines(self):
        with active_context(dry_run=True):
            host = UnixHost(
                ip="10.0.0.1",
                element="box",
                creds=[Cred(login="user", password="pass")],
                log=LogMode.QUIET,
            )
            result = await host.exec("uname -a")

            assert result.status == Status.NotRun
            assert result.retcode == -1
            assert host._connections._ssh_conn is None

    @pytest.mark.asyncio
    async def test_remotehost_run_list_declines_every_command(self):
        with active_context(dry_run=True):
            host = UnixHost(
                ip="10.0.0.1",
                element="box",
                creds=[Cred(login="user", password="pass")],
                log=LogMode.QUIET,
            )
            result = await host.run(["cmd1", "cmd2", "cmd3"])

            assert len(result) == 3
            for r in result:
                assert r.status == Status.NotRun
            assert host._connections._ssh_conn is None

    @pytest.mark.asyncio
    async def test_remotehost_send_is_noop(self):
        with active_context(dry_run=True):
            host = UnixHost(
                ip="10.0.0.1",
                element="box",
                creds=[Cred(login="user", password="pass")],
                log=LogMode.QUIET,
            )
            await host.send("some text")
            assert host._connections._ssh_conn is None

    @pytest.mark.asyncio
    async def test_remotehost_expect_declines_and_dials_nothing(self):
        with active_context(dry_run=True):
            host = UnixHost(
                ip="10.0.0.1",
                element="box",
                creds=[Cred(login="user", password="pass")],
                log=LogMode.QUIET,
            )
            with pytest.raises(CommandNotRunError) as exc:
                await host.expect("some_pattern")

            assert "some_pattern" in str(exc.value)
            assert host._connections._ssh_conn is None

    @pytest.mark.asyncio
    async def test_remotehost_put_declines(self):
        with active_context(dry_run=True):
            host = UnixHost(
                ip="10.0.0.1",
                element="box",
                creds=[Cred(login="user", password="pass")],
                log=LogMode.QUIET,
            )
            files = [Path("/tmp/file1.txt"), Path("/tmp/file2.txt")]
            dest = Path("/remote/dest")

            result = await host.put(files, dest)

            assert result.status == Status.NotRun
            assert "[DRY RUN]" in result.msg
            assert "PUT" in result.msg
            assert host._connections._ssh_conn is None

    @pytest.mark.asyncio
    async def test_remotehost_get_declines(self):
        with active_context(dry_run=True):
            host = UnixHost(
                ip="10.0.0.1",
                element="box",
                creds=[Cred(login="user", password="pass")],
                log=LogMode.QUIET,
            )
            files = [Path("/remote/file.bin")]
            dest = Path("/local/dest")

            result = await host.get(files, dest)

            assert result.status == Status.NotRun
            assert "[DRY RUN]" in result.msg
            assert "GET" in result.msg


def _dry_run_host() -> UnixHost:
    """A host whose ``exec`` a dry run answers synthetically. Build inside the context."""
    return UnixHost(
        ip="10.0.0.1",
        element="box",
        creds=[Cred(login="user", password="pass")],
        log=LogMode.QUIET,
    )


class TestDryRunMeasuresNothing:
    """A dry run must not manufacture a userland MEASUREMENT.

    ``BaseHost._dry_run_result`` answered every command with ``retcode=0``
    without leaving this machine, and :meth:`otto.host.userland.Userland._probe`
    reads exactly that exit code -- so before ``Userland._send`` learned to
    decline, a dry-run resolution SETTLED six capabilities on answers nobody
    took, and :meth:`~otto.host.userland.Userland.as_lab_json` then offered
    them under ``"userland_options"`` as a payload a maintainer pastes into
    lab data permanently. Settled is what makes a value pasteable, so
    ``is_settled`` is the assertion that matters here; the empty pin and the
    quiet log line are the two surfaces it reaches.

    The hostile condition is INJECTED rather than inherited: these build a real
    :class:`~otto.host.unix_host.UnixHost` inside ``active_context(dry_run=True)``
    so the answer under assertion is the product's own and not a test double's.
    The primitive now returns a ``Status.NotRun`` decline rather than that
    ``retcode 0``, which closes the same hole a second time from below --
    these stay because ``Userland._send``'s decline is what must not regress,
    and it is a different line.

    ``otto host <id> probe`` short-circuits above all of this
    (``_dry_run_report``) and is guarded in ``test_userland_probe.py``. That
    branch is a message, not the guard -- these are the guard, and they cover
    every command that resolves a userland rather than the one verb.
    """

    @pytest.mark.asyncio
    async def test_dry_run_settles_no_capability(self):
        # THE BUG ITSELF. Anything settled here is a fabricated measurement.
        with active_context(dry_run=True):
            userland = _dry_run_host()._userland()
            await userland.resolve()

            settled = [n for n in userland_module._UNASKABLE_DEFAULTS if userland.is_settled(n)]
            assert settled == [], (
                f"a dry run settled {settled} without reaching any device, so those values "
                "are guesses recorded as measurements"
            )

    @pytest.mark.asyncio
    async def test_dry_run_pin_is_empty(self):
        with active_context(dry_run=True):
            userland = _dry_run_host()._userland()
            await userland.resolve()

            pin = userland.as_lab_json()
            assert pin == {}, (
                f"a dry run offered a pasteable pin: {json.dumps(pin, sort_keys=True)}"
            )

    @pytest.mark.asyncio
    async def test_dry_run_pin_log_line_offers_no_value(self, caplog):
        # The pin reached users through this DEBUG line before the `probe` verb
        # existed, and it is still how a resolution outside that verb advertises
        # itself -- so an empty `as_lab_json()` is only half the property.
        caplog.set_level(logging.DEBUG, logger="otto.host.userland")
        with active_context(dry_run=True):
            await _dry_run_host()._userland().resolve()

        pins = [r.getMessage() for r in caplog.records if "pin these to skip" in r.getMessage()]
        assert pins, "the pin log line vanished; this guard would no longer see a bad one"
        assert pins == ['userland: pin these to skip the probes -- "userland_options": {}'], (
            f"a dry run advertised values to pin: {pins}"
        )

    @pytest.mark.asyncio
    async def test_dry_run_issues_no_probe_at_all(self, monkeypatch):
        # Not merely "the answers are discarded": the probes are not issued, so
        # the `[DRY RUN]` echo lines they used to print are gone with them. That
        # makes the dry run MORE faithful, not less -- a real resolution issues
        # them at `LogMode.NEVER`, so a real run shows none of them either.
        calls: list[str] = []
        real_exec = UnixHost.exec

        async def recording(self, cmd, *a, **kw):
            calls.append(cmd)
            return await real_exec(self, cmd, *a, **kw)

        monkeypatch.setattr(UnixHost, "exec", recording)
        with active_context(dry_run=True):
            await _dry_run_host()._userland().resolve()

        assert calls == [], f"a dry run issued probes: {calls}"

    @pytest.mark.asyncio
    async def test_dry_run_capabilities_read_their_assumed_defaults(self):
        # What settling nothing leaves in force. `assumed` is not a degraded
        # reading -- it is exactly what otto did before it asked anything.
        with active_context(dry_run=True):
            userland = _dry_run_host()._userland()
            await userland.resolve()

            in_force = {n: userland._get(n) for n in userland_module._UNASKABLE_DEFAULTS}
            assert in_force == userland_module._UNASKABLE_DEFAULTS

    @pytest.mark.asyncio
    async def test_dry_run_elevated_run_still_shows_the_elevated_command(self):
        # Consequence, measured: `elevation` now comes from the assumed default
        # rather than from a fabricated probe, and that default IS `sudo`, so
        # the command a dry run reports is byte-identical to before. The dry run
        # stays informative rather than going silent.
        #
        # PASSES ON BOTH SIDES OF THE CHANGE, deliberately and stated rather
        # than left to be discovered: it is a NO-CHANGE guard, and the thing it
        # would catch is a future `_UNASKABLE_DEFAULTS["elevation"]` that stops
        # agreeing with what `_elevate` builds when there is no resolver at all.
        with active_context(dry_run=True):
            host = _dry_run_host()
            result = (await host.run("systemctl restart nginx", sudo=True)).only

            assert result.status == Status.NotRun
            assert result.command == "sudo -S -p 'otto-sudo:' systemctl restart nginx"
            assert host._connections._ssh_conn is None

    @pytest.mark.asyncio
    async def test_dry_run_does_not_refuse_on_an_unmeasured_base64(self):
        # `refuse_if_base64_is_absent` keys on `absent` AND settled, and this
        # change is what puts weight on the second half. A dry run used to
        # settle `base64_flag` on the fabricated `-d`, so the `absent` half
        # alone kept it from refusing; it now holds the `absent` DEFAULT, and
        # `is_settled` is the only thing between a dry run and a refusal about
        # a device otto never contacted -- the expensive direction.
        #
        # MEASURED, because that difference is invisible in the outcome: this
        # passes on both sides of the change, but deleting the `is_settled`
        # clause from `refuse_if_base64_is_absent` reddens it here and does
        # NOT redden it against the pre-change product.
        with active_context(dry_run=True):
            await refuse_if_base64_is_absent(
                _dry_run_host()._userland(), host="box", attempted="read_file('/etc/hostname')"
            )


class _Sink(logging.Handler):
    """A handler that keeps every record it is offered, in order.

    Pinned at ``INFO``, matching the real console handler's floor and NOT the
    logger's. A sink at ``DEBUG`` would accept a ``_log_command`` demoted from
    ``logger.info`` to ``logger.debug``, so every mode assertion below would
    still pass while the line had in fact vanished from a default-level console.
    At ``INFO`` that demotion takes the positive controls out with it.
    """

    def __init__(self):
        super().__init__(level=logging.INFO)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def lines(self) -> list[str]:
        return [r.getMessage() for r in self.records]

    def mode_of(self, needle: str) -> LogMode:
        """The ``log_mode`` extra on the one record containing *needle*."""
        hits = [r for r in self.records if needle in r.getMessage()]
        assert len(hits) == 1, f"expected exactly one record for {needle!r}, got {hits}"
        return getattr(hits[0], "log_mode", LogMode.NORMAL)


@contextmanager
def _two_sided_sinks():
    """otto's real sink topology, in miniature: a console side behind ``HostFilter``.

    ``attach_console_suppress_filter`` applies :class:`~otto.host.host.HostFilter`
    to the console + ``console.log`` handlers and NOT to ``verbose.log``
    (asserted end-to-end in ``tests/unit/logger/test_management.py``), so a
    ``QUIET`` command is dropped on one side and kept on the other. Both sinks
    are attached directly to the emitting logger rather than relying on
    propagation, so no other test's logging configuration can decide the
    outcome here.
    """
    console, verbose = _Sink(), _Sink()
    console.addFilter(HostFilter())
    log = logging.getLogger("otto.host.host")
    prior_level = log.level
    log.setLevel(logging.DEBUG)
    log.addHandler(console)
    log.addHandler(verbose)
    try:
        yield console, verbose
    finally:
        log.removeHandler(console)
        log.removeHandler(verbose)
        log.setLevel(prior_level)


def _local(log: LogMode = LogMode.NORMAL) -> LocalHost:
    return LocalHost(log=log)


def _unix(log: LogMode = LogMode.NORMAL) -> UnixHost:
    return UnixHost(
        ip="10.0.0.1", element="box", creds=[Cred(login="user", password="pass")], log=log
    )


def _docker(log: LogMode = LogMode.NORMAL) -> DockerContainerHost:
    # The parent is a real host and is never touched: every dry-run branch
    # exercised here returns before `_ensure_running()` reaches it.
    return DockerContainerHost(
        parent=_unix(),
        container_id="abc123def456",
        project="repo1",
        service="api",
        compose_project="otto-repo1-vagrant",
        log=log,
    )


_FAMILIES = [
    pytest.param(_local, id="local"),
    pytest.param(_unix, id="unix"),
    pytest.param(_docker, id="docker"),
]


class TestDryRunHonoursTheCallersLogMode:
    """A dry run must keep off the console exactly what a real run keeps off it.

    ``BaseHost._dry_run_result`` took only ``cmd``, so the caller's ``log`` was
    dropped on the floor and the synthetic line went out at the default
    ``NORMAL``. :meth:`~otto.host.file_ops.HostFileOps.write_file` sends the
    file's whole body base64-encoded at ``LogMode.QUIET`` for exactly this
    reason, so ``--dry-run`` on a credentials file printed the credentials --
    the dry run, the mode a user reaches for to make LESS happen, showed MORE
    than the real thing. ``send()`` had the same defect in a different shape:
    it computed the folded mode and then consulted it only for ``NEVER``.

    Each test carries a POSITIVE CONTROL in the same block and the same sinks
    -- a ``NORMAL`` command that MUST appear. Without it "nothing was logged"
    is satisfied by a sink that was never wired, a logger left below INFO, or
    a filter suppressing for some unrelated reason, and the guard would pass
    against a product that had regressed completely.

    The hostile condition is INJECTED, not inherited: real hosts are built
    inside ``active_context(dry_run=True)`` and the quiet mode is passed by the
    test, so the ``[DRY RUN]`` line under assertion is the product's own.
    """

    @pytest.mark.asyncio
    async def test_exec_keeps_a_per_command_quiet_line_off_the_console(self):
        # BaseHost.exec's dry-run arm (host.py) -- the one `write_file` takes.
        with active_context(dry_run=True), _two_sided_sinks() as (console, verbose):
            host = _local()
            await host.exec("cat /etc/shadow", log=LogMode.QUIET)
            await host.exec("uname -a")  # positive control

        assert any("uname -a" in ln for ln in console.lines()), (
            "the control never reached the console sink, so this test cannot "
            f"tell suppression from a dead sink; saw {console.lines()}"
        )
        assert not any("/etc/shadow" in ln for ln in console.lines()), (
            f"a dry run put a QUIET command on the console: {console.lines()}"
        )
        assert verbose.mode_of("/etc/shadow") is LogMode.QUIET
        assert verbose.mode_of("uname -a") is LogMode.NORMAL

    @pytest.mark.asyncio
    async def test_exec_folds_the_hosts_standing_quiet_mode(self):
        # `exec` hands the RAW mode to `_exec_one` and each family folds, so
        # this one is only honoured because `_dry_run_result` folds internally.
        # A host under `SuppressCommandOutput` running a NORMAL command is the
        # shape: nothing per-command is quiet, the HOST is.
        with active_context(dry_run=True), _two_sided_sinks() as (console, verbose):
            await _local(log=LogMode.QUIET).exec("cat /etc/shadow")
            await _local().exec("uname -a")  # positive control, same sinks

        assert any("uname -a" in ln for ln in console.lines()), (
            f"the control never reached the console sink; saw {console.lines()}"
        )
        assert not any("/etc/shadow" in ln for ln in console.lines()), (
            "a dry run on a host with a standing QUIET mode put its command on "
            f"the console: {console.lines()}"
        )
        assert verbose.mode_of("/etc/shadow") is LogMode.QUIET

    @pytest.mark.asyncio
    @pytest.mark.parametrize("build", _FAMILIES)
    async def test_run_keeps_a_quiet_command_off_the_console(self, build):
        # The three `_run_one` overrides. Their real arm already folds via
        # `_effective_log`; the dry-run arm returned above it.
        with active_context(dry_run=True), _two_sided_sinks() as (console, verbose):
            host = build()
            await host.run("cat /etc/shadow", log=LogMode.QUIET)
            await host.run("uname -a")  # positive control

        assert any("uname -a" in ln for ln in console.lines()), (
            f"the control never reached the console sink; saw {console.lines()}"
        )
        assert not any("/etc/shadow" in ln for ln in console.lines()), (
            f"a dry run put a QUIET command on the console: {console.lines()}"
        )
        assert verbose.mode_of("/etc/shadow") is LogMode.QUIET

    @pytest.mark.asyncio
    @pytest.mark.parametrize("build", _FAMILIES)
    async def test_send_keeps_quiet_text_off_the_console(self, build):
        # `send`'s dry-run arm consulted the folded mode only for NEVER and
        # then logged at NORMAL, so a QUIET send was UPGRADED by the dry run.
        with active_context(dry_run=True), _two_sided_sinks() as (console, verbose):
            host = build()
            await host.send("hunter2\n", log=LogMode.QUIET)
            await host.send("uname -a\n")  # positive control

        assert any("uname -a" in ln for ln in console.lines()), (
            f"the control never reached the console sink; saw {console.lines()}"
        )
        assert not any("hunter2" in ln for ln in console.lines()), (
            f"a dry run put QUIET send text on the console: {console.lines()}"
        )
        assert verbose.mode_of("hunter2") is LogMode.QUIET

    @pytest.mark.asyncio
    @pytest.mark.parametrize("build", _FAMILIES)
    async def test_send_at_never_reaches_no_sink_at_all(self, build):
        # NEVER is the hop password's mode (`login_proxy`), and it is redacted
        # from EVERY sink, not just the console -- `_log_command` returns
        # before it logs. This half already worked; it is asserted so that
        # threading the mode through cannot quietly downgrade it to QUIET.
        with active_context(dry_run=True), _two_sided_sinks() as (console, verbose):
            host = build()
            await host.send("hunter2\n", log=LogMode.NEVER)
            await host.send("uname -a\n")  # positive control

        assert any("uname -a" in ln for ln in console.lines()), (
            f"the control never reached the console sink; saw {console.lines()}"
        )
        assert not any("hunter2" in ln for ln in verbose.lines()), (
            f"a NEVER send reached a sink under dry run: {verbose.lines()}"
        )


_SECRET = "hunter2-TOPSECRET-passphrase"
_BODY = f"password={_SECRET}\n"
_ENCODED = base64.b64encode(_BODY.encode()).decode()


class TestDryRunWriteFileAnnouncesWithoutThePayload:
    """SUPPRESS THE PAYLOAD, NEVER THE ANNOUNCEMENT -- both halves, on disk.

    ``write_file`` base64-encodes the body into the command string, so the
    dry-run echo of that command WAS the file's contents on the console. The
    first half of the property is that the body is gone. The second half is
    that something still comes out: honouring ``QUIET`` alone left
    ``otto host <id> write-file --dry-run`` emitting nothing whatsoever -- no
    path, no action, no line -- and a dry run with no product is useless
    rather than safe. So the two are asserted TOGETHER: neither an echoed
    payload nor a silent dry run can pass.

    Driven through the real three-sink fan-out (``init_cli_logging`` +
    ``attach_console_suppress_filter`` + ``create_output_dir``) rather than a
    stand-in, because the claim is about ``console.log`` and ``verbose.log``
    as files on disk.
    """

    @pytest.fixture(autouse=True)
    def _clean_management(self):
        management.reset()
        yield
        management.reset()

    def _sinks(self, tmp_path):
        management.init_cli_logging(xdir=tmp_path, log_level="INFO", keep_days=7)
        management.attach_console_suppress_filter(HostFilter())
        return management.create_output_dir("run")

    @pytest.mark.asyncio
    async def test_announcement_reaches_the_console_and_the_body_reaches_no_sink(self, tmp_path):
        out = self._sinks(tmp_path)

        with active_context(dry_run=True):
            host = _unix()
            result = await host.write_file("/etc/otto-creds", _BODY)
            await host.exec("uname -a")  # positive control
        management._state.listener.stop()  # flush the queue

        console_log = (out / "console.log").read_text()
        verbose_log = (out / "verbose.log").read_text()

        assert "uname -a" in console_log, (
            "the control never reached console.log, so an empty console.log "
            "would prove nothing about either half of this property"
        )
        # THE ANNOUNCEMENT: the operator learns the action and the destination.
        assert "[DRY RUN] WRITE: 38 bytes -> /etc/otto-creds" in console_log, (
            f"a dry run of a write said nothing an operator can act on: {console_log}"
        )
        # THE PAYLOAD: absent from EVERY sink -- it is never built under a dry
        # run, so there is nothing to suppress and nothing to leak later.
        assert _ENCODED not in console_log, (
            "a dry run wrote the file's body to console.log; base64 is an encoding, not a redaction"
        )
        assert _ENCODED not in verbose_log
        assert _SECRET not in console_log
        assert _SECRET not in verbose_log
        # `msg` is what a programmatic caller reads and what the CLI renders on
        # a non-ok result; it named nothing useful before.
        assert result.msg == "[DRY RUN] WRITE: 38 bytes -> /etc/otto-creds"
        assert result.status is Status.NotRun

    @pytest.mark.asyncio
    async def test_a_declined_write_is_not_ok_so_a_caller_cannot_read_it_as_written(self, tmp_path):
        """Chris's ruling: `Skipped.is_ok` is True, so the old return LIED.

        The library layer, one level above the enum the Task 2 sweep keyed on:
        no `Status.Skipped` token appears in a caller's
        ``if (await host.write_file(...)).is_ok:``, so the sweep walked past
        it while it went on telling every library caller a dry run had written
        the file. `NotRun` is the same decline every other host verb now
        returns, and the whole point of it is `is_ok is False`.

        Deliberately a plain `Result` and NOT a `NotRunResult`: a write
        measures nothing, so `value` is honestly `None`, and a raising `value`
        would only move the explosion onto the renderer, which reads it in
        order to print.
        """
        self._sinks(tmp_path)

        with active_context(dry_run=True):
            host = _unix()
            declined = await host.write_file("/etc/otto-creds", _BODY)

        assert declined.status is Status.NotRun
        assert declined.is_ok is False, (
            "a dry run told a library caller the file was written; "
            "`if (await host.write_file(...)).is_ok:` acts on this"
        )
        assert bool(declined) is False
        assert not isinstance(declined, NotRunResult)
        assert declined.value is None  # a plain read, so the renderer can print

        # POSITIVE CONTROL, same verb, same seam: a real write is ok and its
        # `is_ok` still means what it says. Without this, a `write_file` that
        # had stopped working entirely would satisfy every assertion above.
        wrote = tmp_path / "real.txt"
        real = await _local().write_file(wrote, _BODY)
        assert real.is_ok is True, real.msg
        assert wrote.read_text() == _BODY

    @pytest.mark.asyncio
    async def test_the_renderer_still_prints_the_banner_for_the_declined_write(self, capsys):
        """The interaction that once deleted `write-file --dry-run`'s output.

        `render_leaf_value` prints `msg` only on the NOT-OK path, so moving
        `write_file` off an ok status is exactly the change that could have
        restored the silent dry run the announcement half exists to prevent.
        Asserted against the real renderer, not by inspecting the status.
        """
        from otto.cli.invoke import render_leaf_value

        with active_context(dry_run=True):
            declined = await _unix().write_file("/etc/otto-creds", _BODY)
        capsys.readouterr()  # drop the host logger's own line

        render_leaf_value(declined)
        printed = capsys.readouterr().out
        assert "[DRY RUN] WRITE: 38 bytes -> /etc/otto-creds" in printed, (
            f"the renderer swallowed the dry run's only product: {printed!r}"
        )
        assert _ENCODED not in printed  # ...and still never the body
        assert _SECRET not in printed

        # POSITIVE CONTROL, same renderer: a genuinely ok write prints no
        # decline, so the branch above cannot be announcing unconditionally.
        render_leaf_value(Result(Status.Success))
        assert "[DRY RUN]" not in capsys.readouterr().out

    @pytest.mark.asyncio
    async def test_append_is_named_distinctly_from_overwrite(self, tmp_path):
        # Append vs overwrite is the flag that changes the OUTCOME, so an
        # announcement that blurs the two lets an operator confirm a plan that
        # is not the one they typed.
        out = self._sinks(tmp_path)

        with active_context(dry_run=True):
            host = _unix()
            await host.write_file("/etc/otto-creds", _BODY, append=True)
            await host.exec("uname -a")  # positive control
        management._state.listener.stop()

        console_log = (out / "console.log").read_text()
        assert "uname -a" in console_log, (
            f"the control never reached console.log; saw {console_log}"
        )
        assert "[DRY RUN] APPEND: 38 bytes -> /etc/otto-creds" in console_log, (
            f"an appending dry run reported itself as something else: {console_log}"
        )
        assert "WRITE:" not in console_log


def _zephyr_with_fs(log: LogMode = LogMode.QUIET):
    """A real ZephyrHost with a RAM filesystem — nothing is dialled here."""
    from otto.host.embedded_filesystem import build_filesystem
    from otto.host.embedded_host import ZephyrHost

    return ZephyrHost(
        ip="192.0.2.1", element="zephyr37_fat", log=log, filesystem=build_filesystem("fat-ram")
    )


class TestADryRunInventsNoListingAndNoFileFact:
    """`ls`, `exists` and `glob` decline instead of answering from nothing.

    THE LIBRARY-LAYER TWIN of the primitive, and the one Task 2's sweep could
    not see: it keyed on the `Status.Skipped` TOKEN, and these two carry no
    enum name at all. `ls` returned `[]` on the non-ok branch and `exists`
    returned `False`, so under a dry run a caller was handed an empty
    directory and a missing file — silently, with no raise, and entirely past
    the CLI seam, because these are LIBRARY calls that no `--dry-run` stop
    intercepts. (`exists` fabricated `True` before Task 2; Task 2 flipped the
    direction of the lie without removing it.) `glob` arrived later and never
    had the defect — it is held to the same contract here so it cannot acquire
    one, since its empty list is the same fabricated absence in a shape a
    caller is even likelier to act on ("this host has no logs").

    Neither return type can carry a status, so of the contract's three
    behaviours — fabricate it, decline loudly, or never run the logic — only
    the middle one is available: the caller's `if` is the logic, and otto does
    not own it. The raise is `CommandNotRunError`, the same error the
    primitive's own `.value` raises, so there is one vocabulary for "this was
    not measured" rather than two.

    Every test pairs the decline with a POSITIVE CONTROL against the same verb
    on the same object, because "it raised" is also what a verb that has
    stopped working entirely does.
    """

    @pytest.mark.asyncio
    async def test_unix_ls_declines_rather_than_reporting_an_empty_directory(self, tmp_path):
        with active_context(dry_run=True), pytest.raises(CommandNotRunError) as exc:
            await _unix().ls("/opt/otto")

        # Both halves of "which non-measurement is this": the call the CALLER
        # made (not just the shell line otto would have sent) and the host.
        assert "ls('/opt/otto')" in str(exc.value)
        assert "box" in str(exc.value)

        # POSITIVE CONTROL, same verb, real host, no dry run: a listing is
        # still an ordinary list of names.
        (tmp_path / "a.txt").write_text("x")
        assert await _local().ls(tmp_path) == ["a.txt"]

    @pytest.mark.asyncio
    async def test_unix_exists_declines_rather_than_reporting_an_absent_file(self, tmp_path):
        with active_context(dry_run=True), pytest.raises(CommandNotRunError) as exc:
            await _unix().exists("/etc/otto-creds")
        assert "exists('/etc/otto-creds')" in str(exc.value)

        # POSITIVE CONTROL, same verb, real host: BOTH answers still work, so
        # this cannot be passing against an `exists` that raises always.
        present = tmp_path / "here"
        present.write_text("x")
        assert await _local().exists(present) is True
        assert await _local().exists(tmp_path / "gone") is False

    @pytest.mark.asyncio
    async def test_unix_glob_declines_rather_than_reporting_no_matches(self, tmp_path):
        # The third of the same family, and the one born declining: `glob`
        # returning `[]` under a dry run is a fabricated "nothing matched",
        # which a caller reads as "this host has no logs to collect".
        with active_context(dry_run=True), pytest.raises(CommandNotRunError) as exc:
            await _unix().glob("/var/log/messages*")
        assert "glob('/var/log/messages*')" in str(exc.value)

        # POSITIVE CONTROL, same verb, real host: BOTH answers still work, so
        # this cannot be passing against a `glob` that raises always.
        (tmp_path / "messages").write_text("x")
        assert await _local().glob(str(tmp_path / "messages*")) == [str(tmp_path / "messages")]
        assert await _local().glob(str(tmp_path / "no-such-*")) == []

    @pytest.mark.asyncio
    async def test_embedded_ls_and_exists_decline_on_the_device_shell_too(self):
        # The same defect, second copy: EmbeddedHost overrides both verbs
        # against the `fs ls` former and had its own `return []` / `False`.
        host = _zephyr_with_fs()
        with active_context(dry_run=True):
            with pytest.raises(CommandNotRunError):
                await host.ls("/RAM:")
            with pytest.raises(CommandNotRunError):
                await host.exists("/RAM:/a.bin")
            assert host._connections._telnet_conn is None  # and nothing was dialled

        # POSITIVE CONTROL, same verbs, same host object: with the device
        # answering, both still parse the former's output.
        host._run_one = AsyncMock(  # type: ignore[method-assign]
            return_value=CommandResult(
                status=Status.Success, value="a.bin\nb.bin", command="fs ls /RAM:", retcode=0
            )
        )
        assert await host.ls("/RAM:") == ["a.bin", "b.bin"]
        assert await host.exists("/RAM:/a.bin") is True

    @pytest.mark.asyncio
    async def test_a_real_failure_still_answers_and_is_not_swept_up_by_the_decline(self, tmp_path):
        """The half that must NOT move: a measured failure is a measurement.

        A real `ls` of a path that does not exist genuinely looked, and its
        long-standing empty-list answer is not the fabrication — narrowing the
        guard to `Status.NotRun` rather than to "not ok" is what keeps the two
        apart, and this is the test that fails if a later edit widens it.
        """
        assert await _local().ls(tmp_path / "no-such-dir") == []
        assert await _local().exists(tmp_path / "no-such-dir") is False

        host = _zephyr_with_fs()
        host._run_one = AsyncMock(  # type: ignore[method-assign]
            return_value=CommandResult(
                status=Status.Error, value="", command="fs ls /RAM:/nope", retcode=1
            )
        )
        assert await host.ls("/RAM:/nope") == []
        assert await host.exists("/RAM:/nope") is False


class TestNotRunIsNotData:
    """The synthetic result must be unusable as a measurement.

    Status.NotRun is NOT ok (unlike Skipped, whose is_ok=True is load-bearing
    for genuine skips), and reading .value raises at the exact line that
    mistook a non-measurement for data. Positive controls pin the halves that
    must NOT change: Skipped stays ok, and a real CommandResult's value stays
    a plain read.
    """

    def test_not_run_is_not_ok_and_skipped_still_is(self):
        assert Status.NotRun.is_ok is False
        assert Status.Skipped.is_ok is True  # positive control: genuine skips
        assert (
            NotRunResult(status=Status.NotRun, command="uptime", retcode=-1, host_name="box").is_ok
            is False
        )

    def test_reading_value_raises_naming_command_and_host(self):
        nr = NotRunResult(status=Status.NotRun, command="uptime", retcode=-1, host_name="box")
        with pytest.raises(CommandNotRunError) as exc:
            _ = nr.value
        assert "uptime" in str(exc.value)
        assert "box" in str(exc.value)
        # positive control: an ordinary CommandResult's value is a plain read
        assert CommandResult(Status.Success, value="ok", command="true", retcode=0).value == "ok"

    def test_a_smuggled_value_argument_is_dropped_not_returned(self):
        # The frozen-dataclass __init__ assigns `value` through
        # object.__setattr__ to bypass the frozen __setattr__ -- but that
        # still routes through the class's data descriptor, so the payload
        # lands in the property's setter and is dropped rather than reaching
        # the instance dict. Either way the read must raise: this test is the
        # tripwire if someone re-decorates NotRunResult with @dataclass (which
        # would make `value` a plain field again) or adds slots=True.
        nr = NotRunResult(
            status=Status.NotRun, value="smuggled", command="c", retcode=-1, host_name="h"
        )
        with pytest.raises(CommandNotRunError):
            _ = nr.value
        assert "smuggled" not in repr(nr), "the payload survived in the instance"


class TestNotRunIsStillAnObjectYouCanHandle:
    """Handling the object must never raise; reading the measurement must.

    The generated dataclass dunders all reach through ``self.value``, so
    before this class existed ``repr(nr)``, ``nr == nr``, ``hash(nr)`` and
    ``result in seen`` every one of them detonated -- reporting the mistake at
    a log line, an assertion or a set insertion that had parsed nothing. That
    is the same defect as the fabricated payload, pointed the other way.

    EVERY test here asserts BOTH halves against the same instance: the dunder
    is safe AND ``.value`` still raises. A test that only proved ``repr``
    works would pass just as happily against a NotRunResult that poisons
    nothing at all.
    """

    def _nr(self, **overrides) -> NotRunResult:
        fields = {
            "status": Status.NotRun,
            "command": "uptime",
            "retcode": -1,
            "host_name": "box",
        }
        return NotRunResult(**{**fields, **overrides})

    def test_repr_str_and_fstrings_name_the_absence_instead_of_raising(self):
        nr = self._nr()
        for rendering in (repr(nr), str(nr), f"{nr}"):
            # `<not run>` rather than a dropped field: a log line missing
            # `value` reads as an empty result, which is the fiction.
            assert "<not run>" in rendering
            assert "uptime" in rendering
            assert "box" in rendering
        with pytest.raises(CommandNotRunError):
            _ = nr.value
        # positive control, same seam: a real result still renders its payload
        assert "'ok'" in repr(CommandResult(Status.Success, value="ok", command="true", retcode=0))

    def test_equality_is_safe_and_never_equates_a_decline_with_an_answer(self):
        nr = self._nr()
        assert nr == self._nr()
        assert nr != self._nr(host_name="other")  # same command, different host
        assert nr != self._nr(command="whoami")
        # THE ONE THAT MATTERS: `assert result == expected` must not accept a
        # decline in place of an answer, however well the other fields match.
        answered = CommandResult(
            Status.NotRun, value="14:02:11 up 3 days", command="uptime", retcode=-1
        )
        assert nr != answered
        assert answered != nr  # and the reflected comparison agrees
        with pytest.raises(CommandNotRunError):
            _ = nr.value

    def test_hash_is_safe_agrees_with_equality_and_survives_a_set(self):
        nr, same, other = self._nr(), self._nr(), self._nr(host_name="other")
        assert hash(nr) == hash(same)
        assert len({nr, same}) == 1  # equal declines collapse
        assert len({nr, other}) == 2  # different hosts do not
        assert nr in {same, other}  # `result in seen` must not detonate
        with pytest.raises(CommandNotRunError):
            _ = nr.value

    def test_copying_survives_the_blanket_frozen_override(self):
        # copy and deepcopy rebuild through __reduce_ex__ and write the
        # instance dict directly, so the __setattr__ override below must not
        # break them -- and host_name must survive, since it is NOT a
        # dataclass field and so is invisible to the generated machinery.
        nr = self._nr()
        for clone in (copy.copy(nr), copy.deepcopy(nr)):
            assert clone == nr
            assert clone.host_name == "box"
            with pytest.raises(CommandNotRunError):
                _ = clone.value

    def test_the_instance_rejects_every_assignment_declared_or_stray(self):
        nr = self._nr()
        with pytest.raises(FrozenInstanceError):
            # Silently succeeded before: every later error named the WRONG host.
            nr.host_name = "other"
        with pytest.raises(FrozenInstanceError):
            # Also stuck silently -- `host_name` is not a dataclass field, so
            # the inherited frozen __setattr__ waved both of these through.
            nr.typo = 1  # ty: ignore[unresolved-attribute]
        with pytest.raises(FrozenInstanceError):
            nr.command = "whoami"
        with pytest.raises(FrozenInstanceError):
            del nr.command
        assert nr.host_name == "box"
        # positive control, same seam: the rest of the family already did this
        with pytest.raises(FrozenInstanceError):
            CommandResult(Status.Success, value="ok", command="true", retcode=0).command = "x"

    def test_serialising_or_copying_the_measurement_still_raises(self):
        """``asdict`` and ``replace`` READ the payload, so they stay poisoned.

        Ruled deliberately rather than fixed. ``asdict`` exists to hand the
        fields to a serialiser, and a sentinel there would put a data-shaped
        fabrication into exactly the channel -- JSON on a wire -- that this
        class exists to keep clean. ``dataclasses.replace`` reads every init
        field in order to copy it, so it cannot avoid the payload either; and
        it drops ``host_name``, which is not a dataclass field, so a version
        that "worked" would hand back a decline naming no host at all.
        """
        nr = self._nr()
        with pytest.raises(CommandNotRunError):
            dataclasses.asdict(nr)
        with pytest.raises(CommandNotRunError):
            dataclasses.replace(nr, retcode=2)
        # positive control, same seam: both are ordinary on a real result
        cr = CommandResult(Status.Success, value="ok", command="true", retcode=0)
        assert dataclasses.asdict(cr)["value"] == "ok"
        assert dataclasses.replace(cr, command="false").command == "false"


def _async(value):
    """A coroutine that immediately returns *value* (for monkeypatched awaitables)."""

    async def _run():
        return value

    return _run()


class TestThePrimitiveDeclinesInsteadOfAnswering:
    """The host primitive must hand back a DECLINE, not a synthetic success.

    Task 1 built ``NotRunResult`` and proved it in isolation; this class is
    where it becomes the thing a real host actually returns. Every test builds
    a real :class:`~otto.host.unix_host.UnixHost` /
    :class:`~otto.host.local_host.LocalHost` inside
    ``active_context(dry_run=True)`` -- the hostile condition is INJECTED, so
    the object under assertion is the product's own and not a double's.

    Each test carries a POSITIVE CONTROL against the same seam, because
    "``.value`` raised" and "``is_ok`` was False" are both satisfied by a
    product that has stopped working entirely.
    """

    @pytest.mark.asyncio
    async def test_exec_declines_and_reading_the_answer_raises(self):
        # THE CORE OF THE CONTRACT. Before this, `exec` under a dry run
        # answered `Status.Skipped` / `retcode 0` / `is_ok True` with the
        # literal string "[DRY RUN] Command not executed" in `.value` -- a
        # poison pill shaped exactly like data, which every status branch
        # passed and every parser chewed.
        with active_context(dry_run=True):
            host = _unix()
            result = await host.exec("uptime")

            assert result.status is Status.NotRun
            assert result.is_ok is False
            assert bool(result) is False
            assert result.command == "uptime"
            assert result.retcode == -1  # the codebase's "never ran" sentinel
            with pytest.raises(CommandNotRunError) as exc:
                _ = result.value
            # The error names BOTH halves of "which non-measurement is this",
            # so the traceback line is actionable without the surrounding code.
            assert "uptime" in str(exc.value)
            assert host.name in str(exc.value)
            assert host._connections._ssh_conn is None  # and nothing was dialled

        # POSITIVE CONTROL, same seam, outside the context: a real command's
        # value is an ordinary read. Without this, a product whose `exec`
        # raised unconditionally would pass every assertion above.
        real = await LocalHost().exec("echo otto-lives")
        assert real.is_ok is True
        assert "otto-lives" in real.value

    @pytest.mark.asyncio
    async def test_the_decline_still_announces_at_the_callers_folded_mode(self):
        # SUPPRESS THE PAYLOAD, NEVER THE ANNOUNCEMENT -- both halves in one
        # test, because hardening the returned object is exactly the change
        # that could take the `[DRY RUN]` echo out with it. The LogMode fold at
        # the emit seam shipped separately and must survive untouched.
        with active_context(dry_run=True), _two_sided_sinks() as (console, verbose):
            host = _local()
            quiet = await host.exec("cat /etc/shadow", log=LogMode.QUIET)
            loud = await host.exec("uname -a")  # positive control

        assert any("[DRY RUN] uname -a" in ln for ln in console.lines()), (
            f"the decline stopped announcing itself on the console: {console.lines()}"
        )
        assert not any("/etc/shadow" in ln for ln in console.lines()), (
            f"a dry run put a QUIET command on the console: {console.lines()}"
        )
        # The QUIET line is folded off the console but still reaches verbose.log
        # -- suppressed, not deleted.
        assert verbose.mode_of("/etc/shadow") is LogMode.QUIET
        assert verbose.mode_of("uname -a") is LogMode.NORMAL
        # ...and both are still declines, so the announcement is not being
        # bought back by softening the result.
        assert quiet.status is Status.NotRun
        assert loud.status is Status.NotRun

    @pytest.mark.asyncio
    async def test_a_declined_command_folds_into_a_not_ok_aggregate(self):
        # `Results.collect` computes the aggregate as "the first non-ok entry's
        # status", and Skipped is ok -- so under the old synthetic a dry-run
        # `run` folded to Success and `bool(results)` was True.
        with active_context(dry_run=True):
            results = await _unix().run(["cmd1", "cmd2", "cmd3"])

        assert len(results) == 3
        assert [e.status for e in results] == [Status.NotRun] * 3
        assert results.status is Status.NotRun
        assert results.is_ok is False
        assert bool(results) is False
        assert results.first_failure is not None

        # POSITIVE CONTROL, same fold: a GENUINE skip still counts as passing.
        # This is the half that must not move -- Skipped.is_ok is load-bearing
        # for test steps and trailing transfer entries everywhere.
        genuine = Results.collect(
            [CommandResult(Status.Skipped, value="", command="c", retcode=-1)]
        )
        assert genuine.status is Status.Success  # no non-ok entry to inherit from
        assert genuine.is_ok is True
        assert bool(genuine) is True

    @pytest.mark.asyncio
    async def test_a_declined_transfer_folds_not_ok_but_keeps_its_readable_plan(self):
        # ONLY the status hardens here. A transfer's per-file `value` is the
        # DESTINATION PATH, computed locally from the caller's own arguments --
        # it is the preview, not a device measurement, so poisoning it would
        # delete the product rather than the fabrication.
        src = Path("/tmp/app.bin")
        with active_context(dry_run=True):
            result = await _unix().put([src], Path("/opt/bin"), mode="755")

        assert result.status is Status.NotRun
        assert result.is_ok is False
        assert result.value[src].status is Status.NotRun
        assert result.value[src].value == Path("/opt/bin/app.bin")  # still readable
        assert "[DRY RUN] PUT" in result.msg
        assert "(mode 0o755)" in result.msg  # the announcement survives intact

        # POSITIVE CONTROL, the same fold: a genuine "not attempted" entry is
        # still ok, so a trailing run of real skips never fails an aggregate.
        assert (
            aggregate_transfer(
                {src: Result(Status.Skipped, msg="not attempted (earlier failure)")}
            ).is_ok
            is True
        )

    @pytest.mark.asyncio
    async def test_the_docker_seam_declines_with_the_callers_own_command(self):
        # `_exec_via_parent` relabelled the parent's result with
        # `dataclasses.replace(result, command=cmd)` -- the one `replace` on a
        # Result-family object in src/otto. `replace` READS `value`, so the
        # moment the parent started declining, a line whose only job is
        # relabelling raised. It now declines before the wrapper is even built,
        # which also keeps `_docker_exec` -> `_ensure_running` (a daemon round
        # trip) off a dry run's path.
        with active_context(dry_run=True):
            host = _docker()
            # The seam itself: public `exec`/`run` short-circuit ABOVE this in
            # `BaseHost`, so calling it directly is the only way to inject the
            # condition it has to survive as `SessionManager`'s exec_factory.
            direct = await host._exec_via_parent("uptime", 5.0)
            assert direct.status is Status.NotRun
            assert direct.command == "uptime", "the decline named the docker-exec wrapper"
            with pytest.raises(CommandNotRunError) as exc:
                _ = direct.value
            assert "docker exec" not in str(exc.value)

            # POSITIVE CONTROL, same host: the public path is unchanged and
            # also names the caller's command rather than the wrapper.
            public = await host.exec("uptime")
            assert public.status is Status.NotRun
            assert public.command == "uptime"

    @pytest.mark.asyncio
    async def test_a_dry_run_does_not_answer_which_modules_are_loaded(self):
        # `_loaded_modules` short-circuited to `Result(Status.Skipped,
        # value=[])`: an EMPTY MODULE LIST fabricated for a machine that was
        # never contacted, so "is module X loaded?" was answered "no". Same
        # defect class as the userland probes and `link list` reporting every
        # link clean.
        with active_context(dry_run=True):
            host = _unix()
            result = await host.lsmod()

            assert result.status is Status.NotRun
            assert result.is_ok is False
            with pytest.raises(CommandNotRunError) as exc:
                _ = result.value
            assert "/proc/modules" in str(exc.value)
            assert host.name in str(exc.value)
            assert host._connections._ssh_conn is None

        # POSITIVE CONTROL, same seam: a genuine read failure still reports a
        # module list of `[]` under `Status.Error` -- the documented shape for
        # "asked and could not tell", which is a different answer from "never
        # asked" and must stay distinguishable from it.
        failed = CommandResult(Status.Failed, value="cat: no such file", command="c", retcode=1)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(UnixHost, "exec", lambda self, *a, **kw: _async(failed))
            read_failure = await _unix()._loaded_modules()
        assert read_failure.status is Status.Error
        assert read_failure.value == []

    @pytest.mark.asyncio
    async def test_unload_never_consults_a_fabricated_module_list(self):
        # `unload`'s residency check (`resolved not in (await
        # self._loaded_modules()).value`) short-circuits to Success when the
        # module is absent, so a fabricated empty list made a dry-run `unload`
        # report success without ever naming an `rmmod`. The `not is_dry_run()`
        # guard in front of it is what keeps the logic from running at all --
        # the third of the three honest behaviours.
        #
        # The raise IS the contract here (spec section 4: the PARSE class
        # breaks loudly); WHICH command it names is the discriminator. Drop
        # the guard and the message names `cat /proc/modules` instead.
        with active_context(dry_run=True), pytest.raises(CommandNotRunError) as exc:
            await _unix().unload("dummy_mod")

        assert "rmmod dummy_mod" in str(exc.value)
        assert "/proc/modules" not in str(exc.value), (
            "a dry-run unload consulted the module list it must never ask for"
        )


class _RecordingPowerController(PowerController):
    """A power controller that records what it was asked to do, and does none of it.

    Recording rather than raising inside the double: the claim under test is
    that NOTHING on this object is reached, and a double that raised would
    report the failure from inside the product's call stack rather than at the
    assertion that owns the claim.

    :meth:`cycle` is overridden even though the base class implements it as
    ``off`` then ``on``, because ``reboot(hard=True)`` calls ``cycle`` and that
    is the seam the guard has to stop; ``on``/``off`` still record, so a
    ``reboot`` that reached the controller by some other route is caught too.
    :meth:`status` records as well: for ``power()``'s toggle it is the DEVICE
    READ, and "the preview did not ask" is half of that arm's property.
    """

    type_name = "recording"

    def __init__(self, state: "PowerState | None" = None) -> None:
        self.calls: list[str] = []
        self.state = state

    async def on(self, host) -> Result:
        self.calls.append("on")
        return Result(Status.Success)

    async def off(self, host) -> Result:
        self.calls.append("off")
        return Result(Status.Success)

    async def cycle(self, host) -> Result:
        self.calls.append("cycle")
        return Result(Status.Success)

    async def status(self, host) -> "PowerState | None":
        self.calls.append("status")
        return self.state


class TestADryRunNeitherRebootsNorPowersOffAHost:
    """THE SAFETY ONE: `otto -n <host> reboot --hard` power-cycled a real machine.

    `BaseHost.reboot` had no `is_dry_run()` branch at all, and the hard arm
    does not go through the command path: it calls `PowerController.cycle`
    directly, which drives a PDU or a hypervisor. None of the dry-run plumbing
    Tasks 1-4 built is anywhere near that line, so `--dry-run` -- the flag
    someone types when they are NOT sure -- cycled the box.

    THREE MORE HARMS, all library-reachable and none of them fixed by
    `Status.NotRun` alone (the spec predicted this task "falls out nearly for
    free" and it does not):

    * `_soft_reboot` DISCARDS the `run("reboot")` decline and returns
      `Status.Success`, so `reboot()` saw `is_ok` and fired
      `rebuild_connections()` -- tearing down every cached transport for a host
      that is still up. A dry run with a real side effect;
    * `shutdown` branched only on `Status.Failed`, so the folded `NotRun` fell
      straight through to `Result(Status.Success)`: a FABRICATED POWER-OFF, and
      `shutdown` has no wait behind it for anything downstream to notice;
    * the `wait=True` phases really dial (`is_reachable` ->
      `verify_connection`), so a dry run also burned the whole down/up deadline
      against a live host before failing.

    The fix is the third of the contract's three honest behaviours: one arm at
    the top of each verb, so nothing below it runs. EVERY test here carries its
    POSITIVE CONTROL in the same test, against the SAME host object and the
    same spies -- without it, each "did not happen" assertion is satisfied just
    as well by a `reboot` that does nothing at all.

    Nothing here dials, powers or reboots anything: the controller is a
    recorder, the soft-reboot and probe seams are spies, and the hostile
    condition is INJECTED with `active_context(dry_run=True)` around a real
    `UnixHost`.
    """

    @pytest.mark.asyncio
    async def test_a_hard_reboot_never_reaches_the_power_controller(self):
        spy = _RecordingPowerController()
        host = _unix()
        host.power_control = spy

        with active_context(dry_run=True), _two_sided_sinks() as (console, _verbose):
            declined = await host.reboot(hard=True)
            await host.exec("uname -a")  # positive control for the sink itself

        assert spy.calls == [], f"a dry run power-cycled a real machine: {spy.calls}"
        assert declined.status is Status.NotRun
        assert declined.is_ok is False, (
            "a dry run told a library caller the host was rebooted; "
            "`if (await host.reboot(hard=True)).is_ok:` acts on this"
        )
        assert declined.value is None  # a plain read, so the renderer can print

        # SUPPRESS THE PAYLOAD, NEVER THE ANNOUNCEMENT: the action, the target
        # and the controller that WOULD have been driven all reach the console.
        assert any("uname -a" in ln for ln in console.lines()), (
            f"the control never reached the console sink; saw {console.lines()}"
        )
        assert any("[DRY RUN] REBOOT (hard)" in ln for ln in console.lines()), (
            f"a dry-run reboot said nothing an operator can act on: {console.lines()}"
        )
        assert "[DRY RUN] REBOOT (hard)" in declined.msg
        assert host.name in declined.msg
        assert "_RecordingPowerController" in declined.msg, (
            f"the preview did not name the power controller it would drive: {declined.msg}"
        )
        assert "transports kept" in declined.msg

        # POSITIVE CONTROL, same host, same controller, same call, no dry run:
        # the cycle IS issued. Without this half every assertion above is
        # satisfied by a `reboot` that does nothing at all.
        real = await host.reboot(hard=True)
        assert spy.calls == ["cycle"], "the reboot did not power-cycle even WITHOUT --dry-run"
        assert real.is_ok is True

    @pytest.mark.asyncio
    async def test_a_soft_reboot_issues_nothing_and_keeps_the_cached_transports(self):
        # The `rebuild_connections()` half, which is the one `Status.NotRun`
        # alone was supposed to fix and did not: `_soft_reboot` swallows the
        # decline and answers Success, so the `result.is_ok` gate above the
        # rebuild stayed open under a dry run.
        issued: list[str] = []
        rebuilds: list[str] = []
        host = _unix()

        async def recording_soft_reboot() -> Result:
            issued.append("reboot")
            return Result(Status.Success)

        host._soft_reboot = recording_soft_reboot
        host.rebuild_connections = lambda: rebuilds.append("rebuild")

        with active_context(dry_run=True):
            declined = await host.reboot()

        assert issued == [], f"a dry run issued the reboot command: {issued}"
        assert rebuilds == [], (
            "a dry run tore down every cached transport for a host that is still up"
        )
        assert declined.status is Status.NotRun
        assert "[DRY RUN] REBOOT (soft)" in declined.msg
        assert "transports kept" in declined.msg

        # POSITIVE CONTROL, same host, same spies, no dry run: both happen, in
        # the order the real path needs them.
        real = await host.reboot()
        assert issued == ["reboot"], "the reboot was not issued even WITHOUT --dry-run"
        assert rebuilds == ["rebuild"], "the transports were not rebuilt even WITHOUT --dry-run"
        assert real.is_ok is True

    @pytest.mark.asyncio
    async def test_a_waiting_reboot_dials_nothing_and_reaches_no_wait_phase(self):
        """The deadline is not burned because the WAIT NEVER STARTS.

        Asserted as a CALL COUNT on the probe seam, never as elapsed time: a
        timing assertion would be a wall-clock-bound test, which this repo
        bans, and it would also pass against a dry run that dialled once
        quickly. Zero dials is the property; the minutes of wall clock a real
        host would have cost are a consequence of it.
        """
        dials: list[str] = []
        # Scripted rather than computed from `len(dials)`: the control half
        # runs on the same spy, and a stateful answer would make the down phase
        # read the dry-run half's (absent) probes. Exhausting it raises, so an
        # unexpected third probe is loud rather than silently polled.
        answers = iter([False, True])  # gone, then back
        host = _unix()

        async def recording_soft_reboot() -> Result:
            return Result(Status.Success)

        async def recording_is_reachable(timeout: float = 10.0) -> bool:
            dials.append("dial")
            return next(answers)

        host._soft_reboot = recording_soft_reboot
        host.rebuild_connections = lambda: None
        host.is_reachable = recording_is_reachable
        host._confirm_recovered = AsyncMock(return_value=True)

        with active_context(dry_run=True):
            declined = await host.reboot(wait=True, timeout=5.0, down_timeout=4.0, poll_interval=0)

        assert dials == [], f"a dry run dialled a live host {len(dials)} time(s)"
        assert declined.status is Status.NotRun
        # The bounds are the preview's product: an operator confirms the plan
        # they typed, not a generic "would wait".
        assert "4.0s" in declined.msg, declined.msg  # the down bound
        assert "5.0s" in declined.msg, declined.msg  # the total bound

        # POSITIVE CONTROL, same host, same spies, no dry run: both wait phases
        # really probe. Without it, "no dial" passes against a broken wait.
        real = await host.reboot(wait=True, timeout=5.0, down_timeout=4.0, poll_interval=0)
        assert len(dials) == 2, f"the wait phases did not probe even WITHOUT --dry-run: {dials}"
        assert real.is_ok is True

    @pytest.mark.asyncio
    async def test_a_hard_reboot_with_no_controller_fails_the_same_way_it_always_did(self):
        """A dry run checks the caller's OWN input; it does not preview the impossible.

        Same precedent as `_dry_run_transfer` parsing a typo'd `--mode 789`:
        `reboot(hard=True)` against a host with no power backend cannot work,
        costs nothing to detect, and reaches no device to detect it. Announcing
        a tidy "would power-cycle" plan would fabricate FEASIBILITY, which is
        the same class of lie as fabricating a measurement.

        PASSES ON BOTH SIDES OF THE CHANGE, stated rather than left to be
        discovered: the unguarded product raised here too, from the same line.
        It is a NO-CHANGE guard, and what it catches is the obvious way to
        write the new arm -- skipping `_require_power_control()` and reporting
        a controller-less plan, which reddens exactly this test.
        """
        host = _unix()  # no power_control configured
        assert host.power_control is None

        with active_context(dry_run=True), pytest.raises(ValueError, match="no power_control"):
            await host.reboot(hard=True)

        # POSITIVE CONTROL, same host, same call, no dry run: the identical
        # error, from the identical line -- the dry run did not invent one.
        with pytest.raises(ValueError, match="no power_control"):
            await host.reboot(hard=True)

    @pytest.mark.asyncio
    async def test_power_on_and_off_command_the_controller_only_without_a_dry_run(self):
        """`power()` is `reboot(hard=True)`'s twin and had the identical hole.

        Ten lines apart in the same file, both reaching the controller
        directly. The CLI stops above this verb at the seam (`power` keeps the
        safe default), so the live surface is the LIBRARY layer -- an embedder
        or an in-process caller under `dry_run=True` doing
        `await host.power('off')`, which powered the machine off.
        """
        spy = _RecordingPowerController()
        host = _unix()
        host.power_control = spy

        with active_context(dry_run=True):
            off = await host.power("off")
            on = await host.power("on")

        assert spy.calls == [], f"a dry run powered a real machine: {spy.calls}"
        for declined, named in ((off, "off"), (on, "on")):
            assert declined.status is Status.NotRun
            assert declined.is_ok is False
            assert f"[DRY RUN] POWER ({named})" in declined.msg
            assert host.name in declined.msg
            assert "_RecordingPowerController" in declined.msg
            # `value` is the COMMANDED PowerState on the real path, and a
            # preview commanded nothing -- handing one back would be the same
            # fabrication `_with_state` avoids on a failed command.
            assert declined.value is None

        # POSITIVE CONTROL, same host, same controller, same calls, no dry run:
        # both really command the controller, and the commanded state comes
        # back as the payload.
        real_off = await host.power("off")
        real_on = await host.power("on")
        assert spy.calls == ["off", "on"], "power did not reach the controller even WITHOUT -n"
        assert real_off.value is PowerState.OFF
        assert real_on.value is PowerState.ON

    @pytest.mark.asyncio
    async def test_a_toggle_preview_refuses_to_name_a_direction_it_did_not_measure(self):
        """The arm that could most easily fabricate: a toggle has to ASK first.

        Deciding on/off means `controller.status()`, which is a device read. A
        preview that named a direction anyway would be a measurement dressed as
        a plan -- and the operator would confirm a direction otto never
        established. So the announcement says the state was not read.
        """
        spy = _RecordingPowerController()
        host = _unix()
        host.power_control = spy

        with active_context(dry_run=True):
            declined = await host.power()

        assert spy.calls == [], f"a dry run toggled a real machine: {spy.calls}"
        assert declined.status is Status.NotRun
        assert "[DRY RUN] POWER (toggle)" in declined.msg
        assert "current state not read" in declined.msg, declined.msg
        # THE FABRICATION THIS FORBIDS: a resolved direction. `off`/`on` appear
        # only inside "the opposite"-shaped prose, never as the commanded verb.
        assert "would command power off" not in declined.msg
        assert "would command power on" not in declined.msg

        # POSITIVE CONTROL, same host, same controller, no dry run: the real
        # toggle DOES read the state and command the opposite of it.
        spy.state = PowerState.ON
        real = await host.power()
        assert spy.calls == ["status", "off"], f"the real toggle did not read and act: {spy.calls}"
        assert real.value is PowerState.OFF

    @pytest.mark.asyncio
    async def test_power_still_refuses_a_host_with_no_controller_and_a_bad_state(self):
        """Both local checks stay live under a dry run, from the same lines.

        PASSES ON BOTH SIDES for the no-controller half (the unguarded product
        raised too) and is a genuine discriminator for the bad-state half,
        which a preview arm placed above the validation would swallow.
        """
        with active_context(dry_run=True):
            with pytest.raises(ValueError, match="no power_control"):
                await _unix().power("off")

            host = _unix()
            host.power_control = _RecordingPowerController()
            with pytest.raises(ValueError, match="invalid power state"):
                await host.power("of")  # the typo an operator actually makes

        # POSITIVE CONTROL, same calls, no dry run: identical errors, and the
        # valid spelling still works on the same object.
        with pytest.raises(ValueError, match="no power_control"):
            await _unix().power("off")
        with pytest.raises(ValueError, match="invalid power state"):
            await host.power("of")
        assert (await host.power("off")).is_ok is True

    @pytest.mark.asyncio
    async def test_a_dry_run_shutdown_issues_nothing_and_claims_no_power_off(self):
        issued: list[str] = []
        host = _unix()
        # No device at EITHER half: the userland resolution the real path runs
        # first answers nothing, so nothing settles and `shutdown_command`
        # degrades to the spelling otto has always sent.
        host.exec = AsyncMock(side_effect=OSError("no device in this test"))

        async def recording_run(cmd, **kwargs):
            issued.append(cmd)
            return Results.collect([])

        host.run = recording_run

        with active_context(dry_run=True):
            declined = await host.shutdown()

        assert issued == [], f"a dry run powered a host off: {issued}"
        assert declined.status is Status.NotRun
        assert declined.is_ok is False, (
            "a dry run reported a power-off that never happened; the folded "
            "NotRun used to fall straight through to Result(Status.Success)"
        )
        assert "[DRY RUN] SHUTDOWN" in declined.msg
        assert host.name in declined.msg
        assert "stays up" in declined.msg

        # POSITIVE CONTROL, same host, same spies, no dry run: the command IS
        # issued and the verb reports the power-off it really asked for.
        real = await host.shutdown()
        assert issued == ["shutdown -h now"], "nothing was issued even WITHOUT --dry-run"
        assert real.is_ok is True

    @pytest.mark.asyncio
    async def test_the_renderer_prints_the_reboot_preview_and_does_not_exit_nonzero(self, capsys):
        """The decline must render as an ANNOUNCEMENT, not as a failure.

        `reboot` keeps the CLI's seam default, so `otto -n host <id> reboot`
        stops above the body and this renderer does not see the decline TODAY.
        The property is still load-bearing and still belongs here, for two
        reasons: `render_leaf_value` is the shared renderer every caller that
        does hold the result reaches (an embedder printing a verb's return, a
        suite reporting a step), and it is the one thing that would have to be
        true before the verb could ever be opted in. A `Status.NotRun` result
        is announced and never parsed, so this must print the plan and must
        not raise `typer.Exit` -- an arm whose product exits 255 or explodes on
        `.value` at the print statement is not a preview.
        """
        from otto.cli.invoke import render_leaf_value

        host = _unix()
        host.power_control = _RecordingPowerController()
        # `wait=False`: the bounds belong to the test above, and a `wait=True`
        # here would dial a real address for 60s before the guard exists --
        # the RED half of this pair has to be cheap too.
        with active_context(dry_run=True):
            declined = await host.reboot(hard=True)
        capsys.readouterr()  # drop the host logger's own line

        render_leaf_value(declined)  # must not raise typer.Exit
        printed = capsys.readouterr().out
        assert "REBOOT (hard)" in printed, (
            f"the renderer swallowed the dry run's only product: {printed!r}"
        )

        # POSITIVE CONTROL, same renderer: an ok result prints no decline, so
        # the branch above cannot be announcing unconditionally.
        render_leaf_value(Result(Status.Success))
        assert "[DRY RUN]" not in capsys.readouterr().out


class _NoopShell(AppShell):
    """Minimal :class:`AppShell` for the ``app_shell`` guard — never launched."""

    launch = "true"
    prompt = re.compile(r"\$ \Z")


class _RecordingShellSession(ShellSession):
    """A transport that records every call a :class:`HostSession` makes on it.

    Recording rather than raising, for the reason ``_RecordingPowerController``
    gives above: the claim is that nothing reaches this object, and a double
    that raised would report the failure from inside the product's call stack
    instead of at the assertion that owns it. It also has to answer NORMALLY,
    because every test below drives it a second time without the dry run.

    ``run_cmd``/``send``/``expect`` are overridden at the top rather than driven
    through the real framing engine: they are exactly the three seams
    :class:`HostSession` delegates to, so these lists measure "the guard
    returned before touching the transport". ``_write`` and
    ``_read_until_pattern`` stay unimplemented and LOUD, so a
    :class:`HostSession` method that reaches past that trio cannot pass here
    silently.
    """

    def __init__(self, output: str = "real output", match: str = "real match") -> None:
        super().__init__()
        self.ran: list[str] = []
        self.sent: list[str] = []
        self.awaited: list[str] = []
        self.closed = False
        self._output = output
        self._match = match

    async def _open(self) -> None: ...

    async def _write(self, data: str) -> None:
        raise AssertionError(f"a HostSession reached the raw transport write: {data!r}")

    async def _read_until_pattern(self, pattern: re.Pattern[str]) -> str:
        raise AssertionError(f"a HostSession reached the raw transport read: {pattern!r}")

    async def _ensure_initialized(self) -> None:
        self._initialized = True
        self._alive = True

    async def close(self) -> None:
        self.closed = True
        self._alive = False
        self._initialized = False

    async def run_cmd(
        self,
        cmd: str,
        expects=None,
        timeout: float = 30.0,
        on_output=None,
        redact: bool = False,
        write_progress=None,
    ) -> CommandResult:
        self.ran.append(cmd)
        return CommandResult(Status.Success, value=self._output, command=cmd, retcode=0)

    async def send(self, text: str) -> None:
        self.sent.append(text)

    async def expect(self, pattern, timeout: float = 30.0) -> str:
        self.awaited.append(str(getattr(pattern, "pattern", pattern)))
        return self._match


async def _live_session(host) -> "tuple[HostSession, _RecordingShellSession]":
    """A REAL :class:`HostSession` over a recording transport, opened outside any dry run.

    Built by the product's own ``SessionManager.open_session`` and wired to the
    real host's ``_log_command``/``_log_output``, so the object under test is
    otto's rather than a hand-assembled lookalike, and its ``[DRY RUN]`` lines
    land on the logger the console sink watches.

    Opened OUTSIDE the dry-run context deliberately: this is the handle a suite
    holds from setup, which ``host.open_session``'s refusal never sees. It is
    the whole reason :class:`HostSession` needs guards of its own and not just
    a closed front door.
    """
    transport = _RecordingShellSession()
    mgr = SessionManager(
        name=host.name,
        log_command=host._log_command,
        log_output=host._log_output,
        session_factory=lambda: transport,
        creds=[],
        host_id=host.id,
    )
    return await mgr.open_session("worker"), transport


class TestADryRunOpensNoSessionOnAnyHost:
    """THE OTHER SAFETY ONE: `open_session` announced a dry run and then opened it.

    All three families logged `[DRY RUN] open_session(...)` and then called the
    session manager anyway -- the announcement-without-the-suppression
    inversion, where the log line is precisely what makes the hole look
    handled. On `DockerContainerHost` the next statement was
    `_ensure_running()`, so a dry run could reach `_auto_up` -> `compose_up`
    and START A REAL CONTAINER, contradicting `_ensure_running`'s own docstring
    (which asserted that every dry-run path short-circuits above it).

    What comes back instead is a `DeclinedSession` -- `NotRunResult`'s
    philosophy one level up. The caller gets a handle that is safe to hold,
    pass and close, and every method that would touch a device declines AT THE
    POINT OF USE while announcing what it would have done. One mental model
    covers both layers: `host.exec` under `-n` answers `NotRun`, and so does
    `session.run`.

    Every test carries its POSITIVE CONTROL against the same host and the same
    spy. Nothing contacts a device: the session manager and the
    container-start path are spies, and the hostile condition is INJECTED with
    `active_context(dry_run=True)` around real host objects.
    """

    @pytest.mark.parametrize("build", _FAMILIES)
    @pytest.mark.asyncio
    async def test_no_family_opens_a_session_and_the_handle_says_so(self, build):
        host = build()
        opened: list[str] = []

        async def recording_open(name: str):
            opened.append(name)
            return "a live session"

        host._session_mgr.open_session = recording_open

        with active_context(dry_run=True), _two_sided_sinks() as (console, _verbose):
            session = await host.open_session("monitor")
            await host.exec("uname -a")  # positive control for the sink itself

        assert opened == [], f"a dry run opened a real session: {opened}"
        assert isinstance(session, DeclinedSession)
        assert session.alive is False, "a handle that never opened reported itself live"

        # SUPPRESS THE PAYLOAD, NEVER THE ANNOUNCEMENT -- and the announcement
        # must say the session was NOT opened, which is the half the old line
        # got wrong.
        assert any("uname -a" in ln for ln in console.lines()), (
            f"the control never reached the console sink; saw {console.lines()}"
        )
        banner = [ln for ln in console.lines() if "open_session('monitor')" in ln]
        assert banner, f"a dry-run open_session said nothing at all: {console.lines()}"
        assert "no session opened" in banner[0], (
            f"the banner still reads as though a session was opened: {banner[0]!r}"
        )

        # POSITIVE CONTROL, same host, same spy, no dry run: the manager IS
        # asked. Without it, "nothing was opened" is satisfied just as well by
        # an `open_session` that never opens anything.
        assert await host.open_session("monitor") == "a live session"
        assert opened == ["monitor"], "the session was not opened even WITHOUT --dry-run"

    @pytest.mark.asyncio
    async def test_a_dry_run_never_starts_a_container(self):
        """The one that starts a machine: `open_session` -> `_ensure_running` -> `compose_up`.

        Driven with an EMPTY `container_id`, which is the placeholder every
        declared-but-unresolved container host carries after a lab load, and
        the only shape in which `_ensure_running` does anything at all. With
        the id already cached (the other tests' shape) this path is unreachable
        and the test would pass against the unguarded product.
        """
        parent = _unix()
        probes: list[str] = []
        ups: list[str] = []
        opened: list[str] = []

        async def recording_exec(cmd: str, timeout: float = 30.0, log=LogMode.NORMAL):
            probes.append(cmd)
            return CommandResult(Status.Success, value="", command=cmd, retcode=0)

        parent.exec = recording_exec
        host = DockerContainerHost(
            parent=parent,
            container_id="",  # the declared-but-unresolved placeholder
            project="repo1",
            service="api",
            compose_project="otto-repo1-vagrant",
        )

        async def recording_auto_up() -> str:
            ups.append("compose_up")
            return "started123"

        async def recording_open(name: str):
            opened.append(name)
            return "a live session"

        logins: list[str | None] = []

        async def recording_login(as_user: str | None = None) -> None:
            logins.append(as_user)

        host._auto_up = recording_auto_up
        host._session_mgr.open_session = recording_open
        # `_login` is SPIED, not left real: its second act is
        # `parent._connections.ssh()`, so a mutation that drops `login`'s arm
        # would make this test dial a real address for the duration of the
        # connect timeout. A guard is proven by a spy, never by letting the
        # unguarded product try it. `_login`'s FIRST act is `_ensure_running()`
        # — which is why the arm has to exist — and the `open_session` half
        # below is what pins that call chain against the real resolver.
        host._login = recording_login

        with active_context(dry_run=True):
            assert isinstance(await host.open_session("monitor"), DeclinedSession)
            await host.login()

        assert ups == [], f"A DRY RUN STARTED A CONTAINER: {ups}"
        assert probes == [], f"a dry run questioned the docker daemon: {probes}"
        assert opened == []
        assert logins == [], "a dry run reached `_login`, whose first act is `_ensure_running()`"
        assert host.container_id == "", "a dry run cached a container id it never resolved"

        # POSITIVE CONTROL, same host, same spies, no dry run: the placeholder
        # really does resolve through the probe and then start the stack.
        await host.open_session("monitor")
        assert probes, "the daemon was never asked even WITHOUT --dry-run"
        assert ups == ["compose_up"], "the stack was never started even WITHOUT --dry-run"
        assert opened == ["monitor"]

    @pytest.mark.asyncio
    async def test_app_shell_previews_its_launch_and_then_stops_at_the_prompt_wait(self):
        """`BaseHost.app_shell` is the library surface that made this reachable.

        It is a CALLER, and deliberately grows no guard of its own: guarding
        the session LAYER is what makes every caller safe by construction, and
        this test is the evidence that it does. What comes out is the preview's
        honest shape -- the launch line is ANNOUNCED, and the wait for the
        app's first prompt is where it stops, because that answer only exists
        on the device.

        Also pins the teardown half: `app_shell`'s `finally` closes the
        session, and closing a `DeclinedSession` must neither tear anything
        down nor raise over the exception that got there first.
        """
        host = _local()
        opened: list[str] = []

        async def recording_open(name: str):
            opened.append(name)
            return "a live session"

        host._session_mgr.open_session = recording_open

        with (
            active_context(dry_run=True),
            _two_sided_sinks() as (console, _verbose),
            pytest.raises(CommandNotRunError) as exc,
        ):
            async with host.app_shell(_NoopShell):
                pass  # pragma: no cover -- entering must raise

        assert opened == [], f"a dry-run app_shell opened a real session: {opened}"
        assert any("[DRY RUN] send('true\\n')" in ln for ln in console.lines()), (
            f"the preview lost the launch line it DID announce: {console.lines()}"
        )
        assert "preview is a PREFIX" in str(exc.value), (
            f"the stop read as a breakage rather than the end of the preview: {exc.value}"
        )

        # POSITIVE CONTROL, same host, same spy: the seam does open sessions.
        assert await host.open_session("probe") == "a live session"
        assert opened == ["probe"]

    @pytest.mark.asyncio
    async def test_login_announces_and_bridges_no_terminal(self):
        """`login` returns None, so it takes `send`'s shape: announce and return.

        Not `expect`'s shape -- returning invents no device fact here, because
        there is no fact to invent. Its product is a side effect, and declining
        to perform it is the whole point.
        """
        host = _unix()
        logins: list[str | None] = []

        async def recording_login(as_user: str | None = None) -> None:
            logins.append(as_user)

        host._login = recording_login

        with active_context(dry_run=True), _two_sided_sinks() as (console, _verbose):
            assert await host.login() is None
            await host.exec("uname -a")  # positive control for the sink itself

        assert logins == [], f"a dry run opened an interactive shell: {logins}"
        assert any("uname -a" in ln for ln in console.lines())
        assert any("[DRY RUN] login(" in ln for ln in console.lines()), (
            f"a dry-run login said nothing an operator can act on: {console.lines()}"
        )

        # POSITIVE CONTROL, same host, same spy, no dry run.
        await host.login("mysql")
        assert logins == ["mysql"], "login bridged no terminal even WITHOUT --dry-run"


class TestTheDeclinedHandleIsUsableAndNeverMistakenForALiveOne:
    """What `open_session` hands back under a dry run, driven the way a script drives it.

    The object is a `HostSession` subclass, so it satisfies every caller's type
    and every `isinstance` check, and the surface cannot silently drift: a
    method nobody overrode is INHERITED, and what it reaches is a transport
    that declines rather than an `AttributeError` at a line that made no
    mistake.

    Each test builds the handle through the real `LocalHost.open_session`
    inside `active_context(dry_run=True)` -- the hostile condition is injected
    around a real host -- and the session manager is spied so the positive
    control can show the same call opening a real session without the flag.
    """

    @staticmethod
    def _host_with_spied_manager():
        host = _local()
        opened: list[str] = []

        async def recording_open(name: str):
            opened.append(name)
            return "a live session"

        host._session_mgr.open_session = recording_open
        return host, opened

    @pytest.mark.asyncio
    async def test_run_declines_every_command_and_announces_each(self):
        host, opened = self._host_with_spied_manager()

        with active_context(dry_run=True), _two_sided_sinks() as (console, _verbose):
            session = await host.open_session("mon")
            results = await session.run(["ip addr", "ip route"])

        assert opened == []
        assert [r.command for r in results] == ["ip addr", "ip route"]
        assert all(r.status is Status.NotRun for r in results)
        assert results.status is Status.NotRun, "the aggregate read as ok"
        for r in results:
            with pytest.raises(CommandNotRunError):
                _ = r.value
        assert any("[DRY RUN] ip addr" in ln for ln in console.lines()), (
            f"the declined session produced no preview: {console.lines()}"
        )
        assert any("[DRY RUN] ip route" in ln for ln in console.lines())

        # POSITIVE CONTROL, same host, same spy: without the flag the same call
        # opens a real session, so "nothing was opened" is not vacuous.
        assert await host.open_session("mon") == "a live session"
        assert opened == ["mon"]

    @pytest.mark.asyncio
    async def test_the_preview_is_a_prefix_and_the_announcements_survive_the_stop(self):
        """send -> send -> expect: the sends are announced, the wait ends the preview.

        The consequence the design accepts on purpose. Step N+1 of a session
        script is chosen from step N's output, so a preview can only run until
        the first answer it would have to invent. Both halves are asserted: the
        lines already emitted are still on the sink AFTER the raise propagates,
        and the error explains that the preview ran out of things it can know
        rather than that something broke.
        """
        host, _opened = self._host_with_spied_manager()

        with active_context(dry_run=True), _two_sided_sinks() as (console, _verbose):
            session = await host.open_session("cfg")
            await session.send("configure terminal\n")
            await session.send("interface eth0\n")
            with pytest.raises(CommandNotRunError) as exc:
                await session.expect(r"\(config-if\)#")

        assert any("configure terminal" in ln for ln in console.lines()), (
            f"the announcements were lost when the raise propagated: {console.lines()}"
        )
        assert any("interface eth0" in ln for ln in console.lines())
        assert any("[DRY RUN] expect(" in ln for ln in console.lines()), (
            "the wait that ended the preview was not announced"
        )
        assert "preview is a PREFIX" in str(exc.value)
        assert "nothing is wrong with the script" in str(exc.value).lower()

    @pytest.mark.asyncio
    async def test_it_works_as_a_context_manager_and_closes_nothing(self):
        """`async with await host.open_session(...)` must not break under a dry run.

        And the exit must not fabricate a teardown: `close` on a handle that
        never opened has nothing to tear down, and this repo has a history of
        teardown paths doing real work on objects that were never live.

        The teardown claim is asserted as a CALL COUNT on the transport's own
        `close`, not as "it did not raise". Scored that way it was GREEN
        against a `close` that delegated to `HostSession.close` -- the delegate
        happens to be harmless here, so "nothing broke" proved nothing. The
        property is that the transport is never asked.
        """
        host, _opened = self._host_with_spied_manager()
        closes: list[str] = []

        with active_context(dry_run=True):
            session = await host.open_session("mon")
            assert isinstance(session, DeclinedSession)

            async def recording_close() -> None:
                closes.append("close")

            session._session.close = recording_close

            async with session as entered:
                assert entered is session
                assert (await entered.run("uptime")).only.status is Status.NotRun

            # __aexit__ ran and asked the transport for nothing.
            assert closes == [], f"a handle that never opened tore something down: {closes}"
            assert session.alive is False
            await session.close()  # idempotent, still silent
            assert closes == []

        # POSITIVE CONTROL, same spy shape on a REAL session: `close` DOES
        # reach the transport, so the empty list above is a property of the
        # decline and not of the way this test is wired.
        live, transport = await _live_session(_unix())
        await live.close()
        assert transport.closed is True, "a real session did not close its transport either"

    @pytest.mark.asyncio
    async def test_elevating_the_declined_handle_claims_nothing_it_did_not_do(self):
        host, _opened = self._host_with_spied_manager()

        with active_context(dry_run=True):
            session = await host.open_session("mon")
            before = session.current_user
            with pytest.raises(CommandNotRunError):
                await session.switch_user("root")
            with pytest.raises(CommandNotRunError):
                async with session.as_user("root"):
                    pass  # pragma: no cover -- entering must raise
            assert session.current_user == before, "a decline stamped a user it never became"

    @pytest.mark.asyncio
    async def test_the_declined_handle_answers_current_user_from_configuration(self):
        """`current_user` is NOT a device fact, so declining it would invent a gap.

        Every writer of a named session's user is configuration or already
        refused: `ShellSession.__init__` (`""`), `SessionManager._seed_user`
        (`_login_user()`), `SessionManager._apply_login_proxy` (`login_target`),
        and `switch_user`/`as_user`, whose arms refuse above the stamp. The
        product already answers this question from config before any session
        exists (`SessionManager.current_user` falls back to `_login_user()`).

        The half that decides it is the SECOND assertion: a LIVE handle held
        from setup reports its stamped user inside a dry-run block, because the
        property has no arm and should not have one. A declined handle that
        raised -- or that answered something else -- would make two handles
        disagree about the same host under the same dry run.
        """
        host = _unix()  # creds=[Cred(login="user", ...)], so `_login_user()` is "user"
        # The live handle comes from the host's OWN manager -- same config, same
        # `_login_user()` -- with only the transport swapped, so the comparison
        # below is between two handles on one host and not two fixtures.
        host._session_mgr._session_factory = _RecordingShellSession
        live = await host._session_mgr.open_session("live")

        with active_context(dry_run=True):
            declined = await host.open_session("mon")

            assert declined.current_user == "user", (
                f"the declined handle invented a user: {declined.current_user!r}"
            )
            assert declined.current_user == live.current_user, (
                "two handles on one host disagreed about the user under one dry run: "
                f"declined={declined.current_user!r} live={live.current_user!r}"
            )

        # A loginless family answers "" -- the same thing its real sessions do,
        # which is the point: the decline tracks the real path rather than
        # picking its own answer.
        with active_context(dry_run=True):
            assert (await _local().open_session("mon")).current_user == ""

    @pytest.mark.asyncio
    async def test_an_unoverridden_method_still_declines_at_the_point_of_use(self):
        """FULL SURFACE FIDELITY, asserted at the seam that provides it.

        `DeclinedSession` overrides every `HostSession` method that reaches a
        device TODAY. The claim under test is about tomorrow's: anything that
        reaches through to the transport must decline, not raise
        `AttributeError` somewhere unrelated. Driven directly against the
        transport, because by construction there is no such inherited method
        yet -- the point is that when one appears, it lands here.
        """
        host, _opened = self._host_with_spied_manager()

        with active_context(dry_run=True):
            session = await host.open_session("mon")

        transport = session._session
        with pytest.raises(CommandNotRunError):
            await transport._open()
        with pytest.raises(CommandNotRunError):
            await transport._write("echo hi\n")
        with pytest.raises(CommandNotRunError):
            await transport._read_until_pattern(re.compile("READY"))
        # ...except close, which must stay quiet so a `finally` cannot explode.
        assert await transport.close() is None


class TestADryRunDrivesNoSessionItAlreadyHolds:
    """`HostSession` had no `is_dry_run()` anywhere in the class.

    It reaches its `ShellSession` directly -- `run` -> `run_cmd`, `send` ->
    `send`, `expect` -> `expect` -- bypassing the `BaseHost` seam entirely, so
    none of the primitive hardening from Tasks 1-2 saw it. Closing
    `open_session` is not enough on its own: a suite that opens a session in
    setup and enters a dry-run block later still holds a LIVE handle, and that
    is exactly the shape these tests build (`_live_session` opens OUTSIDE the
    context, on purpose).

    Every test drives the same session twice -- once inside the context, once
    outside -- against the same recording transport, so no "did not reach the
    transport" claim can be satisfied by a transport that is never reached at
    all.
    """

    @pytest.mark.asyncio
    async def test_run_declines_without_writing_the_command(self):
        host = _unix()
        session, transport = await _live_session(host)

        with active_context(dry_run=True), _two_sided_sinks() as (console, _verbose):
            declined = (await session.run("rm -rf /var/tmp/x")).only

        assert transport.ran == [], f"a dry run ran a command on a live session: {transport.ran}"
        assert declined.status is Status.NotRun
        assert declined.is_ok is False
        assert declined.retcode == -1
        assert declined.command == "rm -rf /var/tmp/x"
        with pytest.raises(CommandNotRunError):
            _ = declined.value
        assert any("[DRY RUN] rm -rf /var/tmp/x" in ln for ln in console.lines()), (
            f"the session's dry run said nothing: {console.lines()}"
        )

        # POSITIVE CONTROL, same session, same transport, no dry run.
        real = (await session.run("uname -a")).only
        assert transport.ran == ["uname -a"], "the session ran nothing even WITHOUT --dry-run"
        assert real.value == "real output"

    @pytest.mark.asyncio
    async def test_a_sequence_declines_every_command_and_folds_not_ok(self):
        host = _unix()
        session, transport = await _live_session(host)

        with active_context(dry_run=True):
            results = await session.run(["one", "two", "three"])

        assert transport.ran == []
        assert len(results) == 3
        assert [r.command for r in results] == ["one", "two", "three"]
        assert all(r.status is Status.NotRun for r in results)
        assert results.status is Status.NotRun, "the aggregate read as ok"

        # POSITIVE CONTROL, same session, same transport.
        assert len(await session.run(["one", "two", "three"])) == 3
        assert transport.ran == ["one", "two", "three"]

    @pytest.mark.asyncio
    async def test_a_quiet_session_command_stays_quiet_in_its_dry_run(self):
        """The decline must not put on the console what the real path keeps off it."""
        host = _unix()
        session, _transport = await _live_session(host)

        with active_context(dry_run=True), _two_sided_sinks() as (console, verbose):
            await session.run("cat /etc/shadow", log=LogMode.QUIET)
            await session.run("uname -a")  # positive control at NORMAL

        assert any("uname -a" in ln for ln in console.lines())
        assert not any("/etc/shadow" in ln for ln in console.lines()), (
            "a dry run put a QUIET session command on the console"
        )
        assert any("/etc/shadow" in ln for ln in verbose.lines()), (
            "the QUIET line vanished entirely -- a dry run with no product is a bug"
        )

    @pytest.mark.asyncio
    async def test_send_writes_nothing_to_the_transport(self):
        host = _unix()
        session, transport = await _live_session(host)

        with active_context(dry_run=True), _two_sided_sinks() as (console, _verbose):
            assert await session.send("reboot\n") is None

        assert transport.sent == [], f"a dry run wrote to a live session: {transport.sent}"
        assert any("[DRY RUN] send(" in ln for ln in console.lines()), (
            f"a dry-run send said nothing: {console.lines()}"
        )

        # POSITIVE CONTROL, same session, same transport.
        await session.send("uname -a\n")
        assert transport.sent == ["uname -a\n"]

    @pytest.mark.asyncio
    async def test_expect_declines_rather_than_reporting_no_match(self):
        host = _unix()
        session, transport = await _live_session(host)

        with (
            active_context(dry_run=True),
            _two_sided_sinks() as (console, _verbose),
            pytest.raises(CommandNotRunError) as exc,
        ):
            await session.expect("READY")

        assert transport.awaited == []
        assert "expect('READY')" in str(exc.value)
        assert any("[DRY RUN] expect('READY')" in ln for ln in console.lines())

        # POSITIVE CONTROL, same session, same transport: a real wait answers.
        assert await session.expect("READY") == "real match"
        assert transport.awaited == ["READY"]

    @pytest.mark.asyncio
    async def test_switch_user_claims_no_elevation_it_did_not_perform(self):
        """The fabrication here is `current_user`, not a return value.

        `switch_user` returns None, so `send`'s announce-and-return would look
        safe -- but both elevation methods finish by STAMPING
        `HostSession.current_user`, and a session reporting a user it never
        became steers `as_user`'s undo chain and every "am I root here?" check.
        """
        host = _unix()
        session, transport = await _live_session(host)
        before = session.current_user

        with active_context(dry_run=True), _two_sided_sinks() as (console, _verbose):
            with pytest.raises(CommandNotRunError) as exc:
                await session.switch_user("root")
            with pytest.raises(CommandNotRunError):
                async with session.as_user("root"):
                    pass  # pragma: no cover -- entering must raise

        assert transport.sent == [], f"a dry run typed an su into a live session: {transport.sent}"
        assert session.current_user == before, "a dry run claimed the session had elevated"
        assert "switch_user('root')" in str(exc.value)
        assert any("no elevation attempted" in ln for ln in console.lines()), (
            f"a dry-run elevation said nothing: {console.lines()}"
        )

        # POSITIVE CONTROL, same session, same transport: the su IS typed and
        # the session really does become root.
        await session.switch_user("root")
        assert transport.sent[0] == "su root\n", "no su was typed even WITHOUT --dry-run"
        assert session.current_user == "root"

    @pytest.mark.asyncio
    async def test_host_level_elevation_reports_the_decline_and_not_a_proxy_failure(self):
        """The same wrong-story defect one layer up, in `PosixPrivilege`.

        `host.switch_user` / `host.as_user` drive the DEFAULT session through
        `_HostProxyIO`, which routes to the `host.send`/`host.expect` this
        commit guards -- so they already contacted nothing. But the resulting
        `CommandNotRunError` was swallowed by `run_proxy`'s blanket
        `except Exception` and resurfaced as `LoginProxyError: login proxy
        failed becoming 'root'`: a DIAGNOSIS OF A DEVICE THAT WAS NEVER
        CONTACTED, which is exactly the failure the session-level arms exist to
        prevent.

        Asserted on the error a CALLER ACTUALLY SEES -- the type and its text --
        not merely that something raised. "It raised" was already true before
        the arms, with the wrong story attached.
        """
        host = _unix()
        host._session_mgr._session_factory = _RecordingShellSession
        # Build the default session up front, outside the dry run, so the
        # decline below cannot be an artefact of there being no session yet.
        await host._session_mgr._ensure_session()
        transport = host._session_mgr._session
        before = host._session_mgr.current_user

        with active_context(dry_run=True), _two_sided_sinks() as (console, _verbose):
            with pytest.raises(CommandNotRunError) as exc:
                await host.switch_user("root")
            with pytest.raises(CommandNotRunError):
                async with host.as_user("root"):
                    pass  # pragma: no cover -- entering must raise

        assert "switch_user('root')" in str(exc.value)
        assert host.name in str(exc.value)
        assert "LoginProxyError" not in type(exc.value).__name__
        assert "login proxy failed" not in str(exc.value), (
            f"the decline still reads as a device failure: {exc.value}"
        )
        assert transport.sent == [], f"a dry run typed an su at the host level: {transport.sent}"
        assert host._session_mgr.current_user == before, "a dry run claimed the host had elevated"
        assert any("no elevation attempted" in ln for ln in console.lines()), (
            f"a dry-run host elevation said nothing: {console.lines()}"
        )

        # POSITIVE CONTROL, same host, same transport, no dry run: the su IS
        # typed and the host really does track the new user.
        await host.switch_user("root")
        assert transport.sent[0] == "su root\n", "no su was typed even WITHOUT --dry-run"
        assert host._session_mgr.current_user == "root"

    @pytest.mark.asyncio
    async def test_an_embedded_session_names_its_host_in_a_decline(self):
        """`EmbeddedHost` was the one family building its manager without `host_id`.

        So every session-layer message that names a host said `host ''` --
        a dry run's `CommandNotRunError` here, and `perform_switch`'s proxy
        errors on the real path. The sibling families are the positive control:
        they name their host, and this one now does too, from the same field.
        """
        from otto.host.embedded_host import ZephyrHost

        embedded = ZephyrHost(ip="192.0.2.1", element="zephyr37_fat", log=LogMode.QUIET)
        try:
            embedded._session_mgr._session_factory = _RecordingShellSession
            session = await embedded._session_mgr.open_session("probe")

            with active_context(dry_run=True), pytest.raises(CommandNotRunError) as exc:
                await session.expect("READY")

            assert embedded.id, "the fixture host has no id, so this proves nothing"
            assert embedded.id in str(exc.value), (
                f"an embedded session's decline named no host: {exc.value}"
            )

            # POSITIVE CONTROL, a sibling family through the identical path.
            unix_session, _t = await _live_session(_unix())
            with active_context(dry_run=True), pytest.raises(CommandNotRunError) as unix_exc:
                await unix_session.expect("READY")
            assert _unix().id in str(unix_exc.value)
        finally:
            # A mocked/None `_connections` keeps `__del__` from churning a loop.
            embedded._connections = None


class TestADeclineNeverResurfacesAsAFabricatedFailure:
    """The last type-stripping conversion in the docker package.

    `DockerContainerHost._auto_up` wrapped EVERYTHING `compose_up` raised in
    `RuntimeError("... auto-start failed: ...")`. For a real failure that is
    the right story; for a `CommandNotRunError` it is the wrong one twice
    over -- it asserts an attempt that never happened, and it strips the type
    that is the only thing marking the answer as a decline, so nothing
    upstream can tell the two apart afterwards. The dry-run contract work
    removed twelve of these from `otto.docker.compose`; this is the one that
    was left in the host layer.

    THE SITE IS UNREACHABLE UNDER A DRY RUN TODAY, which is exactly why the
    obvious test is worthless: `_ensure_running` asks `_resolve_container_id`
    first and that refuses outright, so driving the public path proves only
    that the public path is guarded (`TestADryRunOpensNoSessionOnAnyHost`
    already pins that, and would pass against a wide arm that fabricates).
    The hostile condition is therefore INJECTED AT THE SEAM: `_auto_up` is
    called directly with a `compose_up` that declines, which is the shape one
    plausible refactor away (a cached container id, or a second caller that
    skips the probe).

    Nothing contacts a device: `compose_up`, `get_repos` and `get_lab` are all
    spies, and the parent host is never asked anything.
    """

    @staticmethod
    def _host() -> DockerContainerHost:
        return DockerContainerHost(
            parent=_unix(),
            container_id="",  # the declared-but-unresolved placeholder
            project="repo1",
            service="api",
            compose_project="otto-repo1-vagrant",
        )

    @staticmethod
    def _stub_config(monkeypatch) -> None:
        """`_auto_up`'s two late imports, stubbed so nothing reads a real lab."""
        repo = dataclasses.make_dataclass("Repo", ["name"])(name="repo1")
        monkeypatch.setattr("otto.config.get_repos", lambda: [repo])
        # `_auto_up` hands the lab straight to the spied `compose_up` and
        # never looks inside it, so an opaque object is the honest stub.
        monkeypatch.setattr("otto.config.get_lab", object)

    @pytest.mark.asyncio
    async def test_auto_up_hands_a_decline_back_unwrapped(self, monkeypatch):
        """Both arms, one test: a decline passes through, a real failure is wrapped.

        Without the second half the guard is satisfied by an `_auto_up` whose
        wide arm simply re-raises everything -- which would delete the
        actionable "run `otto docker up`" message the real failure path exists
        to produce. The two halves discriminate the named arm from no arm.
        """
        host = self._host()
        self._stub_config(monkeypatch)

        # The decline exactly as `compose_up`'s own dry-run arm builds it.
        declined = CommandNotRunError(
            "compose_up(repo1: otto-repo1-vagrant)",
            host.parent.id,
            "No image was built, no file was staged, no container was started "
            "and no host was registered.",
        )

        async def declining_compose_up(*_args, **_kwargs):
            raise declined

        monkeypatch.setattr("otto.docker.compose.compose_up", declining_compose_up)

        with pytest.raises(CommandNotRunError) as exc:
            await host._auto_up()

        # `is`, not a type check: a bare `raise` re-raises THE SAME object, so
        # a rewrap that happened to preserve the class would still redden.
        assert exc.value is declined, f"the decline was rebuilt on the way out: {exc.value!r}"
        assert "auto-start failed" not in str(exc.value), (
            f"a decline was filed as an attempted start that failed: {exc.value}"
        )

        # POSITIVE CONTROL, same host, same seam: a REAL failure must still
        # come back as the wide arm's actionable RuntimeError. Note
        # `CommandNotRunError` IS a `RuntimeError`, so the type alone proves
        # nothing here -- the message and the NOT-a-decline check do.
        async def failing_compose_up(*_args, **_kwargs):
            raise RuntimeError("no such image: repo1/api:latest")

        monkeypatch.setattr("otto.docker.compose.compose_up", failing_compose_up)

        with pytest.raises(RuntimeError) as real:
            await host._auto_up()

        assert not isinstance(real.value, CommandNotRunError), (
            "a real image failure was reported as a dry-run decline"
        )
        assert "auto-start failed" in str(real.value), (
            f"the wide arm lost its actionable message: {real.value}"
        )
        assert "no such image" in str(real.value), (
            f"the wide arm lost the underlying cause: {real.value}"
        )
        assert "otto docker up" in str(real.value)


class TestTheDryRunLabelFollowsTheInvocationNotTheFile:
    """`verify_connection` printed `[DRY RUN]` while genuinely dialing.

    Its two callers are on opposite sides of the flag. `otto host <id> reboot
    --wait` -- no `-n` anywhere -- polls `is_reachable` against a live box, and
    every poll printed `[DRY RUN] Connection verified` / `FAILED` about a real
    socket. `otto -n --probe ...` reaches the same template, and there the
    connection is the one device contact the flag authorises, so the line must
    say so rather than vanish (SUPPRESS THE PAYLOAD, NEVER THE ANNOUNCEMENT).

    Both halves live in the same test on purpose. A live-only assertion
    (`no [DRY RUN] anywhere`) is satisfied by deleting the label -- and by
    deleting the whole log line -- which is a different defect, not a fix.

    `_probe_connection` is SPIED on every host here: the guard is proven
    without a socket, never by letting a real dial decide the outcome.
    """

    @staticmethod
    def _reachable_host() -> UnixHost:
        host = _unix()

        async def opens() -> None:
            return None

        host._probe_connection = opens
        return host

    @staticmethod
    def _unreachable_host() -> UnixHost:
        host = _unix()

        async def refuses() -> None:
            raise ConnectionError("no route to host")

        host._probe_connection = refuses
        return host

    @pytest.mark.asyncio
    async def test_a_live_verified_connection_is_not_labelled_a_dry_run(self):
        host = self._reachable_host()

        # LIVE HALF: no context at all, which is what `reboot --wait` has.
        with _two_sided_sinks() as (console, _verbose):
            live = await host.verify_connection()

        live_lines = [ln for ln in console.lines() if "Connection verified" in ln]
        assert live_lines, f"the live path went silent about its dial: {console.lines()}"
        assert not any("DRY RUN" in ln for ln in console.lines()), (
            f"a live reboot's reachability poll claimed to be a dry run: {console.lines()}"
        )
        assert live.status is Status.Success

        # DRY-RUN HALF (`--probe`): same host, same spy. The line must still
        # be there, must mark the mode, and must not read as a skipped dial.
        with active_context(dry_run=True), _two_sided_sinks() as (console, _verbose):
            probed = await host.verify_connection()

        probe_lines = [ln for ln in console.lines() if "Connection verified" in ln]
        assert probe_lines, f"a --probe dial announced nothing: {console.lines()}"
        assert "[DRY RUN]" in probe_lines[0], (
            f"a --probe dial did not say which mode opened it: {probe_lines[0]!r}"
        )
        assert "a real connection" in probe_lines[0], (
            "the `[DRY RUN]` token means `otto did not do this` everywhere else, so "
            f"bare it reads as a dial that never happened: {probe_lines[0]!r}"
        )
        assert probed.status is Status.Success

    @pytest.mark.asyncio
    async def test_a_live_failed_connection_is_not_labelled_a_dry_run(self):
        """The failure arm carries its own label and its own chance to lie.

        Separately asserted because it is a separate string: a fix applied to
        the success line alone leaves the noisier half -- the one a `reboot
        --wait` poll prints on every attempt while the box is down -- untouched.
        """
        host = self._unreachable_host()

        with _two_sided_sinks() as (console, _verbose):
            live = await host.verify_connection()

        live_lines = [ln for ln in console.lines() if "Connection FAILED" in ln]
        assert live_lines, f"the live path went silent about its failed dial: {console.lines()}"
        assert not any("DRY RUN" in ln for ln in console.lines()), (
            f"a live reachability failure claimed to be a dry run: {console.lines()}"
        )
        assert "no route to host" in live_lines[0], (
            f"the diagnosis was dropped from the failure line: {live_lines[0]!r}"
        )
        assert live.status is Status.Error

        with active_context(dry_run=True), _two_sided_sinks() as (console, _verbose):
            probed = await host.verify_connection()

        probe_lines = [ln for ln in console.lines() if "Connection FAILED" in ln]
        assert probe_lines, f"a failed --probe dial announced nothing: {console.lines()}"
        assert "[DRY RUN]" in probe_lines[0]
        assert "a real connection" in probe_lines[0], (
            f"a --probe failure reads as a dial that was skipped: {probe_lines[0]!r}"
        )
        assert "no route to host" in probe_lines[0]
        assert probed.status is Status.Error
