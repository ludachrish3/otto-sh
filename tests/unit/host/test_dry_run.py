"""Tests for dry-run mode on hosts (via OttoContext)."""

import base64
import json
import logging
from contextlib import contextmanager
from pathlib import Path

import pytest

from otto.host import userland as userland_module
from otto.host.docker_host import DockerContainerHost
from otto.host.file_ops import refuse_if_base64_is_absent
from otto.host.host import HostFilter, is_dry_run
from otto.host.local_host import LocalHost
from otto.host.login_proxy import Cred
from otto.host.unix_host import UnixHost
from otto.logger import management
from otto.logger.mode import LogMode
from otto.utils import Status
from tests.conftest import active_context


class TestGlobalDryRun:
    def test_global_flag_defaults_to_false(self):
        assert is_dry_run() is False

    # ── LocalHost ─────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_localhost_run_returns_skipped(self):
        with active_context(dry_run=True):
            host = LocalHost()
            result = (await host.run("echo hello")).only

            assert result.status == Status.Skipped
            assert result.retcode == 0
            assert result.command == "echo hello"
            assert "[DRY RUN]" in result.value

    @pytest.mark.asyncio
    async def test_localhost_run_does_not_spawn_subprocess(self):
        with active_context(dry_run=True):
            host = LocalHost()
            result = (await host.run("exit 1")).only

            assert result.status == Status.Skipped
            assert result.retcode == 0

    @pytest.mark.asyncio
    async def test_localhost_run_list_returns_all_skipped(self):
        with active_context(dry_run=True):
            host = LocalHost()
            result = await host.run(["cmd1", "cmd2", "cmd3"])

            assert len(result) == 3
            for r in result:
                assert r.status == Status.Skipped
            assert result.status == Status.Success

    @pytest.mark.asyncio
    async def test_localhost_exec_returns_skipped(self):
        with active_context(dry_run=True):
            host = LocalHost()
            result = await host.exec("echo hello")

            assert result.status == Status.Skipped
            assert result.retcode == 0

    @pytest.mark.asyncio
    async def test_localhost_send_is_noop(self):
        with active_context(dry_run=True):
            host = LocalHost()
            await host.send("some text")

    @pytest.mark.asyncio
    async def test_localhost_expect_returns_empty(self):
        with active_context(dry_run=True):
            host = LocalHost()
            result = await host.expect("some_pattern")
            assert result == ""

    @pytest.mark.asyncio
    async def test_localhost_put_returns_skipped(self):
        with active_context(dry_run=True):
            host = LocalHost()
            files = [Path("/tmp/file1.txt"), Path("/tmp/file2.txt")]
            dest = Path("/tmp/dest")

            result = await host.put(files, dest)

            assert result.status == Status.Skipped
            assert "[DRY RUN]" in result.msg
            assert "PUT" in result.msg

    @pytest.mark.asyncio
    async def test_localhost_put_dry_run_banner_shows_normalized_mode(self):
        with active_context(dry_run=True):
            host = LocalHost()

            result = await host.put([Path("/tmp/app.bin")], Path("/opt/bin"), mode="755")

            assert result.status == Status.Skipped
            # Normalized to octal in the banner, so a dry run shows the value
            # that would actually be applied rather than echoing the input.
            assert "(mode 0o755)" in result.msg

    @pytest.mark.asyncio
    async def test_localhost_put_dry_run_without_mode_has_no_mode_suffix(self):
        with active_context(dry_run=True):
            host = LocalHost()

            result = await host.put([Path("/tmp/app.bin")], Path("/opt/bin"))

            assert result.status == Status.Skipped
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
    async def test_localhost_get_returns_skipped(self):
        with active_context(dry_run=True):
            host = LocalHost()
            files = [Path("/tmp/file.bin")]
            dest = Path("/tmp/dest")

            result = await host.get(files, dest)

            assert result.status == Status.Skipped
            assert "[DRY RUN]" in result.msg
            assert "GET" in result.msg

    # ── UnixHost ──────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_remotehost_run_returns_skipped(self):
        with active_context(dry_run=True):
            host = UnixHost(
                ip="10.0.0.1",
                element="box",
                creds=[Cred(login="user", password="pass")],
                log=LogMode.QUIET,
            )
            result = (await host.run("ls -la")).only

            assert result.status == Status.Skipped
            assert result.retcode == 0
            assert result.command == "ls -la"
            assert "[DRY RUN]" in result.value
            assert host._connections._ssh_conn is None

    @pytest.mark.asyncio
    async def test_remotehost_exec_returns_skipped(self):
        with active_context(dry_run=True):
            host = UnixHost(
                ip="10.0.0.1",
                element="box",
                creds=[Cred(login="user", password="pass")],
                log=LogMode.QUIET,
            )
            result = await host.exec("uname -a")

            assert result.status == Status.Skipped
            assert result.retcode == 0
            assert host._connections._ssh_conn is None

    @pytest.mark.asyncio
    async def test_remotehost_run_list_returns_all_skipped(self):
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
                assert r.status == Status.Skipped
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
    async def test_remotehost_expect_returns_empty(self):
        with active_context(dry_run=True):
            host = UnixHost(
                ip="10.0.0.1",
                element="box",
                creds=[Cred(login="user", password="pass")],
                log=LogMode.QUIET,
            )
            result = await host.expect("some_pattern")

            assert result == ""
            assert host._connections._ssh_conn is None

    @pytest.mark.asyncio
    async def test_remotehost_put_returns_skipped(self):
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

            assert result.status == Status.Skipped
            assert "[DRY RUN]" in result.msg
            assert "PUT" in result.msg
            assert host._connections._ssh_conn is None

    @pytest.mark.asyncio
    async def test_remotehost_get_returns_skipped(self):
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

            assert result.status == Status.Skipped
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

    ``BaseHost._dry_run_result`` answers every command with ``retcode=0``
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
    so the fabricated ``retcode 0`` is the product's own and not a test
    double's.

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

            assert result.status == Status.Skipped
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
        assert result.status is Status.Skipped

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
