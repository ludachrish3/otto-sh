"""Unit tests for the Product lifecycle strategy and orchestration."""

import os
from pathlib import Path

import pytest

from otto.host.element import Element
from otto.host.login_proxy import Cred
from otto.host.product import Product, ShellProduct
from otto.logger.mode import LogMode
from otto.result import CommandResult, NotRunResult, Result, Results
from otto.utils import Status


class _DummyShellProduct(ShellProduct):
    """ShellProduct with the abstract halves stubbed so it can instantiate."""

    async def install(self, host):
        return Result(Status.Success)

    async def uninstall(self, host):
        return Result(Status.Success)

    async def is_installed(self, host):
        return True


def test_shellproduct_name_defaults_to_artifact_basename():
    p = _DummyShellProduct(artifact=Path("/builds/app-1.2.tar.gz"))
    assert p.name == "app-1.2.tar.gz"


def test_shellproduct_explicit_name_wins():
    p = _DummyShellProduct(artifact=Path("/builds/app.tar.gz"), name="myapp")
    assert p.name == "myapp"


def test_product_cannot_be_instantiated_directly():
    with pytest.raises(TypeError):
        Product()  # type: ignore[abstract]


def test_product_is_unowned_until_ingest_stamps_it():
    assert _DummyShellProduct(artifact=Path("/builds/app.bin")).owner is None


@pytest.mark.asyncio
async def test_get_logs_default_is_successful_noop(tmp_path):
    # Kills: an abstract get_logs, which would break every existing Product
    # subclass in every repo at import time — _DummyShellProduct declares only
    # the four abstract verbs that predate the hook.
    #
    # The exact status, not `is_ok`: `Status.Skipped.is_ok` is True, so a
    # default that DECLINED ("no log hook here") instead of succeeding ("there
    # were no logs") passes an is_ok assertion. The two are different claims —
    # only the second is the documented "zero logs is not a failure" — and the
    # host's require_product_logs check reads the haul as ok either way.
    result = await _DummyShellProduct(artifact=Path("/builds/app.bin")).get_logs(
        host=None, dest=tmp_path
    )
    assert result.status is Status.Success
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_shellproduct_stage_delegates_to_host_put():
    from unittest.mock import AsyncMock

    p = _DummyShellProduct(artifact=Path("/builds/app.bin"), stage_dir=Path("/opt"))
    host = AsyncMock()
    host.default_dest_dir = Path()
    put_result = Result(Status.Success, value={})
    host.put.return_value = put_result
    result = await p.stage(host)
    # Returned unchanged — the transfer's own per-file mapping reaches the
    # caller instead of being flattened to a status and a message.
    assert result is put_result
    # The RESOLVED, absolute directory: `put`'s own `_resolve_dest` knows the
    # host default but not the login-home fallback, so handing it the raw
    # value would land the artifact somewhere `install` does not name.
    host.put.assert_awaited_once_with(Path("/builds/app.bin"), Path("/opt"))


async def _resolve(product, host):
    return await product.resolved_stage_dir(host)


def _double(default=Path(), home="/home/tester"):
    """A host double: a transfer default, and a login home it can be asked for."""
    from types import SimpleNamespace

    async def _home():
        return Path(home)

    return SimpleNamespace(id="h1", default_dest_dir=default, login_home=_home)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stage_dir", "default", "expected"),
    [
        (Path("/opt"), Path(), "/opt"),  # declared absolute wins
        (Path("/opt"), Path("/srv/stage"), "/opt"),  # ...over the host default too
        (Path(), Path("/srv/stage"), "/srv/stage"),  # empty → the host default
        (Path(), Path(), "/home/tester"),  # empty + no default → the login home
    ],
)
async def test_resolve_stage_dir_always_answers_an_absolute_path(stage_dir, default, expected):
    """One rule, three sources, and the answer is absolute every time.

    Absolute is the point, not a detail: a relative destination is resolved by
    whoever reads it, and a transfer reads it against the login directory
    while a command reads it against the SHELL's own — the same place only on
    a direct SSH exec channel, and a different one on the pooled-shell route
    (telnet, a proxied login, a `session_setup` hook that cd's).
    """
    p = _DummyShellProduct(artifact=Path("/builds/app.bin"), stage_dir=stage_dir)
    assert await _resolve(p, _double(default)) == Path(expected)


@pytest.mark.asyncio
async def test_the_login_home_is_asked_for_only_when_nothing_else_answers():
    from types import SimpleNamespace

    asked = []

    async def _home():
        asked.append(1)
        return Path("/home/tester")

    host = SimpleNamespace(id="h1", default_dest_dir=Path("/srv/stage"), login_home=_home)
    p = _DummyShellProduct(artifact=Path("/builds/app.bin"))
    assert await _resolve(p, host) == Path("/srv/stage")
    assert asked == []  # a declared default is not worth a round trip


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [Path("sub"), Path("~"), Path("~/stage")])
async def test_a_relative_or_tilde_stage_dir_is_refused_naming_the_product(bad):
    """`~` is not special to any transfer backend, so it is just a relative path."""
    p = _DummyShellProduct(artifact=Path("/builds/app.bin"), name="app", stage_dir=bad)
    with pytest.raises(ValueError, match=r"(?s)product 'app'.*absolute"):
        await _resolve(p, _double())


@pytest.mark.asyncio
async def test_a_relative_default_dest_dir_is_refused_naming_the_host():
    p = _DummyShellProduct(artifact=Path("/builds/app.bin"))
    with pytest.raises(ValueError, match=r"(?s)host h1.*default_dest_dir.*absolute"):
        await _resolve(p, _double(default=Path("stage")))


@pytest.mark.asyncio
async def test_a_host_with_no_login_home_concept_is_refused():
    """An embedded target with no filesystem has nothing this could answer."""
    from types import SimpleNamespace

    p = _DummyShellProduct(artifact=Path("/builds/app.bin"))
    with pytest.raises(ValueError, match=r"(?s)host board1.*no login home"):
        await _resolve(p, SimpleNamespace(id="board1", default_dest_dir=Path()))


def test_the_lab_load_key_stands_in_for_an_undiscovered_home():
    """The collision check runs at ingest, where nothing is connected."""
    from types import SimpleNamespace

    from otto.host.product import LOGIN_HOME

    p = _DummyShellProduct(artifact=Path("/builds/app.bin"))
    assert p.stage_key(SimpleNamespace(id="h1", default_dest_dir=Path())) == LOGIN_HOME
    assert p.stage_key(SimpleNamespace(id="h1", default_dest_dir=Path("/srv"))) == "/srv"
    # ...and keys on the real path once the host has been asked.
    known = SimpleNamespace(id="h1", default_dest_dir=Path(), cached_login_home=Path("/home/t"))
    assert p.stage_key(known) == "/home/t"


def test_shellproduct_stage_dir_defaults_to_empty():
    assert _DummyShellProduct(artifact=Path("/builds/app.bin")).stage_dir == Path()


def test_a_shell_product_stages_an_artifact_but_a_bare_product_does_not():
    # The flag the per-host collision check keys on: only a product that puts
    # a FILE at <stage_dir>/<basename> can collide with another one.
    class _Bare(Product):
        name = "bare"

        async def stage(self, host):
            return Result(Status.Success)

        async def install(self, host):
            return Result(Status.Success)

        async def uninstall(self, host):
            return Result(Status.Success)

        async def is_installed(self, host):
            return True

    assert _DummyShellProduct(artifact=Path("/builds/app.bin")).stages_artifact is True
    assert _Bare().stages_artifact is False


def test_every_host_has_empty_products_by_default():
    from otto.host.embedded_host import ZephyrHost
    from otto.host.local_host import LocalHost
    from otto.host.unix_host import UnixHost

    assert LocalHost().products == []
    assert (
        UnixHost(
            ip="10.0.0.1",
            element=Element("box"),
            creds=[Cred(login="u", password="p")],
            log=LogMode.QUIET,
        ).products
        == []
    )
    assert (
        ZephyrHost(ip="192.0.2.1", element=Element("zephyr37_fat"), log=LogMode.QUIET).products
        == []
    )


def test_products_can_be_injected_at_construction():
    from otto.host.local_host import LocalHost

    p = _DummyShellProduct(artifact=Path("/b/app.bin"))
    host = LocalHost()
    host.products = [p]
    assert host.products == [p]


class _FakeProduct(Product):
    def __init__(self, name, *, installed=False, fail_on=None, result=None):
        self.name = name
        self._installed = installed
        self.fail_on = fail_on
        # When set, returned verbatim by the failing verb — lets a test assert
        # the caller propagates the object rather than rebuilding one from it.
        self.result = result
        self.calls: list[str] = []

    async def stage(self, host):
        self.calls.append("stage")
        return (
            Result(Status.Error, msg="boom") if self.fail_on == "stage" else Result(Status.Success)
        )

    async def install(self, host):
        self.calls.append("install")
        if self.fail_on != "install":
            return Result(Status.Success)
        return self.result if self.result is not None else Result(Status.Error, msg="boom")

    async def uninstall(self, host):
        self.calls.append("uninstall")
        return (
            Result(Status.Error, msg="boom")
            if self.fail_on == "uninstall"
            else Result(Status.Success)
        )

    async def is_installed(self, host):
        return self._installed


def _host_with(products):
    from otto.host.local_host import LocalHost

    h = LocalHost()
    h.products = list(products)
    return h


@pytest.mark.asyncio
async def test_stage_runs_every_product_stage():
    a, b = _FakeProduct("a"), _FakeProduct("b")
    result = await _host_with([a, b]).stage()
    assert result.status is Status.Success
    assert a.calls == ["stage"]
    assert b.calls == ["stage"]


@pytest.mark.asyncio
async def test_stage_empty_is_success_noop():
    result = await _host_with([]).stage()
    assert result.status is Status.Success
    assert result.msg == ""


@pytest.mark.asyncio
async def test_install_stages_then_installs():
    a = _FakeProduct("a")
    result = await _host_with([a]).install()
    assert result.status is Status.Success
    assert a.calls == ["stage", "install"]


@pytest.mark.asyncio
async def test_install_stage_only_skips_install():
    a = _FakeProduct("a")
    result = await _host_with([a]).install(stage_only=True)
    assert result.status is Status.Success
    assert a.calls == ["stage"]


@pytest.mark.asyncio
async def test_install_short_circuits_on_stage_failure():
    a = _FakeProduct("a", fail_on="stage")
    b = _FakeProduct("b")
    result = await _host_with([a, b]).install()
    assert result.status is Status.Error
    assert result.msg == "boom"
    assert b.calls == []  # never reached


@pytest.mark.asyncio
async def test_uninstall_is_best_effort_across_products():
    a = _FakeProduct("a", fail_on="uninstall")
    b = _FakeProduct("b")
    result = await _host_with([a, b]).uninstall()
    assert result.status is Status.Error
    assert result.msg == "boom"
    assert a.calls == ["uninstall"]
    assert b.calls == ["uninstall"]  # both attempted


@pytest.mark.asyncio
async def test_is_installed_true_only_when_all_installed():
    assert (
        await _host_with(
            [_FakeProduct("a", installed=True), _FakeProduct("b", installed=True)]
        ).is_installed()
        is True
    )
    assert (
        await _host_with(
            [_FakeProduct("a", installed=True), _FakeProduct("b", installed=False)]
        ).is_installed()
        is False
    )


@pytest.mark.asyncio
async def test_is_installed_empty_is_false():
    assert await _host_with([]).is_installed() is False


@pytest.mark.asyncio
async def test_is_uninstalled_is_inverse():
    h = _host_with([_FakeProduct("a", installed=False)])
    assert await h.is_uninstalled() is True
    assert await h.is_installed() is False


@pytest.mark.asyncio
async def test_install_under_dry_run_does_not_transfer(tmp_path):
    from tests.conftest import active_context

    class _StageOnlyProduct(ShellProduct):
        async def install(self, host):
            return Result(Status.Success)

        async def uninstall(self, host):
            return Result(Status.Success)

        async def is_installed(self, host):
            return False

    artifact = tmp_path / "app.bin"
    artifact.write_bytes(b"x")
    dest = tmp_path / "dest"
    host = _host_with([_StageOnlyProduct(artifact=artifact, stage_dir=dest.absolute())])
    with active_context(dry_run=True):
        result = await host.install(stage_only=True)
    # NOT ok, deliberately: `stage` returns `put`'s result whole, and a dry
    # run's transfer is a decline. Reporting success here is what let a caller
    # believe an artifact had been placed when nothing left this machine.
    assert result.status is Status.NotRun
    assert result.is_ok is False
    assert "[DRY RUN] PUT" in result.msg  # ...and it says what it did not do
    assert not dest.exists()  # LocalHost.put was a dry-run no-op


@pytest.mark.asyncio
async def test_install_propagates_the_products_result_whole():
    """A product's own CommandResult reaches the caller intact.

    This is the property the Result conversion exists for, and the one a
    ``return Result(result.status, msg=result.msg)`` rebuild would silently
    destroy while keeping every other assertion in this file green: the
    retcode of the command that actually failed becomes the process exit
    code, instead of being flattened to the Status's own value.
    """
    failing = CommandResult(
        Status.Error, value="tar: bad header", command="tar xzf app.tgz", retcode=7
    )
    p = _FakeProduct("a", fail_on="install", result=failing)

    result = await _host_with([p]).install()

    assert result is failing, "the product's result object, not a rebuild"
    assert result.exit_code == 7, "the command's retcode, not Status.Error.value (2)"
    assert result.command == "tar xzf app.tgz"


# ---------------------------------------------------------------------------
# cov_dir / debug_log_globs / instrumented  (spec 2026-09-16 §4)
# ---------------------------------------------------------------------------

from otto.host.product import (
    INSTRUMENTATION_MARKERS,
    cov_dir_of,
    scan_for_instrumentation,
    stamp_cov_dir,
    sudo_gcda_delete,
)


def test_cov_dir_defaults_to_none_until_stamped():
    p = _DummyShellProduct(artifact=Path("/builds/app.bin"), name="app")
    assert p.cov_dir is None
    assert cov_dir_of(p) == "/tmp/app"
    stamp_cov_dir(p)
    assert p.cov_dir == "/tmp/app"


def test_stamp_cov_dir_keeps_an_explicit_value():
    p = _DummyShellProduct(artifact=Path("/builds/app.bin"), name="app", cov_dir="/var/cov/app")
    stamp_cov_dir(p)
    assert p.cov_dir == "/var/cov/app"
    assert cov_dir_of(p) == "/var/cov/app"


def test_abc_defaults_are_immutable_and_unknown():
    class _Bare(Product):
        name = "bare"

        async def stage(self, host):
            return Result(Status.Success)

        async def install(self, host):
            return Result(Status.Success)

        async def uninstall(self, host):
            return Result(Status.Success)

        async def is_installed(self, host):
            return True

    p = _Bare()
    assert p.cov_dir is None
    assert p.debug_log_globs == ()
    assert p.instrumented() is None  # a code product must override to be detected


@pytest.mark.parametrize("marker", INSTRUMENTATION_MARKERS)
def test_scan_finds_each_marker_mid_file(tmp_path, marker):
    f = tmp_path / "bin"
    f.write_bytes(b"\x7fELF" + b"\0" * 100 + marker + b"\0" * 100)
    assert scan_for_instrumentation(f) is True


def test_scan_finds_a_marker_straddling_the_chunk_boundary(tmp_path):
    f = tmp_path / "bin"
    marker = b".gcda"
    pad = (1 << 20) - 2  # the default chunk is 1 MiB; split the marker across it
    f.write_bytes(b"\0" * pad + marker + b"\0" * 10)
    assert scan_for_instrumentation(f) is True


def test_scan_clean_file_is_false(tmp_path):
    f = tmp_path / "bin"
    f.write_bytes(b"\x7fELF" + b"nothing to see" * 50)
    assert scan_for_instrumentation(f) is False


def test_scan_directory_is_true_if_any_file_hits_else_unknown(tmp_path):
    d = tmp_path / "bundle"
    d.mkdir()
    (d / "a").write_bytes(b"clean")
    assert scan_for_instrumentation(d) is None
    (d / "b").write_bytes(b"x__gcov_init")
    assert scan_for_instrumentation(d) is True


def test_scan_missing_path_is_unknown(tmp_path):
    assert scan_for_instrumentation(tmp_path / "absent") is None


@pytest.mark.parametrize(
    "name",
    [
        "app.tar",
        "app.tar.gz",
        "app.tgz",
        "app.tar.xz",
        "app.tar.bz2",
        "app.zip",
        "app.gz",
        "app.xz",
        "app.bz2",
        "app.zst",
        "APP.TAR.GZ",
    ],
)
def test_scan_archive_is_unknown_even_when_a_marker_is_visible(tmp_path, name):
    # An archive's bytes are not the artifact's bytes: a marker that happens to
    # be readable proves nothing about what is inside, and its absence proves
    # nothing either. The answer is "cannot tell", never "no".
    archive = tmp_path / name
    archive.write_bytes(b"\0.gcda\0")
    assert scan_for_instrumentation(archive) is None


def test_shellproduct_archive_artifact_is_unknown_without_an_override(tmp_path):
    archive = tmp_path / "app.tar.gz"
    archive.write_bytes(b"\0")
    assert _DummyShellProduct(artifact=archive).instrumented() is None
    assert _DummyShellProduct(artifact=archive, instrumented_override=True).instrumented() is True


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses file permissions")
def test_scan_unreadable_file_is_unknown_not_a_crash(tmp_path):
    f = tmp_path / "locked"
    f.write_bytes(b"x__gcov_init")
    f.chmod(0o000)
    try:
        assert scan_for_instrumentation(f) is None
    finally:
        f.chmod(0o644)  # tmp_path cleanup needs read/write back


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses file permissions")
def test_scan_directory_skips_an_unreadable_member(tmp_path):
    d = tmp_path / "bundle"
    d.mkdir()
    locked = d / "locked"
    locked.write_bytes(b"x__gcov_init")
    locked.chmod(0o000)
    (d / "clean").write_bytes(b"nothing here")
    try:
        assert scan_for_instrumentation(d) is None
    finally:
        locked.chmod(0o644)


def test_shellproduct_instrumented_uses_the_scan(tmp_path):
    hit = tmp_path / "hit"
    hit.write_bytes(b"\0__llvm_gcov\0")
    assert _DummyShellProduct(artifact=hit).instrumented() is True
    clean = tmp_path / "clean"
    clean.write_bytes(b"\0")
    assert _DummyShellProduct(artifact=clean).instrumented() is False


@pytest.mark.parametrize("override", [True, False])
def test_shellproduct_instrumented_override_wins_over_the_scan(tmp_path, override):
    """The override lives on the base, so every kind inherits one implementation."""
    hit = tmp_path / "hit"
    hit.write_bytes(b"\0__llvm_gcov\0")  # the scan would say True
    p = _DummyShellProduct(artifact=hit, instrumented_override=override)
    assert p.instrumented() is override


class _ExecHost:
    """Host double for the hooks: records exec() and answers a canned status."""

    def __init__(self, status=Status.Success):
        self.id = "h1"
        self.exec_calls: list = []
        self._status = status

    async def exec(self, cmd, **kwargs):
        self.exec_calls.append((cmd, kwargs))
        return CommandResult(self._status, value="", command=cmd, retcode=0)


@pytest.mark.asyncio
async def test_prepare_coverage_default_touches_nothing_and_succeeds():
    host = _ExecHost()
    result = await _DummyShellProduct(artifact=Path("/b/app"), name="app").prepare_coverage(host)
    assert result.is_ok
    assert host.exec_calls == []


@pytest.mark.asyncio
async def test_reset_coverage_default_deletes_every_gcda_under_the_quoted_cov_dir():
    host = _ExecHost()
    p = _DummyShellProduct(artifact=Path("/b/app"), name="app", cov_dir="/opt/My App/cov")
    result = await p.reset_coverage(host)
    assert result.is_ok
    assert host.exec_calls == [
        ("find '/opt/My App/cov' -name '*.gcda' -type f -delete", {"timeout": 60})
    ]


@pytest.mark.asyncio
async def test_reset_coverage_default_uses_the_default_cov_dir_when_unset():
    host = _ExecHost()
    await _DummyShellProduct(artifact=Path("/b/app"), name="app").reset_coverage(host)
    assert host.exec_calls[0][0] == "find /tmp/app -name '*.gcda' -type f -delete"


@pytest.mark.asyncio
async def test_reset_coverage_default_returns_the_hosts_failure():
    host = _ExecHost(status=Status.Error)
    result = await _DummyShellProduct(artifact=Path("/b/app"), name="app").reset_coverage(host)
    assert not result.is_ok


@pytest.mark.asyncio
async def test_reset_coverage_refuses_a_malformed_name_before_any_command():
    host = _ExecHost()
    with pytest.raises(ValueError, match="product name"):
        await _DummyShellProduct(artifact=Path("/b/app"), name="a/b").reset_coverage(host)
    assert host.exec_calls == []


class _RunHost:
    """Host double for sudo_gcda_delete: records run() and answers a canned status."""

    def __init__(self, status=Status.Success, output=""):
        self.id = "h1"
        self.run_calls: list = []
        self._status = status
        self._output = output

    async def run(self, cmd, **kwargs):
        self.run_calls.append((cmd, kwargs))
        if self._status is Status.NotRun:
            entry = NotRunResult(status=Status.NotRun, command=cmd, retcode=-1, host_name=self.id)
        else:
            retcode = 0 if self._status.is_ok else 1
            entry = CommandResult(self._status, value=self._output, command=cmd, retcode=retcode)
        return Results.collect([entry])


@pytest.mark.asyncio
async def test_sudo_gcda_delete_deletes_under_sudo():
    host = _RunHost()
    p = _DummyShellProduct(artifact=Path("/b/app"), name="app", cov_dir="/var/cov/app")
    result = await sudo_gcda_delete(p, host)
    assert result.is_ok
    assert host.run_calls == [("find /var/cov/app -name '*.gcda' -type f -delete", {"sudo": True})]


@pytest.mark.asyncio
async def test_sudo_gcda_delete_refuses_a_malformed_name_before_any_command():
    # sudo_gcda_delete validates the name itself — kmod_kind and
    # docker_image_kind's own reset_coverage() call it directly, bypassing
    # the unelevated default tested above, so this is its own arm.
    host = _RunHost()
    p = _DummyShellProduct(artifact=Path("/b/app"), name="a/b", cov_dir="/var/cov/app")
    with pytest.raises(ValueError, match="product name"):
        await sudo_gcda_delete(p, host)
    assert host.run_calls == []


@pytest.mark.asyncio
async def test_sudo_gcda_delete_failure_includes_the_commands_output():
    host = _RunHost(status=Status.Error, output="permission denied")
    p = _DummyShellProduct(artifact=Path("/b/app"), name="app", cov_dir="/var/cov/app")
    result = await sudo_gcda_delete(p, host)
    assert not result.is_ok
    assert result.msg == "app: deleting .gcda under /var/cov/app failed: permission denied"


@pytest.mark.asyncio
async def test_sudo_gcda_delete_propagates_a_dry_run_decline():
    host = _RunHost(status=Status.NotRun)
    p = _DummyShellProduct(artifact=Path("/b/app"), name="app", cov_dir="/var/cov/app")
    result = await sudo_gcda_delete(p, host)
    assert result.status is Status.NotRun


@pytest.mark.asyncio
async def test_product_get_debug_logs_hauls_globs_into_dest(tmp_path):
    calls = []

    class _Host:
        async def glob(self, pattern):
            return ["/var/log/app/a.log", "/var/log/app/b.log"]

        async def get(self, paths, dest):
            calls.append((paths, dest))
            return Result(Status.Success)

    p = _DummyShellProduct(
        artifact=Path("/x"), name="app", debug_log_globs=["/var/log/app/*.log", "/etc/app.conf"]
    )
    result = await p.get_debug_logs(_Host(), tmp_path)
    assert result.is_ok
    assert calls == [
        ([Path("/var/log/app/a.log"), Path("/var/log/app/b.log"), Path("/etc/app.conf")], tmp_path)
    ]


@pytest.mark.asyncio
async def test_product_get_debug_logs_glob_without_support_fails_loud(tmp_path):
    class _NoGlob:
        async def get(self, paths, dest):
            return Result(Status.Success)

    p = _DummyShellProduct(artifact=Path("/x"), name="app", debug_log_globs=["/logs/*.txt"])
    result = await p.get_debug_logs(_NoGlob(), tmp_path)
    assert not result.is_ok
    assert "glob" in result.msg
    assert "app" in result.msg


@pytest.mark.asyncio
async def test_product_get_debug_logs_no_globs_is_success_without_a_get(tmp_path):
    class _Host:
        async def get(self, paths, dest):
            raise AssertionError("get must not run for an empty list")

    p = _DummyShellProduct(artifact=Path("/x"), name="app")
    assert (await p.get_debug_logs(_Host(), tmp_path)).is_ok
