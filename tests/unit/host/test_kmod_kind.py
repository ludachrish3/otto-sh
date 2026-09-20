"""The built-in ``kmod`` kind: params, the three verbs, the two hooks per method."""

import logging
import shlex
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from otto.declared import DeclaredEntry
from otto.host import kmod_kind  # noqa: F401 — import registers the kind
from otto.host import product as product_mod
from otto.host.kmod_kind import KGCOV_DEBUGFS, KmodProduct
from otto.result import CommandResult, NotRunResult, Result, Results
from otto.utils import Status


def _entry(**params):
    params.setdefault("artifact", "build/demo/otto_kmod_demo.ko")
    return DeclaredEntry(
        name=params.pop("name", "demo"),
        kind="kmod",
        seam="products",
        owner="r",
        base_dir=Path("/repo"),
        match={},
        params=params,
    )


class _KmodHost(SimpleNamespace):
    """A Unix-host double: load/unload/lsmod verbs plus run()/exec() recorders."""

    def __init__(self, *, loaded=(), run_status=Status.Success, run_value="", **attrs):
        super().__init__(**attrs)
        self.id = attrs.get("id", "test1")
        self.load = AsyncMock(return_value=Result(Status.Success))
        self.unload = AsyncMock(return_value=Result(Status.Success))
        self.lsmod = AsyncMock(return_value=Result(Status.Success, value=list(loaded)))
        self.run_calls: list[tuple[str, dict]] = []
        self.exec_calls: list[tuple[str, dict]] = []
        self.dev_tools: list = []
        self.products: list = []
        self._run_status = run_status
        self._run_value = run_value

    async def run(self, cmd, **kw):
        self.run_calls.append((cmd, kw))
        retcode = 0 if self._run_status is Status.Success else 1
        result = CommandResult(
            command=cmd, value=self._run_value, status=self._run_status, retcode=retcode
        )
        return Results.collect([result])

    async def exec(self, cmd, **kw):
        self.exec_calls.append((cmd, kw))
        return CommandResult(self._run_status, value="", command=cmd, retcode=0)


def _build(host=None, **params) -> KmodProduct:
    return product_mod.PRODUCT_KINDS.get("kmod")(_entry(**params), host or _KmodHost())


def _attach_kgcov(host, tmp_path: Path, *, version=None):
    """A kgcov dev tool on *host* whose .ko carries this otto's interface (or *version*)."""
    from otto import kgcov as kgcov_mod
    from otto.host.dev_tool import DEV_TOOL_KINDS

    ko = tmp_path / "otto_kgcov.ko"
    v = version or f"1.6.0+kgcov{kgcov_mod.INTERFACE}"
    ko.write_bytes(b"\x7fELF\x00" + f"version={v}".encode() + b"\x00vermagic=6.8 SMP\x00")
    entry = DeclaredEntry(
        name="kgcov-6.8",
        kind="kgcov",
        seam="dev_tools",
        owner="r",
        base_dir=Path("/repo"),
        match={},
        params={"artifact": str(ko)},
    )
    tool = DEV_TOOL_KINDS.get("kgcov")(entry, host)
    host.dev_tools.append(tool)
    return tool


def _expected_kernel_prepare_cmd(gcov_path: str, cov_dir: str) -> str:
    """Independently reconstruct the exact ``sh -c`` command ``_prepare_kernel`` emits.

    Built from :func:`shlex.quote` directly rather than by calling
    ``kmod_kind`` — an exact-equality pin on the production algorithm, not a
    restatement of it.
    """
    root = "/sys/kernel/debug/gcov"
    rel = gcov_path[len(root) + 1 :]
    root_q = shlex.quote(root)
    rel_q = shlex.quote(rel)
    dest_q = shlex.quote(cov_dir)
    script = (
        f'cd {root_q} && test -d {rel_q} && find {rel_q} -name "*.gcda" -type f | '
        f'while read -r f; do mkdir -p {dest_q}/"$(dirname "$f")" && '
        f'cat "$f" > {dest_q}/"$f" || exit 1; done'
    )
    return f"sh -c {shlex.quote(script)}"


def _expected_module_write_cmd(module_name: str, action: str) -> str:
    """Independently reconstruct the exact ``sh -c`` command for a module dump/reset write."""
    script = f"echo 1 > {KGCOV_DEBUGFS}/{module_name}/{action}"
    return f"sh -c {shlex.quote(script)}"


def _expected_kernel_reset_cmds(gcov_path: str, cov_dir: str) -> tuple[str, str]:
    """Independently reconstruct the two commands the kernel ``reset_coverage`` branch emits."""
    path_q = shlex.quote(gcov_path)
    dest_q = shlex.quote(cov_dir)
    reset_script = (
        f'test -d {path_q} && find {path_q} -name "*.gcda" -type f | '
        'while read -r f; do echo 1 > "$f" || exit 1; done'
    )
    reset_cmd = f"sh -c {shlex.quote(reset_script)}"
    delete_cmd = f"find {dest_q} -name '*.gcda' -type f -delete"
    return reset_cmd, delete_cmd


# ── builder ──────────────────────────────────────────────────────────────────


def test_kmod_is_registered_in_both_seams_with_its_own_factory_each():
    from otto.host import kmod_tool_kind  # noqa: F401 — registers the dev-tool side
    from otto.host.dev_tool import DEV_TOOL_KINDS

    assert "kmod" in product_mod.PRODUCT_KINDS
    assert "kmod" in DEV_TOOL_KINDS
    assert product_mod.PRODUCT_KINDS.get("kmod") is not DEV_TOOL_KINDS.get("kmod")


def test_kmod_defaults_module_name_to_the_stem_with_underscores():
    p = _build(artifact="build/my-driver.ko")
    assert p.artifact == Path("/repo/build/my-driver.ko")
    assert p.module_name == "my_driver"
    assert p.coverage == "none"
    assert p.params == ""


def test_kmod_reads_module_name_params_coverage_and_gcov_path():
    p = _build(
        module_name="demo_x",
        params="debug=1 {name}",
        coverage="kernel",
        gcov_path="/sys/kernel/debug/gcov/home/build/demo",
        cov_dir="/var/cov/demo",
    )
    assert (p.module_name, p.params, p.coverage) == ("demo_x", "debug=1 demo", "kernel")
    assert p.gcov_path == "/sys/kernel/debug/gcov/home/build/demo"
    assert p.cov_dir == "/var/cov/demo"


@pytest.mark.parametrize(
    ("params", "fragment"),
    [
        ({"artifact": "build/demo.bin"}, "'artifact' must be a .ko"),
        ({"coverage": "debugfs"}, "'coverage' must be one of none, module, kernel"),
        ({"coverage": "kernel"}, "'gcov_path' is required with coverage = \"kernel\""),
        (
            {"coverage": "kernel", "gcov_path": "/tmp/x"},
            "'gcov_path' must be under /sys/kernel/debug/gcov/",
        ),
        ({"coverage": "module", "params": "gcov_dir=/x"}, "'params' must not set gcov_dir"),
        (
            {"bogus": 1},
            (
                "kind 'kmod' got unknown param(s): ['bogus']; valid: artifact, module_name, "
                "params, coverage, gcov_path, cov_dir, instrumented, debug_log_globs"
            ),
        ),
        ({"params": "{typo}"}, "unknown placeholder"),
    ],
)
def test_kmod_rejects_bad_params_naming_the_entry(params, fragment):
    with pytest.raises(ValueError, match=r"\[\[products\]\] 'demo'") as ei:
        _build(**params)
    assert fragment in str(ei.value)


def test_kmod_refuses_a_host_without_the_module_verbs():
    host = SimpleNamespace(id="board1", products=[])
    match = r"'demo': kind 'kmod' matched host board1, which has no load/unload/lsmod"
    with pytest.raises(ValueError, match=match):
        product_mod.PRODUCT_KINDS.get("kmod")(_entry(), host)


# ── verbs ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_kmod_stage_is_a_noop_and_install_loads_with_params():
    host = _KmodHost()
    p = _build(host, params="debug=1")
    assert (await p.stage(host)).is_ok
    assert (await p.install(host)).is_ok
    host.load.assert_awaited_once_with(
        Path("/repo/build/demo/otto_kmod_demo.ko"), "otto_kmod_demo", params="debug=1"
    )


@pytest.mark.asyncio
async def test_kmod_module_method_appends_gcov_dir_to_the_params(tmp_path):
    host = _KmodHost(loaded=["otto_kgcov"])
    _attach_kgcov(host, tmp_path)
    p = _build(host, coverage="module", cov_dir="/var/cov/demo", params="debug=1")
    await p.install(host)
    assert host.load.await_args.kwargs["params"] == "debug=1 gcov_dir=/var/cov/demo"
    p2 = _build(host, coverage="module")  # default cov_dir
    await p2.install(host)
    assert host.load.await_args.kwargs["params"] == "gcov_dir=/tmp/demo"


@pytest.mark.asyncio
async def test_kmod_module_method_quotes_a_cov_dir_containing_whitespace(tmp_path):
    # UnixHost.load appends `params` to the insmod line UNQUOTED, so the
    # gcov_dir=<cov_dir> token must be shlex.quote()d as ONE token when
    # cov_dir itself contains whitespace.
    host = _KmodHost(loaded=["otto_kgcov"])
    _attach_kgcov(host, tmp_path)
    p = _build(host, coverage="module", cov_dir="/var/cov/my demo", params="debug=1")
    await p.install(host)
    assert host.load.await_args.kwargs["params"] == "debug=1 'gcov_dir=/var/cov/my demo'"


@pytest.mark.asyncio
async def test_kmod_is_installed_reads_lsmod_and_uninstall_unloads():
    host = _KmodHost(loaded=["ext4", "otto_kmod_demo"])
    p = _build(host)
    assert await p.is_installed(host) is True
    assert await _build(host, module_name="other").is_installed(host) is False
    host.lsmod.return_value = Result(Status.Error, value=[], msg="boom")
    assert await p.is_installed(host) is False
    assert (await p.uninstall(host)).is_ok
    host.unload.assert_awaited_once_with("otto_kmod_demo")


@pytest.mark.asyncio
async def test_kmod_module_install_loads_the_library_first_when_absent(tmp_path):
    host = _KmodHost(loaded=["ext4"])
    tool = _attach_kgcov(host, tmp_path)
    p = _build(host, coverage="module", cov_dir="/var/cov/demo")
    assert (await p.install(host)).is_ok
    calls = host.load.await_args_list
    assert calls[0].args == (tool.artifact, "otto_kgcov")
    assert calls[1].args == (Path("/repo/build/demo/otto_kmod_demo.ko"), "otto_kmod_demo")
    assert calls[1].kwargs["params"] == "gcov_dir=/var/cov/demo"


@pytest.mark.asyncio
async def test_kmod_module_install_announces_the_library_load_at_info(tmp_path, caplog):
    # The one line a lane reads back out of verbose.log to prove the demo
    # loaded the library itself; nothing is said when it was already there.
    host = _KmodHost(loaded=["ext4"])
    tool = _attach_kgcov(host, tmp_path)
    p = _build(host, coverage="module")
    with caplog.at_level(logging.INFO, logger="otto.host.kmod_kind"):
        assert (await p.install(host)).is_ok
    lines = [r.getMessage() for r in caplog.records if "otto_kgcov" in r.getMessage()]
    assert lines == [f"test1: demo: loading otto_kgcov ({tool.name})"]


@pytest.mark.asyncio
async def test_kmod_module_install_says_nothing_when_the_library_is_resident(tmp_path, caplog):
    host = _KmodHost(loaded=["otto_kgcov"])
    _attach_kgcov(host, tmp_path)
    p = _build(host, coverage="module")
    with caplog.at_level(logging.INFO, logger="otto.host.kmod_kind"):
        assert (await p.install(host)).is_ok
    assert [r.getMessage() for r in caplog.records if "otto_kgcov" in r.getMessage()] == []


@pytest.mark.asyncio
async def test_kmod_module_install_skips_the_library_when_resident(tmp_path):
    host = _KmodHost(loaded=["otto_kgcov"])
    _attach_kgcov(host, tmp_path)
    p = _build(host, coverage="module")
    assert (await p.install(host)).is_ok
    assert host.load.await_count == 1
    assert host.load.await_args.args[1] == "otto_kmod_demo"


@pytest.mark.asyncio
async def test_kmod_module_install_fails_naming_the_missing_kgcov_tool():
    host = _KmodHost()
    p = _build(host, coverage="module")
    result = await p.install(host)
    assert result.status is Status.Error
    assert "demo" in result.msg
    assert "test1" in result.msg
    assert "kind 'kgcov'" in result.msg
    host.load.assert_not_awaited()


@pytest.mark.asyncio
async def test_kmod_module_install_reports_a_library_load_failure_as_the_tools(tmp_path):
    host = _KmodHost(loaded=[])
    tool = _attach_kgcov(host, tmp_path)
    host.load.return_value = Result(Status.Error, msg="insmod otto_kgcov failed: vermagic")
    p = _build(host, coverage="module")
    result = await p.install(host)
    assert result.status is Status.Error
    assert tool.name in result.msg
    assert "vermagic" in result.msg
    assert host.load.await_count == 1  # the consumer's own insmod never ran


@pytest.mark.asyncio
async def test_kmod_module_install_failure_after_the_library_loaded_is_the_consumers(tmp_path):
    # The library went in; the consumer's own insmod is what failed. The
    # result is the PRODUCT's failure, with the library named as resident so
    # nobody goes looking for a missing otto_kgcov.
    host = _KmodHost(loaded=["ext4"])
    _attach_kgcov(host, tmp_path)

    async def _load(file, name, params=""):
        if name == "otto_kgcov":
            host.lsmod.return_value = Result(Status.Success, value=["ext4", "otto_kgcov"])
            return Result(Status.Success)
        return Result(Status.Error, msg="insmod otto_kmod_demo failed: Unknown symbol in module")

    host.load = AsyncMock(side_effect=_load)
    p = _build(host, coverage="module", cov_dir="/var/cov/demo")
    result = await p.install(host)
    assert result.status is Status.Error
    assert result.msg.startswith("demo: ")
    assert "insmod otto_kmod_demo failed: Unknown symbol in module" in result.msg
    assert "otto_kgcov: 'kgcov-6.8' dev tool, resident" in result.msg


@pytest.mark.asyncio
async def test_kmod_module_install_propagates_a_dry_run_decline_from_the_library(tmp_path):
    # A declined library load must NOT stop the consumer's own insmod from
    # being announced: a dry run exists to show the whole plan, and the
    # decline of the second line is what carries the NotRun back.
    host = _KmodHost()
    _attach_kgcov(host, tmp_path)
    host.lsmod.return_value = NotRunResult(
        status=Status.NotRun, command="lsmod", retcode=-1, host_name=host.id
    )
    host.load.return_value = NotRunResult(
        status=Status.NotRun, command="insmod", retcode=-1, host_name=host.id
    )
    p = _build(host, coverage="module", cov_dir="/var/cov/demo")
    result = await p.install(host)
    assert result.status is Status.NotRun
    assert host.load.await_count == 2
    assert host.load.await_args.args == (
        Path("/repo/build/demo/otto_kmod_demo.ko"),
        "otto_kmod_demo",
    )
    assert host.load.await_args.kwargs["params"] == "gcov_dir=/var/cov/demo"


@pytest.mark.asyncio
async def test_kmod_none_and_kernel_methods_never_touch_the_library():
    for coverage, extra in (("none", {}), ("kernel", {"gcov_path": "/sys/kernel/debug/gcov/x"})):
        host = _KmodHost()
        p = _build(host, coverage=coverage, **extra)
        assert (await p.install(host)).is_ok
        assert host.load.await_count == 1


# ── hooks ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_kmod_none_method_inherits_the_default_hooks():
    host = _KmodHost()
    p = _build(host, cov_dir="/var/cov/demo")
    assert (await p.prepare_coverage(host)).is_ok
    assert host.run_calls == []
    assert host.exec_calls == []
    await p.reset_coverage(host)
    expected = ("find /var/cov/demo -name '*.gcda' -type f -delete", {"timeout": 60})
    assert host.exec_calls == [expected]


@pytest.mark.asyncio
async def test_kmod_module_prepare_writes_dump_while_loaded_under_sudo():
    host = _KmodHost(loaded=["otto_kmod_demo"])
    p = _build(host, coverage="module")
    assert (await p.prepare_coverage(host)).is_ok
    ((cmd, kw),) = host.run_calls
    assert kw == {"sudo": True}
    assert cmd == _expected_module_write_cmd("otto_kmod_demo", "dump")


@pytest.mark.asyncio
async def test_kmod_module_prepare_is_a_noop_success_when_unloaded():
    host = _KmodHost(loaded=[])
    p = _build(host, coverage="module")
    assert (await p.prepare_coverage(host)).is_ok
    assert host.run_calls == []


@pytest.mark.asyncio
async def test_kmod_module_prepare_failure_names_the_debugfs_path():
    host = _KmodHost(loaded=["otto_kmod_demo"], run_status=Status.Error)
    p = _build(host, coverage="module")
    result = await p.prepare_coverage(host)
    assert not result.is_ok
    assert "/sys/kernel/debug/otto_kgcov/otto_kmod_demo/dump" in result.msg
    assert "otto_kgcov" in result.msg


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("loaded", "expected"),
    [
        (["otto_kmod_demo", "otto_kgcov"], "otto_kgcov: 'kgcov-6.8' dev tool, resident"),
        (["otto_kmod_demo"], "otto_kgcov: 'kgcov-6.8' dev tool, not resident"),
    ],
)
async def test_kmod_module_prepare_failure_names_the_kgcov_tool(tmp_path, loaded, expected):
    # The message STATES whether the library is resident rather than asking:
    # residency is one lsmod away, and this is already an error path.
    host = _KmodHost(loaded=loaded, run_status=Status.Error, run_value="No such file")
    _attach_kgcov(host, tmp_path)
    p = _build(host, coverage="module")
    result = await p.prepare_coverage(host)
    assert result.status is Status.Error
    assert expected in result.msg


@pytest.mark.asyncio
async def test_kmod_module_prepare_propagates_a_dry_run_decline():
    # A NotRun result is the session declining under --dry-run — it must come
    # back as Status.NotRun, not be mapped to Error. `run()`
    # (unlike `exec()`) always answers a `Results`, so the double wraps its
    # NotRunResult exactly as the real BaseHost.run does.
    host = _KmodHost(loaded=["otto_kmod_demo"])

    async def run(cmd, **kw):
        host.run_calls.append((cmd, kw))
        return Results.collect(
            [NotRunResult(status=Status.NotRun, command=cmd, retcode=-1, host_name=host.id)]
        )

    host.run = run
    p = _build(host, coverage="module")
    result = await p.prepare_coverage(host)
    assert result.status is Status.NotRun


@pytest.mark.asyncio
async def test_kmod_module_prepare_announces_the_write_when_lsmod_is_declined():
    # A declined `lsmod` is a decline of the READ, not "not installed" — it
    # must not be laundered through is_installed()'s bool into a fabricated
    # Success/noop. Residency is now unknowable, so the write a real run
    # might issue is announced anyway (the session declines it in turn), and
    # the overall result mirrors that decline.
    host = _KmodHost()
    host.lsmod.return_value = NotRunResult(
        status=Status.NotRun, command="cat /proc/modules", retcode=-1, host_name=host.id
    )

    async def run(cmd, **kw):
        host.run_calls.append((cmd, kw))
        return Results.collect(
            [NotRunResult(status=Status.NotRun, command=cmd, retcode=-1, host_name=host.id)]
        )

    host.run = run
    p = _build(host, coverage="module")
    result = await p.prepare_coverage(host)
    assert result.status is Status.NotRun
    ((cmd, kw),) = host.run_calls
    assert kw == {"sudo": True}
    assert cmd == _expected_module_write_cmd("otto_kmod_demo", "dump")


@pytest.mark.asyncio
async def test_kmod_module_prepare_fails_when_lsmod_itself_fails():
    host = _KmodHost()
    host.lsmod.return_value = Result(Status.Error, value=[], msg="boom")
    p = _build(host, coverage="module")
    result = await p.prepare_coverage(host)
    assert not result.is_ok
    assert result.status is not Status.NotRun
    assert "lsmod" in result.msg
    assert host.run_calls == []


@pytest.mark.asyncio
async def test_kmod_module_reset_writes_reset_then_deletes_under_sudo():
    host = _KmodHost(loaded=["otto_kmod_demo"])
    p = _build(host, coverage="module", cov_dir="/var/cov/demo")
    assert (await p.reset_coverage(host)).is_ok
    cmds = [c for c, _ in host.run_calls]
    assert cmds[0] == _expected_module_write_cmd("otto_kmod_demo", "reset")
    assert cmds[1] == "find /var/cov/demo -name '*.gcda' -type f -delete"
    assert all(kw == {"sudo": True} for _, kw in host.run_calls)
    assert host.exec_calls == []


@pytest.mark.asyncio
async def test_kmod_module_reset_fails_when_lsmod_itself_fails():
    host = _KmodHost()
    host.lsmod.return_value = Result(Status.Error, value=[], msg="boom")
    p = _build(host, coverage="module", cov_dir="/var/cov/demo")
    result = await p.reset_coverage(host)
    assert not result.is_ok
    assert result.status is not Status.NotRun
    assert "lsmod" in result.msg
    assert host.run_calls == []  # neither the debugfs write nor the delete


@pytest.mark.asyncio
async def test_kmod_module_reset_failure_names_the_kgcov_tool_and_its_residency(tmp_path):
    host = _KmodHost(
        loaded=["otto_kmod_demo", "otto_kgcov"], run_status=Status.Error, run_value="No such file"
    )
    _attach_kgcov(host, tmp_path)
    p = _build(host, coverage="module", cov_dir="/var/cov/demo")
    result = await p.reset_coverage(host)
    assert result.status is Status.Error
    assert "otto_kgcov: 'kgcov-6.8' dev tool, resident" in result.msg
    assert f"{KGCOV_DEBUGFS}/otto_kmod_demo/reset" in result.msg
    assert len(host.run_calls) == 1  # the failed write; no delete followed


@pytest.mark.asyncio
async def test_kmod_module_reset_skips_the_debugfs_write_when_unloaded():
    host = _KmodHost(loaded=[])
    p = _build(host, coverage="module", cov_dir="/var/cov/demo")
    await p.reset_coverage(host)
    assert [c for c, _ in host.run_calls] == ["find /var/cov/demo -name '*.gcda' -type f -delete"]


@pytest.mark.asyncio
async def test_kmod_module_reset_propagates_a_dry_run_decline_with_no_delete_following():
    # The reset hook's debugfs write can also be declined under --dry-run
    # (lsmod itself still succeeding); the result must be Status.NotRun, and
    # the delete `run` call that would normally follow must not happen.
    # `run()` always answers a `Results`, so the double wraps its
    # NotRunResult like the real host does.
    host = _KmodHost(loaded=["otto_kmod_demo"])

    async def run(cmd, **kw):
        host.run_calls.append((cmd, kw))
        return Results.collect(
            [NotRunResult(status=Status.NotRun, command=cmd, retcode=-1, host_name=host.id)]
        )

    host.run = run
    p = _build(host, coverage="module", cov_dir="/var/cov/demo")
    result = await p.reset_coverage(host)
    assert result.status is Status.NotRun
    assert len(host.run_calls) == 1  # the reset write only — no delete followed


@pytest.mark.asyncio
async def test_kmod_module_reset_announces_the_write_and_delete_when_lsmod_is_declined():
    # As with prepare_coverage, a declined lsmod leaves residency unknowable —
    # both the reset write and the delete a real run might issue are
    # announced (each declined by the session in turn).
    host = _KmodHost()
    host.lsmod.return_value = NotRunResult(
        status=Status.NotRun, command="cat /proc/modules", retcode=-1, host_name=host.id
    )

    async def run(cmd, **kw):
        host.run_calls.append((cmd, kw))
        return Results.collect(
            [NotRunResult(status=Status.NotRun, command=cmd, retcode=-1, host_name=host.id)]
        )

    host.run = run
    p = _build(host, coverage="module", cov_dir="/var/cov/demo")
    result = await p.reset_coverage(host)
    assert result.status is Status.NotRun
    cmds = [c for c, _ in host.run_calls]
    assert cmds == [
        _expected_module_write_cmd("otto_kmod_demo", "reset"),
        "find /var/cov/demo -name '*.gcda' -type f -delete",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("gcov_path", "cov_dir"),
    [
        ("/sys/kernel/debug/gcov/home/b/demo", "/var/cov/demo"),
        ("/sys/kernel/debug/gcov/mo d$ule/sub dir", "/var/cov/my $demo dir"),
    ],
)
async def test_kmod_kernel_prepare_emits_the_exact_quoted_script(gcov_path, cov_dir):
    host = _KmodHost()
    p = _build(host, coverage="kernel", gcov_path=gcov_path, cov_dir=cov_dir)
    assert (await p.prepare_coverage(host)).is_ok
    ((cmd, kw),) = host.run_calls
    assert kw == {"sudo": True}
    assert cmd == _expected_kernel_prepare_cmd(gcov_path, cov_dir)


@pytest.mark.asyncio
async def test_kmod_kernel_prepare_failure_names_the_missing_path():
    host = _KmodHost(run_status=Status.Error)
    p = _build(host, coverage="kernel", gcov_path="/sys/kernel/debug/gcov/home/b/demo")
    result = await p.prepare_coverage(host)
    assert not result.is_ok
    assert "/sys/kernel/debug/gcov/home/b/demo" in result.msg
    assert "CONFIG_GCOV_KERNEL" in result.msg


@pytest.mark.asyncio
async def test_kmod_kernel_prepare_fails_when_gcov_path_is_missing():
    # Spec §5: a missing gcov_path must fail, naming the path — not succeed
    # with zero files copied. The `test -d` guard is what makes the *shell*
    # script fail; here the double stands in for that shell failure directly.
    host = _KmodHost(run_status=Status.Error, run_value="find: 'x': No such file or directory")
    p = _build(host, coverage="kernel", gcov_path="/sys/kernel/debug/gcov/home/b/demo")
    result = await p.prepare_coverage(host)
    assert not result.is_ok
    assert "/sys/kernel/debug/gcov/home/b/demo" in result.msg


@pytest.mark.asyncio
async def test_kmod_kernel_prepare_propagates_a_dry_run_decline():
    host = _KmodHost()

    async def run(cmd, **kw):
        host.run_calls.append((cmd, kw))
        return Results.collect(
            [NotRunResult(status=Status.NotRun, command=cmd, retcode=-1, host_name=host.id)]
        )

    host.run = run
    p = _build(host, coverage="kernel", gcov_path="/sys/kernel/debug/gcov/home/b/demo")
    result = await p.prepare_coverage(host)
    assert result.status is Status.NotRun


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("gcov_path", "cov_dir"),
    [
        ("/sys/kernel/debug/gcov/home/b/demo", "/var/cov/demo"),
        ("/sys/kernel/debug/gcov/mo d$ule/sub dir", "/var/cov/my $demo dir"),
    ],
)
async def test_kmod_kernel_reset_emits_the_exact_quoted_scripts_never_the_global_reset(
    gcov_path, cov_dir
):
    host = _KmodHost()
    p = _build(host, coverage="kernel", gcov_path=gcov_path, cov_dir=cov_dir)
    assert (await p.reset_coverage(host)).is_ok
    cmds = [c for c, _ in host.run_calls]
    assert cmds == list(_expected_kernel_reset_cmds(gcov_path, cov_dir))
    assert "/sys/kernel/debug/gcov/reset" not in cmds[0]


@pytest.mark.asyncio
async def test_kmod_kernel_reset_propagates_a_dry_run_decline_but_still_deletes():
    # The kernel arm has no separate residency read (no lsmod), so the
    # per-entry reset script's own NotRun is the only decline signal
    # available — it must still fall through to announce the delete a real
    # run would issue, with the overall result mirroring the decline.
    host = _KmodHost()

    async def run(cmd, **kw):
        host.run_calls.append((cmd, kw))
        return Results.collect(
            [NotRunResult(status=Status.NotRun, command=cmd, retcode=-1, host_name=host.id)]
        )

    host.run = run
    gcov_path, cov_dir = "/sys/kernel/debug/gcov/home/b/demo", "/var/cov/demo"
    p = _build(host, coverage="kernel", gcov_path=gcov_path, cov_dir=cov_dir)
    result = await p.reset_coverage(host)
    assert result.status is Status.NotRun
    cmds = [c for c, _ in host.run_calls]
    assert cmds == list(_expected_kernel_reset_cmds(gcov_path, cov_dir))


@pytest.mark.asyncio
async def test_kmod_kernel_reset_error_names_gcov_path():
    host = _KmodHost(run_status=Status.Error, run_value="find: 'x': No such file or directory")
    gcov_path, cov_dir = "/sys/kernel/debug/gcov/home/b/demo", "/var/cov/demo"
    p = _build(host, coverage="kernel", gcov_path=gcov_path, cov_dir=cov_dir)
    result = await p.reset_coverage(host)
    assert not result.is_ok
    assert result.status is not Status.NotRun
    assert gcov_path in result.msg
    assert "find:" in result.msg
    # The script failed outright — no delete follows a failed reset.
    assert len(host.run_calls) == 1
