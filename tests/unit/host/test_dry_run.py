"""Tests for dry-run mode on hosts (via OttoContext)."""

import json
import logging
from pathlib import Path

import pytest

from otto.host import userland as userland_module
from otto.host.file_ops import refuse_if_base64_is_absent
from otto.host.host import is_dry_run
from otto.host.local_host import LocalHost
from otto.host.login_proxy import Cred
from otto.host.unix_host import UnixHost
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
