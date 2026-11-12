"""Unit tests for the Product lifecycle strategy and orchestration."""

import asyncio
import os
import shlex
import shutil
from pathlib import Path, PurePosixPath

import pytest

from otto.host.element import Element
from otto.host.login_proxy import Cred
from otto.host.product import Product, ShellProduct, gcov_prefix_strip
from otto.logger.mode import LogMode
from otto.result import CommandResult, NotRunResult, Result, Results
from otto.utils import Status
from tests._fixtures.shell_host import ELEVATED_MARK, ShellHost, fake_bin
from tests.unit.host._gcda_frame import CHECK_MARK, CHECK_PREFIX, delete_line, framed


class _DummyShellProduct(ShellProduct):
    """ShellProduct with the abstract halves stubbed so it can instantiate."""

    async def install(self, host):
        return Result(status=Status.Success)

    async def uninstall(self, host):
        return Result(status=Status.Success)

    async def is_installed(self, host):
        return True


class _RootOwnedShellProduct(_DummyShellProduct):
    """A kind whose ``cov_dir`` root writes: its reset runs elevated, read off the class."""

    cov_dir_root_owned = True


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


def test_gcov_prefix_strip_counts_build_root_components():
    assert gcov_prefix_strip(build_root=PurePosixPath("/home/dev/proj")) == 3
    assert gcov_prefix_strip(build_root=PurePosixPath("/builds/ci/proj")) == 3
    assert gcov_prefix_strip(build_root=PurePosixPath("/")) == 0


def test_gcov_prefix_strip_refuses_relative():
    with pytest.raises(ValueError, match="absolute"):
        gcov_prefix_strip(build_root=PurePosixPath("build"))


def test_gcov_prefix_strip_refuses_dotdot():
    with pytest.raises(ValueError, match=r"must not contain '\.\.'"):
        gcov_prefix_strip(build_root=PurePosixPath("/home/dev/proj/../other"))


def test_code_product_without_build_root_raises():
    product = _FakeProduct("x")  # a bare Product subclass, as a provider writes one
    assert product.build_root is None
    with pytest.raises(ValueError, match=r"'x' has no build_root.*GCOV_PREFIX_STRIP"):
        _ = product.gcov_prefix_strip


@pytest.mark.asyncio
async def test_get_logs_default_is_successful_noop(tmp_path):
    # Kills: an abstract get_logs, which would break every existing Product
    # subclass in every project at import time — _DummyShellProduct declares only
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
    put_result = Result(status=Status.Success, value={})
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
    assert p.stage_key(host=SimpleNamespace(id="h1", default_dest_dir=Path())) == LOGIN_HOME
    assert p.stage_key(host=SimpleNamespace(id="h1", default_dest_dir=Path("/srv"))) == "/srv"
    # ...and keys on the real path once the host has been asked.
    known = SimpleNamespace(id="h1", default_dest_dir=Path(), cached_login_home=Path("/home/t"))
    assert p.stage_key(host=known) == "/home/t"


def test_shellproduct_stage_dir_defaults_to_empty():
    assert _DummyShellProduct(artifact=Path("/builds/app.bin")).stage_dir == Path()


def test_a_shell_product_stages_an_artifact_but_a_bare_product_does_not():
    # The flag the per-host collision check keys on: only a product that puts
    # a FILE at <stage_dir>/<basename> can collide with another one.
    class _Bare(Product):
        name = "bare"

        async def stage(self, host):
            return Result(status=Status.Success)

        async def install(self, host):
            return Result(status=Status.Success)

        async def uninstall(self, host):
            return Result(status=Status.Success)

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
            element=Element(name="box"),
            creds=[Cred(login="u", password="p")],
            log=LogMode.QUIET,
        ).products
        == []
    )
    assert (
        ZephyrHost(ip="192.0.2.1", element=Element(name="zephyr37_fat"), log=LogMode.QUIET).products
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
            Result(status=Status.Error, msg="boom")
            if self.fail_on == "stage"
            else Result(status=Status.Success)
        )

    async def install(self, host):
        self.calls.append("install")
        if self.fail_on != "install":
            return Result(status=Status.Success)
        return self.result if self.result is not None else Result(status=Status.Error, msg="boom")

    async def uninstall(self, host):
        self.calls.append("uninstall")
        return (
            Result(status=Status.Error, msg="boom")
            if self.fail_on == "uninstall"
            else Result(status=Status.Success)
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
            return Result(status=Status.Success)

        async def uninstall(self, host):
            return Result(status=Status.Success)

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
        status=Status.Error, value="tar: bad header", command="tar xzf app.tgz", retcode=7
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
            return Result(status=Status.Success)

        async def install(self, host):
            return Result(status=Status.Success)

        async def uninstall(self, host):
            return Result(status=Status.Success)

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
        return CommandResult(status=self._status, value=framed(cmd, ""), command=cmd, retcode=0)


@pytest.mark.asyncio
async def test_prepare_coverage_default_touches_nothing_and_succeeds():
    host = _ExecHost()
    result = await _DummyShellProduct(artifact=Path("/b/app"), name="app").prepare_coverage(
        host=host
    )
    assert result.is_ok
    assert host.exec_calls == []


@pytest.mark.asyncio
async def test_reset_coverage_default_deletes_every_gcda_under_the_quoted_cov_dir():
    host = _ExecHost()
    p = _DummyShellProduct(artifact=Path("/b/app"), name="app", cov_dir="/opt/My App/cov")
    result = await p.reset_coverage(host=host)
    assert result.is_ok
    assert host.exec_calls == [
        (
            delete_line("/opt/My App/cov"),
            {"timeout": 60},
        ),
        (
            CHECK_PREFIX + "LC_ALL=C find '/opt/My App/cov' -name '*.gcda' -type f -print 2>&1",
            {"timeout": 60},
        ),
    ]


@pytest.mark.asyncio
async def test_reset_coverage_default_uses_the_default_cov_dir_when_unset():
    host = _ExecHost()
    await _DummyShellProduct(artifact=Path("/b/app"), name="app").reset_coverage(host=host)
    assert [cmd for cmd, _ in host.exec_calls] == [
        delete_line("/tmp/app"),
        f"{CHECK_PREFIX}LC_ALL=C find /tmp/app -name '*.gcda' -type f -print 2>&1",
    ]


@pytest.mark.asyncio
async def test_reset_coverage_default_returns_the_hosts_failure():
    host = _ExecHost(status=Status.Error)
    result = await _DummyShellProduct(artifact=Path("/b/app"), name="app").reset_coverage(host=host)
    assert not result.is_ok


@pytest.mark.asyncio
async def test_reset_coverage_refuses_a_malformed_name_before_any_command():
    host = _ExecHost()
    with pytest.raises(ValueError, match="product name"):
        await _DummyShellProduct(artifact=Path("/b/app"), name="a/b").reset_coverage(host=host)
    assert host.exec_calls == []


class _RunHost:
    """Host double for the elevated reset: records run() and answers a canned status."""

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
            value = framed(cmd, self._output)
            entry = CommandResult(status=self._status, value=value, command=cmd, retcode=retcode)
        return Results.collect(items=[entry])


@pytest.mark.asyncio
async def test_root_owned_reset_deletes_under_sudo():
    host = _RunHost()
    p = _RootOwnedShellProduct(artifact=Path("/b/app"), name="app", cov_dir="/var/cov/app")
    result = await p.reset_coverage(host=host)
    assert result.is_ok
    delete = delete_line("/var/cov/app")
    check = f"{CHECK_PREFIX}LC_ALL=C find /var/cov/app -name '*.gcda' -type f -print 2>&1"
    assert host.run_calls == [
        (f"sh -c {shlex.quote(delete)}", {"sudo": True, "timeout": 60}),
        (f"sh -c {shlex.quote(check)}", {"sudo": True, "timeout": 60}),
    ]


@pytest.mark.asyncio
async def test_root_owned_reset_refuses_a_malformed_name_before_any_command():
    # The elevated path validates the name before any command, like the
    # unelevated default tested above: kmod's override reaches it after its
    # own sysfs write, so this is its own arm.
    host = _RunHost()
    p = _RootOwnedShellProduct(artifact=Path("/b/app"), name="a/b", cov_dir="/var/cov/app")
    with pytest.raises(ValueError, match="product name"):
        await p.reset_coverage(host=host)
    assert host.run_calls == []


@pytest.mark.asyncio
async def test_root_owned_reset_failure_includes_the_commands_output():
    host = _RunHost(status=Status.Error, output="permission denied")
    p = _RootOwnedShellProduct(artifact=Path("/b/app"), name="app", cov_dir="/var/cov/app")
    result = await p.reset_coverage(host=host)
    assert not result.is_ok
    # The consumer prefixes `<host>/<product>: `; the reason never repeats the name.
    assert result.msg == "deleting .gcda under /var/cov/app failed: permission denied"


@pytest.mark.asyncio
async def test_root_owned_reset_propagates_a_dry_run_decline():
    host = _RunHost(status=Status.NotRun)
    p = _RootOwnedShellProduct(artifact=Path("/b/app"), name="app", cov_dir="/var/cov/app")
    result = await p.reset_coverage(host=host)
    assert result.status is Status.NotRun


# A cov_dir that does not exist yet (/tmp/<name> after a reboot, or a product
# deployed but never run) has nothing to clear; a present one keeps find's own
# verdict. These run the generated line through a REAL shell, so the `!`/`||`
# composition is exercised, not just spelled.


def _fake_find(tmp_path: Path, body: str) -> Path:
    """Write a ``find`` stand-in into a fresh bin dir and return that dir."""
    return fake_bin(tmp_path, "find", body)


def _failing_find(tmp_path: Path) -> Path:
    return _fake_find(tmp_path, "echo 'find: permission denied' >&2\nexit 1")


@pytest.mark.asyncio
async def test_reset_coverage_default_treats_a_missing_cov_dir_as_cleared(tmp_path):
    host = ShellHost()
    absent = tmp_path / "never-created"
    p = _DummyShellProduct(artifact=Path("/b/app"), name="app", cov_dir=str(absent))
    result = await p.reset_coverage(host=host)
    assert result.status is Status.Success, result
    assert not absent.exists()


@pytest.mark.asyncio
async def test_reset_coverage_default_fails_when_find_fails_on_a_present_cov_dir(tmp_path):
    host = ShellHost(bin_dir=_failing_find(tmp_path))
    present = tmp_path / "cov"
    present.mkdir()
    p = _DummyShellProduct(artifact=Path("/b/app"), name="app", cov_dir=str(present))
    result = await p.reset_coverage(host=host)
    assert result.status is Status.Error
    # The plain path answers with the same adapter message as the elevated one.
    assert result.msg.startswith(f"deleting .gcda under {present} failed")
    assert "permission denied" in result.msg


@pytest.mark.asyncio
async def test_root_owned_reset_treats_a_missing_cov_dir_as_cleared(tmp_path):
    host = ShellHost()
    absent = tmp_path / "never-created"
    p = _RootOwnedShellProduct(artifact=Path("/b/app"), name="app", cov_dir=str(absent))
    result = await p.reset_coverage(host=host)
    assert result.status is Status.Success, result.msg


@pytest.mark.asyncio
async def test_root_owned_reset_fails_when_find_fails_on_a_present_cov_dir(tmp_path):
    host = ShellHost(bin_dir=_failing_find(tmp_path))
    present = tmp_path / "cov"
    present.mkdir()
    p = _RootOwnedShellProduct(artifact=Path("/b/app"), name="app", cov_dir=str(present))
    result = await p.reset_coverage(host=host)
    assert result.status is Status.Error
    # find's own status, carried out of the pipeline, fails the removal.
    assert result.msg.startswith(f"deleting .gcda under {present} failed")
    assert "permission denied" in result.msg


@pytest.mark.asyncio
async def test_root_owned_reset_runs_the_whole_guarded_line_elevated(tmp_path):
    # A find that only succeeds elevated: the files are root's. Were the
    # guard spliced raw after sudo, sudo would exec `!` and the `||` tail —
    # the find — would run as the session user and fail here.
    real_find = shutil.which("find")
    assert real_find is not None
    bin_dir = _fake_find(
        tmp_path,
        f'[ "${ELEVATED_MARK}" = 1 ] || {{ echo "find: not elevated" >&2; exit 1; }}\n'
        f'exec {real_find} "$@"',
    )
    present = tmp_path / "cov"
    present.mkdir()
    (present / "a.gcda").write_bytes(b"")
    p = _RootOwnedShellProduct(artifact=Path("/b/app"), name="app", cov_dir=str(present))
    result = await p.reset_coverage(host=ShellHost(bin_dir=bin_dir))
    assert result.status is Status.Success, result.msg
    assert not (present / "a.gcda").exists()


# ── the reset's postcondition: every reset re-checks with `find -print` ─────
#
# `find -exec rm -f {} \;` does not carry rm's status, so a zero exit proves
# nothing: every reset, plain and elevated, re-runs the files-only check and
# fails, naming what is left, when a counter survives or the check itself
# fails. These drive both paths with the same inputs.


async def _reset(product, host, path):
    """Reset *product* plainly, or as the same product of a root-owned kind, by *path*."""
    if path == "elevated":
        product = _RootOwnedShellProduct(
            artifact=product.artifact, name=product.name, cov_dir=product.cov_dir
        )
    return await product.reset_coverage(host=host)


_PATHS = ["plain", "elevated"]


class _CheckHost:
    """The removal succeeds; the check (the marked line) answers ``(rc, output)``."""

    def __init__(self, check_rc, check_out):
        self.id = "h1"
        self.calls: list[str] = []
        self._check = (check_rc, check_out)

    def _answer(self, cmd):
        self.calls.append(cmd)
        rc, out = self._check if CHECK_MARK in cmd else (0, "")
        status = Status.Success if rc == 0 else Status.Failed
        return CommandResult(status=status, value=framed(cmd, out), command=cmd, retcode=rc)

    async def exec(self, cmd, **kwargs):
        return self._answer(cmd)

    async def run(self, cmd, **kwargs):
        return Results.collect(items=[self._answer(cmd)])


def _find_failing_only_the_check(tmp_path: Path, message: str) -> Path:
    """A ``find`` that runs the real one for the removal (``-exec``) and fails the check."""
    real_find = shutil.which("find")
    assert real_find is not None
    return _fake_find(
        tmp_path,
        'case " $* " in *" -exec "*) ;; *" -print "*) '
        f"echo {shlex.quote(message)}; exit 1;; esac\n"
        f'exec {real_find} "$@"',
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _PATHS)
async def test_reset_fails_when_a_failed_check_prints_absent(path):
    # The sentinel is a tag, never text: a failed check whose output is
    # exactly `absent` is an error, not an absent cov_dir.
    host = _CheckHost(1, "absent")
    p = _DummyShellProduct(artifact=Path("/b/app"), name="app", cov_dir="/var/cov/app")
    result = await _reset(p, host, path)
    assert result.status is Status.Error, result
    assert result.msg == "checking .gcda under /var/cov/app failed: absent"
    assert len(host.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _PATHS)
async def test_reset_succeeds_when_the_check_says_the_cov_dir_is_absent(path):
    line = "find: '/var/cov/app': No such file or directory"
    p = _DummyShellProduct(artifact=Path("/b/app"), name="app", cov_dir="/var/cov/app")
    result = await _reset(p, _CheckHost(1, line), path)
    assert result.status is Status.Success, result


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.geteuid() == 0, reason="root reads through a chmod 000 dir, so it cannot be made unreadable"
)
async def test_reset_check_fails_on_an_inaccessible_ancestor_plain(tmp_path):
    # `test -d` is false for a dir behind an unreadable parent, so the guarded
    # removal exits 0 without looking. The check has no guard: it must say
    # what stopped it, never report "clean" for a dir nobody could look into.
    parent = tmp_path / "locked"
    cov = parent / "cov"
    cov.mkdir(parents=True)
    (cov / "a.gcda").write_bytes(b"")
    p = _DummyShellProduct(artifact=Path("/b/app"), name="app", cov_dir=str(cov))
    parent.chmod(0)
    try:
        result = await p.reset_coverage(host=ShellHost())
    finally:
        parent.chmod(0o755)
    assert result.status is Status.Error, result
    assert result.msg.startswith(f"checking .gcda under {cov} failed: ")
    assert "Permission denied" in result.msg
    assert (cov / "a.gcda").exists()


@pytest.mark.asyncio
async def test_reset_check_fails_on_an_inaccessible_ancestor_elevated(tmp_path):
    # chmod cannot deny the root-like double, so a fake find stands in for
    # what an unreadable ancestor makes the real one print.
    parent = tmp_path / "locked"
    cov = parent / "cov"
    cov.mkdir(parents=True)
    (cov / "a.gcda").write_bytes(b"")
    bin_dir = _find_failing_only_the_check(tmp_path, f"find: '{parent}': Permission denied")
    p = _RootOwnedShellProduct(artifact=Path("/b/app"), name="app", cov_dir=str(cov))
    result = await p.reset_coverage(host=ShellHost(bin_dir=bin_dir))
    assert result.status is Status.Error, result
    assert result.msg.startswith(f"checking .gcda under {cov} failed: ")
    assert "Permission denied" in result.msg


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _PATHS)
async def test_reset_fails_when_the_check_fails_after_a_successful_removal(tmp_path, path):
    cov = tmp_path / "cov"
    cov.mkdir()
    (cov / "a.gcda").write_bytes(b"")
    bin_dir = _find_failing_only_the_check(tmp_path, f"find: '{cov}': Permission denied")
    p = _DummyShellProduct(artifact=Path("/b/app"), name="app", cov_dir=str(cov))
    result = await _reset(p, ShellHost(bin_dir=bin_dir), path)
    assert not (cov / "a.gcda").exists()  # the removal ran and worked
    assert result.status is Status.Error, result
    assert result.msg == f"checking .gcda under {cov} failed: find: '{cov}': Permission denied"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _PATHS)
async def test_reset_fails_when_a_counter_survives_the_removal(tmp_path, path):
    # An `rm` that fails and deletes nothing: `-exec rm -f {} \;` hides its
    # status, so only the check can see the counter that is still there.
    bin_dir = fake_bin(tmp_path, "rm", "exit 1")
    cov = tmp_path / "cov"
    cov.mkdir()
    survivor = cov / "a.gcda"
    survivor.write_bytes(b"")
    p = _DummyShellProduct(artifact=Path("/b/app"), name="app", cov_dir=str(cov))
    result = await _reset(p, ShellHost(bin_dir=bin_dir), path)
    assert survivor.exists()
    assert result.status is Status.Error, result
    assert result.msg == f"deleting .gcda under {cov} left 1 file(s): {survivor}"


# ── the reset removes the dirs it emptied, strictly below cov_dir (#722) ────


def _emptied_tree(tmp_path: Path, name: str) -> tuple[Path, Path]:
    """``above/<name>`` (the cov_dir) with emptied, mixed and pre-empty subtrees."""
    above = tmp_path / "above"
    cov = above / name
    (cov / "emptied" / "deeper").mkdir(parents=True)
    (cov / "emptied" / "deeper" / "x.gcda").write_bytes(b"")
    (cov / "emptied" / "y.gcda").write_bytes(b"")
    (cov / "mixed" / "sub").mkdir(parents=True)
    (cov / "mixed" / "keep.txt").write_text("user data")
    (cov / "mixed" / "sub" / "z.gcda").write_bytes(b"")
    (cov / "was empty").mkdir()
    (cov / "top.gcda").write_bytes(b"")
    (above / "neighbour").mkdir()
    return above, cov


def _tree(root: Path) -> list[str]:
    """Every path under *root*, relative and sorted."""
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


# A space splits an unquoted word; glob characters make an unquoted `case`
# bound match a different directory, so the walk would stop at once.
_COV_NAMES = ["my cov", "c[x]*"]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", _COV_NAMES)
@pytest.mark.parametrize("path", _PATHS)
async def test_reset_removes_the_dirs_it_emptied_and_nothing_else(tmp_path, path, name):
    above, cov = _emptied_tree(tmp_path, name)
    p = _DummyShellProduct(artifact=Path("/b/app"), name="app", cov_dir=str(cov))
    result = await _reset(p, ShellHost(), path)
    assert result.status is Status.Success, result.msg
    # An emptied subtree goes, deepest first; a dir holding an unrelated
    # file stays (its emptied child goes); a dir that was already empty is
    # not one the reset emptied, so it stays; the cov_dir, still holding
    # both, stays; and nothing above it is ever touched.
    assert _tree(above) == [
        name,
        f"{name}/mixed",
        f"{name}/mixed/keep.txt",
        f"{name}/was empty",
        "neighbour",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", _COV_NAMES)
@pytest.mark.parametrize("path", _PATHS)
async def test_reset_removes_the_cov_dir_it_emptied(tmp_path, path, name):
    cov = tmp_path / name
    (cov / "a" / "b").mkdir(parents=True)
    (cov / "a" / "b" / "c.gcda").write_bytes(b"")
    (cov / "top.gcda").write_bytes(b"")
    # A sibling an unquoted `rmdir c[x]*` would also match: never touched.
    (tmp_path / "cxy").mkdir()
    p = _DummyShellProduct(artifact=Path("/b/app"), name="app", cov_dir=str(cov))
    assert (await _reset(p, ShellHost(), path)).status is Status.Success
    # Emptied up to and including the cov_dir; nothing above it is touched.
    assert not cov.exists()
    assert sorted(q.name for q in tmp_path.iterdir()) == ["cxy"]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _PATHS)
async def test_reset_removes_a_cov_dir_that_was_already_empty(tmp_path, path):
    # Whatever it held before: an empty cov_dir after the clean goes.
    cov = tmp_path / "cov"
    cov.mkdir()
    p = _DummyShellProduct(artifact=Path("/b/app"), name="app", cov_dir=str(cov))
    assert (await _reset(p, ShellHost(), path)).status is Status.Success
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["file", "empty subdir"])
@pytest.mark.parametrize("path", _PATHS)
async def test_reset_keeps_a_cov_dir_holding_anything(tmp_path, path, content):
    # Any content at all keeps the cov_dir: an unrelated file, or an empty
    # dir that never held a counter (the reset did not empty it).
    cov = tmp_path / "cov"
    cov.mkdir()
    (cov / "a.gcda").write_bytes(b"")
    if content == "file":
        (cov / "notes.txt").write_text("user data")
    else:
        (cov / "spare").mkdir()
    p = _DummyShellProduct(artifact=Path("/b/app"), name="app", cov_dir=str(cov))
    assert (await _reset(p, ShellHost(), path)).status is Status.Success
    assert [q.name for q in cov.iterdir()] == ["notes.txt" if content == "file" else "spare"]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _PATHS)
async def test_reset_never_removes_through_a_symlinked_cov_dir(tmp_path, path):
    # rmdir(2) refuses a symlink (ENOTDIR) and never follows it: neither the
    # link nor the empty dir it names is removed.
    target = tmp_path / "target"
    target.mkdir()
    cov = tmp_path / "cov"
    cov.symlink_to(target)
    p = _DummyShellProduct(artifact=Path("/b/app"), name="app", cov_dir=str(cov))
    assert (await _reset(p, ShellHost(), path)).status is Status.Success
    assert cov.is_symlink()
    assert target.is_dir()


class _BindMountedShellProduct(_RootOwnedShellProduct):
    """A kind whose ``cov_dir`` is a live container's bind-mount source, as ``docker_image``'s."""

    cov_dir_bind_mounted = True


@pytest.mark.asyncio
async def test_reset_keeps_a_bind_mounted_cov_dir_it_emptied(tmp_path):
    # Removing a bind-mount source orphans the container's view of it: every
    # counter the container writes afterwards fails ENOENT and is lost.
    cov = tmp_path / "cov"
    (cov / "sub").mkdir(parents=True)
    (cov / "sub" / "a.gcda").write_bytes(b"")
    p = _BindMountedShellProduct(artifact=Path("/b/app"), name="app", cov_dir=str(cov))
    assert (await p.reset_coverage(host=ShellHost())).status is Status.Success
    assert cov.is_dir()
    assert list(cov.iterdir()) == []


def test_only_the_docker_image_kind_keeps_its_cov_dir():
    from otto.host.docker_image_kind import DockerImageProduct
    from otto.host.kmod_kind import KmodProduct

    assert DockerImageProduct.cov_dir_bind_mounted is True
    assert KmodProduct.cov_dir_bind_mounted is False
    assert ShellProduct.cov_dir_bind_mounted is False
    assert Product.cov_dir_bind_mounted is False


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _PATHS)
async def test_reset_never_walks_a_counter_path_holding_a_newline(tmp_path, path):
    # The walk reads find's output line by line, and its `case` bound is
    # textual: a counter whose path holds a newline would be read as a
    # second, forged line naming a path through a symlink, and rmdir would
    # remove an empty dir outside the cov_dir (as root, for an elevated
    # kind). Such a counter is still removed, but never walked.
    cov = tmp_path / "cov"
    outside = tmp_path / "outside"
    (outside / "victim").mkdir(parents=True)
    cov.mkdir()
    (cov / "link").symlink_to(outside)
    forged = Path(f"{cov}/x\n{cov}/link/victim/z.gcda")
    forged.parent.mkdir(parents=True)
    forged.write_bytes(b"")
    p = _DummyShellProduct(artifact=Path("/b/app"), name="app", cov_dir=str(cov))
    result = await _reset(p, ShellHost(), path)
    assert result.status is Status.Success, result.msg
    assert not forged.exists()
    assert (outside / "victim").is_dir()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _PATHS)
async def test_reset_removes_every_counter_when_the_prune_cannot_sort(tmp_path, path):
    # Pruning is best effort: a sort that dies without reading its input
    # must not SIGPIPE the walk, and through it find, mid-removal.
    bin_dir = fake_bin(tmp_path, "sort", "exit 127")
    cov = tmp_path / "cov"
    for sub in range(10):
        (cov / f"d{sub}").mkdir(parents=True)
    for i in range(2000):
        (cov / f"d{i % 10}" / f"{i:04d}_{'x' * 60}.gcda").write_bytes(b"")
    p = _DummyShellProduct(artifact=Path("/b/app"), name="app", cov_dir=str(cov))
    result = await _reset(p, ShellHost(bin_dir=bin_dir), path)
    assert result.status is Status.Success, result.msg
    assert list(cov.rglob("*.gcda")) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _PATHS)
async def test_reset_succeeds_when_rmdir_fails(tmp_path, path):
    # A dir the reset cannot remove stays, silently: the counters are gone.
    bin_dir = fake_bin(tmp_path, "rmdir", 'echo "rmdir: Permission denied" >&2; exit 1')
    cov = tmp_path / "cov"
    (cov / "sub").mkdir(parents=True)
    (cov / "sub" / "a.gcda").write_bytes(b"")
    p = _DummyShellProduct(artifact=Path("/b/app"), name="app", cov_dir=str(cov))
    result = await _reset(p, ShellHost(bin_dir=bin_dir), path)
    assert result.status is Status.Success, result.msg
    assert (cov / "sub").is_dir()
    assert not (cov / "sub" / "a.gcda").exists()


@pytest.mark.asyncio
async def test_reset_removes_the_emptied_dirs_through_the_same_elevation(tmp_path):
    # An rmdir that only works elevated: the root-owned kind's emptied dirs
    # go only if the reset's own elevated runner ran the removal.
    real_rmdir = shutil.which("rmdir")
    assert real_rmdir is not None
    bin_dir = fake_bin(
        tmp_path,
        "rmdir",
        f'[ "${ELEVATED_MARK}" = 1 ] || {{ echo "rmdir: not elevated" >&2; exit 1; }}\n'
        f'exec {real_rmdir} "$@"',
    )
    cov = tmp_path / "cov"
    (cov / "sub").mkdir(parents=True)
    (cov / "sub" / "a.gcda").write_bytes(b"")
    p = _RootOwnedShellProduct(artifact=Path("/b/app"), name="app", cov_dir=str(cov))
    result = await p.reset_coverage(host=ShellHost(bin_dir=bin_dir))
    assert result.status is Status.Success, result.msg
    assert not cov.exists()  # the emptied cov_dir too, under the same elevation


@pytest.mark.asyncio
async def test_reset_check_runs_through_the_same_elevation_as_the_removal(tmp_path):
    # A find that only works elevated and logs each call: the elevated reset
    # passes only if the check ran elevated too.
    real_find = shutil.which("find")
    assert real_find is not None
    log = tmp_path / "find.log"
    bin_dir = _fake_find(
        tmp_path,
        # One line per call, though the removal's -path pattern holds a newline.
        f'echo "${ELEVATED_MARK} $*" | tr "\\n" " " >> {shlex.quote(str(log))}\n'
        f"echo >> {shlex.quote(str(log))}\n"
        f'[ "${ELEVATED_MARK}" = 1 ] || {{ echo "find: not elevated" >&2; exit 1; }}\n'
        f'exec {real_find} "$@"',
    )
    cov = tmp_path / "cov"
    cov.mkdir()
    (cov / "a.gcda").write_bytes(b"")
    p = _RootOwnedShellProduct(artifact=Path("/b/app"), name="app", cov_dir=str(cov))
    result = await p.reset_coverage(host=ShellHost(bin_dir=bin_dir))
    assert result.status is Status.Success, result.msg
    calls = [line.rstrip() for line in log.read_text().splitlines()]
    assert len(calls) == 2
    assert calls[0].startswith("1 ")  # the removal ran elevated
    assert "-exec rm -f {} ; ! -path" in calls[0]
    assert calls[1].startswith("1 ")  # and so did its check
    assert calls[1].endswith("-print")


# What sudo prints before it execs the command lands in the same stream: a
# warning, and the answered prompt, which has no trailing newline when the
# session's stdin is a pipe (LocalHost).
_SUDO_NOISE = (
    "sudo: unable to resolve host h1: Name or service not known\notto-sudo-AAAAAAAAAAAAAAAA:"
)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _PATHS)
async def test_reset_ignores_what_the_session_printed_before_the_check_ran(tmp_path, path):
    host = ShellHost(noise=_SUDO_NOISE)
    # A clean cov_dir is clean, and an absent one is absent, noise or not.
    cov = tmp_path / "cov"
    cov.mkdir()
    (cov / "a.gcda").write_bytes(b"")
    p = _DummyShellProduct(artifact=Path("/b/app"), name="app", cov_dir=str(cov))
    assert (await _reset(p, host, path)).status is Status.Success
    assert not cov.exists()  # emptied, so removed with its last counter
    absent = _DummyShellProduct(
        artifact=Path("/b/app"), name="app", cov_dir=str(tmp_path / "never-created")
    )
    assert (await _reset(absent, host, path)).status is Status.Success
    # A real survivor is still named, and the noise is never counted as one.
    cov.mkdir()
    survivor = cov / "b.gcda"
    survivor.write_bytes(b"")
    noisy_rm = ShellHost(bin_dir=fake_bin(tmp_path, "rm", "exit 1"), noise=_SUDO_NOISE)
    result = await _reset(p, noisy_rm, path)
    assert result.msg == f"deleting .gcda under {cov} left 1 file(s): {survivor}"


@pytest.mark.asyncio
async def test_gcda_delete_cmd_in_a_real_shell(tmp_path):
    from otto.host.product import classify_gcda_check, gcda_check_cmd, gcda_delete_cmd

    async def sh(line):
        proc = await asyncio.create_subprocess_shell(
            line, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        out, _ = await proc.communicate()
        return proc.returncode, out.decode()

    # Absent: nothing to clear, success.
    rc, out = await sh(gcda_delete_cmd(str(tmp_path / "no such dir")))
    assert (rc, out) == (0, "")

    # Present: every .gcda goes, at any depth; nothing else is touched.
    cov = tmp_path / "my cov"  # a space: the quoting is load-bearing
    (cov / "sub").mkdir(parents=True)
    (cov / "a.gcda").write_bytes(b"")
    (cov / "sub" / "b.gcda").write_bytes(b"")
    (cov / "a.gcno").write_bytes(b"")
    rc, out = await sh(gcda_delete_cmd(str(cov)))
    assert (rc, out) == (0, "")
    assert sorted(p.name for p in cov.rglob("*") if p.is_file()) == ["a.gcno"]
    # POSIX only: `-delete` is an extension BusyBox builds compile out (#842).
    assert "-delete" not in gcda_delete_cmd(str(cov))

    # The real-GNU leg of the rendering pin: an absent cov_dir, however its
    # name must be quoted, is recognised as absent from find's own line.
    for name in ("plain", "with space", "it's gone", "back\\slash"):
        absent = str(tmp_path / "absent" / name)
        rc, out = await sh(gcda_check_cmd(absent))
        status = Status.Success if rc == 0 else Status.Failed
        cmd = CommandResult(status=status, value=out, command="find", retcode=rc)
        assert classify_gcda_check(absent, cmd) == "absent", (name, rc, out)


@pytest.mark.asyncio
async def test_product_get_debug_logs_hauls_globs_into_dest(tmp_path):
    calls = []

    class _Host:
        async def glob(self, pattern):
            return ["/var/log/app/a.log", "/var/log/app/b.log"]

        async def get(self, paths, dest):
            calls.append((paths, dest))
            return Result(status=Status.Success)

    p = _DummyShellProduct(
        artifact=Path("/x"), name="app", debug_log_globs=["/var/log/app/*.log", "/etc/app.conf"]
    )
    result = await p.get_debug_logs(host=_Host(), dest=tmp_path)
    assert result.is_ok
    assert calls == [
        ([Path("/var/log/app/a.log"), Path("/var/log/app/b.log"), Path("/etc/app.conf")], tmp_path)
    ]


@pytest.mark.asyncio
async def test_product_get_debug_logs_glob_without_support_fails_loud(tmp_path):
    class _NoGlob:
        async def get(self, paths, dest):
            return Result(status=Status.Success)

    p = _DummyShellProduct(artifact=Path("/x"), name="app", debug_log_globs=["/logs/*.txt"])
    result = await p.get_debug_logs(host=_NoGlob(), dest=tmp_path)
    assert not result.is_ok
    assert "glob" in result.msg
    assert "app" in result.msg


@pytest.mark.asyncio
async def test_product_get_debug_logs_no_globs_is_success_without_a_get(tmp_path):
    class _Host:
        async def get(self, paths, dest):
            raise AssertionError("get must not run for an empty list")

    p = _DummyShellProduct(artifact=Path("/x"), name="app")
    assert (await p.get_debug_logs(host=_Host(), dest=tmp_path)).is_ok
